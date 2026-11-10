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


class _Client:
    def __init__(self, live=True):
        self.live = live


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
        # Like the real start: a live client is kept, a dead one replaced.
        existing = self.running.get(connector_id)
        if existing is None or not existing.live:
            self.events.append(("start", connector_id))
            self.running[connector_id] = _Client()
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
    fakes[a.name].running[CONNECTOR] = _Client()  # A runs it before the move
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


async def test_a_claim_lost_to_this_workers_own_start_keeps_the_connector(pair, monkeypatch):
    a, b, fakes, scheduled = pair
    with a.active():
        ownership.assign("connectors", CONNECTOR, None)
        engine.scan_all()
    converge([a, b])
    for _worker, coro in scheduled:
        coro.close()
    scheduled.clear()
    claimant = None
    for worker in (a, b):  # the worker ``claim`` lets through for an unowned row
        with worker.active():
            if ownership._claimant(engine.require_context()) == worker.device_id:
                claimant = worker
                break
    assert claimant is not None
    fakes[a.name].running.clear()
    fakes[claimant.name].running[CONNECTOR] = _Client()  # boot's start_all claimed and started it

    real_claim = ownership.claim

    def claim_after_start_all(table, row_id):
        assert real_claim(table, row_id)  # start_all's claim lands first ...
        return real_claim(table, row_id)  # ... so the reconcile's own claim changes no row

    monkeypatch.setattr(ownership, "claim", claim_after_start_all)
    with claimant.active():
        await startup._reconcile_owned_connectors([CONNECTOR])
        assert database.get_connector(CONNECTOR)["owner_device_id"] == claimant.device_id
    assert fakes[claimant.name].events == []
    assert CONNECTOR in fakes[claimant.name].running


async def test_a_reconcile_replaces_a_registered_client_that_died(pair):
    a, b, fakes, scheduled = pair
    fakes[a.name].running[CONNECTOR] = _Client(live=False)  # registered, but its login failed
    with a.active():
        await startup._reconcile_owned_connectors([CONNECTOR])
    assert fakes[a.name].events == [("start", CONNECTOR)]
    assert fakes[a.name].running[CONNECTOR].live

    with a.active():  # a healthy client is left alone
        await startup._reconcile_owned_connectors([CONNECTOR])
    assert fakes[a.name].events == [("start", CONNECTOR)]


async def test_outside_a_network_the_reconcile_changes_nothing(workers, monkeypatch):
    solo = workers("worker-solo")
    fake = _FakeManager()
    fake.running[CONNECTOR] = _Client(live=False)
    monkeypatch.setattr(manager_mod, "connector_manager", fake)
    with solo.active():
        database.create_connector(CONNECTOR, "webhook", "Hook", {"mode": "inbound", "inbound_path": "h"},
                                  [], is_active=False)
        assert not ownership.enrolled()
        await startup._reconcile_owned_connectors([CONNECTOR])
        database.delete_connector(CONNECTOR)
        await startup._reconcile_owned_connectors([CONNECTOR])
    assert fake.events == []
