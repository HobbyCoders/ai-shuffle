"""
Tests for Slice B — "defer" decision end-to-end (backend).

Covers:
- B.1  PermissionDecision.DEFER enum value
- B.2  respond("defer") moves request to _deferred, returns sentinel
- B.3  request_permission returns _DeferSentinel when deferred
- B.4  requeue_deferred moves requests back to _pending_requests
- B.7  defer skips rule creation regardless of remember/pattern
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, patch

from app.core.permission_handler import (
    PermissionDecision,
    PermissionRequest,
    PermissionHandler,
    _DeferSentinel,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_request(request_id="req-1", session_id="session-1") -> PermissionRequest:
    return PermissionRequest(
        request_id=request_id,
        session_id=session_id,
        profile_id="profile-1",
        tool_name="Bash",
        tool_input={"command": "rm -rf /"},
    )


# ---------------------------------------------------------------------------
# B.1 — Enum
# ---------------------------------------------------------------------------

class TestPermissionDecisionDefer:
    def test_defer_value(self):
        """PermissionDecision.DEFER should have string value 'defer'."""
        assert PermissionDecision.DEFER.value == "defer"

    def test_defer_is_string(self):
        """PermissionDecision.DEFER should compare equal to the literal string."""
        assert PermissionDecision.DEFER == "defer"


# ---------------------------------------------------------------------------
# B.2 — respond("defer") moves to deferred, sentinel set
# ---------------------------------------------------------------------------

class TestRespondDefer:
    @pytest.fixture
    def handler(self):
        return PermissionHandler()

    @pytest.mark.asyncio
    async def test_respond_defer_request_not_found(self, handler):
        """respond('defer') with unknown request_id returns error."""
        handler._pending_requests["session-1"] = {}
        result = await handler.respond(
            request_id="unknown-req",
            session_id="session-1",
            decision="defer",
        )
        assert result == {"error": "Request not found"}


# ---------------------------------------------------------------------------
# B.3 — request_permission returns _DeferSentinel
# ---------------------------------------------------------------------------

class TestRequestPermissionReturnsSentinel:
    @pytest.fixture
    def handler(self):
        return PermissionHandler()

    @pytest.mark.asyncio
    async def test_request_permission_returns_sentinel_when_deferred(self, handler):
        """When user defers, request_permission must return _DeferSentinel."""
        mock_broadcast = AsyncMock()

        with patch("app.db.database.get_permission_rules", return_value=[]):
            async def defer_after_delay():
                await asyncio.sleep(0.01)
                await handler.respond(
                    request_id="req-1",
                    session_id="session-1",
                    decision="defer",
                )

            task = asyncio.create_task(defer_after_delay())

            result = await handler.request_permission(
                request_id="req-1",
                session_id="session-1",
                profile_id="profile-1",
                tool_name="Bash",
                tool_input={"command": "rm -rf /"},
                broadcast_func=mock_broadcast,
            )

            await task

        assert isinstance(result, _DeferSentinel)


# ---------------------------------------------------------------------------
# B.4 — requeue_deferred moves back to pending
# ---------------------------------------------------------------------------

class TestRequeueDeferred:
    @pytest.fixture
    def handler(self):
        return PermissionHandler()

    @pytest.mark.asyncio
    async def test_requeue_deferred_empty_session(self, handler):
        """requeue_deferred() on a session with no deferred items returns empty list."""
        result = await handler.requeue_deferred("session-with-nothing")
        assert result == []


# ---------------------------------------------------------------------------
# B.7 — defer skips rule creation
#
# NOTE: The three tests that used to live here asserted on `_pending_requests`
# / `_session_rules` mutation as their setup mechanism. Under the V2-only
# collapsed handler (#603) that in-memory dict is never populated by the real
# request path, so these tests could only "pass" by asserting on state they
# themselves seeded directly — they never actually exercised the defer/rule
# code path they claimed to cover. Removed rather than kept as false coverage.
# ---------------------------------------------------------------------------
