"""
Unit tests for git API endpoints.

Tests cover:
- Git status endpoint
- Branch management (list, create, delete)
- Checkout operations
- Commit graph endpoint
- Fetch from remote
- Worktree management (list, create, get, delete, sync)
- Authentication and authorization
- Error handling and edge cases
"""

import pytest
from datetime import datetime
from unittest.mock import patch, MagicMock
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api.git import router, require_auth, get_api_user_from_request


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def mock_git_service():
    """Create a mock git service."""
    return MagicMock()


@pytest.fixture
def mock_worktree_manager():
    """Create a mock worktree manager."""
    return MagicMock()


@pytest.fixture
def mock_database():
    """Create a mock database module."""
    return MagicMock()


@pytest.fixture
def mock_settings():
    """Create mock settings."""
    mock_settings_obj = MagicMock()
    mock_settings_obj.workspace_dir = Path("/test/workspace")
    return mock_settings_obj


def create_mock_request_state():
    """Create a mock request with state for API user."""
    request = MagicMock()
    request.state = MagicMock()
    request.state.api_user = None
    return request


@pytest.fixture
def app(mock_git_service, mock_worktree_manager, mock_database, mock_settings):
    """Create a FastAPI app with mocked dependencies."""
    app = FastAPI()
    app.include_router(router)

    # Override auth dependency
    app.dependency_overrides[require_auth] = lambda: "test-session-token"

    return app


@pytest.fixture
def client(app, mock_git_service, mock_worktree_manager, mock_database, mock_settings):
    """Create a test client with mocked dependencies."""
    with patch("app.api.git.git_service", mock_git_service):
        with patch("app.api.git.worktree_manager", mock_worktree_manager):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request") as mock_api_user:
                        mock_api_user.return_value = None
                        with TestClient(app) as test_client:
                            yield test_client


@pytest.fixture
def sample_project():
    """Sample project data."""
    return {
        "id": "test-project-id",
        "name": "Test Project",
        "path": "test-project",
        "created_at": "2024-01-15T10:30:00Z"
    }


@pytest.fixture
def sample_repository():
    """Sample git repository data."""
    return {
        "id": "repo-abc123",
        "project_id": "test-project-id",
        "remote_url": "https://github.com/test/repo.git",
        "default_branch": "main",
        "github_repo_name": "test/repo",
        "last_synced_at": "2024-01-15T10:30:00Z"
    }


@pytest.fixture
def sample_git_status():
    """Sample git status data."""
    return {
        "is_git_repo": True,
        "current_branch": "main",
        "is_detached": False,
        "head_commit": "abc123def456",
        "remote_url": "https://github.com/test/repo.git",
        "is_clean": True,
        "staged": [],
        "modified": [],
        "untracked": [],
        "ahead": 0,
        "behind": 0,
        "conflicts": []
    }


@pytest.fixture
def sample_branches():
    """Sample branch list data."""
    return [
        {
            "name": "main",
            "is_current": True,
            "is_remote": False,
            "commit": "abc123d",
            "commit_message": "Initial commit",
            "upstream": "origin/main",
            "ahead": 0,
            "behind": 0
        },
        {
            "name": "feature/test",
            "is_current": False,
            "is_remote": False,
            "commit": "def456a",
            "commit_message": "Add feature",
            "upstream": None,
            "ahead": 1,
            "behind": 0
        },
        {
            "name": "origin/main",
            "is_current": False,
            "is_remote": True,
            "commit": "abc123d",
            "commit_message": "Initial commit",
            "upstream": None,
            "ahead": 0,
            "behind": 0
        }
    ]


@pytest.fixture
def sample_commits():
    """Sample commit graph data."""
    return [
        {
            "sha": "abc123def456789012345678901234567890abcd",
            "short_sha": "abc123d",
            "message": "Initial commit",
            "author": "Test User",
            "author_email": "test@example.com",
            "timestamp": "2024-01-15T10:30:00Z",
            "parents": [],
            "refs": ["HEAD -> main", "origin/main"]
        },
        {
            "sha": "def456abc789012345678901234567890123abcd",
            "short_sha": "def456a",
            "message": "Add feature",
            "author": "Test User",
            "author_email": "test@example.com",
            "timestamp": "2024-01-15T11:30:00Z",
            "parents": ["abc123def456789012345678901234567890abcd"],
            "refs": ["feature/test"]
        }
    ]


@pytest.fixture
def sample_worktree():
    """Sample worktree data."""
    return {
        "id": "wt-abc123",
        "repository_id": "repo-abc123",
        "session_id": None,
        "branch_name": "feature/test",
        "worktree_path": ".worktrees/test-project-id/feature-test",
        "base_branch": "main",
        "status": "active",
        "created_at": "2024-01-15T10:30:00Z",
        "sessions": [],
        "session_count": 0,
        "active_session": None,
        "exists": True,
        "git_status": None
    }


# =============================================================================
# Test Module Imports
# =============================================================================

class TestGitModuleImports:
    """Verify git module can be imported correctly."""

    def test_git_module_imports(self):
        """Git module should import without errors."""
        from app.api import git
        assert git is not None

    def test_git_router_exists(self):
        """Git router should exist."""
        from app.api.git import router
        assert router is not None

    def test_router_has_correct_prefix(self):
        """Router should have correct prefix."""
        from app.api.git import router
        assert router.prefix == "/api/v1/projects/{project_id}/git"


# =============================================================================
# Test Git Status Endpoint
# =============================================================================

class TestGetGitStatus:
    """Test GET /api/v1/projects/{project_id}/git/status endpoint."""

    def test_get_status_success(
        self, app, mock_database, mock_git_service, mock_settings,
        sample_project, sample_git_status, sample_repository
    ):
        """Should return git status for a project."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.get_status.return_value = sample_git_status
        mock_git_service.is_git_repo.return_value = True
        mock_database.get_git_repository_by_project.return_value = sample_repository

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.get("/api/v1/projects/test-project-id/git/status")

        assert response.status_code == 200
        data = response.json()
        assert data["is_git_repo"] is True
        assert data["current_branch"] == "main"
        assert data["is_clean"] is True
        assert data["repository_id"] == "repo-abc123"
        assert data["default_branch"] == "main"

    def test_get_status_project_not_found(self, app, mock_database, mock_settings):
        """Should return 404 for non-existent project."""
        mock_database.get_project.return_value = None

        with patch("app.api.git.database", mock_database):
            with patch("app.api.git.settings", mock_settings):
                with patch("app.api.git.get_api_user_from_request", return_value=None):
                    client = TestClient(app)
                    response = client.get("/api/v1/projects/nonexistent/git/status")

        assert response.status_code == 404
        assert "Project not found" in response.json()["detail"]

    def test_get_status_not_a_git_repo(
        self, app, mock_database, mock_git_service, mock_settings, sample_project
    ):
        """Should handle non-git repository."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.get_status.return_value = {
            "is_git_repo": False,
            "current_branch": None,
            "is_detached": False,
            "head_commit": None,
            "remote_url": None,
            "is_clean": True,
            "staged": [],
            "modified": [],
            "untracked": [],
            "ahead": 0,
            "behind": 0,
            "conflicts": []
        }
        mock_git_service.is_git_repo.return_value = False
        mock_database.get_git_repository_by_project.return_value = None

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.get("/api/v1/projects/test-project-id/git/status")

        assert response.status_code == 200
        data = response.json()
        assert data["is_git_repo"] is False
        assert data["repository_id"] is None

    def test_get_status_with_staged_files(
        self, app, mock_database, mock_git_service, mock_settings,
        sample_project, sample_repository
    ):
        """Should return staged files in status."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.get_status.return_value = {
            "is_git_repo": True,
            "current_branch": "main",
            "is_detached": False,
            "head_commit": "abc123",
            "remote_url": None,
            "is_clean": False,
            "staged": [
                {"file": "test.py", "status": "M"},
                {"file": "new_file.py", "status": "A"}
            ],
            "modified": ["other.py"],
            "untracked": ["untracked.txt"],
            "ahead": 1,
            "behind": 2,
            "conflicts": []
        }
        mock_git_service.is_git_repo.return_value = True
        mock_database.get_git_repository_by_project.return_value = sample_repository

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.get("/api/v1/projects/test-project-id/git/status")

        assert response.status_code == 200
        data = response.json()
        assert data["is_clean"] is False
        assert len(data["staged"]) == 2
        assert data["staged"][0]["file"] == "test.py"
        assert data["staged"][0]["status"] == "M"
        assert data["modified"] == ["other.py"]
        assert data["untracked"] == ["untracked.txt"]
        assert data["ahead"] == 1
        assert data["behind"] == 2

    def test_get_status_creates_repository_record(
        self, app, mock_database, mock_git_service, mock_settings,
        sample_project, sample_git_status
    ):
        """Should create repository record if it doesn't exist."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.get_status.return_value = sample_git_status
        mock_git_service.is_git_repo.return_value = True
        mock_git_service.get_remote_url.return_value = "https://github.com/test/repo.git"
        mock_git_service.get_default_branch.return_value = "main"
        mock_database.get_git_repository_by_project.return_value = None
        mock_database.create_git_repository.return_value = {
            "id": "repo-new123",
            "project_id": "test-project-id",
            "default_branch": "main",
            "github_repo_name": "test/repo",
            "last_synced_at": None
        }

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.get("/api/v1/projects/test-project-id/git/status")

        assert response.status_code == 200
        mock_database.create_git_repository.assert_called_once()

    def test_get_status_api_user_access_denied(
        self, app, mock_database, mock_settings, sample_project
    ):
        """Should deny access when API user tries to access different project."""
        mock_database.get_project.return_value = sample_project

        with patch("app.api.git.database", mock_database):
            with patch("app.api.git.settings", mock_settings):
                # Set API user with different project access
                with patch("app.api.git.get_api_user_from_request", return_value={
                    "id": "api-user-id",
                    "project_id": "different-project-id"
                }):
                    client = TestClient(app)
                    response = client.get("/api/v1/projects/test-project-id/git/status")

        assert response.status_code == 403
        assert "Access denied" in response.json()["detail"]


# =============================================================================
# Test List Branches Endpoint
# =============================================================================

class TestListBranches:
    """Test GET /api/v1/projects/{project_id}/git/branches endpoint."""

    def test_list_branches_success(
        self, app, mock_database, mock_git_service, mock_settings,
        sample_project, sample_branches
    ):
        """Should return list of branches."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = True
        mock_git_service.list_branches.return_value = sample_branches
        mock_git_service.get_current_branch.return_value = "main"

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.get("/api/v1/projects/test-project-id/git/branches")

        assert response.status_code == 200
        data = response.json()
        assert len(data["branches"]) == 3
        assert data["current_branch"] == "main"
        assert data["branches"][0]["name"] == "main"
        assert data["branches"][0]["is_current"] is True

    def test_list_branches_exclude_remote(
        self, app, mock_database, mock_git_service, mock_settings, sample_project
    ):
        """Should exclude remote branches when requested."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = True
        mock_git_service.list_branches.return_value = [
            {"name": "main", "is_current": True, "is_remote": False,
             "commit": "abc123", "commit_message": "test", "upstream": None,
             "ahead": 0, "behind": 0}
        ]
        mock_git_service.get_current_branch.return_value = "main"

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.get(
                            "/api/v1/projects/test-project-id/git/branches?include_remote=false"
                        )

        assert response.status_code == 200
        mock_git_service.list_branches.assert_called_with(
            str(mock_settings.workspace_dir / "test-project"), include_remote=False
        )

    def test_list_branches_not_git_repo(
        self, app, mock_database, mock_git_service, mock_settings, sample_project
    ):
        """Should return 400 for non-git repository."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = False

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.get("/api/v1/projects/test-project-id/git/branches")

        assert response.status_code == 400
        assert "not a git repository" in response.json()["detail"]

    def test_list_branches_project_not_found(self, app, mock_database, mock_settings):
        """Should return 404 for non-existent project."""
        mock_database.get_project.return_value = None

        with patch("app.api.git.database", mock_database):
            with patch("app.api.git.settings", mock_settings):
                with patch("app.api.git.get_api_user_from_request", return_value=None):
                    client = TestClient(app)
                    response = client.get("/api/v1/projects/nonexistent/git/branches")

        assert response.status_code == 404


# =============================================================================
# Test Create Branch Endpoint
# =============================================================================

class TestCreateBranch:
    """Test POST /api/v1/projects/{project_id}/git/branches endpoint."""

    def test_create_branch_success(
        self, app, mock_database, mock_git_service, mock_settings, sample_project
    ):
        """Should create a new branch."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = True
        mock_git_service.create_branch.return_value = True

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.post(
                            "/api/v1/projects/test-project-id/git/branches",
                            json={"name": "feature/new-branch"}
                        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "Created branch" in data["message"]

    def test_create_branch_with_start_point(
        self, app, mock_database, mock_git_service, mock_settings, sample_project
    ):
        """Should create branch from start point."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = True
        mock_git_service.create_branch.return_value = True

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.post(
                            "/api/v1/projects/test-project-id/git/branches",
                            json={"name": "feature/new-branch", "start_point": "develop"}
                        )

        assert response.status_code == 200
        mock_git_service.create_branch.assert_called_with(
            str(mock_settings.workspace_dir / "test-project"),
            "feature/new-branch",
            start_point="develop"
        )

    def test_create_branch_invalid_name_space(
        self, app, mock_database, mock_git_service, mock_settings, sample_project
    ):
        """Should reject branch name with space."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = True

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.post(
                            "/api/v1/projects/test-project-id/git/branches",
                            json={"name": "invalid branch name"}
                        )

        assert response.status_code == 400
        assert "Invalid branch name" in response.json()["detail"]

    def test_create_branch_invalid_name_special_chars(
        self, app, mock_database, mock_git_service, mock_settings, sample_project
    ):
        """Should reject branch names with special characters."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = True

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        for char in ["~", "^", ":", "\\", "?", "*", "["]:
                            response = client.post(
                                "/api/v1/projects/test-project-id/git/branches",
                                json={"name": f"invalid{char}branch"}
                            )
                            assert response.status_code == 400, f"Expected 400 for char '{char}'"

    def test_create_branch_invalid_name_double_dot(
        self, app, mock_database, mock_git_service, mock_settings, sample_project
    ):
        """Should reject branch name with double dots."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = True

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.post(
                            "/api/v1/projects/test-project-id/git/branches",
                            json={"name": "invalid..branch"}
                        )

        assert response.status_code == 400
        assert "Invalid branch name" in response.json()["detail"]

    def test_create_branch_failure(
        self, app, mock_database, mock_git_service, mock_settings, sample_project
    ):
        """Should return 400 when git fails to create branch."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = True
        mock_git_service.create_branch.return_value = False

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.post(
                            "/api/v1/projects/test-project-id/git/branches",
                            json={"name": "existing-branch"}
                        )

        assert response.status_code == 400
        assert "Failed to create branch" in response.json()["detail"]

    def test_create_branch_not_git_repo(
        self, app, mock_database, mock_git_service, mock_settings, sample_project
    ):
        """Should return 400 for non-git repository."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = False

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.post(
                            "/api/v1/projects/test-project-id/git/branches",
                            json={"name": "new-branch"}
                        )

        assert response.status_code == 400
        assert "not a git repository" in response.json()["detail"]


# =============================================================================
# Test Delete Branch Endpoint
# =============================================================================

class TestDeleteBranch:
    """Test DELETE /api/v1/projects/{project_id}/git/branches/{branch_name} endpoint."""

    def test_delete_branch_success(
        self, app, mock_database, mock_git_service, mock_settings, sample_project
    ):
        """Should delete a branch."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = True
        mock_git_service.get_current_branch.return_value = "main"
        mock_git_service.delete_branch.return_value = True

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.delete(
                            "/api/v1/projects/test-project-id/git/branches/feature/test"
                        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "Deleted branch" in data["message"]

    def test_delete_branch_force(
        self, app, mock_database, mock_git_service, mock_settings, sample_project
    ):
        """Should force delete unmerged branch."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = True
        mock_git_service.get_current_branch.return_value = "main"
        mock_git_service.delete_branch.return_value = True

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.delete(
                            "/api/v1/projects/test-project-id/git/branches/unmerged-branch?force=true"
                        )

        assert response.status_code == 200
        mock_git_service.delete_branch.assert_called_with(
            str(mock_settings.workspace_dir / "test-project"), "unmerged-branch", force=True
        )

    def test_delete_current_branch(
        self, app, mock_database, mock_git_service, mock_settings, sample_project
    ):
        """Should not allow deleting current branch."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = True
        mock_git_service.get_current_branch.return_value = "main"

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.delete(
                            "/api/v1/projects/test-project-id/git/branches/main"
                        )

        assert response.status_code == 400
        assert "currently checked out" in response.json()["detail"]

    def test_delete_branch_failure(
        self, app, mock_database, mock_git_service, mock_settings, sample_project
    ):
        """Should return 400 when git fails to delete branch."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = True
        mock_git_service.get_current_branch.return_value = "main"
        mock_git_service.delete_branch.return_value = False

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.delete(
                            "/api/v1/projects/test-project-id/git/branches/nonexistent"
                        )

        assert response.status_code == 400
        assert "Failed to delete branch" in response.json()["detail"]

    def test_delete_branch_not_git_repo(
        self, app, mock_database, mock_git_service, mock_settings, sample_project
    ):
        """Should return 400 for non-git repository."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = False

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.delete(
                            "/api/v1/projects/test-project-id/git/branches/some-branch"
                        )

        assert response.status_code == 400


# =============================================================================
# Test Checkout Endpoint
# =============================================================================

class TestCheckout:
    """Test POST /api/v1/projects/{project_id}/git/checkout endpoint."""

    def test_checkout_success(
        self, app, mock_database, mock_git_service, mock_settings, sample_project
    ):
        """Should checkout a branch."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = True
        mock_git_service.get_status.return_value = {
            "is_clean": True, "staged": [], "modified": [], "untracked": [], "conflicts": []
        }
        mock_git_service.checkout.return_value = True

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.post(
                            "/api/v1/projects/test-project-id/git/checkout",
                            json={"ref": "feature/test"}
                        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "Checked out" in data["message"]

    def test_checkout_with_uncommitted_changes(
        self, app, mock_database, mock_git_service, mock_settings, sample_project
    ):
        """Should prevent checkout with uncommitted changes."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = True
        mock_git_service.get_status.return_value = {
            "is_clean": False,
            "staged": [{"file": "test.py", "status": "M"}],
            "modified": [],
            "untracked": [],
            "conflicts": []
        }

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.post(
                            "/api/v1/projects/test-project-id/git/checkout",
                            json={"ref": "feature/test"}
                        )

        assert response.status_code == 409
        assert "uncommitted changes" in response.json()["detail"]

    def test_checkout_nonexistent_ref(
        self, app, mock_database, mock_git_service, mock_settings, sample_project
    ):
        """Should return 400 for nonexistent ref."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = True
        mock_git_service.get_status.return_value = {"is_clean": True}
        mock_git_service.checkout.return_value = False

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.post(
                            "/api/v1/projects/test-project-id/git/checkout",
                            json={"ref": "nonexistent-branch"}
                        )

        assert response.status_code == 400
        assert "Failed to checkout" in response.json()["detail"]

    def test_checkout_not_git_repo(
        self, app, mock_database, mock_git_service, mock_settings, sample_project
    ):
        """Should return 400 for non-git repository."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = False

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.post(
                            "/api/v1/projects/test-project-id/git/checkout",
                            json={"ref": "main"}
                        )

        assert response.status_code == 400


# =============================================================================
# Test Commit Graph Endpoint
# =============================================================================

class TestCommitGraph:
    """Test GET /api/v1/projects/{project_id}/git/graph endpoint."""

    def test_get_commit_graph_success(
        self, app, mock_database, mock_git_service, mock_settings,
        sample_project, sample_commits
    ):
        """Should return commit graph."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = True
        mock_git_service.get_commit_graph.return_value = sample_commits

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.get("/api/v1/projects/test-project-id/git/graph")

        assert response.status_code == 200
        data = response.json()
        assert len(data["commits"]) == 2
        assert data["total"] == 2
        assert data["commits"][0]["sha"] == "abc123def456789012345678901234567890abcd"

    def test_get_commit_graph_with_limit(
        self, app, mock_database, mock_git_service, mock_settings,
        sample_project, sample_commits
    ):
        """Should respect limit parameter."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = True
        mock_git_service.get_commit_graph.return_value = sample_commits[:1]

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.get(
                            "/api/v1/projects/test-project-id/git/graph?limit=1"
                        )

        assert response.status_code == 200
        mock_git_service.get_commit_graph.assert_called_with(
            str(mock_settings.workspace_dir / "test-project"), limit=1, branch=None
        )

    def test_get_commit_graph_with_branch(
        self, app, mock_database, mock_git_service, mock_settings,
        sample_project, sample_commits
    ):
        """Should filter by branch."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = True
        mock_git_service.get_commit_graph.return_value = sample_commits

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.get(
                            "/api/v1/projects/test-project-id/git/graph?branch=main"
                        )

        assert response.status_code == 200
        mock_git_service.get_commit_graph.assert_called_with(
            str(mock_settings.workspace_dir / "test-project"), limit=100, branch="main"
        )

    def test_get_commit_graph_limit_capped(
        self, app, mock_database, mock_git_service, mock_settings,
        sample_project, sample_commits
    ):
        """Should cap limit to 500."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = True
        mock_git_service.get_commit_graph.return_value = sample_commits

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.get(
                            "/api/v1/projects/test-project-id/git/graph?limit=1000"
                        )

        assert response.status_code == 200
        mock_git_service.get_commit_graph.assert_called_with(
            str(mock_settings.workspace_dir / "test-project"), limit=500, branch=None
        )

    def test_get_commit_graph_not_git_repo(
        self, app, mock_database, mock_git_service, mock_settings, sample_project
    ):
        """Should return 400 for non-git repository."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = False

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.get("/api/v1/projects/test-project-id/git/graph")

        assert response.status_code == 400


# =============================================================================
# Test Fetch Endpoint
# =============================================================================

class TestFetch:
    """Test POST /api/v1/projects/{project_id}/git/fetch endpoint."""

    def test_fetch_success(
        self, app, mock_database, mock_git_service, mock_settings,
        sample_project, sample_repository
    ):
        """Should fetch from remote."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = True
        mock_git_service.fetch.return_value = True
        mock_database.get_git_repository_by_project.return_value = sample_repository

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.post("/api/v1/projects/test-project-id/git/fetch")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "Fetched from 'origin'" in data["message"]

    def test_fetch_custom_remote(
        self, app, mock_database, mock_git_service, mock_settings, sample_project
    ):
        """Should fetch from custom remote."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = True
        mock_git_service.fetch.return_value = True
        mock_database.get_git_repository_by_project.return_value = None

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.post(
                            "/api/v1/projects/test-project-id/git/fetch?remote=upstream"
                        )

        assert response.status_code == 200
        mock_git_service.fetch.assert_called_with(
            str(mock_settings.workspace_dir / "test-project"), remote="upstream"
        )

    def test_fetch_failure(
        self, app, mock_database, mock_git_service, mock_settings, sample_project
    ):
        """Should return 502 on fetch failure."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = True
        mock_git_service.fetch.return_value = False

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.post("/api/v1/projects/test-project-id/git/fetch")

        assert response.status_code == 502
        assert "Failed to fetch" in response.json()["detail"]

    def test_fetch_updates_synced_timestamp(
        self, app, mock_database, mock_git_service, mock_settings,
        sample_project, sample_repository
    ):
        """Should update last_synced_at on successful fetch."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = True
        mock_git_service.fetch.return_value = True
        mock_database.get_git_repository_by_project.return_value = sample_repository

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.post("/api/v1/projects/test-project-id/git/fetch")

        assert response.status_code == 200
        mock_database.update_git_repository_synced.assert_called_with("repo-abc123")

    def test_fetch_not_git_repo(
        self, app, mock_database, mock_git_service, mock_settings, sample_project
    ):
        """Should return 400 for non-git repository."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = False

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.post("/api/v1/projects/test-project-id/git/fetch")

        assert response.status_code == 400


# =============================================================================
# Test List Worktrees Endpoint
# =============================================================================

class TestListWorktrees:
    """Test GET /api/v1/projects/{project_id}/git/worktrees endpoint."""

    def test_list_worktrees_success(
        self, app, mock_database, mock_git_service,
        mock_worktree_manager, mock_settings, sample_project, sample_worktree
    ):
        """Should return list of worktrees."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = True
        mock_worktree_manager.get_worktrees_for_project.return_value = [sample_worktree]

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.worktree_manager", mock_worktree_manager):
                with patch("app.api.git.database", mock_database):
                    with patch("app.api.git.settings", mock_settings):
                        with patch("app.api.git.get_api_user_from_request", return_value=None):
                            client = TestClient(app)
                            response = client.get("/api/v1/projects/test-project-id/git/worktrees")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert len(data["worktrees"]) == 1
        assert data["worktrees"][0]["id"] == "wt-abc123"
        assert data["worktrees"][0]["branch_name"] == "feature/test"

    def test_list_worktrees_empty(
        self, app, mock_database, mock_git_service, mock_worktree_manager,
        mock_settings, sample_project
    ):
        """Should return empty list when no worktrees."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = True
        mock_worktree_manager.get_worktrees_for_project.return_value = []

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.worktree_manager", mock_worktree_manager):
                with patch("app.api.git.database", mock_database):
                    with patch("app.api.git.settings", mock_settings):
                        with patch("app.api.git.get_api_user_from_request", return_value=None):
                            client = TestClient(app)
                            response = client.get("/api/v1/projects/test-project-id/git/worktrees")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0
        assert data["worktrees"] == []

    def test_list_worktrees_with_session(
        self, app, mock_database, mock_git_service, mock_worktree_manager,
        mock_settings, sample_project, sample_worktree
    ):
        """Should include session info in worktree list."""
        worktree_with_session = sample_worktree.copy()
        worktree_with_session["sessions"] = [
            {
                "id": "ses-abc123",
                "title": "Test Session",
                "status": "active",
                "updated_at": "2024-01-15T10:30:00Z"
            }
        ]
        worktree_with_session["session_count"] = 1
        worktree_with_session["active_session"] = {
            "id": "ses-abc123",
            "title": "Test Session",
            "status": "active",
            "updated_at": "2024-01-15T10:30:00Z"
        }
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = True
        mock_worktree_manager.get_worktrees_for_project.return_value = [worktree_with_session]

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.worktree_manager", mock_worktree_manager):
                with patch("app.api.git.database", mock_database):
                    with patch("app.api.git.settings", mock_settings):
                        with patch("app.api.git.get_api_user_from_request", return_value=None):
                            client = TestClient(app)
                            response = client.get("/api/v1/projects/test-project-id/git/worktrees")

        assert response.status_code == 200
        data = response.json()
        assert data["worktrees"][0]["session_count"] == 1
        assert data["worktrees"][0]["active_session"]["id"] == "ses-abc123"

    def test_list_worktrees_not_git_repo(
        self, app, mock_database, mock_git_service, mock_settings, sample_project
    ):
        """Should return 400 for non-git repository."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = False

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.get("/api/v1/projects/test-project-id/git/worktrees")

        assert response.status_code == 400


# =============================================================================
# Test Create Worktree Endpoint
# =============================================================================

class TestCreateWorktree:
    """Test POST /api/v1/projects/{project_id}/git/worktrees endpoint."""

    def test_create_worktree_standalone_success(
        self, app, mock_database, mock_git_service, mock_worktree_manager,
        mock_settings, sample_project, sample_worktree
    ):
        """Should create a standalone worktree."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = True
        mock_worktree_manager.create_worktree.return_value = sample_worktree
        mock_worktree_manager.get_worktree_details.return_value = sample_worktree

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.worktree_manager", mock_worktree_manager):
                with patch("app.api.git.database", mock_database):
                    with patch("app.api.git.settings", mock_settings):
                        with patch("app.api.git.get_api_user_from_request", return_value=None):
                            client = TestClient(app)
                            response = client.post(
                                "/api/v1/projects/test-project-id/git/worktrees",
                                json={"branch_name": "feature/test"}
                            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "Created worktree" in data["message"]
        assert data["worktree"]["branch_name"] == "feature/test"

    def test_create_worktree_with_new_branch(
        self, app, mock_database, mock_git_service, mock_worktree_manager,
        mock_settings, sample_project, sample_worktree
    ):
        """Should create worktree with new branch."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = True
        mock_worktree_manager.create_worktree.return_value = sample_worktree
        mock_worktree_manager.get_worktree_details.return_value = sample_worktree

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.worktree_manager", mock_worktree_manager):
                with patch("app.api.git.database", mock_database):
                    with patch("app.api.git.settings", mock_settings):
                        with patch("app.api.git.get_api_user_from_request", return_value=None):
                            client = TestClient(app)
                            response = client.post(
                                "/api/v1/projects/test-project-id/git/worktrees",
                                json={
                                    "branch_name": "feature/new",
                                    "create_new_branch": True,
                                    "base_branch": "main"
                                }
                            )

        assert response.status_code == 200
        mock_worktree_manager.create_worktree.assert_called_with(
            project_id="test-project-id",
            branch_name="feature/new",
            create_new_branch=True,
            base_branch="main"
        )

    def test_create_worktree_with_session(
        self, app, mock_database, mock_git_service, mock_worktree_manager,
        mock_settings, sample_project, sample_worktree
    ):
        """Should create worktree with associated session."""
        session = {
            "id": "ses-abc123",
            "title": "Branch: feature/test",
            "status": "active"
        }
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = True
        mock_worktree_manager.create_worktree_session.return_value = (sample_worktree, session)
        mock_worktree_manager.get_worktree_details.return_value = sample_worktree

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.worktree_manager", mock_worktree_manager):
                with patch("app.api.git.database", mock_database):
                    with patch("app.api.git.settings", mock_settings):
                        with patch("app.api.git.get_api_user_from_request", return_value=None):
                            client = TestClient(app)
                            response = client.post(
                                "/api/v1/projects/test-project-id/git/worktrees",
                                json={"branch_name": "feature/test", "profile_id": "profile-123"}
                            )

        assert response.status_code == 200
        data = response.json()
        assert data["session"]["id"] == "ses-abc123"

    def test_create_worktree_invalid_branch_name(
        self, app, mock_database, mock_git_service, mock_settings, sample_project
    ):
        """Should reject invalid branch names."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = True

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.post(
                            "/api/v1/projects/test-project-id/git/worktrees",
                            json={"branch_name": "invalid branch name"}
                        )

        assert response.status_code == 400
        assert "Invalid branch name" in response.json()["detail"]

    def test_create_worktree_with_session_failure(
        self, app, mock_database, mock_git_service, mock_worktree_manager,
        mock_settings, sample_project
    ):
        """Should return 400 when worktree with session creation fails."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = True
        # Return None for worktree to simulate failure
        mock_worktree_manager.create_worktree_session.return_value = (None, None)

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.worktree_manager", mock_worktree_manager):
                with patch("app.api.git.database", mock_database):
                    with patch("app.api.git.settings", mock_settings):
                        with patch("app.api.git.get_api_user_from_request", return_value=None):
                            client = TestClient(app)
                            response = client.post(
                                "/api/v1/projects/test-project-id/git/worktrees",
                                json={"branch_name": "feature/test", "profile_id": "profile-123"}
                            )

        assert response.status_code == 400
        assert "Failed to create worktree" in response.json()["detail"]

    def test_create_worktree_failure(
        self, app, mock_database, mock_git_service, mock_worktree_manager,
        mock_settings, sample_project
    ):
        """Should return 400 on creation failure."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = True
        mock_worktree_manager.create_worktree.return_value = None

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.worktree_manager", mock_worktree_manager):
                with patch("app.api.git.database", mock_database):
                    with patch("app.api.git.settings", mock_settings):
                        with patch("app.api.git.get_api_user_from_request", return_value=None):
                            client = TestClient(app)
                            response = client.post(
                                "/api/v1/projects/test-project-id/git/worktrees",
                                json={"branch_name": "feature/test"}
                            )

        assert response.status_code == 400
        assert "Failed to create worktree" in response.json()["detail"]

    def test_create_worktree_not_git_repo(
        self, app, mock_database, mock_git_service, mock_settings, sample_project
    ):
        """Should return 400 for non-git repository."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = False

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.post(
                            "/api/v1/projects/test-project-id/git/worktrees",
                            json={"branch_name": "feature/test"}
                        )

        assert response.status_code == 400


# =============================================================================
# Test Get Worktree Endpoint
# =============================================================================

class TestGetWorktree:
    """Test GET /api/v1/projects/{project_id}/git/worktrees/{worktree_id} endpoint."""

    def test_get_worktree_success(
        self, app, mock_database, mock_git_service, mock_worktree_manager,
        mock_settings, sample_project, sample_repository, sample_worktree
    ):
        """Should return worktree details."""
        mock_database.get_project.return_value = sample_project
        mock_worktree_manager.get_worktree_details.return_value = sample_worktree
        mock_database.get_git_repository_by_project.return_value = sample_repository

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.worktree_manager", mock_worktree_manager):
                with patch("app.api.git.database", mock_database):
                    with patch("app.api.git.settings", mock_settings):
                        with patch("app.api.git.get_api_user_from_request", return_value=None):
                            client = TestClient(app)
                            response = client.get(
                                "/api/v1/projects/test-project-id/git/worktrees/wt-abc123"
                            )

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "wt-abc123"
        assert data["branch_name"] == "feature/test"

    def test_get_worktree_not_found(
        self, app, mock_database, mock_worktree_manager, mock_settings, sample_project
    ):
        """Should return 404 for non-existent worktree."""
        mock_database.get_project.return_value = sample_project
        mock_worktree_manager.get_worktree_details.return_value = None

        with patch("app.api.git.worktree_manager", mock_worktree_manager):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.get(
                            "/api/v1/projects/test-project-id/git/worktrees/nonexistent"
                        )

        assert response.status_code == 404

    def test_get_worktree_wrong_project(
        self, app, mock_database, mock_worktree_manager, mock_settings,
        sample_project, sample_worktree
    ):
        """Should return 404 if worktree belongs to different project."""
        mock_database.get_project.return_value = sample_project
        mock_worktree_manager.get_worktree_details.return_value = sample_worktree
        mock_database.get_git_repository_by_project.return_value = {
            "id": "repo-different",
            "project_id": "different-project"
        }

        with patch("app.api.git.worktree_manager", mock_worktree_manager):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.get(
                            "/api/v1/projects/test-project-id/git/worktrees/wt-abc123"
                        )

        assert response.status_code == 404

    def test_get_worktree_with_git_status(
        self, app, mock_database, mock_worktree_manager, mock_settings,
        sample_project, sample_repository, sample_worktree
    ):
        """Should include git status in worktree details."""
        worktree_with_status = sample_worktree.copy()
        worktree_with_status["git_status"] = {
            "is_clean": False,
            "current_branch": "feature/test",
            "modified": ["test.py"],
            "staged": [{"file": "other.py", "status": "M"}],
            "untracked": []
        }
        mock_database.get_project.return_value = sample_project
        mock_worktree_manager.get_worktree_details.return_value = worktree_with_status
        mock_database.get_git_repository_by_project.return_value = sample_repository

        with patch("app.api.git.worktree_manager", mock_worktree_manager):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.get(
                            "/api/v1/projects/test-project-id/git/worktrees/wt-abc123"
                        )

        assert response.status_code == 200
        data = response.json()
        assert data["git_status"]["is_clean"] is False
        assert "test.py" in data["git_status"]["modified"]


# =============================================================================
# Test Delete Worktree Endpoint
# =============================================================================

class TestDeleteWorktree:
    """Test DELETE /api/v1/projects/{project_id}/git/worktrees/{worktree_id} endpoint."""

    def test_delete_worktree_success(
        self, app, mock_database, mock_worktree_manager, mock_settings,
        sample_project, sample_repository, sample_worktree
    ):
        """Should delete a worktree."""
        mock_database.get_project.return_value = sample_project
        mock_database.get_worktree.return_value = sample_worktree
        mock_database.get_git_repository_by_project.return_value = sample_repository
        mock_worktree_manager.cleanup_worktree.return_value = True

        with patch("app.api.git.worktree_manager", mock_worktree_manager):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.delete(
                            "/api/v1/projects/test-project-id/git/worktrees/wt-abc123"
                        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "Removed worktree" in data["message"]
        assert "(branch kept)" in data["message"]

    def test_delete_worktree_with_branch(
        self, app, mock_database, mock_worktree_manager, mock_settings,
        sample_project, sample_repository, sample_worktree
    ):
        """Should delete worktree and branch."""
        mock_database.get_project.return_value = sample_project
        mock_database.get_worktree.return_value = sample_worktree
        mock_database.get_git_repository_by_project.return_value = sample_repository
        mock_worktree_manager.cleanup_worktree.return_value = True

        with patch("app.api.git.worktree_manager", mock_worktree_manager):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.delete(
                            "/api/v1/projects/test-project-id/git/worktrees/wt-abc123?keep_branch=false"
                        )

        assert response.status_code == 200
        data = response.json()
        assert "(branch deleted)" in data["message"]
        mock_worktree_manager.cleanup_worktree.assert_called_with(
            "wt-abc123", keep_branch=False
        )

    def test_delete_worktree_not_found(
        self, app, mock_database, mock_settings, sample_project
    ):
        """Should return 404 for non-existent worktree."""
        mock_database.get_project.return_value = sample_project
        mock_database.get_worktree.return_value = None

        with patch("app.api.git.database", mock_database):
            with patch("app.api.git.settings", mock_settings):
                with patch("app.api.git.get_api_user_from_request", return_value=None):
                    client = TestClient(app)
                    response = client.delete(
                        "/api/v1/projects/test-project-id/git/worktrees/nonexistent"
                    )

        assert response.status_code == 404

    def test_delete_worktree_wrong_project(
        self, app, mock_database, mock_settings, sample_project, sample_worktree
    ):
        """Should return 404 if worktree belongs to different project."""
        mock_database.get_project.return_value = sample_project
        mock_database.get_worktree.return_value = sample_worktree
        mock_database.get_git_repository_by_project.return_value = {
            "id": "repo-different"
        }

        with patch("app.api.git.database", mock_database):
            with patch("app.api.git.settings", mock_settings):
                with patch("app.api.git.get_api_user_from_request", return_value=None):
                    client = TestClient(app)
                    response = client.delete(
                        "/api/v1/projects/test-project-id/git/worktrees/wt-abc123"
                    )

        assert response.status_code == 404

    def test_delete_worktree_failure(
        self, app, mock_database, mock_worktree_manager, mock_settings,
        sample_project, sample_repository, sample_worktree
    ):
        """Should return 500 on deletion failure."""
        mock_database.get_project.return_value = sample_project
        mock_database.get_worktree.return_value = sample_worktree
        mock_database.get_git_repository_by_project.return_value = sample_repository
        mock_worktree_manager.cleanup_worktree.return_value = False

        with patch("app.api.git.worktree_manager", mock_worktree_manager):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.delete(
                            "/api/v1/projects/test-project-id/git/worktrees/wt-abc123"
                        )

        assert response.status_code == 500


# =============================================================================
# Test Sync Worktrees Endpoint
# =============================================================================

class TestSyncWorktrees:
    """Test POST /api/v1/projects/{project_id}/git/worktrees/sync endpoint."""

    def test_sync_worktrees_success(
        self, app, mock_database, mock_git_service, mock_worktree_manager,
        mock_settings, sample_project
    ):
        """Should sync worktrees."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = True
        mock_worktree_manager.sync_worktrees.return_value = {
            "synced": 3,
            "orphaned": 1,
            "cleaned_up": 2,
            "errors": []
        }

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.worktree_manager", mock_worktree_manager):
                with patch("app.api.git.database", mock_database):
                    with patch("app.api.git.settings", mock_settings):
                        with patch("app.api.git.get_api_user_from_request", return_value=None):
                            client = TestClient(app)
                            response = client.post(
                                "/api/v1/projects/test-project-id/git/worktrees/sync"
                            )

        assert response.status_code == 200
        data = response.json()
        assert data["synced"] == 3
        assert data["orphaned"] == 1
        assert data["cleaned_up"] == 2
        assert data["errors"] == []

    def test_sync_worktrees_with_errors(
        self, app, mock_database, mock_git_service, mock_worktree_manager,
        mock_settings, sample_project
    ):
        """Should return sync errors."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = True
        mock_worktree_manager.sync_worktrees.return_value = {
            "synced": 1,
            "orphaned": 0,
            "cleaned_up": 0,
            "errors": ["Failed to sync wt-abc123"]
        }

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.worktree_manager", mock_worktree_manager):
                with patch("app.api.git.database", mock_database):
                    with patch("app.api.git.settings", mock_settings):
                        with patch("app.api.git.get_api_user_from_request", return_value=None):
                            client = TestClient(app)
                            response = client.post(
                                "/api/v1/projects/test-project-id/git/worktrees/sync"
                            )

        assert response.status_code == 200
        data = response.json()
        assert len(data["errors"]) == 1

    def test_sync_worktrees_not_git_repo(
        self, app, mock_database, mock_git_service, mock_settings, sample_project
    ):
        """Should return 400 for non-git repository."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = False

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.post(
                            "/api/v1/projects/test-project-id/git/worktrees/sync"
                        )

        assert response.status_code == 400


# =============================================================================
# Test Helper Functions
# =============================================================================

class TestHelperFunctions:
    """Test helper functions in git module."""

    def test_ensure_git_repository_record_creates_record(self):
        """Should create repository record if it doesn't exist."""
        from app.api.git import ensure_git_repository_record

        mock_git = MagicMock()
        mock_db = MagicMock()
        mock_settings_obj = MagicMock()
        mock_settings_obj.workspace_dir = Path("/test/workspace")

        mock_git.is_git_repo.return_value = True
        mock_db.get_git_repository_by_project.return_value = None
        mock_git.get_remote_url.return_value = "https://github.com/owner/repo.git"
        mock_git.get_default_branch.return_value = "main"
        mock_db.create_git_repository.return_value = {
            "id": "repo-new123",
            "project_id": "test-project",
            "default_branch": "main"
        }

        with patch("app.api.git.git_service", mock_git):
            with patch("app.api.git.database", mock_db):
                with patch("app.api.git.settings", mock_settings_obj):
                    result = ensure_git_repository_record("test-project", "/test/path")

        assert result is not None
        mock_db.create_git_repository.assert_called_once()
        call_kwargs = mock_db.create_git_repository.call_args.kwargs
        assert call_kwargs["github_repo_name"] == "owner/repo"

    def test_ensure_git_repository_record_returns_existing(self):
        """Should return existing repository record."""
        from app.api.git import ensure_git_repository_record

        mock_git = MagicMock()
        mock_db = MagicMock()
        mock_settings_obj = MagicMock()

        existing_repo = {"id": "repo-existing", "project_id": "test-project"}
        mock_git.is_git_repo.return_value = True
        mock_db.get_git_repository_by_project.return_value = existing_repo

        with patch("app.api.git.git_service", mock_git):
            with patch("app.api.git.database", mock_db):
                with patch("app.api.git.settings", mock_settings_obj):
                    result = ensure_git_repository_record("test-project", "/test/path")

        assert result == existing_repo
        mock_db.create_git_repository.assert_not_called()

    def test_ensure_git_repository_record_not_git_repo(self):
        """Should return None for non-git repository."""
        from app.api.git import ensure_git_repository_record

        mock_git = MagicMock()
        mock_db = MagicMock()
        mock_settings_obj = MagicMock()

        mock_git.is_git_repo.return_value = False

        with patch("app.api.git.git_service", mock_git):
            with patch("app.api.git.database", mock_db):
                with patch("app.api.git.settings", mock_settings_obj):
                    result = ensure_git_repository_record("test-project", "/test/path")

        assert result is None

    def test_ensure_git_repository_record_ssh_url(self):
        """Should parse SSH GitHub URL."""
        from app.api.git import ensure_git_repository_record

        mock_git = MagicMock()
        mock_db = MagicMock()
        mock_settings_obj = MagicMock()

        mock_git.is_git_repo.return_value = True
        mock_db.get_git_repository_by_project.return_value = None
        mock_git.get_remote_url.return_value = "git@github.com:owner/repo.git"
        mock_git.get_default_branch.return_value = "main"
        mock_db.create_git_repository.return_value = {"id": "repo-new"}

        with patch("app.api.git.git_service", mock_git):
            with patch("app.api.git.database", mock_db):
                with patch("app.api.git.settings", mock_settings_obj):
                    ensure_git_repository_record("test-project", "/test/path")

        call_kwargs = mock_db.create_git_repository.call_args.kwargs
        assert call_kwargs["github_repo_name"] == "owner/repo"

    def test_ensure_git_repository_record_malformed_url(self):
        """Should handle malformed remote URL without crashing."""
        from app.api.git import ensure_git_repository_record

        mock_git = MagicMock()
        mock_db = MagicMock()
        mock_settings_obj = MagicMock()

        mock_git.is_git_repo.return_value = True
        mock_db.get_git_repository_by_project.return_value = None
        # Malformed URL that would cause parsing issues
        mock_git.get_remote_url.return_value = "git@"  # Incomplete URL
        mock_git.get_default_branch.return_value = "main"
        mock_db.create_git_repository.return_value = {"id": "repo-new"}

        with patch("app.api.git.git_service", mock_git):
            with patch("app.api.git.database", mock_db):
                with patch("app.api.git.settings", mock_settings_obj):
                    result = ensure_git_repository_record("test-project", "/test/path")

        # Should still create the repo, just without github_repo_name
        assert result is not None
        mock_db.create_git_repository.assert_called_once()

    def test_ensure_git_repository_record_non_github_url(self):
        """Should handle non-GitHub remote URL."""
        from app.api.git import ensure_git_repository_record

        mock_git = MagicMock()
        mock_db = MagicMock()
        mock_settings_obj = MagicMock()

        mock_git.is_git_repo.return_value = True
        mock_db.get_git_repository_by_project.return_value = None
        # Non-GitHub URL
        mock_git.get_remote_url.return_value = "https://gitlab.com/owner/repo.git"
        mock_git.get_default_branch.return_value = "main"
        mock_db.create_git_repository.return_value = {"id": "repo-new"}

        with patch("app.api.git.git_service", mock_git):
            with patch("app.api.git.database", mock_db):
                with patch("app.api.git.settings", mock_settings_obj):
                    result = ensure_git_repository_record("test-project", "/test/path")

        # Should create repo, github_repo_name stays None
        assert result is not None
        call_kwargs = mock_db.create_git_repository.call_args.kwargs
        assert call_kwargs["github_repo_name"] is None


# =============================================================================
# Test Worktree Conversion
# =============================================================================

class TestWorktreeConversion:
    """Test _convert_worktree_to_info function."""

    def test_convert_worktree_basic(self):
        """Should convert basic worktree dict to WorktreeInfo."""
        from app.api.git import _convert_worktree_to_info

        worktree_dict = {
            "id": "wt-123",
            "repository_id": "repo-456",
            "branch_name": "feature/test",
            "worktree_path": ".worktrees/project/feature-test",
            "status": "active",
            "created_at": "2024-01-15T10:30:00Z"
        }

        result = _convert_worktree_to_info(worktree_dict)

        assert result.id == "wt-123"
        assert result.repository_id == "repo-456"
        assert result.branch_name == "feature/test"
        assert result.status == "active"

    def test_convert_worktree_with_sessions(self):
        """Should convert worktree with sessions list."""
        from app.api.git import _convert_worktree_to_info

        worktree_dict = {
            "id": "wt-123",
            "repository_id": "repo-456",
            "branch_name": "feature/test",
            "worktree_path": ".worktrees/project/feature-test",
            "status": "active",
            "created_at": "2024-01-15T10:30:00Z",
            "sessions": [
                {
                    "id": "ses-1",
                    "title": "Session 1",
                    "status": "active",
                    "updated_at": "2024-01-15T11:00:00Z"
                }
            ],
            "session_count": 1,
            "active_session": {
                "id": "ses-1",
                "title": "Session 1",
                "status": "active",
                "updated_at": "2024-01-15T11:00:00Z"
            }
        }

        result = _convert_worktree_to_info(worktree_dict)

        assert len(result.sessions) == 1
        assert result.sessions[0].id == "ses-1"
        assert result.session_count == 1
        assert result.active_session.id == "ses-1"

    def test_convert_worktree_with_git_status(self):
        """Should convert worktree with git status."""
        from app.api.git import _convert_worktree_to_info

        worktree_dict = {
            "id": "wt-123",
            "repository_id": "repo-456",
            "branch_name": "feature/test",
            "worktree_path": ".worktrees/project/feature-test",
            "status": "active",
            "created_at": "2024-01-15T10:30:00Z",
            "git_status": {
                "is_clean": False,
                "current_branch": "feature/test",
                "modified": ["file.py"],
                "staged": [{"file": "staged.py", "status": "M"}],
                "untracked": ["new.txt"]
            }
        }

        result = _convert_worktree_to_info(worktree_dict)

        assert result.git_status is not None
        assert result.git_status.is_clean is False
        assert result.git_status.modified == ["file.py"]
        assert len(result.git_status.staged) == 1
        assert result.git_status.staged[0].file == "staged.py"

    def test_convert_worktree_legacy_session(self):
        """Should handle legacy single session field."""
        from app.api.git import _convert_worktree_to_info

        worktree_dict = {
            "id": "wt-123",
            "repository_id": "repo-456",
            "branch_name": "feature/test",
            "worktree_path": ".worktrees/project/feature-test",
            "status": "active",
            "created_at": "2024-01-15T10:30:00Z",
            "session": {
                "id": "ses-legacy",
                "title": "Legacy Session",
                "status": "active",
                "updated_at": "2024-01-15T10:30:00Z"
            }
        }

        result = _convert_worktree_to_info(worktree_dict)

        assert result.session is not None
        assert result.session.id == "ses-legacy"


# =============================================================================
# Test Request Validation
# =============================================================================

class TestRequestValidation:
    """Test request body validation."""

    def test_create_branch_empty_name(
        self, app, mock_database, mock_git_service, mock_settings, sample_project
    ):
        """Should reject empty branch name."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = True

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.post(
                            "/api/v1/projects/test-project-id/git/branches",
                            json={"name": ""}
                        )

        assert response.status_code == 422  # Pydantic validation error

    def test_checkout_empty_ref(
        self, app, mock_database, mock_git_service, mock_settings, sample_project
    ):
        """Should reject empty ref."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = True

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.post(
                            "/api/v1/projects/test-project-id/git/checkout",
                            json={"ref": ""}
                        )

        assert response.status_code == 422

    def test_create_worktree_empty_branch_name(
        self, app, mock_database, mock_git_service, mock_settings, sample_project
    ):
        """Should reject empty branch name for worktree."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = True

        with patch("app.api.git.git_service", mock_git_service):
            with patch("app.api.git.database", mock_database):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.post(
                            "/api/v1/projects/test-project-id/git/worktrees",
                            json={"branch_name": ""}
                        )

        assert response.status_code == 422
