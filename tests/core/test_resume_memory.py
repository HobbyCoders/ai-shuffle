"""General heap reclamation is off-loop, observable and never evicts live data."""
import asyncio
import logging
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core import resume_memory as memory
from app.core.provider.fluid_pg_store import PgFluidStore
from app.core.provider.harness_adapter import HarnessAdapter
from fluid.events import TurnComplete
from fluid.messages import Message, Text
from fluid.store.base import SessionNode

MIB = 1024 * 1024


@pytest.fixture(autouse=True)
def reset_reclaimer(monkeypatch):
    monkeypatch.setattr(memory, "_last_check", None)
    monkeypatch.setattr(memory, "_last_trim_state", None)
    functions = memory._glibc_functions
    functions.cache_clear()
    yield
    functions.cache_clear()


def allocator(monkeypatch, *, free=128 * MIB, before=400 * MIB, after=200 * MIB):
    state = SimpleNamespace(free=free, rss=before, trims=0)
    def trim(padding):
        assert padding == 0
        state.trims += 1
        state.rss = after
        return 1
    monkeypatch.setattr(memory, "_glibc_functions", lambda: (
        lambda: SimpleNamespace(fordblks=state.free), trim))
    monkeypatch.setattr(memory.psutil, "Process", lambda: SimpleNamespace(
        memory_info=lambda: SimpleNamespace(rss=state.rss)))
    return state


@pytest.mark.asyncio
async def test_reclaim_is_off_loop_and_cold_hint_uses_same_worker(monkeypatch):
    monkeypatch.setattr(memory.sys, "platform", "linux")
    worker = AsyncMock(return_value=True)
    monkeypatch.setattr(memory.asyncio, "to_thread", worker)
    assert await memory.release_unused_memory()
    assert await memory.release_cold_resume_memory()
    assert worker.await_count == 2
    worker.assert_awaited_with(memory._trim_glibc)


def test_process_wide_budget_and_unchanged_trimmed_arenas_skip(monkeypatch, caplog):
    state = allocator(monkeypatch)
    clock = [0.0]
    monkeypatch.setattr(memory.time, "monotonic", lambda: clock[0])
    with caplog.at_level(logging.INFO):
        assert memory._trim_glibc()
    assert "reclaimed_bytes=209715200" in caplog.text
    assert "free_heap_bytes=134217728" in caplog.text
    assert "trim_ms=" in caplog.text
    for _ in range(20):
        assert not memory._trim_glibc()
    clock[0] = 60.0
    # glibc still owns the address space after madvise: do not trim it again.
    assert not memory._trim_glibc()
    assert state.trims == 1
    clock[0] = 120.0
    state.free += 64 * MIB  # freed a live allocation without increasing RSS
    assert memory._trim_glibc()
    clock[0] = 180.0
    state.rss += 64 * MIB  # reused previously madvised pages
    assert memory._trim_glibc()
    clock[0] = 240.0
    state.rss -= 64 * MIB  # a smaller live set must not pin an old RSS baseline
    assert memory._trim_glibc()
    assert state.trims == 4


def test_small_free_heap_skips_and_checks_are_rate_limited(monkeypatch):
    state = allocator(monkeypatch, free=MIB)
    clock = [0.0]
    monkeypatch.setattr(memory.time, "monotonic", lambda: clock[0])
    assert not memory._trim_glibc()
    state.free = 64 * MIB
    clock[0] = 59.0
    assert not memory._trim_glibc()
    clock[0] = 60.0
    assert memory._trim_glibc()
    assert state.trims == 1


@pytest.mark.asyncio
async def test_cancelled_caller_cannot_allow_an_overlapping_trim(monkeypatch):
    allocator(monkeypatch)
    monkeypatch.setattr(memory.sys, "platform", "linux")
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    def trim(_):
        entered.set()
        assert release.wait(2)
        return 1
    monkeypatch.setattr(memory, "_glibc_functions", lambda: (
        lambda: SimpleNamespace(fordblks=128 * MIB), trim))
    original = memory._trim_glibc
    def worker():
        try:
            return original()
        finally:
            finished.set()
    monkeypatch.setattr(memory, "_trim_glibc", worker)
    task = asyncio.create_task(memory.release_unused_memory())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Even after the budget expired, the still-running worker holds the lock.
        monkeypatch.setattr(memory, "_last_check", None)
        assert not original()
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["win32", "darwin"])
async def test_other_platforms_do_not_load_libc(monkeypatch, platform):
    monkeypatch.setattr(memory.sys, "platform", platform)
    worker = AsyncMock()
    monkeypatch.setattr(memory.asyncio, "to_thread", worker)
    assert not await memory.release_unused_memory()
    await memory.maintain_heap_memory()
    worker.assert_not_awaited()


@pytest.mark.parametrize("missing", ["gnu_get_libc_version", "mallinfo2", "malloc_trim"])
def test_missing_libc_symbols_are_safe_noops(monkeypatch, missing):
    symbols = dict(gnu_get_libc_version=lambda: b"glibc",
                   mallinfo2=lambda: None, malloc_trim=lambda _: 1)
    symbols.pop(missing)
    monkeypatch.setattr(memory.ctypes, "CDLL", lambda _: SimpleNamespace(**symbols))
    assert not memory._trim_glibc()


def test_libc_abi_and_loading_failure(monkeypatch):
    def unavailable(_):
        raise OSError("libc unavailable")
    monkeypatch.setattr(memory.ctypes, "CDLL", unavailable)
    assert memory._glibc_functions() is None
    memory._glibc_functions.cache_clear()
    info, trim = lambda: None, lambda _: 1
    monkeypatch.setattr(memory.ctypes, "CDLL", lambda _: SimpleNamespace(
        gnu_get_libc_version=lambda: b"glibc", mallinfo2=info, malloc_trim=trim))
    assert memory._glibc_functions() == (info, trim)
    assert info.argtypes == [] and info.restype is memory._Mallinfo2
    assert trim.argtypes == [memory.ctypes.c_size_t]
    assert trim.restype is memory.ctypes.c_int


@pytest.mark.asyncio
async def test_background_sweep_needs_no_resume_hint_and_cancels(monkeypatch):
    monkeypatch.setattr(memory.sys, "platform", "linux")
    sleep = AsyncMock(side_effect=[None, asyncio.CancelledError])
    reclaim = AsyncMock(return_value=False)
    monkeypatch.setattr(memory.asyncio, "sleep", sleep)
    monkeypatch.setattr(memory, "release_unused_memory", reclaim)
    with pytest.raises(asyncio.CancelledError):
        await memory.maintain_heap_memory()
    reclaim.assert_awaited_once_with()
    assert all(call.args == (60.0,) for call in sleep.await_args_list)


def test_zero_reclaim_is_observable_and_concurrent_growth_is_not_negative(monkeypatch, caplog):
    allocator(monkeypatch, before=200 * MIB, after=201 * MIB)
    info, grow = memory._glibc_functions()
    def trim(padding):
        grow(padding)
        return 0
    monkeypatch.setattr(memory, "_glibc_functions", lambda: (info, trim))
    with caplog.at_level(logging.INFO):
        assert not memory._trim_glibc()
    assert "reclaimed_bytes=0" in caplog.text
    assert "trim_ms=" in caplog.text


@pytest.mark.asyncio
async def test_unavailable_rss_is_best_effort(monkeypatch):
    monkeypatch.setattr(memory.sys, "platform", "linux")
    monkeypatch.setattr(memory.asyncio, "to_thread", AsyncMock(side_effect=OSError))
    assert not await memory.release_unused_memory()


@pytest.mark.asyncio
async def test_only_large_cold_load_then_prefix_eviction_reclaims(mock_db, monkeypatch):
    release = AsyncMock()
    monkeypatch.setattr(memory, "release_cold_resume_memory", release)
    hot = PgFluidStore()
    await hot.create_session("large-resume")
    parent = None
    for key, text in [("root", "root"), ("old", "x" * (9 * 1024 * 1024)),
                      ("end", "end"), ("tail", "tail")]:
        await hot.append("large-resume", SessionNode(node_id=key, parent_id=parent,
            message=Message(role="user", content=[Text(text)])))
        parent = key
    await hot.evict_prefix("large-resume", "end", {"root"})
    await hot.release_resume_memory()
    release.assert_not_awaited()  # A growing live session is not a cold load.

    cold = PgFluidStore()
    assert await cold.get_session("large-resume")
    assert cold._resume_trim_pending
    await cold.release_resume_memory()
    release.assert_not_awaited()  # Nothing has been evicted yet.
    await cold.evict_prefix("large-resume", "end", {"root"})
    await cold.release_resume_memory()
    await cold.release_resume_memory()
    release.assert_awaited_once()
    assert cold.cache_stats()["retained_nodes"] == 3
    assert len((await cold.get_node("large-resume", "old")).message.text()) == 9 * 1024 * 1024
    small = PgFluidStore()
    await small.create_session("small")
    small_cold = PgFluidStore()
    assert await small_cold.get_session("small")
    assert not small_cold._resume_trim_pending
    await small_cold.release_resume_memory()
    release.assert_awaited_once()
    lazy = PgFluidStore(cache_nodes=False)
    assert await lazy.get_session("large-resume")
    assert not lazy._resume_trim_pending


@pytest.mark.asyncio
async def test_adapter_reclaims_before_first_live_terminal_not_replay():
    adapter = HarnessAdapter()
    release = AsyncMock()
    adapter._store = SimpleNamespace(release_resume_memory=release)
    adapter._turn_floor = 10
    async def events(**kwargs):
        yield TurnComplete(session_id="s", seq=5, stop_reason="end_turn", num_model_calls=0)
        release.assert_not_awaited()
        yield TurnComplete(session_id="s", seq=11, stop_reason="end_turn", num_model_calls=0)
    adapter._engine = SimpleNamespace(events=events)
    output = [item async for item in adapter.receive_events()]
    assert len(output) == 1
    release.assert_awaited_once()
