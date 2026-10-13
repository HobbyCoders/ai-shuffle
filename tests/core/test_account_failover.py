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

import pytest

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
        with patch.object(af, "_notify", new=AsyncMock()) as notify, \
             patch.object(af, "_is_subscription_session", return_value=True):
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


class TestApiKey429IsNotAUsageWindow:
    """R11-7: an API key's 429 (OpenRouter upstream, Groq or xAI token limits,
    Claude in API-key mode) told the user to wait for a usage window to reset.
    Only a subscription login has one."""

    @staticmethod
    async def _notice(session_row, provider_row, claude_mode="subscription"):
        from app.core import claude_auth_mode
        from app.db import database

        with patch.object(af, "_notify", new=AsyncMock()) as notify, \
             patch.object(database, "get_session", return_value=session_row), \
             patch.object(database, "get_model_provider", return_value=provider_row), \
             patch.object(claude_auth_mode, "current_mode", return_value=claude_mode):
            await af.attempt_failover("sess-1", "user-1")
        notify.assert_awaited_once()
        assert notify.await_args.kwargs["severity"] == "warning"
        return notify.await_args.kwargs["message"]

    @pytest.mark.parametrize("provider", [
        {"provider_type": "openrouter", "auth_mode": None},
        {"provider_type": "custom", "auth_mode": "api_key", "api_dialect": "openai"},
        {"provider_type": "xai", "auth_mode": None},
        {"provider_type": "anthropic", "auth_mode": "api_key"},
    ])
    async def test_an_api_key_provider_gets_the_plain_rate_limit_notice(self, provider):
        message = await self._notice({"provider_id": "p1"}, provider)
        assert message == af.RATE_LIMIT_MESSAGE
        assert "window" not in message.lower()

    @pytest.mark.parametrize("provider", [
        {"provider_type": "chatgpt", "auth_mode": "chatgpt"},
        {"provider_type": "custom", "auth_mode": "chatgpt"},
        {"provider_type": "grok", "auth_mode": None},
        {"provider_type": "anthropic", "auth_mode": "subscription"},
    ])
    async def test_a_subscription_provider_keeps_the_usage_window_notice(self, provider):
        assert await self._notice({"provider_id": "p1"}, provider) == af.USAGE_LIMIT_MESSAGE

    @pytest.mark.parametrize("mode,expected", [
        ("subscription", "USAGE_LIMIT_MESSAGE"),
        ("api_key", "RATE_LIMIT_MESSAGE"),
        ("off", "RATE_LIMIT_MESSAGE"),
    ])
    async def test_a_claude_session_follows_the_claude_auth_mode(self, mode, expected):
        message = await self._notice({"provider_id": None}, None, claude_mode=mode)
        assert message == getattr(af, expected)

    async def test_a_failed_lookup_keeps_the_usage_window_notice(self):
        from app.db import database

        with patch.object(af, "_notify", new=AsyncMock()) as notify, \
             patch.object(database, "get_session", side_effect=RuntimeError("db down")):
            await af.attempt_failover("sess-1", "user-1")
        assert notify.await_args.kwargs["message"] == af.USAGE_LIMIT_MESSAGE


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
