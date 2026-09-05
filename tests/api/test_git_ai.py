"""
Unit tests for git AI generation endpoints (app/api/git_ai.py).

Covers the AI-powered commit-message and PR-description generators:
- commit-message happy path + markdown-fence stripping
- empty diff -> 400
- not-a-repo -> 400
- PR-description JSON parse + fallback on unparseable output
- worktree_id path resolution

Follows the conventions in tests/api/test_git.py: the git router (which
mounts the git_ai sub-router) is included into a fresh FastAPI app, auth is
overridden, and the shared git dependencies (git_service, database, settings,
generate_with_claude) are patched across every module that imports them.
"""

import pytest
from contextlib import ExitStack
from unittest.mock import patch, MagicMock, AsyncMock
from pathlib import Path
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.git import router, require_auth


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def mock_git_service():
    mock = MagicMock()
    mock.is_git_repo.return_value = True
    mock.get_diff_text.return_value = "diff --git a/x b/x\n+hello\n"
    mock.get_branch_log.return_value = (
        "abc123 feat: add thing\ndef456 fix: correct thing",
        " x | 2 +-\n 1 file changed, 1 insertion(+), 1 deletion(-)",
    )
    return mock


@pytest.fixture
def mock_database():
    return MagicMock()


@pytest.fixture
def mock_settings():
    mock = MagicMock()
    mock.workspace_dir = Path("/test/workspace")
    mock.effective_workspace_dir = mock.workspace_dir
    return mock


@pytest.fixture
def sample_project():
    return {
        "id": "test-project-id",
        "name": "Test Project",
        "path": "test-project",
        "created_at": "2024-01-15T10:30:00Z",
    }


@pytest.fixture
def sample_worktree():
    return {
        "id": "wt-123",
        "repository_id": "repo-abc",
        "branch_name": "feat/thing",
        "worktree_path": "worktrees/feat-thing",
        "status": "active",
    }


@pytest.fixture
def app():
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: "test-session-token"
    return app


def _patch_stack(
    stack,
    mock_git_service,
    mock_database,
    mock_settings,
    generate_return="feat: add thing",
    generate_side_effect=None,
):
    """Push the standard git_ai patches onto an ExitStack and return the
    AsyncMock used for generate_with_claude so tests can assert on it."""
    for mod in ("app.api.git", "app.api.git_worktrees", "app.api.git_ai"):
        stack.enter_context(patch(f"{mod}.git_service", mock_git_service))
    for mod in ("app.api.git", "app.api.git_helpers", "app.api.git_worktrees", "app.api.common"):
        stack.enter_context(patch(f"{mod}.database", mock_database))
    stack.enter_context(patch("app.api.git.settings", mock_settings))
    stack.enter_context(patch("app.core.project_utils.settings", mock_settings))
    stack.enter_context(patch("app.api.git.get_api_user_from_request", return_value=None))
    stack.enter_context(patch("app.api.common.get_api_user_from_request", return_value=None))

    gen = AsyncMock(return_value=generate_return)
    if generate_side_effect is not None:
        gen = AsyncMock(side_effect=generate_side_effect)
    stack.enter_context(patch("app.api.generation.generate_with_claude", gen))
    return gen


# =============================================================================
# generate-commit-message
# =============================================================================

class TestGenerateCommitMessage:
    """POST /api/v1/projects/{project_id}/git/generate-commit-message"""

    def test_happy_path(self, app, mock_git_service, mock_database, mock_settings, sample_project):
        mock_database.get_project.return_value = sample_project
        with ExitStack() as stack:
            gen = _patch_stack(
                stack, mock_git_service, mock_database, mock_settings,
                generate_return="feat: add the thing",
            )
            client = TestClient(app)
            resp = client.post(
                "/api/v1/projects/test-project-id/git/generate-commit-message",
                json={"include_unstaged": True},
            )

        assert resp.status_code == 200
        assert resp.json()["message"] == "feat: add the thing"
        gen.assert_awaited_once()

    def test_strips_markdown_fence(self, app, mock_git_service, mock_database, mock_settings, sample_project):
        mock_database.get_project.return_value = sample_project
        fenced = "```\nfeat: add the thing\n\n- detail one\n```"
        with ExitStack() as stack:
            _patch_stack(
                stack, mock_git_service, mock_database, mock_settings,
                generate_return=fenced,
            )
            client = TestClient(app)
            resp = client.post(
                "/api/v1/projects/test-project-id/git/generate-commit-message",
                json={},
            )

        assert resp.status_code == 200
        message = resp.json()["message"]
        assert "```" not in message
        assert message == "feat: add the thing\n\n- detail one"

    def test_empty_diff_returns_400(self, app, mock_git_service, mock_database, mock_settings, sample_project):
        mock_database.get_project.return_value = sample_project
        mock_git_service.get_diff_text.return_value = "   \n  "
        with ExitStack() as stack:
            _patch_stack(stack, mock_git_service, mock_database, mock_settings)
            client = TestClient(app)
            resp = client.post(
                "/api/v1/projects/test-project-id/git/generate-commit-message",
                json={},
            )

        assert resp.status_code == 400
        assert "No changes" in resp.json()["detail"]

    def test_not_a_repo_returns_400(self, app, mock_git_service, mock_database, mock_settings, sample_project):
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = False
        with ExitStack() as stack:
            _patch_stack(stack, mock_git_service, mock_database, mock_settings)
            client = TestClient(app)
            resp = client.post(
                "/api/v1/projects/test-project-id/git/generate-commit-message",
                json={},
            )

        assert resp.status_code == 400
        assert "not a git repository" in resp.json()["detail"]

    def test_worktree_id_path_resolution(
        self, app, mock_git_service, mock_database, mock_settings, sample_project, sample_worktree
    ):
        """worktree_id resolves the working dir via the worktree record."""
        mock_database.get_project.return_value = sample_project
        mock_database.get_worktree.return_value = sample_worktree
        with ExitStack() as stack:
            _patch_stack(
                stack, mock_git_service, mock_database, mock_settings,
                generate_return="feat: from worktree",
            )
            client = TestClient(app)
            resp = client.post(
                "/api/v1/projects/test-project-id/git/generate-commit-message?worktree_id=wt-123",
                json={},
            )

        assert resp.status_code == 200
        assert resp.json()["message"] == "feat: from worktree"
        # get_diff_text must have run against the worktree path, not the project path.
        called_dir = mock_git_service.get_diff_text.call_args.args[0]
        assert "feat-thing" in called_dir


# =============================================================================
# generate-pr-description
# =============================================================================

class TestGeneratePRDescription:
    """POST /api/v1/projects/{project_id}/git/generate-pr-description"""

    def test_parses_json(self, app, mock_git_service, mock_database, mock_settings, sample_project):
        mock_database.get_project.return_value = sample_project
        payload = '{"title": "feat: ship it", "body": "## Summary\\n- did stuff"}'
        with ExitStack() as stack:
            _patch_stack(
                stack, mock_git_service, mock_database, mock_settings,
                generate_return=payload,
            )
            client = TestClient(app)
            resp = client.post(
                "/api/v1/projects/test-project-id/git/generate-pr-description",
                json={"base_branch": "main"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "feat: ship it"
        assert "did stuff" in data["body"]

    def test_fallback_on_unparseable(self, app, mock_git_service, mock_database, mock_settings, sample_project):
        """Unparseable AI output falls back to the first commit message."""
        mock_database.get_project.return_value = sample_project
        with ExitStack() as stack:
            _patch_stack(
                stack, mock_git_service, mock_database, mock_settings,
                generate_return="this is not json at all",
            )
            client = TestClient(app)
            resp = client.post(
                "/api/v1/projects/test-project-id/git/generate-pr-description",
                json={},
            )

        assert resp.status_code == 200
        data = resp.json()
        # First commit line "abc123 feat: add thing" -> SHA prefix stripped.
        assert data["title"] == "feat: add thing"
        assert data["body"] == ""

    def test_empty_branch_log_returns_400(self, app, mock_git_service, mock_database, mock_settings, sample_project):
        mock_database.get_project.return_value = sample_project
        mock_git_service.get_branch_log.return_value = ("", "")
        with ExitStack() as stack:
            _patch_stack(stack, mock_git_service, mock_database, mock_settings)
            client = TestClient(app)
            resp = client.post(
                "/api/v1/projects/test-project-id/git/generate-pr-description",
                json={},
            )

        assert resp.status_code == 400
        assert "No commits" in resp.json()["detail"]

    def test_not_a_repo_returns_400(self, app, mock_git_service, mock_database, mock_settings, sample_project):
        mock_database.get_project.return_value = sample_project
        mock_git_service.is_git_repo.return_value = False
        with ExitStack() as stack:
            _patch_stack(stack, mock_git_service, mock_database, mock_settings)
            client = TestClient(app)
            resp = client.post(
                "/api/v1/projects/test-project-id/git/generate-pr-description",
                json={},
            )

        assert resp.status_code == 400
        assert "not a git repository" in resp.json()["detail"]
