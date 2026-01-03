"""
Comprehensive tests for GitHub integration API endpoints.

This module tests all endpoints in app/api/github.py:
- GET /api/v1/projects/{project_id}/github/status
- GET /api/v1/projects/{project_id}/github/pulls
- GET /api/v1/projects/{project_id}/github/pulls/{number}
- POST /api/v1/projects/{project_id}/github/pulls
- POST /api/v1/projects/{project_id}/github/pulls/{number}/merge
- DELETE /api/v1/projects/{project_id}/github/pulls/{number}
- GET /api/v1/projects/{project_id}/github/actions
- GET /api/v1/projects/{project_id}/github/actions/{run_id}
- POST /api/v1/projects/{project_id}/github/actions/{run_id}/rerun
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from pathlib import Path
from fastapi import Request, HTTPException

from app.core.github_service import PullRequest, WorkflowRun


# =============================================================================
# Test Data Fixtures
# =============================================================================

@pytest.fixture
def sample_pull_request():
    """Sample PullRequest object for testing"""
    return PullRequest(
        number=42,
        title="Add new feature",
        body="This PR adds a great new feature",
        state="open",
        head_branch="feature/new-stuff",
        base_branch="main",
        author="testuser",
        url="https://github.com/owner/repo/pull/42",
        created_at="2024-01-01T10:00:00Z",
        updated_at="2024-01-02T15:30:00Z",
        mergeable=True
    )


@pytest.fixture
def sample_pull_request_2():
    """Second sample PullRequest for list tests"""
    return PullRequest(
        number=43,
        title="Fix bug",
        body="This PR fixes a critical bug",
        state="open",
        head_branch="fix/critical-bug",
        base_branch="main",
        author="anotheruser",
        url="https://github.com/owner/repo/pull/43",
        created_at="2024-01-02T10:00:00Z",
        updated_at="2024-01-03T15:30:00Z",
        mergeable=True
    )


@pytest.fixture
def sample_workflow_run():
    """Sample WorkflowRun object for testing"""
    return WorkflowRun(
        id=12345,
        name="CI",
        status="completed",
        conclusion="success",
        branch="main",
        event="push",
        url="https://github.com/owner/repo/actions/runs/12345",
        created_at="2024-01-01T10:00:00Z"
    )


@pytest.fixture
def sample_workflow_run_2():
    """Second sample WorkflowRun for list tests"""
    return WorkflowRun(
        id=12346,
        name="Deploy",
        status="in_progress",
        conclusion=None,
        branch="main",
        event="workflow_dispatch",
        url="https://github.com/owner/repo/actions/runs/12346",
        created_at="2024-01-02T10:00:00Z"
    )


@pytest.fixture
def mock_project_data():
    """Mock project data returned by database.get_project"""
    return {
        "id": "test-project-id",
        "name": "Test Project",
        "path": "test-project",
        "description": "A test project",
        "settings": {}
    }


# =============================================================================
# Module Import Tests
# =============================================================================

class TestGitHubModuleImports:
    """Verify GitHub module can be imported correctly."""

    def test_github_module_imports(self):
        """GitHub module should import without errors."""
        from app.api import github
        assert github is not None

    def test_github_router_exists(self):
        """GitHub router should exist."""
        from app.api.github import router
        assert router is not None
        assert router.prefix == "/api/v1/projects/{project_id}/github"

    def test_pydantic_models_exist(self):
        """Pydantic models should be importable."""
        from app.api.github import (
            PullRequestResponse,
            WorkflowRunResponse,
            CreatePullRequest,
            MergePullRequest,
            GitHubStatusResponse
        )
        assert PullRequestResponse is not None
        assert WorkflowRunResponse is not None
        assert CreatePullRequest is not None
        assert MergePullRequest is not None
        assert GitHubStatusResponse is not None


# =============================================================================
# Helper Function Tests
# =============================================================================

class TestPrToResponse:
    """Test pr_to_response helper function."""

    def test_converts_all_fields(self, sample_pull_request):
        """Test converting PullRequest to response model."""
        from app.api.github import pr_to_response

        response = pr_to_response(sample_pull_request)

        assert response.number == 42
        assert response.title == "Add new feature"
        assert response.body == "This PR adds a great new feature"
        assert response.state == "open"
        assert response.head_branch == "feature/new-stuff"
        assert response.base_branch == "main"
        assert response.author == "testuser"
        assert response.url == "https://github.com/owner/repo/pull/42"
        assert response.created_at == "2024-01-01T10:00:00Z"
        assert response.updated_at == "2024-01-02T15:30:00Z"
        assert response.mergeable is True

    def test_handles_none_mergeable(self):
        """Test handling of None mergeable field."""
        from app.api.github import pr_to_response

        pr = PullRequest(
            number=1,
            title="Test",
            body="",
            state="open",
            head_branch="test",
            base_branch="main",
            author="user",
            url="https://github.com/test/test/pull/1",
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
            mergeable=None
        )

        response = pr_to_response(pr)
        assert response.mergeable is None


class TestRunToResponse:
    """Test run_to_response helper function."""

    def test_converts_all_fields(self, sample_workflow_run):
        """Test converting WorkflowRun to response model."""
        from app.api.github import run_to_response

        response = run_to_response(sample_workflow_run)

        assert response.id == 12345
        assert response.name == "CI"
        assert response.status == "completed"
        assert response.conclusion == "success"
        assert response.branch == "main"
        assert response.event == "push"
        assert response.url == "https://github.com/owner/repo/actions/runs/12345"
        assert response.created_at == "2024-01-01T10:00:00Z"

    def test_handles_none_conclusion(self, sample_workflow_run_2):
        """Test handling of None conclusion field (in-progress run)."""
        from app.api.github import run_to_response

        response = run_to_response(sample_workflow_run_2)
        assert response.conclusion is None
        assert response.status == "in_progress"


class TestCheckProjectAccess:
    """Test check_project_access helper function."""

    def test_allows_access_when_project_id_matches(self):
        """Test check_project_access allows access when project_id matches."""
        from app.api.github import check_project_access

        mock_request = MagicMock(spec=Request)

        with patch("app.api.github.get_api_user_from_request") as mock_get_user:
            mock_get_user.return_value = {"project_id": "test-project-id"}

            # Should not raise
            check_project_access(mock_request, "test-project-id")
            mock_get_user.assert_called_once_with(mock_request)

    def test_denies_access_when_project_id_differs(self):
        """Test check_project_access denies access when project_id does not match."""
        from app.api.github import check_project_access

        mock_request = MagicMock(spec=Request)

        with patch("app.api.github.get_api_user_from_request") as mock_get_user:
            mock_get_user.return_value = {"project_id": "different-project-id"}

            with pytest.raises(HTTPException) as exc_info:
                check_project_access(mock_request, "test-project-id")

            assert exc_info.value.status_code == 403
            assert "Access denied" in exc_info.value.detail

    def test_allows_access_when_no_api_user(self):
        """Test check_project_access allows access when no api_user (admin)."""
        from app.api.github import check_project_access

        mock_request = MagicMock(spec=Request)

        with patch("app.api.github.get_api_user_from_request") as mock_get_user:
            mock_get_user.return_value = None

            # Should not raise when no api_user
            check_project_access(mock_request, "test-project-id")

    def test_allows_access_when_api_user_has_no_project_id(self):
        """Test check_project_access allows access when api_user has no project_id restriction."""
        from app.api.github import check_project_access

        mock_request = MagicMock(spec=Request)

        with patch("app.api.github.get_api_user_from_request") as mock_get_user:
            mock_get_user.return_value = {"id": "user-id"}  # No project_id

            # Should not raise when api_user has no project_id restriction
            check_project_access(mock_request, "test-project-id")


class TestGetProjectPath:
    """Test get_project_path helper function."""

    def test_returns_path_for_valid_project(self, mock_project_data):
        """Test get_project_path returns correct path."""
        from app.api.github import get_project_path
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_path = Path(tmpdir)
            project_path = workspace_path / mock_project_data["path"]
            project_path.mkdir(parents=True, exist_ok=True)

            with patch("app.api.github.database") as mock_db:
                mock_db.get_project.return_value = mock_project_data

                with patch("app.api.github.settings") as mock_settings:
                    mock_settings.workspace_dir = workspace_path

                    result = get_project_path("test-project-id")

                    assert result == str(project_path)
                    mock_db.get_project.assert_called_once_with("test-project-id")

    def test_raises_404_for_nonexistent_project(self):
        """Test get_project_path raises 404 for non-existent project."""
        from app.api.github import get_project_path

        with patch("app.api.github.database") as mock_db:
            mock_db.get_project.return_value = None

            with pytest.raises(HTTPException) as exc_info:
                get_project_path("non-existent-project")

            assert exc_info.value.status_code == 404
            assert "Project not found" in exc_info.value.detail

    def test_raises_404_for_missing_directory(self, mock_project_data):
        """Test get_project_path raises 404 when directory doesn't exist."""
        from app.api.github import get_project_path
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_path = Path(tmpdir)
            # Don't create the project directory

            with patch("app.api.github.database") as mock_db:
                mock_db.get_project.return_value = mock_project_data

                with patch("app.api.github.settings") as mock_settings:
                    mock_settings.workspace_dir = workspace_path

                    with pytest.raises(HTTPException) as exc_info:
                        get_project_path("test-project-id")

                    assert exc_info.value.status_code == 404
                    assert "Project directory not found" in exc_info.value.detail


class TestGetRepoForProject:
    """Test get_repo_for_project helper function."""

    def test_returns_repo_when_found(self, mock_project_data):
        """Test get_repo_for_project returns repo when found."""
        from app.api.github import get_repo_for_project
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_path = Path(tmpdir)
            project_path = workspace_path / mock_project_data["path"]
            project_path.mkdir(parents=True, exist_ok=True)

            with patch("app.api.github.database") as mock_db:
                mock_db.get_project.return_value = mock_project_data

                with patch("app.api.github.settings") as mock_settings:
                    mock_settings.workspace_dir = workspace_path

                    with patch("app.api.github.github_service") as mock_github:
                        mock_github.get_repo_from_remote.return_value = "owner/repo"

                        result = get_repo_for_project("test-project-id")

                        assert result == "owner/repo"

    def test_raises_400_when_no_remote(self, mock_project_data):
        """Test get_repo_for_project raises 400 when no GitHub remote."""
        from app.api.github import get_repo_for_project
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_path = Path(tmpdir)
            project_path = workspace_path / mock_project_data["path"]
            project_path.mkdir(parents=True, exist_ok=True)

            with patch("app.api.github.database") as mock_db:
                mock_db.get_project.return_value = mock_project_data

                with patch("app.api.github.settings") as mock_settings:
                    mock_settings.workspace_dir = workspace_path

                    with patch("app.api.github.github_service") as mock_github:
                        mock_github.get_repo_from_remote.return_value = None

                        with pytest.raises(HTTPException) as exc_info:
                            get_repo_for_project("test-project-id")

                        assert exc_info.value.status_code == 400
                        assert "Could not determine GitHub repository" in exc_info.value.detail


class TestCheckGitHubAuth:
    """Test check_github_auth helper function."""

    def test_passes_when_authenticated(self):
        """Test check_github_auth passes when authenticated."""
        from app.api.github import check_github_auth

        with patch("app.api.github.github_service") as mock_github:
            mock_github.is_authenticated.return_value = True

            # Should not raise
            check_github_auth()
            mock_github.is_authenticated.assert_called_once()

    def test_raises_401_when_not_authenticated(self):
        """Test check_github_auth raises 401 when not authenticated."""
        from app.api.github import check_github_auth

        with patch("app.api.github.github_service") as mock_github:
            mock_github.is_authenticated.return_value = False

            with pytest.raises(HTTPException) as exc_info:
                check_github_auth()

            assert exc_info.value.status_code == 401
            assert "not authenticated" in exc_info.value.detail


# =============================================================================
# Pydantic Model Validation Tests
# =============================================================================

class TestPydanticModels:
    """Test Pydantic model validation."""

    def test_create_pull_request_valid(self):
        """Test CreatePullRequest with valid data."""
        from app.api.github import CreatePullRequest

        pr = CreatePullRequest(
            title="Test PR",
            body="Description",
            head="feature-branch",
            base="main"
        )
        assert pr.title == "Test PR"
        assert pr.body == "Description"
        assert pr.head == "feature-branch"
        assert pr.base == "main"

    def test_create_pull_request_default_body(self):
        """Test CreatePullRequest with default body."""
        from app.api.github import CreatePullRequest

        pr = CreatePullRequest(
            title="Test PR",
            head="feature-branch",
            base="main"
        )
        assert pr.body == ""

    def test_create_pull_request_empty_title_fails(self):
        """Test CreatePullRequest with empty title fails."""
        from app.api.github import CreatePullRequest
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc_info:
            CreatePullRequest(title="", head="feature", base="main")

        assert "title" in str(exc_info.value).lower()

    def test_create_pull_request_title_too_long_fails(self):
        """Test CreatePullRequest with too long title fails."""
        from app.api.github import CreatePullRequest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            CreatePullRequest(
                title="x" * 257,  # Exceeds 256 char limit
                head="feature",
                base="main"
            )

    def test_create_pull_request_missing_required_fails(self):
        """Test CreatePullRequest with missing required fields fails."""
        from app.api.github import CreatePullRequest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            CreatePullRequest(title="Test")  # Missing head and base

    def test_merge_pull_request_valid_methods(self):
        """Test MergePullRequest with valid merge methods."""
        from app.api.github import MergePullRequest

        assert MergePullRequest(method="merge").method == "merge"
        assert MergePullRequest(method="squash").method == "squash"
        assert MergePullRequest(method="rebase").method == "rebase"

    def test_merge_pull_request_default_method(self):
        """Test MergePullRequest default method is 'merge'."""
        from app.api.github import MergePullRequest

        assert MergePullRequest().method == "merge"

    def test_merge_pull_request_invalid_method_fails(self):
        """Test MergePullRequest with invalid method fails."""
        from app.api.github import MergePullRequest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            MergePullRequest(method="invalid")

    def test_pull_request_response_all_fields(self):
        """Test PullRequestResponse with all fields."""
        from app.api.github import PullRequestResponse

        pr = PullRequestResponse(
            number=1,
            title="Test",
            body="Body",
            state="open",
            head_branch="feature",
            base_branch="main",
            author="user",
            url="https://github.com/test/test/pull/1",
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
            mergeable=True
        )
        assert pr.mergeable is True

    def test_pull_request_response_optional_mergeable(self):
        """Test PullRequestResponse with optional mergeable."""
        from app.api.github import PullRequestResponse

        pr = PullRequestResponse(
            number=1,
            title="Test",
            body="",
            state="open",
            head_branch="feature",
            base_branch="main",
            author="user",
            url="https://github.com/test/test/pull/1",
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z"
        )
        assert pr.mergeable is None

    def test_workflow_run_response_all_fields(self):
        """Test WorkflowRunResponse with all fields."""
        from app.api.github import WorkflowRunResponse

        run = WorkflowRunResponse(
            id=1,
            name="CI",
            status="completed",
            conclusion="success",
            branch="main",
            event="push",
            url="https://github.com/test/test/actions/runs/1",
            created_at="2024-01-01T00:00:00Z"
        )
        assert run.conclusion == "success"

    def test_workflow_run_response_optional_conclusion(self):
        """Test WorkflowRunResponse with optional conclusion."""
        from app.api.github import WorkflowRunResponse

        run = WorkflowRunResponse(
            id=1,
            name="CI",
            status="in_progress",
            conclusion=None,
            branch="main",
            event="push",
            url="https://github.com/test/test/actions/runs/1",
            created_at="2024-01-01T00:00:00Z"
        )
        assert run.conclusion is None

    def test_github_status_response_minimal(self):
        """Test GitHubStatusResponse with minimal fields."""
        from app.api.github import GitHubStatusResponse

        status = GitHubStatusResponse(authenticated=False)
        assert status.authenticated is False
        assert status.repo is None
        assert status.repo_info is None
        assert status.error is None

    def test_github_status_response_full(self):
        """Test GitHubStatusResponse with all fields."""
        from app.api.github import GitHubStatusResponse

        status = GitHubStatusResponse(
            authenticated=True,
            repo="owner/repo",
            repo_info={"name": "repo", "owner": "owner"},
            error=None
        )
        assert status.authenticated is True
        assert status.repo == "owner/repo"
        assert status.repo_info["name"] == "repo"


# =============================================================================
# Endpoint Function Tests (Unit Tests)
# =============================================================================

class TestGetGitHubStatusEndpoint:
    """Test get_github_status endpoint function directly."""

    @pytest.mark.asyncio
    async def test_returns_status_when_authenticated_with_repo(self, mock_project_data):
        """Test status endpoint returns full status when authenticated with repo."""
        from app.api.github import get_github_status
        from pathlib import Path
        import tempfile

        mock_request = MagicMock(spec=Request)

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_path = Path(tmpdir)
            project_path = workspace_path / mock_project_data["path"]
            project_path.mkdir(parents=True, exist_ok=True)

            with patch("app.api.github.check_project_access"):
                with patch("app.api.github.github_service") as mock_github:
                    mock_github.is_authenticated.return_value = True
                    mock_github.get_repo_from_remote.return_value = "owner/repo"
                    mock_github.get_repo_info.return_value = {
                        "name": "repo",
                        "owner": "owner",
                        "description": "Test repo"
                    }

                    with patch("app.api.github.get_project_path") as mock_get_path:
                        mock_get_path.return_value = str(project_path)

                        result = await get_github_status(
                            request=mock_request,
                            project_id="test-project-id",
                            token="test-token"
                        )

                        assert result.authenticated is True
                        assert result.repo == "owner/repo"
                        assert result.repo_info["name"] == "repo"
                        assert result.error is None

    @pytest.mark.asyncio
    async def test_returns_status_when_not_authenticated(self):
        """Test status endpoint returns error when not authenticated."""
        from app.api.github import get_github_status

        mock_request = MagicMock(spec=Request)

        with patch("app.api.github.check_project_access"):
            with patch("app.api.github.github_service") as mock_github:
                mock_github.is_authenticated.return_value = False

                result = await get_github_status(
                    request=mock_request,
                    project_id="test-project-id",
                    token="test-token"
                )

                assert result.authenticated is False
                assert result.error == "GitHub CLI is not authenticated"

    @pytest.mark.asyncio
    async def test_returns_status_when_no_remote(self, mock_project_data):
        """Test status endpoint returns error when no remote configured."""
        from app.api.github import get_github_status
        from pathlib import Path
        import tempfile

        mock_request = MagicMock(spec=Request)

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_path = Path(tmpdir)
            project_path = workspace_path / mock_project_data["path"]
            project_path.mkdir(parents=True, exist_ok=True)

            with patch("app.api.github.check_project_access"):
                with patch("app.api.github.github_service") as mock_github:
                    mock_github.is_authenticated.return_value = True
                    mock_github.get_repo_from_remote.return_value = None

                    with patch("app.api.github.get_project_path") as mock_get_path:
                        mock_get_path.return_value = str(project_path)

                        result = await get_github_status(
                            request=mock_request,
                            project_id="test-project-id",
                            token="test-token"
                        )

                        assert result.authenticated is True
                        assert result.repo is None
                        assert result.error == "No GitHub remote found in project"

    @pytest.mark.asyncio
    async def test_handles_exception_gracefully(self):
        """Test status endpoint handles exceptions gracefully."""
        from app.api.github import get_github_status

        mock_request = MagicMock(spec=Request)

        with patch("app.api.github.check_project_access"):
            with patch("app.api.github.github_service") as mock_github:
                mock_github.is_authenticated.return_value = True

                with patch("app.api.github.get_project_path") as mock_get_path:
                    mock_get_path.side_effect = Exception("Network error")

                    result = await get_github_status(
                        request=mock_request,
                        project_id="test-project-id",
                        token="test-token"
                    )

                    assert result.authenticated is True
                    assert "Network error" in result.error


class TestListPullRequestsEndpoint:
    """Test list_pull_requests endpoint function directly."""

    @pytest.mark.asyncio
    async def test_returns_list_of_prs(self, sample_pull_request, sample_pull_request_2):
        """Test list PRs returns a list of pull requests."""
        from app.api.github import list_pull_requests

        mock_request = MagicMock(spec=Request)

        with patch("app.api.github.check_project_access"):
            with patch("app.api.github.check_github_auth"):
                with patch("app.api.github.get_repo_for_project") as mock_get_repo:
                    mock_get_repo.return_value = "owner/repo"

                    with patch("app.api.github.github_service") as mock_github:
                        mock_github.list_pulls.return_value = [sample_pull_request, sample_pull_request_2]

                        result = await list_pull_requests(
                            request=mock_request,
                            project_id="test-project-id",
                            state="open",
                            limit=30,
                            token="test-token"
                        )

                        assert len(result) == 2
                        assert result[0].number == 42
                        assert result[1].number == 43
                        mock_github.list_pulls.assert_called_once_with("owner/repo", state="open", limit=30)

    @pytest.mark.asyncio
    async def test_caps_limit_at_100(self, sample_pull_request):
        """Test list PRs caps limit at 100."""
        from app.api.github import list_pull_requests

        mock_request = MagicMock(spec=Request)

        with patch("app.api.github.check_project_access"):
            with patch("app.api.github.check_github_auth"):
                with patch("app.api.github.get_repo_for_project") as mock_get_repo:
                    mock_get_repo.return_value = "owner/repo"

                    with patch("app.api.github.github_service") as mock_github:
                        mock_github.list_pulls.return_value = []

                        await list_pull_requests(
                            request=mock_request,
                            project_id="test-project-id",
                            state="open",
                            limit=500,  # Over the limit
                            token="test-token"
                        )

                        mock_github.list_pulls.assert_called_once_with("owner/repo", state="open", limit=100)

    @pytest.mark.asyncio
    async def test_returns_empty_list(self):
        """Test list PRs returns empty list when no PRs."""
        from app.api.github import list_pull_requests

        mock_request = MagicMock(spec=Request)

        with patch("app.api.github.check_project_access"):
            with patch("app.api.github.check_github_auth"):
                with patch("app.api.github.get_repo_for_project") as mock_get_repo:
                    mock_get_repo.return_value = "owner/repo"

                    with patch("app.api.github.github_service") as mock_github:
                        mock_github.list_pulls.return_value = []

                        result = await list_pull_requests(
                            request=mock_request,
                            project_id="test-project-id",
                            state="closed",
                            limit=30,
                            token="test-token"
                        )

                        assert len(result) == 0


class TestGetPullRequestEndpoint:
    """Test get_pull_request endpoint function directly."""

    @pytest.mark.asyncio
    async def test_returns_pr_when_found(self, sample_pull_request):
        """Test get PR returns the pull request when found."""
        from app.api.github import get_pull_request

        mock_request = MagicMock(spec=Request)

        with patch("app.api.github.check_project_access"):
            with patch("app.api.github.check_github_auth"):
                with patch("app.api.github.get_repo_for_project") as mock_get_repo:
                    mock_get_repo.return_value = "owner/repo"

                    with patch("app.api.github.github_service") as mock_github:
                        mock_github.get_pull.return_value = sample_pull_request

                        result = await get_pull_request(
                            request=mock_request,
                            project_id="test-project-id",
                            number=42,
                            token="test-token"
                        )

                        assert result.number == 42
                        assert result.title == "Add new feature"
                        mock_github.get_pull.assert_called_once_with("owner/repo", 42)

    @pytest.mark.asyncio
    async def test_raises_404_when_not_found(self):
        """Test get PR raises 404 when not found."""
        from app.api.github import get_pull_request

        mock_request = MagicMock(spec=Request)

        with patch("app.api.github.check_project_access"):
            with patch("app.api.github.check_github_auth"):
                with patch("app.api.github.get_repo_for_project") as mock_get_repo:
                    mock_get_repo.return_value = "owner/repo"

                    with patch("app.api.github.github_service") as mock_github:
                        mock_github.get_pull.return_value = None

                        with pytest.raises(HTTPException) as exc_info:
                            await get_pull_request(
                                request=mock_request,
                                project_id="test-project-id",
                                number=999,
                                token="test-token"
                            )

                        assert exc_info.value.status_code == 404
                        assert "#999" in exc_info.value.detail


class TestCreatePullRequestEndpoint:
    """Test create_pull_request endpoint function directly."""

    @pytest.mark.asyncio
    async def test_creates_pr_successfully(self, sample_pull_request):
        """Test create PR creates and returns a pull request."""
        from app.api.github import create_pull_request, CreatePullRequest

        mock_request = MagicMock(spec=Request)
        body = CreatePullRequest(
            title="New Feature",
            body="Description",
            head="feature-branch",
            base="main"
        )

        with patch("app.api.github.check_project_access"):
            with patch("app.api.github.check_github_auth"):
                with patch("app.api.github.get_repo_for_project") as mock_get_repo:
                    mock_get_repo.return_value = "owner/repo"

                    with patch("app.api.github.github_service") as mock_github:
                        mock_github.create_pull.return_value = sample_pull_request

                        result = await create_pull_request(
                            request=mock_request,
                            project_id="test-project-id",
                            body=body,
                            token="test-token"
                        )

                        assert result.number == 42
                        mock_github.create_pull.assert_called_once_with(
                            repo="owner/repo",
                            title="New Feature",
                            body="Description",
                            head="feature-branch",
                            base="main"
                        )

    @pytest.mark.asyncio
    async def test_raises_400_when_creation_fails(self):
        """Test create PR raises 400 when creation fails."""
        from app.api.github import create_pull_request, CreatePullRequest

        mock_request = MagicMock(spec=Request)
        body = CreatePullRequest(
            title="New Feature",
            body="Description",
            head="feature-branch",
            base="main"
        )

        with patch("app.api.github.check_project_access"):
            with patch("app.api.github.check_github_auth"):
                with patch("app.api.github.get_repo_for_project") as mock_get_repo:
                    mock_get_repo.return_value = "owner/repo"

                    with patch("app.api.github.github_service") as mock_github:
                        mock_github.create_pull.return_value = None

                        with pytest.raises(HTTPException) as exc_info:
                            await create_pull_request(
                                request=mock_request,
                                project_id="test-project-id",
                                body=body,
                                token="test-token"
                            )

                        assert exc_info.value.status_code == 400
                        assert "Failed to create" in exc_info.value.detail


class TestMergePullRequestEndpoint:
    """Test merge_pull_request endpoint function directly."""

    @pytest.mark.asyncio
    async def test_merges_pr_successfully(self):
        """Test merge PR merges successfully."""
        from app.api.github import merge_pull_request

        mock_request = MagicMock(spec=Request)

        with patch("app.api.github.check_project_access"):
            with patch("app.api.github.check_github_auth"):
                with patch("app.api.github.get_repo_for_project") as mock_get_repo:
                    mock_get_repo.return_value = "owner/repo"

                    with patch("app.api.github.github_service") as mock_github:
                        mock_github.merge_pull.return_value = True

                        result = await merge_pull_request(
                            request=mock_request,
                            project_id="test-project-id",
                            number=42,
                            body=None,
                            token="test-token"
                        )

                        assert result["status"] == "ok"
                        assert "merged successfully" in result["message"]
                        mock_github.merge_pull.assert_called_once_with("owner/repo", 42, method="merge")

    @pytest.mark.asyncio
    async def test_merges_with_squash_method(self):
        """Test merge PR with squash method."""
        from app.api.github import merge_pull_request, MergePullRequest

        mock_request = MagicMock(spec=Request)
        body = MergePullRequest(method="squash")

        with patch("app.api.github.check_project_access"):
            with patch("app.api.github.check_github_auth"):
                with patch("app.api.github.get_repo_for_project") as mock_get_repo:
                    mock_get_repo.return_value = "owner/repo"

                    with patch("app.api.github.github_service") as mock_github:
                        mock_github.merge_pull.return_value = True

                        await merge_pull_request(
                            request=mock_request,
                            project_id="test-project-id",
                            number=42,
                            body=body,
                            token="test-token"
                        )

                        mock_github.merge_pull.assert_called_once_with("owner/repo", 42, method="squash")

    @pytest.mark.asyncio
    async def test_raises_400_when_merge_fails(self):
        """Test merge PR raises 400 when merge fails."""
        from app.api.github import merge_pull_request

        mock_request = MagicMock(spec=Request)

        with patch("app.api.github.check_project_access"):
            with patch("app.api.github.check_github_auth"):
                with patch("app.api.github.get_repo_for_project") as mock_get_repo:
                    mock_get_repo.return_value = "owner/repo"

                    with patch("app.api.github.github_service") as mock_github:
                        mock_github.merge_pull.return_value = False

                        with pytest.raises(HTTPException) as exc_info:
                            await merge_pull_request(
                                request=mock_request,
                                project_id="test-project-id",
                                number=42,
                                body=None,
                                token="test-token"
                            )

                        assert exc_info.value.status_code == 400
                        assert "Failed to merge" in exc_info.value.detail


class TestClosePullRequestEndpoint:
    """Test close_pull_request endpoint function directly."""

    @pytest.mark.asyncio
    async def test_closes_pr_successfully(self):
        """Test close PR closes successfully."""
        from app.api.github import close_pull_request

        mock_request = MagicMock(spec=Request)

        with patch("app.api.github.check_project_access"):
            with patch("app.api.github.check_github_auth"):
                with patch("app.api.github.get_repo_for_project") as mock_get_repo:
                    mock_get_repo.return_value = "owner/repo"

                    with patch("app.api.github.github_service") as mock_github:
                        mock_github.close_pull.return_value = True

                        result = await close_pull_request(
                            request=mock_request,
                            project_id="test-project-id",
                            number=42,
                            token="test-token"
                        )

                        assert result["status"] == "ok"
                        assert "closed" in result["message"]
                        mock_github.close_pull.assert_called_once_with("owner/repo", 42)

    @pytest.mark.asyncio
    async def test_raises_400_when_close_fails(self):
        """Test close PR raises 400 when close fails."""
        from app.api.github import close_pull_request

        mock_request = MagicMock(spec=Request)

        with patch("app.api.github.check_project_access"):
            with patch("app.api.github.check_github_auth"):
                with patch("app.api.github.get_repo_for_project") as mock_get_repo:
                    mock_get_repo.return_value = "owner/repo"

                    with patch("app.api.github.github_service") as mock_github:
                        mock_github.close_pull.return_value = False

                        with pytest.raises(HTTPException) as exc_info:
                            await close_pull_request(
                                request=mock_request,
                                project_id="test-project-id",
                                number=42,
                                token="test-token"
                            )

                        assert exc_info.value.status_code == 400
                        assert "Failed to close" in exc_info.value.detail


class TestListWorkflowRunsEndpoint:
    """Test list_workflow_runs endpoint function directly."""

    @pytest.mark.asyncio
    async def test_returns_list_of_runs(self, sample_workflow_run, sample_workflow_run_2):
        """Test list runs returns a list of workflow runs."""
        from app.api.github import list_workflow_runs

        mock_request = MagicMock(spec=Request)

        with patch("app.api.github.check_project_access"):
            with patch("app.api.github.check_github_auth"):
                with patch("app.api.github.get_repo_for_project") as mock_get_repo:
                    mock_get_repo.return_value = "owner/repo"

                    with patch("app.api.github.github_service") as mock_github:
                        mock_github.list_runs.return_value = [sample_workflow_run, sample_workflow_run_2]

                        result = await list_workflow_runs(
                            request=mock_request,
                            project_id="test-project-id",
                            limit=10,
                            token="test-token"
                        )

                        assert len(result) == 2
                        assert result[0].id == 12345
                        assert result[1].id == 12346
                        mock_github.list_runs.assert_called_once_with("owner/repo", limit=10)

    @pytest.mark.asyncio
    async def test_caps_limit_at_50(self):
        """Test list runs caps limit at 50."""
        from app.api.github import list_workflow_runs

        mock_request = MagicMock(spec=Request)

        with patch("app.api.github.check_project_access"):
            with patch("app.api.github.check_github_auth"):
                with patch("app.api.github.get_repo_for_project") as mock_get_repo:
                    mock_get_repo.return_value = "owner/repo"

                    with patch("app.api.github.github_service") as mock_github:
                        mock_github.list_runs.return_value = []

                        await list_workflow_runs(
                            request=mock_request,
                            project_id="test-project-id",
                            limit=100,  # Over the limit
                            token="test-token"
                        )

                        mock_github.list_runs.assert_called_once_with("owner/repo", limit=50)


class TestGetWorkflowRunEndpoint:
    """Test get_workflow_run endpoint function directly."""

    @pytest.mark.asyncio
    async def test_returns_run_when_found(self, sample_workflow_run):
        """Test get run returns the workflow run when found."""
        from app.api.github import get_workflow_run

        mock_request = MagicMock(spec=Request)

        with patch("app.api.github.check_project_access"):
            with patch("app.api.github.check_github_auth"):
                with patch("app.api.github.get_repo_for_project") as mock_get_repo:
                    mock_get_repo.return_value = "owner/repo"

                    with patch("app.api.github.github_service") as mock_github:
                        mock_github.get_run.return_value = sample_workflow_run

                        result = await get_workflow_run(
                            request=mock_request,
                            project_id="test-project-id",
                            run_id=12345,
                            token="test-token"
                        )

                        assert result.id == 12345
                        assert result.name == "CI"
                        mock_github.get_run.assert_called_once_with("owner/repo", 12345)

    @pytest.mark.asyncio
    async def test_raises_404_when_not_found(self):
        """Test get run raises 404 when not found."""
        from app.api.github import get_workflow_run

        mock_request = MagicMock(spec=Request)

        with patch("app.api.github.check_project_access"):
            with patch("app.api.github.check_github_auth"):
                with patch("app.api.github.get_repo_for_project") as mock_get_repo:
                    mock_get_repo.return_value = "owner/repo"

                    with patch("app.api.github.github_service") as mock_github:
                        mock_github.get_run.return_value = None

                        with pytest.raises(HTTPException) as exc_info:
                            await get_workflow_run(
                                request=mock_request,
                                project_id="test-project-id",
                                run_id=99999,
                                token="test-token"
                            )

                        assert exc_info.value.status_code == 404
                        assert "#99999" in exc_info.value.detail


class TestRerunWorkflowEndpoint:
    """Test rerun_workflow endpoint function directly."""

    @pytest.mark.asyncio
    async def test_reruns_workflow_successfully(self):
        """Test rerun workflow reruns successfully."""
        from app.api.github import rerun_workflow

        mock_request = MagicMock(spec=Request)

        with patch("app.api.github.check_project_access"):
            with patch("app.api.github.check_github_auth"):
                with patch("app.api.github.get_repo_for_project") as mock_get_repo:
                    mock_get_repo.return_value = "owner/repo"

                    with patch("app.api.github.github_service") as mock_github:
                        mock_github.rerun_workflow.return_value = True

                        result = await rerun_workflow(
                            request=mock_request,
                            project_id="test-project-id",
                            run_id=12345,
                            token="test-token"
                        )

                        assert result["status"] == "ok"
                        assert "re-run triggered" in result["message"]
                        mock_github.rerun_workflow.assert_called_once_with("owner/repo", 12345)

    @pytest.mark.asyncio
    async def test_raises_400_when_rerun_fails(self):
        """Test rerun workflow raises 400 when rerun fails."""
        from app.api.github import rerun_workflow

        mock_request = MagicMock(spec=Request)

        with patch("app.api.github.check_project_access"):
            with patch("app.api.github.check_github_auth"):
                with patch("app.api.github.get_repo_for_project") as mock_get_repo:
                    mock_get_repo.return_value = "owner/repo"

                    with patch("app.api.github.github_service") as mock_github:
                        mock_github.rerun_workflow.return_value = False

                        with pytest.raises(HTTPException) as exc_info:
                            await rerun_workflow(
                                request=mock_request,
                                project_id="test-project-id",
                                run_id=12345,
                                token="test-token"
                            )

                        assert exc_info.value.status_code == 400
                        assert "Failed to re-run" in exc_info.value.detail


# =============================================================================
# Edge Cases and Error Handling Tests
# =============================================================================

class TestEdgeCases:
    """Test edge cases and error handling."""

    @pytest.mark.asyncio
    async def test_status_handles_http_exception(self):
        """Test status endpoint handles HTTPException from get_project_path."""
        from app.api.github import get_github_status

        mock_request = MagicMock(spec=Request)

        with patch("app.api.github.check_project_access"):
            with patch("app.api.github.github_service") as mock_github:
                mock_github.is_authenticated.return_value = True

                with patch("app.api.github.get_project_path") as mock_get_path:
                    mock_get_path.side_effect = HTTPException(
                        status_code=404,
                        detail="Project not found: test-project-id"
                    )

                    result = await get_github_status(
                        request=mock_request,
                        project_id="test-project-id",
                        token="test-token"
                    )

                    assert result.authenticated is True
                    assert "Project not found" in result.error

    def test_pr_response_serialization(self, sample_pull_request):
        """Test that PullRequestResponse can be serialized to JSON."""
        from app.api.github import pr_to_response

        response = pr_to_response(sample_pull_request)
        json_data = response.model_dump_json()

        assert '"number":42' in json_data
        assert '"title":"Add new feature"' in json_data

    def test_run_response_serialization(self, sample_workflow_run):
        """Test that WorkflowRunResponse can be serialized to JSON."""
        from app.api.github import run_to_response

        response = run_to_response(sample_workflow_run)
        json_data = response.model_dump_json()

        assert '"id":12345' in json_data
        assert '"name":"CI"' in json_data

    def test_status_response_serialization(self):
        """Test that GitHubStatusResponse can be serialized to JSON."""
        from app.api.github import GitHubStatusResponse

        status = GitHubStatusResponse(
            authenticated=True,
            repo="owner/repo",
            repo_info={"name": "repo"},
            error=None
        )
        json_data = status.model_dump_json()

        assert '"authenticated":true' in json_data
        assert '"repo":"owner/repo"' in json_data

    @pytest.mark.asyncio
    async def test_handles_empty_body_for_merge(self):
        """Test merge endpoint handles None body properly."""
        from app.api.github import merge_pull_request

        mock_request = MagicMock(spec=Request)

        with patch("app.api.github.check_project_access"):
            with patch("app.api.github.check_github_auth"):
                with patch("app.api.github.get_repo_for_project") as mock_get_repo:
                    mock_get_repo.return_value = "owner/repo"

                    with patch("app.api.github.github_service") as mock_github:
                        mock_github.merge_pull.return_value = True

                        result = await merge_pull_request(
                            request=mock_request,
                            project_id="test-project-id",
                            number=42,
                            body=None,  # No body provided
                            token="test-token"
                        )

                        assert result["status"] == "ok"
                        # Should use default "merge" method
                        mock_github.merge_pull.assert_called_once_with("owner/repo", 42, method="merge")
