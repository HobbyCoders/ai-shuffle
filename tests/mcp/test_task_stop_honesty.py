"""TaskStop must not claim a stop the OS did not give it (audit item 5).

Twice in one audited session ``TaskStop`` answered ``{"success": true,
"running": false}`` while ``netstat`` still showed port 8010 LISTENING: the
shell had been terminated, the dev server two processes below it had not, and
the agent only found out by checking the port by hand. A stop that leaves
descendants behind now says so, names the pids, and reports the task as still
running.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.task_registry import task_registry
from app.mcp import builtin_tools as bt

# asyncio_mode = auto (pytest.ini) runs the coroutines below; no marker needed.


@pytest.fixture(autouse=True)
def _clean():
    bt._background_tasks.clear()
    task_registry.reset_all()
    yield
    bt._background_tasks.clear()
    task_registry.reset_all()


def _shell(task_id: str = "t1", session_id: str = "sess-A"):
    task = bt._BackgroundTask(task_id, "python run_local.py", "/work", session_id)
    proc = MagicMock()
    proc.returncode = None
    proc.pid = 1234
    proc.terminate = MagicMock(side_effect=lambda: setattr(proc, "returncode", 0))
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)
    task.process = proc
    task.own_process_group = True
    bt._background_tasks[task_id] = task
    return task


async def test_clean_stop_reports_success():
    task = _shell()

    async def _kill(proc, **kwargs):
        proc.returncode = 0
        return []

    with patch("app.core.process_tree.kill_process_tree", new=_kill):
        result = await bt.stop_background_shell_task("t1", session_id="sess-A")

    assert task.is_running() is False

    assert result["running"] is False
    assert "warning" not in result


async def test_survivors_keep_the_task_running_and_name_the_pids():
    _shell()
    with patch(
        "app.core.process_tree.kill_process_tree",
        new=AsyncMock(return_value=[91234]),
    ), patch("app.core.process_tree.listening_ports", return_value=[8010]):
        result = await bt.stop_background_shell_task("t1", session_id="sess-A")

    assert result["running"] is True
    assert result["survivors"] == [91234]
    assert "91234" in result["warning"]
    assert "8010" in result["warning"]


async def test_survivors_still_settle_the_registry_row():
    """The shell itself is gone, so nothing else would ever settle the row —
    leaving it 'running' would strand the chip forever."""
    _shell()
    task_registry.start_task("sess-A", "t1", "shell", "python run_local.py")
    with patch(
        "app.core.process_tree.kill_process_tree",
        new=AsyncMock(return_value=[91234]),
    ), patch("app.core.process_tree.listening_ports", return_value=[]):
        await bt.stop_background_shell_task("t1", session_id="sess-A")

    assert task_registry.get_task("sess-A", "t1")["status"] == "killed"


async def test_stop_session_task_reports_not_ok_when_orphans_survive():
    from app.core.query_session_lifecycle import stop_session_task

    _shell()
    with patch(
        "app.core.process_tree.kill_process_tree",
        new=AsyncMock(return_value=[91234]),
    ), patch("app.core.process_tree.listening_ports", return_value=[]):
        result = await stop_session_task("sess-A", "t1")

    assert result["ok"] is False
    assert result["kind"] == "shell"
    assert result["survivors"] == [91234]


async def test_is_background_shell_running_tracks_the_subprocess():
    task = _shell("t2")
    assert bt.is_background_shell_running("t2") is True
    task.process.returncode = 0
    assert bt.is_background_shell_running("t2") is False
    assert bt.is_background_shell_running("nope") is False
