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
from contextlib import ExitStack
from unittest.mock import patch, MagicMock, AsyncMock
from pathlib import Path
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.git import router, require_auth


def _enter_git_patches(
    stack,
    mock_git_service=None,
    mock_worktree_manager=None,
    mock_database=None,
    mock_settings=None,
    mock_api_user_return=...,
):
    """Push standard git-dependency patches onto an ``ExitStack``.

    After splitting git.py into sub-modules, shared dependencies must be
    patched in every module that imports them.
    """
    if mock_git_service is not None:
        for m in ("app.api.git", "app.api.git_worktrees", "app.api.git_ai"):
            stack.enter_context(patch(f"{m}.git_service", mock_git_service))
    if mock_worktree_manager is not None:
        for m in ("app.api.git", "app.api.git_worktrees"):
            stack.enter_context(patch(f"{m}.worktree_manager", mock_worktree_manager))
    if mock_database is not None:
        for m in ("app.api.git", "app.api.git_helpers", "app.api.git_worktrees"):
            stack.enter_context(patch(f"{m}.database", mock_database))
    if mock_settings is not None:
        stack.enter_context(patch("app.api.git.settings", mock_settings))
    if mock_api_user_return is not ...:
        stack.enter_context(
            patch("app.api.git.get_api_user_from_request", return_value=mock_api_user_return)
        )


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def mock_git_service():
    """Create a mock git service with proper return values."""
    mock = MagicMock()
    # Configure get_status to return a proper dict for Pydantic validation
    mock.get_status.return_value = {
        "is_clean": True,
        "current_branch": "main",
        "modified": [],
        "staged": [],
        "untracked": []
    }
    return mock


@pytest.fixture
def mock_worktree_manager():
    """Create a mock worktree manager.

    cleanup_worktree is async, so we use AsyncMock for it to avoid
    RuntimeWarning when the endpoint awaits the mock.
    """
    mock = MagicMock()
    mock.cleanup_worktree = AsyncMock()
    return mock


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
    with ExitStack() as stack:
        _enter_git_patches(
            stack,
            mock_git_service=mock_git_service,
            mock_worktree_manager=mock_worktree_manager,
            mock_database=mock_database,
            mock_settings=mock_settings,
            mock_api_user_return=None,
        )
        with TestClient(app) as test_client:
            yield test_client


@pytest.fixture(autouse=True)
def _patch_shared_git_resolution(mock_database, mock_settings, mock_git_service):
    """Route the refactored shared git helpers to the per-test mocks.

    Git endpoints no longer read ``app.api.git.{database,settings,git_service}``
    directly: project lookups now go through ``app.api.common.resolve_project_path``
    (reads ``app.api.common.database``), path composition through
    ``app.core.project_utils`` (reads its own module-level ``settings``), and the
    repository-record helper lives in ``app.api.git_helpers`` (reads
    ``git_helpers.git_service``). The inline ``app.api.git.*`` patches in each test
    don't reach those modules, so patch them here for the whole suite, mirroring
    the mock objects the tests already install on the endpoint modules.

    Also clears the module-level ``_repo_record_cache`` (60s TTL) so a repo record
    resolved in one test can't leak into the next.
    """
    from app.api import git_helpers as _gh
    from app.api import git_worktrees as _gw
    _gh._repo_record_cache.clear()
    _gw._worktree_status_cache.clear()
    with patch("app.api.common.database", mock_database), \
         patch("app.core.project_utils.settings", mock_settings), \
         patch("app.api.git_helpers.git_service", mock_git_service):
        yield
    _gh._repo_record_cache.clear()
    _gw._worktree_status_cache.clear()


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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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
        """Should return an empty (non-git) status, not 404, for a missing project.

        The status endpoint intentionally resolves the project path with
        ``allow_missing=True`` and returns ``is_git_repo=False`` when the record
        doesn't exist yet, so the frontend poller can retry silently during the
        startup race between polling and project creation (see git.py get_status).
        """
        mock_database.get_project.return_value = None

        with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
            with patch("app.api.git.settings", mock_settings):
                with patch("app.api.git.get_api_user_from_request", return_value=None):
                    client = TestClient(app)
                    response = client.get("/api/v1/projects/nonexistent/git/status")

        assert response.status_code == 200
        assert response.json()["is_git_repo"] is False

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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.get("/api/v1/projects/test-project-id/git/status")

        assert response.status_code == 200
        mock_database.create_git_repository.assert_called_once()

    def test_get_status_cached_repo_record_skips_gitpool(
        self, app, mock_database, mock_git_service, mock_settings,
        sample_project, sample_git_status, sample_repository
    ):
        """A fresh cached repo record must NOT submit a run_git job.

        /git/status is polled every ~2s per active session. The repo record has
        a 60s cache, but ``ensure_git_repository_record`` did its TTL check INSIDE
        the executor job, so every poll still burned a GitPool slot even on a
        cache hit. The cache check is now hoisted so a hit never touches the pool.
        """
        import time as _time
        from app.api import git_helpers as _gh

        mock_database.get_project.return_value = sample_project
        mock_git_service.get_status.return_value = sample_git_status
        mock_git_service.is_git_repo.return_value = True

        submitted = []

        async def counting_run_git(fn, *args, **kwargs):
            submitted.append(getattr(fn, "__name__", str(fn)))
            if kwargs:
                return fn(*args, **kwargs)
            return fn(*args)

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.run_git", counting_run_git):
                        with patch("app.api.git.get_api_user_from_request", return_value=None):
                            # Prime the repo-record cache as if a prior poll had
                            # populated it (autouse fixture clears it at setup).
                            _gh._repo_record_cache["test-project-id"] = (
                                sample_repository, _time.monotonic()
                            )
                            client = TestClient(app)
                            response = client.get(
                                "/api/v1/projects/test-project-id/git/status"
                            )

        assert response.status_code == 200
        # Cache hit: the repo-record helper was never submitted to the pool.
        assert "ensure_git_repository_record" not in submitted
        # The cached record is still surfaced in the response.
        assert response.json()["repository_id"] == "repo-abc123"

    def test_get_status_api_user_access_denied(
        self, app, mock_database, mock_settings, sample_project
    ):
        """Should deny access when API user tries to access different project."""
        mock_database.get_project.return_value = sample_project

        with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
            with patch("app.api.git.settings", mock_settings):
                # Access control now lives in app.api.common.check_project_access,
                # which reads app.api.common.get_api_user_from_request.
                with patch("app.api.common.get_api_user_from_request", return_value={
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.get("/api/v1/projects/test-project-id/git/branches")

        assert response.status_code == 400
        assert "not a git repository" in response.json()["detail"]

    def test_list_branches_project_not_found(self, app, mock_database, mock_settings):
        """Should return 404 for non-existent project."""
        mock_database.get_project.return_value = None

        with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.post(
                            "/api/v1/projects/test-project-id/git/checkout",
                            json={"ref": "feature/test"}
                        )

        assert response.status_code == 409
        assert "uncommitted changes" in response.json()["detail"]

    def test_checkout_with_uncommitted_changes_auto_stash(
        self, app, mock_database, mock_git_service, mock_settings, sample_project
    ):
        """Should stash uncommitted changes and checkout when auto_stash=True."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = True
        mock_git_service.get_status.return_value = {
            "is_clean": False,
            "staged": [{"file": "test.py", "status": "M"}],
            "modified": [],
            "untracked": [],
            "conflicts": []
        }
        mock_git_service.stash_push.return_value = True
        mock_git_service.checkout.return_value = True

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.post(
                            "/api/v1/projects/test-project-id/git/checkout",
                            json={"ref": "feature/test", "auto_stash": True}
                        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "Stashed changes" in data["message"]
        # Must have stashed before checking out.
        mock_git_service.stash_push.assert_called_once()
        mock_git_service.checkout.assert_called_once()

    def test_checkout_auto_stash_fails_to_stash(
        self, app, mock_database, mock_git_service, mock_settings, sample_project
    ):
        """Should return 409 if auto_stash is requested but stashing fails."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = True
        mock_git_service.get_status.return_value = {
            "is_clean": False,
            "staged": [{"file": "test.py", "status": "M"}],
            "modified": [],
            "untracked": [],
            "conflicts": []
        }
        mock_git_service.stash_push.return_value = False

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.post(
                            "/api/v1/projects/test-project-id/git/checkout",
                            json={"ref": "feature/test", "auto_stash": True}
                        )

        assert response.status_code == 409
        assert "stash" in response.json()["detail"].lower()
        # Checkout must NOT have been attempted when the stash failed.
        mock_git_service.checkout.assert_not_called()

    def test_checkout_nonexistent_ref(
        self, app, mock_database, mock_git_service, mock_settings, sample_project
    ):
        """Should return 400 for nonexistent ref."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = True
        mock_git_service.get_status.return_value = {"is_clean": True}
        mock_git_service.checkout.return_value = False

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.worktree_manager", mock_worktree_manager),
                 patch("app.api.git_worktrees.worktree_manager", mock_worktree_manager)):
                with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.worktree_manager", mock_worktree_manager),
                 patch("app.api.git_worktrees.worktree_manager", mock_worktree_manager)):
                with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.worktree_manager", mock_worktree_manager),
                 patch("app.api.git_worktrees.worktree_manager", mock_worktree_manager)):
                with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
                    with patch("app.api.git.settings", mock_settings):
                        with patch("app.api.git.get_api_user_from_request", return_value=None):
                            client = TestClient(app)
                            response = client.get("/api/v1/projects/test-project-id/git/worktrees")

        assert response.status_code == 200
        data = response.json()
        assert data["worktrees"][0]["session_count"] == 1
        assert data["worktrees"][0]["active_session"]["id"] == "ses-abc123"

    def test_list_worktrees_status_uses_single_gitpool_job(
        self, app, mock_database, mock_git_service,
        mock_worktree_manager, mock_settings, sample_project, sample_worktree
    ):
        """Per-worktree git status must be gathered in ONE git-pool job.

        Regression guard for the GitPool queue-depth bursts: the endpoint used to
        submit one ``run_git`` job PER worktree, so a project with N worktrees
        instantly queued N jobs onto the 4-worker pool (depth ~35 with ~40
        worktrees). It must now submit a constant number of jobs regardless of
        worktree count: one ``is_git_repo`` probe + one batched status job, while
        still gathering ``git status`` for every existing worktree.
        """
        worktrees = []
        for i in range(12):
            wt = sample_worktree.copy()
            wt["id"] = f"wt-{i}"
            wt["branch_name"] = f"feature/{i}"
            wt["exists"] = True
            wt["git_status"] = None
            worktrees.append(wt)

        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = True
        mock_git_service.get_status.return_value = {
            "is_clean": True,
            "current_branch": "main",
            "modified": [],
            "staged": [],
            "untracked": [],
        }
        mock_worktree_manager.get_worktrees_for_project.return_value = worktrees

        submitted = []

        async def counting_run_git(fn, *args, **kwargs):
            submitted.append(getattr(fn, "__name__", str(fn)))
            if kwargs:
                return fn(*args, **kwargs)
            return fn(*args)

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.worktree_manager", mock_worktree_manager),
                 patch("app.api.git_worktrees.worktree_manager", mock_worktree_manager)):
                with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
                    with patch("app.api.git.settings", mock_settings):
                        with patch("app.api.git_worktrees.run_git", counting_run_git):
                            with patch("app.api.git.get_api_user_from_request", return_value=None):
                                client = TestClient(app)
                                response = client.get(
                                    "/api/v1/projects/test-project-id/git/worktrees"
                                )

        assert response.status_code == 200
        # Constant job count: one is_git_repo probe + one batched status job.
        # NOT 1 + N (the old per-worktree fan-out) even with 12 worktrees.
        assert len(submitted) == 2
        assert submitted.count("_collect_worktree_statuses") == 1
        # Status is still gathered for every existing worktree.
        assert mock_git_service.get_status.call_count == 12
        data = response.json()
        assert data["total"] == 12
        assert all(
            wt_resp["git_status"]["is_clean"] for wt_resp in data["worktrees"]
        )

    def _make_worktrees(self, sample_worktree, n):
        worktrees = []
        for i in range(n):
            wt = sample_worktree.copy()
            wt["id"] = f"wt-{i}"
            wt["branch_name"] = f"feature/{i}"
            wt["exists"] = True
            wt["git_status"] = None
            worktrees.append(wt)
        return worktrees

    def test_list_worktrees_status_runs_concurrently_bounded(
        self, app, mock_database, mock_git_service,
        mock_worktree_manager, mock_settings, sample_project, sample_worktree
    ):
        """Per-worktree status calls must run CONCURRENTLY, bounded by the pool.

        The 2b98408f fix collapsed the fan-out into ONE sequential job, so N
        worktrees cost N * per_status wall time (the observed 6-9s). Distinct
        worktree dirs are safe to parallelize, so the fan-out now runs on a
        bounded dedicated pool. Assert real overlap (>1 concurrent) while never
        exceeding the bound (_STATUS_FANOUT_MAX_WORKERS).
        """
        import threading
        import time as _t
        from app.api.git_worktrees import _STATUS_FANOUT_MAX_WORKERS

        worktrees = self._make_worktrees(sample_worktree, 12)

        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = True
        mock_worktree_manager.get_worktrees_for_project.return_value = worktrees

        lock = threading.Lock()
        active = {"cur": 0, "max": 0}

        def slow_status(path):
            with lock:
                active["cur"] += 1
                active["max"] = max(active["max"], active["cur"])
            _t.sleep(0.05)
            with lock:
                active["cur"] -= 1
            return {"is_clean": True, "current_branch": "main",
                    "modified": [], "staged": [], "untracked": []}

        mock_git_service.get_status.side_effect = slow_status

        # NOTE: run_git is NOT patched here — the real dedicated pool must run.
        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.worktree_manager", mock_worktree_manager),
                 patch("app.api.git_worktrees.worktree_manager", mock_worktree_manager)):
                with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
                    with patch("app.api.git.settings", mock_settings):
                        with patch("app.api.git.get_api_user_from_request", return_value=None):
                            client = TestClient(app)
                            response = client.get(
                                "/api/v1/projects/test-project-id/git/worktrees"
                            )

        assert response.status_code == 200
        assert mock_git_service.get_status.call_count == 12
        # Ran concurrently...
        assert active["max"] > 1, "statuses did not run concurrently"
        # ...but never exceeded the pool bound.
        assert active["max"] <= _STATUS_FANOUT_MAX_WORKERS

    def test_list_worktrees_status_ttl_cache_serves_and_expires(
        self, app, mock_database, mock_git_service,
        mock_worktree_manager, mock_settings, sample_project, sample_worktree
    ):
        """A burst of polls within the TTL shares one fan-out and skips the pool.

        After the window expires, the fan-out re-runs. Mirrors the "check the TTL
        cache BEFORE submitting pool work" pattern.
        """
        import time as _t
        from app.api import git_worktrees as _gw

        worktrees = self._make_worktrees(sample_worktree, 5)

        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = True
        mock_git_service.get_status.return_value = {
            "is_clean": True, "current_branch": "main",
            "modified": [], "staged": [], "untracked": [],
        }
        mock_worktree_manager.get_worktrees_for_project.return_value = worktrees

        submitted = []

        async def counting_run_git(fn, *args, **kwargs):
            submitted.append(getattr(fn, "__name__", str(fn)))
            if kwargs:
                return fn(*args, **kwargs)
            return fn(*args)

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.worktree_manager", mock_worktree_manager),
                 patch("app.api.git_worktrees.worktree_manager", mock_worktree_manager)):
                with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
                    with patch("app.api.git.settings", mock_settings):
                        with patch("app.api.git_worktrees.run_git", counting_run_git):
                            with patch("app.api.git.get_api_user_from_request", return_value=None):
                                client = TestClient(app)
                                url = "/api/v1/projects/test-project-id/git/worktrees"
                                r1 = client.get(url)
                                r2 = client.get(url)  # within TTL -> cache hit
                                # Force-expire the cached entry, then poll again.
                                statuses, _ts = _gw._worktree_status_cache["test-project-id"]
                                _gw._worktree_status_cache["test-project-id"] = (
                                    statuses, _t.monotonic() - (_gw._WORKTREE_STATUS_CACHE_TTL + 1)
                                )
                                r3 = client.get(url)

        assert r1.status_code == r2.status_code == r3.status_code == 200
        # Miss -> fan-out ran (5 statuses); hit -> served from cache (still 5);
        # expiry -> fan-out ran again (10 total).
        assert submitted.count("_collect_worktree_statuses") == 2
        assert mock_git_service.get_status.call_count == 10
        # Every response still carries fresh per-worktree status.
        assert all(wt["git_status"]["is_clean"] for wt in r2.json()["worktrees"])

    def test_list_worktrees_status_one_error_does_not_poison_others(
        self, app, mock_database, mock_git_service,
        mock_worktree_manager, mock_settings, sample_project, sample_worktree
    ):
        """One worktree's status failure must not poison the rest of the batch."""
        import threading

        worktrees = self._make_worktrees(sample_worktree, 12)

        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = True
        mock_worktree_manager.get_worktrees_for_project.return_value = worktrees

        lock = threading.Lock()
        calls = {"n": 0}

        def flaky_status(path):
            with lock:
                calls["n"] += 1
                first = calls["n"] == 1
            if first:
                raise RuntimeError("boom: corrupt index.lock")
            return {"is_clean": True, "current_branch": "main",
                    "modified": [], "staged": [], "untracked": []}

        mock_git_service.get_status.side_effect = flaky_status

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.worktree_manager", mock_worktree_manager),
                 patch("app.api.git_worktrees.worktree_manager", mock_worktree_manager)):
                with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
                    with patch("app.api.git.settings", mock_settings):
                        with patch("app.api.git.get_api_user_from_request", return_value=None):
                            client = TestClient(app)
                            response = client.get(
                                "/api/v1/projects/test-project-id/git/worktrees"
                            )

        assert response.status_code == 200
        statuses = [wt["git_status"] for wt in response.json()["worktrees"]]
        # Exactly one worktree failed (git_status None); the other 11 succeeded.
        assert sum(1 for s in statuses if s is None) == 1
        assert sum(1 for s in statuses if s is not None) == 11

    def test_list_worktrees_not_git_repo(
        self, app, mock_database, mock_git_service, mock_settings, sample_project
    ):
        """Should return 400 for non-git repository."""
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = False

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.worktree_manager", mock_worktree_manager),
                 patch("app.api.git_worktrees.worktree_manager", mock_worktree_manager)):
                with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.worktree_manager", mock_worktree_manager),
                 patch("app.api.git_worktrees.worktree_manager", mock_worktree_manager)):
                with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.worktree_manager", mock_worktree_manager),
                 patch("app.api.git_worktrees.worktree_manager", mock_worktree_manager)):
                with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.worktree_manager", mock_worktree_manager),
                 patch("app.api.git_worktrees.worktree_manager", mock_worktree_manager)):
                with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.worktree_manager", mock_worktree_manager),
                 patch("app.api.git_worktrees.worktree_manager", mock_worktree_manager)):
                with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.worktree_manager", mock_worktree_manager),
                 patch("app.api.git_worktrees.worktree_manager", mock_worktree_manager)):
                with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.worktree_manager", mock_worktree_manager),
                 patch("app.api.git_worktrees.worktree_manager", mock_worktree_manager)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.worktree_manager", mock_worktree_manager),
                 patch("app.api.git_worktrees.worktree_manager", mock_worktree_manager)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.worktree_manager", mock_worktree_manager),
                 patch("app.api.git_worktrees.worktree_manager", mock_worktree_manager)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.worktree_manager", mock_worktree_manager),
                 patch("app.api.git_worktrees.worktree_manager", mock_worktree_manager)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.worktree_manager", mock_worktree_manager),
                 patch("app.api.git_worktrees.worktree_manager", mock_worktree_manager)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.worktree_manager", mock_worktree_manager),
                 patch("app.api.git_worktrees.worktree_manager", mock_worktree_manager)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.worktree_manager", mock_worktree_manager),
                 patch("app.api.git_worktrees.worktree_manager", mock_worktree_manager)):
                with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.worktree_manager", mock_worktree_manager),
                 patch("app.api.git_worktrees.worktree_manager", mock_worktree_manager)):
                with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git),
             patch("app.api.git_helpers.git_service", mock_git)):
            with (patch("app.api.git.database", mock_db),
                 patch("app.api.git_helpers.database", mock_db)):
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

        with (patch("app.api.git.git_service", mock_git),
             patch("app.api.git_helpers.git_service", mock_git)):
            with (patch("app.api.git.database", mock_db),
                 patch("app.api.git_helpers.database", mock_db)):
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

        with (patch("app.api.git.git_service", mock_git),
             patch("app.api.git_helpers.git_service", mock_git)):
            with (patch("app.api.git.database", mock_db),
                 patch("app.api.git_helpers.database", mock_db)):
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

        with (patch("app.api.git.git_service", mock_git),
             patch("app.api.git_helpers.git_service", mock_git)):
            with (patch("app.api.git.database", mock_db),
                 patch("app.api.git_helpers.database", mock_db)):
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

        with (patch("app.api.git.git_service", mock_git),
             patch("app.api.git_helpers.git_service", mock_git)):
            with (patch("app.api.git.database", mock_db),
                 patch("app.api.git_helpers.database", mock_db)):
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

        with (patch("app.api.git.git_service", mock_git),
             patch("app.api.git_helpers.git_service", mock_git)):
            with (patch("app.api.git.database", mock_db),
                 patch("app.api.git_helpers.database", mock_db)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
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

        with (patch("app.api.git.git_service", mock_git_service),
             patch("app.api.git_worktrees.git_service", mock_git_service),
             patch("app.api.git_ai.git_service", mock_git_service)):
            with (patch("app.api.git.database", mock_database),
                 patch("app.api.git_helpers.database", mock_database),
                 patch("app.api.git_worktrees.database", mock_database)):
                with patch("app.api.git.settings", mock_settings):
                    with patch("app.api.git.get_api_user_from_request", return_value=None):
                        client = TestClient(app)
                        response = client.post(
                            "/api/v1/projects/test-project-id/git/worktrees",
                            json={"branch_name": ""}
                        )

        assert response.status_code == 422
