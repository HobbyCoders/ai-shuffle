"""Tests for the account_failover module AFTER native auto-switch was retired.

OC-1 retired native-Claude automatic account failover. What remains:

* the 429 classifier (:func:`is_usage_limit_error`),
* :func:`attempt_failover`, which no longer rotates accounts — it surfaces the
  rate-limited state (OC-4), and
* :func:`execute_query_with_failover`, which now runs the query exactly once and
  routes a 429 through ``attempt_failover`` for the notification (no retry).

Mock-freeze gotcha honored: ``execute_query`` is looked up lazily inside
``execute_query_with_failover`` via ``from app.core.query_engine import
execute_query``, and ``emit_notification`` is imported lazily inside ``_notify``
— both are patched where they are DEFINED so the consumer's binding is reached.
"""

from unittest.mock import AsyncMock, patch

from app.core import account_failover as af


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
# attempt_failover — no rotation; surfaces the rate-limited state
# ---------------------------------------------------------------------------

class TestAttemptFailover:
    async def test_no_rotation_returns_nothing(self):
        """A usage limit no longer rotates accounts: the seam only notifies."""
        with patch.object(af, "_notify", new=AsyncMock()):
            assert await af.attempt_failover("sess-1", "user-1") is None

    async def test_notification_is_rate_limited_warning(self):
        """The single user-facing notification is the OC-4 'wait or switch'
        warning, scoped to the caller's user + session."""
        with patch.object(af, "_notify", new=AsyncMock()) as notify:
            await af.attempt_failover("sess-1", "user-1")
        notify.assert_awaited_once()
        assert notify.await_args.args[:2] == ("user-1", "sess-1")
        kwargs = notify.await_args.kwargs
        assert kwargs.get("severity") == "warning"
        msg = kwargs.get("message", "").lower()
        assert "wait" in msg and "account" in msg

    async def test_seam_survives_notification_failure(self):
        """A notification-funnel failure is swallowed by ``_notify``; the seam
        still returns cleanly."""
        with patch(
            "app.services.notification_service.emit_notification",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            assert await af.attempt_failover("sess-1", None) is None


# ---------------------------------------------------------------------------
# execute_query_with_failover — single pass; no retry
# ---------------------------------------------------------------------------

class TestExecuteQueryWithFailover:
    async def test_clean_result_no_notification(self):
        """A clean (non-429) result returns immediately; attempt_failover is
        never consulted."""
        result = {"session_id": "sess-1", "metadata": {"api_error_status": None}}
        eq = AsyncMock(return_value=result)
        with patch("app.core.query_engine.execute_query", new=eq), \
             patch.object(af, "attempt_failover", new=AsyncMock()) as af_mock:
            out = await af.execute_query_with_failover(
                prompt="hi", profile_id="claude-code", session_id="sess-1",
            )
        assert out is result
        eq.assert_awaited_once()
        af_mock.assert_not_awaited()

    async def test_limit_surfaces_notification_no_retry(self):
        """A 429 result runs execute_query exactly ONCE (no retry) and routes
        through attempt_failover so the rate-limited notification is emitted."""
        result = {"session_id": "sess-1", "metadata": {"api_error_status": 429}}
        eq = AsyncMock(return_value=result)
        with patch("app.core.query_engine.execute_query", new=eq), \
             patch.object(af, "attempt_failover", new=AsyncMock()) as af_mock:
            out = await af.execute_query_with_failover(
                prompt="hi", profile_id="claude-code", session_id="sess-1",
                api_user_id="u1",
            )
        assert out is result
        eq.assert_awaited_once()  # NO retry — single pass
        af_mock.assert_awaited_once_with("sess-1", "u1")
