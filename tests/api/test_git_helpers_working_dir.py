"""
Tests for the cached checkout-path resolution in ``app/api/git_helpers.py``.

Finding G9: working-dir resolution ran ``database.get_worktree`` /
``database.get_session`` plus a filesystem stat **synchronously inside async
handlers**, on every git poll (~2s cadence per open tab) — a Postgres round trip
per request, on the event loop. It is now memoized with a short TTL and has an
awaitable variant that resolves in a worker thread on a miss.

Covered here:
* cache hit avoids the DB entirely,
* worktree / session / project-path resolution still behaves identically,
* provisional (fallback) resolutions are held only briefly,
* explicit invalidation, keyed and global,
* the async variant keeps the blocking resolution off the calling thread.
"""

import asyncio
import threading

import pytest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

import app.api.git_helpers as helpers


def _resolve(*args):
    """Resolve through the async helper from a synchronous test."""
    return asyncio.run(helpers.get_working_dir_async(*args))


@pytest.fixture(autouse=True)
def _clear_cache():
    helpers.invalidate_working_dir_cache()
    yield
    helpers.invalidate_working_dir_cache()


@pytest.fixture
def db():
    """Patch the database module git_helpers resolves through."""
    with patch.object(helpers, "database") as mock_db:
        mock_db.get_worktree.return_value = {"id": "wt-1", "path": "/checkouts/wt-1"}
        mock_db.get_session.return_value = {"id": "ses-1"}
        yield mock_db


@pytest.fixture
def resolvers():
    """Patch the two non-DB resolution helpers."""
    with patch.object(helpers, "get_worktree_working_dir", lambda wt: wt["path"]), \
         patch.object(helpers, "get_project_path", MagicMock(return_value="/projects/p1")), \
         patch("app.core.project_resolver.resolve_existing_cwd", return_value="/checkouts/ses-1"):
        yield


class TestResolution:
    def test_worktree_id_wins(self, db, resolvers):
        assert _resolve("p1", "wt-1", "ses-1") == "/checkouts/wt-1"

    def test_session_checkout_used_when_no_worktree(self, db, resolvers):
        assert _resolve("p1", None, "ses-1") == "/checkouts/ses-1"

    def test_falls_back_to_project_path(self, db, resolvers):
        assert _resolve("p1") == "/projects/p1"

    def test_unknown_worktree_404s_and_is_not_cached(self, db, resolvers):
        db.get_worktree.return_value = None
        with pytest.raises(HTTPException) as exc:
            _resolve("p1", "missing")
        assert exc.value.status_code == 404

        # A raised resolution leaves nothing behind to serve later.
        db.get_worktree.return_value = {"id": "missing", "path": "/checkouts/late"}
        assert _resolve("p1", "missing") == "/checkouts/late"


@pytest.mark.parametrize("worktree_id", [None, "wt-1"])
@pytest.mark.parametrize("use_async", [False, True])
def test_missing_v2_checkout_never_uses_legacy_path(db, resolvers, use_async, worktree_id):
    db.get_session.return_value = {"id": "ses-1", "project_v2_id": "pv2-1"}
    with patch("app.core.project_resolver.resolve_existing_cwd", return_value=None), \
         patch("app.core.project_resolver.resolve_cwd") as materialize:
        with pytest.raises(HTTPException) as exc:
            if use_async:
                asyncio.run(helpers.get_working_dir_async("p1", worktree_id, "ses-1"))
            else:
                _resolve("p1", worktree_id, "ses-1")
        assert exc.value.status_code == 409
        assert "checkout is not available" in exc.value.detail
        materialize.assert_not_called()
        helpers.get_project_path.assert_not_called()
        assert len(helpers._working_dir_cache) == 0
    # A later successful preparation is visible immediately (errors aren't cached).
    assert _resolve("p1", None, "ses-1") == "/checkouts/ses-1"


@pytest.mark.parametrize("use_async", [False, True])
def test_adopted_v2_session_ignores_foreign_legacy_worktree(db, resolvers, use_async):
    db.get_session.return_value = {"id": "ses-1", "project_v2_id": "pv2-1",
                                   "project_id": "foreign-project", "worktree_id": "wt-1"}
    if use_async:
        result = asyncio.run(helpers.get_working_dir_async("pv2-1", "wt-1", "ses-1"))
    else:
        result = _resolve("pv2-1", "wt-1", "ses-1")
    assert result == "/checkouts/ses-1"
    db.get_worktree.assert_not_called()


class TestCaching:
    def test_second_call_does_not_touch_the_database(self, db, resolvers):
        assert _resolve("p1", None, "ses-1") == "/checkouts/ses-1"
        assert _resolve("p1", None, "ses-1") == "/checkouts/ses-1"
        assert db.get_session.call_count == 1

    def test_cache_is_keyed_by_all_three_scope_ids(self, db, resolvers):
        _resolve("p1", None, "ses-1")
        _resolve("p1", "wt-1", None)
        _resolve("p2", None, None)
        assert len(helpers._working_dir_cache) == 3

    def test_fallback_resolution_is_only_held_briefly(self, db, resolvers):
        """A session whose checkout is not materialized yet resolves to the base
        project path — provisional, so it must not be pinned for the full TTL."""
        with patch("app.core.project_resolver.resolve_existing_cwd", return_value=None):
            assert _resolve("p1", None, "ses-1") == "/projects/p1"

        entry = helpers._working_dir_cache.peek(("p1", None, "ses-1"))
        held = entry.expires_at - entry.stored_at
        assert held == pytest.approx(helpers._WORKING_DIR_FALLBACK_TTL)
        assert held < helpers._WORKING_DIR_TTL

    def test_resolved_checkout_gets_the_full_ttl(self, db, resolvers):
        _resolve("p1", None, "ses-1")
        entry = helpers._working_dir_cache.peek(("p1", None, "ses-1"))
        assert entry.expires_at - entry.stored_at == pytest.approx(
            helpers._WORKING_DIR_TTL
        )

    def test_expired_entry_re_resolves(self, db, resolvers):
        import time as _time

        _resolve("p1", "wt-1", None)
        helpers._working_dir_cache.peek(("p1", "wt-1", None)).expires_at = (
            _time.monotonic() - 1
        )
        db.get_worktree.return_value = {"id": "wt-1", "path": "/checkouts/moved"}
        assert _resolve("p1", "wt-1", None) == "/checkouts/moved"


class TestInvalidation:
    def test_global_invalidation(self, db, resolvers):
        _resolve("p1", None, "ses-1")
        _resolve("p2", None, None)
        helpers.invalidate_working_dir_cache()
        assert len(helpers._working_dir_cache) == 0

    def test_keyed_invalidation_only_drops_matching_entries(self, db, resolvers):
        _resolve("p1", None, "ses-1")
        _resolve("p2", None, None)

        helpers.invalidate_working_dir_cache(session_id="ses-1")

        assert helpers._working_dir_cache.get_or_miss(("p1", None, "ses-1")) is helpers.CACHE_MISS
        assert helpers._working_dir_cache.get(("p2", None, None)) == "/projects/p1"

    def test_invalidation_makes_a_moved_checkout_visible_immediately(self, db, resolvers):
        assert _resolve("p1", "wt-1", None) == "/checkouts/wt-1"
        db.get_worktree.return_value = {"id": "wt-1", "path": "/checkouts/elsewhere"}
        helpers.invalidate_working_dir_cache(project_id="p1", worktree_id="wt-1")
        assert _resolve("p1", "wt-1", None) == "/checkouts/elsewhere"


class TestAsyncVariant:
    async def test_resolves_off_the_calling_thread(self, db, resolvers):
        """G9: the DB read must not run on the event loop."""
        caller = threading.current_thread().ident
        seen = {}

        def _get_session(session_id):
            seen["thread"] = threading.current_thread().ident
            return {"id": session_id}

        db.get_session.side_effect = _get_session

        result = await helpers.get_working_dir_async("p1", None, "ses-1")

        assert result == "/checkouts/ses-1"
        assert seen["thread"] != caller

    async def test_cache_hit_needs_no_thread_hop(self, db, resolvers):
        await helpers.get_working_dir_async("p1", None, "ses-1")
        db.get_session.side_effect = AssertionError("must not re-resolve")
        assert await helpers.get_working_dir_async("p1", None, "ses-1") == "/checkouts/ses-1"

    async def test_concurrent_resolutions_do_not_corrupt_the_cache(self, db, resolvers):
        results = await asyncio.gather(
            *[helpers.get_working_dir_async("p1", None, "ses-1") for _ in range(20)]
        )
        assert set(results) == {"/checkouts/ses-1"}
