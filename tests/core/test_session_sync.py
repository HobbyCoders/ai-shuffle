"""
Unit tests for session sync service.

Tests cover:
- Discovering unimported sessions from Claude Code JSONL files
- Importing individual sessions
- Batch syncing sessions for a project
- Error handling and edge cases
- Integration with database operations
"""

import json
import pytest
import tempfile
import shutil
import uuid
from pathlib import Path
from unittest.mock import patch, MagicMock


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

    def test_discover_empty_directory(self, mock_settings, mock_database):
        """Should return empty list when no sessions exist."""
        with patch("app.core.session_sync.settings", mock_settings):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            # Create project directory
            working_dir = str(mock_settings.workspace_dir / "test-project")
            project_dir = mock_settings.get_claude_projects_dir / "-workspace-test-project"
            project_dir.mkdir(parents=True, exist_ok=True)

            result = service.discover_unimported_sessions("test-project", working_dir)

            assert result == []

    def test_discover_finds_unimported_sessions(self, mock_settings, mock_database, sample_jsonl_content):
        """Should find sessions that are not in the database."""
        with patch("app.core.session_sync.settings", mock_settings):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            # Create project directory with session files
            working_dir = str(mock_settings.workspace_dir / "test-project")
            project_dir_name = working_dir.replace("\\", "-").replace("/", "-").replace(":", "-")
            project_dir = mock_settings.get_claude_projects_dir / project_dir_name
            project_dir.mkdir(parents=True, exist_ok=True)

            # Create two session files
            session1_id = str(uuid.uuid4())
            session2_id = str(uuid.uuid4())
            create_jsonl_file(project_dir / f"{session1_id}.jsonl", sample_jsonl_content)
            create_jsonl_file(project_dir / f"{session2_id}.jsonl", sample_jsonl_content)

            result = service.discover_unimported_sessions("test-project", working_dir)

            assert len(result) == 2
            sdk_ids = [r["sdk_session_id"] for r in result]
            assert session1_id in sdk_ids
            assert session2_id in sdk_ids

    def test_discover_skips_already_imported(self, mock_settings, mock_database, sample_jsonl_content):
        """Should skip sessions that are already in the database."""
        with patch("app.core.session_sync.settings", mock_settings):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            # Create project directory with session files
            working_dir = str(mock_settings.workspace_dir / "test-project")
            project_dir_name = working_dir.replace("\\", "-").replace("/", "-").replace(":", "-")
            project_dir = mock_settings.get_claude_projects_dir / project_dir_name
            project_dir.mkdir(parents=True, exist_ok=True)

            session1_id = str(uuid.uuid4())
            session2_id = str(uuid.uuid4())
            create_jsonl_file(project_dir / f"{session1_id}.jsonl", sample_jsonl_content)
            create_jsonl_file(project_dir / f"{session2_id}.jsonl", sample_jsonl_content)

            # Mark one as already imported
            mock_database.get_session_by_sdk_id.side_effect = lambda sdk_id: (
                {"id": "ses-existing", "sdk_session_id": sdk_id} if sdk_id == session1_id else None
            )

            result = service.discover_unimported_sessions("test-project", working_dir)

            assert len(result) == 1
            assert result[0]["sdk_session_id"] == session2_id

    def test_discover_skips_agent_files(self, mock_settings, mock_database, sample_jsonl_content):
        """Should skip agent-*.jsonl files (subagent logs)."""
        with patch("app.core.session_sync.settings", mock_settings):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            working_dir = str(mock_settings.workspace_dir / "test-project")
            project_dir_name = working_dir.replace("\\", "-").replace("/", "-").replace(":", "-")
            project_dir = mock_settings.get_claude_projects_dir / project_dir_name
            project_dir.mkdir(parents=True, exist_ok=True)

            # Create regular session and agent file
            session_id = str(uuid.uuid4())
            create_jsonl_file(project_dir / f"{session_id}.jsonl", sample_jsonl_content)
            create_jsonl_file(project_dir / "agent-abc123.jsonl", sample_jsonl_content)

            result = service.discover_unimported_sessions("test-project", working_dir)

            assert len(result) == 1
            assert result[0]["sdk_session_id"] == session_id

    def test_discover_nonexistent_directory(self, mock_settings, mock_database):
        """Should return empty list when project directory doesn't exist."""
        with patch("app.core.session_sync.settings", mock_settings):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            working_dir = str(mock_settings.workspace_dir / "nonexistent-project")

            result = service.discover_unimported_sessions("test-project", working_dir)

            assert result == []

    def test_discover_sorts_by_modified_time(self, mock_settings, mock_database, sample_jsonl_content):
        """Should sort results by modified time, newest first."""
        import time
        with patch("app.core.session_sync.settings", mock_settings):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            working_dir = str(mock_settings.workspace_dir / "test-project")
            project_dir_name = working_dir.replace("\\", "-").replace("/", "-").replace(":", "-")
            project_dir = mock_settings.get_claude_projects_dir / project_dir_name
            project_dir.mkdir(parents=True, exist_ok=True)

            # Create sessions with different timestamps
            old_session = str(uuid.uuid4())
            new_session = str(uuid.uuid4())

            create_jsonl_file(project_dir / f"{old_session}.jsonl", sample_jsonl_content)
            time.sleep(0.1)  # Ensure different mtime
            create_jsonl_file(project_dir / f"{new_session}.jsonl", sample_jsonl_content)

            result = service.discover_unimported_sessions("test-project", working_dir)

            assert len(result) == 2
            # Newest should be first
            assert result[0]["sdk_session_id"] == new_session
            assert result[1]["sdk_session_id"] == old_session


# =============================================================================
# Test Import Session
# =============================================================================

class TestImportSession:
    """Test importing individual sessions."""

    def test_import_session_success(self, mock_settings, mock_database, sample_jsonl_content):
        """Should successfully import a session."""
        with patch("app.core.session_sync.settings", mock_settings):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            working_dir = str(mock_settings.workspace_dir / "test-project")
            project_dir_name = working_dir.replace("\\", "-").replace("/", "-").replace(":", "-")
            project_dir = mock_settings.get_claude_projects_dir / project_dir_name
            project_dir.mkdir(parents=True, exist_ok=True)

            sdk_session_id = str(uuid.uuid4())
            jsonl_path = project_dir / f"{sdk_session_id}.jsonl"
            create_jsonl_file(jsonl_path, sample_jsonl_content)

            result = service.import_session(
                sdk_session_id=sdk_session_id,
                jsonl_path=jsonl_path,
                project_id="test-project",
                working_dir=working_dir,
                profile_id="default"
            )

            assert result is not None
            # Session is created atomically with all stats in one call
            mock_database.create_session_with_stats.assert_called_once()

    def test_import_session_already_exists(self, mock_settings, mock_database, sample_jsonl_content):
        """Should return existing session if already imported."""
        with patch("app.core.session_sync.settings", mock_settings):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            sdk_session_id = str(uuid.uuid4())
            existing_session = {"id": "ses-existing", "sdk_session_id": sdk_session_id}
            mock_database.get_session_by_sdk_id.return_value = existing_session

            working_dir = str(mock_settings.workspace_dir / "test-project")
            project_dir_name = working_dir.replace("\\", "-").replace("/", "-").replace(":", "-")
            project_dir = mock_settings.get_claude_projects_dir / project_dir_name
            project_dir.mkdir(parents=True, exist_ok=True)

            jsonl_path = project_dir / f"{sdk_session_id}.jsonl"
            create_jsonl_file(jsonl_path, sample_jsonl_content)

            result = service.import_session(
                sdk_session_id=sdk_session_id,
                jsonl_path=jsonl_path,
                project_id="test-project",
                working_dir=working_dir
            )

            assert result == existing_session
            mock_database.create_session_with_stats.assert_not_called()

    def test_import_session_generates_title_from_content(self, mock_settings, mock_database, sample_jsonl_content):
        """Should extract title from first user message."""
        with patch("app.core.session_sync.settings", mock_settings):
            with patch("app.core.session_sync._get_session_title_and_preview") as mock_title:
                mock_title.return_value = ("Hello, can you help me?", "Of course!", 4)

                from app.core.session_sync import SessionSyncService
                service = SessionSyncService()

                working_dir = str(mock_settings.workspace_dir / "test-project")
                project_dir_name = working_dir.replace("\\", "-").replace("/", "-").replace(":", "-")
                project_dir = mock_settings.get_claude_projects_dir / project_dir_name
                project_dir.mkdir(parents=True, exist_ok=True)

                sdk_session_id = str(uuid.uuid4())
                jsonl_path = project_dir / f"{sdk_session_id}.jsonl"
                create_jsonl_file(jsonl_path, sample_jsonl_content)

                service.import_session(
                    sdk_session_id=sdk_session_id,
                    jsonl_path=jsonl_path,
                    project_id="test-project",
                    working_dir=working_dir
                )

                # Check title was passed to create_session_with_stats
                call_kwargs = mock_database.create_session_with_stats.call_args[1]
                assert call_kwargs["title"] == "Hello, can you help me?"

    def test_import_session_fallback_profile(self, mock_settings, mock_database, sample_jsonl_content):
        """Should fall back to default profile if specified profile doesn't exist."""
        with patch("app.core.session_sync.settings", mock_settings):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            # Profile doesn't exist for first call, then exists for default
            mock_database.get_profile.side_effect = [
                None,  # "nonexistent" doesn't exist
                {"id": "default", "name": "Default"}  # "default" exists
            ]

            working_dir = str(mock_settings.workspace_dir / "test-project")
            project_dir_name = working_dir.replace("\\", "-").replace("/", "-").replace(":", "-")
            project_dir = mock_settings.get_claude_projects_dir / project_dir_name
            project_dir.mkdir(parents=True, exist_ok=True)

            sdk_session_id = str(uuid.uuid4())
            jsonl_path = project_dir / f"{sdk_session_id}.jsonl"
            create_jsonl_file(jsonl_path, sample_jsonl_content)

            service.import_session(
                sdk_session_id=sdk_session_id,
                jsonl_path=jsonl_path,
                project_id="test-project",
                working_dir=working_dir,
                profile_id="nonexistent"
            )

            # Should use default profile
            call_kwargs = mock_database.create_session_with_stats.call_args[1]
            assert call_kwargs["profile_id"] == "default"

    def test_import_session_no_profiles_available(self, mock_settings, mock_database, sample_jsonl_content):
        """Should return None if no profiles are available."""
        with patch("app.core.session_sync.settings", mock_settings):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            mock_database.get_profile.return_value = None
            mock_database.get_all_profiles.return_value = []

            working_dir = str(mock_settings.workspace_dir / "test-project")
            project_dir_name = working_dir.replace("\\", "-").replace("/", "-").replace(":", "-")
            project_dir = mock_settings.get_claude_projects_dir / project_dir_name
            project_dir.mkdir(parents=True, exist_ok=True)

            sdk_session_id = str(uuid.uuid4())
            jsonl_path = project_dir / f"{sdk_session_id}.jsonl"
            create_jsonl_file(jsonl_path, sample_jsonl_content)

            result = service.import_session(
                sdk_session_id=sdk_session_id,
                jsonl_path=jsonl_path,
                project_id="test-project",
                working_dir=working_dir
            )

            assert result is None
            mock_database.create_session_with_stats.assert_not_called()


# =============================================================================
# Test Sync Project Sessions
# =============================================================================

class TestSyncProjectSessions:
    """Test batch syncing sessions for a project."""

    def test_sync_project_not_found(self, mock_settings, mock_database):
        """Should return error when project doesn't exist."""
        with patch("app.core.session_sync.settings", mock_settings):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            mock_database.get_project.return_value = None

            result = service.sync_project_sessions("nonexistent-project")

            assert result["imported_count"] == 0
            assert "not found" in result["errors"][0].lower()

    def test_sync_imports_all_unimported(self, mock_settings, mock_database, sample_jsonl_content):
        """Should import all unimported sessions for a project."""
        with patch("app.core.session_sync.settings", mock_settings):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            mock_database.get_project.return_value = {
                "id": "test-project",
                "path": "test-project"
            }

            # Create sessions
            working_dir = str(mock_settings.workspace_dir / "test-project")
            project_dir_name = working_dir.replace("\\", "-").replace("/", "-").replace(":", "-")
            project_dir = mock_settings.get_claude_projects_dir / project_dir_name
            project_dir.mkdir(parents=True, exist_ok=True)

            session1_id = str(uuid.uuid4())
            session2_id = str(uuid.uuid4())
            create_jsonl_file(project_dir / f"{session1_id}.jsonl", sample_jsonl_content)
            create_jsonl_file(project_dir / f"{session2_id}.jsonl", sample_jsonl_content)

            result = service.sync_project_sessions("test-project")

            assert result["imported_count"] == 2
            assert result["errors"] == []
            assert len(result["sessions"]) == 2

    def test_sync_returns_imported_sessions(self, mock_settings, mock_database, sample_jsonl_content):
        """Should return list of imported session objects."""
        with patch("app.core.session_sync.settings", mock_settings):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            mock_database.get_project.return_value = {
                "id": "test-project",
                "path": "test-project"
            }

            working_dir = str(mock_settings.workspace_dir / "test-project")
            project_dir_name = working_dir.replace("\\", "-").replace("/", "-").replace(":", "-")
            project_dir = mock_settings.get_claude_projects_dir / project_dir_name
            project_dir.mkdir(parents=True, exist_ok=True)

            session_id = str(uuid.uuid4())
            create_jsonl_file(project_dir / f"{session_id}.jsonl", sample_jsonl_content)

            result = service.sync_project_sessions("test-project")

            assert len(result["sessions"]) == 1
            # The returned session should be from get_session
            assert result["sessions"][0]["id"] == "ses-test123"

    def test_sync_continues_when_import_fails(self, mock_settings, mock_database, sample_jsonl_content):
        """Should continue importing other sessions when one fails."""
        with patch("app.core.session_sync.settings", mock_settings):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            mock_database.get_project.return_value = {
                "id": "test-project",
                "path": "test-project"
            }

            working_dir = str(mock_settings.workspace_dir / "test-project")
            project_dir_name = working_dir.replace("\\", "-").replace("/", "-").replace(":", "-")
            project_dir = mock_settings.get_claude_projects_dir / project_dir_name
            project_dir.mkdir(parents=True, exist_ok=True)

            session1_id = str(uuid.uuid4())
            session2_id = str(uuid.uuid4())
            create_jsonl_file(project_dir / f"{session1_id}.jsonl", sample_jsonl_content)
            create_jsonl_file(project_dir / f"{session2_id}.jsonl", sample_jsonl_content)

            # Make first session import fail by making create_session_with_stats throw
            call_count = [0]

            def create_session_with_stats_side_effect(**kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    raise Exception("Database error")
                return {"id": "ses-new"}

            mock_database.create_session_with_stats.side_effect = create_session_with_stats_side_effect

            result = service.sync_project_sessions("test-project")

            # One succeeded (the error is caught inside import_session which returns None)
            # The sync continues and the second import succeeds
            assert result["imported_count"] == 1
            # Skipped count should account for the failed one
            assert result["skipped_count"] == 1
            assert len(result["sessions"]) == 1

    def test_sync_no_sessions_to_import(self, mock_settings, mock_database):
        """Should handle case where all sessions are already imported."""
        with patch("app.core.session_sync.settings", mock_settings):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            mock_database.get_project.return_value = {
                "id": "test-project",
                "path": "test-project"
            }

            # Empty project directory
            working_dir = str(mock_settings.workspace_dir / "test-project")
            project_dir_name = working_dir.replace("\\", "-").replace("/", "-").replace(":", "-")
            project_dir = mock_settings.get_claude_projects_dir / project_dir_name
            project_dir.mkdir(parents=True, exist_ok=True)

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

    def test_sync_all_projects_empty(self, mock_settings, mock_database):
        """Should handle case where no projects exist."""
        with patch("app.core.session_sync.settings", mock_settings):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            mock_database.get_all_projects.return_value = []

            result = service.sync_all_projects()

            assert result["total_imported"] == 0
            assert result["projects_synced"] == 0
            assert result["errors"] == []

    def test_sync_all_projects_imports_from_multiple(self, mock_settings, mock_database, sample_jsonl_content):
        """Should sync sessions from multiple projects."""
        with patch("app.core.session_sync.settings", mock_settings):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            # Two projects
            mock_database.get_all_projects.return_value = [
                {"id": "project-1", "path": "project-1"},
                {"id": "project-2", "path": "project-2"}
            ]
            mock_database.get_project.side_effect = lambda pid: {
                "project-1": {"id": "project-1", "path": "project-1"},
                "project-2": {"id": "project-2", "path": "project-2"}
            }.get(pid)

            # Create session files for both projects
            for proj in ["project-1", "project-2"]:
                working_dir = str(mock_settings.workspace_dir / proj)
                project_dir_name = working_dir.replace("\\", "-").replace("/", "-").replace(":", "-")
                project_dir = mock_settings.get_claude_projects_dir / project_dir_name
                project_dir.mkdir(parents=True, exist_ok=True)

                session_id = str(uuid.uuid4())
                create_jsonl_file(project_dir / f"{session_id}.jsonl", sample_jsonl_content)

            result = service.sync_all_projects()

            assert result["total_imported"] == 2
            assert result["projects_synced"] == 2
            assert result["errors"] == []

    def test_sync_all_projects_with_limit(self, mock_settings, mock_database, sample_jsonl_content):
        """Should respect limit_projects parameter."""
        with patch("app.core.session_sync.settings", mock_settings):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            # Three projects
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

            # Create session files for all projects
            for proj in ["project-1", "project-2", "project-3"]:
                working_dir = str(mock_settings.workspace_dir / proj)
                project_dir_name = working_dir.replace("\\", "-").replace("/", "-").replace(":", "-")
                project_dir = mock_settings.get_claude_projects_dir / project_dir_name
                project_dir.mkdir(parents=True, exist_ok=True)

                session_id = str(uuid.uuid4())
                create_jsonl_file(project_dir / f"{session_id}.jsonl", sample_jsonl_content)

            # Only sync 2 projects
            result = service.sync_all_projects(limit_projects=2)

            assert result["projects_synced"] == 2
            # Should have imported from only 2 projects
            assert result["total_imported"] == 2

    def test_sync_all_projects_continues_on_error(self, mock_settings, mock_database, sample_jsonl_content):
        """Should continue syncing other projects when one fails."""
        with patch("app.core.session_sync.settings", mock_settings):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            # Two projects, but one will fail
            mock_database.get_all_projects.return_value = [
                {"id": "project-1", "path": "project-1"},
                {"id": "project-2", "path": "project-2"}
            ]

            # First project doesn't exist (returns None), second exists
            def get_project_side_effect(pid):
                if pid == "project-1":
                    return None  # Simulate missing project
                return {"id": "project-2", "path": "project-2"}

            mock_database.get_project.side_effect = get_project_side_effect

            # Create session for project-2 only
            working_dir = str(mock_settings.workspace_dir / "project-2")
            project_dir_name = working_dir.replace("\\", "-").replace("/", "-").replace(":", "-")
            project_dir = mock_settings.get_claude_projects_dir / project_dir_name
            project_dir.mkdir(parents=True, exist_ok=True)

            session_id = str(uuid.uuid4())
            create_jsonl_file(project_dir / f"{session_id}.jsonl", sample_jsonl_content)

            result = service.sync_all_projects()

            # Project-2 should have synced successfully
            assert result["projects_synced"] == 2  # Both were processed
            assert result["total_imported"] == 1  # Only project-2 had sessions to import
            # Project-1 should have generated an error
            assert any("project-1" in err.lower() or "not found" in err.lower() for err in result["errors"])

    def test_sync_all_projects_aggregates_counts(self, mock_settings, mock_database, sample_jsonl_content):
        """Should correctly aggregate import counts across projects."""
        with patch("app.core.session_sync.settings", mock_settings):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            mock_database.get_all_projects.return_value = [
                {"id": "project-1", "path": "project-1"},
                {"id": "project-2", "path": "project-2"}
            ]
            mock_database.get_project.side_effect = lambda pid: {
                "project-1": {"id": "project-1", "path": "project-1"},
                "project-2": {"id": "project-2", "path": "project-2"}
            }.get(pid)

            # Create 2 sessions for project-1, 3 for project-2
            for proj, count in [("project-1", 2), ("project-2", 3)]:
                working_dir = str(mock_settings.workspace_dir / proj)
                project_dir_name = working_dir.replace("\\", "-").replace("/", "-").replace(":", "-")
                project_dir = mock_settings.get_claude_projects_dir / project_dir_name
                project_dir.mkdir(parents=True, exist_ok=True)

                for _ in range(count):
                    session_id = str(uuid.uuid4())
                    create_jsonl_file(project_dir / f"{session_id}.jsonl", sample_jsonl_content)

            result = service.sync_all_projects()

            assert result["total_imported"] == 5
            assert result["projects_synced"] == 2
