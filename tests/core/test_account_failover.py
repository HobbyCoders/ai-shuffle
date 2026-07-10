"""Tests for the account auto-switch-on-limit (failover) core module.

Covers the pure selection policy (``_select_next_account``), the 429 classifier,
the per-install toggle round-trip, and ``attempt_failover`` (disabled short
circuit, the happy switch+evict path, the debounce piggyback, and serialization
of concurrent callers). Runs over the pg_shim SQLite DB like
tests/core/test_claude_accounts.py.

Mock-freeze gotcha honored: ``switch_account`` / ``get_active_account_id`` are
looked up as attributes on the ``claude_accounts`` MODULE inside
account_failover (never from-imported), so patching
``app.core.claude_accounts.<attr>`` reaches the consumer's binding. The pool
getter is imported lazily inside ``_evict_pooled_session`` so patching
``app.core.session_pool.get_session_pool`` reaches it.
"""

import asyncio
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core import account_failover as af
from app.core import encryption


def _future_iso(hours=1):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _past_iso(hours=1):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _mk_account(db, aid, *, has_refresh=True, expires_at=9999999999000):
    db.create_claude_account(
        account_id=aid, name=aid, credentials="enc:x",
        credential_fingerprint="fp", expires_at=expires_at,
        has_refresh_token=has_refresh,
    )


def _snapshot(instance_id, account_id, windows, fetched_at=1000):
    from app.db import usage_snapshots as usage_db
    usage_db.upsert_usage_snapshot(
        instance_id=instance_id, windows=windows, available=True,
        generation=1, fetched_at=fetched_at, account_id=account_id,
    )


@pytest.fixture(autouse=True)
def _reset_failover_state():
    af._last_switch_at = 0.0
    yield
    af._last_switch_at = 0.0


# ---------------------------------------------------------------------------
# is_usage_limit_error
# ---------------------------------------------------------------------------

class TestIsUsageLimitError:
    def test_429_int(self):
        assert af.is_usage_limit_error({"api_error_status": 429}) is True

    def test_429_str(self):
        assert af.is_usage_limit_error({"api_error_status": "429"}) is True

    def test_500_false(self):
        assert af.is_usage_limit_error({"api_error_status": 500}) is False

    def test_none_false(self):
        assert af.is_usage_limit_error(None) is False

    def test_empty_false(self):
        assert af.is_usage_limit_error({}) is False

    def test_garbage_false(self):
        assert af.is_usage_limit_error({"api_error_status": "nope"}) is False


# ---------------------------------------------------------------------------
# toggle
# ---------------------------------------------------------------------------

class TestToggle:
    def test_default_false(self, mock_db):
        assert af.get_auto_switch_enabled() is False

    def test_round_trip(self, mock_db):
        af.set_auto_switch_enabled(True)
        assert af.get_auto_switch_enabled() is True
        af.set_auto_switch_enabled(False)
        assert af.get_auto_switch_enabled() is False


# ---------------------------------------------------------------------------
# _select_next_account
# ---------------------------------------------------------------------------

class TestSelectNextAccount:
    def test_skips_attempted(self, mock_db):
        from app.db import database as db
        _mk_account(db, "a")
        _mk_account(db, "b")
        picked = af._select_next_account({"a"})
        assert picked["id"] == "b"

    def test_none_when_all_attempted(self, mock_db):
        from app.db import database as db
        _mk_account(db, "a")
        _mk_account(db, "b")
        assert af._select_next_account({"a", "b"}) is None

    def test_drops_expired_no_refresh(self, mock_db):
        from app.db import database as db
        # a: expired + no refresh → not authenticable, dropped.
        _mk_account(db, "a", has_refresh=False, expires_at=1000)
        _mk_account(db, "b")
        picked = af._select_next_account(set())
        assert picked["id"] == "b"

    def test_none_when_only_candidate_is_dead(self, mock_db):
        from app.db import database as db
        _mk_account(db, "a", has_refresh=False, expires_at=1000)
        assert af._select_next_account(set()) is None

    def test_expired_but_has_refresh_is_eligible(self, mock_db):
        from app.db import database as db
        _mk_account(db, "a", has_refresh=True, expires_at=1000)
        picked = af._select_next_account(set())
        assert picked["id"] == "a"

    def test_prefers_non_exhausted_snapshot(self, mock_db):
        from app.db import database as db
        _mk_account(db, "b")
        _mk_account(db, "c")
        # b has headroom, c is exhausted → b preferred (rank 0 over rank 2).
        _snapshot("inst-b", "b", [{"type": "5h", "utilization": 20, "resets_at": _future_iso()}])
        _snapshot("inst-c", "c", [{"type": "5h", "utilization": 100, "resets_at": _future_iso()}])
        picked = af._select_next_account(set())
        assert picked["id"] == "b"

    def test_rejected_status_counts_as_exhausted(self, mock_db):
        from app.db import database as db
        _mk_account(db, "b")
        _mk_account(db, "c")
        _snapshot("inst-b", "b", [{"type": "5h", "utilization": 10, "resets_at": _future_iso()}])
        _snapshot("inst-c", "c", [{"type": "5h", "status": "rejected", "resets_at": _future_iso()}])
        picked = af._select_next_account(set())
        assert picked["id"] == "b"

    def test_unknown_ranks_above_known_exhausted(self, mock_db):
        from app.db import database as db
        _mk_account(db, "b")  # no snapshot → rank 1
        _mk_account(db, "c")  # exhausted snapshot → rank 2
        _snapshot("inst-c", "c", [{"type": "5h", "utilization": 100, "resets_at": _future_iso()}])
        picked = af._select_next_account(set())
        assert picked["id"] == "b"

    def test_exhausted_window_in_past_is_not_blocking(self, mock_db):
        from app.db import database as db
        _mk_account(db, "c")
        # utilization 100 but the window already reset (resets_at in the past)
        # → not currently exhausting → rank 0.
        _snapshot("inst-c", "c", [{"type": "5h", "utilization": 100, "resets_at": _past_iso()}])
        picked = af._select_next_account(set())
        assert picked["id"] == "c"

    def test_lru_tie_break(self, mock_db):
        from app.db import database as db
        _mk_account(db, "b")
        _mk_account(db, "c")
        # Both lack snapshots (rank 1). Touch b first (older), then c → b is LRU.
        db.touch_claude_account_last_used("b")
        time.sleep(0.01)
        db.touch_claude_account_last_used("c")
        picked = af._select_next_account(set())
        assert picked["id"] == "b"


# ---------------------------------------------------------------------------
# attempt_failover
# ---------------------------------------------------------------------------

def _seed_two_and_activate(db):
    """Two accounts with a valid encryption key and active pointer at 'a'."""
    from app.core import claude_accounts as svc
    encryption.set_encryption_key("pw", db)
    _mk_account(db, "a")
    _mk_account(db, "b")
    svc._set_active_account_id("a")


class TestAttemptFailover:
    async def test_disabled_short_circuits(self, mock_db):
        from app.db import database as db
        _seed_two_and_activate(db)
        # toggle left OFF
        with patch("app.core.claude_accounts.switch_account") as sw:
            decision = await af.attempt_failover("sess-1", None, set())
        assert decision.retry is False
        sw.assert_not_called()

    async def test_success_switches_and_evicts(self, mock_db):
        from app.db import database as db
        _seed_two_and_activate(db)
        af.set_auto_switch_enabled(True)

        pool = MagicMock()
        pool.remove_broken = AsyncMock()
        attempted: set = set()

        with patch("app.core.claude_accounts.switch_account",
                   return_value={"success": True, "active_account_id": "b"}) as sw, \
             patch("app.core.session_pool.get_session_pool", return_value=pool), \
             patch.object(af, "_notify", new=AsyncMock()), \
             patch.object(af, "schedule_usage_refresh"):
            decision = await af.attempt_failover("sess-1", None, attempted)

        assert decision.retry is True
        assert decision.switched is True
        assert decision.account_id == "b"
        sw.assert_called_once_with("b")
        pool.remove_broken.assert_awaited_once_with("sess-1")
        # Both the outgoing active and the new target are recorded so the
        # caller's retry loop terminates.
        assert attempted == {"a", "b"}

    async def test_exhausted_returns_no_retry(self, mock_db):
        from app.db import database as db
        _seed_two_and_activate(db)
        af.set_auto_switch_enabled(True)

        # Everything already attempted → no candidate.
        with patch("app.core.claude_accounts.switch_account") as sw, \
             patch.object(af, "_notify", new=AsyncMock()) as notify:
            decision = await af.attempt_failover("sess-1", None, {"a", "b"})

        assert decision.retry is False
        sw.assert_not_called()
        notify.assert_awaited()  # emits the "all accounts exhausted" warning

    async def test_debounce_piggyback_single_switch(self, mock_db):
        """Two back-to-back calls from DIFFERENT sessions rotate creds once; the
        second piggybacks on the freshly-active account."""
        from app.db import database as db
        _seed_two_and_activate(db)
        af.set_auto_switch_enabled(True)

        pool = MagicMock()
        pool.remove_broken = AsyncMock()

        with patch("app.core.claude_accounts.switch_account",
                   return_value={"success": True, "active_account_id": "b"}) as sw, \
             patch("app.core.session_pool.get_session_pool", return_value=pool), \
             patch.object(af, "_notify", new=AsyncMock()), \
             patch.object(af, "schedule_usage_refresh"):
            d1 = await af.attempt_failover("sess-1", None, set())
            d2 = await af.attempt_failover("sess-2", None, set())

        assert d1.retry is True and d2.retry is True
        assert d1.switched is True
        assert d2.switched is False  # piggybacked, no re-switch
        sw.assert_called_once()

    async def test_piggyback_records_attempt_no_repeat(self, mock_db):
        """A piggybacked account counts as attempted: if the piggyback retry
        also 429s, the next call for the SAME turn (same ``attempted`` set)
        must switch instead of piggybacking onto the same exhausted account
        until the debounce window expires."""
        from app.db import database as db
        _seed_two_and_activate(db)  # active = "a"
        af.set_auto_switch_enabled(True)
        af._last_switch_at = time.monotonic()  # a concurrent switch just happened

        pool = MagicMock()
        pool.remove_broken = AsyncMock()
        attempted = set()

        with patch("app.core.claude_accounts.switch_account",
                   return_value={"success": True, "active_account_id": "b"}) as sw, \
             patch("app.core.session_pool.get_session_pool", return_value=pool), \
             patch.object(af, "_notify", new=AsyncMock()), \
             patch.object(af, "schedule_usage_refresh"):
            d1 = await af.attempt_failover("sess-1", None, attempted)
            d2 = await af.attempt_failover("sess-1", None, attempted)

        assert d1.retry is True and d1.switched is False  # piggyback on "a"
        assert "a" in attempted  # the piggybacked account was recorded
        assert d2.retry is True and d2.switched is True  # real switch, no repeat piggyback
        sw.assert_called_once_with("b")

    async def test_concurrent_callers_serialize(self, mock_db):
        """Concurrent callers (asyncio.gather) must serialize through the lock —
        exactly one switch."""
        from app.db import database as db
        _seed_two_and_activate(db)
        af.set_auto_switch_enabled(True)

        pool = MagicMock()
        pool.remove_broken = AsyncMock()

        with patch("app.core.claude_accounts.switch_account",
                   return_value={"success": True, "active_account_id": "b"}) as sw, \
             patch("app.core.session_pool.get_session_pool", return_value=pool), \
             patch.object(af, "_notify", new=AsyncMock()), \
             patch.object(af, "schedule_usage_refresh"):
            results = await asyncio.gather(
                af.attempt_failover("sess-1", None, set()),
                af.attempt_failover("sess-2", None, set()),
            )

        assert all(r.retry for r in results)
        sw.assert_called_once()

    async def test_switch_failure_no_retry(self, mock_db):
        from app.db import database as db
        _seed_two_and_activate(db)
        af.set_auto_switch_enabled(True)

        with patch("app.core.claude_accounts.switch_account",
                   return_value={"success": False, "error": "decrypt_failed"}) as sw, \
             patch.object(af, "_notify", new=AsyncMock()) as notify:
            decision = await af.attempt_failover("sess-1", None, set())

        assert decision.retry is False
        sw.assert_called_once_with("b")
        # The generic 429 toast was suppressed upstream, so a failed switch must
        # surface its own warning rather than leaving the user in silence.
        notify.assert_awaited_once()
        assert notify.await_args.kwargs.get("severity") == "warning"

    async def test_single_account_toggle_on_silent_no_retry(self, mock_db):
        """A single-account install with the toggle ON must return retry=False
        WITHOUT emitting the 'all accounts exhausted' toast — the unified
        eligibility gate (>=2 accounts) short-circuits before any notify, so only
        the generic upstream rate-limit toast shows (no contradiction)."""
        from app.db import database as db
        from app.core import claude_accounts as svc
        encryption.set_encryption_key("pw", db)
        _mk_account(db, "a")  # only ONE account
        svc._set_active_account_id("a")
        af.set_auto_switch_enabled(True)

        with patch("app.core.claude_accounts.switch_account") as sw, \
             patch.object(af, "_notify", new=AsyncMock()) as notify:
            decision = await af.attempt_failover("sess-1", None, set())

        assert decision.retry is False
        sw.assert_not_called()
        notify.assert_not_awaited()  # no exhausted toast


# ---------------------------------------------------------------------------
# execute_query_with_failover
# ---------------------------------------------------------------------------

class TestExecuteQueryWithFailover:
    async def test_happy_path_no_failover(self, mock_db):
        """A clean (non-429) result returns immediately; attempt_failover is
        never consulted and the user message is persisted (persist=True)."""
        result = {"session_id": "sess-1", "metadata": {"api_error_status": None}}
        # Mock-freeze gotcha: execute_query is looked up lazily inside the
        # function via ``from app.core.query_engine import execute_query``, so we
        # patch it where it's DEFINED (the query_engine module attribute).
        eq = AsyncMock(return_value=result)
        with patch("app.core.query_engine.execute_query", new=eq), \
             patch.object(af, "attempt_failover", new=AsyncMock()) as af_mock:
            out = await af.execute_query_with_failover(
                prompt="hi", profile_id="claude-code", session_id="sess-1",
            )

        assert out is result
        af_mock.assert_not_awaited()
        assert eq.await_args.kwargs.get("persist_user_message") is True

    async def test_retry_then_success(self, mock_db):
        """First result 429s and failover says retry → re-run with
        persist_user_message=False, tracking the new session_id; the second
        (clean) result is returned."""
        first = {"session_id": "sess-1", "metadata": {"api_error_status": 429}}
        second = {"session_id": "sess-2", "metadata": {"api_error_status": None}}
        eq = AsyncMock(side_effect=[first, second])
        decision = MagicMock(retry=True)

        async def _attempt(session_id, api_user_id, attempted):
            # The real attempt_failover mutates ``attempted`` (that's what drives
            # persist_user_message=not attempted on the retry) — mirror that.
            attempted.add("a")
            return decision

        with patch("app.core.query_engine.execute_query", new=eq), \
             patch.object(af, "attempt_failover",
                          new=AsyncMock(side_effect=_attempt)) as af_mock:
            out = await af.execute_query_with_failover(
                prompt="hi", profile_id="claude-code", session_id="sess-1",
            )

        assert out is second
        assert eq.await_count == 2
        # First call persists the user message; the retry does not.
        assert eq.await_args_list[0].kwargs.get("persist_user_message") is True
        assert eq.await_args_list[1].kwargs.get("persist_user_message") is False
        # The retry resumed the session_id carried out of the first result.
        assert eq.await_args_list[1].kwargs.get("session_id") == "sess-1"
        af_mock.assert_awaited_once()

    async def test_retry_declined_returns_failed_result(self, mock_db):
        """A 429 result where failover declines (retry=False) returns that
        result without re-running execute_query."""
        first = {"session_id": "sess-1", "metadata": {"api_error_status": 429}}
        eq = AsyncMock(return_value=first)
        decision = MagicMock(retry=False)
        with patch("app.core.query_engine.execute_query", new=eq), \
             patch.object(af, "attempt_failover",
                          new=AsyncMock(return_value=decision)):
            out = await af.execute_query_with_failover(
                prompt="hi", profile_id="claude-code", session_id="sess-1",
            )

        assert out is first
        assert eq.await_count == 1
