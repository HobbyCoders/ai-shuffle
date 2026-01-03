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
import tempfile
import shutil
from datetime import datetime
from pathlib import Path
from io import BytesIO
from unittest.mock import patch, MagicMock, AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.projects import router, validate_project_path, check_project_access
from app.api.auth import require_auth, require_admin


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
    """Mock settings with temporary workspace directory."""
    mock_settings_obj = MagicMock()
    mock_settings_obj.workspace_dir = temp_dir
    return mock_settings_obj


@pytest.fixture
def mock_database():
    """Mock database module."""
    with patch("app.api.projects.database") as mock_db:
        yield mock_db


@pytest.fixture
def mock_api_user():
    """Mock for get_api_user_from_request."""
    with patch("app.api.projects.get_api_user_from_request") as mock:
        mock.return_value = None  # Default to admin (no api user)
        yield mock


@pytest.fixture
def app(mock_database, mock_settings, mock_api_user):
    """Create a FastAPI app with the projects router and mocked dependencies."""
    with patch("app.api.projects.settings", mock_settings):
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


@pytest.fixture
def sample_project_with_settings():
    """Sample project with settings."""
    return {
        "id": "test-project-settings",
        "name": "Test Project with Settings",
        "description": "A test project with settings",
        "path": "test-project-settings",
        "settings": {
            "default_profile_id": "custom-profile",
            "custom_instructions": "Some instructions"
        },
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


class TestCheckProjectAccess:
    """Test project access control."""

    def test_admin_has_full_access(self):
        """Admin user should have access to any project."""
        request = MagicMock()
        with patch("app.api.projects.get_api_user_from_request") as mock_api_user:
            mock_api_user.return_value = None  # Admin has no api_user
            # Should not raise
            check_project_access(request, "any-project")

    def test_api_user_with_matching_project(self):
        """API user should access their assigned project."""
        request = MagicMock()
        with patch("app.api.projects.get_api_user_from_request") as mock_api_user:
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
        with patch("app.api.projects.get_api_user_from_request") as mock_api_user:
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
        with patch("app.api.projects.get_api_user_from_request") as mock_api_user:
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
        """Should return 404 for non-existent project."""
        mock_database.get_project.return_value = None

        response = client.post("/api/v1/projects/nonexistent/sync-worktrees")

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

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
