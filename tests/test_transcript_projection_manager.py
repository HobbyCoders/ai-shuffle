from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock

import pytest

from app.core.event_gateway import EventGateway
from app.core.transcript_views import manager
from app.core.transcript_views.bridge import ReducerUnavailable


@pytest.fixture(autouse=True)
def producer_opted_in(monkeypatch):
    monkeypatch.setenv('AI_SHUFFLE_TRANSCRIPT_PRODUCER', '1')


class Bridge:
    def __init__(self):
        self.closed = False
    async def request(self, *args, **kwargs):
        return []
    async def close(self):
        self.closed = True


class Gateway:
    def __init__(self):
        self.listeners = []
        self.ready = asyncio.Event()
    def add_global_listener(self, listener):
        self.listeners.append(listener)
        self.ready.set()
    def remove_global_listener(self, listener):
        self.listeners.remove(listener)


class Actor:
    def __init__(self):
        self.processed = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.forgotten = asyncio.Event()
        self.burst_done = asyncio.Event()
    async def process(self, session_id):
        self.processed.append(session_id)
        self.entered.set()
        if len(self.processed) >= manager.LIVE_QUEUE + 1:
            self.burst_done.set()
        if session_id == 'forgotten':
            self.forgotten.set()
        await self.release.wait()
        return False


def instance(monkeypatch, *, scan_seconds=10):
    monkeypatch.setattr(manager.store, 'initialize', lambda: None)
    monkeypatch.setattr(manager.work, 'source_sessions', lambda after: [])
    monkeypatch.setattr(manager.work, 'projection_sources', lambda after: [])
    monkeypatch.setattr(manager.work, 'collect_garbage', lambda: False)
    bridge, gateway, actor = Bridge(), Gateway(), Actor()
    producer = manager.ProjectionProducer(bridge=bridge, gateway=gateway, actor=actor, scan_seconds=scan_seconds)
    return producer, bridge, gateway, actor


def test_producer_is_default_off_without_creating_a_task(monkeypatch):
    monkeypatch.delenv('AI_SHUFFLE_TRANSCRIPT_PRODUCER', raising=False)
    producer, bridge, gateway, actor = instance(monkeypatch)
    assert not manager.enabled()
    producer.start()
    assert producer._task is None
    assert producer.wake('disabled') is False
    assert gateway.listeners == []
    assert actor.processed == []


@pytest.mark.asyncio
async def test_notify_storm_is_bounded_before_scheduling_and_never_awaits_actor(monkeypatch):
    producer, bridge, gateway, actor = instance(monkeypatch)
    loop = asyncio.get_running_loop()
    original = loop.call_soon_threadsafe
    scheduled = []
    def schedule(callback, *args, **kwargs):
        if getattr(callback, '__self__', None) is producer:
            scheduled.append(args)
        return original(callback, *args, **kwargs)
    monkeypatch.setattr(loop, 'call_soon_threadsafe', schedule)
    producer.start()
    try:
        await asyncio.wait_for(gateway.ready.wait(), 1)
        # Invoke the real gateway callback shape from a different thread while
        # this loop cannot consume admissions. No DB/actor call is in the hint.
        def storm():
            for _ in range(10000):
                for index in range(40):
                    gateway.listeners[0](f'live-{index}')
        await asyncio.to_thread(storm)
        await asyncio.wait_for(actor.entered.wait(), 1)
        assert len(scheduled) <= manager.LIVE_QUEUE + 1
        assert producer.stats()['max_pending'] <= manager.LIVE_QUEUE
        assert producer.stats()['dropped_hints'] > 0
        assert len(actor.processed) == 1
    finally:
        await producer.stop()
    assert bridge.closed
    assert gateway.listeners == []


@pytest.mark.asyncio
async def test_backfill_has_its_own_capacity_and_makes_progress_after_live_burst(monkeypatch):
    producer, bridge, gateway, actor = instance(monkeypatch)
    actor.release.set()
    producer.start()
    try:
        # No await between admission calls: both physical queues are fully
        # exercised before the worker gets a turn to consume either one.
        assert all(producer.wake(f'live-{i}') for i in range(manager.LIVE_QUEUE))
        assert all(producer.wake(f'bulk-{i}', backfill=True) for i in range(manager.BACKFILL_QUEUE))
        assert producer.wake('too-many-live') is False
        assert producer.wake('too-many-bulk', backfill=True) is False
        await asyncio.wait_for(gateway.ready.wait(), 1)
        await asyncio.wait_for(actor.burst_done.wait(), 1)
        assert actor.processed[manager.LIVE_QUEUE].startswith('bulk-')
        assert producer.max_pending == manager.LIVE_QUEUE + manager.BACKFILL_QUEUE
    finally:
        await producer.stop()
    assert bridge.closed


@pytest.mark.asyncio
async def test_bounded_round_robin_scan_repairs_a_lost_notify(monkeypatch):
    producer, _, gateway, actor = instance(monkeypatch, scan_seconds=.01)
    actor.release.set()
    monkeypatch.setattr(manager.work, 'source_sessions', lambda after: ['forgotten'] if not after else [])
    producer.start()
    try:
        await asyncio.wait_for(gateway.ready.wait(), 1)
        await asyncio.wait_for(actor.forgotten.wait(), 1)
        assert 'forgotten' in actor.processed
    finally:
        await producer.stop()


@pytest.mark.asyncio
async def test_disabled_producer_has_no_child_database_or_gateway_work(monkeypatch):
    monkeypatch.setenv('AI_SHUFFLE_TRANSCRIPT_PRODUCER', '0')
    touched = AsyncMock(side_effect=AssertionError('disabled producer did work'))
    monkeypatch.setattr(manager, 'database', touched)
    producer = manager.ProjectionProducer()
    producer.start()
    await producer.stop()
    assert producer._task is None
    touched.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_node_is_a_nonfatal_startup_fallback(monkeypatch):
    producer, bridge, gateway, _ = instance(monkeypatch)
    bridge.request = AsyncMock(side_effect=ReducerUnavailable('node_missing'))
    producer.start()
    await asyncio.wait_for(asyncio.shield(producer._task), 1)
    assert producer.last_error == 'node_missing'
    assert bridge.closed
    assert gateway.listeners == []
    assert producer.wake('after-stop') is False


@pytest.mark.asyncio
async def test_stop_before_run_starts_still_closes_owned_bridge(monkeypatch):
    monkeypatch.setenv('AI_SHUFFLE_TRANSCRIPT_PRODUCER', '1')
    bridge, gateway = Bridge(), Gateway()
    producer = manager.ProjectionProducer(bridge=bridge, actor=Actor(), gateway=gateway)
    producer.start()
    # Deliberately do not yield: the task's own try/finally has not run yet.
    await producer.stop()
    assert bridge.closed
    assert gateway.listeners == []
    producer.start()
    assert producer._task.done()
    assert producer.wake('after-stop') is False


@pytest.mark.asyncio
@pytest.mark.parametrize('worker_failure', [False, True])
async def test_concurrent_stops_join_blocked_database_work_before_closing_bridge(
        monkeypatch, worker_failure):
    from app.core.transcript_views.producer import database

    producer, bridge, gateway, _ = instance(monkeypatch)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    finished_at_close = []
    close = bridge.close

    def transaction():
        entered.set()
        try:
            assert release.wait(5), 'fixture transaction was not released'
            if worker_failure:
                raise ValueError('fixture transaction failed')
            return False
        finally:
            finished.set()

    class BlockedActor:
        async def process(self, session_id):
            return await database(transaction)

    async def close_after_join():
        finished_at_close.append(finished.is_set())
        await close()

    producer.actor = BlockedActor()
    bridge.close = close_after_join
    stops = []
    producer.start()
    try:
        await asyncio.wait_for(gateway.ready.wait(), 2)
        assert producer.wake('blocked-transaction')
        assert await asyncio.to_thread(entered.wait, 2)
        stops.append(asyncio.create_task(producer.stop()))
        # Let the first cancellation reach the DB join before issuing another.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        stops.append(asyncio.create_task(producer.stop()))
        # Drain the cancellation callbacks, not a wall-clock transaction delay.
        for _ in range(10):
            await asyncio.sleep(0)
        assert producer._task.cancelling() >= 2
        assert not finished.is_set()
        assert all(not stop.done() for stop in stops)
        assert not bridge.closed
        assert gateway.listeners == [producer.wake]
        release.set()
        await asyncio.wait_for(asyncio.gather(*stops), 2)
        assert finished.is_set()
        assert bridge.closed
        assert finished_at_close and all(finished_at_close)
        assert gateway.listeners == []
        # Cancellation remains cancellation, even when the joined worker fails.
        assert producer.last_error is None
    finally:
        release.set()
        if stops:
            await asyncio.wait_for(asyncio.gather(*stops, return_exceptions=True), 2)
        await producer.stop()


def test_global_listener_ownership_can_be_released_without_touching_subscribers(monkeypatch):
    gateway = EventGateway()
    monkeypatch.setattr(gateway, '_ensure_listener', lambda: None)
    seen = []
    callback = seen.append
    gateway.add_global_listener(callback)
    gateway.add_global_listener(callback)
    assert gateway._global_listeners == [callback]
    gateway.remove_global_listener(callback)
    gateway.remove_global_listener(callback)
    assert gateway._global_listeners == []
    assert gateway._subscribers == {}
