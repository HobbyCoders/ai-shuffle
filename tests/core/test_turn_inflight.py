"""Admission waits for actual executor work, independently of engine phases."""

import ast
import asyncio
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from app.core.provider import session_admission as admission
from app.db import async_database as adb


@pytest.mark.parametrize("dispatch", [adb.run_sync, adb.run_event_write, adb.run_lease])
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
