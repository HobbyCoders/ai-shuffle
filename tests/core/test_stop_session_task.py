"""
Tests for stopping ONE in-flight background task from the UI.

Covers:
- app.mcp.builtin_tools.stop_background_shell_task — terminates a tracked
  Monitor subprocess, marks it killed in the registry, and returns None for an
  unknown id (so callers fall through to SDK agent-task handling).
- app.core.query_session_lifecycle.stop_session_task — routes shell tasks to the
  shell helper and agent tasks to the SDK client's stop_task(); handles a
  missing / disconnected session gracefully (never raises).

No Postgres or live SDK is needed: the session pool and SDK client are mocked.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.mcp.builtin_tools as bt
from app.mcp.builtin_tools import stop_background_shell_task
from app.core.query_session_lifecycle import stop_session_task
from app.core.task_registry import task_registry


def _fake_shell_task(task_id: str) -> bt._BackgroundTask:
    """A _BackgroundTask whose process terminates cleanly when asked."""
    task = bt._BackgroundTask(task_id=task_id, command="sleep 999", cwd=None)
    proc = MagicMock()
    proc.returncode = None

    def _terminate():
        proc.returncode = -15

    proc.terminate.side_effect = _terminate
    proc.wait = AsyncMock(return_value=-15)
    proc.kill = MagicMock()
    task.process = proc
    return task


@pytest.fixture(autouse=True)
def _clean_state():
    task_registry.reset_all()
    bt._background_tasks.clear()
    yield
    task_registry.reset_all()
    bt._background_tasks.clear()


# =============================================================================
# stop_background_shell_task
# =============================================================================

class TestStopBackgroundShellTask:
    @pytest.mark.asyncio
    async def test_unknown_id_returns_none(self):
        assert await stop_background_shell_task("nope", session_id="s1") is None

    @pytest.mark.asyncio
    async def test_terminates_and_marks_killed(self):
        task_registry.start_task("s1", "sh1", "shell", "sleep 999")
        task = _fake_shell_task("sh1")
        bt._background_tasks["sh1"] = task

        result = await stop_background_shell_task("sh1", session_id="s1")

        assert result is not None
        assert result["kind"] == "shell"
        assert result["task_id"] == "sh1"
        # Process was asked to terminate and is no longer running.
        task.process.terminate.assert_called_once()
        assert result["running"] is False
        # Registry flipped to the terminal 'killed' status -> drops from active.
        assert task_registry.get_active("s1") == []
        rec = task_registry.get_task("s1", "sh1")
        assert rec is not None and rec["status"] == "killed"

    @pytest.mark.asyncio
    async def test_escalates_to_kill_on_timeout(self):
        task = _fake_shell_task("sh2")
        # wait() never returns -> wait_for raises TimeoutError -> kill() path.
        task.process.wait = AsyncMock(side_effect=[__import__("asyncio").TimeoutError(), -9])
        bt._background_tasks["sh2"] = task

        await stop_background_shell_task("sh2", session_id="s1")
        task.process.kill.assert_called_once()


# =============================================================================
# stop_session_task
# =============================================================================

class TestStopSessionTask:
    @pytest.mark.asyncio
    async def test_routes_shell_task_to_shell_helper(self):
        task_registry.start_task("s1", "sh1", "shell", "sleep 999")
        bt._background_tasks["sh1"] = _fake_shell_task("sh1")

        result = await stop_session_task("s1", "sh1")

        assert result["ok"] is True
        assert result["kind"] == "shell"

    @pytest.mark.asyncio
    async def test_routes_agent_task_to_sdk_client(self):
        fake_client = MagicMock()
        fake_client.stop_task = AsyncMock()
        fake_state = MagicMock(is_connected=True, client=fake_client)
        fake_pool = MagicMock()
        fake_pool.peek.return_value = fake_state

        with patch(
            "app.core.query_session_lifecycle.get_session_pool",
            return_value=fake_pool,
        ):
            result = await stop_session_task("s1", "agent-123")

        fake_client.stop_task.assert_awaited_once_with("agent-123")
        assert result["ok"] is True
        assert result["kind"] == "agent"

    @pytest.mark.asyncio
    async def test_agent_task_session_not_active(self):
        fake_pool = MagicMock()
        fake_pool.peek.return_value = None
        with patch(
            "app.core.query_session_lifecycle.get_session_pool",
            return_value=fake_pool,
        ):
            result = await stop_session_task("s1", "agent-x")
        assert result["ok"] is False
        assert "not active" in result["detail"]

    @pytest.mark.asyncio
    async def test_agent_task_session_not_connected(self):
        fake_state = MagicMock(is_connected=False, client=None)
        fake_pool = MagicMock()
        fake_pool.peek.return_value = fake_state
        with patch(
            "app.core.query_session_lifecycle.get_session_pool",
            return_value=fake_pool,
        ):
            result = await stop_session_task("s1", "agent-x")
        assert result["ok"] is False
        assert "not connected" in result["detail"]

    @pytest.mark.asyncio
    async def test_agent_task_sdk_error_is_caught(self):
        fake_client = MagicMock()
        fake_client.stop_task = AsyncMock(side_effect=RuntimeError("boom"))
        fake_state = MagicMock(is_connected=True, client=fake_client)
        fake_pool = MagicMock()
        fake_pool.peek.return_value = fake_state
        with patch(
            "app.core.query_session_lifecycle.get_session_pool",
            return_value=fake_pool,
        ):
            result = await stop_session_task("s1", "agent-x")
        assert result["ok"] is False
        assert result["kind"] == "agent"
        assert "boom" in result["detail"]


# =============================================================================
# stop_session_task — "ghost" task reaping (Bug 4)
#
# Tasks terminated by outside forces (idle eviction, process death, session
# teardown) can be left parked "running" in the registry with no live client to
# stop them. Clicking the UI's "x" must REAP such ghosts — mark them killed so
# they drop off the next /live poll — and report success, rather than returning
# ok:false and leaving the strip showing a task whose close button is stuck
# disabled forever.
# =============================================================================

class TestStopSessionTaskReapsGhosts:
    @pytest.mark.asyncio
    async def test_agent_ghost_reaped_when_session_gone(self):
        # A backgrounded sub-agent still 'running' in the registry, but the live
        # session is gone from the pool (peek -> None).
        task_registry.start_task("s1", "agent-ghost", "agent", "bg work")
        fake_pool = MagicMock()
        fake_pool.peek.return_value = None
        with patch(
            "app.core.query_session_lifecycle.get_session_pool",
            return_value=fake_pool,
        ):
            result = await stop_session_task("s1", "agent-ghost")

        assert result["ok"] is True
        assert result["kind"] == "agent"
        assert "reaped" in result["detail"]
        # Marked terminal -> drops off the next /live poll.
        assert task_registry.get_active("s1") == []
        rec = task_registry.get_task("s1", "agent-ghost")
        assert rec is not None and rec["status"] == "killed"

    @pytest.mark.asyncio
    async def test_agent_ghost_reaped_when_session_disconnected(self):
        task_registry.start_task("s1", "agent-ghost", "agent", "bg work")
        fake_state = MagicMock(is_connected=False, client=None)
        fake_pool = MagicMock()
        fake_pool.peek.return_value = fake_state
        with patch(
            "app.core.query_session_lifecycle.get_session_pool",
            return_value=fake_pool,
        ):
            result = await stop_session_task("s1", "agent-ghost")

        assert result["ok"] is True
        assert "reaped" in result["detail"]
        assert task_registry.get_active("s1") == []

    @pytest.mark.asyncio
    async def test_shell_ghost_reaped_when_subprocess_untracked(self):
        # A registry shell task whose subprocess already exited (not in
        # _background_tasks) — the shell helper returns None and we must reap it
        # rather than mis-route a shell id to the SDK's agent stop.
        task_registry.start_task("s1", "sh-ghost", "shell", "sleep 999")
        # Deliberately NOT adding to bt._background_tasks -> untracked ghost.
        result = await stop_session_task("s1", "sh-ghost")

        assert result["ok"] is True
        assert result["kind"] == "shell"
        assert "reaped" in result["detail"]
        rec = task_registry.get_task("s1", "sh-ghost")
        assert rec is not None and rec["status"] == "killed"

    @pytest.mark.asyncio
    async def test_already_terminal_task_is_idempotent_success(self):
        # The registry already has the task in a terminal state (e.g. a prior
        # kill that the UI hasn't polled yet). Closing it again must succeed so
        # the stale row clears — without touching the session pool at all.
        task_registry.start_task("s1", "agent-done", "agent", "bg work")
        task_registry.complete_task("s1", "agent-done", "killed")

        called = {"peek": False}
        fake_pool = MagicMock()

        def _peek(*a, **k):
            called["peek"] = True
            return None

        fake_pool.peek.side_effect = _peek
        with patch(
            "app.core.query_session_lifecycle.get_session_pool",
            return_value=fake_pool,
        ):
            result = await stop_session_task("s1", "agent-done")

        assert result["ok"] is True
        assert "already finished" in result["detail"]
        # Short-circuited before any pool/SDK interaction.
        assert called["peek"] is False

    @pytest.mark.asyncio
    async def test_unknown_task_with_dead_session_still_reports_honestly(self):
        # No registry record AND no live session -> nothing to reap, nothing the
        # UI is showing. Preserve the honest ok:false so callers aren't misled.
        fake_pool = MagicMock()
        fake_pool.peek.return_value = None
        with patch(
            "app.core.query_session_lifecycle.get_session_pool",
            return_value=fake_pool,
        ):
            result = await stop_session_task("s1", "never-seen")
        assert result["ok"] is False
        assert "not active" in result["detail"]
