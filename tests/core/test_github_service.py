"""
Unit tests for GitHub service module.

Tests cover:
- Authentication checking (is_authenticated, get_auth_info)
- Repository operations (get_repo_from_remote, get_repo_info)
- Pull request operations (list_pulls, get_pull, create_pull, merge_pull, close_pull)
- Workflow run operations (list_runs, get_run, rerun_workflow)
- Error handling and edge cases
- Timeout handling
"""

import pytest
import subprocess
import json
import time
from unittest.mock import patch, MagicMock

from app.core.github_service import (
    CACHE_MISS,
    GitHubCliError,
    GitHubService,
    PullRequest,
    TTLCache,
    WorkflowRun,
    github_service,
)


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def service():
    """Create a fresh GitHubService instance for each test."""
    return GitHubService()


@pytest.fixture
def mock_completed_process():
    """Factory for creating mock CompletedProcess objects."""
    def _create(returncode=0, stdout="", stderr=""):
        result = MagicMock(spec=subprocess.CompletedProcess)
        result.returncode = returncode
        result.stdout = stdout
        result.stderr = stderr
        return result
    return _create


@pytest.fixture
def sample_pr_data():
    """Sample pull request data as returned by gh CLI."""
    return {
        "number": 42,
        "title": "Fix bug in authentication",
        "body": "This PR fixes the auth bug",
        "state": "OPEN",
        "headRefName": "fix-auth-bug",
        "baseRefName": "main",
        "author": {"login": "testuser"},
        "url": "https://github.com/owner/repo/pull/42",
        "createdAt": "2024-01-15T10:00:00Z",
        "updatedAt": "2024-01-15T12:00:00Z",
        "mergeable": "MERGEABLE"
    }


@pytest.fixture
def sample_workflow_data():
    """Sample workflow run data as returned by gh CLI."""
    return {
        "databaseId": 12345,
        "name": "CI",
        "status": "completed",
        "conclusion": "success",
        "headBranch": "main",
        "event": "push",
        "url": "https://github.com/owner/repo/actions/runs/12345",
        "createdAt": "2024-01-15T10:00:00Z"
    }


# =============================================================================
# Dataclass Tests
# =============================================================================

class TestPullRequest:
    """Test PullRequest dataclass."""

    def test_create_pull_request(self):
        """Should create a PullRequest with all fields."""
        pr = PullRequest(
            number=1,
            title="Test PR",
            body="Test body",
            state="open",
            head_branch="feature",
            base_branch="main",
            author="user",
            url="https://github.com/owner/repo/pull/1",
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
            mergeable=True
        )

        assert pr.number == 1
        assert pr.title == "Test PR"
        assert pr.body == "Test body"
        assert pr.state == "open"
        assert pr.head_branch == "feature"
        assert pr.base_branch == "main"
        assert pr.author == "user"
        assert pr.mergeable is True

    def test_pull_request_optional_mergeable(self):
        """Mergeable should be optional and default to None."""
        pr = PullRequest(
            number=1,
            title="Test",
            body="",
            state="open",
            head_branch="feat",
            base_branch="main",
            author="user",
            url="",
            created_at="",
            updated_at=""
        )

        assert pr.mergeable is None


class TestWorkflowRun:
    """Test WorkflowRun dataclass."""

    def test_create_workflow_run(self):
        """Should create a WorkflowRun with all fields."""
        run = WorkflowRun(
            id=12345,
            name="CI",
            status="completed",
            conclusion="success",
            branch="main",
            event="push",
            url="https://github.com/owner/repo/actions/runs/12345",
            created_at="2024-01-01T00:00:00Z"
        )

        assert run.id == 12345
        assert run.name == "CI"
        assert run.status == "completed"
        assert run.conclusion == "success"
        assert run.branch == "main"
        assert run.event == "push"

    def test_workflow_run_optional_conclusion(self):
        """Conclusion should be optional for in-progress runs."""
        run = WorkflowRun(
            id=12345,
            name="CI",
            status="in_progress",
            conclusion=None,
            branch="main",
            event="push",
            url="",
            created_at=""
        )

        assert run.conclusion is None


# =============================================================================
# GitHubService Initialization Tests
# =============================================================================

class TestGitHubServiceInit:
    """Test GitHubService initialization."""

    def test_default_timeout(self, service):
        """Should have default timeout of 60 seconds."""
        assert service.timeout == 60

    def test_global_instance_exists(self):
        """Global github_service instance should exist."""
        assert github_service is not None
        assert isinstance(github_service, GitHubService)


# =============================================================================
# _run_gh Tests
# =============================================================================

class TestRunGh:
    """Test the _run_gh internal method."""

    def test_run_gh_success(self, service, mock_completed_process):
        """Should run gh command successfully."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=0,
                stdout='{"status": "ok"}'
            )

            result = service._run_gh(['auth', 'status'])

            assert result.returncode == 0
            mock_run.assert_called_once()
            call_args = mock_run.call_args
            assert call_args[0][0] == ['gh', 'auth', 'status']

    def test_run_gh_with_custom_timeout(self, service, mock_completed_process):
        """Should use custom timeout when provided."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process()

            service._run_gh(['auth', 'status'], timeout=30)

            call_kwargs = mock_run.call_args[1]
            assert call_kwargs['timeout'] == 30

    def test_run_gh_uses_default_timeout(self, service, mock_completed_process):
        """Should use default timeout when not provided."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process()

            service._run_gh(['auth', 'status'])

            call_kwargs = mock_run.call_args[1]
            assert call_kwargs['timeout'] == 60

    def test_run_gh_failure_logs_warning(self, service, mock_completed_process):
        """Should log warning when command fails."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=1,
                stderr="Error message"
            )

            with patch('app.core.github_service.logger') as mock_logger:
                result = service._run_gh(['pr', 'list'])

                assert result.returncode == 1
                mock_logger.warning.assert_called()

    def test_run_gh_timeout_raises(self, service):
        """Should raise TimeoutExpired on timeout."""
        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(
                cmd=['gh', 'auth', 'status'],
                timeout=60
            )

            with pytest.raises(subprocess.TimeoutExpired):
                service._run_gh(['auth', 'status'])

    def test_run_gh_generic_exception_raises(self, service):
        """Should raise on generic exceptions."""
        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = OSError("Command not found")

            with pytest.raises(OSError):
                service._run_gh(['auth', 'status'])


# =============================================================================
# Authentication Tests
# =============================================================================

class TestIsAuthenticated:
    """Test is_authenticated method."""

    def test_authenticated_returns_true(self, service, mock_completed_process):
        """Should return True when authenticated."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(returncode=0)

            result = service.is_authenticated()

            assert result is True

    def test_not_authenticated_returns_false(self, service, mock_completed_process):
        """Should return False when not authenticated."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(returncode=1)

            result = service.is_authenticated()

            assert result is False

    def test_exception_returns_false(self, service):
        """Should return False on exception."""
        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd=[], timeout=10)

            result = service.is_authenticated()

            assert result is False


class TestGetAuthInfo:
    """Test get_auth_info method."""

    def test_authenticated_with_username(self, service, mock_completed_process):
        """Should parse username from output."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=0,
                stdout="Logged in to github.com account testuser (keyring)"
            )

            result = service.get_auth_info()

            assert result['authenticated'] is True
            assert result['username'] == 'testuser'

    def test_authenticated_username_in_stderr(self, service, mock_completed_process):
        """Should also check stderr for username."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=0,
                stdout="",
                stderr="Logged in to github.com account anotheruser"
            )

            result = service.get_auth_info()

            assert result['authenticated'] is True
            assert result['username'] == 'anotheruser'

    def test_not_authenticated(self, service, mock_completed_process):
        """Should return unauthenticated info when not logged in."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=1,
                stderr="You are not logged in"
            )

            result = service.get_auth_info()

            assert result['authenticated'] is False
            assert result['username'] is None

    def test_exception_returns_default_info(self, service):
        """Should return default info on exception."""
        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = Exception("Network error")

            result = service.get_auth_info()

            assert result['authenticated'] is False
            assert result['username'] is None
            assert result['scopes'] == []


# =============================================================================
# Repository Operations Tests
# =============================================================================

class TestGetRepoFromRemote:
    """Test get_repo_from_remote method."""

    def test_https_url(self, service, mock_completed_process):
        """Should parse HTTPS remote URL."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=0,
                stdout="https://github.com/owner/repo.git\n"
            )

            result = service.get_repo_from_remote("/path/to/repo")

            assert result == "owner/repo"

    def test_https_url_without_git_suffix(self, service, mock_completed_process):
        """Should parse HTTPS URL without .git suffix."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=0,
                stdout="https://github.com/owner/repo\n"
            )

            result = service.get_repo_from_remote("/path/to/repo")

            assert result == "owner/repo"

    def test_ssh_url(self, service, mock_completed_process):
        """Should parse SSH remote URL."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=0,
                stdout="git@github.com:owner/repo.git\n"
            )

            result = service.get_repo_from_remote("/path/to/repo")

            assert result == "owner/repo"

    def test_ssh_url_without_git_suffix(self, service, mock_completed_process):
        """Should parse SSH URL without .git suffix."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=0,
                stdout="git@github.com:owner/repo\n"
            )

            result = service.get_repo_from_remote("/path/to/repo")

            assert result == "owner/repo"

    def test_no_remote_returns_none(self, service, mock_completed_process):
        """Should return None when no remote exists."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=1,
                stderr="fatal: No such remote 'origin'"
            )

            result = service.get_repo_from_remote("/path/to/repo")

            assert result is None

    def test_unrecognized_url_format(self, service, mock_completed_process):
        """Should return None for unrecognized URL formats."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=0,
                stdout="some-other-host.com/owner/repo\n"
            )

            result = service.get_repo_from_remote("/path/to/repo")

            assert result is None

    def test_exception_returns_none(self, service):
        """Should return None on exception."""
        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = Exception("Git not found")

            result = service.get_repo_from_remote("/path/to/repo")

            assert result is None


class TestGetRepoInfo:
    """Test get_repo_info method."""

    def test_success(self, service, mock_completed_process):
        """Should return repo info on success."""
        repo_data = {
            "name": "repo",
            "owner": {"login": "owner"},
            "description": "A test repo",
            "url": "https://github.com/owner/repo",
            "defaultBranchRef": {"name": "main"}
        }

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=0,
                stdout=json.dumps(repo_data)
            )

            result = service.get_repo_info("owner/repo")

            assert result['name'] == "repo"
            assert result['owner'] == "owner"
            assert result['description'] == "A test repo"
            assert result['url'] == "https://github.com/owner/repo"
            assert result['default_branch'] == "main"

    def test_missing_default_branch(self, service, mock_completed_process):
        """Should default to 'main' when defaultBranchRef is missing."""
        repo_data = {
            "name": "repo",
            "owner": {"login": "owner"},
            "description": None,
            "url": "https://github.com/owner/repo"
        }

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=0,
                stdout=json.dumps(repo_data)
            )

            result = service.get_repo_info("owner/repo")

            assert result['default_branch'] == "main"

    def test_not_found_returns_none(self, service, mock_completed_process):
        """Should return None when repo not found."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=1,
                stderr="Could not resolve to a Repository"
            )

            result = service.get_repo_info("owner/nonexistent")

            assert result is None

    def test_transport_failure_raises(self, service):
        """A gh failure is 'unknown', not 'no such repo' — it must raise."""
        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = Exception("Network error")

            with pytest.raises(GitHubCliError):
                service.get_repo_info("owner/repo")

    def test_unclassified_nonzero_exit_raises(self, service, mock_completed_process):
        """Non-zero exit that does not say 'not found' is a failure."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=1, stderr="HTTP 503: upstream unavailable"
            )

            with pytest.raises(GitHubCliError):
                service.get_repo_info("owner/repo")


# =============================================================================
# Pull Request Tests
# =============================================================================

class TestListPulls:
    """Test list_pulls method."""

    def test_list_open_pulls(self, service, mock_completed_process, sample_pr_data):
        """Should list open pull requests."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=0,
                stdout=json.dumps([sample_pr_data])
            )

            result = service.list_pulls("owner/repo")

            assert len(result) == 1
            pr = result[0]
            assert pr.number == 42
            assert pr.title == "Fix bug in authentication"
            assert pr.head_branch == "fix-auth-bug"
            assert pr.base_branch == "main"
            assert pr.author == "testuser"

    def test_list_multiple_pulls(self, service, mock_completed_process, sample_pr_data):
        """Should handle multiple pull requests."""
        pr2 = sample_pr_data.copy()
        pr2['number'] = 43
        pr2['title'] = "Another PR"

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=0,
                stdout=json.dumps([sample_pr_data, pr2])
            )

            result = service.list_pulls("owner/repo")

            assert len(result) == 2
            assert result[0].number == 42
            assert result[1].number == 43

    def test_list_with_state_filter(self, service, mock_completed_process):
        """Should pass state filter to gh CLI."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=0,
                stdout="[]"
            )

            service.list_pulls("owner/repo", state="closed")

            call_args = mock_run.call_args[0][0]
            assert '--state' in call_args
            state_idx = call_args.index('--state')
            assert call_args[state_idx + 1] == 'closed'

    def test_list_with_limit(self, service, mock_completed_process):
        """Should pass limit to gh CLI."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=0,
                stdout="[]"
            )

            service.list_pulls("owner/repo", limit=10)

            call_args = mock_run.call_args[0][0]
            assert '--limit' in call_args
            limit_idx = call_args.index('--limit')
            assert call_args[limit_idx + 1] == '10'

    def test_empty_list(self, service, mock_completed_process):
        """Should return empty list when no PRs."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=0,
                stdout="[]"
            )

            result = service.list_pulls("owner/repo")

            assert result == []

    def test_command_failure_raises(self, service, mock_completed_process):
        """G2: a gh failure must NOT look like 'this repo has no PRs'."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=1,
                stderr="API rate limit exceeded"
            )

            with pytest.raises(GitHubCliError) as exc_info:
                service.list_pulls("owner/repo")

            assert "rate limit" in exc_info.value.stderr

    def test_json_decode_error_raises(self, service, mock_completed_process):
        """Truncated/garbled output is a failure, not an empty repository."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=0,
                stdout="invalid json"
            )

            with pytest.raises(GitHubCliError):
                service.list_pulls("owner/repo")

    def test_transport_exception_raises(self, service):
        """A timeout/spawn failure surfaces as GitHubCliError."""
        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd=[], timeout=60)

            with pytest.raises(GitHubCliError):
                service.list_pulls("owner/repo")

    def test_empty_list_is_a_real_answer(self, service, mock_completed_process):
        """The only way to get [] is gh actually answering with []."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(returncode=0, stdout="[]")

            assert service.list_pulls("owner/repo") == []

    def test_missing_author_login(self, service, mock_completed_process, sample_pr_data):
        """Should handle missing author login gracefully."""
        sample_pr_data['author'] = {}

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=0,
                stdout=json.dumps([sample_pr_data])
            )

            result = service.list_pulls("owner/repo")

            assert len(result) == 1
            assert result[0].author == "unknown"


class TestGetPull:
    """Test get_pull method."""

    def test_get_existing_pull(self, service, mock_completed_process, sample_pr_data):
        """Should return pull request when found."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=0,
                stdout=json.dumps(sample_pr_data)
            )

            result = service.get_pull("owner/repo", 42)

            assert result is not None
            assert result.number == 42
            assert result.title == "Fix bug in authentication"

    def test_get_nonexistent_pull(self, service, mock_completed_process):
        """Should return None when PR not found."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=1,
                stderr="Could not find PR"
            )

            result = service.get_pull("owner/repo", 9999)

            assert result is None

    def test_json_decode_error_raises(self, service, mock_completed_process):
        """Garbled output is a failure, not 'the PR does not exist'."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=0,
                stdout="not json"
            )

            with pytest.raises(GitHubCliError):
                service.get_pull("owner/repo", 42)

    def test_transport_exception_raises(self, service):
        """Should raise on a subprocess failure."""
        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = Exception("Error")

            with pytest.raises(GitHubCliError):
                service.get_pull("owner/repo", 42)

    def test_unclassified_failure_raises(self, service, mock_completed_process):
        """A 5xx from GitHub is not 'no such PR'."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=1, stderr="HTTP 502: Bad gateway"
            )

            with pytest.raises(GitHubCliError):
                service.get_pull("owner/repo", 42)


class TestCreatePull:
    """Test create_pull method."""

    def test_create_pull_success(self, service, mock_completed_process, sample_pr_data):
        """Should create and return new pull request."""
        with patch('subprocess.run') as mock_run:
            # First call: create PR, second call: get PR
            mock_run.side_effect = [
                mock_completed_process(
                    returncode=0,
                    stdout="https://github.com/owner/repo/pull/42\n"
                ),
                mock_completed_process(
                    returncode=0,
                    stdout=json.dumps(sample_pr_data)
                )
            ]

            pr, error = service.create_pull(
                repo="owner/repo",
                title="Fix bug",
                body="Description",
                head="feature",
                base="main"
            )

            assert pr is not None
            assert pr.number == 42
            assert error is None

    def test_create_pull_failure(self, service, mock_completed_process):
        """Should return None on creation failure."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=1,
                stderr="Branch doesn't exist"
            )

            pr, error = service.create_pull(
                repo="owner/repo",
                title="Fix bug",
                body="Description",
                head="nonexistent",
                base="main"
            )

            assert pr is None
            assert error  # failure detail is surfaced to the caller

    def test_create_pull_no_pr_url_in_output(self, service, mock_completed_process):
        """Should return None if PR URL not found in output."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=0,
                stdout="Created successfully but no URL\n"
            )

            pr, error = service.create_pull(
                repo="owner/repo",
                title="Fix bug",
                body="Description",
                head="feature",
                base="main"
            )

            assert pr is None
            assert error  # explains why no PR was returned

    def test_create_pull_exception(self, service):
        """Should return None on exception."""
        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = Exception("Network error")

            pr, error = service.create_pull(
                repo="owner/repo",
                title="Fix bug",
                body="Description",
                head="feature",
                base="main"
            )

            assert pr is None
            assert error  # exception detail surfaced to the caller


class TestMergePull:
    """Test merge_pull method."""

    def test_merge_success(self, service, mock_completed_process):
        """Should return True on successful merge."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(returncode=0)

            success, error = service.merge_pull("owner/repo", 42)

            assert success is True
            assert error is None

    def test_merge_with_squash(self, service, mock_completed_process):
        """Should use squash method when specified."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(returncode=0)

            service.merge_pull("owner/repo", 42, method="squash")

            call_args = mock_run.call_args[0][0]
            assert '--squash' in call_args

    def test_merge_with_rebase(self, service, mock_completed_process):
        """Should use rebase method when specified."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(returncode=0)

            service.merge_pull("owner/repo", 42, method="rebase")

            call_args = mock_run.call_args[0][0]
            assert '--rebase' in call_args

    def test_merge_failure(self, service, mock_completed_process):
        """Should return False on merge failure."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=1,
                stderr="Merge conflict"
            )

            success, error = service.merge_pull("owner/repo", 42)

            assert success is False
            assert error == "Merge conflict"

    def test_merge_exception(self, service):
        """Should return False on exception."""
        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = Exception("Error")

            success, error = service.merge_pull("owner/repo", 42)

            assert success is False
            assert error  # exception detail surfaced to the caller


class TestClosePull:
    """Test close_pull method."""

    def test_close_success(self, service, mock_completed_process):
        """Should return True on successful close."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(returncode=0)

            success, error = service.close_pull("owner/repo", 42)

            assert success is True
            assert error is None

    def test_close_failure(self, service, mock_completed_process):
        """Should return False on close failure."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=1,
                stderr="PR already closed"
            )

            success, error = service.close_pull("owner/repo", 42)

            assert success is False
            assert error == "PR already closed"

    def test_close_exception(self, service):
        """Should return False on exception."""
        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = Exception("Error")

            success, error = service.close_pull("owner/repo", 42)

            assert success is False
            assert error  # exception detail surfaced to the caller


# =============================================================================
# Workflow Run Tests
# =============================================================================

class TestListRuns:
    """Test list_runs method."""

    def test_list_runs_success(self, service, mock_completed_process, sample_workflow_data):
        """Should list workflow runs."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=0,
                stdout=json.dumps([sample_workflow_data])
            )

            result = service.list_runs("owner/repo")

            assert len(result) == 1
            run = result[0]
            assert run.id == 12345
            assert run.name == "CI"
            assert run.status == "completed"
            assert run.conclusion == "success"
            assert run.branch == "main"

    def test_list_runs_with_limit(self, service, mock_completed_process):
        """Should pass limit to gh CLI."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=0,
                stdout="[]"
            )

            service.list_runs("owner/repo", limit=5)

            call_args = mock_run.call_args[0][0]
            assert '--limit' in call_args
            limit_idx = call_args.index('--limit')
            assert call_args[limit_idx + 1] == '5'

    def test_list_runs_empty(self, service, mock_completed_process):
        """Should return empty list when no runs."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=0,
                stdout="[]"
            )

            result = service.list_runs("owner/repo")

            assert result == []

    def test_list_runs_empty_is_a_real_answer(self, service, mock_completed_process):
        """[] only ever means 'no runs'."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(returncode=0, stdout="[]")

            assert service.list_runs("owner/repo") == []

    def test_list_runs_failure_raises(self, service, mock_completed_process):
        """A gh failure must not read as 'this repo has no workflow runs'."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=1,
                stderr="Error"
            )

            with pytest.raises(GitHubCliError):
                service.list_runs("owner/repo")

    def test_list_runs_json_error_raises(self, service, mock_completed_process):
        """Should raise on JSON error."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=0,
                stdout="not json"
            )

            with pytest.raises(GitHubCliError):
                service.list_runs("owner/repo")

    def test_list_runs_exception_raises(self, service):
        """Should raise on a subprocess failure."""
        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = Exception("Error")

            with pytest.raises(GitHubCliError):
                service.list_runs("owner/repo")


class TestGetRun:
    """Test get_run method."""

    def test_get_run_success(self, service, mock_completed_process, sample_workflow_data):
        """Should return workflow run when found."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=0,
                stdout=json.dumps(sample_workflow_data)
            )

            result = service.get_run("owner/repo", 12345)

            assert result is not None
            assert result.id == 12345
            assert result.name == "CI"

    def test_get_run_not_found(self, service, mock_completed_process):
        """Should return None when run not found."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=1,
                stderr="Run not found"
            )

            result = service.get_run("owner/repo", 99999)

            assert result is None

    def test_get_run_json_error_raises(self, service, mock_completed_process):
        """Should raise on JSON error."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=0,
                stdout="invalid"
            )

            with pytest.raises(GitHubCliError):
                service.get_run("owner/repo", 12345)

    def test_get_run_exception_raises(self, service):
        """Should raise on a subprocess failure."""
        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = Exception("Error")

            with pytest.raises(GitHubCliError):
                service.get_run("owner/repo", 12345)


class TestRerunWorkflow:
    """Test rerun_workflow method."""

    def test_rerun_success(self, service, mock_completed_process):
        """Should return True on successful rerun."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(returncode=0)

            result = service.rerun_workflow("owner/repo", 12345)

            assert result is True

    def test_rerun_failure(self, service, mock_completed_process):
        """Should return False on rerun failure."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=1,
                stderr="Cannot rerun"
            )

            result = service.rerun_workflow("owner/repo", 12345)

            assert result is False

    def test_rerun_exception(self, service):
        """Should return False on exception."""
        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = Exception("Error")

            result = service.rerun_workflow("owner/repo", 12345)

            assert result is False


# =============================================================================
# Edge Cases and Integration Tests
# =============================================================================

class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_empty_repo_string(self, service, mock_completed_process):
        """An invalid repo argument is a gh failure, surfaced as such."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=1,
                stderr="Invalid repository"
            )

            with pytest.raises(GitHubCliError):
                service.get_repo_info("")

    def test_special_characters_in_repo_name(self, service, mock_completed_process):
        """Should handle special characters in repo name."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=0,
                stdout=json.dumps({
                    "name": "my-repo.js",
                    "owner": {"login": "my-org"},
                    "description": None,
                    "url": "https://github.com/my-org/my-repo.js"
                })
            )

            result = service.get_repo_info("my-org/my-repo.js")

            assert result is not None
            assert result['name'] == "my-repo.js"

    def test_unicode_in_pr_title(self, service, mock_completed_process):
        """Should handle unicode in PR title."""
        # Use the full unicode codepoint - JSON encoding normalizes surrogate pairs
        rocket_emoji = "\U0001f680"
        pr_data = {
            "number": 1,
            "title": f"Fix: Handle emoji correctly {rocket_emoji}",
            "body": "Some unicode text",
            "state": "OPEN",
            "headRefName": "fix",
            "baseRefName": "main",
            "author": {"login": "user"},
            "url": "",
            "createdAt": "",
            "updatedAt": "",
            "mergeable": None
        }

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=0,
                stdout=json.dumps([pr_data])
            )

            result = service.list_pulls("owner/repo")

            assert len(result) == 1
            assert rocket_emoji in result[0].title

    def test_very_long_pr_body(self, service, mock_completed_process):
        """Should handle very long PR bodies."""
        long_body = "x" * 100000
        pr_data = {
            "number": 1,
            "title": "Long PR",
            "body": long_body,
            "state": "OPEN",
            "headRefName": "fix",
            "baseRefName": "main",
            "author": {"login": "user"},
            "url": "",
            "createdAt": "",
            "updatedAt": "",
            "mergeable": None
        }

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=0,
                stdout=json.dumps([pr_data])
            )

            result = service.list_pulls("owner/repo")

            assert len(result) == 1
            assert len(result[0].body) == 100000

    def test_null_author_in_json(self, service, mock_completed_process):
        """A null author degrades to 'unknown' — it must not eat the PR.

        Previously ``author: None`` raised inside the parse loop and the blanket
        handler turned the whole response into ``[]`` — i.e. one odd PR made the
        branch look PR-less. The parse is now null-safe, and a failure is the
        only thing that can stop a list from coming back.
        """
        pr_data = {
            "number": 1,
            "title": "Test",
            "body": "Body",
            "state": "OPEN",
            "headRefName": "fix",
            "baseRefName": "main",
            "author": None,  # This will cause an error in the parsing
            "url": "",
            "createdAt": "",
            "updatedAt": "",
            "mergeable": None
        }

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=0,
                stdout=json.dumps([pr_data])
            )

            result = service.list_pulls("owner/repo")

            assert len(result) == 1
            assert result[0].author == "unknown"
            assert result[0].number == 1

    def test_missing_optional_fields(self, service, mock_completed_process):
        """Should handle missing optional fields with defaults."""
        # Test that missing fields (not null) are handled with defaults
        pr_data = {
            "number": 1,
            "author": {"login": "user"}
            # All other fields are missing
        }

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(
                returncode=0,
                stdout=json.dumps([pr_data])
            )

            result = service.list_pulls("owner/repo")

            assert len(result) == 1
            pr = result[0]
            assert pr.number == 1
            assert pr.title == ""
            assert pr.body == ""
            assert pr.state == "unknown"
            assert pr.head_branch == ""
            assert pr.base_branch == ""
            assert pr.author == "user"


# =============================================================================
# TTLCache
# =============================================================================

class TestTTLCache:
    """The shared cache primitive behind gh-auth, pr-state and checkout paths."""

    def test_hit_within_ttl(self):
        cache = TTLCache(ttl=60)
        cache.set("k", "v")
        assert cache.get("k") == "v"
        assert cache.get_or_miss("k") == "v"
        assert "k" in cache

    def test_miss_is_distinguishable_from_a_cached_none(self):
        """False/None are legitimate values, so the miss signal is a sentinel."""
        cache = TTLCache(ttl=60)
        cache.set("k", None)
        assert cache.get_or_miss("k") is None
        assert cache.get_or_miss("absent") is CACHE_MISS

    def test_expiry(self):
        cache = TTLCache(ttl=60)
        cache.set("k", "v", ttl=0.001)
        time.sleep(0.005)
        assert cache.get_or_miss("k") is CACHE_MISS
        assert "k" not in cache

    def test_peek_returns_expired_entries_for_stale_while_revalidate(self):
        cache = TTLCache(ttl=60)
        cache.set("k", "v", ttl=0.001)
        time.sleep(0.005)
        entry = cache.peek("k")
        assert entry is not None
        assert entry.value == "v"
        assert entry.is_fresh() is False
        assert entry.age() >= 0.005

    def test_invalidate_single_key_and_all(self):
        cache = TTLCache(ttl=60)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.invalidate("a")
        assert cache.get_or_miss("a") is CACHE_MISS
        assert cache.get("b") == 2
        cache.invalidate()
        assert len(cache) == 0

    def test_invalidate_where(self):
        cache = TTLCache(ttl=60)
        cache.set("/wd:main", 1)
        cache.set("/wd:feat", 2)
        cache.set("/other:main", 3)
        dropped = cache.invalidate_where(lambda k: k.startswith("/wd:"))
        assert dropped == 2
        assert cache.get("/other:main") == 3
        assert cache.get_or_miss("/wd:main") is CACHE_MISS

    def test_bounded_by_max_entries(self):
        """Unbounded growth is the failure mode of the dict-plus-timestamp
        pattern this replaces: a per-branch key space must not leak."""
        cache = TTLCache(ttl=60, max_entries=3)
        for i in range(10):
            cache.set(f"k{i}", i)
        assert len(cache) == 3
        # The most recent writes survive; the oldest are evicted.
        assert cache.get("k9") == 9
        assert cache.get_or_miss("k0") is CACHE_MISS

    def test_expired_entries_are_evicted_before_live_ones(self):
        cache = TTLCache(ttl=60, max_entries=2)
        cache.set("stale", "x", ttl=0.001)
        time.sleep(0.005)
        cache.set("live", "y")
        cache.set("newer", "z")
        assert cache.get("live") == "y"
        assert cache.get("newer") == "z"
        assert cache.peek("stale") is None

    def test_rejects_nonsense_configuration(self):
        with pytest.raises(ValueError):
            TTLCache(ttl=0)
        with pytest.raises(ValueError):
            TTLCache(ttl=60, max_entries=0)

    def test_is_thread_safe(self):
        """Populated from to_thread workers as well as the loop."""
        import threading

        cache = TTLCache(ttl=60, max_entries=1000)
        errors = []

        def worker(base):
            try:
                for i in range(200):
                    cache.set((base, i), i)
                    cache.get((base, i))
                    cache.invalidate_where(lambda k: k == (base, i))
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors


# =============================================================================
# gh auth status caching (G1)
# =============================================================================

class TestAuthStatusCaching:
    """``gh auth status`` is a 10s-timeout subprocess asked by every PR and
    actions endpoint. It must be asked once per TTL window, not once per
    request — and an explicit login/logout must not have to wait that out."""

    def test_repeat_calls_hit_the_cache(self, service, mock_completed_process):
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(returncode=0)

            assert service.is_authenticated() is True
            assert service.is_authenticated() is True
            assert service.is_authenticated() is True

            assert mock_run.call_count == 1

    def test_refresh_forces_a_probe(self, service, mock_completed_process):
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(returncode=0)

            service.is_authenticated()
            service.is_authenticated(refresh=True)

            assert mock_run.call_count == 2

    def test_invalidate_forces_the_next_call_to_probe(self, service, mock_completed_process):
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(returncode=0)
            assert service.is_authenticated() is True

            # A real logout happened; the cached True must not survive it.
            service.invalidate_auth_cache()
            mock_run.return_value = mock_completed_process(returncode=1)

            assert service.is_authenticated() is False
            assert mock_run.call_count == 2

    def test_invalidate_can_seed_a_known_value_without_probing(
        self, service, mock_completed_process
    ):
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(returncode=1)

            # The login endpoint knows first-hand that gh is now signed in.
            service.invalidate_auth_cache(True)

            assert service.is_authenticated() is True
            assert mock_run.call_count == 0

    def test_negative_answers_are_held_briefly(self, service, mock_completed_process):
        """A transient failure must not hide a working setup for the full TTL."""
        from app.core import github_service as gh_mod

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(returncode=1)
            assert service.is_authenticated() is False

        entry = service._auth_cache.peek(service._AUTH_CACHE_KEY)
        held = entry.expires_at - entry.stored_at
        assert held == pytest.approx(gh_mod.GH_AUTH_NEGATIVE_TTL_SECONDS)
        assert held < gh_mod.GH_AUTH_TTL_SECONDS

    def test_positive_answers_are_held_for_the_full_ttl(
        self, service, mock_completed_process
    ):
        from app.core import github_service as gh_mod

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(returncode=0)
            assert service.is_authenticated() is True

        entry = service._auth_cache.peek(service._AUTH_CACHE_KEY)
        assert entry.expires_at - entry.stored_at == pytest.approx(
            gh_mod.GH_AUTH_TTL_SECONDS
        )

    def test_expired_cache_reprobes(self, service, mock_completed_process):
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(returncode=0)
            assert service.is_authenticated() is True

            entry = service._auth_cache.peek(service._AUTH_CACHE_KEY)
            entry.expires_at = time.monotonic() - 1

            mock_run.return_value = mock_completed_process(returncode=1)
            assert service.is_authenticated() is False
            assert mock_run.call_count == 2

    def test_each_service_instance_has_its_own_cache(self, mock_completed_process):
        a, b = GitHubService(), GitHubService()
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = mock_completed_process(returncode=0)
            a.is_authenticated()
            b.is_authenticated()
            assert mock_run.call_count == 2

