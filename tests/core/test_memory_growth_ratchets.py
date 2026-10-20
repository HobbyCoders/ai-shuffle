"""Regression ratchets for issue #264; no real DB, CLI, or worker access."""
import asyncio
import gc
import weakref

import pytest

from app.core.event_gateway import EventGateway
from app.core.git_cache import _GitResultCache
from app.core.session_runtime import get_reader_lock, _reader_locks


@pytest.fixture
def gateway(monkeypatch):
    result = EventGateway()
    monkeypatch.setattr(result, "_ensure_listener", lambda: None)
    return result


async def test_gateway_bounds_cross_thread_callbacks_and_slow_reader(gateway):
    queue = gateway.subscribe("slow")
    loop = asyncio.get_running_loop()
    ready_before = len(loop._ready)
    for seq in range(10000):
        gateway._dispatch("slow", seq)
    assert len(loop._ready) - ready_before <= 1
    await asyncio.sleep(0)
    assert queue.qsize() == 1
    assert queue.get_nowait() == 9999
    # Already-delivered but not consumed ticks must coalesce too.
    for seq in range(10):
        gateway._dispatch("slow", seq)
        await asyncio.sleep(0)
    assert queue.qsize() == 1
    assert queue.get_nowait() == 9
    gateway.unsubscribe("slow", queue)


async def test_gateway_keeps_force_refresh_when_coalescing(gateway):
    queue = gateway.subscribe("slow")
    kick = object()  # SSE tail's private _KICK sentinel
    queue.put_nowait(kick)
    gateway._dispatch("slow", 10)
    await asyncio.sleep(0)
    assert queue.qsize() == 1
    assert queue.get_nowait() is kick
    gateway._dispatch("slow", 11)
    await asyncio.sleep(0)
    queue.put_nowait(kick)
    assert queue.get_nowait() is kick
    gateway.unsubscribe("slow", queue)


async def test_unsubscribe_discards_an_already_scheduled_delivery(gateway):
    queue = gateway.subscribe("closed")
    gateway._dispatch("closed", 10)
    gateway.unsubscribe("closed", queue)
    await asyncio.sleep(0)
    assert queue.empty()
    assert not gateway._subscribers


async def test_reader_tokens_release_without_breaking_waiters():
    before = len(_reader_locks)
    for i in range(1000):
        get_reader_lock(f"memory-ratchet-{i}")
    gc.collect()
    assert len(_reader_locks) == before
    first = get_reader_lock("contended-memory-ratchet")
    ref = weakref.ref(first)
    await first.acquire()
    second = get_reader_lock("contended-memory-ratchet")
    assert second is first
    acquired = asyncio.create_task(second.acquire())
    await asyncio.sleep(0)
    del second
    first.release()
    await acquired
    assert get_reader_lock("contended-memory-ratchet") is first
    first.release()
    del first, acquired
    gc.collect()
    assert ref() is None


def test_git_cache_bounded_without_periodic_monitor():
    cache = _GitResultCache()
    for i in range(2000):
        cache.put(f"status:{i}", {"untracked": [str(i) + "x" * 20000]})
    assert len(cache._data) <= 256
    assert cache._data.current_bytes <= 8 * 1024 * 1024


def test_git_expiry_releases_payload(monkeypatch):
    import app.core.git_cache as module
    now = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    cache = _GitResultCache()
    cache.put("status:old", {"untracked": ["large"]})
    now[0] += 3
    assert cache.get("status:old", 2) is None
    assert len(cache._data) == 0


def test_git_oversize_result_is_returnable_but_not_retained():
    cache = _GitResultCache()
    cache.put("status:big", "previous")
    cache.put("status:big", {"untracked": ["x" * (3 * 1024 * 1024)]})
    assert cache.get("status:big", 2) is None


def test_git_cache_invalidation_and_falsy_values(monkeypatch):
    import app.core.git_cache as module
    now = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    cache = _GitResultCache()
    cache.put("status:a", False)
    cache.put("status:b", [])
    cache.put("branch:c", "main")
    assert cache.get("status:a", 2) is False
    assert cache.get("status:b", 2) == []
    cache.invalidate_prefix("status:")
    assert len(cache._data) == 1
    now[0] += 301
    assert cache.purge_expired() == 1
    assert len(cache._data) == 0


def test_task_census_is_bounded_and_does_not_expose_task_names():
    from types import SimpleNamespace
    from app.core.resource_monitor import _task_census
    tasks = [SimpleNamespace(get_coro=lambda i=i: SimpleNamespace(
        cr_code=SimpleNamespace(co_qualname=f"coroutine-{i}"))) for i in range(50)]
    counts = _task_census(tasks)
    assert len(counts) == 21  # top 20 plus remainder
    assert sum(counts.values()) == 50


async def test_retention_metrics_are_observe_only(gateway, monkeypatch):
    from app.core import event_gateway, resource_monitor
    monkeypatch.setattr(event_gateway, "_gateway", gateway)
    queue = gateway.subscribe("sensitive-session-id")
    gateway._dispatch("sensitive-session-id", 20)
    metrics = resource_monitor._retention_metrics()
    assert metrics["event_gateway_pending_ticks"] == 1
    assert metrics["event_gateway_subscribers"] == 1
    assert "sensitive-session-id" not in repr(metrics)
    assert "store_parse_cache_estimated_bytes" in metrics
    await asyncio.sleep(0)
    assert queue.get_nowait() == 20  # observation did not drain the queue
    gateway.unsubscribe("sensitive-session-id", queue)
