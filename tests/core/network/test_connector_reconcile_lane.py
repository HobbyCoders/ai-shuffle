"""The network connector reconcile decides and hands out RUN/STOP (#1398).

These drive the real reconcile in ``app.network.startup`` (scheduled through
``request_connector_reconcile`` onto the running loop) with a real
``ConnectorManager``; only the connector client is a fake whose connect() and
disconnect() wait on events the test controls. Membership and the rows are
stand-ins: ``state["fenced"]`` is this worker's execution eligibility.
Nothing sleeps for more than a few milliseconds and nothing touches a network.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core import managed_connection
from app.core.connectors import manager as manager_module
from app.core.connectors.manager import ConnectorManager
from app.core.global_events import global_events
from app.db import database
from app.network import (
    engine,
    enrollment,
    hub,
    ownership,
    startup,
    startup_sync,
    transport,
)
from tests.core.connectors.test_connector_lifecycle_lane import (
    GatedConnector,
    clients,
    connecting,
    live_clients,
    open_gates,
    settle,
    until,
)

A, B, UNOWNED = "conn-a", "conn-b", "conn-unowned"


@pytest.fixture(autouse=True)
def _reset_gates():
    GatedConnector.instances = []
    GatedConnector.connect_gates = {}
    GatedConnector.disconnect_gates = {}
    yield
    open_gates()


@pytest.fixture
async def net(monkeypatch):
    """A real manager behind the real reconcile, on this test's loop."""
    state = {"fenced": False}
    rows = {cid: {"id": cid, "type": "fake", "name": cid, "config": {}, "is_active": True,
                  "owner_device_id": "me"} for cid in (A, B)}
    claims = MagicMock(return_value=True)
    reads = MagicMock()

    def decide(row):
        if state["fenced"]:
            return "skip"
        if row.get("owner_device_id") is None:
            return "claim"
        return "start" if row.get("owner_device_id") == "me" else "skip"

    def active_rows():
        reads()
        return [r for r in rows.values() if r.get("is_active")]

    monkeypatch.setattr(database, "get_connector", lambda cid: rows.get(cid))
    monkeypatch.setattr(database, "get_active_connectors", active_rows)
    monkeypatch.setattr(database, "get_all_connectors", lambda: list(rows.values()))
    monkeypatch.setattr(database, "update_connector_status", MagicMock())
    monkeypatch.setattr(ownership, "enrolled", lambda: True)
    monkeypatch.setattr(ownership, "decide_connector", decide)
    monkeypatch.setattr(ownership, "can_execute_local", lambda: not state["fenced"])
    monkeypatch.setattr(ownership, "claim", claims)
    monkeypatch.setattr(ownership, "my_device_id", lambda: "me")
    monkeypatch.setattr(manager_module, "get_connector_class", lambda ctype: GatedConnector)
    monkeypatch.setattr(manager_module, "TEARDOWN_NOTICE_SECONDS", 0.001, raising=False)
    monkeypatch.setattr(manager_module, "SHUTDOWN_SETTLE_SECONDS", 0.05, raising=False)
    monkeypatch.setattr(startup, "RECONCILE_SETTLE_SECONDS", 0.5, raising=False)

    mgr = ConnectorManager()
    monkeypatch.setattr(manager_module, "connector_manager", mgr)
    monkeypatch.setattr(global_events, "_loop", asyncio.get_running_loop())
    # Every reconcile the code schedules, so the test can see which are still pending.
    scheduled = []
    real_schedule = asyncio.run_coroutine_threadsafe

    def spy(coro, loop):
        future = real_schedule(coro, loop)
        scheduled.append(future)
        return future

    monkeypatch.setattr(startup.asyncio, "run_coroutine_threadsafe", spy)
    # The network loops themselves stay off: only the reconcile bookkeeping runs.
    monkeypatch.setattr(transport, "start", list)
    monkeypatch.setattr(transport, "stop", AsyncMock())
    monkeypatch.setattr(hub, "start", list)
    monkeypatch.setattr(hub, "stop", AsyncMock())
    monkeypatch.setattr(startup_sync, "stop", AsyncMock())
    monkeypatch.setattr(enrollment, "resume_enrollment", lambda: True)
    monkeypatch.setattr(managed_connection, "supervise", AsyncMock())
    monkeypatch.setattr(startup, "reconcile_memory", AsyncMock())
    monkeypatch.setattr(engine, "on_applied", lambda listener: (lambda: None))
    yield SimpleNamespace(mgr=mgr, state=state, rows=rows, claims=claims, reads=reads, scheduled=scheduled)
    open_gates()
    for future in scheduled:
        future.cancel()
    tasks = [lane.task for lane in mgr._lanes.values() if lane.task is not None]
    for task in tasks:
        task.cancel()
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)
    with startup._reconcile_guard:
        startup._refused_loop = None
        startup._reconcile_futures.clear()


def pending(n):
    return [f for f in n.scheduled if not f.done()]


async def reconcile(n, connector_ids=None):
    """Schedule one reconcile the way the sync apply does, and let it run."""
    startup.request_connector_reconcile(connector_ids)
    await settle()


async def running(n, *cids):
    """Start the named connectors through the membership pass and wait for them."""
    await reconcile(n)
    await until(lambda: all(n.mgr.get_running_instance(c) is not None for c in cids))


async def test_a_hung_connect_does_not_delay_the_fence_stopping_another(net):
    """Before: the re-admission reconcile awaited A's connect under the shared
    lock, so the fence queued behind it never stopped B."""
    GatedConnector.connect_gates[A] = asyncio.Event()  # A's connect never returns
    await running(net, B)  # the same pass started A, still connecting
    await until(lambda: connecting(A))
    assert len(clients(A)) == 1 and net.mgr.get_running_instance(A) is None

    net.state["fenced"] = True
    await reconcile(net)

    await until(lambda: net.mgr.get_running_instance(B) is None, timeout=1.0)
    await until(lambda: clients(A)[0].connect_cancelled, timeout=1.0)  # the unwanted start was cancelled
    assert pending(net) == []


async def test_a_hung_disconnect_recovers_by_itself(net):
    """Before: the fence reconcile stayed on A's hung disconnect, holding the
    lock, so the re-admission behind it waited; here both return at once and
    A's lane alone carries it to running when the disconnect returns."""
    await running(net, A)
    GatedConnector.disconnect_gates[A] = asyncio.Event()  # the first client will not stop
    net.state["fenced"] = True
    await reconcile(net)
    net.state["fenced"] = False
    await reconcile(net)

    assert pending(net) == []  # no reconcile waits on the hang
    assert len(clients(A)) == 1  # no second client while the first has not stopped
    scheduled = len(net.scheduled)

    GatedConnector.disconnect_gates[A].set()
    await until(lambda: net.mgr.get_running_instance(A) is not None, timeout=1.0)
    assert len(net.scheduled) == scheduled  # no further reconcile was needed
    assert len(live_clients(A)) == 1


async def test_repeated_fence_and_readmission_during_a_hung_stop_do_not_grow(net):
    """Before: every fence and re-admission queued behind the hung stop, so
    pending reconciles grew by two each round."""
    await running(net, A)
    GatedConnector.disconnect_gates[A] = asyncio.Event()
    for _ in range(5):
        net.state["fenced"] = True
        await reconcile(net)
        net.state["fenced"] = False
        await reconcile(net)

    assert pending(net) == []
    assert len(clients(A)) == 1
    assert list(net.mgr._lanes) == [A]
    lanes = [t for t in asyncio.all_tasks() if t.get_name().startswith("connector-lane:")]
    assert len(lanes) == 1
    assert len(startup._reconcile_futures) == 0  # finished reconciles are not kept

    GatedConnector.disconnect_gates[A].set()
    await until(lambda: net.mgr.get_running_instance(A) is not None, timeout=1.0)
    assert len(clients(A)) == 2 and len(live_clients(A)) == 1


async def test_a_reconcile_scheduled_just_before_shutdown_does_nothing_after_it(net):
    """Before: a reconcile scheduled before the network stop still ran after
    ``shutdown_all`` and claimed an unowned connector for a worker going away."""
    net.rows[UNOWNED] = {"id": UNOWNED, "type": "fake", "name": UNOWNED, "config": {},
                         "is_active": True, "owner_device_id": None}
    startup.request_connector_reconcile()  # the app shuts down before it runs
    await startup.stop()
    await net.mgr.shutdown_all()
    await settle()

    net.claims.assert_not_called()
    net.reads.assert_not_called()
    assert list(net.mgr.get_running_connectors()) == []
    assert net.mgr._lanes == {}
    assert pending(net) == []


async def test_a_start_in_flight_at_network_stop_is_settled(net):
    """Before: the reconcile stayed on A's hung connect and was still pending
    after the network stop. Now it has handed A its RUN and returned; the
    network stop is not an app shutdown, so the wanted connect goes on."""
    GatedConnector.connect_gates[A] = asyncio.Event()
    await reconcile(net)  # starts A (connect hangs) and B
    await until(lambda: net.mgr.get_running_instance(B) is not None, timeout=1.0)

    await asyncio.wait_for(startup.stop(), 1.0)

    assert pending(net) == []
    assert startup._reconcile_futures == set()
    assert not net.mgr._closing
    assert not clients(A)[0].connect_cancelled  # a wanted connect is never cancelled
    GatedConnector.connect_gates[A].set()
    await until(lambda: net.mgr.get_running_instance(A) is not None, timeout=1.0)


async def test_a_reconcile_after_stop_is_refused_and_accepted_after_start(net):
    """Before: a reconcile requested after the network stop still ran."""
    await startup.stop()
    startup.request_connector_reconcile()
    await settle()
    assert net.mgr.get_running_instance(A) is None and net.mgr._lanes == {}
    net.reads.assert_not_called()

    await startup.start()
    await reconcile(net)
    await until(lambda: net.mgr.get_running_instance(A) is not None, timeout=1.0)
    await startup.stop()
