"""Admission waits for actual executor work, independently of engine phases."""

import ast
import asyncio
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from app.core.provider import session_admission as admission
from app.db import async_database as adb


async def _raw_executor(fn):
    return await asyncio.get_running_loop().run_in_executor(None, fn)


async def _raw_custom_executor(fn):
    return await asyncio.get_running_loop().run_in_executor(adb._event_executor, fn)


@pytest.mark.parametrize("dispatch", [
    adb.run_sync, adb.run_event_write, adb.run_lease, asyncio.to_thread,
    _raw_executor, _raw_custom_executor,
])
@pytest.mark.parametrize("cancel_release", ["none", "before_start", "during"])
async def test_cancelled_thread_still_owns_admission(monkeypatch, dispatch, cancel_release):
    admission.clear()
    monkeypatch.setattr(admission, "get_session_pool", lambda: SimpleNamespace(peek=lambda sid: None))
    started, release, returned = threading.Event(), threading.Event(), threading.Event()
    lease = await admission.acquire("thread-owner")

    def write():
        started.set()
        assert release.wait(5)
        returned.set()

    issuing = asyncio.create_task(dispatch(write))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        issuing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await issuing
        assert lease.work.pending, "caller cancellation hid the still-running thread"
        admission.close(lease)
        if cancel_release != "none":
            owners = [t for t in admission._unattended if t.get_coro().__name__ == "_close_after_writes"]
            assert len(owners) == 1
            if cancel_release == "during":
                await asyncio.sleep(0)
            owners[0].cancel()
            if cancel_release == "during":
                await asyncio.sleep(0)
                owners[0].cancel()
        assert not lease.exited.is_set()
        with pytest.raises(admission.SessionBusyError):
            await admission.acquire("thread-owner")
        release.set()
        await asyncio.wait_for(lease.exited.wait(), 5)
        assert returned.is_set()
        assert not lease.work.pending
        successor = await admission.acquire("thread-owner")
        admission.close(successor)
    finally:
        release.set()
        admission.close(lease)
        await asyncio.wait_for(lease.exited.wait(), 5)
        admission.clear()


async def test_detached_write_producer_is_included_in_release(monkeypatch):
    admission.clear()
    monkeypatch.setattr(admission, "get_session_pool", lambda: SimpleNamespace(peek=lambda sid: None))
    lease = await admission.acquire("detached-producer")
    start = asyncio.Event()
    writes = []

    async def producer():
        await start.wait()
        await adb.run_sync(writes.append, "settled")

    producing = asyncio.create_task(producer())
    adb.keep_in_turn(producing)
    try:
        admission.close(lease)
        assert not lease.exited.is_set()
        start.set()
        await asyncio.wait_for(lease.exited.wait(), 5)
        assert producing.done()
        assert writes == ["settled"]
        assert not lease.work.pending
    finally:
        start.set()
        await asyncio.wait_for(producing, 5)
        await asyncio.wait_for(lease.exited.wait(), 5)
        admission.clear()


@pytest.mark.parametrize("relative", [
    "app/core/provider/harness_adapter.py",
    "app/core/query_engine.py",
    "app/core/query_persistence.py",
    "app/core/user_question_handler.py",
    "app/core/memory_sync.py",
])
def test_turn_app_threads_use_tracked_dispatch(relative):
    root = Path(__file__).resolve().parents[2]
    tree = ast.parse((root / relative).read_text())
    bypasses = [node.lineno for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"to_thread", "run_in_executor"}]
    assert not bypasses, f"Use tracked async_database.run_sync for turn app work: {relative}:{bypasses}"


async def test_nested_event_loop_threads_are_drained_on_the_admission_loop(monkeypatch):
    """The real watch-authorization shape: run_sync -> asyncio.run -> run_sync."""
    admission.clear()
    monkeypatch.setattr(admission, "get_session_pool", lambda: SimpleNamespace(peek=lambda sid: None))
    started, release, outer_returned = threading.Event(), threading.Event(), threading.Event()
    writes = []
    lease = await admission.acquire("nested-loop")
    owner_loop = asyncio.get_running_loop()

    def write():
        started.set()
        assert release.wait(5)
        writes.append("A")

    def sync_watch_check():
        try:
            asyncio.run(adb.run_sync(write))
        finally:
            outer_returned.set()

    issuing = asyncio.create_task(adb.run_sync(sync_watch_check))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        issuing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await issuing
        admission.close(lease)
        await asyncio.sleep(0.02)
        released_with_live_thread = lease.exited.is_set()
        if released_with_live_thread:
            writes.append("B")
        else:
            with pytest.raises(admission.SessionBusyError):
                await admission.acquire("nested-loop")
        pending_loops = {future.get_loop() for future in lease.work.pending}
        release.set()
        assert await asyncio.to_thread(outer_returned.wait, 5)
        await asyncio.wait_for(lease.exited.wait(), 5)
        if not released_with_live_thread:
            successor = await admission.acquire("nested-loop")
            writes.append("B")
            admission.close(successor)
        assert writes == ["A", "B"], "a foreign-loop drain failure admitted B before A's write"
        assert not released_with_live_thread
        assert pending_loops == {owner_loop}
    finally:
        release.set()
        assert await asyncio.to_thread(outer_returned.wait, 5)
        admission.close(lease)
        await asyncio.wait_for(lease.exited.wait(), 5)
        admission.clear()


async def test_failed_drain_does_not_release_admission(monkeypatch, caplog):
    from unittest.mock import AsyncMock

    admission.clear()
    monkeypatch.setattr(admission, "get_session_pool", lambda: SimpleNamespace(peek=lambda sid: None))
    started, release = threading.Event(), threading.Event()
    lease = await admission.acquire("failed-drain")

    def write():
        started.set()
        assert release.wait(5)

    writing = asyncio.create_task(adb.run_sync(write))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        monkeypatch.setattr(lease.work, "drain", AsyncMock(side_effect=RuntimeError("broken drain")))
        admission.close(lease)
        owners = [task for task in admission._unattended
                  if task.get_coro().__name__ == "_close_after_writes"]
        assert len(owners) == 1
        await asyncio.gather(*owners, return_exceptions=True)
        assert not lease.exited.is_set(), "drain failure must fail closed, not admit a successor"
        with pytest.raises(admission.SessionBusyError):
            await admission.acquire("failed-drain")
        assert "keeping admission held" in caplog.text
        lease.work.drain.assert_awaited_once()
    finally:
        release.set()
        await asyncio.wait_for(writing, 5)
        # Explicit recovery, only after the actual write has returned.
        admission._close_now(lease)
        admission.clear()


async def test_executor_hook_is_idempotent_and_does_not_track_context_free_work(monkeypatch):
    admission.clear()
    monkeypatch.setattr(admission, "get_session_pool", lambda: SimpleNamespace(peek=lambda sid: None))
    loop = asyncio.get_running_loop()
    lease = await admission.acquire("unscoped-dispatch")
    installed = loop.run_in_executor
    admission._bind(lease)
    assert loop.run_in_executor is installed
    admission.close(lease)
    assert adb._turn_work.get() is None
    started, release, returned = threading.Event(), threading.Event(), threading.Event()

    def independent_work():
        started.set()
        try:
            assert release.wait(5)
        finally:
            returned.set()

    future = loop.run_in_executor(None, independent_work)
    try:
        assert await asyncio.to_thread(started.wait, 5)
        # Without an admission context, asyncio's ordinary cancellation
        # behavior is unchanged, even after the loop's hook was installed.
        future.cancel()
        assert future.cancelled()
        assert not returned.is_set()
        assert not lease.work.pending
        admission.close(await admission.acquire("unscoped-dispatch"))
    finally:
        release.set()
        assert await asyncio.to_thread(returned.wait, 5)
        admission.clear()


@pytest.mark.parametrize("dispatch", [adb.run_event_write, adb.run_lease])
async def test_nested_custom_executor_cancellation_still_belongs_to_outer_dispatch(monkeypatch, dispatch):
    admission.clear()
    monkeypatch.setattr(admission, "get_session_pool", lambda: SimpleNamespace(peek=lambda sid: None))
    started, release, outer_returned = threading.Event(), threading.Event(), threading.Event()
    owner_loop = asyncio.get_running_loop()
    inner_cancelled = asyncio.Event()
    lease = await admission.acquire("nested-custom-loop")

    def write():
        started.set()
        assert release.wait(5)

    async def nested():
        writing = asyncio.create_task(dispatch(write))
        # Cancel the nested caller while its custom-executor thread is live.
        assert await asyncio.to_thread(started.wait, 5)
        writing.cancel()
        owner_loop.call_soon_threadsafe(inner_cancelled.set)
        with pytest.raises(asyncio.CancelledError):
            await writing

    def sync_check():
        try:
            asyncio.run(nested())
        finally:
            outer_returned.set()

    issuing = asyncio.create_task(adb.run_sync(sync_check))
    try:
        await asyncio.wait_for(inner_cancelled.wait(), 5)
        issuing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await issuing
        admission.close(lease)
        await asyncio.sleep(0.02)
        assert not lease.exited.is_set(), "the outer dispatch returned before its nested custom write"
        assert not outer_returned.is_set()
        assert {future.get_loop() for future in lease.work.pending} == {owner_loop}
    finally:
        release.set()
        assert await asyncio.to_thread(outer_returned.wait, 5)
        admission.close(lease)
        await asyncio.wait_for(lease.exited.wait(), 5)
        admission.clear()
