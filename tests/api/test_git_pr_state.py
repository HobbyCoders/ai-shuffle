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
from collections.abc import Callable
from typing import Any

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
        base_branch="main", head_repo="o/r", draft=draft, mergeable=mergeable,
    )


@pytest.fixture(autouse=True)
def _clear_pr_cache():
    gitops._pr_state_cache.clear()
    yield
    gitops._pr_state_cache.clear()


@pytest.fixture
def mock_git_service():
    m = MagicMock()
    m.get_current_branch.return_value = "feat/x"
    return m


@pytest.fixture
def mock_github_service():
    m = MagicMock()
    m.get_repo_from_remote.return_value = "o/r"
    m.is_authenticated.return_value = True
    m.list_pulls.return_value = [_pr()]
    m.list_commit_pulls.return_value = []
    return m


@pytest.fixture
def pr_app():
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: "test-token"
    return app


@pytest.fixture
def pr_checkout(tmp_path, monkeypatch):
    from tests.git_repo_helpers import git, hermetic_git_env, init_repo, make_origin

    hermetic_git_env(monkeypatch, tmp_path)
    repo = init_repo(tmp_path / "repo")
    make_origin(tmp_path, repo)
    git(repo, "checkout", "-q", "-b", "feat/x", "origin/main")
    return repo


@pytest.fixture
def pr_client(pr_app, mock_git_service, mock_github_service, pr_checkout):
    with ExitStack() as stack:
        stack.enter_context(patch("app.api.git_owner.database.get_session", side_effect=lambda sid: {"id": sid, "project_id": "p1"}))
        stack.enter_context(patch.object(gitops, "git_service", mock_git_service))
        stack.enter_context(patch.object(gitops, "github_service", mock_github_service))
        stack.enter_context(patch.object(gitops, "check_project_access", MagicMock()))
        stack.enter_context(patch.object(gitops, "get_working_dir_async", AsyncMock(return_value=str(pr_checkout))))
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
        mock_git_service.get_current_branch.return_value = None
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

    @property
    def KEY(self):
        return f"{gitops.get_working_dir_async.return_value}:feat/x"

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


@pytest.mark.asyncio
@pytest.mark.parametrize("delay_second", [False, True])
async def test_cancelled_poll_and_invalidation_do_not_poison_shared_refresh(
    pr_app: FastAPI, mock_git_service: MagicMock, mock_github_service: MagicMock,
    monkeypatch: pytest.MonkeyPatch, delay_second: bool,
) -> None:
    import asyncio
    import threading
    import httpx

    entered = asyncio.Event()
    joined = asyncio.Event()
    allow_second = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original_shield = asyncio.shield
    original_run_git = gitops.run_git
    waiters: list[asyncio.Task[Any]] = []
    branch_reads = 0

    def shield(flight: asyncio.Task[Any]) -> asyncio.Future[Any]:
        if flight in gitops._pr_state_refreshes.values():
            waiters.append(flight)
            if len(waiters) == 2:
                joined.set()
        return original_shield(flight)

    async def run_git(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        nonlocal branch_reads
        if function is mock_git_service.get_current_branch:
            branch_reads += 1
            if branch_reads == 2 and delay_second:
                await allow_second.wait()
        return await original_run_git(function, *args, **kwargs)

    def pulls(*args: Any, **kwargs: Any) -> list[SimpleNamespace]:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        return [_pr()]

    monkeypatch.setattr(gitops, "git_service", mock_git_service)
    monkeypatch.setattr(gitops, "github_service", mock_github_service)
    monkeypatch.setattr(gitops, "check_project_access", MagicMock())
    monkeypatch.setattr(gitops, "get_working_dir_async", AsyncMock(return_value="/wd"))
    monkeypatch.setattr(gitops, "run_git", run_git)
    monkeypatch.setattr(gitops.asyncio, "shield", shield)
    mock_github_service.list_pulls.side_effect = pulls
    transport = httpx.ASGITransport(app=pr_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = asyncio.create_task(client.get("/api/v1/projects/p1/git/pr-state"))
        second = asyncio.create_task(client.get("/api/v1/projects/p1/git/pr-state"))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            allow_second.set()
            # Task creation is not a join: the second poll may still be awaiting git.
            await asyncio.wait_for(joined.wait(), 2)
            assert waiters[0] is waiters[1]
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert not waiters[0].done()  # cancellation must not reach the shared flight
            gitops._invalidate_pr_state_cache("/wd")
            release.set()
            response = await asyncio.wait_for(second, 2)
            assert response.json()["open_pr"]["number"] == 7
            assert not gitops._pr_state_cache  # invalidated flight cannot repopulate it
            assert mock_github_service.list_pulls.call_count == 1
            assert (await client.get("/api/v1/projects/p1/git/pr-state")).status_code == 200
            assert mock_github_service.list_pulls.call_count == 2
            assert gitops._pr_state_cache
        finally:
            allow_second.set()
            release.set()
            await asyncio.gather(first, second, *waiters, return_exceptions=True)


@pytest.mark.asyncio
async def test_poll_after_invalidation_caches_new_flight_before_old_one_finishes(
    pr_app: FastAPI, mock_git_service: MagicMock, mock_github_service: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import threading
    import httpx

    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def pulls(*args: Any, **kwargs: Any) -> list[SimpleNamespace]:
        if not entered.is_set():
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5)
            return [_pr(number=7)]
        return [_pr(number=8)]

    monkeypatch.setattr(gitops, "git_service", mock_git_service)
    monkeypatch.setattr(gitops, "github_service", mock_github_service)
    monkeypatch.setattr(gitops, "check_project_access", MagicMock())
    monkeypatch.setattr(gitops, "get_working_dir_async", AsyncMock(return_value="/wd"))
    mock_github_service.list_pulls.side_effect = pulls
    transport = httpx.ASGITransport(app=pr_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        old = asyncio.create_task(client.get("/api/v1/projects/p1/git/pr-state"))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            gitops._invalidate_pr_state_cache("/wd")
            fresh = await asyncio.wait_for(client.get("/api/v1/projects/p1/git/pr-state"), 2)
            assert fresh.json()["open_pr"]["number"] == 8
            assert mock_github_service.list_pulls.call_count == 2
            release.set()
            assert (await asyncio.wait_for(old, 2)).json()["open_pr"]["number"] == 7
            cached = await client.get("/api/v1/projects/p1/git/pr-state")
            assert cached.json()["open_pr"]["number"] == 8
            assert mock_github_service.list_pulls.call_count == 2
        finally:
            release.set()
            await asyncio.gather(old, return_exceptions=True)


@pytest.mark.parametrize('lookup', ['upstream', 'commit'])
@pytest.mark.parametrize('state', ['OPEN', 'MERGED'])
def test_header_finds_pr_pushed_under_another_name(
    pr_client, pr_checkout, mock_github_service, tmp_path, lookup, state,
):
    from tests.git_repo_helpers import commit_file, git

    sha = commit_file(pr_checkout, 'feature.txt', 'feature', 'feature')
    if lookup == 'commit':
        git(pr_checkout, 'branch', '--unset-upstream')
    flags = ['-u'] if lookup == 'upstream' else []
    git(pr_checkout, 'push', '-q', *flags, 'origin', 'HEAD:feat/remote')
    pr = _pr(head='feat/remote')
    pr.state, pr.head_sha = state, sha
    mock_github_service.list_pulls.side_effect = (
        lambda repo, **kw: [pr] if kw['head'] == 'feat/remote' else []
    )
    mock_github_service.list_commit_pulls.return_value = [pr]

    data = pr_client.get('/api/v1/projects/p1/git/pr-state').json()
    assert data['current_branch'] == 'feat/x'  # still the local branch
    assert data['pr_lookup_failed'] is False
    if state == 'OPEN':
        assert data['open_pr']['number'] == pr.number
        assert data['merged_head_sha'] is None
    else:
        assert data['open_pr'] is None
        assert data['merged_head_sha'] == sha
    if lookup == 'upstream':
        mock_github_service.list_commit_pulls.assert_not_called()
    else:
        mock_github_service.list_commit_pulls.assert_called_once_with('o/r', sha)


def test_header_rejects_unrelated_commit_pr(pr_client, pr_checkout, mock_github_service):
    from tests.git_repo_helpers import git

    sha = git(pr_checkout, 'rev-parse', 'HEAD')
    unrelated = _pr(head='feat/unrelated')
    unrelated.state, unrelated.head_sha = 'MERGED', 'a' * 40
    mock_github_service.list_pulls.return_value = []
    mock_github_service.list_commit_pulls.return_value = [unrelated]

    data = pr_client.get('/api/v1/projects/p1/git/pr-state').json()
    assert data['pr_lookup_failed'] is False
    assert data['open_pr'] is None
    assert data['merged_head_sha'] is None
    mock_github_service.list_commit_pulls.assert_not_called()


def test_header_commit_lookup_failure_is_not_cached(pr_client, pr_checkout, mock_github_service):
    from tests.git_repo_helpers import commit_file

    commit_file(pr_checkout, 'own.txt', 'own work', 'session commit')
    mock_github_service.list_pulls.return_value = []
    mock_github_service.list_commit_pulls.side_effect = GitHubCliError('offline')
    data = pr_client.get('/api/v1/projects/p1/git/pr-state').json()
    assert data['has_github'] is True
    assert data['pr_lookup_failed'] is True
    assert not gitops._pr_state_cache


@pytest.mark.parametrize('own_commit', [False, True])
def test_header_does_not_lookup_default_upstream(pr_client, pr_checkout, mock_github_service, own_commit):
    from tests.git_repo_helpers import commit_file, git

    assert git(pr_checkout, 'rev-parse', '--abbrev-ref', '@{u}') == 'origin/main'
    if own_commit:
        commit_file(pr_checkout, 'own.txt', 'own work', 'session commit')
    main_pr = _pr(head='main')
    main_pr.base_branch = 'release'
    main_pr.head_sha = git(pr_checkout, 'rev-parse', 'HEAD')
    mock_github_service.list_pulls.side_effect = lambda repo, **kw: [main_pr] if kw['head'] == 'main' else []
    mock_github_service.list_commit_pulls.return_value = [main_pr]
    data = pr_client.get('/api/v1/projects/p1/git/pr-state').json()
    assert data['open_pr'] is None
    assert data['merged_head_sha'] is None
    assert data['pr_lookup_failed'] is False
    assert all(c.kwargs['head'] != 'main' for c in mock_github_service.list_pulls.call_args_list)


def test_header_fallback_claims_are_scoped_per_session(pr_client, pr_checkout, mock_github_service):
    from app.db import database
    from tests.git_repo_helpers import commit_file

    sha = commit_file(pr_checkout, 'own.txt', 'own work', 'session commit')
    pr = _pr(head='feat/remote')
    pr.head_sha = sha
    mock_github_service.list_pulls.return_value = []
    mock_github_service.list_commit_pulls.return_value = [pr]
    owner = {'id': 'owner', 'project_id': 'p1', 'pr_number': pr.number, 'is_worker': False}
    with patch.object(database, 'list_sessions_by_pr_number', return_value=[owner]), \
            patch.object(gitops, 'schedule_session_pr_reconcile'):
        own = pr_client.get('/api/v1/projects/p1/git/pr-state?session_id=owner').json()
        assert own['open_pr']['number'] == pr.number
        other = pr_client.get('/api/v1/projects/p1/git/pr-state?session_id=other').json()
        assert other['open_pr'] is None
        assert other['pr_lookup_failed'] is False


@pytest.mark.parametrize('with_valid', [False, True])
def test_header_unfetched_pr_head_is_not_a_github_failure(pr_client, pr_checkout, mock_github_service, with_valid):
    from tests.git_repo_helpers import commit_file

    sha = commit_file(pr_checkout, 'own.txt', 'own work', 'session commit')
    unknown, valid = _pr(99, head='feat/remote'), _pr(12, head='feat/remote')
    unknown.head_sha, valid.head_sha = 'b' * 40, sha
    mock_github_service.list_pulls.return_value = []
    mock_github_service.list_commit_pulls.return_value = [unknown, valid] if with_valid else [unknown]
    data = pr_client.get('/api/v1/projects/p1/git/pr-state').json()
    assert data['pr_lookup_failed'] is False
    if with_valid:
        assert data['open_pr']['number'] == valid.number
    else:
        assert data['open_pr'] is None


@pytest.mark.parametrize('scoped', [False, True])
def test_pr_state_invalidation_clears_fallback_miss(pr_client, pr_checkout, mock_github_service, scoped):
    from tests.git_repo_helpers import commit_file

    sha = commit_file(pr_checkout, 'own.txt', 'own work', 'session commit')
    mock_github_service.list_pulls.return_value = []
    mock_github_service.list_commit_pulls.return_value = []
    assert pr_client.get('/api/v1/projects/p1/git/pr-state').json()['open_pr'] is None
    pr = _pr(head='feat/remote')
    pr.head_sha = sha
    mock_github_service.list_commit_pulls.return_value = [pr]
    gitops._invalidate_pr_state_cache(str(pr_checkout) if scoped else None)
    data = pr_client.get('/api/v1/projects/p1/git/pr-state').json()
    assert data['open_pr']['number'] == pr.number
    assert mock_github_service.list_commit_pulls.call_count == 2


@pytest.mark.parametrize('action', ['commit', 'push'])
@pytest.mark.parametrize('success', [False, True])
def test_commit_push_invalidate_pr_caches_only_on_success(pr_app, pr_client, pr_checkout, action, success):
    from app.api import git as git_routes
    from app.core.session_pr_state import _fallback_misses

    pr_app.include_router(git_routes.router)
    key = (str(pr_checkout), 'head', 'upstream')
    other = (str(pr_checkout) + '-other', 'head', 'upstream')
    header_key = f'{pr_checkout}:feat/x'
    _fallback_misses.set(key, True)
    _fallback_misses.set(other, True)
    gitops._pr_state_cache.set(header_key, {})
    service = MagicMock()
    service.is_git_repo.return_value = True
    service.get_status.return_value = {}
    service.get_diff_stats.return_value = {}
    service.push.return_value = (success, None if success else 'push failed')
    try:
        with patch.object(git_routes, 'check_project_access'), \
                patch.object(git_routes, 'get_working_dir_async', AsyncMock(return_value=str(pr_checkout))), \
                patch.object(git_routes, 'git_service', service), \
                patch.object(git_routes, 'stage_and_commit', AsyncMock(return_value=(success, 'abc', 'commit_failed'))):
            body = {'message': 'feature'} if action == 'commit' else {'branch': 'feat/x'}
            response = pr_client.post(f'/api/v1/projects/p1/git/{action}', json=body)
        assert response.status_code == (200 if success else 400), response.text
        assert bool(_fallback_misses.get(key)) is not success
        assert (gitops._pr_state_cache.peek(header_key) is None) is success
        assert _fallback_misses.get(other) is True  # unrelated checkout stays cached
    finally:
        _fallback_misses.invalidate(key)
        _fallback_misses.invalidate(other)


@pytest.mark.parametrize('success', [False, True])
@pytest.mark.parametrize('scoped', [False, True])
def test_create_pr_invalidates_pr_caches_only_on_success(pr_app, pr_client, pr_checkout, success, scoped):
    from app.api import github as github_routes
    from app.core.session_pr_state import _fallback_misses

    pr_app.include_router(github_routes.router)
    key = (str(pr_checkout), 'head', 'upstream')
    header_key = f'{pr_checkout}:feat/x'
    _fallback_misses.set(key, True)
    gitops._pr_state_cache.set(header_key, {})
    try:
        with patch.object(github_routes, 'check_project_access'), \
                patch.object(github_routes, 'check_github_auth', AsyncMock()), \
                patch('app.api.git_helpers.get_working_dir_async', AsyncMock(return_value=str(pr_checkout))), \
                patch.object(github_routes, 'get_repo_for_project', AsyncMock(return_value='o/r')), \
                patch.object(github_routes, 'github_service') as gh, \
                patch.object(github_routes, '_broadcast_pr_changed', AsyncMock()), \
                patch.object(github_routes, 'persist_pr_state', AsyncMock()):
            gh.get_repo_from_remote.return_value = 'o/r'
            gh.create_pull.return_value = (_pr() if success else None, 'failed')
            body = {'title': 'Feature', 'head': 'feat/x', 'base': 'main'}
            if scoped:
                body['session_id'] = 'owner'
            response = pr_client.post('/api/v1/projects/p1/github/pulls', json=body)
        assert response.status_code == (201 if success else 400), response.text
        assert bool(_fallback_misses.get(key)) is not success
        assert (gitops._pr_state_cache.peek(header_key) is None) is success
    finally:
        _fallback_misses.invalidate(key)
