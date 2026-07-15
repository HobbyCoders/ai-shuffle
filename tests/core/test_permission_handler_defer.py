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

from app.core.config import settings
from app.core.permission_handler import (
    PermissionDecision,
    PermissionRequest,
    PermissionHandler,
    _DeferSentinel,
)


@pytest.fixture(autouse=True)
def _legacy_placement(monkeypatch):
    """This module asserts the legacy (flag-OFF) in-memory defer/requeue path.
    The V2 Phase 4 cutover defaults ``v2_placement`` ON (the shared
    ``pending_interactions`` adapter, covered by test_interaction_placement.py);
    pin it OFF here so these tests exercise the legacy else-branch."""
    monkeypatch.setattr(settings, "v2_placement", False)


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
    async def test_respond_defer_moves_to_deferred(self, handler):
        """respond('defer') must move the request from pending to _deferred."""
        req = _make_request()
        handler._pending_requests["session-1"] = {"req-1": req}

        result = await handler.respond(
            request_id="req-1",
            session_id="session-1",
            decision="defer",
        )

        assert result["resolved"] is True
        assert result.get("deferred") is True

        # No longer in pending
        assert "req-1" not in handler._pending_requests.get("session-1", {})

        # Now in deferred
        assert "req-1" in handler._deferred["session-1"]

        # State flag updated
        deferred_req = handler._deferred["session-1"]["req-1"]
        assert deferred_req.state == "deferred"

    @pytest.mark.asyncio
    async def test_respond_defer_sets_sentinel_on_response_event(self, handler):
        """The response field must be a _DeferSentinel and event must be set."""
        req = _make_request()
        handler._pending_requests["session-1"] = {"req-1": req}

        await handler.respond(
            request_id="req-1",
            session_id="session-1",
            decision="defer",
        )

        # The response attribute holds the sentinel (checked by request_permission)
        assert isinstance(req.response, _DeferSentinel)
        assert req.response_event.is_set()

    @pytest.mark.asyncio
    async def test_respond_defer_broadcasts_queue_update(self, handler):
        """respond('defer') must broadcast a permission_queue_update with deferred_ids."""
        req = _make_request()
        handler._pending_requests["session-1"] = {"req-1": req}
        mock_broadcast = AsyncMock()

        await handler.respond(
            request_id="req-1",
            session_id="session-1",
            decision="defer",
            broadcast_func=mock_broadcast,
        )

        mock_broadcast.assert_awaited_once()
        payload = mock_broadcast.call_args[0][0]
        assert payload["type"] == "permission_queue_update"
        assert "req-1" in payload["deferred_ids"]
        assert "req-1" in payload["resolved_ids"]

    @pytest.mark.asyncio
    async def test_respond_defer_session_not_found(self, handler):
        """respond('defer') with unknown session returns error."""
        result = await handler.respond(
            request_id="req-1",
            session_id="unknown-session",
            decision="defer",
        )
        assert result == {"error": "Session not found"}

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
    async def test_requeue_deferred_moves_back_to_pending(self, handler):
        """requeue_deferred() must move all deferred entries back to _pending_requests."""
        req = _make_request()
        req.state = "deferred"
        handler._deferred["session-1"]["req-1"] = req

        requeued = await handler.requeue_deferred("session-1")

        assert "req-1" in requeued
        assert "req-1" in handler._pending_requests.get("session-1", {})
        assert "req-1" not in handler._deferred.get("session-1", {})

    @pytest.mark.asyncio
    async def test_requeue_deferred_resets_state(self, handler):
        """requeue_deferred() must reset state to 'pending' and clear response."""
        req = _make_request()
        req.state = "deferred"
        req.response = _DeferSentinel()
        handler._deferred["session-1"]["req-1"] = req

        await handler.requeue_deferred("session-1")

        restored = handler._pending_requests["session-1"]["req-1"]
        assert restored.state == "pending"
        assert restored.response is None
        assert not restored.response_event.is_set()

    @pytest.mark.asyncio
    async def test_requeue_deferred_empty_session(self, handler):
        """requeue_deferred() on a session with no deferred items returns empty list."""
        result = await handler.requeue_deferred("session-with-nothing")
        assert result == []

    @pytest.mark.asyncio
    async def test_requeue_deferred_rebroadcasts(self, handler):
        """requeue_deferred() must re-broadcast each request."""
        req = _make_request()
        req.state = "deferred"
        handler._deferred["session-1"]["req-1"] = req
        mock_broadcast = AsyncMock()

        await handler.requeue_deferred("session-1", broadcast_func=mock_broadcast)

        # At minimum, permission_request event + queue_update event
        assert mock_broadcast.await_count >= 2
        types = [call[0][0]["type"] for call in mock_broadcast.call_args_list]
        assert "permission_request" in types
        assert "permission_queue_update" in types

    @pytest.mark.asyncio
    async def test_requeue_deferred_can_re_resolve(self, handler):
        """After requeue, the request can be resolved via respond()."""
        req = _make_request()
        req.state = "deferred"
        handler._deferred["session-1"]["req-1"] = req

        await handler.requeue_deferred("session-1")

        # Now allow it
        result = await handler.respond(
            request_id="req-1",
            session_id="session-1",
            decision="allow",
        )
        assert result["resolved"] is True
        pending_req = handler._pending_requests.get("session-1", {})
        assert "req-1" not in pending_req


# ---------------------------------------------------------------------------
# B.7 — defer skips rule creation
# ---------------------------------------------------------------------------

class TestDeferSkipsRuleCreation:
    @pytest.fixture
    def handler(self):
        return PermissionHandler()

    @pytest.mark.asyncio
    async def test_defer_skips_rule_creation_session(self, handler):
        """respond('defer', remember='session') must not create any session rule."""
        req = _make_request()
        handler._pending_requests["session-1"] = {"req-1": req}

        await handler.respond(
            request_id="req-1",
            session_id="session-1",
            decision="defer",
            remember="session",
            pattern="*",
        )

        # No session rules should have been created
        assert len(handler._session_rules.get("session-1", [])) == 0

    @pytest.mark.asyncio
    async def test_defer_skips_rule_creation_profile(self, handler):
        """respond('defer', remember='profile') must not call database.add_permission_rule."""
        req = _make_request()
        handler._pending_requests["session-1"] = {"req-1": req}

        with patch("app.db.database.add_permission_rule") as mock_add:
            await handler.respond(
                request_id="req-1",
                session_id="session-1",
                decision="defer",
                remember="profile",
                pattern="*",
            )

        mock_add.assert_not_called()

    @pytest.mark.asyncio
    async def test_defer_no_auto_resolve(self, handler):
        """respond('defer') must not auto-resolve other pending requests."""
        req1 = _make_request("req-1")
        req2 = PermissionRequest(
            request_id="req-2",
            session_id="session-1",
            profile_id="profile-1",
            tool_name="Bash",
            tool_input={"command": "rm -rf /"},
        )
        handler._pending_requests["session-1"] = {"req-1": req1, "req-2": req2}

        await handler.respond(
            request_id="req-1",
            session_id="session-1",
            decision="defer",
        )

        # req-2 must still be pending, untouched
        assert "req-2" in handler._pending_requests["session-1"]
        assert not req2.response_event.is_set()
