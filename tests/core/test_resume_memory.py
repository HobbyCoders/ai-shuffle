"""Cold-resume reclamation is bounded and never runs on ordinary live turns."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core import resume_memory as memory
from app.core.provider.fluid_pg_store import PgFluidStore
from app.core.provider.harness_adapter import HarnessAdapter
from fluid.events import TurnComplete
from fluid.messages import Message, Text
from fluid.store.base import SessionNode


@pytest.mark.asyncio
async def test_trim_is_off_loop_and_process_wide_rate_limited(monkeypatch):
    monkeypatch.setattr(memory.sys, "platform", "linux")
    monkeypatch.setattr(memory, "_last_trim", None)
    clock = [0.0]
    monkeypatch.setattr(memory.time, "monotonic", lambda: clock[0])
    worker = AsyncMock(return_value=True)
    monkeypatch.setattr(memory.asyncio, "to_thread", worker)
    assert await memory.release_cold_resume_memory()
    results = await asyncio.gather(*(memory.release_cold_resume_memory() for _ in range(20)))
    assert results == [False] * 20
    clock[0] = 59.0
    assert not await memory.release_cold_resume_memory()
    clock[0] = 60.0
    assert await memory.release_cold_resume_memory()
    assert worker.await_count == 2
    worker.assert_awaited_with(memory._trim_glibc)


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["win32", "darwin"])
async def test_other_platforms_do_not_load_libc(monkeypatch, platform):
    monkeypatch.setattr(memory.sys, "platform", platform)
    worker = AsyncMock()
    monkeypatch.setattr(memory.asyncio, "to_thread", worker)
    assert not await memory.release_cold_resume_memory()
    worker.assert_not_awaited()


def test_trim_requires_glibc_and_tolerates_unavailable_symbols(monkeypatch):
    monkeypatch.setattr(memory.ctypes, "CDLL", lambda _: SimpleNamespace())
    assert not memory._trim_glibc()
    def unavailable(_):
        raise OSError("libc unavailable")
    monkeypatch.setattr(memory.ctypes, "CDLL", unavailable)
    assert not memory._trim_glibc()
    calls = []
    def trim(padding):
        calls.append(padding)
        return 1
    monkeypatch.setattr(memory.ctypes, "CDLL", lambda _: SimpleNamespace(
        gnu_get_libc_version=lambda: b"glibc", malloc_trim=trim))
    assert memory._trim_glibc()
    assert calls == [0]
    assert trim.argtypes == [memory.ctypes.c_size_t]
    assert trim.restype == memory.ctypes.c_int


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
