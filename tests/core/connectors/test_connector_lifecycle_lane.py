"""ConnectorManager lifecycle lane (#1398): one lane per connector moves its
client toward the latest wanted state, so a connect or disconnect that never
returns cannot hold a lock forever, start a second client, or let a late
finish overwrite a newer client's status.

Fake connectors block on events the test controls; nothing sleeps for more
than a millisecond and nothing touches a network.
"""

import asyncio
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import MagicMock

import pytest

from app.core.connectors import manager as manager_module
from app.core.connectors.base import BaseConnector, ConnectorStatus
from app.core.connectors.manager import ConnectorManager
from app.db import database
from app.network import ownership


class GatedConnector(BaseConnector):
    """connect()/disconnect() wait on per-connector events when one is set."""

    instances: ClassVar[list] = []
    connect_gates: ClassVar[dict] = {}
    disconnect_gates: ClassVar[dict] = {}

    def __init__(self, connector_id, connector_type, config, on_incoming_message,
                 on_permission_response, on_trigger=None):
        super().__init__(connector_id, connector_type, config, on_incoming_message,
                         on_permission_response, on_trigger=on_trigger)
        self.connect_started = False
        self.connect_cancelled = False
        self.disconnect_started = False
        self.disconnected = False
        GatedConnector.instances.append(self)

    async def connect(self):
        self.connect_started = True
        gate = GatedConnector.connect_gates.get(self.connector_id)
        if gate is not None:
            try:
                await gate.wait()
            except asyncio.CancelledError:
                self.connect_cancelled = True
                raise
        self._status = ConnectorStatus.CONNECTED
        return True

    async def disconnect(self):
        self.disconnect_started = True
        gate = GatedConnector.disconnect_gates.get(self.connector_id)
        if gate is not None:
            await gate.wait()
        self._status = ConnectorStatus.DISCONNECTED
        self.disconnected = True

    async def send_message(self, message):
        return True

    async def send_permission_prompt(self, platform_chat_id, prompt):
        return False

    async def send_notification(self, platform_chat_id, event_type, data):
        return False

    async def test_connection(self):
        return {"success": True}


def clients(cid):
    return [c for c in GatedConnector.instances if c.connector_id == cid]


def live_clients(cid):
    """Clients that connected and have not finished disconnecting."""
    return [c for c in clients(cid)
            if c.status == ConnectorStatus.CONNECTED and not c.disconnected]


async def until(condition, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.001)


async def settle():
    for _ in range(20):
        await asyncio.sleep(0)
    await asyncio.sleep(0.005)


@pytest.fixture(autouse=True)
def _reset():
    GatedConnector.instances = []
    GatedConnector.connect_gates = {}
    GatedConnector.disconnect_gates = {}
    yield
    open_gates()


@pytest.fixture
def mgr():
    return ConnectorManager()


@pytest.fixture
def db(monkeypatch):
    rows = {cid: {"id": cid, "type": "fake", "name": cid, "config": {}, "is_active": True}
            for cid in ("c1", "c2")}
    fakes = SimpleNamespace(
        rows=rows,
        get_connector=MagicMock(side_effect=lambda cid: rows.get(cid)),
        update_connector_status=MagicMock(),
    )
    monkeypatch.setattr(database, "get_connector", fakes.get_connector)
    monkeypatch.setattr(database, "update_connector_status", fakes.update_connector_status)
    monkeypatch.setattr(ownership, "enrolled", lambda: False)
    monkeypatch.setattr(ownership, "decide_connector", lambda row: None)
    monkeypatch.setattr(manager_module, "get_connector_class", lambda ctype: GatedConnector)
    # The "previous client has not stopped yet" notice is shown at once here.
    monkeypatch.setattr(manager_module, "TEARDOWN_NOTICE_SECONDS", 0.001, raising=False)
    monkeypatch.setattr(manager_module, "SHUTDOWN_SETTLE_SECONDS", 0.05, raising=False)
    return fakes


def statuses(db, cid):
    return [c.args[1] for c in db.update_connector_status.call_args_list if c.args[0] == cid]


def status_calls(db, cid):
    return [c for c in db.update_connector_status.call_args_list if c.args[0] == cid]


def open_gates():
    for gate in (*GatedConnector.connect_gates.values(), *GatedConnector.disconnect_gates.values()):
        gate.set()


async def cleanup(*tasks):
    open_gates()
    for task in tasks:
        task.cancel()
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)


async def test_late_stop_does_not_overwrite_the_newer_clients_status(mgr, db):
    """A restart's stop whose disconnect finishes late must not stamp
    "disconnected" over the replacement client that connected after it."""
    assert await mgr.start_connector("c1") is True
    first = mgr.get_running_instance("c1")
    gate = GatedConnector.disconnect_gates["c1"] = asyncio.Event()
    stop = asyncio.create_task(mgr.stop_connector("c1"))  # a fence, say
    start = None
    try:
        await until(lambda: first.disconnect_started)
        # The re-admission right after it, from another caller.
        start = asyncio.create_task(mgr.start_connector("c1"))
        await settle()
        # Never two clients: the replacement waits for the old one to stop.
        assert len(live_clients("c1")) <= 1
        gate.set()
        await asyncio.wait_for(stop, 2)
        assert await asyncio.wait_for(start, 2) is True
        second = mgr.get_running_instance("c1")
        assert second is not None and second is not first
        # The new client reports itself connected, as a real one does.
        database.update_connector_status("c1", "connected")
        await settle()
        assert statuses(db, "c1")[-1] == "connected"
    finally:
        await cleanup(*(t for t in (stop, start) if t is not None))


async def test_hung_disconnect_blocks_only_its_connector_and_recovers_by_itself(mgr, db):
    assert await mgr.start_connector("c1") is True
    first = mgr.get_running_instance("c1")
    gate = GatedConnector.disconnect_gates["c1"] = asyncio.Event()
    stop = asyncio.create_task(mgr.stop_connector("c1"))
    await until(lambda: first.disconnect_started)
    # The owner switches it back on while the old client is still stopping.
    start = asyncio.create_task(mgr.start_connector("c1"))
    try:
        await until(lambda: any(c.args[1] == "error" for c in status_calls(db, "c1")))
        await settle()
        assert clients("c1") == [first], "no second client while the first has not stopped"
        error = [c for c in status_calls(db, "c1") if c.args[1] == "error"][-1]
        assert "not stopped" in (error.kwargs.get("error") or "")
        assert not start.done()

        # Another connector is unaffected.
        assert await asyncio.wait_for(mgr.start_connector("c2"), 2) is True
        await asyncio.wait_for(mgr.stop_connector("c2"), 2)
        assert mgr.get_running_instance("c2") is None
        assert statuses(db, "c2")[-1] == "disconnected"

        # The disconnect returns: c1 reaches the wanted state with no new call.
        gate.set()
        await asyncio.wait_for(stop, 2)
        assert await asyncio.wait_for(start, 2) is True
        second = mgr.get_running_instance("c1")
        assert second is not None and second is not first and first.disconnected
        assert statuses(db, "c1")[-1] == "connecting"
    finally:
        await cleanup(stop, start)


async def test_stop_during_connect_cancels_it_and_frees_the_receiver_lock(mgr, db):
    gate = GatedConnector.connect_gates["c1"] = asyncio.Event()
    start = asyncio.create_task(mgr.start_connector("c1"))
    try:
        await until(lambda: clients("c1"))
        [unwanted] = clients("c1")
        await asyncio.wait_for(mgr.stop_connector("c1"), 2)
        assert unwanted.connect_cancelled
        assert await asyncio.wait_for(start, 2) is False
        assert not mgr.receiver_lock("c1").locked()
        assert mgr.get_running_instance("c1") is None
        assert statuses(db, "c1")[-1] == "disconnected"

        del GatedConnector.connect_gates["c1"]
        gate.set()
        assert await asyncio.wait_for(mgr.start_connector("c1"), 2) is True
        assert mgr.get_running_instance("c1") is clients("c1")[-1]
    finally:
        await cleanup(start)


async def test_wanted_slow_connect_is_never_cancelled(mgr, db, monkeypatch):
    gate = GatedConnector.connect_gates["c1"] = asyncio.Event()
    start = asyncio.create_task(mgr.start_connector("c1"))
    try:
        await until(lambda: clients("c1"))
        [slow] = clients("c1")
        # Much later on the manager's clock: no time limit on a wanted connect.
        real = manager_module.time.monotonic
        monkeypatch.setattr(manager_module.time, "monotonic", lambda: real() + 3600)
        mgr.request_run("c1")
        again = asyncio.create_task(mgr.start_connector("c1"))
        assert await asyncio.wait_for(mgr.start_connector("c2"), 2) is True
        await asyncio.wait_for(mgr.stop_connector("c2"), 2)
        await settle()
        assert not slow.connect_cancelled and not start.done() and not again.done()
        assert clients("c1") == [slow]

        gate.set()
        assert await asyncio.wait_for(start, 2) is True
        assert await asyncio.wait_for(again, 2) is True
        assert mgr.get_running_instance("c1") is slow and clients("c1") == [slow]
    finally:
        await cleanup(start)


async def test_requests_while_a_teardown_hangs_do_not_pile_up(mgr, db):
    assert await mgr.start_connector("c1") is True
    first = mgr.get_running_instance("c1")
    gate = GatedConnector.disconnect_gates["c1"] = asyncio.Event()
    callers = [asyncio.create_task(mgr.stop_connector("c1"))]
    try:
        await until(lambda: first.disconnect_started)
        await settle()
        baseline_tasks = len(asyncio.all_tasks())
        # Fence / re-admission flapping, from awaiting callers and from requests.
        for _ in range(3):
            callers.append(asyncio.create_task(mgr.start_connector("c1")))
            callers.append(asyncio.create_task(mgr.stop_connector("c1")))
        await settle()
        assert clients("c1") == [first]
        for _ in range(25):
            mgr.request_run("c1")
            mgr.request_stop("c1")
        mgr.request_run("c1")
        await settle()
        assert clients("c1") == [first]
        assert len(asyncio.all_tasks()) == baseline_tasks + 6  # only the callers' own tasks
        assert len(mgr._lanes) == 1
        sizes = {name: len(value) for name, value in vars(mgr).items() if isinstance(value, dict)}
        assert all(size <= 2 for size in sizes.values()), sizes

        gate.set()
        await asyncio.wait_for(asyncio.gather(*callers), 2)
        await until(lambda: not mgr._lanes)
        # The last request wins: one new client, running.
        assert len(clients("c1")) == 2
        assert mgr.get_running_instance("c1") is clients("c1")[-1]
    finally:
        await cleanup(*callers)


async def test_shutdown_settles_an_inflight_connect_and_refuses_new_starts(mgr, db):
    gate = GatedConnector.connect_gates["c1"] = asyncio.Event()
    start = asyncio.create_task(mgr.start_connector("c1"))
    try:
        await until(lambda: clients("c1"))
        await asyncio.wait_for(mgr.shutdown_all(), 2)
        gate.set()  # the connect would finish now
        await settle()
        assert mgr.get_running_instance("c1") is None
        assert list(mgr.get_running_connectors()) == []
        assert start.done() and start.result() is False
        assert await mgr.start_connector("c2") is False
        assert clients("c2") == []
        assert not mgr.receiver_lock("c1").locked()
    finally:
        await cleanup(start)


async def test_shutdown_disconnects_a_connect_that_finishes_after_closing(mgr, db, monkeypatch):
    """A connect that ignores cancellation and returns late is disconnected,
    not registered."""
    gate = asyncio.Event()

    async def stubborn_connect(self):
        while not gate.is_set():
            try:
                await gate.wait()
            except asyncio.CancelledError:
                self.connect_cancelled = True
        self._status = ConnectorStatus.CONNECTED
        return True

    monkeypatch.setattr(GatedConnector, "connect", stubborn_connect)
    start = asyncio.create_task(mgr.start_connector("c1"))
    try:
        await until(lambda: clients("c1"))
        [client] = clients("c1")
        shutdown = asyncio.create_task(mgr.shutdown_all())
        await until(lambda: client.connect_cancelled)
        gate.set()
        await asyncio.wait_for(shutdown, 2)
        await settle()
        assert mgr.get_running_instance("c1") is None
        assert client.disconnected
    finally:
        gate.set()
        await cleanup(start)


async def test_a_lane_cancelled_before_its_first_step_answers_its_waiters(mgr, db):
    """A task cancelled before it first runs never enters its ``finally``;
    the lane's done-callback still forgets it and answers the waiting start."""
    start = asyncio.create_task(mgr.start_connector("c1"))
    await asyncio.sleep(0)  # the start has asked; its lane has not run yet
    lane = mgr._lanes["c1"]
    lane.task.cancel()
    assert await asyncio.wait_for(start, 1) is False
    assert "c1" not in mgr._lanes
    assert clients("c1") == []


async def test_a_stop_during_the_connecting_write_abandons_the_start(mgr, db, monkeypatch):
    """Before: a STOP that came while the "connecting" status write was in
    flight found no connect to cancel, so the lane then began the unwanted
    connect; hung there, the stop never returned and the receiver lock stayed held."""
    GatedConnector.connect_gates["c1"] = asyncio.Event()  # a connect would never return
    paused, release = asyncio.Event(), asyncio.Event()
    real_run_sync = manager_module.run_sync

    async def run_sync(fn, *args, **kwargs):
        if fn is database.update_connector_status and args[1:2] == ("connecting",):
            paused.set()
            await release.wait()
        return await real_run_sync(fn, *args, **kwargs)

    monkeypatch.setattr(manager_module, "run_sync", run_sync)
    start = asyncio.create_task(mgr.start_connector("c1"))
    stop = None
    try:
        await asyncio.wait_for(paused.wait(), 2)
        stop = asyncio.create_task(mgr.stop_connector("c1"))
        await settle()
        release.set()
        await asyncio.wait_for(stop, 1)
        assert await asyncio.wait_for(start, 1) is False
        [built] = clients("c1")
        assert not built.connect_started and not built.disconnect_started
        assert not mgr.receiver_lock("c1").locked()
        assert mgr.get_running_instance("c1") is None and "c1" not in mgr._lanes
        assert statuses(db, "c1")[-1] == "disconnected"
    finally:
        release.set()
        await cleanup(*(t for t in (start, stop) if t is not None))


async def test_a_failed_not_stopped_notice_does_not_end_the_teardown(mgr, db):
    """Before: when the "previous client has not stopped" write raised, the
    teardown's disconnect was cancelled and the queued start built a second
    client while the first had not stopped."""
    assert await mgr.start_connector("c1") is True
    first = mgr.get_running_instance("c1")
    gate = GatedConnector.disconnect_gates["c1"] = asyncio.Event()
    notices = []

    def update_status(cid, status, **fields):
        if fields.get("error") == manager_module.PREVIOUS_CLIENT_NOT_STOPPED_MESSAGE:
            notices.append(cid)
            raise RuntimeError("database unavailable")

    db.update_connector_status.side_effect = update_status
    stop = asyncio.create_task(mgr.stop_connector("c1"))
    start = None
    try:
        await until(lambda: first.disconnect_started)
        start = asyncio.create_task(mgr.start_connector("c1"))
        await until(lambda: notices)
        await settle()
        assert clients("c1") == [first], "no second client while the first has not stopped"
        assert not stop.done() and not start.done()

        gate.set()  # the disconnect returns: the lane carries on by itself
        await asyncio.wait_for(stop, 2)
        assert await asyncio.wait_for(start, 2) is True
        assert first.disconnected and len(live_clients("c1")) == 1
        assert notices == ["c1"]  # tried once, not in a loop
    finally:
        await cleanup(*(t for t in (stop, start) if t is not None))


async def test_a_restart_is_not_swallowed_by_a_run_asked_before_its_stop_ran(mgr, db):
    """Before: a reconcile's RUN landing before the lane ran a restart's STOP
    collapsed both into "keep the client", so the restart did not restart."""
    assert await mgr.start_connector("c1") is True
    first = mgr.get_running_instance("c1")
    restart = asyncio.create_task(mgr.restart_connector("c1"))
    await asyncio.sleep(0)  # the restart has asked STOP; its lane has not run yet
    assert "c1" in mgr._lanes
    mgr.request_run("c1")  # a reconcile, before the lane's first step
    try:
        assert await asyncio.wait_for(restart, 2) is True
        assert first.disconnected
        second = mgr.get_running_instance("c1")
        assert second is not None and second is not first
        assert len(live_clients("c1")) == 1
    finally:
        await cleanup(restart)
