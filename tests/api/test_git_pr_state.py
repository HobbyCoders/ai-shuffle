"""
Tests for the GET /git/pr-state endpoint (app/api/git_operations.py).

pr-state is the session-scoped PR-lifecycle read that drives the frontend
button state: which open PR (if any) has its head at the current branch of the
resolved checkout, plus GitHub's lazy ``mergeable`` verdict. Coverage:

* branch→PR matching and the no-match case,
* graceful degradation (not a repo, no GitHub remote, gh not authenticated),
* server-side TTL cache behavior,
* failure-vs-empty (finding G2): a gh failure is reported as
  ``pr_lookup_failed`` and never cached as "no PR", and a previously known-good
  answer is re-served flagged ``stale`` instead of being replaced with null,
* worktree_id / session_id precedence reaching the checkout resolver.
"""

import time

import pytest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.git_operations as gitops
from app.api.git_operations import router, require_auth
from app.core.github_service import GitHubCliError


def _expire(cache_key: str) -> None:
    """Age the cached entry out without waiting for the TTL."""
    entry = gitops._pr_state_cache.peek(cache_key)
    assert entry is not None
    entry.expires_at = time.monotonic() - 1


def _expire_all() -> None:
    for key in gitops._pr_state_cache.keys():
        _expire(key)


def _age(cache_key: str, seconds: float) -> None:
    """Make the cached entry look *seconds* old (and expired)."""
    entry = gitops._pr_state_cache.peek(cache_key)
    assert entry is not None
    entry.stored_at = time.monotonic() - seconds
    entry.expires_at = time.monotonic() - 1


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
        stack.enter_context(patch("app.api.git_owner.database.get_session", side_effect=lambda sid: {"id": sid, "project_id": "p1"}))
        stack.enter_context(patch.object(gitops, "git_service", mock_git_service))
        stack.enter_context(patch.object(gitops, "github_service", mock_github_service))
        stack.enter_context(patch.object(gitops, "check_project_access", MagicMock()))
        stack.enter_context(patch.object(gitops, "get_working_dir_async", AsyncMock(return_value="/wd")))
        with TestClient(pr_app) as client:
            yield client




class TestMergedHead:
    SHA = "a" * 40

    def merged(self):
        pr = _pr()
        pr.state = "MERGED"
        pr.head_sha = self.SHA
        return pr

    def test_returns_original_head_from_branch_filtered_merged_lookup(self, pr_client, mock_github_service):
        mock_github_service.list_pulls.side_effect = [[], [self.merged()]]
        data = pr_client.get("/api/v1/projects/p1/git/pr-state").json()
        assert data["merged_head_sha"] == self.SHA
        assert data["open_pr"] is None
        assert data["pr_lookup_failed"] is False
        mock_github_service.list_pulls.assert_called_with("o/r", state="merged", head="feat/x", limit=1)

    def test_open_pr_takes_priority_without_merged_lookup(self, pr_client, mock_github_service):
        data = pr_client.get("/api/v1/projects/p1/git/pr-state").json()
        assert data["open_pr"]["number"] == 7
        assert data["merged_head_sha"] is None
        assert mock_github_service.list_pulls.call_count == 1

    def test_unmerged_branch_has_no_merged_head(self, pr_client, mock_github_service):
        mock_github_service.list_pulls.return_value = []
        data = pr_client.get("/api/v1/projects/p1/git/pr-state").json()
        assert data["merged_head_sha"] is None
        assert data["pr_lookup_failed"] is False

    def test_failed_merged_lookup_preserves_cached_head(self, pr_client, mock_github_service):
        mock_github_service.list_pulls.side_effect = [[], [self.merged()]]
        pr_client.get("/api/v1/projects/p1/git/pr-state")
        _expire_all()
        mock_github_service.list_pulls.side_effect = [[], GitHubCliError("offline")]
        data = pr_client.get("/api/v1/projects/p1/git/pr-state").json()
        assert data["merged_head_sha"] == self.SHA
        assert data["stale"] is True
        assert data["pr_lookup_failed"] is True

    def test_failed_merged_lookup_is_not_cached_as_no_pr(self, pr_client, mock_github_service):
        mock_github_service.list_pulls.side_effect = [[], GitHubCliError("offline")]
        data = pr_client.get("/api/v1/projects/p1/git/pr-state").json()
        assert data["pr_lookup_failed"] is True
        assert not gitops._pr_state_cache

    def test_new_open_pr_replaces_merged_head_after_refresh(self, pr_client, mock_github_service):
        mock_github_service.list_pulls.side_effect = [[], [self.merged()]]
        pr_client.get("/api/v1/projects/p1/git/pr-state")
        _expire_all()
        mock_github_service.list_pulls.side_effect = None
        mock_github_service.list_pulls.return_value = [_pr(number=9)]
        data = pr_client.get("/api/v1/projects/p1/git/pr-state").json()
        assert data["open_pr"]["number"] == 9
        assert data["merged_head_sha"] is None


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
        assert data == {
            "current_branch": None,
            "has_github": False,
            "open_pr": None,
            "merged_head_sha": None,
            "pr_lookup_failed": False,
            "stale": False,
        }

    def test_no_remote_degrades(self, pr_client, mock_github_service):
        mock_github_service.get_repo_from_remote.return_value = None
        data = pr_client.get("/api/v1/projects/p1/git/pr-state").json()
        assert data == {
            "current_branch": "feat/x",
            "has_github": False,
            "open_pr": None,
            "merged_head_sha": None,
            "pr_lookup_failed": False,
            "stale": False,
        }

    def test_successful_lookup_is_not_flagged(self, pr_client):
        data = pr_client.get("/api/v1/projects/p1/git/pr-state").json()
        assert data["pr_lookup_failed"] is False
        assert data["stale"] is False

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
        # The load-bearing part of G2: "we could not ask" is reported as such,
        # so the client does not render it as "there is no PR".
        assert data["pr_lookup_failed"] is True
        assert data["stale"] is False
        assert not gitops._pr_state_cache  # failure not cached

    def test_gh_cli_error_is_flagged_not_swallowed(self, pr_client, mock_github_service):
        """The typed failure from github_service takes the same path."""
        mock_github_service.list_pulls.side_effect = GitHubCliError(
            "gh pr list failed", stderr="API rate limit exceeded"
        )
        data = pr_client.get("/api/v1/projects/p1/git/pr-state").json()
        assert data["pr_lookup_failed"] is True
        assert data["open_pr"] is None
        assert not gitops._pr_state_cache

    def test_scope_ids_reach_working_dir(self, pr_client):
        resp = pr_client.get(
            "/api/v1/projects/p1/git/pr-state?worktree_id=wt-1&session_id=ses-1"
        )
        assert resp.status_code == 200
        gitops.get_working_dir_async.assert_called_with("p1", "wt-1", "ses-1")


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
        _expire_all()
        mock_github_service.list_pulls.return_value = []
        data = pr_client.get("/api/v1/projects/p1/git/pr-state").json()
        assert data["open_pr"] is None
        assert data["stale"] is False
        assert data["pr_lookup_failed"] is False

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


class TestStaleWhileRevalidate:
    """G2: a failed refresh serves the last known-good answer, flagged stale —
    it never replaces a real PR with ``open_pr: None`` (the Merge-PR → Create-PR
    → Merge-PR flip), and it never writes that negative answer to the cache."""

    KEY = "/wd:feat/x"

    def test_failed_refresh_serves_last_known_good_flagged_stale(
        self, pr_client, mock_github_service
    ):
        first = pr_client.get("/api/v1/projects/p1/git/pr-state").json()
        assert first["open_pr"]["number"] == 7

        _expire(self.KEY)
        mock_github_service.list_pulls.side_effect = GitHubCliError("gh is down")

        second = pr_client.get("/api/v1/projects/p1/git/pr-state").json()
        assert second["open_pr"]["number"] == 7      # NOT None
        assert second["stale"] is True
        assert second["pr_lookup_failed"] is True
        assert second["has_github"] is True

    def test_stale_service_does_not_refresh_the_cache(
        self, pr_client, mock_github_service
    ):
        """The stale answer is served, not re-cached: the very next poll retries
        instead of being pinned to a stale value for another TTL."""
        pr_client.get("/api/v1/projects/p1/git/pr-state")
        _expire(self.KEY)
        mock_github_service.list_pulls.side_effect = GitHubCliError("gh is down")
        pr_client.get("/api/v1/projects/p1/git/pr-state")

        assert gitops._pr_state_cache.peek(self.KEY).is_fresh() is False
        # Recovery: gh answers again and the fresh value wins immediately.
        mock_github_service.list_pulls.side_effect = None
        mock_github_service.list_pulls.return_value = [_pr(number=9)]
        third = pr_client.get("/api/v1/projects/p1/git/pr-state").json()
        assert third["open_pr"]["number"] == 9
        assert third["stale"] is False
        assert third["pr_lookup_failed"] is False

    def test_stale_answer_expires_eventually(self, pr_client, mock_github_service):
        """Past the staleness ceiling the state is reported as unknown rather
        than serving something arbitrarily old."""
        pr_client.get("/api/v1/projects/p1/git/pr-state")
        _age(self.KEY, gitops._PR_STATE_STALE_MAX_SECONDS + 1)
        mock_github_service.list_pulls.side_effect = GitHubCliError("gh is down")

        data = pr_client.get("/api/v1/projects/p1/git/pr-state").json()
        assert data["open_pr"] is None
        assert data["stale"] is False
        assert data["pr_lookup_failed"] is True

    def test_stale_no_pr_is_also_preserved(self, pr_client, mock_github_service):
        """A known-good "no PR" is equally worth preserving across a hiccup."""
        mock_github_service.list_pulls.return_value = []
        first = pr_client.get("/api/v1/projects/p1/git/pr-state").json()
        assert first["open_pr"] is None
        assert first["pr_lookup_failed"] is False

        _expire(self.KEY)
        mock_github_service.list_pulls.side_effect = GitHubCliError("gh is down")
        second = pr_client.get("/api/v1/projects/p1/git/pr-state").json()
        assert second["open_pr"] is None
        assert second["stale"] is True
        assert second["pr_lookup_failed"] is True


class TestPrStateOffLoop:
    """G1: the pr-state poll must not run gh/git on the event loop.

    ``check_project_access`` runs inline in the handler, so the thread it sees
    IS the event-loop thread; every blocking gh call must be on another one.
    """

    def test_blocking_calls_run_in_worker_threads(
        self, pr_app, mock_git_service, mock_github_service
    ):
        import threading
        from contextlib import ExitStack
        from fastapi.testclient import TestClient

        threads = {}

        def _record(name, value):
            def _inner(*args, **kwargs):
                threads.setdefault(name, set()).add(threading.current_thread().ident)
                return value
            return _inner

        mock_github_service.get_repo_from_remote.side_effect = _record("remote", "o/r")
        mock_github_service.is_authenticated.side_effect = _record("auth", True)
        mock_github_service.list_pulls.side_effect = _record("list_pulls", [_pr()])

        with ExitStack() as stack:
            stack.enter_context(patch.object(gitops, "git_service", mock_git_service))
            stack.enter_context(patch.object(gitops, "github_service", mock_github_service))
            stack.enter_context(
                patch.object(
                    gitops,
                    "check_project_access",
                    MagicMock(side_effect=_record("loop", None)),
                )
            )
            stack.enter_context(
                patch.object(gitops, "get_working_dir_async", AsyncMock(return_value="/wd"))
            )
            with TestClient(pr_app) as client:
                assert client.get("/api/v1/projects/p1/git/pr-state").status_code == 200

        loop_thread = threads["loop"]
        assert len(loop_thread) == 1
        for name in ("remote", "auth", "list_pulls"):
            assert threads[name], f"{name} was never called"
            assert not (threads[name] & loop_thread), (
                f"{name} ran on the event-loop thread"
            )


class TestSidebarRecovery:
    def test_session_poll_repairs_sidebar_even_on_header_cache_hit(self, pr_client):
        with patch.object(gitops, "schedule_session_pr_reconcile") as schedule:
            for _ in range(2):
                response = pr_client.get(
                    "/api/v1/projects/p1/git/pr-state?session_id=s"
                )
                assert response.status_code == 200
            assert schedule.call_count == 2
            schedule.assert_called_with("s")

    def test_project_poll_does_not_schedule_session_reconcile(self, pr_client):
        with patch.object(gitops, "schedule_session_pr_reconcile") as schedule:
            assert pr_client.get("/api/v1/projects/p1/git/pr-state").status_code == 200
            schedule.assert_not_called()

    def test_non_github_session_does_not_schedule_refresh(self, pr_client, mock_github_service):
        mock_github_service.get_repo_from_remote.return_value = None
        with patch.object(gitops, "schedule_session_pr_reconcile") as schedule:
            response = pr_client.get("/api/v1/projects/p1/git/pr-state?session_id=s")
            assert response.status_code == 200
            schedule.assert_not_called()
