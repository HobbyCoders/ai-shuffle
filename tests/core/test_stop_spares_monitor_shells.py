"""Stop ends the turn; it does not tear down the user's dev server (audit item 5).

The audited failure: the model told the user the server "stays up while we
work", the user pressed Stop, and ``handle_stop`` -> ``stop_background=True``
-> ``stop_all_background_tasks`` killed every registry-active task including the
Monitor shell holding the server. It was also inconsistent — typing mid-stream
goes through ``interrupt_for_query``, which never set the flag, so the same
server survived a steer and died on a button press.

What is pinned here: a plain Stop spares a Monitor shell whose subprocess is
still alive, still stops agent work, still sweeps ghost shell rows (so the UI
never strands one), and does not wake-suppress the shell it spared. The force
paths — per-task "x", session teardown reap — keep killing shells.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.core.query_session_lifecycle import stop_all_background_tasks
from app.core.task_registry import task_registry

# asyncio_mode = auto (pytest.ini) runs the coroutines below; no marker needed.


@pytest.fixture(autouse=True)
def _clean_registry():
    task_registry.reset_all()
    yield
    task_registry.reset_all()


def _seed(session_id: str, rows: list[tuple[str, str]]) -> None:
    for task_id, kind in rows:
        task_registry.start_task(session_id, task_id, kind, task_id)


async def test_plain_stop_spares_a_live_monitor_shell():
    session_id = "stop-spares"
    _seed(session_id, [("agent-1", "agent"), ("shell-1", "shell")])

    stopped = []

    async def _stop(sid, task_id):
        stopped.append(task_id)
        return {"ok": True, "task_id": task_id}

    with patch(
        "app.mcp.builtin_tools.is_background_shell_running", return_value=True
    ), patch(
        "app.core.query_session_lifecycle.stop_session_task",
        new=AsyncMock(side_effect=_stop),
    ), patch("app.core.turn_scheduler.discard_queued_task_wakes", return_value=0):
        results = await stop_all_background_tasks(session_id, include_shells=False)

    assert stopped == ["agent-1"]
    spared = [r for r in results if r.get("skipped")]
    assert [r["task_id"] for r in spared] == ["shell-1"]
    # The shell is still running, so its eventual death must still wake the
    # session — sparing it must not also silence it.
    assert task_registry.is_wake_suppressed(session_id, "agent-1")
    assert not task_registry.is_wake_suppressed(session_id, "shell-1")
    assert task_registry.get_task(session_id, "shell-1")["status"] == "running"


async def test_plain_stop_still_reaps_a_ghost_shell_row():
    """A registry row whose process already exited has nothing to spare."""
    session_id = "stop-ghost"
    _seed(session_id, [("shell-dead", "shell")])

    stopped = []

    async def _stop(sid, task_id):
        stopped.append(task_id)
        return {"ok": True, "task_id": task_id}

    with patch(
        "app.mcp.builtin_tools.is_background_shell_running", return_value=False
    ), patch(
        "app.core.query_session_lifecycle.stop_session_task",
        new=AsyncMock(side_effect=_stop),
    ), patch("app.core.turn_scheduler.discard_queued_task_wakes", return_value=0):
        results = await stop_all_background_tasks(session_id, include_shells=False)

    assert stopped == ["shell-dead"]
    assert not any(r.get("skipped") for r in results)


async def test_force_path_still_kills_shells():
    """``include_shells`` defaults to True — session-scoped teardown is unchanged."""
    session_id = "stop-force"
    _seed(session_id, [("shell-1", "shell")])

    stopped = []

    async def _stop(sid, task_id):
        stopped.append(task_id)
        return {"ok": True, "task_id": task_id}

    with patch(
        "app.core.query_session_lifecycle.stop_session_task",
        new=AsyncMock(side_effect=_stop),
    ), patch("app.core.turn_scheduler.discard_queued_task_wakes", return_value=0):
        await stop_all_background_tasks(session_id)

    assert stopped == ["shell-1"]


async def test_wake_discard_covers_only_the_tasks_being_stopped():
    session_id = "stop-discard"
    _seed(session_id, [("agent-1", "agent"), ("shell-1", "shell")])

    with patch(
        "app.mcp.builtin_tools.is_background_shell_running", return_value=True
    ), patch(
        "app.core.query_session_lifecycle.stop_session_task",
        new=AsyncMock(return_value={"ok": True}),
    ), patch(
        "app.core.turn_scheduler.discard_queued_task_wakes", return_value=0
    ) as discard:
        await stop_all_background_tasks(session_id, include_shells=False)

    assert set(discard.call_args.args[1]) == {"agent-1"}
