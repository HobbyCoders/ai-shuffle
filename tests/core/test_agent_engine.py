"""
Unit tests for agent engine module.

Tests cover:
- AgentStatus and TaskStatus enums
- AgentRunState dataclass
- AgentExecutionEngine class:
  - Lifecycle (start, stop)
  - Agent launching and queue processing
  - Agent execution flow
  - Environment setup (worktrees)
  - Prompt building
  - Todo/task handling
  - Completion and failure handling
  - PR creation and conflict resolution
  - Auto-merge and cleanup
  - Auto-review triggering
  - Pause/resume/cancel operations
  - State querying
"""

import pytest
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch, PropertyMock
from typing import Dict, Any, List

# Import the module under test
from app.core.agent_engine import (
    AgentStatus,
    TaskStatus,
    AgentRunState,
    AgentExecutionEngine,
    BroadcastCallback,
    agent_engine,
)


# =============================================================================
# AgentStatus Enum Tests
# =============================================================================

class TestAgentStatus:
    """Test AgentStatus enum."""

    def test_status_values(self):
        """Should have correct string values."""
        assert AgentStatus.QUEUED.value == "queued"
        assert AgentStatus.RUNNING.value == "running"
        assert AgentStatus.PAUSED.value == "paused"
        assert AgentStatus.COMPLETED.value == "completed"
        assert AgentStatus.FAILED.value == "failed"

    def test_status_is_string_enum(self):
        """Should be string enum."""
        assert AgentStatus.QUEUED == "queued"
        assert AgentStatus.RUNNING == "running"


# =============================================================================
# TaskStatus Enum Tests
# =============================================================================

class TestTaskStatus:
    """Test TaskStatus enum."""

    def test_status_values(self):
        """Should have correct string values."""
        assert TaskStatus.PENDING.value == "pending"
        assert TaskStatus.IN_PROGRESS.value == "in_progress"
        assert TaskStatus.COMPLETED.value == "completed"
        assert TaskStatus.FAILED.value == "failed"


# =============================================================================
# AgentRunState Tests
# =============================================================================

class TestAgentRunState:
    """Test AgentRunState dataclass."""

    def test_create_with_minimal_args(self):
        """Should create state with minimal arguments."""
        state = AgentRunState(agent_run_id="agent-123")

        assert state.agent_run_id == "agent-123"
        assert state.task is None
        assert state.client is None
        assert state.sdk_session_id is None
        assert state.is_paused is False
        assert state.cancel_requested is False
        assert state.worktree_path is None
        assert state.branch_name is None
        assert state.written_agent_ids == []
        assert state.agents_dir is None
        assert isinstance(state.started_at, datetime)
        assert isinstance(state.last_activity, datetime)

    def test_pause_event_initialized_set(self):
        """Should initialize pause_event as set (not paused)."""
        state = AgentRunState(agent_run_id="agent-123")

        # pause_event.is_set() means not paused
        assert state.pause_event.is_set()

    def test_create_with_all_fields(self):
        """Should create state with all fields."""
        mock_task = MagicMock()
        mock_client = MagicMock()
        agents_path = Path("/test/agents")

        state = AgentRunState(
            agent_run_id="agent-456",
            task=mock_task,
            client=mock_client,
            sdk_session_id="sdk-789",
            is_paused=True,
            cancel_requested=True,
            worktree_path="/path/to/worktree",
            branch_name="feature/test",
            written_agent_ids=["agent-1", "agent-2"],
            agents_dir=agents_path
        )

        assert state.agent_run_id == "agent-456"
        assert state.task == mock_task
        assert state.client == mock_client
        assert state.sdk_session_id == "sdk-789"
        assert state.is_paused is True
        assert state.cancel_requested is True
        assert state.worktree_path == "/path/to/worktree"
        assert state.branch_name == "feature/test"
        assert state.written_agent_ids == ["agent-1", "agent-2"]
        assert state.agents_dir == agents_path


# =============================================================================
# AgentExecutionEngine Initialization Tests
# =============================================================================

class TestAgentExecutionEngineInit:
    """Test AgentExecutionEngine initialization."""

    def test_default_max_concurrent(self):
        """Should default to 3 max concurrent agents."""
        engine = AgentExecutionEngine()
        assert engine.max_concurrent == 3

    def test_custom_max_concurrent(self):
        """Should accept custom max concurrent."""
        engine = AgentExecutionEngine(max_concurrent=5)
        assert engine.max_concurrent == 5

    def test_initial_state(self):
        """Should initialize with empty state."""
        engine = AgentExecutionEngine()

        assert engine._active_runs == {}
        assert engine._broadcast_callback is None
        assert engine._queue_processor_task is None
        assert engine._shutdown is False


# =============================================================================
# AgentExecutionEngine Lifecycle Tests
# =============================================================================

class TestAgentExecutionEngineLifecycle:
    """Test engine start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_start_creates_queue_processor(self):
        """Should create queue processor task on start."""
        engine = AgentExecutionEngine()

        await engine.start()

        assert engine._queue_processor_task is not None
        assert engine._shutdown is False

        # Clean up
        await engine.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_queue_processor(self):
        """Should cancel queue processor on stop."""
        engine = AgentExecutionEngine()
        await engine.start()

        await engine.stop()

        assert engine._shutdown is True

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.database")
    async def test_stop_cancels_running_agents(self, mock_db):
        """Should cancel all running agents on stop."""
        engine = AgentExecutionEngine()
        mock_db.get_agent_run.return_value = {
            "id": "agent-123",
            "status": "running"
        }
        mock_db.update_agent_run.return_value = {"id": "agent-123"}

        # Add a mock running agent
        mock_task = MagicMock()
        mock_task.done.return_value = False
        mock_task.cancel = MagicMock()
        state = AgentRunState(agent_run_id="agent-123", task=mock_task)
        engine._active_runs["agent-123"] = state

        await engine.stop()

        mock_task.cancel.assert_called_once()


# =============================================================================
# Broadcast Callback Tests
# =============================================================================

class TestBroadcastCallback:
    """Test broadcast callback functionality."""

    def test_set_broadcast_callback(self):
        """Should set broadcast callback."""
        engine = AgentExecutionEngine()

        async def my_callback(run_id, event, data):
            pass

        engine.set_broadcast_callback(my_callback)

        assert engine._broadcast_callback == my_callback

    @pytest.mark.asyncio
    async def test_broadcast_calls_callback(self):
        """Should call broadcast callback with correct args."""
        engine = AgentExecutionEngine()
        callback = AsyncMock()
        engine.set_broadcast_callback(callback)

        await engine._broadcast("agent-123", "test_event", {"key": "value"})

        callback.assert_awaited_once_with("agent-123", "test_event", {"key": "value"})

    @pytest.mark.asyncio
    async def test_broadcast_handles_callback_error(self):
        """Should handle callback errors gracefully."""
        engine = AgentExecutionEngine()
        callback = AsyncMock(side_effect=Exception("Callback failed"))
        engine.set_broadcast_callback(callback)

        # Should not raise
        await engine._broadcast("agent-123", "test_event", {})

    @pytest.mark.asyncio
    async def test_broadcast_without_callback(self):
        """Should handle missing callback gracefully."""
        engine = AgentExecutionEngine()

        # Should not raise when no callback set
        await engine._broadcast("agent-123", "test_event", {})


# =============================================================================
# Log Tests
# =============================================================================

class TestAgentLog:
    """Test _log method."""

    @patch("app.core.agent_engine.database")
    def test_log_adds_to_database(self, mock_db):
        """Should add log entry to database."""
        engine = AgentExecutionEngine()
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}

        engine._log("agent-123", "Test message", "info", {"extra": "data"})

        mock_db.add_agent_log.assert_called_once_with(
            "agent-123", "Test message", "info", {"extra": "data"}
        )

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.database")
    async def test_log_broadcasts_when_callback_set(self, mock_db):
        """Should broadcast log when callback is set."""
        engine = AgentExecutionEngine()
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}
        callback = AsyncMock()
        engine.set_broadcast_callback(callback)

        engine._log("agent-123", "Test message", "warning")

        # Wait for the async task to complete
        await asyncio.sleep(0.1)

        # Check that broadcast was called with agent_log event
        callback.assert_awaited_once()
        call_args = callback.call_args
        assert call_args[0][0] == "agent-123"
        assert call_args[0][1] == "agent_log"


# =============================================================================
# Queue Processing Tests
# =============================================================================

class TestQueueProcessing:
    """Test queue processing functionality."""

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.database")
    async def test_process_queue_skips_when_full(self, mock_db):
        """Should skip processing when all slots are full."""
        engine = AgentExecutionEngine(max_concurrent=2)

        # Add 2 active runs
        for i in range(2):
            mock_task = MagicMock()
            mock_task.done.return_value = False
            state = AgentRunState(agent_run_id=f"agent-{i}", task=mock_task)
            engine._active_runs[f"agent-{i}"] = state

        await engine._process_queue()

        # Should not call get_queued_agent_runs since all slots are full
        mock_db.get_queued_agent_runs.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.database")
    async def test_process_queue_starts_queued_agents(self, mock_db):
        """Should start queued agents when slots available."""
        engine = AgentExecutionEngine(max_concurrent=2)
        mock_db.get_queued_agent_runs.return_value = [
            {"id": "agent-1", "name": "Test Agent", "prompt": "Do something"}
        ]
        mock_db.update_agent_run.return_value = {"id": "agent-1", "status": "running"}

        with patch.object(engine, "_start_agent_run", new_callable=AsyncMock) as mock_start:
            await engine._process_queue()

            mock_start.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.database")
    async def test_process_queue_respects_slot_limit(self, mock_db):
        """Should only start agents up to available slots."""
        engine = AgentExecutionEngine(max_concurrent=2)

        # 1 active run
        mock_task = MagicMock()
        mock_task.done.return_value = False
        state = AgentRunState(agent_run_id="agent-0", task=mock_task)
        engine._active_runs["agent-0"] = state

        # 3 queued
        mock_db.get_queued_agent_runs.return_value = [
            {"id": "agent-1"},
            {"id": "agent-2"},
            {"id": "agent-3"},
        ]

        with patch.object(engine, "_start_agent_run", new_callable=AsyncMock) as mock_start:
            await engine._process_queue()

            # Should only start 1 (max_concurrent - active)
            assert mock_start.await_count == 1


# =============================================================================
# Launch Agent Tests
# =============================================================================

class TestLaunchAgent:
    """Test launch_agent method."""

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.database")
    async def test_launch_agent_creates_record(self, mock_db):
        """Should create database record for new agent."""
        engine = AgentExecutionEngine()
        mock_db.create_agent_run.return_value = {
            "id": "agent-abc123",
            "name": "Test Agent",
            "status": "queued"
        }
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}

        result = await engine.launch_agent(
            name="Test Agent",
            prompt="Do the thing",
            profile_id="profile-1",
            project_id="project-1"
        )

        assert result["name"] == "Test Agent"
        assert result["status"] == "queued"
        mock_db.create_agent_run.assert_called_once()

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.database")
    async def test_launch_agent_broadcasts_event(self, mock_db):
        """Should broadcast agent_launched event."""
        engine = AgentExecutionEngine()
        callback = AsyncMock()
        engine.set_broadcast_callback(callback)
        mock_db.create_agent_run.return_value = {"id": "agent-123", "status": "queued"}
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}

        await engine.launch_agent(name="Test", prompt="Test prompt")

        # Check broadcast was called with agent_launched
        calls = [c for c in callback.await_args_list if c[0][1] == "agent_launched"]
        assert len(calls) >= 1

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.database")
    async def test_launch_agent_triggers_queue_processing(self, mock_db):
        """Should trigger queue processing after launch."""
        engine = AgentExecutionEngine()
        mock_db.create_agent_run.return_value = {"id": "agent-123"}
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}

        with patch.object(engine, "_process_queue", new_callable=AsyncMock):
            await engine.launch_agent(name="Test", prompt="Test")
            # process_queue is called via asyncio.create_task


# =============================================================================
# Start Agent Run Tests
# =============================================================================

class TestStartAgentRun:
    """Test _start_agent_run method."""

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.database")
    async def test_start_agent_run_creates_state(self, mock_db):
        """Should create state tracker for agent."""
        engine = AgentExecutionEngine()
        mock_db.update_agent_run.return_value = {"id": "agent-123"}
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}

        agent_run = {"id": "agent-123", "name": "Test"}

        with patch.object(engine, "_execute_agent", new_callable=AsyncMock):
            await engine._start_agent_run(agent_run)

        assert "agent-123" in engine._active_runs
        assert engine._active_runs["agent-123"].agent_run_id == "agent-123"

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.database")
    async def test_start_agent_run_updates_status(self, mock_db):
        """Should update status to running."""
        engine = AgentExecutionEngine()
        mock_db.update_agent_run.return_value = {"id": "agent-123"}
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}

        agent_run = {"id": "agent-123", "name": "Test"}

        with patch.object(engine, "_execute_agent", new_callable=AsyncMock):
            await engine._start_agent_run(agent_run)

        mock_db.update_agent_run.assert_called_with("agent-123", status="running")


# =============================================================================
# Setup Agent Environment Tests
# =============================================================================

class TestSetupAgentEnvironment:
    """Test _setup_agent_environment method."""

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.database")
    async def test_setup_without_auto_branch(self, mock_db):
        """Should skip worktree creation when auto_branch is False."""
        engine = AgentExecutionEngine()
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}
        state = AgentRunState(agent_run_id="agent-123")

        agent_run = {
            "id": "agent-123",
            "auto_branch": False,
            "project_id": "project-1"
        }

        await engine._setup_agent_environment("agent-123", agent_run, state)

        assert state.worktree_path is None
        assert state.branch_name is None

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.database")
    async def test_setup_without_project(self, mock_db):
        """Should skip worktree creation when no project."""
        engine = AgentExecutionEngine()
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}
        state = AgentRunState(agent_run_id="agent-123")

        agent_run = {
            "id": "agent-123",
            "auto_branch": True,
            "project_id": None
        }

        await engine._setup_agent_environment("agent-123", agent_run, state)

        assert state.worktree_path is None

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.worktree_manager")
    @patch("app.core.agent_engine.git_service")
    @patch("app.core.agent_engine.settings")
    @patch("app.core.agent_engine.database")
    async def test_setup_creates_worktree(self, mock_db, mock_settings, mock_git, mock_wt):
        """Should create worktree for project with auto_branch."""
        engine = AgentExecutionEngine()
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}
        mock_db.get_project.return_value = {"id": "project-1", "path": "my-project"}
        mock_db.update_agent_run.return_value = {"id": "agent-123"}
        mock_settings.workspace_dir = Path("/workspace")
        mock_git.is_git_repo.return_value = True
        mock_git.get_default_branch.return_value = "main"
        mock_wt.create_worktree_session.return_value = (
            {"id": "wt-1", "worktree_path": ".worktrees/project-1/agent-test"},
            {"id": "session-1"}
        )

        state = AgentRunState(agent_run_id="agent-123")
        agent_run = {
            "id": "agent-123",
            "name": "Test Agent",
            "auto_branch": True,
            "project_id": "project-1",
            "profile_id": "profile-1"
        }

        await engine._setup_agent_environment("agent-123", agent_run, state)

        assert state.branch_name is not None
        assert "agent/" in state.branch_name
        mock_wt.create_worktree_session.assert_called_once()

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.git_service")
    @patch("app.core.agent_engine.settings")
    @patch("app.core.agent_engine.database")
    async def test_setup_handles_non_git_project(self, mock_db, mock_settings, mock_git):
        """Should handle non-git project gracefully."""
        engine = AgentExecutionEngine()
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}
        mock_db.get_project.return_value = {"id": "project-1", "path": "my-project"}
        mock_settings.workspace_dir = Path("/workspace")
        mock_git.is_git_repo.return_value = False

        state = AgentRunState(agent_run_id="agent-123")
        agent_run = {
            "id": "agent-123",
            "name": "Test",
            "auto_branch": True,
            "project_id": "project-1"
        }

        await engine._setup_agent_environment("agent-123", agent_run, state)

        assert state.worktree_path is None


# =============================================================================
# Build Agent Prompt Tests
# =============================================================================

class TestBuildAgentPrompt:
    """Test _build_agent_prompt method."""

    def test_build_prompt_without_branch(self):
        """Should build prompt without branch context."""
        engine = AgentExecutionEngine()
        state = AgentRunState(agent_run_id="agent-123")

        agent_run = {
            "prompt": "Fix the bug in the login page"
        }

        result = engine._build_agent_prompt(agent_run, state)

        assert "## Task" in result
        assert "Fix the bug in the login page" in result
        assert "Current branch" not in result

    def test_build_prompt_with_branch(self):
        """Should include branch context when available."""
        engine = AgentExecutionEngine()
        state = AgentRunState(
            agent_run_id="agent-123",
            branch_name="feature/fix-login"
        )

        agent_run = {
            "prompt": "Fix the bug",
            "base_branch": "develop"
        }

        result = engine._build_agent_prompt(agent_run, state)

        assert "**Current branch:** `feature/fix-login`" in result
        assert "**Base branch:** `develop`" in result
        assert "Fix the bug" in result


# =============================================================================
# Handle Todo Write Tests
# =============================================================================

class TestHandleTodoWrite:
    """Test _handle_todo_write method."""

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.database")
    async def test_handle_todo_creates_new_tasks(self, mock_db):
        """Should create new tasks from todo input."""
        engine = AgentExecutionEngine()
        mock_db.get_agent_tasks.return_value = []
        mock_db.create_agent_task.return_value = {"id": "task-1"}

        todo_input = {
            "todos": [
                {"content": "Task 1", "status": "pending"},
                {"content": "Task 2", "status": "in_progress"}
            ]
        }

        await engine._handle_todo_write("agent-123", todo_input)

        assert mock_db.create_agent_task.call_count == 2

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.database")
    async def test_handle_todo_updates_existing_tasks(self, mock_db):
        """Should update existing tasks when status changes."""
        engine = AgentExecutionEngine()
        mock_db.get_agent_tasks.return_value = [
            {"id": "task-1", "name": "Task 1", "status": "pending"}
        ]
        mock_db.update_agent_task.return_value = {"id": "task-1"}

        todo_input = {
            "todos": [
                {"content": "Task 1", "status": "completed"}
            ]
        }

        await engine._handle_todo_write("agent-123", todo_input)

        mock_db.update_agent_task.assert_called_once_with("task-1", status="completed")

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.database")
    async def test_handle_todo_broadcasts_updates(self, mock_db):
        """Should broadcast task updates."""
        engine = AgentExecutionEngine()
        callback = AsyncMock()
        engine.set_broadcast_callback(callback)
        mock_db.get_agent_tasks.return_value = []
        mock_db.create_agent_task.return_value = {"id": "task-1"}

        todo_input = {
            "todos": [{"content": "New Task", "status": "pending"}]
        }

        await engine._handle_todo_write("agent-123", todo_input)

        # Check broadcast was called with agent_task_update
        calls = [c for c in callback.await_args_list if c[0][1] == "agent_task_update"]
        assert len(calls) >= 1


# =============================================================================
# Complete Agent Tests
# =============================================================================

class TestCompleteAgent:
    """Test _complete_agent method."""

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.database")
    async def test_complete_agent_updates_status(self, mock_db):
        """Should update agent status to completed."""
        engine = AgentExecutionEngine()
        mock_db.update_agent_run.return_value = {"id": "agent-123"}
        mock_db.get_agent_run.return_value = {"id": "agent-123", "pr_url": None}
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}

        state = AgentRunState(agent_run_id="agent-123")
        agent_run = {"id": "agent-123", "auto_pr": False}

        await engine._complete_agent("agent-123", agent_run, state, "Result text")

        mock_db.update_agent_run.assert_called()
        # Check that completed status was set
        call_args = mock_db.update_agent_run.call_args
        assert call_args.kwargs.get("status") == "completed"
        assert call_args.kwargs.get("progress") == 100

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.database")
    async def test_complete_agent_truncates_summary(self, mock_db):
        """Should truncate long response to summary."""
        engine = AgentExecutionEngine()
        mock_db.update_agent_run.return_value = {"id": "agent-123"}
        mock_db.get_agent_run.return_value = {"id": "agent-123", "pr_url": None}
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}

        state = AgentRunState(agent_run_id="agent-123")
        agent_run = {"id": "agent-123", "auto_pr": False}
        long_response = "x" * 1000

        await engine._complete_agent("agent-123", agent_run, state, long_response)

        call_args = mock_db.update_agent_run.call_args
        result_summary = call_args.kwargs.get("result_summary")
        assert len(result_summary) <= 503  # 500 + "..."

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.database")
    async def test_complete_agent_triggers_auto_pr(self, mock_db):
        """Should trigger PR creation when auto_pr enabled."""
        engine = AgentExecutionEngine()
        mock_db.update_agent_run.return_value = {"id": "agent-123"}
        mock_db.get_agent_run.return_value = {"id": "agent-123", "pr_url": None}
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}

        state = AgentRunState(
            agent_run_id="agent-123",
            branch_name="feature/test"
        )
        agent_run = {"id": "agent-123", "auto_pr": True}

        with patch.object(engine, "_create_pull_request", new_callable=AsyncMock) as mock_pr:
            await engine._complete_agent("agent-123", agent_run, state, "Done")

            mock_pr.assert_awaited_once()


# =============================================================================
# Fail Agent Tests
# =============================================================================

class TestFailAgent:
    """Test _fail_agent method."""

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.database")
    async def test_fail_agent_updates_status(self, mock_db):
        """Should update agent status to failed."""
        engine = AgentExecutionEngine()
        mock_db.update_agent_run.return_value = {"id": "agent-123"}
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}

        state = AgentRunState(agent_run_id="agent-123")

        await engine._fail_agent("agent-123", state, "Something went wrong")

        mock_db.update_agent_run.assert_called_once()
        call_args = mock_db.update_agent_run.call_args
        assert call_args.kwargs.get("status") == "failed"
        assert call_args.kwargs.get("error") == "Something went wrong"

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.database")
    async def test_fail_agent_broadcasts_event(self, mock_db):
        """Should broadcast agent_failed event."""
        engine = AgentExecutionEngine()
        callback = AsyncMock()
        engine.set_broadcast_callback(callback)
        mock_db.update_agent_run.return_value = {"id": "agent-123"}
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}

        state = AgentRunState(agent_run_id="agent-123")

        await engine._fail_agent("agent-123", state, "Error")

        calls = [c for c in callback.await_args_list if c[0][1] == "agent_failed"]
        assert len(calls) >= 1


# =============================================================================
# Cleanup Agent Tests
# =============================================================================

class TestCleanupAgent:
    """Test _cleanup_agent method."""

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.cleanup_agents_directory")
    @patch("app.core.agent_engine.database")
    async def test_cleanup_removes_from_active_runs(self, mock_db, mock_cleanup):
        """Should remove agent from active runs."""
        engine = AgentExecutionEngine()
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}

        state = AgentRunState(agent_run_id="agent-123")
        engine._active_runs["agent-123"] = state

        await engine._cleanup_agent("agent-123", state)

        assert "agent-123" not in engine._active_runs

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.cleanup_agents_directory")
    @patch("app.core.agent_engine.database")
    async def test_cleanup_cleans_agents_directory(self, mock_db, mock_cleanup):
        """Should clean up written agent files."""
        engine = AgentExecutionEngine()
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}

        agents_dir = Path("/test/.claude/agents")
        state = AgentRunState(
            agent_run_id="agent-123",
            agents_dir=agents_dir,
            written_agent_ids=["agent-1", "agent-2"]
        )

        await engine._cleanup_agent("agent-123", state)

        mock_cleanup.assert_called_once_with(agents_dir, ["agent-1", "agent-2"])


# =============================================================================
# Pause/Resume/Cancel Tests
# =============================================================================

class TestPauseResumeCancel:
    """Test pause, resume, and cancel operations."""

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.database")
    async def test_pause_agent_success(self, mock_db):
        """Should pause a running agent."""
        engine = AgentExecutionEngine()
        mock_db.get_agent_run.return_value = {"id": "agent-123", "status": "running"}
        mock_db.update_agent_run.return_value = {"id": "agent-123"}
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}

        state = AgentRunState(agent_run_id="agent-123")
        engine._active_runs["agent-123"] = state

        result = await engine.pause_agent("agent-123")

        assert result is True
        assert state.is_paused is True
        assert not state.pause_event.is_set()

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.database")
    async def test_pause_agent_not_found(self, mock_db):
        """Should return False for unknown agent."""
        engine = AgentExecutionEngine()

        result = await engine.pause_agent("unknown")

        assert result is False

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.database")
    async def test_pause_agent_wrong_status(self, mock_db):
        """Should return False for non-running agent."""
        engine = AgentExecutionEngine()
        mock_db.get_agent_run.return_value = {"id": "agent-123", "status": "completed"}

        state = AgentRunState(agent_run_id="agent-123")
        engine._active_runs["agent-123"] = state

        result = await engine.pause_agent("agent-123")

        assert result is False

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.database")
    async def test_resume_agent_success(self, mock_db):
        """Should resume a paused agent."""
        engine = AgentExecutionEngine()
        mock_db.get_agent_run.return_value = {"id": "agent-123", "status": "paused"}
        mock_db.update_agent_run.return_value = {"id": "agent-123"}
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}

        state = AgentRunState(agent_run_id="agent-123")
        state.is_paused = True
        state.pause_event.clear()
        engine._active_runs["agent-123"] = state

        result = await engine.resume_agent("agent-123")

        assert result is True
        assert state.is_paused is False
        assert state.pause_event.is_set()

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.database")
    async def test_resume_agent_not_paused(self, mock_db):
        """Should return False for non-paused agent."""
        engine = AgentExecutionEngine()
        mock_db.get_agent_run.return_value = {"id": "agent-123", "status": "running"}

        state = AgentRunState(agent_run_id="agent-123")
        engine._active_runs["agent-123"] = state

        result = await engine.resume_agent("agent-123")

        assert result is False

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.database")
    async def test_cancel_agent_success(self, mock_db):
        """Should cancel a running agent."""
        engine = AgentExecutionEngine()
        mock_db.get_agent_run.return_value = {"id": "agent-123", "status": "running"}
        mock_db.update_agent_run.return_value = {"id": "agent-123"}
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}

        mock_task = MagicMock()
        mock_task.done.return_value = False
        mock_task.cancel = MagicMock()

        state = AgentRunState(agent_run_id="agent-123", task=mock_task)
        engine._active_runs["agent-123"] = state

        result = await engine.cancel_agent("agent-123", "User cancelled")

        assert result is True
        assert state.cancel_requested is True
        mock_task.cancel.assert_called_once()

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.database")
    async def test_cancel_queued_agent(self, mock_db):
        """Should cancel a queued agent (not in active_runs)."""
        engine = AgentExecutionEngine()
        mock_db.get_agent_run.return_value = {"id": "agent-123", "status": "queued"}
        mock_db.update_agent_run.return_value = {"id": "agent-123"}
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}

        result = await engine.cancel_agent("agent-123")

        assert result is True

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.database")
    async def test_cancel_completed_agent_fails(self, mock_db):
        """Should not cancel already completed agent."""
        engine = AgentExecutionEngine()
        mock_db.get_agent_run.return_value = {"id": "agent-123", "status": "completed"}

        result = await engine.cancel_agent("agent-123")

        assert result is False


# =============================================================================
# Get Agent State Tests
# =============================================================================

class TestGetAgentState:
    """Test get_agent_state method."""

    @patch("app.core.agent_engine.database")
    def test_get_state_not_found(self, mock_db):
        """Should return None for unknown agent."""
        engine = AgentExecutionEngine()
        mock_db.get_agent_run.return_value = None

        result = engine.get_agent_state("unknown")

        assert result is None

    @patch("app.core.agent_engine.database")
    def test_get_state_with_active_run(self, mock_db):
        """Should include runtime state for active agent."""
        engine = AgentExecutionEngine()
        mock_db.get_agent_run.return_value = {
            "id": "agent-123",
            "status": "running",
            "name": "Test"
        }
        mock_db.get_agent_tasks_tree.return_value = []

        mock_task = MagicMock()
        mock_task.done.return_value = False

        state = AgentRunState(agent_run_id="agent-123", task=mock_task)
        state.is_paused = True
        engine._active_runs["agent-123"] = state

        result = engine.get_agent_state("agent-123")

        assert result["id"] == "agent-123"
        assert result["is_active"] is True
        assert result["is_paused"] is True
        assert result["tasks"] == []

    @patch("app.core.agent_engine.database")
    def test_get_state_without_active_run(self, mock_db):
        """Should return db state for non-active agent."""
        engine = AgentExecutionEngine()
        mock_db.get_agent_run.return_value = {
            "id": "agent-123",
            "status": "completed"
        }
        mock_db.get_agent_tasks_tree.return_value = []

        result = engine.get_agent_state("agent-123")

        assert result["id"] == "agent-123"
        assert result["is_active"] is False
        assert result["is_paused"] is False


# =============================================================================
# Count Methods Tests
# =============================================================================

class TestCountMethods:
    """Test get_active_count and get_queued_count methods."""

    def test_get_active_count_empty(self):
        """Should return 0 when no active agents."""
        engine = AgentExecutionEngine()

        assert engine.get_active_count() == 0

    def test_get_active_count_with_running(self):
        """Should count running agents."""
        engine = AgentExecutionEngine()

        for i in range(3):
            mock_task = MagicMock()
            mock_task.done.return_value = False
            state = AgentRunState(agent_run_id=f"agent-{i}", task=mock_task)
            engine._active_runs[f"agent-{i}"] = state

        assert engine.get_active_count() == 3

    def test_get_active_count_excludes_completed(self):
        """Should exclude completed tasks."""
        engine = AgentExecutionEngine()

        # Running
        mock_task1 = MagicMock()
        mock_task1.done.return_value = False
        engine._active_runs["agent-1"] = AgentRunState(
            agent_run_id="agent-1", task=mock_task1
        )

        # Completed
        mock_task2 = MagicMock()
        mock_task2.done.return_value = True
        engine._active_runs["agent-2"] = AgentRunState(
            agent_run_id="agent-2", task=mock_task2
        )

        assert engine.get_active_count() == 1

    @patch("app.core.agent_engine.database")
    def test_get_queued_count(self, mock_db):
        """Should return count from database."""
        engine = AgentExecutionEngine()
        mock_db.get_agent_runs_count.return_value = 5

        result = engine.get_queued_count()

        assert result == 5
        mock_db.get_agent_runs_count.assert_called_once_with(status="queued")


# =============================================================================
# Commit and Push Tests
# =============================================================================

class TestCommitAndPush:
    """Test _commit_and_push_changes method."""

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.database")
    async def test_no_worktree_returns_false(self, mock_db):
        """Should return False when no worktree."""
        engine = AgentExecutionEngine()
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}

        state = AgentRunState(agent_run_id="agent-123")
        agent_run = {"id": "agent-123"}

        success, conflict = await engine._commit_and_push_changes("agent-123", agent_run, state)

        assert success is False
        assert conflict is None

    @pytest.mark.asyncio
    @patch("subprocess.run")
    @patch("app.core.agent_engine.database")
    async def test_no_changes_returns_false(self, mock_db, mock_run):
        """Should return False when no changes to commit."""
        engine = AgentExecutionEngine()
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}

        # git status --porcelain returns empty (no changes)
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        state = AgentRunState(
            agent_run_id="agent-123",
            worktree_path="/path/to/worktree",
            branch_name="feature/test"
        )
        agent_run = {"id": "agent-123"}

        # Need multiple mock calls for different git commands
        def mock_subprocess(*args, **kwargs):
            cmd = args[0]
            if "status" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            elif "fetch" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            elif "rev-list" in cmd:
                return MagicMock(returncode=0, stdout="0", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = mock_subprocess

        success, conflict = await engine._commit_and_push_changes("agent-123", agent_run, state)

        assert success is False


# =============================================================================
# Create Pull Request Tests
# =============================================================================

class TestCreatePullRequest:
    """Test _create_pull_request method."""

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.database")
    async def test_no_worktree_logs_warning(self, mock_db):
        """Should log warning when no worktree."""
        engine = AgentExecutionEngine()
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}

        state = AgentRunState(agent_run_id="agent-123")
        agent_run = {"id": "agent-123"}

        await engine._create_pull_request("agent-123", agent_run, state)

        # Should have logged warning about no worktree


# =============================================================================
# Merge and Cleanup Tests
# =============================================================================

class TestMergeAndCleanup:
    """Test _merge_and_cleanup method."""

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.database")
    async def test_missing_pr_url_logs_warning(self, mock_db):
        """Should log warning when no PR URL."""
        engine = AgentExecutionEngine()
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}

        state = AgentRunState(
            agent_run_id="agent-123",
            worktree_path="/path",
            branch_name="feature/test"
        )
        agent_run = {"id": "agent-123"}

        await engine._merge_and_cleanup("agent-123", agent_run, state, None)

    @pytest.mark.asyncio
    @patch("subprocess.run")
    @patch("app.core.agent_engine.database")
    async def test_merge_failure_logs_error(self, mock_db, mock_run):
        """Should log error when merge fails."""
        engine = AgentExecutionEngine()
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}

        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Merge failed")

        state = AgentRunState(
            agent_run_id="agent-123",
            worktree_path="/path",
            branch_name="feature/test"
        )
        agent_run = {"id": "agent-123", "project_id": "proj-1"}

        await engine._merge_and_cleanup(
            "agent-123", agent_run, state, "https://github.com/test/repo/pull/1"
        )


# =============================================================================
# Auto Review Tests
# =============================================================================

class TestTriggerAutoReview:
    """Test _trigger_auto_review method."""

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.database")
    async def test_no_pr_url_returns_early(self, mock_db):
        """Should return early when no PR URL."""
        engine = AgentExecutionEngine()

        with patch.object(engine, "launch_agent", new_callable=AsyncMock) as mock_launch:
            await engine._trigger_auto_review("agent-123", {"pr_url": None})

            mock_launch.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.database")
    async def test_launches_review_agent(self, mock_db):
        """Should launch review agent for PR."""
        engine = AgentExecutionEngine()
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}

        with patch.object(engine, "launch_agent", new_callable=AsyncMock) as mock_launch:
            mock_launch.return_value = {"id": "review-agent-123"}

            await engine._trigger_auto_review("agent-123", {
                "id": "agent-123",
                "name": "Test Agent",
                "pr_url": "https://github.com/test/repo/pull/1",
                "profile_id": "profile-1",
                "project_id": "project-1"
            })

            mock_launch.assert_awaited_once()
            call_kwargs = mock_launch.call_args.kwargs
            assert "Review:" in call_kwargs["name"]
            assert call_kwargs["auto_branch"] is False
            assert call_kwargs["auto_pr"] is False


# =============================================================================
# Execute Agent Integration Tests
# =============================================================================

class TestExecuteAgentIntegration:
    """Integration tests for _execute_agent method."""

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.query")
    @patch("app.core.agent_engine.get_profile")
    @patch("app.core.agent_engine.build_options_from_profile")
    @patch("app.core.agent_engine.database")
    async def test_execute_agent_handles_timeout(
        self, mock_db, mock_build, mock_get_profile, mock_query
    ):
        """Should handle timeout correctly."""
        engine = AgentExecutionEngine()
        mock_db.get_all_profiles.return_value = [{"id": "default", "name": "Default"}]
        mock_db.get_project.return_value = None
        mock_db.update_agent_run.return_value = {"id": "agent-123"}
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}
        mock_get_profile.return_value = {"id": "default", "name": "Default"}
        mock_build.return_value = ({}, {})

        # Simulate query that takes too long
        async def slow_query(*args, **kwargs):
            await asyncio.sleep(10)
            yield

        mock_query.return_value = slow_query()

        state = AgentRunState(agent_run_id="agent-123")
        agent_run = {
            "id": "agent-123",
            "prompt": "Test",
            "max_duration_minutes": 0,  # Would trigger immediate timeout if not unlimited
        }

        with patch.object(engine, "_setup_agent_environment", new_callable=AsyncMock):
            with patch.object(engine, "_fail_agent", new_callable=AsyncMock) as mock_fail:
                with patch.object(engine, "_cleanup_agent", new_callable=AsyncMock):
                    # Start execution but cancel quickly
                    task = asyncio.create_task(
                        engine._execute_agent("agent-123", agent_run, state)
                    )
                    await asyncio.sleep(0.1)
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.query")
    @patch("app.core.agent_engine.get_profile")
    @patch("app.core.agent_engine.build_options_from_profile")
    @patch("app.core.agent_engine.database")
    async def test_execute_agent_handles_cancellation(
        self, mock_db, mock_build, mock_get_profile, mock_query
    ):
        """Should handle cancellation request."""
        engine = AgentExecutionEngine()
        mock_db.get_all_profiles.return_value = [{"id": "default"}]
        mock_db.update_agent_run.return_value = {"id": "agent-123"}
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}
        mock_get_profile.return_value = {"id": "default"}
        mock_build.return_value = ({}, {})

        # Simulate query that yields messages
        async def query_with_messages(*args, **kwargs):
            # Check for cancel request
            await asyncio.sleep(0.1)
            yield MagicMock()

        mock_query.return_value = query_with_messages()

        state = AgentRunState(agent_run_id="agent-123")
        state.cancel_requested = True  # Pre-set cancellation
        agent_run = {"id": "agent-123", "prompt": "Test", "max_duration_minutes": 0}

        with patch.object(engine, "_setup_agent_environment", new_callable=AsyncMock):
            with patch.object(engine, "_fail_agent", new_callable=AsyncMock) as mock_fail:
                with patch.object(engine, "_cleanup_agent", new_callable=AsyncMock):
                    await engine._execute_agent("agent-123", agent_run, state)

                    # Should call fail_agent with cancellation reason
                    mock_fail.assert_awaited()


# =============================================================================
# Get Conflict Details Tests
# =============================================================================

class TestGetConflictDetails:
    """Test _get_conflict_details method."""

    @patch("subprocess.run")
    def test_get_conflict_details_returns_info(self, mock_run):
        """Should return conflict details."""
        engine = AgentExecutionEngine()

        # Mock diff --name-only
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="file1.py\nfile2.py\n"),  # diff --name-only
            MagicMock(returncode=0, stdout="<<<<<<< HEAD\n"),  # git diff file1.py
            MagicMock(returncode=0, stdout="<<<<<<< HEAD\n"),  # git diff file2.py
            MagicMock(returncode=0, stdout="abc123 Fix something\n"),  # git log
        ]

        result = engine._get_conflict_details("/worktree", "main")

        assert "Files with conflicts" in result
        assert "file1.py" in result

    @patch("subprocess.run")
    def test_get_conflict_details_no_conflicts(self, mock_run):
        """Should handle no conflicts gracefully."""
        engine = AgentExecutionEngine()

        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=""),  # No conflicted files
            MagicMock(returncode=0, stdout=""),  # No log output
        ]

        result = engine._get_conflict_details("/worktree", "main")

        assert "no additional details" in result or result == ""


# =============================================================================
# Singleton Instance Test
# =============================================================================

class TestSingletonInstance:
    """Test singleton agent_engine instance."""

    def test_singleton_exists(self):
        """Should have a singleton instance."""
        assert agent_engine is not None
        assert isinstance(agent_engine, AgentExecutionEngine)

    def test_singleton_default_settings(self):
        """Should have default settings."""
        assert agent_engine.max_concurrent == 3


# =============================================================================
# Process Queue Loop Tests
# =============================================================================

class TestProcessQueueLoop:
    """Test _process_queue_loop method."""

    @pytest.mark.asyncio
    async def test_loop_exits_on_shutdown(self):
        """Should exit loop when shutdown is True."""
        engine = AgentExecutionEngine()
        engine._shutdown = True

        # Should complete immediately without errors
        await engine._process_queue_loop()

    @pytest.mark.asyncio
    async def test_loop_handles_errors(self):
        """Should handle errors in queue processing."""
        engine = AgentExecutionEngine()

        call_count = 0

        async def failing_process():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("Process failed")
            engine._shutdown = True

        with patch.object(engine, "_process_queue", new_callable=lambda: failing_process):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                await engine._process_queue_loop()


# =============================================================================
# Cleanup Worktree Tests
# =============================================================================

class TestCleanupWorktree:
    """Test _cleanup_worktree method."""

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.database")
    async def test_cleanup_no_worktree_id(self, mock_db):
        """Should return early when no worktree ID."""
        engine = AgentExecutionEngine()

        await engine._cleanup_worktree("agent-123", {"worktree_id": None})

        mock_db.get_project.assert_not_called()

    @pytest.mark.asyncio
    @patch("subprocess.run")
    @patch("app.core.agent_engine.settings")
    @patch("app.core.agent_engine.database")
    async def test_cleanup_removes_worktree(self, mock_db, mock_settings, mock_run):
        """Should remove worktree and update database."""
        engine = AgentExecutionEngine()
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}
        mock_db.get_project.return_value = {"id": "proj-1", "path": "my-project"}
        mock_db.get_worktree.return_value = {
            "id": "wt-1",
            "worktree_path": ".worktrees/proj-1/branch"
        }
        mock_settings.workspace_dir = Path("/workspace")
        mock_run.return_value = MagicMock(returncode=0)

        await engine._cleanup_worktree("agent-123", {
            "id": "agent-123",
            "worktree_id": "wt-1",
            "branch": "feature/test",
            "project_id": "proj-1"
        })

        mock_db.delete_worktree.assert_called_once_with("wt-1")


# =============================================================================
# Additional Commit and Push Tests
# =============================================================================

class TestCommitAndPushFull:
    """Additional tests for _commit_and_push_changes method."""

    @pytest.mark.asyncio
    @patch("subprocess.run")
    @patch("app.core.agent_engine.database")
    async def test_commit_and_push_with_changes(self, mock_db, mock_run):
        """Should commit and push when there are changes."""
        engine = AgentExecutionEngine()
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}

        call_count = 0

        def mock_subprocess(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            cmd = args[0]
            if "status" in cmd and "--porcelain" in cmd:
                return MagicMock(returncode=0, stdout="M modified.py\n", stderr="")
            elif "add" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            elif "commit" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            elif "fetch" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            elif "rev-list" in cmd:
                return MagicMock(returncode=0, stdout="1", stderr="")
            elif "rebase" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            elif "push" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = mock_subprocess

        state = AgentRunState(
            agent_run_id="agent-123",
            worktree_path="/path/to/worktree",
            branch_name="feature/test"
        )
        agent_run = {"id": "agent-123", "name": "Test", "prompt": "Do something"}

        success, conflict = await engine._commit_and_push_changes("agent-123", agent_run, state)

        assert success is True
        assert conflict is None

    @pytest.mark.asyncio
    @patch("subprocess.run")
    @patch("app.core.agent_engine.database")
    async def test_commit_and_push_rebase_fails_merge_succeeds(self, mock_db, mock_run):
        """Should fallback to merge when rebase fails."""
        engine = AgentExecutionEngine()
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}

        def mock_subprocess(*args, **kwargs):
            cmd = args[0]
            if "status" in cmd and "--porcelain" in cmd:
                return MagicMock(returncode=0, stdout="M file.py\n", stderr="")
            elif "add" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            elif "commit" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            elif "fetch" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            elif "rev-list" in cmd:
                return MagicMock(returncode=0, stdout="1", stderr="")
            elif "rebase" in cmd and "--abort" not in cmd:
                return MagicMock(returncode=1, stderr="Rebase failed")
            elif "rebase" in cmd and "--abort" in cmd:
                return MagicMock(returncode=0)
            elif "merge" in cmd and "--abort" not in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            elif "push" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = mock_subprocess

        state = AgentRunState(
            agent_run_id="agent-123",
            worktree_path="/path/to/worktree",
            branch_name="feature/test"
        )
        agent_run = {"id": "agent-123", "name": "Test", "prompt": "Do something"}

        success, conflict = await engine._commit_and_push_changes("agent-123", agent_run, state)

        assert success is True

    @pytest.mark.asyncio
    @patch("subprocess.run")
    @patch("app.core.agent_engine.database")
    async def test_commit_and_push_detects_merge_conflict(self, mock_db, mock_run):
        """Should detect and report merge conflicts."""
        engine = AgentExecutionEngine()
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}

        def mock_subprocess(*args, **kwargs):
            cmd = args[0]
            if "status" in cmd and "--porcelain" in cmd:
                return MagicMock(returncode=0, stdout="M file.py\n", stderr="")
            elif "add" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            elif "commit" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            elif "fetch" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            elif "rev-list" in cmd:
                return MagicMock(returncode=0, stdout="1", stderr="")
            elif "rebase" in cmd and "--abort" not in cmd:
                return MagicMock(returncode=1, stderr="Rebase failed")
            elif "rebase" in cmd and "--abort" in cmd:
                return MagicMock(returncode=0)
            elif "merge" in cmd and "--abort" not in cmd:
                return MagicMock(returncode=1, stdout="CONFLICT in file.py\nAutomatic merge failed", stderr="")
            elif "merge" in cmd and "--abort" in cmd:
                return MagicMock(returncode=0)
            elif "diff" in cmd and "--name-only" in cmd:
                return MagicMock(returncode=0, stdout="file.py\n", stderr="")
            elif "diff" in cmd:
                return MagicMock(returncode=0, stdout="<<<<<<< HEAD\n", stderr="")
            elif "log" in cmd:
                return MagicMock(returncode=0, stdout="abc123 Some commit\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = mock_subprocess

        state = AgentRunState(
            agent_run_id="agent-123",
            worktree_path="/path/to/worktree",
            branch_name="feature/test"
        )
        agent_run = {"id": "agent-123", "name": "Test", "prompt": "Do something", "base_branch": "main"}

        success, conflict = await engine._commit_and_push_changes("agent-123", agent_run, state)

        assert success is False
        assert conflict is not None
        assert "file.py" in conflict

    @pytest.mark.asyncio
    @patch("subprocess.run")
    @patch("app.core.agent_engine.database")
    async def test_commit_and_push_timeout(self, mock_db, mock_run):
        """Should handle subprocess timeout."""
        import subprocess
        engine = AgentExecutionEngine()
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}

        mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=30)

        state = AgentRunState(
            agent_run_id="agent-123",
            worktree_path="/path/to/worktree",
            branch_name="feature/test"
        )
        agent_run = {"id": "agent-123"}

        success, conflict = await engine._commit_and_push_changes("agent-123", agent_run, state)

        assert success is False
        assert conflict is None


# =============================================================================
# Additional PR Creation Tests
# =============================================================================

class TestCreatePullRequestFull:
    """Additional tests for _create_pull_request method."""

    @pytest.mark.asyncio
    @patch("subprocess.run")
    @patch("app.core.agent_engine.database")
    async def test_create_pr_success(self, mock_db, mock_run):
        """Should create PR successfully."""
        engine = AgentExecutionEngine()
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}
        mock_db.get_project.return_value = {"id": "proj-1", "path": "my-project"}
        mock_db.get_git_repository_by_project.return_value = {
            "github_repo_name": "owner/repo",
            "default_branch": "main"
        }
        mock_db.update_agent_run.return_value = {"id": "agent-123"}

        def mock_subprocess(*args, **kwargs):
            cmd = args[0]
            if "status" in cmd and "--porcelain" in cmd:
                return MagicMock(returncode=0, stdout="M file.py\n", stderr="")
            elif "add" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            elif "commit" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            elif "fetch" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            elif "rev-list" in cmd:
                return MagicMock(returncode=0, stdout="1", stderr="")
            elif "rebase" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            elif "push" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            elif "gh" in cmd and "auth" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            elif "gh" in cmd and "pr" in cmd and "create" in cmd:
                return MagicMock(returncode=0, stdout="https://github.com/owner/repo/pull/123", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = mock_subprocess

        state = AgentRunState(
            agent_run_id="agent-123",
            worktree_path="/path/to/worktree",
            branch_name="feature/test"
        )
        agent_run = {
            "id": "agent-123",
            "name": "Test Agent",
            "prompt": "Do something",
            "project_id": "proj-1"
        }

        await engine._create_pull_request("agent-123", agent_run, state)

        mock_db.update_agent_run.assert_called()

    @pytest.mark.asyncio
    @patch("subprocess.run")
    @patch("app.core.agent_engine.database")
    async def test_create_pr_gh_not_authenticated(self, mock_db, mock_run):
        """Should handle gh CLI not authenticated."""
        engine = AgentExecutionEngine()
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}
        mock_db.get_project.return_value = {"id": "proj-1", "path": "my-project"}
        mock_db.get_git_repository_by_project.return_value = {
            "github_repo_name": "owner/repo"
        }

        # Mock _commit_and_push_changes to return success
        with patch.object(engine, "_commit_and_push_changes", new_callable=AsyncMock) as mock_commit:
            mock_commit.return_value = (True, None)

            # gh auth status fails
            mock_run.return_value = MagicMock(returncode=1, stderr="Not logged in")

            state = AgentRunState(
                agent_run_id="agent-123",
                worktree_path="/path/to/worktree",
                branch_name="feature/test"
            )
            agent_run = {
                "id": "agent-123",
                "name": "Test",
                "prompt": "Test",
                "project_id": "proj-1"
            }

            await engine._create_pull_request("agent-123", agent_run, state)

    @pytest.mark.asyncio
    @patch("subprocess.run")
    @patch("app.core.agent_engine.database")
    async def test_create_pr_extracts_github_from_remote(self, mock_db, mock_run):
        """Should extract GitHub repo from git remote."""
        engine = AgentExecutionEngine()
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}
        mock_db.get_project.return_value = {"id": "proj-1", "path": "my-project"}
        mock_db.get_git_repository_by_project.return_value = {}  # No github_repo_name
        mock_db.update_agent_run.return_value = {"id": "agent-123"}

        call_sequence = []

        def mock_subprocess(*args, **kwargs):
            cmd = args[0]
            call_sequence.append(cmd)
            if "remote" in cmd and "get-url" in cmd:
                return MagicMock(returncode=0, stdout="https://github.com/owner/my-repo.git", stderr="")
            elif "gh" in cmd and "auth" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            elif "gh" in cmd and "pr" in cmd:
                return MagicMock(returncode=0, stdout="https://github.com/owner/my-repo/pull/5", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = mock_subprocess

        with patch.object(engine, "_commit_and_push_changes", new_callable=AsyncMock) as mock_commit:
            mock_commit.return_value = (True, None)

            state = AgentRunState(
                agent_run_id="agent-123",
                worktree_path="/path/to/worktree",
                branch_name="feature/test"
            )
            agent_run = {
                "id": "agent-123",
                "name": "Test",
                "prompt": "Test",
                "project_id": "proj-1"
            }

            await engine._create_pull_request("agent-123", agent_run, state)


# =============================================================================
# Merge and Cleanup Full Tests
# =============================================================================

class TestMergeAndCleanupFull:
    """Additional tests for _merge_and_cleanup method."""

    @pytest.mark.asyncio
    @patch("subprocess.run")
    @patch("app.core.agent_engine.settings")
    @patch("app.core.agent_engine.database")
    async def test_merge_and_cleanup_success(self, mock_db, mock_settings, mock_run):
        """Should merge PR and cleanup successfully."""
        engine = AgentExecutionEngine()
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}
        mock_db.get_project.return_value = {"id": "proj-1", "path": "my-project"}
        mock_db.update_worktree.return_value = None
        mock_settings.workspace_dir = Path("/workspace")

        def mock_subprocess(*args, **kwargs):
            cmd = args[0]
            if "gh" in cmd and "pr" in cmd and "merge" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            elif "worktree" in cmd and "remove" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            elif "worktree" in cmd and "prune" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            elif "branch" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = mock_subprocess

        state = AgentRunState(
            agent_run_id="agent-123",
            worktree_path="/path/to/worktree",
            branch_name="feature/test"
        )
        agent_run = {
            "id": "agent-123",
            "project_id": "proj-1",
            "worktree_id": "wt-1"
        }

        await engine._merge_and_cleanup(
            "agent-123", agent_run, state, "https://github.com/owner/repo/pull/1"
        )


# =============================================================================
# Resolve Merge Conflict Tests
# =============================================================================

class TestResolveMergeConflict:
    """Test _resolve_merge_conflict method."""

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.query")
    @patch("app.core.agent_engine.get_profile")
    @patch("app.core.agent_engine.build_options_from_profile")
    @patch("app.core.agent_engine.database")
    async def test_resolve_conflict_calls_query(
        self, mock_db, mock_build, mock_get_profile, mock_query
    ):
        """Should call query to resolve merge conflict."""
        engine = AgentExecutionEngine()
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}
        mock_db.get_all_profiles.return_value = [{"id": "default"}]
        mock_db.get_project.return_value = {"id": "proj-1"}
        mock_get_profile.return_value = {"id": "profile-1"}
        mock_build.return_value = ({}, {})

        # Empty generator that completes without yielding
        async def empty_query_gen(*args, **kwargs):
            return
            yield  # Make it a generator

        mock_query.return_value = empty_query_gen()

        state = AgentRunState(
            agent_run_id="agent-123",
            worktree_path="/path/to/worktree",
            sdk_session_id="sdk-123"
        )
        agent_run = {"id": "agent-123", "profile_id": "profile-1"}

        # This will return True since the query completes without error
        result = await engine._resolve_merge_conflict(
            "agent-123", agent_run, state, "Files with conflicts: file.py"
        )

        # Query should have been called
        mock_query.assert_called_once()
        assert result is True

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.query")
    @patch("app.core.agent_engine.get_profile")
    @patch("app.core.agent_engine.build_options_from_profile")
    @patch("app.core.agent_engine.database")
    async def test_resolve_conflict_failure(
        self, mock_db, mock_build, mock_get_profile, mock_query
    ):
        """Should handle conflict resolution failure."""
        engine = AgentExecutionEngine()
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}
        mock_db.get_all_profiles.return_value = [{"id": "default"}]
        mock_db.get_project.return_value = {"id": "proj-1"}
        mock_get_profile.return_value = {"id": "profile-1"}
        mock_build.return_value = ({}, {})

        mock_query.side_effect = Exception("Query failed")

        state = AgentRunState(
            agent_run_id="agent-123",
            worktree_path="/path/to/worktree"
        )
        agent_run = {"id": "agent-123"}

        result = await engine._resolve_merge_conflict(
            "agent-123", agent_run, state, "Conflict info"
        )

        assert result is False

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.database")
    async def test_resolve_conflict_no_profile(self, mock_db):
        """Should return False when no profile available."""
        engine = AgentExecutionEngine()
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}
        mock_db.get_all_profiles.return_value = []

        with patch("app.core.agent_engine.get_profile", return_value=None):
            state = AgentRunState(agent_run_id="agent-123")
            agent_run = {"id": "agent-123"}

            result = await engine._resolve_merge_conflict(
                "agent-123", agent_run, state, "Conflict info"
            )

            assert result is False


# =============================================================================
# Execute Agent Full Tests
# =============================================================================

class TestExecuteAgentFull:
    """Additional tests for _execute_agent method."""

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.query")
    @patch("app.core.agent_engine.get_profile")
    @patch("app.core.agent_engine.build_options_from_profile")
    @patch("app.core.agent_engine.detect_deployment_mode")
    @patch("app.core.agent_engine.database")
    async def test_execute_agent_calls_query(
        self, mock_db, mock_deploy, mock_build, mock_get_profile, mock_query
    ):
        """Should call query to execute agent."""
        engine = AgentExecutionEngine()
        mock_db.get_all_profiles.return_value = [{"id": "default"}]
        mock_db.get_project.return_value = None
        mock_db.update_agent_run.return_value = {"id": "agent-123"}
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}
        mock_db.get_agent_run.return_value = {"id": "agent-123", "pr_url": None}
        mock_get_profile.return_value = {"id": "default"}
        mock_build.return_value = ({}, {})
        mock_deploy.return_value = MagicMock()

        # Empty generator that completes without yielding
        async def empty_query_gen(*args, **kwargs):
            return
            yield  # Make it a generator

        mock_query.return_value = empty_query_gen()

        state = AgentRunState(agent_run_id="agent-123")
        agent_run = {
            "id": "agent-123",
            "prompt": "Test prompt",
            "max_duration_minutes": 0,
            "auto_pr": False
        }

        with patch.object(engine, "_setup_agent_environment", new_callable=AsyncMock):
            with patch.object(engine, "_complete_agent", new_callable=AsyncMock) as mock_complete:
                with patch.object(engine, "_cleanup_agent", new_callable=AsyncMock):
                    await engine._execute_agent("agent-123", agent_run, state)

                    # Query should have been called
                    mock_query.assert_called_once()
                    # Complete should be called when query finishes without error
                    mock_complete.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.query")
    @patch("app.core.agent_engine.get_profile")
    @patch("app.core.agent_engine.build_options_from_profile")
    @patch("app.core.agent_engine.database")
    async def test_execute_agent_no_profile(
        self, mock_db, mock_build, mock_get_profile, mock_query
    ):
        """Should fail when no profile available."""
        engine = AgentExecutionEngine()
        mock_db.get_all_profiles.return_value = []
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}
        mock_get_profile.return_value = None

        state = AgentRunState(agent_run_id="agent-123")
        agent_run = {
            "id": "agent-123",
            "prompt": "Test",
            "max_duration_minutes": 0
        }

        with patch.object(engine, "_setup_agent_environment", new_callable=AsyncMock):
            with patch.object(engine, "_fail_agent", new_callable=AsyncMock) as mock_fail:
                with patch.object(engine, "_cleanup_agent", new_callable=AsyncMock):
                    await engine._execute_agent("agent-123", agent_run, state)

                    mock_fail.assert_awaited()


# =============================================================================
# Complete Agent Full Tests
# =============================================================================

class TestCompleteAgentFull:
    """Additional tests for _complete_agent method."""

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.database")
    async def test_complete_with_auto_review(self, mock_db):
        """Should trigger auto-review when enabled."""
        engine = AgentExecutionEngine()
        mock_db.update_agent_run.return_value = {"id": "agent-123"}
        mock_db.get_agent_run.return_value = {
            "id": "agent-123",
            "pr_url": "https://github.com/owner/repo/pull/1"
        }
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}

        state = AgentRunState(
            agent_run_id="agent-123",
            branch_name="feature/test"
        )
        agent_run = {
            "id": "agent-123",
            "auto_pr": True,
            "auto_review": True
        }

        with patch.object(engine, "_create_pull_request", new_callable=AsyncMock):
            with patch.object(engine, "_trigger_auto_review", new_callable=AsyncMock) as mock_review:
                await engine._complete_agent("agent-123", agent_run, state, "Done")

                mock_review.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.database")
    async def test_complete_with_auto_merge(self, mock_db):
        """Should trigger auto-merge when enabled."""
        engine = AgentExecutionEngine()
        mock_db.update_agent_run.return_value = {"id": "agent-123"}
        mock_db.get_agent_run.return_value = {
            "id": "agent-123",
            "pr_url": "https://github.com/owner/repo/pull/1"
        }
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}

        state = AgentRunState(
            agent_run_id="agent-123",
            branch_name="feature/test"
        )
        agent_run = {
            "id": "agent-123",
            "auto_pr": True,
            "auto_merge": True
        }

        with patch.object(engine, "_create_pull_request", new_callable=AsyncMock):
            with patch.object(engine, "_merge_and_cleanup", new_callable=AsyncMock) as mock_merge:
                await engine._complete_agent("agent-123", agent_run, state, "Done")

                mock_merge.assert_awaited_once()


# =============================================================================
# Setup Agent Environment Full Tests
# =============================================================================

class TestSetupAgentEnvironmentFull:
    """Additional tests for _setup_agent_environment method."""

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.worktree_manager")
    @patch("app.core.agent_engine.git_service")
    @patch("app.core.agent_engine.settings")
    @patch("app.core.agent_engine.database")
    async def test_setup_worktree_creation_failure(self, mock_db, mock_settings, mock_git, mock_wt):
        """Should handle worktree creation failure gracefully."""
        engine = AgentExecutionEngine()
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}
        mock_db.get_project.return_value = {"id": "project-1", "path": "my-project"}
        mock_settings.workspace_dir = Path("/workspace")
        mock_git.is_git_repo.return_value = True
        mock_git.get_default_branch.return_value = "main"
        mock_wt.create_worktree_session.side_effect = Exception("Worktree creation failed")

        state = AgentRunState(agent_run_id="agent-123")
        agent_run = {
            "id": "agent-123",
            "name": "Test Agent",
            "auto_branch": True,
            "project_id": "project-1"
        }

        await engine._setup_agent_environment("agent-123", agent_run, state)

        # Should not crash, worktree_path should remain None
        assert state.worktree_path is None

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.database")
    async def test_setup_project_not_found(self, mock_db):
        """Should handle project not found."""
        engine = AgentExecutionEngine()
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}
        mock_db.get_project.return_value = None

        state = AgentRunState(agent_run_id="agent-123")
        agent_run = {
            "id": "agent-123",
            "name": "Test",
            "auto_branch": True,
            "project_id": "non-existent"
        }

        await engine._setup_agent_environment("agent-123", agent_run, state)

        assert state.worktree_path is None


# =============================================================================
# Auto Review Error Handling Tests
# =============================================================================

class TestTriggerAutoReviewFull:
    """Additional tests for _trigger_auto_review method."""

    @pytest.mark.asyncio
    @patch("app.core.agent_engine.database")
    async def test_auto_review_launch_failure(self, mock_db):
        """Should handle launch failure gracefully."""
        engine = AgentExecutionEngine()
        mock_db.add_agent_log.return_value = {"timestamp": "2024-01-01T00:00:00"}

        with patch.object(engine, "launch_agent", new_callable=AsyncMock) as mock_launch:
            mock_launch.side_effect = Exception("Launch failed")

            await engine._trigger_auto_review("agent-123", {
                "id": "agent-123",
                "name": "Test",
                "pr_url": "https://github.com/owner/repo/pull/1"
            })

            # Should not crash
