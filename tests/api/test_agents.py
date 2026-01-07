"""
Comprehensive tests for the agents API endpoints.

Tests cover:
- Agent CRUD operations (launch, list, get, delete)
- Agent control endpoints (pause, resume, cancel)
- Agent statistics and logs
- Bulk operations (clear-completed, clear-failed)
- WebSocket broadcast functionality
- Request/Response model validation
- Helper functions
"""

import pytest
import asyncio
from datetime import datetime
from unittest.mock import patch, MagicMock, AsyncMock


class TestAgentModuleImports:
    """Verify agents module can be imported correctly."""

    def test_agents_module_imports(self):
        """Agents module should import without errors."""
        from app.api import agents
        assert agents is not None

    def test_agents_router_exists(self):
        """Agents router should exist."""
        from app.api.agents import router
        assert router is not None
        assert router.prefix == "/api/v1/agents"

    def test_agent_status_enum_imports(self):
        """AgentStatus enum should be importable."""
        from app.core.agent_engine import AgentStatus
        assert AgentStatus.QUEUED.value == "queued"
        assert AgentStatus.RUNNING.value == "running"
        assert AgentStatus.PAUSED.value == "paused"
        assert AgentStatus.COMPLETED.value == "completed"
        assert AgentStatus.FAILED.value == "failed"


class TestRequestResponseModels:
    """Test Pydantic models for request/response validation."""

    def test_agent_launch_request_valid(self):
        """AgentLaunchRequest should accept valid data."""
        from app.api.agents import AgentLaunchRequest

        request = AgentLaunchRequest(
            name="Test Agent",
            prompt="Do something useful"
        )
        assert request.name == "Test Agent"
        assert request.prompt == "Do something useful"
        assert request.auto_branch is True
        assert request.auto_pr is True
        assert request.auto_merge is False
        assert request.auto_review is False
        assert request.max_duration_minutes == 0
        assert request.profile_id is None
        assert request.project_id is None
        assert request.base_branch is None

    def test_agent_launch_request_with_all_fields(self):
        """AgentLaunchRequest should accept all optional fields."""
        from app.api.agents import AgentLaunchRequest

        request = AgentLaunchRequest(
            name="Full Agent",
            prompt="Complete task",
            profile_id="profile-123",
            project_id="project-456",
            auto_branch=True,
            auto_pr=True,
            auto_merge=True,
            auto_review=True,
            max_duration_minutes=120,
            base_branch="develop"
        )
        assert request.profile_id == "profile-123"
        assert request.project_id == "project-456"
        assert request.auto_merge is True
        assert request.auto_review is True
        assert request.max_duration_minutes == 120
        assert request.base_branch == "develop"

    def test_agent_launch_request_validation(self):
        """AgentLaunchRequest should validate inputs."""
        from app.api.agents import AgentLaunchRequest
        from pydantic import ValidationError

        # Name too long
        with pytest.raises(ValidationError):
            AgentLaunchRequest(name="x" * 101, prompt="test")

        # Empty name
        with pytest.raises(ValidationError):
            AgentLaunchRequest(name="", prompt="test")

        # Empty prompt
        with pytest.raises(ValidationError):
            AgentLaunchRequest(name="test", prompt="")

        # Negative max_duration_minutes
        with pytest.raises(ValidationError):
            AgentLaunchRequest(name="test", prompt="test", max_duration_minutes=-1)

        # max_duration_minutes too high
        with pytest.raises(ValidationError):
            AgentLaunchRequest(name="test", prompt="test", max_duration_minutes=500)

    def test_agent_response_model(self):
        """AgentResponse should accept valid data."""
        from app.api.agents import AgentResponse

        response = AgentResponse(
            id="agent-123",
            name="Test Agent",
            prompt="Do something",
            status="running",
            progress=50.0,
            started_at=datetime.utcnow()
        )
        assert response.id == "agent-123"
        assert response.progress == 50.0
        assert response.branch is None
        assert response.pr_url is None
        assert response.tasks == []

    def test_agent_response_with_all_fields(self):
        """AgentResponse should accept all optional fields."""
        from app.api.agents import AgentResponse, AgentTask

        response = AgentResponse(
            id="agent-123",
            name="Test Agent",
            prompt="Do something",
            status="completed",
            progress=100.0,
            branch="agent/test-branch",
            pr_url="https://github.com/org/repo/pull/123",
            tasks=[AgentTask(id="t1", name="Task 1", status="completed")],
            started_at=datetime.utcnow(),
            completed_at=datetime.utcnow(),
            error=None,
            result_summary="Agent completed successfully",
            profile_id="profile-1",
            project_id="project-1",
            worktree_id="wt-1",
            auto_branch=True,
            auto_pr=True,
            auto_review=False,
            max_duration_minutes=60
        )
        assert response.branch == "agent/test-branch"
        assert response.pr_url == "https://github.com/org/repo/pull/123"
        assert len(response.tasks) == 1
        assert response.result_summary == "Agent completed successfully"

    def test_agent_task_model(self):
        """AgentTask model should work correctly."""
        from app.api.agents import AgentTask

        task = AgentTask(
            id="task-1",
            name="Some task",
            status="pending",
            children=None
        )
        assert task.id == "task-1"
        assert task.children is None

        # Test with nested children
        parent_task = AgentTask(
            id="parent",
            name="Parent task",
            status="in_progress",
            children=[task]
        )
        assert len(parent_task.children) == 1
        assert parent_task.children[0].id == "task-1"

    def test_agent_log_entry_model(self):
        """AgentLogEntry model should work correctly."""
        from app.api.agents import AgentLogEntry

        log = AgentLogEntry(
            timestamp=datetime.utcnow(),
            level="info",
            message="Test message",
            metadata={"key": "value"}
        )
        assert log.level == "info"
        assert log.metadata["key"] == "value"

        # Without metadata
        log_no_meta = AgentLogEntry(
            timestamp=datetime.utcnow(),
            level="error",
            message="Error message"
        )
        assert log_no_meta.metadata is None

    def test_agent_list_response_model(self):
        """AgentListResponse model should work correctly."""
        from app.api.agents import AgentListResponse, AgentResponse

        response = AgentListResponse(
            agents=[
                AgentResponse(
                    id="agent-1",
                    name="Agent 1",
                    prompt="Task 1",
                    status="completed",
                    progress=100.0,
                    started_at=datetime.utcnow()
                )
            ],
            total=1
        )
        assert len(response.agents) == 1
        assert response.total == 1

    def test_agent_logs_response_model(self):
        """AgentLogsResponse model should work correctly."""
        from app.api.agents import AgentLogsResponse, AgentLogEntry

        response = AgentLogsResponse(
            agent_id="agent-123",
            logs=[
                AgentLogEntry(
                    timestamp=datetime.utcnow(),
                    level="info",
                    message="Test log"
                )
            ],
            total=1,
            offset=0,
            limit=100
        )
        assert response.agent_id == "agent-123"
        assert response.limit == 100
        assert len(response.logs) == 1

    def test_agent_stats_response_model(self):
        """AgentStatsResponse model should work correctly."""
        from app.api.agents import AgentStatsResponse

        response = AgentStatsResponse(
            total=10,
            running=2,
            queued=1,
            completed=5,
            failed=2,
            success_rate=71.4,
            avg_duration_minutes=15.5,
            by_day={"2024-01-01": 3, "2024-01-02": 7}
        )
        assert response.total == 10
        assert response.success_rate == 71.4
        assert response.by_day["2024-01-01"] == 3


class TestHelperFunctions:
    """Test helper functions in the agents module."""

    def test_task_to_model(self):
        """_task_to_model should convert dict to AgentTask."""
        from app.api.agents import _task_to_model

        task_dict = {
            "id": "task-123",
            "name": "Test Task",
            "status": "completed",
            "children": []
        }

        result = _task_to_model(task_dict)
        assert result.id == "task-123"
        assert result.name == "Test Task"
        assert result.status == "completed"
        assert result.children is None  # Empty list becomes None

    def test_task_to_model_with_children(self):
        """_task_to_model should handle nested children."""
        from app.api.agents import _task_to_model

        task_dict = {
            "id": "parent",
            "name": "Parent Task",
            "status": "in_progress",
            "children": [
                {
                    "id": "child-1",
                    "name": "Child Task",
                    "status": "pending",
                    "children": []
                }
            ]
        }

        result = _task_to_model(task_dict)
        assert len(result.children) == 1
        assert result.children[0].id == "child-1"

    def test_task_to_model_deeply_nested(self):
        """_task_to_model should handle deeply nested children."""
        from app.api.agents import _task_to_model

        task_dict = {
            "id": "grandparent",
            "name": "Grandparent Task",
            "status": "in_progress",
            "children": [
                {
                    "id": "parent",
                    "name": "Parent Task",
                    "status": "completed",
                    "children": [
                        {
                            "id": "child",
                            "name": "Child Task",
                            "status": "pending",
                            "children": []
                        }
                    ]
                }
            ]
        }

        result = _task_to_model(task_dict)
        assert result.id == "grandparent"
        assert result.children[0].id == "parent"
        assert result.children[0].children[0].id == "child"

    def test_task_to_model_no_children_key(self):
        """_task_to_model should handle missing children key."""
        from app.api.agents import _task_to_model

        task_dict = {
            "id": "task-solo",
            "name": "Solo Task",
            "status": "pending"
        }

        result = _task_to_model(task_dict)
        assert result.children is None

    def test_create_agent_response(self):
        """_create_agent_response should convert agent data to response model."""
        from app.api.agents import _create_agent_response

        with patch("app.api.agents.database") as mock_db:
            mock_db.get_agent_tasks_tree.return_value = []

            agent_data = {
                "id": "agent-abc",
                "name": "Test Agent",
                "prompt": "Test prompt",
                "status": "queued",
                "progress": 0.0,
                "branch": None,
                "pr_url": None,
                "started_at": datetime.utcnow(),
                "completed_at": None,
                "error": None,
                "result_summary": None,
                "profile_id": "profile-1",
                "project_id": "project-1",
                "worktree_id": None,
                "auto_branch": True,
                "auto_pr": True,
                "auto_review": False,
                "max_duration_minutes": 30
            }

            result = _create_agent_response(agent_data)
            assert result.id == "agent-abc"
            assert result.status == "queued"
            assert result.profile_id == "profile-1"
            mock_db.get_agent_tasks_tree.assert_called_once_with("agent-abc")

    def test_create_agent_response_with_tasks(self):
        """_create_agent_response should include tasks."""
        from app.api.agents import _create_agent_response

        with patch("app.api.agents.database") as mock_db:
            mock_db.get_agent_tasks_tree.return_value = [
                {"id": "task-1", "name": "Task 1", "status": "completed", "children": []}
            ]

            agent_data = {
                "id": "agent-abc",
                "name": "Test Agent",
                "prompt": "Test prompt",
                "status": "running",
                "progress": 50.0,
                "branch": "agent/test",
                "pr_url": None,
                "started_at": datetime.utcnow(),
                "completed_at": None,
                "error": None,
                "result_summary": None,
                "profile_id": None,
                "project_id": None,
                "worktree_id": None,
                "auto_branch": True,
                "auto_pr": True,
                "auto_review": False,
                "max_duration_minutes": 0
            }

            result = _create_agent_response(agent_data)
            assert len(result.tasks) == 1
            assert result.tasks[0].name == "Task 1"


class TestLaunchAgentEndpoint:
    """Test POST /api/v1/agents/launch endpoint function."""

    @pytest.mark.asyncio
    async def test_launch_agent_success(self):
        """Launching an agent should work."""
        from app.api.agents import launch_agent, AgentLaunchRequest

        with patch("app.api.agents.agent_engine") as mock_engine:
            with patch("app.api.agents.database") as mock_db:
                mock_engine.launch_agent = AsyncMock(return_value={
                    "id": "agent-test123",
                    "name": "Test Agent",
                    "prompt": "Do something",
                    "status": "queued",
                    "progress": 0.0,
                    "started_at": datetime.utcnow(),
                    "branch": None,
                    "pr_url": None,
                    "profile_id": None,
                    "project_id": None,
                    "worktree_id": None,
                    "auto_branch": True,
                    "auto_pr": True,
                    "auto_review": False,
                    "max_duration_minutes": 0
                })
                mock_db.get_agent_tasks_tree.return_value = []

                request = AgentLaunchRequest(
                    name="Test Agent",
                    prompt="Do something"
                )

                result = await launch_agent(request, token="test-token")

                assert result.id == "agent-test123"
                assert result.name == "Test Agent"
                assert result.status == "queued"
                mock_engine.launch_agent.assert_called_once()

    @pytest.mark.asyncio
    async def test_launch_agent_with_all_options(self):
        """Launching an agent with all options should work."""
        from app.api.agents import launch_agent, AgentLaunchRequest

        with patch("app.api.agents.agent_engine") as mock_engine:
            with patch("app.api.agents.database") as mock_db:
                mock_engine.launch_agent = AsyncMock(return_value={
                    "id": "agent-full",
                    "name": "Full Test",
                    "prompt": "Do everything",
                    "status": "queued",
                    "progress": 0.0,
                    "started_at": datetime.utcnow(),
                    "branch": None,
                    "pr_url": None,
                    "profile_id": "profile-1",
                    "project_id": "project-1",
                    "worktree_id": None,
                    "auto_branch": True,
                    "auto_pr": True,
                    "auto_merge": True,
                    "auto_review": True,
                    "max_duration_minutes": 60,
                    "base_branch": "develop"
                })
                mock_db.get_agent_tasks_tree.return_value = []

                request = AgentLaunchRequest(
                    name="Full Test",
                    prompt="Do everything",
                    profile_id="profile-1",
                    project_id="project-1",
                    auto_merge=True,
                    auto_review=True,
                    max_duration_minutes=60,
                    base_branch="develop"
                )

                result = await launch_agent(request, token="test-token")

                assert result.id == "agent-full"
                mock_engine.launch_agent.assert_called_once_with(
                    name="Full Test",
                    prompt="Do everything",
                    profile_id="profile-1",
                    project_id="project-1",
                    auto_branch=True,
                    auto_pr=True,
                    auto_merge=True,
                    auto_review=True,
                    max_duration_minutes=60,
                    base_branch="develop"
                )


class TestListAgentsEndpoint:
    """Test GET /api/v1/agents endpoint function."""

    @pytest.mark.asyncio
    async def test_list_agents_empty(self):
        """Listing agents when none exist should return empty list."""
        from app.api.agents import list_agents

        with patch("app.api.agents.database") as mock_db:
            mock_db.get_agent_runs.return_value = []
            mock_db.get_agent_runs_count.return_value = 0

            result = await list_agents(token="test-token")

            assert result.agents == []
            assert result.total == 0

    @pytest.mark.asyncio
    async def test_list_agents_with_data(self):
        """Listing agents should return all agents."""
        from app.api.agents import list_agents

        with patch("app.api.agents.database") as mock_db:
            mock_db.get_agent_runs.return_value = [
                {
                    "id": "agent-1",
                    "name": "Agent 1",
                    "prompt": "Task 1",
                    "status": "running",
                    "progress": 50.0,
                    "started_at": datetime.utcnow(),
                    "branch": "agent/test",
                    "pr_url": None,
                    "profile_id": None,
                    "project_id": None,
                    "worktree_id": None,
                    "auto_branch": True,
                    "auto_pr": True,
                    "auto_review": False,
                    "max_duration_minutes": 30
                }
            ]
            mock_db.get_agent_runs_count.return_value = 1
            mock_db.get_agent_tasks_tree.return_value = []

            result = await list_agents(token="test-token")

            assert len(result.agents) == 1
            assert result.agents[0].name == "Agent 1"
            assert result.total == 1

    @pytest.mark.asyncio
    async def test_list_agents_with_filters(self):
        """Listing agents with filters should work."""
        from app.api.agents import list_agents

        with patch("app.api.agents.database") as mock_db:
            mock_db.get_agent_runs.return_value = []
            mock_db.get_agent_runs_count.return_value = 0

            await list_agents(
                status_filter="running",
                project_id="proj-123",
                limit=10,
                offset=20,
                token="test-token"
            )

            mock_db.get_agent_runs.assert_called_once_with(
                status="running",
                project_id="proj-123",
                limit=10,
                offset=20
            )


class TestGetAgentStatsEndpoint:
    """Test GET /api/v1/agents/stats endpoint function."""

    @pytest.mark.asyncio
    async def test_get_stats_default(self):
        """Getting stats with default params should work."""
        from app.api.agents import get_stats

        with patch("app.api.agents.database") as mock_db:
            mock_db.get_agent_run_stats.return_value = {
                "total": 10,
                "running": 2,
                "queued": 1,
                "completed": 5,
                "failed": 2,
                "success_rate": 71.4,
                "avg_duration_minutes": 15.5,
                "by_day": {"2024-01-01": 3}
            }

            result = await get_stats(token="test-token")

            assert result.total == 10
            assert result.success_rate == 71.4
            # Function is called - don't check exact args due to Query wrappers
            mock_db.get_agent_run_stats.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_stats_with_params(self):
        """Getting stats with custom params should work."""
        from app.api.agents import get_stats

        with patch("app.api.agents.database") as mock_db:
            mock_db.get_agent_run_stats.return_value = {
                "total": 5,
                "running": 1,
                "queued": 0,
                "completed": 4,
                "failed": 0,
                "success_rate": 100.0,
                "avg_duration_minutes": 10.0,
                "by_day": {}
            }

            result = await get_stats(days=30, project_id="proj-1", token="test-token")

            assert result.total == 5
            mock_db.get_agent_run_stats.assert_called_once_with(days=30, project_id="proj-1")


class TestGetAgentEndpoint:
    """Test GET /api/v1/agents/{agent_id} endpoint function."""

    @pytest.mark.asyncio
    async def test_get_agent_success(self):
        """Getting an existing agent should work."""
        from app.api.agents import get_agent

        with patch("app.api.agents.database") as mock_db:
            mock_db.get_agent_run.return_value = {
                "id": "agent-123",
                "name": "Test Agent",
                "prompt": "Do something",
                "status": "running",
                "progress": 75.0,
                "started_at": datetime.utcnow(),
                "completed_at": None,
                "error": None,
                "result_summary": None,
                "branch": "agent/test",
                "pr_url": None,
                "profile_id": None,
                "project_id": None,
                "worktree_id": None,
                "auto_branch": True,
                "auto_pr": True,
                "auto_review": False,
                "max_duration_minutes": 30
            }
            mock_db.get_agent_tasks_tree.return_value = []

            result = await get_agent("agent-123", token="test-token")

            assert result.id == "agent-123"
            assert result.progress == 75.0

    @pytest.mark.asyncio
    async def test_get_agent_not_found(self):
        """Getting a non-existent agent should raise HTTPException."""
        from app.api.agents import get_agent
        from fastapi import HTTPException

        with patch("app.api.agents.database") as mock_db:
            mock_db.get_agent_run.return_value = None

            with pytest.raises(HTTPException) as exc_info:
                await get_agent("nonexistent", token="test-token")

            assert exc_info.value.status_code == 404
            assert "not found" in exc_info.value.detail.lower()


class TestGetAgentLogsEndpoint:
    """Test GET /api/v1/agents/{agent_id}/logs endpoint function."""

    @pytest.mark.asyncio
    async def test_get_logs_success(self):
        """Getting logs for an existing agent should work."""
        from app.api.agents import get_agent_logs

        with patch("app.api.agents.database") as mock_db:
            mock_db.get_agent_run.return_value = {"id": "agent-123", "status": "running"}
            mock_db.get_agent_logs.return_value = [
                {
                    "timestamp": datetime.utcnow(),
                    "level": "info",
                    "message": "Started execution",
                    "metadata": None
                }
            ]
            mock_db.get_agent_logs_count.return_value = 1

            # Pass actual values for Query-wrapped parameters
            result = await get_agent_logs(
                "agent-123",
                limit=100,
                offset=0,
                level=None,
                token="test-token"
            )

            assert result.agent_id == "agent-123"
            assert len(result.logs) == 1
            assert result.total == 1
            mock_db.get_agent_logs.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_logs_with_filter(self):
        """Getting logs with level filter should work."""
        from app.api.agents import get_agent_logs

        with patch("app.api.agents.database") as mock_db:
            mock_db.get_agent_run.return_value = {"id": "agent-123", "status": "running"}
            mock_db.get_agent_logs.return_value = []
            mock_db.get_agent_logs_count.return_value = 0

            # Pass actual values for Query-wrapped parameters
            await get_agent_logs(
                "agent-123",
                limit=100,
                offset=0,
                level="error",
                token="test-token"
            )

            mock_db.get_agent_logs.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_logs_agent_not_found(self):
        """Getting logs for non-existent agent should raise HTTPException."""
        from app.api.agents import get_agent_logs
        from fastapi import HTTPException

        with patch("app.api.agents.database") as mock_db:
            mock_db.get_agent_run.return_value = None

            with pytest.raises(HTTPException) as exc_info:
                await get_agent_logs(
                    "nonexistent",
                    limit=100,
                    offset=0,
                    level=None,
                    token="test-token"
                )

            assert exc_info.value.status_code == 404


class TestPauseAgentEndpoint:
    """Test POST /api/v1/agents/{agent_id}/pause endpoint function."""

    @pytest.mark.asyncio
    async def test_pause_running_agent_success(self):
        """Pausing a running agent should work."""
        from app.api.agents import pause_agent

        with patch("app.api.agents.database") as mock_db:
            with patch("app.api.agents.agent_engine") as mock_engine:
                mock_db.get_agent_run.return_value = {"id": "agent-123", "status": "running"}
                mock_engine.pause_agent = AsyncMock(return_value=True)

                result = await pause_agent("agent-123", token="test-token")

                assert result["status"] == "ok"
                mock_engine.pause_agent.assert_called_once_with("agent-123")

    @pytest.mark.asyncio
    async def test_pause_non_running_agent(self):
        """Pausing a non-running agent should fail."""
        from app.api.agents import pause_agent
        from fastapi import HTTPException

        with patch("app.api.agents.database") as mock_db:
            mock_db.get_agent_run.return_value = {"id": "agent-123", "status": "queued"}

            with pytest.raises(HTTPException) as exc_info:
                await pause_agent("agent-123", token="test-token")

            assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_pause_agent_engine_failure(self):
        """Pausing should fail if engine returns False."""
        from app.api.agents import pause_agent
        from fastapi import HTTPException

        with patch("app.api.agents.database") as mock_db:
            with patch("app.api.agents.agent_engine") as mock_engine:
                mock_db.get_agent_run.return_value = {"id": "agent-123", "status": "running"}
                mock_engine.pause_agent = AsyncMock(return_value=False)

                with pytest.raises(HTTPException) as exc_info:
                    await pause_agent("agent-123", token="test-token")

                assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_pause_agent_not_found(self):
        """Pausing a non-existent agent should return 404."""
        from app.api.agents import pause_agent
        from fastapi import HTTPException

        with patch("app.api.agents.database") as mock_db:
            mock_db.get_agent_run.return_value = None

            with pytest.raises(HTTPException) as exc_info:
                await pause_agent("nonexistent", token="test-token")

            assert exc_info.value.status_code == 404


class TestResumeAgentEndpoint:
    """Test POST /api/v1/agents/{agent_id}/resume endpoint function."""

    @pytest.mark.asyncio
    async def test_resume_paused_agent_success(self):
        """Resuming a paused agent should work."""
        from app.api.agents import resume_agent

        with patch("app.api.agents.database") as mock_db:
            with patch("app.api.agents.agent_engine") as mock_engine:
                mock_db.get_agent_run.return_value = {"id": "agent-123", "status": "paused"}
                mock_engine.resume_agent = AsyncMock(return_value=True)

                result = await resume_agent("agent-123", token="test-token")

                assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_resume_non_paused_agent(self):
        """Resuming a non-paused agent should fail."""
        from app.api.agents import resume_agent
        from fastapi import HTTPException

        with patch("app.api.agents.database") as mock_db:
            mock_db.get_agent_run.return_value = {"id": "agent-123", "status": "running"}

            with pytest.raises(HTTPException) as exc_info:
                await resume_agent("agent-123", token="test-token")

            assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_resume_agent_not_found(self):
        """Resuming a non-existent agent should return 404."""
        from app.api.agents import resume_agent
        from fastapi import HTTPException

        with patch("app.api.agents.database") as mock_db:
            mock_db.get_agent_run.return_value = None

            with pytest.raises(HTTPException) as exc_info:
                await resume_agent("nonexistent", token="test-token")

            assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_resume_agent_engine_failure(self):
        """Resuming should fail if engine returns False."""
        from app.api.agents import resume_agent
        from fastapi import HTTPException

        with patch("app.api.agents.database") as mock_db:
            with patch("app.api.agents.agent_engine") as mock_engine:
                mock_db.get_agent_run.return_value = {"id": "agent-123", "status": "paused"}
                mock_engine.resume_agent = AsyncMock(return_value=False)

                with pytest.raises(HTTPException) as exc_info:
                    await resume_agent("agent-123", token="test-token")

                assert exc_info.value.status_code == 400


class TestCancelAgentEndpoint:
    """Test POST /api/v1/agents/{agent_id}/cancel endpoint function."""

    @pytest.mark.asyncio
    async def test_cancel_running_agent_success(self):
        """Cancelling a running agent should work."""
        from app.api.agents import cancel_agent

        with patch("app.api.agents.database") as mock_db:
            with patch("app.api.agents.agent_engine") as mock_engine:
                mock_db.get_agent_run.return_value = {"id": "agent-123", "status": "running"}
                mock_engine.cancel_agent = AsyncMock(return_value=True)

                result = await cancel_agent("agent-123", token="test-token")

                assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_cancel_completed_agent(self):
        """Cancelling a completed agent should fail."""
        from app.api.agents import cancel_agent
        from fastapi import HTTPException

        with patch("app.api.agents.database") as mock_db:
            mock_db.get_agent_run.return_value = {"id": "agent-123", "status": "completed"}

            with pytest.raises(HTTPException) as exc_info:
                await cancel_agent("agent-123", token="test-token")

            assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_cancel_agent_not_found(self):
        """Cancelling a non-existent agent should return 404."""
        from app.api.agents import cancel_agent
        from fastapi import HTTPException

        with patch("app.api.agents.database") as mock_db:
            mock_db.get_agent_run.return_value = None

            with pytest.raises(HTTPException) as exc_info:
                await cancel_agent("nonexistent", token="test-token")

            assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_cancel_agent_engine_failure(self):
        """Cancelling should fail if engine returns False."""
        from app.api.agents import cancel_agent
        from fastapi import HTTPException

        with patch("app.api.agents.database") as mock_db:
            with patch("app.api.agents.agent_engine") as mock_engine:
                mock_db.get_agent_run.return_value = {"id": "agent-123", "status": "running"}
                mock_engine.cancel_agent = AsyncMock(return_value=False)

                with pytest.raises(HTTPException) as exc_info:
                    await cancel_agent("agent-123", token="test-token")

                assert exc_info.value.status_code == 400


class TestDeleteAgentEndpoint:
    """Test DELETE /api/v1/agents/{agent_id} endpoint function."""

    @pytest.mark.asyncio
    async def test_delete_completed_agent_success(self):
        """Deleting a completed agent should work."""
        from app.api.agents import delete_agent

        with patch("app.api.agents.database") as mock_db:
            with patch("app.api.agents._broadcast_agent_update", new_callable=AsyncMock):
                mock_db.get_agent_run.return_value = {"id": "agent-123", "status": "completed"}

                # Should not raise
                await delete_agent("agent-123", token="test-token")

                mock_db.delete_agent_run.assert_called_once_with("agent-123")

    @pytest.mark.asyncio
    async def test_delete_running_agent(self):
        """Deleting a running agent should fail."""
        from app.api.agents import delete_agent
        from fastapi import HTTPException

        with patch("app.api.agents.database") as mock_db:
            mock_db.get_agent_run.return_value = {"id": "agent-123", "status": "running"}

            with pytest.raises(HTTPException) as exc_info:
                await delete_agent("agent-123", token="test-token")

            assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_delete_agent_not_found(self):
        """Deleting a non-existent agent should return 404."""
        from app.api.agents import delete_agent
        from fastapi import HTTPException

        with patch("app.api.agents.database") as mock_db:
            mock_db.get_agent_run.return_value = None

            with pytest.raises(HTTPException) as exc_info:
                await delete_agent("nonexistent", token="test-token")

            assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_failed_agent_success(self):
        """Deleting a failed agent should work."""
        from app.api.agents import delete_agent

        with patch("app.api.agents.database") as mock_db:
            with patch("app.api.agents._broadcast_agent_update", new_callable=AsyncMock):
                mock_db.get_agent_run.return_value = {"id": "agent-123", "status": "failed"}

                await delete_agent("agent-123", token="test-token")

                mock_db.delete_agent_run.assert_called_once_with("agent-123")


class TestBulkOperationsEndpoints:
    """Test bulk operation endpoints."""

    @pytest.mark.asyncio
    async def test_clear_completed_agents(self):
        """Clearing completed agents should work."""
        from app.api.agents import clear_completed_agents

        with patch("app.api.agents.database") as mock_db:
            mock_db.get_agent_runs.return_value = [
                {"id": "agent-1"},
                {"id": "agent-2"},
                {"id": "agent-3"}
            ]

            result = await clear_completed_agents(token="test-token")

            assert result["deleted"] == 3
            assert mock_db.delete_agent_run.call_count == 3

    @pytest.mark.asyncio
    async def test_clear_completed_agents_empty(self):
        """Clearing when no completed agents should return 0."""
        from app.api.agents import clear_completed_agents

        with patch("app.api.agents.database") as mock_db:
            mock_db.get_agent_runs.return_value = []

            result = await clear_completed_agents(token="test-token")

            assert result["deleted"] == 0

    @pytest.mark.asyncio
    async def test_clear_failed_agents(self):
        """Clearing failed agents should work."""
        from app.api.agents import clear_failed_agents

        with patch("app.api.agents.database") as mock_db:
            mock_db.get_agent_runs.return_value = [
                {"id": "agent-1"},
                {"id": "agent-2"}
            ]

            result = await clear_failed_agents(token="test-token")

            assert result["deleted"] == 2


class TestBroadcastFunction:
    """Test the WebSocket broadcast functionality."""

    @pytest.mark.asyncio
    async def test_broadcast_agent_update_connected(self):
        """Broadcasting should send to connected WebSockets."""
        from app.api.agents import _broadcast_agent_update, _agent_websockets
        from fastapi.websockets import WebSocketState

        mock_ws = MagicMock()
        mock_ws.client_state = WebSocketState.CONNECTED
        mock_ws.send_json = AsyncMock()

        _agent_websockets.clear()
        _agent_websockets.add(mock_ws)

        try:
            await _broadcast_agent_update("agent-123", "agent_progress", {"progress": 50})

            mock_ws.send_json.assert_called_once()
            call_args = mock_ws.send_json.call_args[0][0]
            assert call_args["type"] == "agent_progress"
            assert call_args["agent_id"] == "agent-123"
        finally:
            _agent_websockets.clear()

    @pytest.mark.asyncio
    async def test_broadcast_agent_update_disconnected(self):
        """Broadcasting should clean up disconnected WebSockets."""
        from app.api.agents import _broadcast_agent_update, _agent_websockets
        from fastapi.websockets import WebSocketState

        mock_ws = MagicMock()
        mock_ws.client_state = WebSocketState.DISCONNECTED

        _agent_websockets.clear()
        _agent_websockets.add(mock_ws)

        try:
            await _broadcast_agent_update("agent-123", "agent_progress", {"progress": 50})

            assert mock_ws not in _agent_websockets
        finally:
            _agent_websockets.clear()

    @pytest.mark.asyncio
    async def test_broadcast_agent_update_send_failure(self):
        """Broadcasting should handle send failures gracefully."""
        from app.api.agents import _broadcast_agent_update, _agent_websockets
        from fastapi.websockets import WebSocketState

        mock_ws = MagicMock()
        mock_ws.client_state = WebSocketState.CONNECTED
        mock_ws.send_json = AsyncMock(side_effect=Exception("Connection closed"))

        _agent_websockets.clear()
        _agent_websockets.add(mock_ws)

        try:
            await _broadcast_agent_update("agent-123", "agent_progress", {"progress": 50})
            assert mock_ws not in _agent_websockets
        finally:
            _agent_websockets.clear()

    @pytest.mark.asyncio
    async def test_broadcast_to_multiple_sockets(self):
        """Broadcasting should send to all connected WebSockets."""
        from app.api.agents import _broadcast_agent_update, _agent_websockets
        from fastapi.websockets import WebSocketState

        mock_ws1 = MagicMock()
        mock_ws1.client_state = WebSocketState.CONNECTED
        mock_ws1.send_json = AsyncMock()

        mock_ws2 = MagicMock()
        mock_ws2.client_state = WebSocketState.CONNECTED
        mock_ws2.send_json = AsyncMock()

        _agent_websockets.clear()
        _agent_websockets.add(mock_ws1)
        _agent_websockets.add(mock_ws2)

        try:
            await _broadcast_agent_update("agent-123", "agent_completed", {"status": "completed"})

            mock_ws1.send_json.assert_called_once()
            mock_ws2.send_json.assert_called_once()
        finally:
            _agent_websockets.clear()


class TestEngineLifecycle:
    """Test engine lifecycle functions."""

    @pytest.mark.asyncio
    async def test_start_agent_engine(self):
        """Starting the agent engine should work."""
        from app.api.agents import start_agent_engine

        with patch("app.api.agents.agent_engine") as mock_engine:
            mock_engine.start = AsyncMock()

            await start_agent_engine()

            mock_engine.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_agent_engine(self):
        """Stopping the agent engine should work."""
        from app.api.agents import stop_agent_engine

        with patch("app.api.agents.agent_engine") as mock_engine:
            mock_engine.stop = AsyncMock()

            await stop_agent_engine()

            mock_engine.stop.assert_called_once()


class TestProgressValidation:
    """Test progress field validation."""

    def test_progress_bounds(self):
        """Progress should be bounded 0-100."""
        from app.api.agents import AgentResponse
        from pydantic import ValidationError

        # Valid progress values
        for progress in [0.0, 50.0, 100.0]:
            response = AgentResponse(
                id="test",
                name="Test",
                prompt="Test",
                status="running",
                progress=progress,
                started_at=datetime.utcnow()
            )
            assert response.progress == progress

        # Invalid progress (below 0)
        with pytest.raises(ValidationError):
            AgentResponse(
                id="test",
                name="Test",
                prompt="Test",
                status="running",
                progress=-1.0,
                started_at=datetime.utcnow()
            )

        # Invalid progress (above 100)
        with pytest.raises(ValidationError):
            AgentResponse(
                id="test",
                name="Test",
                prompt="Test",
                status="running",
                progress=101.0,
                started_at=datetime.utcnow()
            )


class TestMaxDurationValidation:
    """Test max_duration_minutes field validation."""

    def test_max_duration_bounds(self):
        """max_duration_minutes should be bounded 0-480."""
        from app.api.agents import AgentLaunchRequest
        from pydantic import ValidationError

        # Valid values at boundaries
        for duration in [0, 240, 480]:
            request = AgentLaunchRequest(name="test", prompt="test", max_duration_minutes=duration)
            assert request.max_duration_minutes == duration

        # Invalid values
        with pytest.raises(ValidationError):
            AgentLaunchRequest(name="test", prompt="test", max_duration_minutes=-1)

        with pytest.raises(ValidationError):
            AgentLaunchRequest(name="test", prompt="test", max_duration_minutes=481)


class TestNameValidation:
    """Test name field validation."""

    def test_name_length_bounds(self):
        """Name should be bounded 1-100 characters."""
        from app.api.agents import AgentLaunchRequest
        from pydantic import ValidationError

        # Valid names at boundaries
        request = AgentLaunchRequest(name="A", prompt="test")
        assert request.name == "A"

        request = AgentLaunchRequest(name="x" * 100, prompt="test")
        assert len(request.name) == 100

        # Invalid names
        with pytest.raises(ValidationError):
            AgentLaunchRequest(name="", prompt="test")

        with pytest.raises(ValidationError):
            AgentLaunchRequest(name="x" * 101, prompt="test")
