"""
Unit tests for projects API endpoints.

Tests cover:
- Project CRUD operations (create, read, update, delete)
- File operations (list, upload, download, rename, delete)
- Folder operations (create, list)
- Worktree sync endpoint
- Authentication and authorization
- Access control for API users
- Path validation and security
- Error handling and edge cases
"""

import pytest
from io import BytesIO
from unittest.mock import patch, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import projects as projects_module
from app.api.projects import router, validate_project_path, normalize_docker_path, check_project_access
from app.api.auth import require_auth, require_admin


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def mock_settings(temp_dir):
    """Mock settings with temporary workspace directory."""
    mock_settings_obj = MagicMock()
    mock_settings_obj.workspace_dir = temp_dir
    mock_settings_obj.effective_workspace_dir = temp_dir
    return mock_settings_obj


@pytest.fixture
def mock_database():
    """Mock database module."""
    with patch("app.api.projects.database") as mock_db:
        yield mock_db


@pytest.fixture
def mock_api_user():
    """Mock for get_api_user_from_request.

    check_project_access lives in app.api.common and reads the name from that
    module's namespace, while list_projects calls the copy imported into
    app.api.projects — patch both so a single fixture governs access checks.
    """
    with patch("app.api.projects.get_api_user_from_request") as mock, \
         patch("app.api.common.get_api_user_from_request", new=mock):
        mock.return_value = None  # Default to admin (no api user)
        yield mock


@pytest.fixture
def app(mock_database, mock_settings, mock_api_user):
    """Create a FastAPI app with the projects router and mocked dependencies."""
    # Path composition happens in app.core.project_utils (get_project_working_dir*),
    # which reads project_utils.settings.workspace_dir; projects.py also references
    # settings.workspace_dir directly. Patch both so the temp workspace applies.
    with patch("app.api.projects.settings", mock_settings), \
         patch("app.core.project_utils.settings", mock_settings):
        app = FastAPI()
        app.include_router(router)

        # Override auth dependencies
        app.dependency_overrides[require_auth] = lambda: "test-session-token"
        app.dependency_overrides[require_admin] = lambda: "test-admin-token"

        yield app


@pytest.fixture
def client(app):
    """Create a test client with mocked dependencies."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def sample_project():
    """Sample project data."""
    return {
        "id": "test-project",
        "name": "Test Project",
        "description": "A test project",
        "path": "test-project",
        "settings": {},
        "created_at": "2024-01-15T10:30:00",
        "updated_at": "2024-01-15T10:30:00"
    }


# =============================================================================
# Test Module Imports
# =============================================================================

class TestProjectsModuleImports:
    """Verify projects module can be imported correctly."""

    def test_projects_module_imports(self):
        """Projects module should import without errors."""
        from app.api import projects
        assert projects is not None

    def test_projects_router_exists(self):
        """Projects router should exist."""
        from app.api.projects import router
        assert router is not None

    def test_router_has_correct_prefix(self):
        """Router should have /api/v1/projects prefix."""
        from app.api.projects import router
        assert router.prefix == "/api/v1/projects"


# =============================================================================
# Test Utility Functions
# =============================================================================

class TestValidateProjectPath:
    """Test path validation utility."""

    def test_valid_path_within_workspace(self, temp_dir):
        """Valid path within workspace should be accepted."""
        mock_settings = MagicMock()
        mock_settings.workspace_dir = temp_dir

        with patch("app.api.projects.settings", mock_settings):
            # Create the path first
            test_path = temp_dir / "valid-project"
            test_path.mkdir()
            result = validate_project_path("valid-project")
            assert result == test_path

    def test_path_escape_attempt_rejected(self, temp_dir):
        """Path traversal attempts should be rejected."""
        from fastapi import HTTPException
        mock_settings = MagicMock()
        mock_settings.workspace_dir = temp_dir

        with patch("app.api.projects.settings", mock_settings):
            with pytest.raises(HTTPException) as exc_info:
                validate_project_path("../../../etc/passwd")
            assert exc_info.value.status_code == 400
            assert "Path escapes workspace boundary" in exc_info.value.detail


class TestNormalizeDockerPath:
    """Test Docker mode path normalization."""

    def test_absolute_workspace_path_normalized_in_docker_mode(self, temp_dir):
        """Absolute workspace path should be converted to relative in Docker mode."""
        mock_settings_obj = MagicMock()
        mock_settings_obj.workspace_dir = temp_dir

        with patch("app.api.projects.settings", mock_settings_obj), \
             patch("app.api.projects.is_local_mode", return_value=False):
            result = normalize_docker_path(str(temp_dir / "my-project"))
            assert result == "my-project"

    def test_absolute_workspace_root_normalized_in_docker_mode(self, temp_dir):
        """Absolute workspace root path should be normalized to empty string in Docker mode."""
        mock_settings_obj = MagicMock()
        mock_settings_obj.workspace_dir = temp_dir

        with patch("app.api.projects.settings", mock_settings_obj), \
             patch("app.api.projects.is_local_mode", return_value=False):
            result = normalize_docker_path(str(temp_dir))
            assert result == ""

    def test_nested_workspace_path_normalized_in_docker_mode(self, temp_dir):
        """Nested absolute workspace path should be converted to relative in Docker mode."""
        mock_settings_obj = MagicMock()
        mock_settings_obj.workspace_dir = temp_dir

        with patch("app.api.projects.settings", mock_settings_obj), \
             patch("app.api.projects.is_local_mode", return_value=False):
            result = normalize_docker_path(str(temp_dir / "deep" / "nested" / "project"))
            assert result == "deep/nested/project"

    def test_absolute_path_outside_workspace_unchanged_in_docker_mode(self, temp_dir):
        """Absolute path outside workspace should remain unchanged in Docker mode."""
        mock_settings_obj = MagicMock()
        mock_settings_obj.workspace_dir = temp_dir

        with patch("app.api.projects.settings", mock_settings_obj), \
             patch("app.api.projects.is_local_mode", return_value=False):
            result = normalize_docker_path("/etc/something")
            assert result == "/etc/something"

    def test_absolute_path_unchanged_in_local_mode(self, temp_dir):
        """Absolute paths should be unchanged in local mode."""
        mock_settings_obj = MagicMock()
        mock_settings_obj.workspace_dir = temp_dir

        with patch("app.api.projects.settings", mock_settings_obj), \
             patch("app.api.projects.is_local_mode", return_value=True):
            abs_path = str(temp_dir / "my-project")
            result = normalize_docker_path(abs_path)
            assert result == abs_path

    def test_relative_path_unchanged(self, temp_dir):
        """Relative paths should be returned unchanged."""
        mock_settings_obj = MagicMock()
        mock_settings_obj.workspace_dir = temp_dir

        with patch("app.api.projects.settings", mock_settings_obj), \
             patch("app.api.projects.is_local_mode", return_value=False):
            result = normalize_docker_path("my-project")
            assert result == "my-project"

    def test_validate_accepts_absolute_workspace_path_in_docker_mode(self, temp_dir):
        """validate_project_path should accept absolute workspace paths in Docker mode."""
        mock_settings_obj = MagicMock()
        mock_settings_obj.workspace_dir = temp_dir

        # Create the target directory
        project_dir = temp_dir / "ai-dungeon"
        project_dir.mkdir()

        with patch("app.api.projects.settings", mock_settings_obj), \
             patch("app.api.projects.is_local_mode", return_value=False):
            result = validate_project_path(str(temp_dir / "ai-dungeon"))
            assert result == project_dir.resolve()

    def test_validate_rejects_outside_workspace_in_docker_mode(self, temp_dir):
        """validate_project_path should reject absolute paths outside workspace in Docker mode."""
        from fastapi import HTTPException
        mock_settings_obj = MagicMock()
        mock_settings_obj.workspace_dir = temp_dir

        with patch("app.api.projects.settings", mock_settings_obj), \
             patch("app.api.projects.is_local_mode", return_value=False):
            with pytest.raises(HTTPException) as exc_info:
                validate_project_path("/etc/passwd")
            assert exc_info.value.status_code == 400

    def test_create_project_with_absolute_workspace_path_in_docker_mode(
        self, client, mock_database, temp_dir, mock_settings
    ):
        """Creating a project with absolute workspace path should work in Docker mode."""
        mock_database.get_project.return_value = None
        mock_database.create_project.return_value = {
            "id": "ai-dungeon",
            "name": "AI Dungeon",
            "description": None,
            "path": "ai-dungeon",
            "settings": {},
            "created_at": "2024-01-15T10:30:00",
            "updated_at": "2024-01-15T10:30:00"
        }

        with patch("app.api.projects.is_local_mode", return_value=False):
            response = client.post(
                "/api/v1/projects",
                json={
                    "id": "ai-dungeon",
                    "name": "AI Dungeon",
                    "path": str(temp_dir / "ai-dungeon")
                }
            )

        assert response.status_code == 201
        data = response.json()
        assert data["id"] == "ai-dungeon"
        # Verify the directory was created
        assert (temp_dir / "ai-dungeon").exists()
        # Verify the db_path is stored as relative, not absolute
        call_kwargs = mock_database.create_project.call_args
        assert call_kwargs.kwargs.get("path") == "ai-dungeon" or call_kwargs[1].get("path") == "ai-dungeon"


class TestCheckProjectAccess:
    """Test project access control."""

    def test_admin_has_full_access(self):
        """Admin user should have access to any project."""
        request = MagicMock()
        with patch("app.api.common.get_api_user_from_request") as mock_api_user:
            mock_api_user.return_value = None  # Admin has no api_user
            # Should not raise
            check_project_access(request, "any-project")

    def test_api_user_with_matching_project(self):
        """API user should access their assigned project."""
        request = MagicMock()
        with patch("app.api.common.get_api_user_from_request") as mock_api_user:
            mock_api_user.return_value = {
                "id": "user-1",
                "project_id": "test-project"
            }
            # Should not raise
            check_project_access(request, "test-project")

    def test_api_user_denied_other_project(self):
        """API user should be denied access to other projects."""
        from fastapi import HTTPException
        request = MagicMock()
        with patch("app.api.common.get_api_user_from_request") as mock_api_user:
            mock_api_user.return_value = {
                "id": "user-1",
                "project_id": "project-a"
            }
            with pytest.raises(HTTPException) as exc_info:
                check_project_access(request, "project-b")
            assert exc_info.value.status_code == 403
            assert "Access denied" in exc_info.value.detail

    def test_api_user_without_project_restriction(self):
        """API user without project_id should have full access."""
        request = MagicMock()
        with patch("app.api.common.get_api_user_from_request") as mock_api_user:
            mock_api_user.return_value = {
                "id": "user-1",
                "project_id": None
            }
            # Should not raise
            check_project_access(request, "any-project")


class TestFileSizeFormatting:
    """Test file size formatting utility."""

    def test_format_bytes(self):
        """Should format bytes correctly."""
        from app.api.projects import format_file_size
        assert format_file_size(500) == "500 B"

    def test_format_kilobytes(self):
        """Should format KB correctly."""
        from app.api.projects import format_file_size
        assert format_file_size(2048) == "2.0 KB"

    def test_format_megabytes(self):
        """Should format MB correctly."""
        from app.api.projects import format_file_size
        result = format_file_size(2 * 1024 * 1024)
        assert result == "2.0 MB"

    def test_format_gigabytes(self):
        """Should format GB correctly."""
        from app.api.projects import format_file_size
        result = format_file_size(3 * 1024 * 1024 * 1024)
        assert result == "3.0 GB"


class TestFileExtension:
    """Test file extension extraction utility."""

    def test_get_extension_simple(self):
        """Should get simple extension."""
        from app.api.projects import get_file_extension
        assert get_file_extension("file.txt") == "txt"

    def test_get_extension_uppercase(self):
        """Should convert extension to lowercase."""
        from app.api.projects import get_file_extension
        assert get_file_extension("FILE.TXT") == "txt"

    def test_get_extension_no_extension(self):
        """Should return empty string for no extension."""
        from app.api.projects import get_file_extension
        assert get_file_extension("filename") == ""

    def test_get_extension_hidden_file(self):
        """Should handle hidden files (no extension, just a dot-prefix)."""
        from app.api.projects import get_file_extension
        # .gitignore returns empty string because pathlib treats it as the stem, not extension
        result = get_file_extension(".gitignore")
        assert result == ""

    def test_get_extension_multiple_dots(self):
        """Should get last extension only."""
        from app.api.projects import get_file_extension
        assert get_file_extension("file.tar.gz") == "gz"


# =============================================================================
# Test List Projects Endpoint
# =============================================================================

class TestListProjects:
    """Test GET /api/v1/projects endpoint."""

    def test_list_projects_success(self, client, mock_database, sample_project):
        """Should return list of all projects for admin."""
        mock_database.get_all_projects.return_value = [sample_project]

        response = client.get("/api/v1/projects")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["id"] == "test-project"
        assert data[0]["name"] == "Test Project"

    def test_list_projects_empty(self, client, mock_database):
        """Should return empty list when no projects."""
        mock_database.get_all_projects.return_value = []

        response = client.get("/api/v1/projects")

        assert response.status_code == 200
        assert response.json() == []

    def test_list_projects_api_user_sees_only_assigned(self, client, mock_database, mock_api_user, sample_project):
        """API user with project should only see their project."""
        mock_api_user.return_value = {
            "id": "user-1",
            "project_id": "test-project"
        }
        mock_database.get_project.return_value = sample_project

        response = client.get("/api/v1/projects")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["id"] == "test-project"
        mock_database.get_project.assert_called_with("test-project")

    def test_list_projects_api_user_no_project_returns_empty(self, client, mock_database, mock_api_user):
        """API user with non-existent project should see empty list."""
        mock_api_user.return_value = {
            "id": "user-1",
            "project_id": "nonexistent-project"
        }
        mock_database.get_project.return_value = None

        response = client.get("/api/v1/projects")

        assert response.status_code == 200
        assert response.json() == []


    def test_list_projects_backfills_missing_link_group(self, client, mock_database, sample_project):
        """A project with NULL link_group_id gets one derived from its git remote."""
        mock_database.get_all_projects.return_value = [dict(sample_project, link_group_id=None)]
        mock_database.get_remote_urls_for_projects.return_value = {
            "test-project": "https://github.com/Owner/Repo.git"
        }
        expected = "git:" + __import__("hashlib").sha256(
            b"https://github.com/owner/repo"
        ).hexdigest()[:16]

        response = client.get("/api/v1/projects")

        assert response.status_code == 200
        mock_database.set_project_link_group.assert_called_once_with("test-project", expected)
        assert response.json()[0]["link_group_id"] == expected

    def test_list_projects_keeps_locked_link_group(self, client, mock_database, sample_project):
        """A manually linked (locked) row keeps its manual group."""
        locked = dict(
            sample_project,
            link_group_id="manual:abc",
            settings={"link_group_locked": True},
        )
        mock_database.get_all_projects.return_value = [locked]

        response = client.get("/api/v1/projects")

        assert response.status_code == 200
        mock_database.get_remote_urls_for_projects.assert_not_called()
        mock_database.set_project_link_group.assert_not_called()
        assert response.json()[0]["link_group_id"] == "manual:abc"

    def test_list_projects_writes_nothing_in_the_steady_state(
        self, client, mock_database, sample_project
    ):
        """B2: a GET listing must not write — nor even read — for the backfill.

        Once every row carries a link group (the steady state, since a derived
        group is persisted the one time it is derived), the backfill is a pure
        in-memory no-op: no ``get_remote_urls_for_projects``, no
        ``set_project_link_group``.
        """
        mock_database.get_all_projects.return_value = [
            dict(sample_project, id="p1", link_group_id="git:abc"),
            dict(sample_project, id="p2", link_group_id="git:def"),
        ]

        for _ in range(3):  # repeated polls, as the picker does
            assert client.get("/api/v1/projects").status_code == 200

        mock_database.get_remote_urls_for_projects.assert_not_called()
        mock_database.set_project_link_group.assert_not_called()

    def test_link_group_write_happens_once_then_never_again(
        self, client, mock_database, sample_project
    ):
        """The derivation persists, so the next listing has nothing to do."""
        mock_database.get_all_projects.return_value = [
            dict(sample_project, link_group_id=None)
        ]
        mock_database.get_remote_urls_for_projects.return_value = {
            "test-project": "https://github.com/Owner/Repo.git"
        }

        assert client.get("/api/v1/projects").status_code == 200
        assert mock_database.set_project_link_group.call_count == 1

        # The row now carries the derived group (as the DB row would).
        mock_database.get_all_projects.return_value = [
            dict(sample_project, link_group_id="git:whatever")
        ]
        mock_database.set_project_link_group.reset_mock()
        mock_database.get_remote_urls_for_projects.reset_mock()

        assert client.get("/api/v1/projects").status_code == 200
        mock_database.set_project_link_group.assert_not_called()
        mock_database.get_remote_urls_for_projects.assert_not_called()

    def test_needs_link_backfill_is_a_pure_check(self, sample_project):
        """The gate that keeps the hot path query-free."""
        from app.api.projects import _needs_link_backfill

        assert _needs_link_backfill([dict(sample_project, link_group_id="git:x")]) == []
        assert _needs_link_backfill([
            dict(sample_project, link_group_id=None, settings={"link_group_locked": True})
        ]) == []
        pending = _needs_link_backfill([dict(sample_project, link_group_id=None)])
        assert [p["id"] for p in pending] == ["test-project"]

    def test_listing_reads_are_offloaded_off_the_event_loop(
        self, client, mock_database, sample_project
    ):
        """B2: the listing's synchronous psycopg2 work runs in a worker thread."""
        mock_database.get_all_projects.return_value = [sample_project]
        seen = []

        real_run_sync = projects_module.run_sync

        async def tracking_run_sync(fn, *args, **kwargs):
            seen.append(fn.__name__)
            return await real_run_sync(fn, *args, **kwargs)

        with patch.object(projects_module, "run_sync", tracking_run_sync):
            assert client.get("/api/v1/projects").status_code == 200

        assert seen == ["_load_projects_for_listing"], (
            "the projects listing should do all of its DB work in ONE offloaded call"
        )

    def test_list_projects_skips_remoteless_rows(self, client, mock_database, sample_project):
        """A project with no git remote stays NULL and is not written."""
        mock_database.get_all_projects.return_value = [dict(sample_project, link_group_id=None)]
        mock_database.get_remote_urls_for_projects.return_value = {"test-project": None}

        response = client.get("/api/v1/projects")

        assert response.status_code == 200
        mock_database.set_project_link_group.assert_not_called()
        assert response.json()[0]["link_group_id"] is None

    def test_list_projects_backfill_failure_does_not_break_listing(self, client, mock_database, sample_project):
        """A backfill error degrades to the plain project list."""
        mock_database.get_all_projects.return_value = [dict(sample_project, link_group_id=None)]
        mock_database.get_remote_urls_for_projects.side_effect = RuntimeError("db down")

        response = client.get("/api/v1/projects")

        assert response.status_code == 200
        assert len(response.json()) == 1


# =============================================================================
# Test Get Project Endpoint
# =============================================================================

class TestGetProject:
    """Test GET /api/v1/projects/{project_id} endpoint."""

    def test_get_project_success(self, client, mock_database, sample_project):
        """Should return project details."""
        mock_database.get_project.return_value = sample_project

        response = client.get("/api/v1/projects/test-project")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "test-project"
        assert data["name"] == "Test Project"
        assert data["description"] == "A test project"

    def test_get_project_not_found(self, client, mock_database):
        """Should return 404 for non-existent project."""
        mock_database.get_project.return_value = None

        response = client.get("/api/v1/projects/nonexistent")

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_get_project_access_denied(self, client, mock_database, mock_api_user, sample_project):
        """API user should be denied access to other projects."""
        mock_api_user.return_value = {
            "id": "user-1",
            "project_id": "other-project"
        }
        mock_database.get_project.return_value = sample_project

        response = client.get("/api/v1/projects/test-project")

        assert response.status_code == 403
        assert "Access denied" in response.json()["detail"]


# =============================================================================
# Test Create Project Endpoint
# =============================================================================

class TestCreateProject:
    """Test POST /api/v1/projects endpoint."""

    def test_create_project_success(self, client, mock_database, temp_dir, mock_settings):
        """Should create a new project."""
        mock_database.get_project.return_value = None  # No existing project
        mock_database.create_project.return_value = {
            "id": "new-project",
            "name": "New Project",
            "description": "Description",
            "path": "new-project",
            "settings": {},
            "created_at": "2024-01-15T10:30:00",
            "updated_at": "2024-01-15T10:30:00"
        }

        response = client.post(
            "/api/v1/projects",
            json={
                "id": "new-project",
                "name": "New Project",
                "description": "Description"
            }
        )

        assert response.status_code == 201
        data = response.json()
        assert data["id"] == "new-project"
        assert data["name"] == "New Project"
        mock_database.create_project.assert_called_once()

    def test_create_project_with_settings(self, client, mock_database, temp_dir, mock_settings):
        """Should create project with settings."""
        mock_database.get_project.return_value = None
        mock_database.create_project.return_value = {
            "id": "new-project",
            "name": "New Project",
            "description": None,
            "path": "new-project",
            "settings": {"default_profile_id": "custom"},
            "created_at": "2024-01-15T10:30:00",
            "updated_at": "2024-01-15T10:30:00"
        }

        response = client.post(
            "/api/v1/projects",
            json={
                "id": "new-project",
                "name": "New Project",
                "settings": {"default_profile_id": "custom"}
            }
        )

        assert response.status_code == 201
        data = response.json()
        assert data["settings"]["default_profile_id"] == "custom"

    def test_create_project_conflict(self, client, mock_database, sample_project):
        """Should return 409 if project already exists."""
        mock_database.get_project.return_value = sample_project

        response = client.post(
            "/api/v1/projects",
            json={
                "id": "test-project",
                "name": "Duplicate Project"
            }
        )

        assert response.status_code == 409
        assert "already exists" in response.json()["detail"].lower()

    def test_create_project_invalid_id(self, client, mock_database):
        """Should reject invalid project ID format."""
        response = client.post(
            "/api/v1/projects",
            json={
                "id": "Invalid ID!",  # Invalid - contains special chars
                "name": "Test Project"
            }
        )

        assert response.status_code == 422  # Validation error


# =============================================================================
# Test Update Project Endpoint
# =============================================================================

class TestUpdateProject:
    """Test PUT /api/v1/projects/{project_id} endpoint."""

    def test_update_project_success(self, client, mock_database, sample_project):
        """Should update project details."""
        mock_database.get_project.return_value = sample_project
        updated_project = {**sample_project, "name": "Updated Name"}
        mock_database.update_project.return_value = updated_project

        response = client.put(
            "/api/v1/projects/test-project",
            json={"name": "Updated Name"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Updated Name"

    def test_update_project_not_found(self, client, mock_database):
        """Should return 404 for non-existent project."""
        mock_database.get_project.return_value = None

        response = client.put(
            "/api/v1/projects/nonexistent",
            json={"name": "Updated Name"}
        )

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_update_project_with_settings(self, client, mock_database, sample_project):
        """Should update project settings."""
        mock_database.get_project.return_value = sample_project
        updated_project = {**sample_project, "settings": {"default_profile_id": "new-profile"}}
        mock_database.update_project.return_value = updated_project

        response = client.put(
            "/api/v1/projects/test-project",
            json={"settings": {"default_profile_id": "new-profile"}}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["settings"]["default_profile_id"] == "new-profile"


# =============================================================================
# Test Delete Project Endpoint
# =============================================================================

class TestDeleteProject:
    """Test DELETE /api/v1/projects/{project_id} endpoint."""

    def test_delete_project_success(self, client, mock_database, sample_project):
        """Should delete project record."""
        mock_database.get_project.return_value = sample_project
        mock_database.delete_project.return_value = True

        response = client.delete("/api/v1/projects/test-project")

        assert response.status_code == 204
        mock_database.delete_project.assert_called_with("test-project")

    def test_delete_project_not_found(self, client, mock_database):
        """Should return 404 for non-existent project."""
        mock_database.get_project.return_value = None

        response = client.delete("/api/v1/projects/nonexistent")

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()


# =============================================================================
# Test List Files Endpoint
# =============================================================================

class TestListProjectFiles:
    """Test GET /api/v1/projects/{project_id}/files endpoint."""

    def test_list_files_success(self, client, mock_database, sample_project, temp_dir, mock_settings):
        """Should list files in project directory."""
        mock_database.get_project.return_value = sample_project

        # Create test files
        project_dir = temp_dir / "test-project"
        project_dir.mkdir()
        (project_dir / "file1.txt").write_text("content1")
        (project_dir / "file2.py").write_text("content2")
        (project_dir / "subdir").mkdir()

        response = client.get("/api/v1/projects/test-project/files")

        assert response.status_code == 200
        data = response.json()
        assert "files" in data
        assert data["projectId"] == "test-project"

        # Check files are returned
        file_names = [f["name"] for f in data["files"]]
        assert "file1.txt" in file_names
        assert "file2.py" in file_names
        assert "subdir" in file_names

    def test_list_files_project_not_found(self, client, mock_database):
        """Should return 404 for non-existent project."""
        mock_database.get_project.return_value = None

        response = client.get("/api/v1/projects/nonexistent/files")

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_list_files_subdirectory(self, client, mock_database, sample_project, temp_dir, mock_settings):
        """Should list files in subdirectory."""
        mock_database.get_project.return_value = sample_project

        # Create test structure
        project_dir = temp_dir / "test-project"
        project_dir.mkdir()
        subdir = project_dir / "subdir"
        subdir.mkdir()
        (subdir / "nested.txt").write_text("nested content")

        response = client.get("/api/v1/projects/test-project/files?path=subdir")

        assert response.status_code == 200
        data = response.json()
        file_names = [f["name"] for f in data["files"]]
        assert "nested.txt" in file_names

    def test_list_files_hide_hidden_by_default(self, client, mock_database, sample_project, temp_dir, mock_settings):
        """Should hide hidden files by default."""
        mock_database.get_project.return_value = sample_project

        project_dir = temp_dir / "test-project"
        project_dir.mkdir()
        (project_dir / ".hidden").write_text("hidden")
        (project_dir / "visible.txt").write_text("visible")

        response = client.get("/api/v1/projects/test-project/files")

        assert response.status_code == 200
        data = response.json()
        file_names = [f["name"] for f in data["files"]]
        assert ".hidden" not in file_names
        assert "visible.txt" in file_names

    def test_list_files_show_hidden(self, client, mock_database, sample_project, temp_dir, mock_settings):
        """Should show hidden files when requested."""
        mock_database.get_project.return_value = sample_project

        project_dir = temp_dir / "test-project"
        project_dir.mkdir()
        (project_dir / ".hidden").write_text("hidden")
        (project_dir / "visible.txt").write_text("visible")

        response = client.get("/api/v1/projects/test-project/files?show_hidden=true")

        assert response.status_code == 200
        data = response.json()
        file_names = [f["name"] for f in data["files"]]
        assert ".hidden" in file_names
        assert "visible.txt" in file_names

    def test_list_files_directory_not_found(self, client, mock_database, sample_project, temp_dir, mock_settings):
        """Should return 404 for non-existent directory."""
        mock_database.get_project.return_value = sample_project

        project_dir = temp_dir / "test-project"
        project_dir.mkdir()

        response = client.get("/api/v1/projects/test-project/files?path=nonexistent")

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_list_files_path_is_file_not_directory(self, client, mock_database, sample_project, temp_dir, mock_settings):
        """Should return 400 when path is a file not directory."""
        mock_database.get_project.return_value = sample_project

        project_dir = temp_dir / "test-project"
        project_dir.mkdir()
        (project_dir / "file.txt").write_text("content")

        response = client.get("/api/v1/projects/test-project/files?path=file.txt")

        assert response.status_code == 400
        assert "not a directory" in response.json()["detail"].lower()

    def test_list_files_rejects_traversal_outside_project(self, client, mock_database, sample_project, temp_dir, mock_settings):
        """A ``../`` path must not list a sibling directory of the project."""
        mock_database.get_project.return_value = sample_project
        (temp_dir / "test-project").mkdir()
        (temp_dir / "other-project").mkdir()

        response = client.get("/api/v1/projects/test-project/files?path=../other-project")

        assert response.status_code == 400


class TestListProjectFilesSessionCheckout:
    """GET /projects/{id}/files?session_id= lists a session's checkout, confined to it."""

    @pytest.fixture
    def checkout(self, temp_dir):
        checkout = temp_dir / "checkout"
        (checkout / "src").mkdir(parents=True)
        (checkout / "src" / "main.py").write_text("x")
        (temp_dir / "outside").mkdir()
        (temp_dir / "outside" / "secret.txt").write_text("s")
        with patch("app.core.project_resolver.resolve_existing_cwd", return_value=str(checkout)):
            yield checkout

    def _session(self, project_id="test-project", **extra):
        return {"id": "s1", "project_id": project_id, **extra}

    def test_lists_session_checkout(self, client, mock_database, checkout):
        mock_database.get_session.return_value = self._session()

        response = client.get("/api/v1/projects/test-project/files?session_id=s1&path=src")

        assert response.status_code == 200
        assert [f["name"] for f in response.json()["files"]] == ["main.py"]

    @pytest.mark.parametrize("path", ["..", "../outside", "src/../../outside"])
    def test_rejects_relative_traversal(self, client, mock_database, checkout, path):
        mock_database.get_session.return_value = self._session()

        response = client.get("/api/v1/projects/test-project/files", params={"session_id": "s1", "path": path})

        assert response.status_code == 400
        assert "secret.txt" not in response.text

    def test_rejects_absolute_path(self, client, mock_database, checkout, temp_dir):
        mock_database.get_session.return_value = self._session()

        response = client.get(
            "/api/v1/projects/test-project/files",
            params={"session_id": "s1", "path": str(temp_dir / "outside")},
        )

        assert response.status_code == 400

    def test_session_from_another_project_is_not_found(self, client, mock_database, checkout):
        mock_database.get_session.return_value = self._session(project_id="other-project")

        response = client.get("/api/v1/projects/test-project/files?session_id=s1")

        assert response.status_code == 404
        mock_database.get_project.assert_not_called()

    def test_restricted_api_user_denied_foreign_profile_session(self, client, mock_database, mock_api_user, checkout):
        mock_api_user.return_value = {"id": "u1", "project_id": "test-project", "profile_id": "mine"}
        mock_database.get_session.return_value = self._session(profile_id="theirs")

        response = client.get("/api/v1/projects/test-project/files?session_id=s1")

        assert response.status_code == 403

    def test_chat_history_rejects_session_from_another_project(self, client, mock_database, checkout):
        mock_database.get_session.return_value = self._session(project_id="other-project")

        with patch("app.core.jsonl_parser.list_chat_history_sessions", return_value=[]) as listing:
            response = client.get("/api/v1/projects/test-project/chat-history?session_id=s1")

        assert response.status_code == 404
        listing.assert_not_called()

    def test_chat_history_rejects_worktree_from_another_project(self, client, mock_database):
        mock_database.get_worktree.return_value = {"id": "wt1", "repository_id": "repo-other", "worktree_path": "/elsewhere"}
        mock_database.get_git_repository_by_project.return_value = {"id": "repo-mine"}

        with patch("app.core.jsonl_parser.list_chat_history_sessions", return_value=[]) as listing:
            response = client.get("/api/v1/projects/test-project/chat-history?worktree_id=wt1")

        assert response.status_code == 404
        mock_database.get_git_repository_by_project.assert_called_once_with("test-project")
        listing.assert_not_called()

    def test_chat_history_lists_worktree_of_this_project(self, client, mock_database):
        mock_database.get_worktree.return_value = {"id": "wt1", "repository_id": "repo-mine", "worktree_path": "/wt"}
        mock_database.get_git_repository_by_project.return_value = {"id": "repo-mine"}

        with patch("app.api.projects.get_worktree_working_dir", return_value="/wt"), \
             patch("app.core.jsonl_parser.list_chat_history_sessions", return_value=[]) as listing:
            response = client.get("/api/v1/projects/test-project/chat-history?worktree_id=wt1")

        assert response.status_code == 200
        assert listing.call_args.kwargs["working_dir"] == "/wt"


# =============================================================================
# Test Upload File Endpoint
# =============================================================================

class TestUploadFile:
    """Test POST /api/v1/projects/{project_id}/upload endpoint."""

    def test_upload_file_success(self, client, mock_database, sample_project, temp_dir, mock_settings):
        """Should upload file successfully."""
        mock_database.get_project.return_value = sample_project

        # Create project directory
        project_dir = temp_dir / "test-project"
        project_dir.mkdir()

        files = {"file": ("test.txt", BytesIO(b"file content"), "text/plain")}
        response = client.post(
            "/api/v1/projects/test-project/upload",
            files=files,
            data={"path": ""}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["filename"] == "test.txt"
        assert data["size"] == 12  # len("file content")

    def test_upload_file_to_subdirectory(self, client, mock_database, sample_project, temp_dir, mock_settings):
        """Should upload to specified subdirectory."""
        mock_database.get_project.return_value = sample_project

        project_dir = temp_dir / "test-project"
        project_dir.mkdir()

        files = {"file": ("test.txt", BytesIO(b"content"), "text/plain")}
        response = client.post(
            "/api/v1/projects/test-project/upload",
            files=files,
            data={"path": "subdir"}
        )

        assert response.status_code == 200
        assert "subdir" in response.json()["path"]

    def test_upload_file_duplicate_renames(self, client, mock_database, sample_project, temp_dir, mock_settings):
        """Should rename duplicate files with counter."""
        mock_database.get_project.return_value = sample_project

        project_dir = temp_dir / "test-project"
        project_dir.mkdir()
        uploads_dir = project_dir / "uploads"
        uploads_dir.mkdir()
        (uploads_dir / "test.txt").write_text("existing")

        files = {"file": ("test.txt", BytesIO(b"new content"), "text/plain")}
        response = client.post(
            "/api/v1/projects/test-project/upload",
            files=files,
            data={"path": ""}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["filename"] == "test_1.txt"

    def test_upload_file_invalid_filename(self, client, mock_database, sample_project, temp_dir, mock_settings):
        """Should reject hidden/invalid filenames."""
        mock_database.get_project.return_value = sample_project

        project_dir = temp_dir / "test-project"
        project_dir.mkdir()

        files = {"file": (".hidden", BytesIO(b"content"), "text/plain")}
        response = client.post(
            "/api/v1/projects/test-project/upload",
            files=files,
            data={"path": ""}
        )

        assert response.status_code == 400
        assert "Invalid filename" in response.json()["detail"]

    def test_upload_file_project_not_found(self, client, mock_database):
        """Should return 404 for non-existent project."""
        mock_database.get_project.return_value = None

        files = {"file": ("test.txt", BytesIO(b"content"), "text/plain")}
        response = client.post(
            "/api/v1/projects/nonexistent/upload",
            files=files,
            data={"path": ""}
        )

        assert response.status_code == 404


# =============================================================================
# Test Download File Endpoint
# =============================================================================

class TestDownloadFile:
    """Test GET /api/v1/projects/{project_id}/files/download endpoint."""

    def test_download_file_success(self, client, mock_database, sample_project, temp_dir, mock_settings):
        """Should download file successfully."""
        mock_database.get_project.return_value = sample_project

        project_dir = temp_dir / "test-project"
        project_dir.mkdir()
        (project_dir / "download.txt").write_text("file content")

        response = client.get("/api/v1/projects/test-project/files/download?path=download.txt")

        assert response.status_code == 200
        assert response.content == b"file content"

    def test_download_file_not_found(self, client, mock_database, sample_project, temp_dir, mock_settings):
        """Should return 404 for non-existent file."""
        mock_database.get_project.return_value = sample_project

        project_dir = temp_dir / "test-project"
        project_dir.mkdir()

        response = client.get("/api/v1/projects/test-project/files/download?path=nonexistent.txt")

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_download_directory_rejected(self, client, mock_database, sample_project, temp_dir, mock_settings):
        """Should reject downloading a directory."""
        mock_database.get_project.return_value = sample_project

        project_dir = temp_dir / "test-project"
        project_dir.mkdir()
        (project_dir / "subdir").mkdir()

        response = client.get("/api/v1/projects/test-project/files/download?path=subdir")

        assert response.status_code == 400
        assert "not a file" in response.json()["detail"].lower()

    def test_download_project_not_found(self, client, mock_database):
        """Should return 404 for non-existent project."""
        mock_database.get_project.return_value = None

        response = client.get("/api/v1/projects/nonexistent/files/download?path=file.txt")

        assert response.status_code == 404


# =============================================================================
# Test Create Folder Endpoint
# =============================================================================

class TestCreateFolder:
    """Test POST /api/v1/projects/{project_id}/files/folder endpoint."""

    def test_create_folder_success(self, client, mock_database, sample_project, temp_dir, mock_settings):
        """Should create folder successfully."""
        mock_database.get_project.return_value = sample_project

        project_dir = temp_dir / "test-project"
        project_dir.mkdir()

        response = client.post(
            "/api/v1/projects/test-project/files/folder",
            json={"name": "newfolder"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "newfolder"
        assert data["type"] == "directory"
        assert (project_dir / "newfolder").exists()

    def test_create_folder_in_subdirectory(self, client, mock_database, sample_project, temp_dir, mock_settings):
        """Should create folder in subdirectory."""
        mock_database.get_project.return_value = sample_project

        project_dir = temp_dir / "test-project"
        project_dir.mkdir()
        (project_dir / "parent").mkdir()

        response = client.post(
            "/api/v1/projects/test-project/files/folder?path=parent",
            json={"name": "child"}
        )

        assert response.status_code == 200
        assert (project_dir / "parent" / "child").exists()

    def test_create_folder_invalid_name(self, client, mock_database, sample_project, temp_dir, mock_settings):
        """Should reject folder names with slashes."""
        mock_database.get_project.return_value = sample_project

        project_dir = temp_dir / "test-project"
        project_dir.mkdir()

        response = client.post(
            "/api/v1/projects/test-project/files/folder",
            json={"name": "folder/with/slashes"}
        )

        assert response.status_code == 400
        assert "Invalid folder name" in response.json()["detail"]

    def test_create_folder_already_exists(self, client, mock_database, sample_project, temp_dir, mock_settings):
        """Should return 409 if folder already exists."""
        mock_database.get_project.return_value = sample_project

        project_dir = temp_dir / "test-project"
        project_dir.mkdir()
        (project_dir / "existing").mkdir()

        response = client.post(
            "/api/v1/projects/test-project/files/folder",
            json={"name": "existing"}
        )

        assert response.status_code == 409
        assert "already exists" in response.json()["detail"].lower()

    def test_create_folder_project_not_found(self, client, mock_database):
        """Should return 404 for non-existent project."""
        mock_database.get_project.return_value = None

        response = client.post(
            "/api/v1/projects/nonexistent/files/folder",
            json={"name": "newfolder"}
        )

        assert response.status_code == 404


# =============================================================================
# Test Rename File/Folder Endpoint
# =============================================================================

class TestRenameFileOrFolder:
    """Test PUT /api/v1/projects/{project_id}/files/rename endpoint."""

    def test_rename_file_success(self, client, mock_database, sample_project, temp_dir, mock_settings):
        """Should rename file successfully."""
        mock_database.get_project.return_value = sample_project

        project_dir = temp_dir / "test-project"
        project_dir.mkdir()
        (project_dir / "oldname.txt").write_text("content")

        response = client.put(
            "/api/v1/projects/test-project/files/rename?path=oldname.txt",
            json={"new_name": "newname.txt"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "newname.txt"
        assert data["type"] == "file"
        assert not (project_dir / "oldname.txt").exists()
        assert (project_dir / "newname.txt").exists()

    def test_rename_folder_success(self, client, mock_database, sample_project, temp_dir, mock_settings):
        """Should rename folder successfully."""
        mock_database.get_project.return_value = sample_project

        project_dir = temp_dir / "test-project"
        project_dir.mkdir()
        (project_dir / "oldfolder").mkdir()

        response = client.put(
            "/api/v1/projects/test-project/files/rename?path=oldfolder",
            json={"new_name": "newfolder"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "newfolder"
        assert data["type"] == "directory"

    def test_rename_invalid_name(self, client, mock_database, sample_project, temp_dir, mock_settings):
        """Should reject names with slashes."""
        mock_database.get_project.return_value = sample_project

        project_dir = temp_dir / "test-project"
        project_dir.mkdir()
        (project_dir / "file.txt").write_text("content")

        response = client.put(
            "/api/v1/projects/test-project/files/rename?path=file.txt",
            json={"new_name": "path/to/file.txt"}
        )

        assert response.status_code == 400
        assert "Invalid name" in response.json()["detail"]

    def test_rename_target_exists(self, client, mock_database, sample_project, temp_dir, mock_settings):
        """Should return 409 if target already exists."""
        mock_database.get_project.return_value = sample_project

        project_dir = temp_dir / "test-project"
        project_dir.mkdir()
        (project_dir / "source.txt").write_text("source")
        (project_dir / "target.txt").write_text("target")

        response = client.put(
            "/api/v1/projects/test-project/files/rename?path=source.txt",
            json={"new_name": "target.txt"}
        )

        assert response.status_code == 409
        assert "already exists" in response.json()["detail"].lower()

    def test_rename_source_not_found(self, client, mock_database, sample_project, temp_dir, mock_settings):
        """Should return 404 for non-existent source."""
        mock_database.get_project.return_value = sample_project

        project_dir = temp_dir / "test-project"
        project_dir.mkdir()

        response = client.put(
            "/api/v1/projects/test-project/files/rename?path=nonexistent.txt",
            json={"new_name": "new.txt"}
        )

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_rename_project_not_found(self, client, mock_database):
        """Should return 404 for non-existent project."""
        mock_database.get_project.return_value = None

        response = client.put(
            "/api/v1/projects/nonexistent/files/rename?path=file.txt",
            json={"new_name": "new.txt"}
        )

        assert response.status_code == 404


# =============================================================================
# Test Delete File/Folder Endpoint
# =============================================================================

class TestDeleteFileOrFolder:
    """Test DELETE /api/v1/projects/{project_id}/files endpoint."""

    def test_delete_file_success(self, client, mock_database, sample_project, temp_dir, mock_settings):
        """Should delete file successfully."""
        mock_database.get_project.return_value = sample_project

        project_dir = temp_dir / "test-project"
        project_dir.mkdir()
        (project_dir / "todelete.txt").write_text("content")

        response = client.delete("/api/v1/projects/test-project/files?path=todelete.txt")

        assert response.status_code == 200
        assert response.json()["deleted"] == "todelete.txt"
        assert not (project_dir / "todelete.txt").exists()

    def test_delete_folder_success(self, client, mock_database, sample_project, temp_dir, mock_settings):
        """Should delete folder recursively."""
        mock_database.get_project.return_value = sample_project

        project_dir = temp_dir / "test-project"
        project_dir.mkdir()
        folder = project_dir / "todelete"
        folder.mkdir()
        (folder / "nested.txt").write_text("nested")

        response = client.delete("/api/v1/projects/test-project/files?path=todelete")

        assert response.status_code == 200
        assert not folder.exists()

    def test_delete_project_root_rejected(self, client, mock_database, sample_project, temp_dir, mock_settings):
        """Should reject deleting project root."""
        mock_database.get_project.return_value = sample_project

        project_dir = temp_dir / "test-project"
        project_dir.mkdir()

        response = client.delete("/api/v1/projects/test-project/files?path=.")

        assert response.status_code == 400
        assert "Cannot delete project root" in response.json()["detail"]

    def test_delete_not_found(self, client, mock_database, sample_project, temp_dir, mock_settings):
        """Should return 404 for non-existent target."""
        mock_database.get_project.return_value = sample_project

        project_dir = temp_dir / "test-project"
        project_dir.mkdir()

        response = client.delete("/api/v1/projects/test-project/files?path=nonexistent.txt")

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_delete_project_not_found(self, client, mock_database):
        """Should return 404 for non-existent project."""
        mock_database.get_project.return_value = None

        response = client.delete("/api/v1/projects/nonexistent/files?path=file.txt")

        assert response.status_code == 404


class TestProjectFileRoutesStayInProject:
    """Download, create-folder, rename, delete and upload are confined to the project.

    The workspace boundary alone lets ``../other-project`` through, so each route
    must also reject paths that leave the project directory, and upload must reject
    a session or worktree that belongs to another project.
    """

    TRAVERSALS = ["..", "../other-project", "sub/../../other-project"]

    @pytest.fixture
    def layout(self, mock_database, sample_project, temp_dir, mock_settings):
        mock_database.get_project.return_value = sample_project
        project_dir = temp_dir / "test-project"
        (project_dir / "sub").mkdir(parents=True)
        other = temp_dir / "other-project"
        other.mkdir()
        (other / "secret.txt").write_text("secret")
        return project_dir, other

    @pytest.mark.parametrize("path", ["../other-project/secret.txt", "sub/../../other-project/secret.txt"])
    def test_download_rejects_traversal(self, client, layout, path):
        response = client.get("/api/v1/projects/test-project/files/download", params={"path": path})

        assert response.status_code == 400
        assert "secret" not in response.text

    def test_download_rejects_traversal_for_project_scoped_api_user(self, client, layout, mock_api_user):
        mock_api_user.return_value = {"id": "u1", "project_id": "test-project"}

        response = client.get(
            "/api/v1/projects/test-project/files/download", params={"path": "../other-project/secret.txt"}
        )

        assert response.status_code == 400

    def test_download_absolute_path_stays_in_project(self, client, layout):
        _, other = layout

        response = client.get(
            "/api/v1/projects/test-project/files/download", params={"path": str(other / "secret.txt")}
        )

        assert response.status_code == 404
        assert "secret" not in response.text

    @pytest.mark.parametrize("path", TRAVERSALS)
    def test_create_folder_rejects_traversal(self, client, layout, temp_dir, path):
        response = client.post(
            "/api/v1/projects/test-project/files/folder", params={"path": path}, json={"name": "planted"}
        )

        assert response.status_code == 400
        assert not (temp_dir / "other-project" / "planted").exists()
        assert not (temp_dir / "planted").exists()

    def test_create_folder_rejects_dotdot_name(self, client, layout):
        response = client.post("/api/v1/projects/test-project/files/folder", json={"name": ".."})

        assert response.status_code == 400

    @pytest.mark.parametrize("path", ["../other-project/secret.txt", "sub/../../other-project/secret.txt"])
    def test_rename_rejects_traversal(self, client, layout, path):
        _, other = layout

        response = client.put(
            "/api/v1/projects/test-project/files/rename", params={"path": path}, json={"new_name": "moved.txt"}
        )

        assert response.status_code == 400
        assert (other / "secret.txt").exists()

    @pytest.mark.parametrize("path", ["", "."])
    def test_rename_rejects_project_root(self, client, layout, temp_dir, path):
        response = client.put(
            "/api/v1/projects/test-project/files/rename", params={"path": path}, json={"new_name": "renamed"}
        )

        assert response.status_code == 400
        assert (temp_dir / "test-project").is_dir()
        assert not (temp_dir / "renamed").exists()

    @pytest.mark.parametrize("path", TRAVERSALS)
    def test_delete_rejects_traversal(self, client, layout, path):
        project_dir, other = layout

        response = client.delete("/api/v1/projects/test-project/files", params={"path": path})

        assert response.status_code == 400
        assert (other / "secret.txt").exists()
        assert project_dir.is_dir()

    def test_delete_absolute_path_stays_in_project(self, client, layout):
        _, other = layout

        response = client.delete("/api/v1/projects/test-project/files", params={"path": str(other)})

        assert response.status_code == 404
        assert (other / "secret.txt").exists()

    def test_delete_inside_project_still_works(self, client, layout):
        project_dir, _ = layout
        (project_dir / "sub" / "gone.txt").write_text("x")

        response = client.delete("/api/v1/projects/test-project/files", params={"path": "sub/gone.txt"})

        assert response.status_code == 200
        assert not (project_dir / "sub" / "gone.txt").exists()

    @pytest.fixture
    def alias(self, layout):
        """``real/keep.txt`` plus ``alias -> real`` and ``sub/link.txt -> ../real/keep.txt``."""
        project_dir, _ = layout
        real = project_dir / "real"
        real.mkdir()
        (real / "keep.txt").write_text("keep")
        (project_dir / "alias").symlink_to(real, target_is_directory=True)
        (project_dir / "sub" / "link.txt").symlink_to(real / "keep.txt")
        return project_dir

    @pytest.mark.parametrize("path", ["alias", "/alias", "sub/link.txt"])
    def test_delete_removes_the_symlink_not_its_target(self, client, alias, path):
        response = client.delete("/api/v1/projects/test-project/files", params={"path": path})

        assert response.status_code == 200
        assert not (alias / path.lstrip("/")).is_symlink()
        assert (alias / "real" / "keep.txt").read_text() == "keep"

    def test_delete_removes_a_dangling_symlink(self, client, alias):
        (alias / "real" / "keep.txt").unlink()

        response = client.delete("/api/v1/projects/test-project/files", params={"path": "sub/link.txt"})

        assert response.status_code == 200
        assert not (alias / "sub" / "link.txt").is_symlink()

    @pytest.mark.parametrize("path, new_name", [("alias", "renamed"), ("sub/link.txt", "moved.txt")])
    def test_rename_renames_the_symlink_not_its_target(self, client, alias, path, new_name):
        response = client.put(
            "/api/v1/projects/test-project/files/rename", params={"path": path}, json={"new_name": new_name}
        )

        assert response.status_code == 200
        link = alias / path
        renamed = link.parent / new_name
        assert not link.is_symlink()
        assert renamed.is_symlink()
        assert response.json()["path"] == str(renamed.relative_to(alias))
        assert (alias / "real" / "keep.txt").read_text() == "keep"
        assert not (alias / "real" / new_name).exists()

    def test_delete_through_a_symlinked_directory_stays_confined(self, client, layout, temp_dir):
        project_dir, other = layout
        (project_dir / "out").symlink_to(other, target_is_directory=True)

        response = client.delete("/api/v1/projects/test-project/files", params={"path": "out/secret.txt"})

        assert response.status_code == 400
        assert (other / "secret.txt").exists()

    def test_upload_rejects_session_from_another_project(self, client, mock_database, layout):
        mock_database.get_session.return_value = {"id": "s1", "project_id": "other-project"}

        response = client.post(
            "/api/v1/projects/test-project/upload",
            files={"file": ("a.txt", BytesIO(b"x"), "text/plain")},
            data={"session_id": "s1"},
        )

        assert response.status_code == 404
        mock_database.get_worktree_by_session.assert_not_called()

    def test_upload_rejects_worktree_from_another_project(self, client, mock_database, layout):
        mock_database.get_worktree.return_value = {"id": "wt1", "repository_id": "repo-other", "worktree_path": "wt"}
        mock_database.get_git_repository_by_project.return_value = {"id": "repo-mine"}

        response = client.post(
            "/api/v1/projects/test-project/upload",
            files={"file": ("a.txt", BytesIO(b"x"), "text/plain")},
            data={"worktree_id": "wt1"},
        )

        assert response.status_code == 404

    def test_upload_with_own_session_still_works(self, client, mock_database, layout):
        project_dir, _ = layout
        mock_database.get_session.return_value = {"id": "s1", "project_id": "test-project"}
        mock_database.get_worktree_by_session.return_value = None

        response = client.post(
            "/api/v1/projects/test-project/upload",
            files={"file": ("a.txt", BytesIO(b"x"), "text/plain")},
            data={"session_id": "s1"},
        )

        assert response.status_code == 200
        assert (project_dir / "uploads" / "a.txt").read_bytes() == b"x"


# =============================================================================
# Test Sync Worktrees Endpoint
# =============================================================================

class TestSyncWorktrees:
    """Test POST /api/v1/projects/{project_id}/sync-worktrees endpoint."""

    def test_sync_worktrees_success(self, client, mock_database, sample_project):
        """Should sync worktrees successfully."""
        mock_database.get_project.return_value = sample_project

        mock_result = {
            "is_git_repo": True,
            "active": 2,
            "cleaned_up": []
        }

        mock_wm = MagicMock()
        mock_wm.sync_worktrees_for_project.return_value = mock_result

        with patch.dict("sys.modules", {"app.core.worktree_manager": MagicMock(worktree_manager=mock_wm)}):
            response = client.post("/api/v1/projects/test-project/sync-worktrees")

            assert response.status_code == 200
            data = response.json()
            assert data["is_git_repo"] is True
            assert data["active_worktrees"] == 2
            assert data["cleaned_up"] == []

    def test_sync_worktrees_not_git_repo(self, client, mock_database, sample_project):
        """Should handle non-git repository."""
        mock_database.get_project.return_value = sample_project

        mock_result = {
            "is_git_repo": False,
            "active": 0,
            "cleaned_up": []
        }

        mock_wm = MagicMock()
        mock_wm.sync_worktrees_for_project.return_value = mock_result

        with patch.dict("sys.modules", {"app.core.worktree_manager": MagicMock(worktree_manager=mock_wm)}):
            response = client.post("/api/v1/projects/test-project/sync-worktrees")

            assert response.status_code == 200
            data = response.json()
            assert data["is_git_repo"] is False
            assert data["active_worktrees"] == 0

    def test_sync_worktrees_with_cleanup(self, client, mock_database, sample_project):
        """Should report cleaned up worktrees."""
        mock_database.get_project.return_value = sample_project

        mock_result = {
            "is_git_repo": True,
            "active": 1,
            "cleaned_up": [
                {"id": "wt-1", "branch": "feature-1", "reason": "path_not_found"}
            ]
        }

        mock_wm = MagicMock()
        mock_wm.sync_worktrees_for_project.return_value = mock_result

        with patch.dict("sys.modules", {"app.core.worktree_manager": MagicMock(worktree_manager=mock_wm)}):
            response = client.post("/api/v1/projects/test-project/sync-worktrees")

            assert response.status_code == 200
            data = response.json()
            assert len(data["cleaned_up"]) == 1
            assert data["cleaned_up"][0]["id"] == "wt-1"

    def test_sync_worktrees_project_not_found(self, client, mock_database):
        """Missing project returns a safe default, not 404.

        The frontend polls sync-worktrees while loading a chat card, and may hit
        it before the project record exists during startup. The endpoint returns
        WorktreeSyncResponse(is_git_repo=False, ...) with 200 to avoid noisy logs.
        """
        mock_database.get_project.return_value = None

        response = client.post("/api/v1/projects/nonexistent/sync-worktrees")

        assert response.status_code == 200
        data = response.json()
        assert data["is_git_repo"] is False
        assert data["active_worktrees"] == 0
        assert data["cleaned_up"] == []
        assert data["imported"] == 0

    def test_sync_worktrees_access_denied(self, client, mock_database, mock_api_user, sample_project):
        """API user should be denied access to other projects."""
        mock_api_user.return_value = {
            "id": "user-1",
            "project_id": "other-project"
        }
        mock_database.get_project.return_value = sample_project

        response = client.post("/api/v1/projects/test-project/sync-worktrees")

        assert response.status_code == 403
        assert "Access denied" in response.json()["detail"]


# =============================================================================
# Test Access Control
# =============================================================================

class TestAccessControl:
    """Test access control across endpoints."""

    def test_api_user_can_access_assigned_project(self, client, mock_database, mock_api_user, sample_project, temp_dir, mock_settings):
        """API user should access their assigned project."""
        mock_api_user.return_value = {
            "id": "user-1",
            "project_id": "test-project"
        }
        mock_database.get_project.return_value = sample_project

        project_dir = temp_dir / "test-project"
        project_dir.mkdir()
        (project_dir / "file.txt").write_text("content")

        response = client.get("/api/v1/projects/test-project/files")

        assert response.status_code == 200

    def test_api_user_denied_other_project(self, client, mock_database, mock_api_user, sample_project):
        """API user should be denied access to other projects."""
        mock_api_user.return_value = {
            "id": "user-1",
            "project_id": "my-project"
        }
        mock_database.get_project.return_value = sample_project

        response = client.get("/api/v1/projects/test-project/files")

        assert response.status_code == 403


# =============================================================================
# Test File Info Details
# =============================================================================

class TestFileInfoDetails:
    """Test file info details returned by list endpoint."""

    def test_file_info_includes_size(self, client, mock_database, sample_project, temp_dir, mock_settings):
        """File info should include size and formatted size."""
        mock_database.get_project.return_value = sample_project

        project_dir = temp_dir / "test-project"
        project_dir.mkdir()
        (project_dir / "test.txt").write_text("0123456789")  # 10 bytes

        response = client.get("/api/v1/projects/test-project/files")

        assert response.status_code == 200
        data = response.json()
        file_info = next(f for f in data["files"] if f["name"] == "test.txt")
        assert file_info["size"] == 10
        assert file_info["sizeFormatted"] == "10 B"

    def test_file_info_includes_extension(self, client, mock_database, sample_project, temp_dir, mock_settings):
        """File info should include extension."""
        mock_database.get_project.return_value = sample_project

        project_dir = temp_dir / "test-project"
        project_dir.mkdir()
        (project_dir / "script.py").write_text("print('hi')")

        response = client.get("/api/v1/projects/test-project/files")

        assert response.status_code == 200
        data = response.json()
        file_info = next(f for f in data["files"] if f["name"] == "script.py")
        assert file_info["extension"] == "py"
        assert file_info["type"] == "file"

    def test_directory_info_has_null_size(self, client, mock_database, sample_project, temp_dir, mock_settings):
        """Directory info should have null size and extension."""
        mock_database.get_project.return_value = sample_project

        project_dir = temp_dir / "test-project"
        project_dir.mkdir()
        (project_dir / "mydir").mkdir()

        response = client.get("/api/v1/projects/test-project/files")

        assert response.status_code == 200
        data = response.json()
        dir_info = next(f for f in data["files"] if f["name"] == "mydir")
        assert dir_info["size"] is None
        assert dir_info["extension"] is None
        assert dir_info["type"] == "directory"

    def test_files_sorted_directories_first(self, client, mock_database, sample_project, temp_dir, mock_settings):
        """Files should be sorted with directories first, then alphabetically."""
        mock_database.get_project.return_value = sample_project

        project_dir = temp_dir / "test-project"
        project_dir.mkdir()
        (project_dir / "zfile.txt").write_text("z")
        (project_dir / "afile.txt").write_text("a")
        (project_dir / "zdir").mkdir()
        (project_dir / "adir").mkdir()

        response = client.get("/api/v1/projects/test-project/files")

        assert response.status_code == 200
        data = response.json()
        names = [f["name"] for f in data["files"]]

        # Directories should come first, alphabetically
        dir_idx_a = names.index("adir")
        dir_idx_z = names.index("zdir")
        file_idx_a = names.index("afile.txt")
        file_idx_z = names.index("zfile.txt")

        assert dir_idx_a < file_idx_a  # dirs before files
        assert dir_idx_z < file_idx_a  # dirs before files
        assert dir_idx_a < dir_idx_z  # adir before zdir
        assert file_idx_a < file_idx_z  # afile before zfile


# =============================================================================
# Test Session Sync Agent
# =============================================================================

class TestSyncSessionsAgent:
    """sync-sessions files imports under a real agent; there is no "default"."""

    @pytest.fixture
    def sync(self):
        with patch("app.core.session_sync.session_sync_service") as svc:
            svc.sync_project_sessions.return_value = {"imported_count": 0, "skipped_count": 0, "errors": [], "sessions": []}
            yield svc.sync_project_sessions

    def test_defaults_to_first_agent(self, client, mock_database, sample_project, sync):
        mock_database.get_project.return_value = sample_project
        mock_database.get_all_profiles.return_value = [{"id": "agent-a"}, {"id": "agent-b"}]

        assert client.post("/api/v1/projects/test-project/sync-sessions").status_code == 200
        assert sync.call_args.kwargs["profile_id"] == "agent-a"

    def test_prefers_project_default_agent(self, client, mock_database, sample_project, sync):
        mock_database.get_project.return_value = {**sample_project, "settings": {"default_profile_id": "agent-b"}}
        mock_database.get_profile.return_value = {"id": "agent-b"}

        assert client.post("/api/v1/projects/test-project/sync-sessions").status_code == 200
        assert sync.call_args.kwargs["profile_id"] == "agent-b"

    def test_no_agents_is_a_clear_conflict(self, client, mock_database, sample_project, sync):
        mock_database.get_project.return_value = sample_project
        mock_database.get_all_profiles.return_value = []

        response = client.post("/api/v1/projects/test-project/sync-sessions")

        assert response.status_code == 409
        assert "create an agent" in response.json()["detail"]
        sync.assert_not_called()

    def test_unknown_agent_is_rejected(self, client, mock_database, sample_project, sync):
        mock_database.get_project.return_value = sample_project
        mock_database.get_profile.return_value = None

        response = client.post("/api/v1/projects/test-project/sync-sessions?profile_id=missing")

        assert response.status_code == 404
        sync.assert_not_called()
