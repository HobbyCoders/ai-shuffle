"""
U2 regression: a session's Monitor shells must be reaped when the live session
is finally removed.

Monitor shell background tasks (spawned via _spawn_background, tracked in the
module-global _background_tasks dict) deliberately do NOT pin a live session
open. Before this fix, that meant they STRANDED when the session was cleaned up:
the subprocess kept running, its `_reader` task awaited readline() forever (for a
long-running command), and the _background_tasks entry leaked for the whole
process lifetime.

stop_session_background_shells() terminates the subprocess, cancels the reader,
and drops the dict entry — scoped to one session.

V2 (Phase-1 deletion): ``LiveSessionStore.cleanup_ended_session`` — the old
fire-and-forget scheduler of this reap — is deleted with the live store. The
reap is now scheduled by session teardown in
``resource_monitor._cleanup_evicted_sessions`` (pool eviction / death), and
THAT wiring is pinned in
tests/core/test_cleanup_session_reap.py::test_force_cleanup_schedules_monitor_shell_reap.
This file keeps the reap mechanics themselves, which are unchanged.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.mcp import builtin_tools as bt


def _make_running_shell(task_id: str, session_id: str):
    """A _BackgroundTask with a mock 'running' proc and a forever-blocking reader."""
    task = bt._BackgroundTask(task_id, f"tail -f {task_id}", "/tmp", session_id=session_id)

    proc = MagicMock()
    proc.returncode = None  # running

    def _terminate():
        proc.returncode = 0  # terminate causes the proc to exit

    proc.terminate = MagicMock(side_effect=_terminate)
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)
    task.process = proc

    async def _blocker():
        await asyncio.Event().wait()  # never completes on its own

    task._reader_task = asyncio.create_task(_blocker())
    return task


@pytest.fixture(autouse=True)
def _clean_registry():
    """Keep the module-global shell dict isolated per test."""
    bt._background_tasks.clear()
    yield
    bt._background_tasks.clear()


async def test_reap_terminates_cancels_and_removes_session_shells():
    task = _make_running_shell("t1", "sess-A")
    bt._background_tasks["t1"] = task
    reader = task._reader_task

    reaped = await bt.stop_session_background_shells("sess-A")

    assert reaped == 1
    assert "t1" not in bt._background_tasks
    task.process.terminate.assert_called_once()

    # The reader was cancelled by the reap (not stranded). Give the loop a few
    # turns to deliver the cancellation — the test never cancels it itself, so a
    # cancelled reader proves the reap did it.
    for _ in range(10):
        if reader.done():
            break
        await asyncio.sleep(0)
    assert reader.cancelled()


async def test_reap_only_touches_the_named_session():
    a = _make_running_shell("ta", "sess-A")
    b = _make_running_shell("tb", "sess-B")
    bt._background_tasks["ta"] = a
    bt._background_tasks["tb"] = b

    reaped = await bt.stop_session_background_shells("sess-A")

    assert reaped == 1
    assert "ta" not in bt._background_tasks
    assert "tb" in bt._background_tasks  # other session untouched
    a.process.terminate.assert_called_once()
    b.process.terminate.assert_not_called()

    # cleanup: cancel B's reader so the loop doesn't warn about a pending task
    b._reader_task.cancel()


async def test_reap_also_removes_finished_shells():
    """A completed shell's dict entry is reaped too (bounds dict growth)."""
    task = bt._BackgroundTask("done", "echo hi", "/tmp", session_id="sess-A")
    proc = MagicMock()
    proc.returncode = 0  # already finished
    proc.terminate = MagicMock()
    task.process = proc
    task._reader_task = None
    bt._background_tasks["done"] = task

    reaped = await bt.stop_session_background_shells("sess-A")

    assert reaped == 1
    assert "done" not in bt._background_tasks
    proc.terminate.assert_not_called()  # not running — nothing to terminate


async def test_reap_no_shells_is_noop():
    assert await bt.stop_session_background_shells("ghost") == 0


async def test_reap_scheduler_runs_without_running_loop_guard():
    """reap_session_background_shells schedules a task on the running loop."""
    task = _make_running_shell("t9", "sess-Z")
    bt._background_tasks["t9"] = task

    bt.reap_session_background_shells("sess-Z")
    # Let the scheduled reap task run to completion.
    for _ in range(20):
        if "t9" not in bt._background_tasks:
            break
        await asyncio.sleep(0)

    assert "t9" not in bt._background_tasks
