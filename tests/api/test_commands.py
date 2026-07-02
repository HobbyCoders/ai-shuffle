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
    """Mock the database module for command tests.

    get_working_dir_for_project now delegates to app.api.common.resolve_project_path,
    which reads app.api.common.database — patch both to the same mock so the DB
    lookups the helper makes are observable and never hit a real pool.
    """
    with patch("app.api.commands.database") as mock_db, \
            patch("app.api.common.database", mock_db):
        yield mock_db


@pytest.fixture
def mock_settings():
    """Mock the settings module.

    Path resolution spans app.api.commands (workspace fallback), app.api.common
    (resolve_project_path fallback) and app.core.project_utils (working-dir
    composition). Patch settings in all three so one fixture governs it.
    """
    with patch("app.api.commands.settings") as mock_settings, \
            patch("app.api.common.settings", mock_settings), \
            patch("app.core.project_utils.settings", mock_settings):
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
    role: str = "user"  # "user" or "assistant"
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
    # Mirror the real RewindResult schema so tests exercising execute_rewind,
    # which reads this to update session turn_count, don't blow up with
    # AttributeError. See app/core/jsonl_rewind.py:RewindResult.
    remaining_user_turns: Optional[int] = None


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
            include_response=True,
            target_role="user"
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
# Tests for execute_rewind DB/JSONL sync (fix/rewind-db-history-sync)
# =============================================================================

@dataclass
class _RewindResultStub:
    """Full-shape stub for app.core.jsonl_rewind.RewindResult, including the
    ``remaining_user_turns`` field that ``execute_rewind`` reads."""
    success: bool
    message: str
    chat_rewound: bool = False
    messages_removed: int = 0
    remaining_user_turns: Optional[int] = None
    error: Optional[str] = None


class TestExecuteRewindDbSync:
    """
    After the JSONL file is truncated, ``execute_rewind`` must also truncate
    the ``session_messages`` rows in the database so the two stores stay in
    sync. The DB truncation is now a two-step helper pair on
    ``app.api.commands``: ``resolve_rewind_anchor`` maps the target uuid to a DB
    anchor row id, then ``apply_rewind_delete`` purges rows after that anchor.
    (Both replaced the old direct ``database.delete_session_messages_after_jsonl_uuid``.)
    These tests drive that plumbing in disabled/mirror mode — mock_settings is a
    MagicMock, so ``session_store_mode`` is not ``postgres_only`` and the JSONL +
    DB path (not the store cutover) runs.
    """

    @pytest.mark.asyncio
    async def test_rewind_truncates_db_messages_on_success(
        self, mock_database, mock_jsonl_rewind_service, mock_sync_engine, mock_settings
    ):
        """On successful JSONL truncation, execute_rewind must also purge DB rows."""
        from app.api.commands import execute_rewind, RewindRequestV2

        mock_database.get_session.return_value = {
            "id": "session-1",
            "sdk_session_id": "sdk-session-1",
            "project_id": None,
            "turn_count": 5,
        }
        mock_database.get_project.return_value = None
        mock_jsonl_rewind_service.truncate_to_checkpoint.return_value = _RewindResultStub(
            success=True,
            message="Rewind successful",
            chat_rewound=True,
            messages_removed=4,
            remaining_user_turns=1,
        )

        request = RewindRequestV2(
            target_uuid="uuid-target",
            restore_chat=True,
            include_response=True,
            target_role="user",
        )

        with patch("app.api.commands.resolve_rewind_anchor", return_value=42) as mock_anchor, \
                patch("app.api.commands.apply_rewind_delete", return_value=4) as mock_delete:
            response = await execute_rewind("session-1", request)

        assert response.success is True
        # target uuid is resolved to a DB anchor id first...
        mock_anchor.assert_called_once()
        assert mock_anchor.call_args.kwargs["session_id"] == "session-1"
        assert mock_anchor.call_args.kwargs["target_uuid"] == "uuid-target"
        # ...then rows after the anchor are deleted.
        mock_delete.assert_called_once()
        bound = mock_delete.call_args.kwargs
        assert bound["session_id"] == "session-1"
        assert bound["anchor_id"] == 42
        assert bound["include_response"] is True

    @pytest.mark.asyncio
    async def test_rewind_does_not_truncate_db_when_jsonl_truncation_fails(
        self, mock_database, mock_jsonl_rewind_service, mock_sync_engine, mock_settings
    ):
        """If JSONL truncation fails, DB must not be touched (no partial state)."""
        from app.api.commands import execute_rewind, RewindRequestV2

        mock_database.get_session.return_value = {
            "id": "session-1",
            "sdk_session_id": "sdk-session-1",
            "project_id": None,
            "turn_count": 5,
        }
        mock_database.get_project.return_value = None
        mock_jsonl_rewind_service.truncate_to_checkpoint.return_value = _RewindResultStub(
            success=False,
            message="Failed",
            chat_rewound=False,
            messages_removed=0,
            error="Target uuid not found in JSONL",
            remaining_user_turns=None,
        )

        request = RewindRequestV2(
            target_uuid="uuid-target",
            restore_chat=True,
            include_response=False,
        )

        with patch("app.api.commands.resolve_rewind_anchor", return_value=42), \
                patch("app.api.commands.apply_rewind_delete", return_value=0) as mock_delete:
            response = await execute_rewind("session-1", request)

        assert response.success is False
        mock_delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_rewind_passes_include_response_false_to_db_delete(
        self, mock_database, mock_jsonl_rewind_service, mock_sync_engine, mock_settings
    ):
        """include_response flag must propagate to the DB helper so it keeps/drops the trailing assistant row consistently with JSONL."""
        from app.api.commands import execute_rewind, RewindRequestV2

        mock_database.get_session.return_value = {
            "id": "session-1",
            "sdk_session_id": "sdk-session-1",
            "project_id": None,
            "turn_count": 3,
        }
        mock_database.get_project.return_value = None
        mock_jsonl_rewind_service.truncate_to_checkpoint.return_value = _RewindResultStub(
            success=True,
            message="ok",
            chat_rewound=True,
            messages_removed=2,
            remaining_user_turns=1,
        )

        request = RewindRequestV2(
            target_uuid="uuid-target",
            restore_chat=True,
            include_response=False,
            target_role="user",
        )

        with patch("app.api.commands.resolve_rewind_anchor", return_value=7), \
                patch("app.api.commands.apply_rewind_delete", return_value=2) as mock_delete:
            await execute_rewind("session-1", request)

        assert mock_delete.call_args.kwargs["include_response"] is False

    @pytest.mark.asyncio
    async def test_rewind_skipped_when_restore_chat_false(
        self, mock_database, mock_jsonl_rewind_service, mock_sync_engine, mock_settings
    ):
        """If the caller opted out of chat restore, DB truncation must also be skipped."""
        from app.api.commands import execute_rewind, RewindRequestV2

        mock_database.get_session.return_value = {
            "id": "session-1",
            "sdk_session_id": "sdk-session-1",
            "project_id": None,
            "turn_count": 3,
        }
        mock_database.get_project.return_value = None

        request = RewindRequestV2(
            target_uuid="uuid-target",
            restore_chat=False,
            restore_files=False,
        )

        with patch("app.api.commands.resolve_rewind_anchor", return_value=42), \
                patch("app.api.commands.apply_rewind_delete", return_value=0) as mock_delete:
            await execute_rewind("session-1", request)

        mock_jsonl_rewind_service.truncate_to_checkpoint.assert_not_called()
        mock_delete.assert_not_called()


class TestExecuteRewindPostgresOnly:
    """Bug #2 regression: in ``postgres_only`` mode the UI reads the Postgres
    session store (``session_store_entries``), so ``execute_rewind`` must
    truncate that store — and the rewind's success must hinge on the store, NOT
    on the JSONL transcript (which may be gone on a fresh server). Without this,
    rewind reports success while the message stays in the chat after reload (the
    exact reported symptom).
    """

    @pytest.mark.asyncio
    async def test_postgres_only_truncates_store_and_succeeds_without_jsonl(
        self, mock_database, mock_jsonl_rewind_service, mock_sync_engine, mock_settings
    ):
        from app.api.commands import execute_rewind, RewindRequestV2

        mock_settings.session_store_mode = "postgres_only"
        mock_database.get_session.return_value = {
            "id": "session-1",
            "sdk_session_id": "sdk-session-1",
            "project_id": None,
            "turn_count": 2,
        }
        mock_database.get_project.return_value = None
        # JSONL transcript gone (tmpfs on a fresh server) — must not matter.
        mock_jsonl_rewind_service.truncate_to_checkpoint.return_value = _RewindResultStub(
            success=False,
            message="JSONL not found",
            chat_rewound=False,
            messages_removed=0,
            error="JSONL file not found",
            remaining_user_turns=None,
        )

        with patch(
            "app.api.commands.truncate_session_store_to_checkpoint",
            new=AsyncMock(return_value=2),
        ) as mock_store_trunc, patch(
            "app.api.commands.resolve_rewind_anchor", return_value=None
        ), patch(
            "app.api.commands.live_session_store"
        ) as mock_live_store:
            mock_live_store.cleanup_ended_session = AsyncMock()

            request = RewindRequestV2(
                target_uuid="uuid-target",
                restore_chat=True,
                include_response=False,
                target_role="user",
            )
            response = await execute_rewind("session-1", request)

        # The store — authoritative chat in postgres_only — was truncated with
        # the rewind parameters forwarded verbatim.
        mock_store_trunc.assert_called_once()
        call = mock_store_trunc.call_args
        assert call.kwargs["sdk_session_id"] == "sdk-session-1"
        assert call.kwargs["target_uuid"] == "uuid-target"
        assert call.kwargs["include_response"] is False
        assert call.kwargs["target_role"] == "user"
        # ...and the rewind succeeds on the store alone, despite JSONL failure.
        assert response.success is True
        assert response.chat_rewound is True
        assert response.messages_removed == 2
        # Live store cleared so the reload reads truncated data, not stale memory.
        mock_live_store.cleanup_ended_session.assert_awaited_once_with("session-1")

    @pytest.mark.asyncio
    async def test_postgres_only_store_target_not_found_fails_rewind(
        self, mock_database, mock_jsonl_rewind_service, mock_sync_engine, mock_settings
    ):
        """When the store has no such target, the rewind must FAIL in
        postgres_only (the store is the chat) rather than falsely report success
        off a JSONL that is irrelevant in this mode."""
        from app.api.commands import execute_rewind, RewindRequestV2
        from app.core.pg_session_store import RewindTargetNotFound

        mock_settings.session_store_mode = "postgres_only"
        mock_database.get_session.return_value = {
            "id": "session-1",
            "sdk_session_id": "sdk-session-1",
            "project_id": None,
            "turn_count": 2,
        }
        mock_database.get_project.return_value = None
        mock_jsonl_rewind_service.truncate_to_checkpoint.return_value = _RewindResultStub(
            success=False,
            message="JSONL not found",
            chat_rewound=False,
            messages_removed=0,
            error="JSONL file not found",
            remaining_user_turns=None,
        )

        with patch(
            "app.api.commands.truncate_session_store_to_checkpoint",
            new=AsyncMock(side_effect=RewindTargetNotFound("nope")),
        ), patch(
            "app.api.commands.resolve_rewind_anchor", return_value=None
        ), patch(
            "app.api.commands.live_session_store"
        ) as mock_live_store:
            mock_live_store.cleanup_ended_session = AsyncMock()

            request = RewindRequestV2(
                target_uuid="missing",
                restore_chat=True,
                include_response=False,
                target_role="user",
            )
            response = await execute_rewind("session-1", request)

        assert response.success is False
        assert response.chat_rewound is False
        mock_live_store.cleanup_ended_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_postgres_only_invalidates_parse_cache_after_rewind(
        self, mock_database, mock_jsonl_rewind_service, mock_sync_engine, mock_settings
    ):
        """Regression: a successful rewind must invalidate the store-parse cache.

        ``_store_parse_cache`` (keyed by ``sdk_session_id`` in
        ``app.core.jsonl_parser``) has no on-disk mtime to self-invalidate, so a
        fresh ``/live`` cold-load re-serves the PRE-rewind transcript from cache
        even though the Postgres store is now truncated — the rewound messages
        reappear on reload. ``execute_rewind`` must call
        ``invalidate_parse_cache(sdk_session_id)`` after truncating the store so
        the next read re-parses the truncated transcript.
        """
        from app.api.commands import execute_rewind, RewindRequestV2

        mock_settings.session_store_mode = "postgres_only"
        mock_database.get_session.return_value = {
            "id": "session-1",
            "sdk_session_id": "sdk-session-1",
            "project_id": None,
            "turn_count": 2,
        }
        mock_database.get_project.return_value = None
        mock_jsonl_rewind_service.truncate_to_checkpoint.return_value = _RewindResultStub(
            success=False,
            message="JSONL not found",
            chat_rewound=False,
            messages_removed=0,
            error="JSONL file not found",
            remaining_user_turns=None,
        )

        with patch(
            "app.api.commands.truncate_session_store_to_checkpoint",
            new=AsyncMock(return_value=2),
        ), patch(
            "app.api.commands.resolve_rewind_anchor", return_value=None
        ), patch(
            "app.api.commands.live_session_store"
        ) as mock_live_store, patch(
            "app.api.commands.invalidate_parse_cache"
        ) as mock_invalidate:
            mock_live_store.cleanup_ended_session = AsyncMock()

            request = RewindRequestV2(
                target_uuid="uuid-target",
                restore_chat=True,
                include_response=False,
                target_role="user",
            )
            response = await execute_rewind("session-1", request)

        assert response.chat_rewound is True
        # The stale store-parse cache for this transcript must be dropped so the
        # next /live cold-load re-parses the truncated store instead of serving
        # the pre-rewind transcript verbatim.
        mock_invalidate.assert_called_once_with("sdk-session-1")

    @pytest.mark.asyncio
    async def test_postgres_only_no_cache_invalidation_when_rewind_fails(
        self, mock_database, mock_jsonl_rewind_service, mock_sync_engine, mock_settings
    ):
        """A failed rewind (target absent) must NOT invalidate the parse cache —
        there is nothing to refresh and a needless clear would force an expensive
        re-parse on the next read."""
        from app.api.commands import execute_rewind, RewindRequestV2
        from app.core.pg_session_store import RewindTargetNotFound

        mock_settings.session_store_mode = "postgres_only"
        mock_database.get_session.return_value = {
            "id": "session-1",
            "sdk_session_id": "sdk-session-1",
            "project_id": None,
            "turn_count": 2,
        }
        mock_database.get_project.return_value = None
        mock_jsonl_rewind_service.truncate_to_checkpoint.return_value = _RewindResultStub(
            success=False,
            message="JSONL not found",
            chat_rewound=False,
            messages_removed=0,
            error="JSONL file not found",
            remaining_user_turns=None,
        )

        with patch(
            "app.api.commands.truncate_session_store_to_checkpoint",
            new=AsyncMock(side_effect=RewindTargetNotFound("nope")),
        ), patch(
            "app.api.commands.resolve_rewind_anchor", return_value=None
        ), patch(
            "app.api.commands.live_session_store"
        ) as mock_live_store, patch(
            "app.api.commands.invalidate_parse_cache"
        ) as mock_invalidate:
            mock_live_store.cleanup_ended_session = AsyncMock()

            request = RewindRequestV2(
                target_uuid="missing",
                restore_chat=True,
                include_response=False,
                target_role="user",
            )
            response = await execute_rewind("session-1", request)

        assert response.chat_rewound is False
        mock_invalidate.assert_not_called()


class TestExecuteRewindDbSyncWithoutJsonl:
    """Bug D regression: in postgres_only with the (tmpfs, often absent) JSONL
    gone, the legacy ``session_messages`` sync was gated on ``jsonl_ok`` — so
    the store truncated fine but the legacy rows survived every rewind, and
    resurfaced through the empty-store legacy fallback after a full rewind.
    The DB sync must run whenever the store truncation succeeded."""

    def _session(self, mock_database):
        mock_database.get_session.return_value = {
            "id": "session-1",
            "sdk_session_id": "sdk-session-1",
            "project_id": None,
            "turn_count": 2,
        }
        mock_database.get_project.return_value = None

    def _jsonl_gone(self, mock_jsonl_rewind_service):
        mock_jsonl_rewind_service.truncate_to_checkpoint.return_value = _RewindResultStub(
            success=False,
            message="JSONL not found",
            chat_rewound=False,
            messages_removed=0,
            error="JSONL file not found",
            remaining_user_turns=None,
        )

    @pytest.mark.asyncio
    async def test_postgres_only_syncs_legacy_rows_when_jsonl_missing(
        self, mock_database, mock_jsonl_rewind_service, mock_sync_engine, mock_settings
    ):
        from app.api.commands import execute_rewind, RewindRequestV2

        mock_settings.session_store_mode = "postgres_only"
        self._session(mock_database)
        self._jsonl_gone(mock_jsonl_rewind_service)

        with patch(
            "app.api.commands.truncate_session_store_to_checkpoint",
            new=AsyncMock(return_value=2),
        ), patch(
            "app.api.commands.resolve_rewind_anchor", return_value=42
        ), patch(
            "app.api.commands.apply_rewind_delete", return_value=2
        ) as mock_delete, patch(
            "app.api.commands.live_session_store"
        ) as mock_live_store:
            mock_live_store.cleanup_ended_session = AsyncMock()

            request = RewindRequestV2(
                target_uuid="uuid-target",
                restore_chat=True,
                include_response=False,
                target_role="user",
            )
            response = await execute_rewind("session-1", request)

        assert response.success is True
        mock_delete.assert_called_once()
        bound = mock_delete.call_args.kwargs
        assert bound["session_id"] == "session-1"
        assert bound["anchor_id"] == 42
        assert bound["include_response"] is False

    @pytest.mark.asyncio
    async def test_anchor_resolution_runs_before_store_truncation(
        self, mock_database, mock_jsonl_rewind_service, mock_sync_engine, mock_settings
    ):
        """The content-fallback anchor matcher reads the store transcript when
        the JSONL is gone — so the anchor must be resolved while the target
        entry still EXISTS in the store, i.e. before the store truncation."""
        from app.api.commands import execute_rewind, RewindRequestV2

        mock_settings.session_store_mode = "postgres_only"
        self._session(mock_database)
        self._jsonl_gone(mock_jsonl_rewind_service)

        calls = []

        async def store_trunc(**kwargs):
            calls.append("store")
            return 1

        def anchor(**kwargs):
            calls.append("anchor")
            return 42

        with patch(
            "app.api.commands.truncate_session_store_to_checkpoint",
            new=store_trunc,
        ), patch(
            "app.api.commands.resolve_rewind_anchor", side_effect=anchor
        ), patch(
            "app.api.commands.apply_rewind_delete", return_value=1
        ), patch(
            "app.api.commands.live_session_store"
        ) as mock_live_store:
            mock_live_store.cleanup_ended_session = AsyncMock()

            request = RewindRequestV2(
                target_uuid="uuid-target",
                restore_chat=True,
                include_response=False,
                target_role="user",
            )
            await execute_rewind("session-1", request)

        assert calls == ["anchor", "store"]

    @pytest.mark.asyncio
    async def test_no_db_sync_when_store_truncation_also_failed(
        self, mock_database, mock_jsonl_rewind_service, mock_sync_engine, mock_settings
    ):
        """Neither transcript layer was truncated → deleting legacy rows would
        create the inverse inconsistency. Leave them alone."""
        from app.api.commands import execute_rewind, RewindRequestV2
        from app.core.pg_session_store import RewindTargetNotFound

        mock_settings.session_store_mode = "postgres_only"
        self._session(mock_database)
        self._jsonl_gone(mock_jsonl_rewind_service)

        with patch(
            "app.api.commands.truncate_session_store_to_checkpoint",
            new=AsyncMock(side_effect=RewindTargetNotFound("nope")),
        ), patch(
            "app.api.commands.resolve_rewind_anchor", return_value=42
        ), patch(
            "app.api.commands.apply_rewind_delete", return_value=0
        ) as mock_delete, patch(
            "app.api.commands.live_session_store"
        ) as mock_live_store:
            mock_live_store.cleanup_ended_session = AsyncMock()

            request = RewindRequestV2(
                target_uuid="uuid-target",
                restore_chat=True,
                include_response=False,
                target_role="user",
            )
            response = await execute_rewind("session-1", request)

        assert response.success is False
        mock_delete.assert_not_called()


class TestExecuteRewindInvalidatesParseCache:
    """Primary regression for "chat rewind still not working".

    In postgres_only the UI read path serves ``parse_session_history_from_store``
    verbatim from ``_store_parse_cache`` — an LRU cache with NO mtime/version
    check. The turn-end reseed warms it with the FULL pre-rewind transcript
    before every rewind. If ``execute_rewind`` truncates the rows but leaves the
    cache primed, the next reload serves the cached pre-rewind transcript and the
    rewound message reappears. ``execute_rewind`` must invalidate the cache.
    """

    @pytest.mark.asyncio
    async def test_rewind_invalidates_store_parse_cache(
        self, mock_database, mock_jsonl_rewind_service, mock_sync_engine, mock_settings
    ):
        from app.api.commands import execute_rewind, RewindRequestV2
        from app.core import jsonl_parser

        sdk_sid = "sdk-session-cache"
        # Warm the store-backed parse cache with a pre-rewind transcript, exactly
        # as the turn-end reseed (pre_seed_parse_cache) would leave it.
        pre_rewind = [
            {"id": "m1", "role": "user", "type": "text", "content": "first"},
            {"id": "m2", "role": "assistant", "type": "text", "content": "reply"},
            {"id": "m3", "role": "user", "type": "text", "content": "SECOND (to be rewound away)"},
        ]
        # pre_seed refuses to write an UNSTAMPED entry (it would be untrusted on
        # read), so supply the (count, max_id) store version the reseed would
        # resolve from the DB.
        jsonl_parser.pre_seed_parse_cache(
            sdk_sid, pre_rewind, {}, "/workspace",
            store_version_loader=lambda _sid: (3, 42),
        )

        # Sanity: the cache is primed and would serve the pre-rewind transcript.
        with jsonl_parser._store_parse_cache_lock:
            assert sdk_sid in jsonl_parser._store_parse_cache

        mock_settings.session_store_mode = "postgres_only"
        mock_database.get_session.return_value = {
            "id": "session-1",
            "sdk_session_id": sdk_sid,
            "project_id": None,
            "turn_count": 2,
        }
        mock_database.get_project.return_value = None
        mock_jsonl_rewind_service.truncate_to_checkpoint.return_value = _RewindResultStub(
            success=False,
            message="JSONL not found",
            chat_rewound=False,
            messages_removed=0,
            error="JSONL file not found",
            remaining_user_turns=None,
        )

        try:
            with patch(
                "app.api.commands.truncate_session_store_to_checkpoint",
                new=AsyncMock(return_value=1),
            ), patch(
                "app.api.commands.resolve_rewind_anchor", return_value=None
            ), patch(
                "app.api.commands.live_session_store"
            ) as mock_live_store:
                # Session fully removed → the RC3 in-place trim path is skipped.
                mock_live_store.cleanup_ended_session = AsyncMock(return_value=True)

                request = RewindRequestV2(
                    target_uuid="uuid-m3",
                    restore_chat=True,
                    include_response=False,
                    target_role="user",
                )
                response = await execute_rewind("session-1", request)

            assert response.success is True
            # The cache entry for this session must be gone, so the next reload
            # re-parses the truncated store instead of serving the stale list.
            with jsonl_parser._store_parse_cache_lock:
                assert sdk_sid not in jsonl_parser._store_parse_cache
        finally:
            jsonl_parser.invalidate_parse_cache(sdk_sid)


class TestExecuteRewindTrimsRefusedLiveSession:
    """RC3 regression: when ``cleanup_ended_session`` refuses to drop the live
    session (streaming / active team members / parked-on-background-work), the
    surviving in-memory snapshot still holds the post-checkpoint messages and
    wins the "longer wins" heal on reload — visually no-opping the rewind.
    ``execute_rewind`` must trim that snapshot in place at the checkpoint.
    """

    @pytest.mark.asyncio
    async def test_refused_cleanup_trims_live_snapshot_in_place(
        self, mock_database, mock_jsonl_rewind_service, mock_sync_engine, mock_settings
    ):
        from app.api.commands import execute_rewind, RewindRequestV2
        from app.core.live_session_store import live_session_store, LiveMessage, LiveSession

        session_id = "session-trim"
        sdk_sid = "sdk-session-trim"
        # A live session that cleanup will refuse to drop: not streaming, no team
        # members, but parked on active background work (patched below).
        live = LiveSession(session_id=session_id, is_streaming=False)
        live.messages = [
            LiveMessage(id="m1", role="user", type="text", content="first", uuid="uuid-1"),
            LiveMessage(id="m2", role="assistant", type="text", content="reply-1", uuid="uuid-a1"),
            LiveMessage(id="m3", role="user", type="text", content="SECOND", uuid="uuid-target"),
            LiveMessage(id="m4", role="assistant", type="text", content="reply-2", uuid="uuid-a2"),
        ]
        live_session_store._sessions[session_id] = live

        from app.core.task_registry import task_registry

        mock_settings.session_store_mode = "postgres_only"
        mock_database.get_session.return_value = {
            "id": session_id,
            "sdk_session_id": sdk_sid,
            "project_id": None,
            "turn_count": 2,
        }
        mock_database.get_project.return_value = None
        mock_jsonl_rewind_service.truncate_to_checkpoint.return_value = _RewindResultStub(
            success=False,
            message="JSONL not found",
            chat_rewound=False,
            messages_removed=0,
            error="JSONL file not found",
            remaining_user_turns=None,
        )

        try:
            with patch(
                "app.api.commands.truncate_session_store_to_checkpoint",
                new=AsyncMock(return_value=2),
            ), patch(
                "app.api.commands.resolve_rewind_anchor", return_value=None
            ), patch.object(
                task_registry, "has_active_background_work", return_value=True
            ):
                request = RewindRequestV2(
                    target_uuid="uuid-target",
                    restore_chat=True,
                    include_response=False,
                    target_role="user",
                )
                response = await execute_rewind(session_id, request)

            assert response.success is True
            # Cleanup was refused (parked on active background work), so the
            # session survives — but its snapshot must be trimmed at the target.
            surviving = live_session_store._sessions.get(session_id)
            assert surviving is not None, "cleanup should have been refused, keeping the session"
            remaining_uuids = [m.uuid for m in surviving.messages]
            assert "uuid-target" not in remaining_uuids
            assert "uuid-a2" not in remaining_uuids
            assert remaining_uuids == ["uuid-1", "uuid-a1"]
            assert surviving.needs_full_refresh is True
        finally:
            live_session_store._sessions.pop(session_id, None)
            live_session_store._session_locks.pop(session_id, None)

    @pytest.mark.asyncio
    async def test_refused_cleanup_trim_miss_surfaces_warning(
        self, mock_database, mock_jsonl_rewind_service, mock_sync_engine, mock_settings
    ):
        """BUG 3: when cleanup is refused AND the in-place trim MISSES (the target
        uuid isn't in the live snapshot — e.g. _backfill_message_uuids was skipped
        by its count-mismatch guard), the rewind must still SUCCEED (chat + store
        truncation worked) but surface a warning instead of silently reporting a
        clean rewind. The stale snapshot would otherwise win the "longer wins"
        heal on reload and visually no-op the rewind."""
        from app.api.commands import execute_rewind, RewindRequestV2
        from app.core.live_session_store import live_session_store, LiveMessage, LiveSession

        session_id = "session-trim-miss"
        sdk_sid = "sdk-session-trim-miss"
        # Live session whose messages carry NO transcript uuids — backfill never
        # ran (count-mismatch guard), so truncate_after_uuid cannot find the target.
        live = LiveSession(session_id=session_id, is_streaming=False)
        live.messages = [
            LiveMessage(id="m1", role="user", type="text", content="first"),
            LiveMessage(id="m2", role="assistant", type="text", content="reply-1"),
            LiveMessage(id="m3", role="user", type="text", content="SECOND"),
        ]
        live_session_store._sessions[session_id] = live

        from app.core.task_registry import task_registry

        mock_settings.session_store_mode = "postgres_only"
        mock_database.get_session.return_value = {
            "id": session_id,
            "sdk_session_id": sdk_sid,
            "project_id": None,
            "turn_count": 2,
        }
        mock_database.get_project.return_value = None
        mock_jsonl_rewind_service.truncate_to_checkpoint.return_value = _RewindResultStub(
            success=False,
            message="JSONL not found",
            chat_rewound=False,
            messages_removed=0,
            error="JSONL file not found",
            remaining_user_turns=None,
        )

        try:
            with patch(
                "app.api.commands.truncate_session_store_to_checkpoint",
                new=AsyncMock(return_value=2),
            ), patch(
                "app.api.commands.resolve_rewind_anchor", return_value=None
            ), patch.object(
                task_registry, "has_active_background_work", return_value=True
            ):
                request = RewindRequestV2(
                    target_uuid="uuid-not-in-buffer",
                    restore_chat=True,
                    include_response=False,
                    target_role="user",
                )
                response = await execute_rewind(session_id, request)

            # Rewind still succeeds — chat + store truncation worked.
            assert response.success is True
            assert response.chat_rewound is True
            # ...but the trim miss is surfaced (not silently reported clean).
            assert response.error is not None
            assert "Live buffer trim skipped" in response.error
            # The snapshot is UNCHANGED (the trim couldn't find the target).
            surviving = live_session_store._sessions.get(session_id)
            assert surviving is not None
            assert len(surviving.messages) == 3
        finally:
            live_session_store._sessions.pop(session_id, None)
            live_session_store._session_locks.pop(session_id, None)


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
