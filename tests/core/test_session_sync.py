"""
Unit tests for session sync service.

Tests cover:
- Discovering unimported sessions from Claude Code via SDK list_sessions()
- Importing individual sessions from SDKSessionInfo
- Batch syncing sessions for a project
- Error handling and edge cases
- Integration with database operations
"""

import json
import pytest
import tempfile
import shutil
import time
import uuid
from pathlib import Path
from unittest.mock import patch, MagicMock

from app.core.cli_sessions import SessionInfo as SDKSessionInfo


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def temp_dir():
    """Create a temporary directory for tests."""
    tmpdir = tempfile.mkdtemp()
    yield Path(tmpdir)
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def mock_settings(temp_dir):
    """Mock settings with temporary directories."""
    mock_settings_obj = MagicMock()
    mock_settings_obj.workspace_dir = temp_dir / "workspace"
    mock_settings_obj.get_claude_projects_dir = temp_dir / ".claude" / "projects"
    mock_settings_obj.workspace_dir.mkdir(parents=True, exist_ok=True)
    mock_settings_obj.get_claude_projects_dir.mkdir(parents=True, exist_ok=True)
    return mock_settings_obj


@pytest.fixture
def mock_database():
    """Mock database module."""
    with patch("app.core.session_sync.database") as mock_db:
        # Default behaviors
        mock_db.get_session_by_sdk_id.return_value = None
        mock_db.get_profile.return_value = {"id": "default", "name": "Default"}
        mock_db.get_all_profiles.return_value = [{"id": "default", "name": "Default"}]
        mock_db.get_session.return_value = {
            "id": "ses-test123",
            "title": "Test Session",
            "profile_id": "default",
            "sdk_session_id": "test-sdk-id"
        }
        yield mock_db


@pytest.fixture
def sample_project():
    """Sample project data."""
    return {
        "id": "test-project",
        "name": "Test Project",
        "path": "test-project",
        "settings": {}
    }


@pytest.fixture
def sample_jsonl_content():
    """Sample JSONL session content."""
    return [
        {
            "type": "user",
            "message": {"role": "user", "content": "Hello, can you help me?"},
            "uuid": str(uuid.uuid4()),
            "timestamp": "2024-01-15T10:00:00Z"
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Of course! How can I help you today?"}],
                "usage": {"input_tokens": 100, "output_tokens": 50}
            },
            "uuid": str(uuid.uuid4()),
            "timestamp": "2024-01-15T10:00:01Z"
        },
        {
            "type": "user",
            "message": {"role": "user", "content": "Write a hello world program"},
            "uuid": str(uuid.uuid4()),
            "timestamp": "2024-01-15T10:00:02Z"
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Here's a simple Python hello world:\n\n```python\nprint('Hello, World!')\n```"}],
                "usage": {"input_tokens": 150, "output_tokens": 80}
            },
            "uuid": str(uuid.uuid4()),
            "timestamp": "2024-01-15T10:00:03Z"
        }
    ]


def create_jsonl_file(path: Path, content: list) -> None:
    """Helper to create a JSONL file."""
    with open(path, 'w', encoding='utf-8') as f:
        for entry in content:
            f.write(json.dumps(entry) + '\n')


def make_sdk_session_info(
    session_id: str = None,
    summary: str = "Test session",
    last_modified: int = None,
    file_size: int = 1024,
    custom_title: str = None,
    first_prompt: str = None,
    git_branch: str = None,
    cwd: str = None,
) -> SDKSessionInfo:
    """Helper to create SDKSessionInfo objects for testing."""
    return SDKSessionInfo(
        session_id=session_id or str(uuid.uuid4()),
        summary=summary,
        last_modified=last_modified or int(time.time() * 1000),
        file_size=file_size,
        custom_title=custom_title,
        first_prompt=first_prompt,
        git_branch=git_branch,
        cwd=cwd,
    )


# =============================================================================
# Test Module Imports
# =============================================================================

class TestSessionSyncModuleImports:
    """Verify session sync module can be imported correctly."""

    def test_session_sync_module_imports(self):
        """Session sync module should import without errors."""
        from app.core import session_sync
        assert session_sync is not None

    def test_session_sync_service_exists(self):
        """SessionSyncService class should exist."""
        from app.core.session_sync import SessionSyncService
        assert SessionSyncService is not None

    def test_global_service_instance_exists(self):
        """Global session_sync_service instance should exist."""
        from app.core.session_sync import session_sync_service
        assert session_sync_service is not None


# =============================================================================
# Test Discover Unimported Sessions
# =============================================================================

class TestDiscoverUnimportedSessions:
    """Test discovering sessions that exist on disk but not in database."""

    def test_discover_empty_directory(self, mock_database):
        """Should return empty list when SDK returns no sessions."""
        with patch("app.core.session_sync.list_sessions", return_value=[]):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            result = service.discover_unimported_sessions("test-project", "/tmp/test")

            assert result == []

    def test_discover_finds_unimported_sessions(self, mock_database):
        """Should find sessions that are not in the database."""
        session1 = make_sdk_session_info()
        session2 = make_sdk_session_info()

        with patch("app.core.session_sync.list_sessions", return_value=[session1, session2]):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            result = service.discover_unimported_sessions("test-project", "/tmp/test")

            assert len(result) == 2
            session_ids = [s.session_id for s in result]
            assert session1.session_id in session_ids
            assert session2.session_id in session_ids

    def test_discover_skips_already_imported(self, mock_database):
        """Should skip sessions that are already in the database."""
        session1 = make_sdk_session_info()
        session2 = make_sdk_session_info()

        # Mark session1 as already imported
        mock_database.get_session_by_sdk_id.side_effect = lambda sdk_id: (
            {"id": "ses-existing", "sdk_session_id": sdk_id}
            if sdk_id == session1.session_id else None
        )

        with patch("app.core.session_sync.list_sessions", return_value=[session1, session2]):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            result = service.discover_unimported_sessions("test-project", "/tmp/test")

            assert len(result) == 1
            assert result[0].session_id == session2.session_id

    def test_discover_handles_sdk_error(self, mock_database):
        """Should return empty list when SDK list_sessions fails."""
        with patch("app.core.session_sync.list_sessions", side_effect=Exception("SDK error")):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            result = service.discover_unimported_sessions("test-project", "/tmp/test")

            assert result == []

    def test_discover_sorted_by_last_modified(self, mock_database):
        """SDK list_sessions returns sessions sorted by last_modified desc."""
        # SDK itself sorts by last_modified — verify we pass them through correctly
        newer = make_sdk_session_info(last_modified=2000)
        older = make_sdk_session_info(last_modified=1000)

        with patch("app.core.session_sync.list_sessions", return_value=[newer, older]):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            result = service.discover_unimported_sessions("test-project", "/tmp/test")

            assert len(result) == 2
            # Order from SDK should be preserved
            assert result[0].session_id == newer.session_id
            assert result[1].session_id == older.session_id


# =============================================================================
# Test Import Session
# =============================================================================

class TestImportSession:
    """Test importing individual sessions."""

    def test_import_session_success(self, mock_database):
        """Should successfully import a session."""
        session_info = make_sdk_session_info(summary="Hello world session")

        with patch("app.core.session_sync.get_session_cost_from_jsonl", return_value={}):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            result = service.import_session(
                session_info=session_info,
                project_id="test-project",
                working_dir="/tmp/test",
                profile_id="default"
            )

            assert result is not None
            mock_database.create_session_with_stats.assert_called_once()

    def test_import_session_already_exists(self, mock_database):
        """Should return existing session if already imported."""
        session_info = make_sdk_session_info()
        existing_session = {"id": "ses-existing", "sdk_session_id": session_info.session_id}
        mock_database.get_session_by_sdk_id.return_value = existing_session

        from app.core.session_sync import SessionSyncService
        service = SessionSyncService()

        result = service.import_session(
            session_info=session_info,
            project_id="test-project",
            working_dir="/tmp/test"
        )

        assert result == existing_session
        mock_database.create_session_with_stats.assert_not_called()

    def test_import_session_uses_custom_title(self, mock_database):
        """Should prefer custom_title over summary."""
        session_info = make_sdk_session_info(
            custom_title="My Custom Title",
            summary="Auto-generated summary",
        )

        with patch("app.core.session_sync.get_session_cost_from_jsonl", return_value={}):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            service.import_session(
                session_info=session_info,
                project_id="test-project",
                working_dir="/tmp/test"
            )

            call_kwargs = mock_database.create_session_with_stats.call_args[1]
            assert call_kwargs["title"] == "My Custom Title"

    def test_import_session_uses_summary_without_custom_title(self, mock_database):
        """Should use summary when no custom_title is set."""
        session_info = make_sdk_session_info(
            custom_title=None,
            summary="Hello, can you help me?",
        )

        with patch("app.core.session_sync.get_session_cost_from_jsonl", return_value={}):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            service.import_session(
                session_info=session_info,
                project_id="test-project",
                working_dir="/tmp/test"
            )

            call_kwargs = mock_database.create_session_with_stats.call_args[1]
            assert call_kwargs["title"] == "Hello, can you help me?"

    def test_import_session_fallback_profile(self, mock_database):
        """Should fall back to default profile if specified profile doesn't exist."""
        session_info = make_sdk_session_info()

        # Profile doesn't exist for first call, then exists for default
        mock_database.get_profile.side_effect = [
            None,  # "nonexistent" doesn't exist
            {"id": "default", "name": "Default"}  # "default" exists
        ]

        with patch("app.core.session_sync.get_session_cost_from_jsonl", return_value={}):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            service.import_session(
                session_info=session_info,
                project_id="test-project",
                working_dir="/tmp/test",
                profile_id="nonexistent"
            )

            # Should use default profile
            call_kwargs = mock_database.create_session_with_stats.call_args[1]
            assert call_kwargs["profile_id"] == "default"

    def test_import_session_no_profiles_available(self, mock_database):
        """Should return None if no profiles are available."""
        session_info = make_sdk_session_info()

        mock_database.get_profile.return_value = None
        mock_database.get_all_profiles.return_value = []

        from app.core.session_sync import SessionSyncService
        service = SessionSyncService()

        result = service.import_session(
            session_info=session_info,
            project_id="test-project",
            working_dir="/tmp/test"
        )

        assert result is None
        mock_database.create_session_with_stats.assert_not_called()

    def test_import_session_includes_token_usage(self, mock_database):
        """Should include token usage from JSONL when available."""
        session_info = make_sdk_session_info()

        usage_data = {"total_tokens_in": 500, "total_tokens_out": 200}
        with patch("app.core.session_sync.get_session_cost_from_jsonl", return_value=usage_data):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            service.import_session(
                session_info=session_info,
                project_id="test-project",
                working_dir="/tmp/test"
            )

            call_kwargs = mock_database.create_session_with_stats.call_args[1]
            assert call_kwargs["total_tokens_in"] == 500
            assert call_kwargs["total_tokens_out"] == 200


# =============================================================================
# Test Sync Project Sessions
# =============================================================================

class TestSyncProjectSessions:
    """Test batch syncing sessions for a project."""

    def test_sync_project_not_found(self, mock_database):
        """Should return error when project doesn't exist."""
        from app.core.session_sync import SessionSyncService
        service = SessionSyncService()

        mock_database.get_project.return_value = None

        result = service.sync_project_sessions("nonexistent-project")

        assert result["imported_count"] == 0
        assert "not found" in result["errors"][0].lower()

    def test_sync_imports_all_unimported(self, mock_database):
        """Should import all unimported sessions for a project."""
        session1 = make_sdk_session_info()
        session2 = make_sdk_session_info()

        mock_database.get_project.return_value = {
            "id": "test-project",
            "path": "test-project"
        }

        with patch("app.core.session_sync.list_sessions", return_value=[session1, session2]), \
             patch("app.core.session_sync.get_session_cost_from_jsonl", return_value={}):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            result = service.sync_project_sessions("test-project")

            assert result["imported_count"] == 2
            assert result["errors"] == []
            assert len(result["sessions"]) == 2

    def test_sync_returns_imported_sessions(self, mock_database):
        """Should return list of imported session objects."""
        session = make_sdk_session_info()

        mock_database.get_project.return_value = {
            "id": "test-project",
            "path": "test-project"
        }

        with patch("app.core.session_sync.list_sessions", return_value=[session]), \
             patch("app.core.session_sync.get_session_cost_from_jsonl", return_value={}):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            result = service.sync_project_sessions("test-project")

            assert len(result["sessions"]) == 1
            # The returned session should be from get_session
            assert result["sessions"][0]["id"] == "ses-test123"

    def test_sync_continues_when_import_fails(self, mock_database):
        """Should continue importing other sessions when one fails."""
        session1 = make_sdk_session_info()
        session2 = make_sdk_session_info()

        mock_database.get_project.return_value = {
            "id": "test-project",
            "path": "test-project"
        }

        # Make first session import fail by making create_session_with_stats throw
        call_count = [0]

        def create_session_with_stats_side_effect(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("Database error")
            return {"id": "ses-new"}

        mock_database.create_session_with_stats.side_effect = create_session_with_stats_side_effect

        with patch("app.core.session_sync.list_sessions", return_value=[session1, session2]), \
             patch("app.core.session_sync.get_session_cost_from_jsonl", return_value={}):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            result = service.sync_project_sessions("test-project")

            # One succeeded (the error is caught inside import_session which returns None)
            assert result["imported_count"] == 1
            assert result["skipped_count"] == 1
            assert len(result["sessions"]) == 1

    def test_sync_no_sessions_to_import(self, mock_database):
        """Should handle case where all sessions are already imported."""
        mock_database.get_project.return_value = {
            "id": "test-project",
            "path": "test-project"
        }

        with patch("app.core.session_sync.list_sessions", return_value=[]):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            result = service.sync_project_sessions("test-project")

            assert result["imported_count"] == 0
            assert result["errors"] == []
            assert result["sessions"] == []


# =============================================================================
# Test API Endpoint Integration
# =============================================================================

class TestSessionSyncAPIIntegration:
    """Test session sync API endpoint."""

    def test_sync_sessions_endpoint_exists(self):
        """The sync-sessions endpoint should exist in projects router."""
        from app.api.projects import router
        # Routes include the router prefix, so we check for the full path
        routes = [r.path for r in router.routes]
        # Check that any route ends with sync-sessions
        sync_session_routes = [r for r in routes if r.endswith("/sync-sessions")]
        assert len(sync_session_routes) > 0, f"sync-sessions endpoint not found in routes: {routes}"

    def test_list_sessions_has_sync_parameter(self):
        """The list sessions endpoint should have sync_claude_sessions parameter."""
        from app.api.sessions import list_sessions
        import inspect
        sig = inspect.signature(list_sessions)
        assert "sync_claude_sessions" in sig.parameters


# =============================================================================
# Test Sync All Projects
# =============================================================================

class TestSyncAllProjects:
    """Test syncing sessions across all projects."""

    def test_sync_all_projects_empty(self, mock_database):
        """Should handle case where no projects exist."""
        from app.core.session_sync import SessionSyncService
        service = SessionSyncService()

        mock_database.get_all_projects.return_value = []

        result = service.sync_all_projects()

        assert result["total_imported"] == 0
        assert result["projects_synced"] == 0
        assert result["errors"] == []

    def test_sync_all_projects_imports_from_multiple(self, mock_database):
        """Should sync sessions from multiple projects."""
        mock_database.get_all_projects.return_value = [
            {"id": "project-1", "path": "project-1"},
            {"id": "project-2", "path": "project-2"}
        ]
        mock_database.get_project.side_effect = lambda pid: {
            "project-1": {"id": "project-1", "path": "project-1"},
            "project-2": {"id": "project-2", "path": "project-2"}
        }.get(pid)

        # Each project has one session
        sessions_by_dir = {}

        def mock_list_sessions(directory=None, **kwargs):
            if directory not in sessions_by_dir:
                sessions_by_dir[directory] = [make_sdk_session_info()]
            return sessions_by_dir[directory]

        with patch("app.core.session_sync.list_sessions", side_effect=mock_list_sessions), \
             patch("app.core.session_sync.get_session_cost_from_jsonl", return_value={}):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            result = service.sync_all_projects()

            assert result["total_imported"] == 2
            assert result["projects_synced"] == 2
            assert result["errors"] == []

    def test_sync_all_projects_with_limit(self, mock_database):
        """Should respect limit_projects parameter."""
        mock_database.get_all_projects.return_value = [
            {"id": "project-1", "path": "project-1"},
            {"id": "project-2", "path": "project-2"},
            {"id": "project-3", "path": "project-3"}
        ]
        mock_database.get_project.side_effect = lambda pid: {
            "project-1": {"id": "project-1", "path": "project-1"},
            "project-2": {"id": "project-2", "path": "project-2"},
            "project-3": {"id": "project-3", "path": "project-3"}
        }.get(pid)

        def mock_list_sessions(directory=None, **kwargs):
            return [make_sdk_session_info()]

        with patch("app.core.session_sync.list_sessions", side_effect=mock_list_sessions), \
             patch("app.core.session_sync.get_session_cost_from_jsonl", return_value={}):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            # Only sync 2 projects
            result = service.sync_all_projects(limit_projects=2)

            assert result["projects_synced"] == 2
            assert result["total_imported"] == 2

    def test_sync_all_projects_continues_on_error(self, mock_database):
        """Should continue syncing other projects when one fails."""
        mock_database.get_all_projects.return_value = [
            {"id": "project-1", "path": "project-1"},
            {"id": "project-2", "path": "project-2"}
        ]

        # First project doesn't exist (returns None), second exists
        def get_project_side_effect(pid):
            if pid == "project-1":
                return None
            return {"id": "project-2", "path": "project-2"}

        mock_database.get_project.side_effect = get_project_side_effect

        def mock_list_sessions(directory=None, **kwargs):
            return [make_sdk_session_info()]

        with patch("app.core.session_sync.list_sessions", side_effect=mock_list_sessions), \
             patch("app.core.session_sync.get_session_cost_from_jsonl", return_value={}):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            result = service.sync_all_projects()

            # Project-2 should have synced successfully
            assert result["projects_synced"] == 2
            assert result["total_imported"] == 1
            assert any("project-1" in err.lower() or "not found" in err.lower() for err in result["errors"])

    def test_sync_all_projects_aggregates_counts(self, mock_database):
        """Should correctly aggregate import counts across projects."""
        mock_database.get_all_projects.return_value = [
            {"id": "project-1", "path": "project-1"},
            {"id": "project-2", "path": "project-2"}
        ]
        mock_database.get_project.side_effect = lambda pid: {
            "project-1": {"id": "project-1", "path": "project-1"},
            "project-2": {"id": "project-2", "path": "project-2"}
        }.get(pid)

        # 2 sessions for project-1, 3 for project-2
        session_counts = {"project-1": 2, "project-2": 3}

        def mock_list_sessions(directory=None, **kwargs):
            for proj_name, count in session_counts.items():
                if proj_name in (directory or ""):
                    return [make_sdk_session_info() for _ in range(count)]
            return [make_sdk_session_info() for _ in range(2)]  # default

        with patch("app.core.session_sync.list_sessions", side_effect=mock_list_sessions), \
             patch("app.core.session_sync.get_session_cost_from_jsonl", return_value={}):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            result = service.sync_all_projects()

            assert result["total_imported"] == 5
            assert result["projects_synced"] == 2
