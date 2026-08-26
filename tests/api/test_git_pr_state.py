"""
Tests for the GET /git/pr-state endpoint (app/api/git_operations.py).

pr-state is the session-scoped PR-lifecycle read that drives the frontend
button state: which open PR (if any) has its head at the current branch of the
resolved checkout, plus GitHub's lazy ``mergeable`` verdict. Coverage:

* branch→PR matching and the no-match case,
* graceful degradation (not a repo, no GitHub remote, gh not authenticated),
* server-side TTL cache behavior,
* worktree_id / session_id precedence reaching get_working_dir.
"""

import time

import pytest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.git_operations as gitops
from app.api.git_operations import router, require_auth


def _pr(number=7, url="https://github.com/o/r/pull/7", head="feat/x",
        title="Do a thing", draft=False, mergeable=True):
    return SimpleNamespace(
        number=number, url=url, head_branch=head, state="open",
        title=title, body="", author="me", created_at="t", updated_at="t",
        base_branch="main", draft=draft, mergeable=mergeable,
    )


@pytest.fixture(autouse=True)
def _clear_pr_cache():
    gitops._pr_state_cache.clear()
    yield
    gitops._pr_state_cache.clear()


@pytest.fixture
def mock_git_service():
    m = MagicMock()
    m.get_status.return_value = {
        "is_git_repo": True, "is_clean": True, "current_branch": "feat/x"
    }
    return m


@pytest.fixture
def mock_github_service():
    m = MagicMock()
    m.get_repo_from_remote.return_value = "o/r"
    m.is_authenticated.return_value = True
    m.list_pulls.return_value = [_pr()]
    return m


@pytest.fixture
def pr_app():
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: "test-token"
    return app


@pytest.fixture
def pr_client(pr_app, mock_git_service, mock_github_service):
    with ExitStack() as stack:
        stack.enter_context(patch.object(gitops, "git_service", mock_git_service))
        stack.enter_context(patch.object(gitops, "github_service", mock_github_service))
        stack.enter_context(patch.object(gitops, "check_project_access", MagicMock()))
        stack.enter_context(patch.object(gitops, "get_working_dir", MagicMock(return_value="/wd")))
        with TestClient(pr_app) as client:
            yield client


class TestPrStateEndpoint:
    def test_open_pr_matched_to_current_branch(self, pr_client):
        resp = pr_client.get("/api/v1/projects/p1/git/pr-state")
        assert resp.status_code == 200
        data = resp.json()
        assert data["current_branch"] == "feat/x"
        assert data["has_github"] is True
        assert data["open_pr"]["number"] == 7
        assert data["open_pr"]["mergeable"] is True

    def test_no_match_yields_null_pr_but_has_github(self, pr_client, mock_github_service):
        mock_github_service.list_pulls.return_value = [_pr(head="other-branch")]
        data = pr_client.get("/api/v1/projects/p1/git/pr-state").json()
        assert data["has_github"] is True
        assert data["open_pr"] is None

    def test_mergeable_unknown_passes_through_as_none(self, pr_client, mock_github_service):
        mock_github_service.list_pulls.return_value = [_pr(mergeable=None)]
        data = pr_client.get("/api/v1/projects/p1/git/pr-state").json()
        assert data["open_pr"]["mergeable"] is None

    def test_not_a_repo_degrades(self, pr_client, mock_git_service):
        mock_git_service.get_status.return_value = {"is_git_repo": False}
        data = pr_client.get("/api/v1/projects/p1/git/pr-state").json()
        assert data == {"current_branch": None, "has_github": False, "open_pr": None}

    def test_no_remote_degrades(self, pr_client, mock_github_service):
        mock_github_service.get_repo_from_remote.return_value = None
        data = pr_client.get("/api/v1/projects/p1/git/pr-state").json()
        assert data == {"current_branch": "feat/x", "has_github": False, "open_pr": None}

    def test_gh_unauthenticated_degrades(self, pr_client, mock_github_service):
        mock_github_service.is_authenticated.return_value = False
        data = pr_client.get("/api/v1/projects/p1/git/pr-state").json()
        assert data["has_github"] is False

    def test_gh_failure_does_not_500(self, pr_client, mock_github_service):
        """A gh hiccup degrades to 'repo known, PR state unknown' — never a 500,
        and never cached (the next call retries)."""
        mock_github_service.list_pulls.side_effect = RuntimeError("gh exploded")
        resp = pr_client.get("/api/v1/projects/p1/git/pr-state")
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_github"] is True
        assert data["open_pr"] is None
        assert not gitops._pr_state_cache  # failure not cached

    def test_scope_ids_reach_working_dir(self, pr_client):
        resp = pr_client.get(
            "/api/v1/projects/p1/git/pr-state?worktree_id=wt-1&session_id=ses-1"
        )
        assert resp.status_code == 200
        gitops.get_working_dir.assert_called_with("p1", "wt-1", "ses-1")


class TestPrStateCache:
    def test_ttl_cache_serves_repeat_calls_without_refetch(self, pr_client, mock_github_service):
        first = pr_client.get("/api/v1/projects/p1/git/pr-state")
        assert first.json()["open_pr"]["number"] == 7
        # Second call within TTL: no new gh traffic, same answer even though
        # the underlying data changed.
        mock_github_service.list_pulls.return_value = []
        second = pr_client.get("/api/v1/projects/p1/git/pr-state")
        assert second.json()["open_pr"]["number"] == 7
        # list_pulls called exactly once across both requests.
        assert mock_github_service.list_pulls.call_count == 1

    def test_expired_entry_refetches(self, pr_client, mock_github_service):
        pr_client.get("/api/v1/projects/p1/git/pr-state")
        # Age out every cached entry.
        for key in gitops._pr_state_cache:
            exp, val = gitops._pr_state_cache[key]
            gitops._pr_state_cache[key] = (time.monotonic() - 1, val)
        mock_github_service.list_pulls.return_value = []
        data = pr_client.get("/api/v1/projects/p1/git/pr-state").json()
        assert data["open_pr"] is None

    def test_invalidate_helper_clears_entries(self, pr_client):
        pr_client.get("/api/v1/projects/p1/git/pr-state")
        assert gitops._pr_state_cache
        gitops._invalidate_pr_state_cache()
        assert not gitops._pr_state_cache

    def test_failed_lookup_is_not_cached(self, pr_client, mock_github_service):
        """A degraded lookup must not poison the cache — next call retries."""
        mock_github_service.is_authenticated.return_value = False
        first = pr_client.get("/api/v1/projects/p1/git/pr-state").json()
        assert first["has_github"] is False
        assert not gitops._pr_state_cache  # nothing cached
        mock_github_service.is_authenticated.return_value = True
        second = pr_client.get("/api/v1/projects/p1/git/pr-state").json()
        assert second["has_github"] is True
        assert second["open_pr"]["number"] == 7
