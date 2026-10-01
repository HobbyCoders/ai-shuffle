"""HTTP statuses from the delegated leg reach the app's error handling.

The claude CLI reports the status a failed call got (``api_error_status``);
fluid carries it on ``ErrorEvent.status``. A 429 must reach the usage-limit
handling (``account_failover.is_usage_limit_error``), and a 401, which on this
leg means the CLI's own sign-in expired, must tell the user to sign in again.

Synthetic events only: no CLI is spawned.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fluid.events.model import ErrorEvent

import app.core.provider.harness_adapter as ha
from app.core import query_engine as qe
from app.core.account_failover import is_usage_limit_error
from app.core.provider.events import ResultMessage


def test_a_delegated_429_reaches_the_usage_limit_check():
    event = ErrorEvent(
        session_id="ses_x", turn_id="turn_1",
        message="claude reported success: Claude AI usage limit reached|1790812800",
        status=429, provider_message="Claude AI usage limit reached|1790812800",
    )
    adapter = ha.HarnessAdapter()
    [result] = adapter._translate(event)

    assert isinstance(result, ResultMessage) and result.is_error
    assert result.api_error_status == 429
    assert "Claude AI usage limit reached" in result.result
    assert is_usage_limit_error({"api_error_status": result.api_error_status}) is True


@pytest.mark.asyncio
async def test_a_delegated_401_asks_for_a_claude_code_sign_in():
    emit = AsyncMock()
    with patch("app.services.notification_service.emit_notification", new=emit):
        await qe._emit_api_error_notification("u1", "s1", 401, delegated=True)

    emit.assert_awaited_once()
    assert emit.await_args.args == ("u1",)
    message = emit.await_args.kwargs["message"]
    assert "claude auth login" in message
    assert "Settings > Providers > Claude Code CLI" in message
    assert emit.await_args.kwargs["session_id"] == "s1"


@pytest.mark.asyncio
async def test_a_native_401_keeps_the_generic_text():
    emit = AsyncMock()
    with patch("app.services.notification_service.emit_notification", new=emit):
        await qe._emit_api_error_notification("u1", "s1", 401)

    message = emit.await_args.kwargs["message"]
    assert message == "API error: HTTP 401"
    assert "claude auth login" not in message


@pytest.mark.asyncio
async def test_a_delegated_429_still_leaves_the_notice_to_account_failover():
    emit = AsyncMock()
    with patch("app.services.notification_service.emit_notification", new=emit):
        await qe._emit_api_error_notification("u1", "s1", 429, delegated=True)
    emit.assert_not_awaited()


def test_the_leg_is_read_from_the_adapter():
    delegated = ha.HarnessAdapter()
    delegated._provider_kind = "delegated"
    native = ha.HarnessAdapter()
    native._provider_kind = "anthropic"

    assert qe._adapter_is_delegated(delegated) is True
    assert qe._adapter_is_delegated(native) is False
    assert qe._adapter_is_delegated(object()) is False
