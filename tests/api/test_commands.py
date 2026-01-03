"""
Tests for commands API endpoints (app/api/commands.py).

These tests cover:
- Command listing endpoints
- Command detail endpoints
- Command execution
- Sync after rewind
- Rewind API V2 (checkpoints and execute)
- Legacy rewind endpoints
- Rewind status and clear endpoints
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from dataclasses import dataclass
from typing import Optional


# =============================================================================
# Test Fixtures
# =============================================================================

@pytest.fixture
def mock_database():
    """Mock the database module for command tests."""
    with patch("app.api.commands.database") as mock_db:
        yield mock_db


@pytest.fixture
def mock_settings():
    """Mock the settings module."""
    with patch("app.api.commands.settings") as mock_settings:
        mock_settings.workspace_dir = MagicMock()
        mock_settings.workspace_dir.__truediv__ = lambda self, x: MagicMock(__str__=lambda s: f"/workspace/{x}")
        yield mock_settings


@pytest.fixture
def mock_slash_commands():
    """Mock slash_commands module."""
    with patch("app.api.commands.get_command_by_name") as mock_get_cmd, \
         patch("app.api.commands.get_all_commands") as mock_get_all, \
         patch("app.api.commands.is_interactive_command") as mock_is_interactive, \
         patch("app.api.commands.is_rest_api_command") as mock_is_rest, \
         patch("app.api.commands.get_rest_api_command_info") as mock_rest_info, \
         patch("app.api.commands.parse_command_input") as mock_parse:
        yield {
            "get_command_by_name": mock_get_cmd,
            "get_all_commands": mock_get_all,
            "is_interactive_command": mock_is_interactive,
            "is_rest_api_command": mock_is_rest,
            "get_rest_api_command_info": mock_rest_info,
            "parse_command_input": mock_parse,
        }


@pytest.fixture
def mock_jsonl_rewind_service():
    """Mock the JSONL rewind service."""
    with patch("app.api.commands.jsonl_rewind_service") as mock_jrs:
        yield mock_jrs


@pytest.fixture
def mock_sync_engine():
    """Mock the sync engine."""
    with patch("app.api.commands.sync_engine") as mock_se:
        mock_se.broadcast_session_rewound = AsyncMock()
        yield mock_se


@dataclass
class MockSlashCommand:
    """Mock slash command for testing."""
    name: str
    description: str
    content: str
    file_path: str = "/test/path"
    source: str = "project"
    namespace: Optional[str] = None
    allowed_tools: list = None
    argument_hint: Optional[str] = None
    model: Optional[str] = None
    disable_model_invocation: bool = False

    def __post_init__(self):
        if self.allowed_tools is None:
            self.allowed_tools = []

    def get_display_name(self) -> str:
        return f"/{self.name}"

    def expand_prompt(self, arguments: str = "") -> str:
        return self.content.replace("$ARGUMENTS", arguments)


@dataclass
class MockCheckpoint:
    """Mock checkpoint for testing."""
    uuid: str
    index: int
    message_preview: str
    full_message: str
    timestamp: Optional[str] = None
    git_ref: Optional[str] = None


@dataclass
class MockRewindResult:
    """Mock rewind result for testing."""
    success: bool
    message: str
    chat_rewound: bool = False
    code_rewound: bool = False
    messages_removed: int = 0
    error: Optional[str] = None


# =============================================================================
# Tests for get_working_dir_for_project helper
# =============================================================================

class TestGetWorkingDirForProject:
    """Test the get_working_dir_for_project helper function."""

    def test_returns_workspace_dir_when_no_project(self, mock_database, mock_settings):
        """Should return workspace dir when project_id is None."""
        from app.api.commands import get_working_dir_for_project

        result = get_working_dir_for_project(None)

        mock_database.get_project.assert_not_called()

    def test_returns_workspace_dir_when_project_not_found(self, mock_database, mock_settings):
        """Should return workspace dir when project doesn't exist."""
        from app.api.commands import get_working_dir_for_project

        mock_database.get_project.return_value = None

        result = get_working_dir_for_project("nonexistent-project")

        mock_database.get_project.assert_called_once_with("nonexistent-project")

    def test_returns_project_path_when_project_exists(self, mock_database, mock_settings):
        """Should return project path when project exists."""
        from app.api.commands import get_working_dir_for_project

        mock_database.get_project.return_value = {"id": "test-project", "path": "my-project"}

        result = get_working_dir_for_project("test-project")

        mock_database.get_project.assert_called_once_with("test-project")


# =============================================================================
# Tests for list_commands endpoint
# =============================================================================

class TestListCommandsEndpoint:
    """Tests for GET /api/v1/commands/ endpoint."""

    @pytest.mark.asyncio
    async def test_list_commands_without_project(self, mock_slash_commands, mock_settings, mock_database):
        """Should list commands from workspace when no project specified."""
        from app.api.commands import list_commands

        mock_database.get_project.return_value = None
        mock_slash_commands["get_all_commands"].return_value = [
            {"name": "test-cmd", "display": "/test-cmd", "description": "Test", "type": "custom"}
        ]

        response = await list_commands(project_id=None)

        assert response.count == 1
        assert len(response.commands) == 1
        assert response.commands[0].name == "test-cmd"

    @pytest.mark.asyncio
    async def test_list_commands_with_project(self, mock_slash_commands, mock_settings, mock_database):
        """Should list commands from project directory when project specified."""
        from app.api.commands import list_commands

        mock_database.get_project.return_value = {"id": "proj-1", "path": "project-path"}
        mock_slash_commands["get_all_commands"].return_value = [
            {"name": "cmd1", "display": "/cmd1", "description": "Command 1", "type": "custom"},
            {"name": "cmd2", "display": "/cmd2", "description": "Command 2", "type": "sdk_builtin"},
        ]

        response = await list_commands(project_id="proj-1")

        assert response.count == 2
        assert len(response.commands) == 2

    @pytest.mark.asyncio
    async def test_list_commands_empty(self, mock_slash_commands, mock_settings, mock_database):
        """Should return empty list when no commands exist."""
        from app.api.commands import list_commands

        mock_database.get_project.return_value = None
        mock_slash_commands["get_all_commands"].return_value = []

        response = await list_commands(project_id=None)

        assert response.count == 0
        assert response.commands == []


# =============================================================================
# Tests for get_command endpoint
# =============================================================================

class TestGetCommandEndpoint:
    """Tests for GET /api/v1/commands/{command_name} endpoint."""

    @pytest.mark.asyncio
    async def test_get_interactive_command(self, mock_slash_commands, mock_settings, mock_database):
        """Should return interactive command info."""
        from app.api.commands import get_command

        mock_slash_commands["is_interactive_command"].return_value = True

        # Mock INTERACTIVE_COMMANDS in the slash_commands module
        with patch("app.core.slash_commands.INTERACTIVE_COMMANDS", {"resume": {"description": "Resume conversation"}}):
            response = await get_command("resume", project_id=None)

        assert response.name == "resume"
        assert response.display == "/resume"
        assert response.type == "interactive"
        assert response.is_interactive is True

    @pytest.mark.asyncio
    async def test_get_custom_command(self, mock_slash_commands, mock_settings, mock_database):
        """Should return custom command info."""
        from app.api.commands import get_command

        mock_slash_commands["is_interactive_command"].return_value = False
        mock_database.get_project.return_value = None

        mock_cmd = MockSlashCommand(
            name="test-command",
            description="Test description",
            content="Test content",
            argument_hint="<arg>",
            model="opus"
        )
        mock_slash_commands["get_command_by_name"].return_value = mock_cmd

        response = await get_command("test-command", project_id=None)

        assert response.name == "test-command"
        assert response.display == "/test-command"
        assert response.description == "Test description"
        assert response.content == "Test content"
        assert response.type == "custom"
        assert response.argument_hint == "<arg>"
        assert response.model == "opus"
        assert response.is_interactive is False

    @pytest.mark.asyncio
    async def test_get_command_not_found(self, mock_slash_commands, mock_settings, mock_database):
        """Should raise 404 when command not found."""
        from app.api.commands import get_command
        from fastapi import HTTPException

        mock_slash_commands["is_interactive_command"].return_value = False
        mock_database.get_project.return_value = None
        mock_slash_commands["get_command_by_name"].return_value = None

        with pytest.raises(HTTPException) as exc_info:
            await get_command("nonexistent", project_id=None)

        assert exc_info.value.status_code == 404
        assert "Command not found" in str(exc_info.value.detail)


# =============================================================================
# Tests for execute_command endpoint
# =============================================================================

class TestExecuteCommandEndpoint:
    """Tests for POST /api/v1/commands/execute endpoint."""

    @pytest.mark.asyncio
    async def test_execute_invalid_command_format(self, mock_slash_commands, mock_database):
        """Should raise 400 when command format is invalid."""
        from app.api.commands import execute_command, ExecuteCommandRequest
        from fastapi import HTTPException

        mock_slash_commands["parse_command_input"].return_value = ("", "invalid")

        request = ExecuteCommandRequest(command="invalid", session_id="session-1")

        with pytest.raises(HTTPException) as exc_info:
            await execute_command(request)

        assert exc_info.value.status_code == 400
        assert "Invalid command format" in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_execute_interactive_command(self, mock_slash_commands, mock_database):
        """Should return is_interactive=True for interactive commands."""
        from app.api.commands import execute_command, ExecuteCommandRequest

        mock_slash_commands["parse_command_input"].return_value = ("resume", "")
        mock_slash_commands["is_interactive_command"].return_value = True

        request = ExecuteCommandRequest(command="/resume", session_id="session-1")

        response = await execute_command(request)

        assert response.success is True
        assert response.is_interactive is True
        assert "requires interactive terminal" in response.message

    @pytest.mark.asyncio
    async def test_execute_rest_api_command(self, mock_slash_commands, mock_database):
        """Should return API endpoint for REST API commands."""
        from app.api.commands import execute_command, ExecuteCommandRequest

        mock_slash_commands["parse_command_input"].return_value = ("rewind", "")
        mock_slash_commands["is_interactive_command"].return_value = False
        mock_slash_commands["is_rest_api_command"].return_value = True
        mock_slash_commands["get_rest_api_command_info"].return_value = {
            "api_endpoint": "/api/v1/commands/rewind/checkpoints/{session_id}"
        }

        request = ExecuteCommandRequest(command="/rewind", session_id="session-123")

        response = await execute_command(request)

        assert response.success is True
        assert "uses REST API" in response.message
        assert "session-123" in response.message

    @pytest.mark.asyncio
    async def test_execute_session_not_found(self, mock_slash_commands, mock_database, mock_settings):
        """Should raise 404 when session not found."""
        from app.api.commands import execute_command, ExecuteCommandRequest
        from fastapi import HTTPException

        mock_slash_commands["parse_command_input"].return_value = ("my-command", "arg1")
        mock_slash_commands["is_interactive_command"].return_value = False
        mock_slash_commands["is_rest_api_command"].return_value = False
        mock_database.get_session.return_value = None

        request = ExecuteCommandRequest(command="/my-command arg1", session_id="nonexistent")

        with pytest.raises(HTTPException) as exc_info:
            await execute_command(request)

        assert exc_info.value.status_code == 404
        assert "Session not found" in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_execute_command_not_found(self, mock_slash_commands, mock_database, mock_settings):
        """Should raise 404 when custom command not found."""
        from app.api.commands import execute_command, ExecuteCommandRequest
        from fastapi import HTTPException

        mock_slash_commands["parse_command_input"].return_value = ("unknown-cmd", "")
        mock_slash_commands["is_interactive_command"].return_value = False
        mock_slash_commands["is_rest_api_command"].return_value = False
        mock_database.get_session.return_value = {"id": "session-1", "project_id": None}
        mock_database.get_project.return_value = None
        mock_slash_commands["get_command_by_name"].return_value = None

        request = ExecuteCommandRequest(command="/unknown-cmd", session_id="session-1")

        with pytest.raises(HTTPException) as exc_info:
            await execute_command(request)

        assert exc_info.value.status_code == 404
        assert "Command not found" in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_execute_command_missing_required_arguments(self, mock_slash_commands, mock_database, mock_settings):
        """Should return failure when required arguments are missing."""
        from app.api.commands import execute_command, ExecuteCommandRequest

        mock_slash_commands["parse_command_input"].return_value = ("my-command", "")
        mock_slash_commands["is_interactive_command"].return_value = False
        mock_slash_commands["is_rest_api_command"].return_value = False
        mock_database.get_session.return_value = {"id": "session-1", "project_id": None}
        mock_database.get_project.return_value = None

        mock_cmd = MockSlashCommand(
            name="my-command",
            description="Test",
            content="Do something with $ARGUMENTS",
            argument_hint="<required-arg>"
        )
        mock_slash_commands["get_command_by_name"].return_value = mock_cmd

        request = ExecuteCommandRequest(command="/my-command", session_id="session-1")

        response = await execute_command(request)

        assert response.success is False
        assert "requires arguments" in response.message
        assert response.expanded_prompt is None

    @pytest.mark.asyncio
    async def test_execute_command_success(self, mock_slash_commands, mock_database, mock_settings):
        """Should expand prompt and return success."""
        from app.api.commands import execute_command, ExecuteCommandRequest

        mock_slash_commands["parse_command_input"].return_value = ("my-command", "test-arg")
        mock_slash_commands["is_interactive_command"].return_value = False
        mock_slash_commands["is_rest_api_command"].return_value = False
        mock_database.get_session.return_value = {"id": "session-1", "project_id": "proj-1"}
        mock_database.get_project.return_value = {"id": "proj-1", "path": "my-project"}

        mock_cmd = MockSlashCommand(
            name="my-command",
            description="Test",
            content="Process: $ARGUMENTS",
        )
        mock_slash_commands["get_command_by_name"].return_value = mock_cmd

        request = ExecuteCommandRequest(command="/my-command test-arg", session_id="session-1")

        response = await execute_command(request)

        assert response.success is True
        assert response.message == "Command expanded successfully"
        assert response.expanded_prompt == "Process: test-arg"
        assert response.is_interactive is False


# =============================================================================
# Tests for sync_after_rewind endpoint
# =============================================================================

class TestSyncAfterRewindEndpoint:
    """Tests for POST /api/v1/commands/sync-after-rewind endpoint."""

    @pytest.mark.asyncio
    async def test_sync_invalid_restore_option(self, mock_database):
        """Should raise 400 for invalid restore option."""
        from app.api.commands import sync_after_rewind
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await sync_after_rewind(
                session_id="session-1",
                checkpoint_message="test message",
                restore_option=5
            )

        assert exc_info.value.status_code == 400
        assert "Invalid restore option" in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_sync_option_3_no_sync_needed(self, mock_database):
        """Should return no sync needed for option 3 (code only)."""
        from app.api.commands import sync_after_rewind

        response = await sync_after_rewind(
            session_id="session-1",
            checkpoint_message="test message",
            restore_option=3
        )

        assert response["success"] is True
        assert response["deleted_count"] == 0
        assert "No chat sync needed" in response["message"]

    @pytest.mark.asyncio
    async def test_sync_option_4_no_sync_needed(self, mock_database):
        """Should return no sync needed for option 4 (cancel)."""
        from app.api.commands import sync_after_rewind

        response = await sync_after_rewind(
            session_id="session-1",
            checkpoint_message="test message",
            restore_option=4
        )

        assert response["success"] is True
        assert response["deleted_count"] == 0

    @pytest.mark.asyncio
    async def test_sync_session_not_found(self, mock_database):
        """Should raise 404 when session not found."""
        from app.api.commands import sync_after_rewind
        from fastapi import HTTPException

        mock_database.get_session.return_value = None

        with pytest.raises(HTTPException) as exc_info:
            await sync_after_rewind(
                session_id="nonexistent",
                checkpoint_message="test message",
                restore_option=1
            )

        assert exc_info.value.status_code == 404
        assert "Session not found" in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_sync_checkpoint_not_found(self, mock_database):
        """Should return failure when checkpoint message not found."""
        from app.api.commands import sync_after_rewind

        mock_database.get_session.return_value = {"id": "session-1"}
        mock_database.get_session_messages.return_value = [
            {"id": 1, "role": "user", "content": "different message"},
            {"id": 2, "role": "assistant", "content": "response"},
        ]

        response = await sync_after_rewind(
            session_id="session-1",
            checkpoint_message="checkpoint message not in history",
            restore_option=1
        )

        assert response["success"] is False
        assert "Could not find checkpoint message" in response["message"]

    @pytest.mark.asyncio
    async def test_sync_checkpoint_found_exact_match(self, mock_database):
        """Should find checkpoint and delete messages after it."""
        from app.api.commands import sync_after_rewind

        mock_database.get_session.return_value = {"id": "session-1"}
        mock_database.get_session_messages.return_value = [
            {"id": 1, "role": "user", "content": "first message"},
            {"id": 2, "role": "assistant", "content": "response 1"},
            {"id": 3, "role": "user", "content": "checkpoint message"},
            {"id": 4, "role": "assistant", "content": "response 2"},
            {"id": 5, "role": "user", "content": "after checkpoint"},
        ]
        mock_database.delete_session_message.return_value = True

        response = await sync_after_rewind(
            session_id="session-1",
            checkpoint_message="checkpoint message",
            restore_option=1
        )

        assert response["success"] is True
        assert response["deleted_count"] == 2  # Messages 4 and 5
        assert response["checkpoint_index"] == 2

    @pytest.mark.asyncio
    async def test_sync_checkpoint_found_partial_match(self, mock_database):
        """Should find checkpoint with partial match."""
        from app.api.commands import sync_after_rewind

        mock_database.get_session.return_value = {"id": "session-1"}
        mock_database.get_session_messages.return_value = [
            {"id": 1, "role": "user", "content": "checkpoint message that is very long"},
            {"id": 2, "role": "assistant", "content": "response"},
        ]
        mock_database.delete_session_message.return_value = True

        response = await sync_after_rewind(
            session_id="session-1",
            checkpoint_message="checkpoint message that is very long and continues...",
            restore_option=2
        )

        assert response["success"] is True

    @pytest.mark.asyncio
    async def test_sync_delete_message_raises_exception(self, mock_database):
        """Should continue even when delete_session_message raises an exception."""
        from app.api.commands import sync_after_rewind

        mock_database.get_session.return_value = {"id": "session-1"}
        mock_database.get_session_messages.return_value = [
            {"id": 1, "role": "user", "content": "checkpoint message"},
            {"id": 2, "role": "assistant", "content": "response 1"},
            {"id": 3, "role": "user", "content": "after checkpoint"},
        ]
        # First call succeeds, second call raises exception
        mock_database.delete_session_message.side_effect = [True, Exception("DB error")]

        response = await sync_after_rewind(
            session_id="session-1",
            checkpoint_message="checkpoint message",
            restore_option=1
        )

        # Should still return success, but with only 1 deleted
        assert response["success"] is True
        assert response["deleted_count"] == 1


# =============================================================================
# Tests for get_rewind_checkpoints endpoint (V2)
# =============================================================================

class TestGetRewindCheckpointsEndpoint:
    """Tests for GET /api/v1/commands/rewind/checkpoints/{session_id} endpoint."""

    @pytest.mark.asyncio
    async def test_checkpoints_session_not_found(self, mock_database):
        """Should raise 404 when session not found."""
        from app.api.commands import get_rewind_checkpoints
        from fastapi import HTTPException

        mock_database.get_session.return_value = None

        with pytest.raises(HTTPException) as exc_info:
            await get_rewind_checkpoints("nonexistent")

        assert exc_info.value.status_code == 404
        assert "Session not found" in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_checkpoints_no_sdk_session(self, mock_database):
        """Should return error when no SDK session ID."""
        from app.api.commands import get_rewind_checkpoints

        mock_database.get_session.return_value = {"id": "session-1", "sdk_session_id": None}

        response = await get_rewind_checkpoints("session-1")

        assert response.success is False
        assert "no SDK session ID" in response.error

    @pytest.mark.asyncio
    async def test_checkpoints_from_jsonl_service(self, mock_database, mock_jsonl_rewind_service, mock_settings):
        """Should return checkpoints from JSONL rewind service."""
        from app.api.commands import get_rewind_checkpoints

        mock_database.get_session.return_value = {
            "id": "session-1",
            "sdk_session_id": "sdk-session-1",
            "project_id": None
        }
        mock_database.get_project.return_value = None
        mock_jsonl_rewind_service.get_checkpoints.return_value = [
            MockCheckpoint(
                uuid="uuid-1",
                index=0,
                message_preview="First message",
                full_message="First message content",
                timestamp="2024-01-01T00:00:00",
            )
        ]

        response = await get_rewind_checkpoints("session-1")

        assert response.success is True
        assert response.session_id == "session-1"
        assert response.sdk_session_id == "sdk-session-1"
        assert len(response.checkpoints) == 1
        assert response.checkpoints[0].uuid == "uuid-1"

    @pytest.mark.asyncio
    async def test_checkpoints_fallback_to_database(self, mock_database, mock_jsonl_rewind_service, mock_settings):
        """Should fallback to database when JSONL not found."""
        from app.api.commands import get_rewind_checkpoints

        mock_database.get_session.return_value = {
            "id": "session-1",
            "sdk_session_id": "sdk-session-1",
            "project_id": None
        }
        mock_database.get_project.return_value = None
        mock_jsonl_rewind_service.get_checkpoints.return_value = []
        mock_database.get_session_messages.return_value = [
            {"id": 1, "role": "user", "content": "Hello", "created_at": "2024-01-01"},
            {"id": 2, "role": "assistant", "content": "Hi there"},
            {"id": 3, "role": "user", "content": "How are you?", "created_at": "2024-01-02"},
        ]

        response = await get_rewind_checkpoints("session-1")

        assert response.success is True
        assert len(response.checkpoints) == 2  # Only user messages
        assert response.checkpoints[0].uuid == "db-1"
        assert response.checkpoints[1].uuid == "db-3"


# =============================================================================
# Tests for execute_rewind endpoint (V2)
# =============================================================================

class TestExecuteRewindEndpoint:
    """Tests for POST /api/v1/commands/rewind/execute/{session_id} endpoint."""

    @pytest.mark.asyncio
    async def test_rewind_session_not_found(self, mock_database):
        """Should raise 404 when session not found."""
        from app.api.commands import execute_rewind, RewindRequestV2
        from fastapi import HTTPException

        mock_database.get_session.return_value = None

        request = RewindRequestV2(target_uuid="uuid-1")

        with pytest.raises(HTTPException) as exc_info:
            await execute_rewind("nonexistent", request)

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_rewind_no_sdk_session(self, mock_database):
        """Should return error when no SDK session ID."""
        from app.api.commands import execute_rewind, RewindRequestV2

        mock_database.get_session.return_value = {"id": "session-1", "sdk_session_id": None}

        request = RewindRequestV2(target_uuid="uuid-1")

        response = await execute_rewind("session-1", request)

        assert response.success is False
        assert "no SDK session ID" in response.error

    @pytest.mark.asyncio
    async def test_rewind_db_sourced_uuid_rejected(self, mock_database):
        """Should reject database-sourced UUIDs."""
        from app.api.commands import execute_rewind, RewindRequestV2

        mock_database.get_session.return_value = {
            "id": "session-1",
            "sdk_session_id": "sdk-session-1"
        }

        request = RewindRequestV2(target_uuid="db-123")

        response = await execute_rewind("session-1", request)

        assert response.success is False
        assert "JSONL file not found" in response.error

    @pytest.mark.asyncio
    async def test_rewind_success(self, mock_database, mock_jsonl_rewind_service, mock_sync_engine, mock_settings):
        """Should execute rewind successfully."""
        from app.api.commands import execute_rewind, RewindRequestV2

        mock_database.get_session.return_value = {
            "id": "session-1",
            "sdk_session_id": "sdk-session-1",
            "project_id": None
        }
        mock_database.get_project.return_value = None
        mock_jsonl_rewind_service.truncate_to_checkpoint.return_value = MockRewindResult(
            success=True,
            message="Rewind successful",
            chat_rewound=True,
            messages_removed=5
        )

        request = RewindRequestV2(
            target_uuid="uuid-1",
            restore_chat=True,
            include_response=True
        )

        response = await execute_rewind("session-1", request)

        assert response.success is True
        assert response.chat_rewound is True
        assert response.messages_removed == 5
        mock_jsonl_rewind_service.truncate_to_checkpoint.assert_called_once_with(
            sdk_session_id="sdk-session-1",
            target_uuid="uuid-1",
            working_dir=mock_settings.workspace_dir.__truediv__.return_value.__str__.return_value if mock_database.get_project.return_value else str(mock_settings.workspace_dir),
            include_response=True
        )

    @pytest.mark.asyncio
    async def test_rewind_broadcasts_event_on_success(self, mock_database, mock_jsonl_rewind_service, mock_sync_engine, mock_settings):
        """Should broadcast rewind event on success."""
        from app.api.commands import execute_rewind, RewindRequestV2
        import asyncio

        mock_database.get_session.return_value = {
            "id": "session-1",
            "sdk_session_id": "sdk-session-1",
            "project_id": None
        }
        mock_database.get_project.return_value = None
        mock_jsonl_rewind_service.truncate_to_checkpoint.return_value = MockRewindResult(
            success=True,
            message="Rewind successful",
            chat_rewound=True,
            messages_removed=3
        )

        request = RewindRequestV2(target_uuid="uuid-1")

        await execute_rewind("session-1", request)

        # Allow the asyncio.create_task to complete
        await asyncio.sleep(0.1)

        mock_sync_engine.broadcast_session_rewound.assert_called_once()


# =============================================================================
# Tests for legacy rewind endpoints
# =============================================================================

class TestLegacyRewindEndpoints:
    """Tests for legacy rewind endpoints."""

    @pytest.mark.asyncio
    async def test_legacy_checkpoints_redirects_to_v2(self, mock_database, mock_jsonl_rewind_service, mock_settings):
        """Legacy checkpoint endpoint should convert V2 response to legacy format."""
        from app.api.commands import get_rewind_checkpoints_legacy

        mock_database.get_session.return_value = {
            "id": "session-1",
            "sdk_session_id": "sdk-session-1",
            "project_id": None
        }
        mock_database.get_project.return_value = None
        mock_jsonl_rewind_service.get_checkpoints.return_value = [
            MockCheckpoint(
                uuid="uuid-1",
                index=0,
                message_preview="First message",
                full_message="First message content",
                timestamp="2024-01-01T00:00:00",
            )
        ]

        response = await get_rewind_checkpoints_legacy("session-1")

        assert response.success is True
        assert len(response.checkpoints) == 1
        assert response.checkpoints[0].index == 0
        assert response.checkpoints[0].message == "First message"

    @pytest.mark.asyncio
    async def test_legacy_execute_session_not_found(self, mock_database):
        """Should raise 404 when session not found."""
        from app.api.commands import execute_rewind_legacy
        from app.core.models import RewindRequest
        from fastapi import HTTPException

        mock_database.get_session.return_value = None

        request = RewindRequest(checkpoint_index=0, restore_option=1)

        with pytest.raises(HTTPException) as exc_info:
            await execute_rewind_legacy("nonexistent", request)

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_legacy_execute_option_4_cancels(self, mock_database, mock_jsonl_rewind_service, mock_settings):
        """Option 4 should cancel the rewind."""
        from app.api.commands import execute_rewind_legacy
        from app.core.models import RewindRequest

        mock_database.get_session.return_value = {
            "id": "session-1",
            "sdk_session_id": "sdk-session-1",
            "project_id": None
        }
        mock_database.get_project.return_value = None

        mock_checkpoint = MockCheckpoint(
            uuid="uuid-0",
            index=0,
            message_preview="First",
            full_message="First message"
        )
        mock_jsonl_rewind_service.get_checkpoints.return_value = [mock_checkpoint]

        request = RewindRequest(checkpoint_index=0, restore_option=4)

        response = await execute_rewind_legacy("session-1", request)

        assert response.success is True
        assert "cancelled" in response.message.lower()
        assert response.restore_option == 4

    @pytest.mark.asyncio
    async def test_legacy_execute_invalid_checkpoint_index(self, mock_database, mock_jsonl_rewind_service, mock_settings):
        """Should return error for invalid checkpoint index."""
        from app.api.commands import execute_rewind_legacy
        from app.core.models import RewindRequest

        mock_database.get_session.return_value = {
            "id": "session-1",
            "sdk_session_id": "sdk-session-1",
            "project_id": None
        }
        mock_database.get_project.return_value = None
        mock_jsonl_rewind_service.get_checkpoints.return_value = []

        request = RewindRequest(checkpoint_index=5, restore_option=1)

        response = await execute_rewind_legacy("session-1", request)

        assert response.success is False
        assert "Invalid checkpoint index" in response.message

    @pytest.mark.asyncio
    async def test_legacy_execute_option_1_chat_and_code(
        self, mock_database, mock_jsonl_rewind_service, mock_sync_engine, mock_settings
    ):
        """Option 1 should restore chat via V2 (code restore no longer supported)."""
        from app.api.commands import execute_rewind_legacy
        from app.core.models import RewindRequest

        mock_database.get_session.return_value = {
            "id": "session-1",
            "sdk_session_id": "sdk-session-1",
            "project_id": None
        }
        mock_database.get_project.return_value = None

        mock_checkpoint = MockCheckpoint(
            uuid="uuid-0",
            index=0,
            message_preview="First",
            full_message="First message"
        )
        mock_jsonl_rewind_service.get_checkpoints.return_value = [mock_checkpoint]

        mock_jsonl_rewind_service.truncate_to_checkpoint.return_value = MockRewindResult(
            success=True,
            message="Rewind successful",
            chat_rewound=True,
            messages_removed=5
        )

        request = RewindRequest(checkpoint_index=0, restore_option=1)

        response = await execute_rewind_legacy("session-1", request)

        assert response.success is True
        assert response.restore_option == 1
        mock_jsonl_rewind_service.truncate_to_checkpoint.assert_called_once()

    @pytest.mark.asyncio
    async def test_legacy_execute_option_2_chat_only(
        self, mock_database, mock_jsonl_rewind_service, mock_sync_engine, mock_settings
    ):
        """Option 2 should restore chat only via V2."""
        from app.api.commands import execute_rewind_legacy
        from app.core.models import RewindRequest

        mock_database.get_session.return_value = {
            "id": "session-1",
            "sdk_session_id": "sdk-session-1",
            "project_id": None
        }
        mock_database.get_project.return_value = None

        mock_checkpoint = MockCheckpoint(
            uuid="uuid-0",
            index=0,
            message_preview="First",
            full_message="First message"
        )
        mock_jsonl_rewind_service.get_checkpoints.return_value = [mock_checkpoint]

        mock_jsonl_rewind_service.truncate_to_checkpoint.return_value = MockRewindResult(
            success=True,
            message="Rewind successful",
            chat_rewound=True,
            messages_removed=3
        )

        request = RewindRequest(checkpoint_index=0, restore_option=2)

        response = await execute_rewind_legacy("session-1", request)

        assert response.success is True
        assert response.restore_option == 2

    @pytest.mark.asyncio
    async def test_legacy_execute_option_3_code_only(
        self, mock_database, mock_jsonl_rewind_service, mock_sync_engine, mock_settings
    ):
        """Option 3 should return success without action (code restore no longer supported)."""
        from app.api.commands import execute_rewind_legacy
        from app.core.models import RewindRequest

        mock_database.get_session.return_value = {
            "id": "session-1",
            "sdk_session_id": "sdk-session-1",
            "project_id": None
        }
        mock_database.get_project.return_value = None

        mock_checkpoint = MockCheckpoint(
            uuid="uuid-0",
            index=0,
            message_preview="First",
            full_message="First message"
        )
        mock_jsonl_rewind_service.get_checkpoints.return_value = [mock_checkpoint]

        # Option 3 is code-only, but code restore is no longer supported
        # So we expect the execute_rewind to return success without calling truncate_to_checkpoint

        request = RewindRequest(checkpoint_index=0, restore_option=3)

        response = await execute_rewind_legacy("session-1", request)

        # Option 3 still returns success (no action taken for code restore)
        assert response.success is True
        assert response.restore_option == 3


# =============================================================================
# Tests for rewind status and clear endpoints
# =============================================================================

class TestRewindStatusEndpoints:
    """Tests for rewind status and clear endpoints."""

    @pytest.mark.asyncio
    async def test_get_rewind_status(self):
        """Should return V2 rewind status."""
        from app.api.commands import get_rewind_status

        response = await get_rewind_status()

        assert response["version"] == "v2"
        assert response["method"] == "direct_jsonl"
        assert response["has_pending"] is False
        assert response["pending_rewind"] is None

    @pytest.mark.asyncio
    async def test_clear_pending_rewind(self):
        """Should return success (V2 doesn't use pending config)."""
        from app.api.commands import clear_pending_rewind

        response = await clear_pending_rewind()

        assert response["success"] is True
        assert "V2" in response["message"]


# =============================================================================
# Tests for Pydantic models
# =============================================================================

class TestCommandModels:
    """Test Pydantic models for commands API."""

    def test_command_info_model(self):
        """Test CommandInfo model."""
        from app.api.commands import CommandInfo

        cmd = CommandInfo(
            name="test",
            display="/test",
            description="Test command",
            type="custom"
        )

        assert cmd.name == "test"
        assert cmd.type == "custom"
        assert cmd.argument_hint is None

    def test_command_list_response_model(self):
        """Test CommandListResponse model."""
        from app.api.commands import CommandListResponse, CommandInfo

        response = CommandListResponse(
            commands=[
                CommandInfo(name="cmd1", display="/cmd1", description="Cmd 1", type="custom"),
                CommandInfo(name="cmd2", display="/cmd2", description="Cmd 2", type="sdk_builtin"),
            ],
            count=2
        )

        assert response.count == 2
        assert len(response.commands) == 2

    def test_execute_command_request_model(self):
        """Test ExecuteCommandRequest model."""
        from app.api.commands import ExecuteCommandRequest

        request = ExecuteCommandRequest(
            command="/test arg1 arg2",
            session_id="session-123"
        )

        assert request.command == "/test arg1 arg2"
        assert request.session_id == "session-123"

    def test_rewind_request_v2_model(self):
        """Test RewindRequestV2 model."""
        from app.api.commands import RewindRequestV2

        request = RewindRequestV2(
            target_uuid="uuid-123",
            restore_chat=True,
            include_response=True
        )

        assert request.target_uuid == "uuid-123"
        assert request.restore_chat is True
        assert request.include_response is True

    def test_rewind_request_v2_defaults(self):
        """Test RewindRequestV2 default values."""
        from app.api.commands import RewindRequestV2

        request = RewindRequestV2(target_uuid="uuid-123")

        assert request.restore_chat is True
        assert request.include_response is True

    def test_checkpoint_v2_model(self):
        """Test CheckpointV2 model."""
        from app.api.commands import CheckpointV2

        cp = CheckpointV2(
            uuid="uuid-1",
            index=0,
            message_preview="Preview...",
            full_message="Full message content",
            timestamp="2024-01-01T00:00:00"
        )

        assert cp.uuid == "uuid-1"
        assert cp.index == 0
        assert cp.message_preview == "Preview..."
        assert cp.full_message == "Full message content"


# =============================================================================
# Integration-style tests with full client
# =============================================================================

class TestCommandsAPIIntegration:
    """Integration-style tests using the test client."""

    def test_commands_router_exists(self):
        """Verify commands router can be imported."""
        from app.api.commands import router
        assert router is not None
        assert router.prefix == "/api/v1/commands"

    def test_commands_router_tags(self):
        """Verify commands router has correct tags."""
        from app.api.commands import router
        assert "Commands" in router.tags
