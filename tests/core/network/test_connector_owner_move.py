"""Moving a connector to another worker moves its live client (#1316).

The move is the real ``POST /api/v1/network/owners`` route on one worker and
the real sync apply on the other. The worker that lost the connector must
disconnect it and the one that gained it must connect it, without a restart.
Only the connector client is a stand-in: each worker gets a fake manager that
records starts and stops.
"""
import asyncio

import pytest

from app.api import network as network_api
from app.core.connectors import manager as manager_mod
from app.core.global_events import global_events
from app.db import database
from app.network import engine, ownership, startup
from tests.core.network.harness import _ACTIVE, converge, exchange, join

CONNECTOR = "conn-move-1316"


class _FakeManager:
    def __init__(self):
        self.running = {}
        self.events = []

    def get_running_connectors(self, connector_type=None):
        yield from list(self.running.items())

    def get_running_instance(self, connector_id):
        return self.running.get(connector_id)

    async def start_connector(self, connector_id):
        # The real start re-reads the row and refuses one owned elsewhere.
        if ownership.decide_connector(database.get_connector(connector_id)) != "start":
            return False
        if connector_id not in self.running:
            self.events.append(("start", connector_id))
            self.running[connector_id] = object()
        return True

    async def stop_connector(self, connector_id):
        self.events.append(("stop", connector_id))
        self.running.pop(connector_id, None)


class _PerWorker:
    """``connector_manager`` resolves to whichever worker is active."""

    def __init__(self, fakes):
        self.fakes = fakes

    def __getattr__(self, name):
        return getattr(self.fakes[_ACTIVE.get().name], name)


@pytest.fixture
def pair(workers, monkeypatch):
    a, b = workers("worker-a"), workers("worker-b")
    with a.active():
        engine.create_network("Home", device_name="Worker A")
    join(a, b)
    with a.active():
        database.create_connector(CONNECTOR, "webhook", "Hook", {"mode": "inbound", "inbound_path": "h"},
                                  [], is_active=True)
        ownership.assign("connectors", CONNECTOR, a.device_id)
        engine.scan_all()
    converge([a, b])
    fakes = {a.name: _FakeManager(), b.name: _FakeManager()}
    fakes[a.name].running[CONNECTOR] = object()  # A runs it before the move
    monkeypatch.setattr(manager_mod, "connector_manager", _PerWorker(fakes))

    scheduled = []

    def schedule(coro, loop):
        scheduled.append((_ACTIVE.get(), coro))

    class _AppLoop:  # the app loop the scheduler hands work to; captured above
        def is_closed(self):
            return False

    monkeypatch.setattr(global_events, "_loop", _AppLoop())
    monkeypatch.setattr(startup.asyncio, "run_coroutine_threadsafe", schedule)
    unsubscribe = engine.on_applied(startup._on_applied)
    yield a, b, fakes, scheduled
    unsubscribe()


async def _run(scheduled, *, reverse=False):
    """Run what the apply and the route scheduled, as the worker that scheduled it."""
    batch = list(reversed(scheduled)) if reverse else list(scheduled)
    scheduled.clear()
    for worker, coro in batch:
        with worker.active():
            await coro


async def _move(worker, device_id):
    with worker.active():
        return await network_api.assign_owner(
            network_api.OwnerAssignment(table="connectors", id=CONNECTOR, device_id=device_id), "owner")


async def test_move_starts_on_the_new_owner_and_stops_on_the_old_one(pair):
    a, b, fakes, scheduled = pair

    # The move is made on A, the worker losing it.
    assert (await _move(a, b.device_id))["owner_device_id"] == b.device_id
    await _run(scheduled)
    assert fakes[a.name].events == [("stop", CONNECTOR)]
    assert fakes[b.name].events == []  # B has not seen the move yet

    exchange(a, b)  # the real sync apply on B schedules its reconcile
    assert [w.name for w, _coro in scheduled] == [b.name]
    assert fakes[b.name].events == []  # the apply only scheduled it: a slow connect cannot hold it up
    await _run(scheduled)
    assert CONNECTOR in fakes[b.name].running
    assert CONNECTOR not in fakes[a.name].running

    # Back to A, made on A, the worker gaining it: A starts locally, B stops on apply.
    await _move(a, a.device_id)
    await _run(scheduled)
    assert CONNECTOR in fakes[a.name].running
    exchange(a, b)
    await _run(scheduled)
    assert fakes[b.name].events == [("start", CONNECTOR), ("stop", CONNECTOR)]
    assert CONNECTOR not in fakes[b.name].running


async def test_the_old_owner_stops_when_the_move_arrives_by_sync(pair):
    a, b, fakes, scheduled = pair

    await _move(b, b.device_id)  # made on B, the worker gaining it
    await _run(scheduled)
    assert CONNECTOR in fakes[b.name].running
    assert CONNECTOR in fakes[a.name].running  # A has not heard yet

    exchange(b, a)
    assert [w.name for w, _coro in scheduled] == [a.name]
    await _run(scheduled)
    assert fakes[a.name].events == [("stop", CONNECTOR)]
    assert CONNECTOR not in fakes[a.name].running


async def test_repeated_and_out_of_order_reconciles_follow_the_latest_row(pair):
    a, b, fakes, scheduled = pair

    for owner in (b, a, b):
        await _move(a, owner.device_id)
        exchange(a, b)
    # A's and B's reconciles for three moves, run newest first: each reads the
    # committed row, so the final owner is the one that holds.
    await _run(scheduled, reverse=True)
    assert CONNECTOR in fakes[b.name].running
    assert CONNECTOR not in fakes[a.name].running

    exchange(a, b)  # nothing new: no reconcile, nothing changes
    await _run(scheduled)
    assert CONNECTOR in fakes[b.name].running


async def test_switching_off_or_deleting_on_another_worker_stops_the_owner(pair):
    a, b, fakes, scheduled = pair
    await _move(b, b.device_id)
    exchange(b, a)
    await _run(scheduled)
    assert list(fakes[b.name].running) == [CONNECTOR] and not fakes[a.name].running

    with a.active():
        database.update_connector(CONNECTOR, is_active=False)
        engine.scan_all()
    exchange(a, b)
    await _run(scheduled)
    assert not fakes[b.name].running

    with a.active():
        database.update_connector(CONNECTOR, is_active=True)
        engine.scan_all()
    exchange(a, b)
    await _run(scheduled)
    assert CONNECTOR in fakes[b.name].running

    with a.active():
        database.delete_connector(CONNECTOR)
        engine.scan_all()
    exchange(a, b)
    await _run(scheduled)
    assert not fakes[b.name].running
