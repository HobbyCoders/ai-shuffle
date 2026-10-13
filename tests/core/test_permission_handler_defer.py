"""
Tests for Slice B — "defer" decision end-to-end (backend).

Covers:
- B.1  PermissionDecision.DEFER enum value
- B.2  respond("defer") defers the request, returns sentinel
- B.3  request_permission returns _DeferSentinel when deferred
- B.4  requeue_deferred moves requests back to pending
- B.7  defer skips rule creation regardless of remember/pattern
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, patch

from app.core.permission_handler import (
    PermissionDecision,
    PermissionHandler,
    _DeferSentinel,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
                # Answer once the durable row exists: an answer that arrives
                # first finds nothing and the request waits out its full TTL.
                loop = asyncio.get_running_loop()
                deadline = loop.time() + 10.0
                while handler.get_queue_count("session-1") != 1:
                    assert loop.time() < deadline, "the permission row never appeared"
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


# ---------------------------------------------------------------------------
# Switch model prompts cannot be deferred (#1881)
#
# The model switch caller reads a defer as a refusal and never comes back, so
# a re-shown deferred card had nobody waiting for it and Approve did nothing.
# ---------------------------------------------------------------------------

class TestSwitchModelPromptIsNotDeferrable:
    @pytest.fixture
    def handler(self):
        return PermissionHandler()

    @staticmethod
    async def _wait_for_rows(handler, session_id, count):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 10.0
        while handler.get_queue_count(session_id) != count:
            assert loop.time() < deadline, "the permission row never appeared"
            await asyncio.sleep(0.01)

    @pytest.mark.asyncio
    async def test_defer_is_refused_and_the_prompt_can_still_be_approved(self, handler):
        from app.core.agent_types import PermissionResultAllow
        from app.core.model_override import PROMPT_TOOL

        session_id = "session-switch-defer"
        broadcast = AsyncMock()
        seen = {}

        async def answer():
            await self._wait_for_rows(handler, session_id, 1)
            seen["pending"] = handler.get_pending_requests(session_id)
            seen["defer"] = await handler.respond(
                request_id="switch-1", session_id=session_id, decision="defer",
            )
            seen["after_defer"] = handler.get_queue_count(session_id)
            seen["allow"] = await handler.respond(
                request_id="switch-1", session_id=session_id, decision="allow",
            )

        with patch("app.db.database.get_permission_rules", return_value=[]):
            task = asyncio.create_task(answer())
            result = await handler.request_permission(
                request_id="switch-1",
                session_id=session_id,
                profile_id="profile-1",
                tool_name=PROMPT_TOOL,
                tool_input={"model": "gpt-6", "instead_of": "opus", "model_switch": "start a chat"},
                broadcast_func=broadcast,
                skip_saved_rules=True,
            )
            await task

        event = broadcast.await_args_list[0].args[0]
        assert event["type"] == "permission_request"
        assert event["deferrable"] is False
        assert seen["pending"][0]["deferrable"] is False
        assert seen["defer"] == {"error": "This prompt cannot be deferred. Approve or deny it."}
        assert seen["after_defer"] == 1, "a refused defer must leave the prompt pending"
        assert seen["allow"]["resolved"] is True
        assert isinstance(result, PermissionResultAllow)
        assert handler.get_deferred_requests(session_id) == []

    @pytest.mark.asyncio
    async def test_ordinary_prompts_stay_deferrable(self, handler):
        session_id = "session-bash-defer"
        broadcast = AsyncMock()
        seen = {}

        async def answer():
            await self._wait_for_rows(handler, session_id, 1)
            seen["pending"] = handler.get_pending_requests(session_id)
            await handler.respond(request_id="bash-1", session_id=session_id, decision="defer")

        with patch("app.db.database.get_permission_rules", return_value=[]):
            task = asyncio.create_task(answer())
            result = await handler.request_permission(
                request_id="bash-1", session_id=session_id, profile_id="profile-1",
                tool_name="Bash", tool_input={"command": "ls"}, broadcast_func=broadcast,
            )
            await task

        assert isinstance(result, _DeferSentinel)
        assert "deferrable" not in broadcast.await_args_list[0].args[0]
        assert "deferrable" not in seen["pending"][0]

    @pytest.mark.asyncio
    async def test_a_switch_prompt_deferred_before_the_fix_is_not_reshown(self, handler):
        """A row deferred by an older build stays answered: re-showing it would
        give a card whose Approve starts nothing. Other deferred prompts still
        come back."""
        from app.core.model_override import PROMPT_TOOL
        from app.core.permission_handler import PERMISSION_KIND
        from app.db import interactions

        session_id = "session-legacy-defer"
        for request_id, tool_name in (("old-switch", PROMPT_TOOL), ("old-bash", "Bash")):
            row = interactions.create_interaction(
                session_id, PERMISSION_KIND,
                {"request_id": request_id, "profile_id": "p", "tool_name": tool_name, "tool_input": {}},
                300,
            )
            assert interactions.answer_interaction(row["id"], {"decision": "defer"}, None)

        assert [r["request_id"] for r in handler.get_deferred_requests(session_id)] == ["old-bash"]
        broadcast = AsyncMock()
        assert await handler.requeue_deferred(session_id, broadcast) == ["old-bash"]
        assert [r["request_id"] for r in handler.get_pending_requests(session_id)] == ["old-bash"]
        shown = [c.args[0]["request_id"] for c in broadcast.await_args_list
                 if c.args[0].get("type") == "permission_request"]
        assert shown == ["old-bash"]
        # Marked handled, so a later requeue does not pick it up again either.
        assert await handler.requeue_deferred(session_id, broadcast) == []
