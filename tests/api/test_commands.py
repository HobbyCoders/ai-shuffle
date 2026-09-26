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
from pathlib import Path
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
            patch("app.api.common.database", mock_db), \
            patch("app.api.command_context.database", mock_db), \
            patch("app.core.project_resolver.resolve_existing_cwd", return_value="/workspace/session-checkout"):
        yield mock_db


@pytest.fixture
def mock_settings():
    """Mock the settings module.

    Path resolution spans app.api.command_context (workspace fallback), app.api.common
    (resolve_project_path fallback) and app.core.project_utils (working-dir
    composition). Patch settings in all three so one fixture governs it.
    """
    with patch("app.api.command_context.settings") as mock_settings, \
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
         patch("app.api.commands.is_interactive_command") as mock_is_interactive:
        yield {
            "get_command_by_name": mock_get_cmd,
            "get_all_commands": mock_get_all,
            "is_interactive_command": mock_is_interactive,
        }


@pytest.fixture
def mock_jsonl_rewind_service():
    """Mock the JSONL rewind service."""
    with patch("app.api.commands.jsonl_rewind_service") as mock_jrs:
        yield mock_jrs


@pytest.fixture(autouse=True)
def mock_session_store_truncation():
    """Stub the always-on Postgres session-store truncation.

    execute_rewind truncates session_store_entries first on every chat rewind
    (the store is the single source of truth). Default the stub to success with
    a still-populated transcript; tests that pin store-specific behavior
    re-patch these names inside their own ``with patch(...)`` blocks, which
    nests over this fixture.
    """
    with patch(
        "app.api.commands.truncate_session_store_to_checkpoint",
        new=AsyncMock(return_value=1),
    ) as mock_truncate, patch(
        "app.api.commands.main_transcript_is_empty",
        new=AsyncMock(return_value=False),
    ):
        yield mock_truncate


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

        mock_settings.effective_workspace_dir = Path("/workspace")

        result = get_working_dir_for_project(None)

        assert result == "/workspace"
        mock_database.get_project.assert_not_called()

    def test_returns_workspace_dir_when_project_not_found(self, mock_database, mock_settings):
        """Should return workspace dir when project doesn't exist."""
        from app.api.commands import get_working_dir_for_project

        mock_database.get_project.return_value = None
        mock_settings.effective_workspace_dir = Path("/workspace")

        result = get_working_dir_for_project("nonexistent-project")

        assert result == "/workspace"
        mock_database.get_project.assert_called_once_with("nonexistent-project")

    def test_returns_project_path_when_project_exists(self, mock_database, mock_settings):
        """Should return project path when project exists."""
        from app.api.commands import get_working_dir_for_project

        mock_database.get_project.return_value = {"id": "test-project", "path": "my-project"}
        mock_settings.effective_workspace_dir = Path("/workspace")

        result = get_working_dir_for_project("test-project")

        assert result == "/workspace/my-project"
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
        mock_settings.effective_workspace_dir = Path("/workspace")
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
            working_dir="/workspace",  # no project: the effective workspace
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

    @pytest.mark.asyncio
    async def test_rewind_evicts_live_pooled_client_before_truncation(
        self, mock_database, mock_jsonl_rewind_service, mock_sync_engine, mock_settings
    ):
        """A rewind MUST evict any live pooled CLI subprocess for the session
        BEFORE mutating the transcript.

        A pooled ``ClaudeSDKClient`` holds the JSONL open and keeps appending.
        Truncating/rewriting the file underneath it makes its next write land
        past the new EOF (a gap of NUL bytes) or flush a stale buffer — the
        exact JSONL corruption that breaks resume and, via ``--session-mirror``,
        pollutes the Postgres session store the UI reads. The pool client must
        be removed first so the next turn resumes fresh from the truncated file.
        """
        from app.api.commands import execute_rewind, RewindRequestV2

        mock_database.get_session.return_value = {
            "id": "session-1",
            "sdk_session_id": "sdk-session-1",
            "project_id": None,
        }
        mock_database.get_project.return_value = None

        call_order: list[str] = []

        mock_pool = MagicMock()
        mock_pool.contains.return_value = True

        async def _remove(_sid):
            call_order.append("evict")
            return MagicMock()

        mock_pool.remove = AsyncMock(side_effect=_remove)

        def _truncate(**_kwargs):
            call_order.append("truncate")
            return MockRewindResult(
                success=True,
                message="Rewind successful",
                chat_rewound=True,
                messages_removed=5,
            )

        mock_jsonl_rewind_service.truncate_to_checkpoint.side_effect = _truncate

        request = RewindRequestV2(
            target_uuid="uuid-1", restore_chat=True, include_response=True
        )

        with patch("app.api.commands.get_session_pool", return_value=mock_pool):
            response = await execute_rewind("session-1", request)

        assert response.success is True
        # The live client was evicted...
        mock_pool.remove.assert_awaited_once_with("session-1")
        # ...and that eviction happened BEFORE the transcript was truncated.
        assert call_order == ["evict", "truncate"], (
            f"expected eviction before truncation, got {call_order}"
        )

    @pytest.mark.asyncio
    async def test_rewind_survives_pool_eviction_error(
        self, mock_database, mock_jsonl_rewind_service, mock_sync_engine, mock_settings
    ):
        """A failure while evicting the pooled client must not abort the rewind."""
        from app.api.commands import execute_rewind, RewindRequestV2

        mock_database.get_session.return_value = {
            "id": "session-1",
            "sdk_session_id": "sdk-session-1",
            "project_id": None,
        }
        mock_database.get_project.return_value = None
        mock_jsonl_rewind_service.truncate_to_checkpoint.return_value = MockRewindResult(
            success=True,
            message="Rewind successful",
            chat_rewound=True,
            messages_removed=2,
        )

        mock_pool = MagicMock()
        mock_pool.contains.return_value = True
        mock_pool.remove = AsyncMock(side_effect=RuntimeError("boom"))

        request = RewindRequestV2(target_uuid="uuid-1", restore_chat=True)

        with patch("app.api.commands.get_session_pool", return_value=mock_pool):
            response = await execute_rewind("session-1", request)

        assert response.success is True
        assert response.chat_rewound is True


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
    async def test_rewind_does_not_truncate_db_when_store_truncation_fails(
        self, mock_database, mock_jsonl_rewind_service, mock_sync_engine, mock_settings
    ):
        """If the session-store truncation fails (and the JSONL layer also
        yields nothing), the DB must not be touched (no partial state)."""
        from app.api.commands import execute_rewind, RewindRequestV2
        from app.core.pg_session_store import RewindTargetNotFound

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

        with patch(
            "app.api.commands.truncate_session_store_to_checkpoint",
            new=AsyncMock(side_effect=RewindTargetNotFound("no store entries")),
        ), patch("app.api.commands.resolve_rewind_anchor", return_value=42), \
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
        ):

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
        ):

            request = RewindRequestV2(
                target_uuid="missing",
                restore_chat=True,
                include_response=False,
                target_role="user",
            )
            response = await execute_rewind("session-1", request)

        assert response.success is False
        assert response.chat_rewound is False

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
            "app.api.commands.invalidate_parse_cache"
        ) as mock_invalidate:

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
            "app.api.commands.invalidate_parse_cache"
        ) as mock_invalidate:

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
        ) as mock_delete:

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
        ):

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
        ) as mock_delete:

            request = RewindRequestV2(
                target_uuid="uuid-target",
                restore_chat=True,
                include_response=False,
                target_role="user",
            )
            response = await execute_rewind("session-1", request)

        assert response.success is False
        mock_delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_mirror_mode_syncs_legacy_rows_when_jsonl_missing(
        self, mock_database, mock_jsonl_rewind_service, mock_sync_engine, mock_settings
    ):
        """Same gap as Bug D but in ``mirror`` mode: the store truncated fine,
        the JSONL was gone, and the ``postgres_only``-scoped gate skipped the
        legacy sync — leaving orphan session_messages rows to resurface through
        the empty-store fallback. Any truncated transcript layer must trigger
        the sync, regardless of mode."""
        from app.api.commands import execute_rewind, RewindRequestV2

        mock_settings.session_store_mode = "mirror"
        self._session(mock_database)
        self._jsonl_gone(mock_jsonl_rewind_service)

        with patch(
            "app.api.commands.truncate_session_store_to_checkpoint",
            new=AsyncMock(return_value=2),
        ), patch(
            "app.api.commands.resolve_rewind_anchor", return_value=42
        ), patch(
            "app.api.commands.apply_rewind_delete", return_value=2
        ) as mock_delete:

            request = RewindRequestV2(
                target_uuid="uuid-target",
                restore_chat=True,
                include_response=False,
                target_role="user",
            )
            response = await execute_rewind("session-1", request)

        assert response.success is True
        mock_delete.assert_called_once()
        assert mock_delete.call_args.kwargs["anchor_id"] == 42

    @pytest.mark.asyncio
    async def test_full_rewind_without_anchor_purges_all_legacy_rows(
        self, mock_database, mock_jsonl_rewind_service, mock_sync_engine, mock_settings
    ):
        """Full-rewind edge: the store was truncated to EMPTY and no legacy
        anchor resolved (nothing left to content-match). The empty-store read
        fallback would resurrect every leftover session_messages row on reload,
        so they must all be purged."""
        from app.api.commands import execute_rewind, RewindRequestV2

        mock_settings.session_store_mode = "postgres_only"
        self._session(mock_database)
        self._jsonl_gone(mock_jsonl_rewind_service)

        with patch(
            "app.api.commands.truncate_session_store_to_checkpoint",
            new=AsyncMock(return_value=3),
        ), patch(
            "app.api.commands.resolve_rewind_anchor", return_value=None
        ), patch(
            "app.api.commands.main_transcript_is_empty",
            new=AsyncMock(return_value=True),
        ) as mock_empty, patch(
            "app.api.commands.delete_all_session_rows", return_value=5
        ) as mock_purge, patch(
            "app.api.commands.apply_rewind_delete", return_value=0
        ) as mock_delete:

            request = RewindRequestV2(
                target_uuid="uuid-first-message",
                restore_chat=True,
                include_response=False,
                target_role="user",
            )
            response = await execute_rewind("session-1", request)

        assert response.success is True
        mock_empty.assert_awaited_once_with("sdk-session-1")
        mock_purge.assert_called_once_with("session-1")
        mock_delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_partial_rewind_without_anchor_keeps_legacy_rows(
        self, mock_database, mock_jsonl_rewind_service, mock_sync_engine, mock_settings
    ):
        """No anchor but the store still has content: the legacy rows can't be
        cut precisely, and the store (non-empty) shields them from the read
        fallback — deleting them all would be data loss. Leave them alone."""
        from app.api.commands import execute_rewind, RewindRequestV2

        mock_settings.session_store_mode = "postgres_only"
        self._session(mock_database)
        self._jsonl_gone(mock_jsonl_rewind_service)

        with patch(
            "app.api.commands.truncate_session_store_to_checkpoint",
            new=AsyncMock(return_value=1),
        ), patch(
            "app.api.commands.resolve_rewind_anchor", return_value=None
        ), patch(
            "app.api.commands.main_transcript_is_empty",
            new=AsyncMock(return_value=False),
        ), patch(
            "app.api.commands.delete_all_session_rows", return_value=0
        ) as mock_purge:
            request = RewindRequestV2(
                target_uuid="uuid-target",
                restore_chat=True,
                include_response=False,
                target_role="user",
            )
            response = await execute_rewind("session-1", request)

        assert response.success is True
        mock_purge.assert_not_called()


class TestExecuteRewindInvalidatesParseCache:
    """Primary regression for "chat rewind still not working".

    In postgres_only the UI read path serves ``parse_session_history_from_store``
    from ``_store_parse_cache`` — an LRU cache that (at the time of the bug) had
    no version check; it is now (count, max_id)-stamped, but the turn-end reseed
    still warms it with the FULL pre-rewind transcript before every rewind. If
    ``execute_rewind`` truncates the rows but leaves the cache primed, a reload
    can serve the cached pre-rewind transcript and the rewound message
    reappears. ``execute_rewind`` must invalidate the cache directly.
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
            ):
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


class TestExecuteRewindLiveOverlayDeleted:
    """RC3 hazard deleted at root (V2 Phase-1 deletion).

    The old bug: ``cleanup_ended_session`` could REFUSE to drop the live
    session (streaming / active team members / parked-on-background-work), and
    the surviving in-memory snapshot — still holding the post-checkpoint
    messages — won the "longer wins" heal on reload, visually no-opping the
    rewind. ``execute_rewind`` therefore had to trim that snapshot in place
    (and surface a warning when the trim missed).

    V2 deleted the in-memory live overlay entirely: reads serve the persisted
    transcript, so a rewind's truncation is visible on the very next load and
    there is NO snapshot to trim, refuse, or miss. This tripwire pins that the
    overlay (and with it the whole trim/refuse/miss surface) stays deleted from
    the rewind path — reintroducing a live-store reference here would resurrect
    the RC3 class of bugs without its compensating trim.
    """

    def test_rewind_path_has_no_live_overlay_to_trim(self):
        import inspect

        import app.api.commands as commands

        assert not hasattr(commands, "live_session_store")
        src = inspect.getsource(commands)
        assert "cleanup_ended_session" not in src
        assert "truncate_after_uuid" not in src


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


# =============================================================================
# Security: router-level authentication + per-session ownership (Phase 1.1)
# =============================================================================

class TestCommandsAuthentication:
    """The /api/v1/commands router must require authentication."""

    def test_router_has_auth_dependency(self):
        """Router should carry a router-level auth dependency."""
        from app.api.commands import router
        # There must be at least one router-level dependency (require_auth).
        assert len(router.dependencies) >= 1

    def _build_app_no_auth_override(self):
        """Build an app WITHOUT overriding require_auth so the real dependency runs."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.commands import router

        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def test_get_checkpoints_unauthenticated_returns_401(self):
        """Unauthenticated GET rewind checkpoints → 401."""
        client = self._build_app_no_auth_override()
        resp = client.get("/api/v1/commands/rewind/checkpoints/session-1")
        assert resp.status_code == 401

    def test_execute_rewind_unauthenticated_returns_401(self):
        """Unauthenticated POST rewind execute → 401."""
        client = self._build_app_no_auth_override()
        resp = client.post(
            "/api/v1/commands/rewind/execute/session-1",
            json={"target_uuid": "uuid-1", "restore_chat": True},
        )
        assert resp.status_code == 401

    def test_list_commands_unauthenticated_returns_401(self):
        """Even the read-only list endpoint requires auth (router-level)."""
        client = self._build_app_no_auth_override()
        resp = client.get("/api/v1/commands/")
        assert resp.status_code == 401


class TestCommandsSessionOwnership:
    """Session-scoped routes must enforce per-session access control."""

    @staticmethod
    def _request_for_api_user(project_id=None, profile_id=None):
        """Build a mock FastAPI Request carrying an API user (non-admin)."""
        req = MagicMock()
        req.state = MagicMock()
        req.state.api_user = {"project_id": project_id,
                              "profile_ids": [profile_id] if profile_id else None}
        return req

    @pytest.mark.asyncio
    async def test_execute_rewind_cross_project_denied(self, mock_database):
        """API user restricted to another project → 403 on rewind/execute."""
        from app.api.commands import execute_rewind, RewindRequestV2
        from fastapi import HTTPException

        mock_database.get_session.return_value = {"id": "session-1", "project_id": "proj-A"}

        http_request = self._request_for_api_user(project_id="proj-B")
        request = RewindRequestV2(target_uuid="uuid-1", restore_chat=True)

        with pytest.raises(HTTPException) as exc_info:
            await execute_rewind("session-1", request, http_request)

        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_get_checkpoints_cross_project_denied(self, mock_database, mock_settings):
        """API user restricted to another project → 403 on rewind checkpoints."""
        from app.api.commands import get_rewind_checkpoints
        from fastapi import HTTPException

        mock_database.get_session.return_value = {"id": "session-1", "project_id": "proj-A"}

        http_request = self._request_for_api_user(project_id="proj-B")

        with pytest.raises(HTTPException) as exc_info:
            await get_rewind_checkpoints("session-1", http_request)

        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_admin_request_bypasses_ownership(self, mock_database, mock_settings):
        """Admin (no api_user) is not blocked by check_session_access."""
        from app.api.commands import get_rewind_checkpoints

        mock_database.get_session.return_value = {
            "id": "session-1", "project_id": "proj-A", "sdk_session_id": None,
        }
        # Admin request: state.api_user is None → full access.
        req = MagicMock()
        req.state = MagicMock()
        req.state.api_user = None

        response = await get_rewind_checkpoints("session-1", req)
        # No 403 raised; handler proceeds (returns the no-sdk-session response).
        assert response.session_id == "session-1"


# =============================================================================
# Legacy rewind -> V2 restore_option plumbing (regression for issue #584)
# =============================================================================

class TestExecuteRewindLegacyRestoreCode:
    """execute_rewind_legacy must map restore_option onto RewindRequestV2.

    Regression for #584: the legacy path constructed RewindRequestV2 with a
    non-existent ``restore_code=`` kwarg (Pydantic silently dropped it), so
    ``restore_files`` stayed False and code-restore was never requested.
    """

    def _session(self):
        return {"id": "session-1", "project_id": "proj-A", "sdk_session_id": "sdk-1"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "restore_option, expect_chat, expect_files",
        [
            (1, True, True),    # code + conversation
            (2, True, False),   # conversation only
            (3, False, True),   # code only
        ],
    )
    async def test_restore_option_maps_to_v2_flags(
        self, restore_option, expect_chat, expect_files,
        mock_database, mock_jsonl_rewind_service, mock_settings,
    ):
        from app.api.commands import execute_rewind_legacy
        from app.core.models import RewindRequest

        mock_database.get_session.return_value = self._session()
        mock_jsonl_rewind_service.get_checkpoints.return_value = [
            MockCheckpoint(uuid="uuid-0", index=0, message_preview="hi", full_message="hi"),
        ]

        captured = {}

        async def _fake_execute_rewind(session_id, request, http_request=None):
            captured["request"] = request
            return MagicMock(success=True, message="ok", error=None)

        with patch("app.api.commands.execute_rewind", side_effect=_fake_execute_rewind):
            await execute_rewind_legacy(
                "session-1",
                RewindRequest(checkpoint_index=0, restore_option=restore_option),
                http_request=None,
            )

        v2_request = captured["request"]
        assert v2_request.target_uuid == "uuid-0"
        assert v2_request.restore_chat is expect_chat
        # The crux of #584: restore_files must reflect the code-restore intent.
        assert v2_request.restore_files is expect_files

    @pytest.mark.asyncio
    async def test_cancel_option_does_not_execute(
        self, mock_database, mock_jsonl_rewind_service, mock_settings,
    ):
        from app.api.commands import execute_rewind_legacy
        from app.core.models import RewindRequest

        mock_database.get_session.return_value = self._session()
        mock_jsonl_rewind_service.get_checkpoints.return_value = [
            MockCheckpoint(uuid="uuid-0", index=0, message_preview="hi", full_message="hi"),
        ]

        with patch("app.api.commands.execute_rewind") as mock_exec:
            result = await execute_rewind_legacy(
                "session-1",
                RewindRequest(checkpoint_index=0, restore_option=4),
                http_request=None,
            )

        mock_exec.assert_not_called()
        assert result.restore_option == 4
