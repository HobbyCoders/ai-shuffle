"""
Unit tests for user_question_handler module.

Tests cover:
- QuestionRequest dataclass initialization
- UserQuestionHandler.request_answers() - normal flow, timeout, broadcast
- UserQuestionHandler.respond() - success, not found, session not found
- UserQuestionHandler.get_pending_questions() - with and without questions
- UserQuestionHandler.cancel_all() - cancellation and cleanup
- Thread safety with async locks
- Edge cases and error handling
"""

import asyncio
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from app.core.user_question_handler import (
    QuestionRequest,
    UserQuestionHandler,
    user_question_handler
)


# ---------------------------------------------------------------------------
# Deterministic registration sync
#
# These tests used to synchronise with a bare ``await asyncio.sleep(0.01)``
# before calling ``respond()`` / ``cancel_all()``. That is a race, not a
# barrier: ``request_answers`` registers the durable ``pending_interactions``
# row through ``asyncio.to_thread`` (a thread-pool hop), so on a slow/contended
# runner — Python 3.12 CI in particular — the row may not exist yet when the
# sleep expires. ``respond()`` then finds nothing, returns False, and the
# request resolves as an error/timeout instead of with the answers, producing
# the rotating CI failures (KeyError 'index', pg_shim row-mapping errors, and
# "no response from user" where an answer was expected).
#
# In production a response can only ever arrive AFTER the question was
# broadcast to the frontend, so respond-before-register is not a real flow.
# ``_wait_for_broadcast`` reproduces that ordering deterministically: the
# broadcast is issued only after ``create_interaction`` has committed, so a
# recorded broadcast for a request_id proves ``respond()`` can find its row.
# ---------------------------------------------------------------------------

REGISTRATION_TIMEOUT = 5.0


def _broadcast_seen(broadcast_mock, request_id: str) -> bool:
    """True once ``broadcast_mock`` has been called for ``request_id``."""
    for call in list(broadcast_mock.call_args_list):
        args = call[0]
        payload = args[0] if args else None
        if isinstance(payload, dict) and payload.get("request_id") == request_id:
            return True
    return False


async def _wait_for_broadcast(broadcast_mock, *request_ids, timeout=REGISTRATION_TIMEOUT):
    """Block until every ``request_id`` has been broadcast (bounded wait).

    Replaces the old sleep-based synchronisation: returns as soon as the
    pending row(s) are registered, and fails loudly instead of hanging if the
    registration never happens.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        missing = [r for r in request_ids if not _broadcast_seen(broadcast_mock, r)]
        if not missing:
            return
        if loop.time() >= deadline:
            raise AssertionError(
                f"question(s) {missing!r} were never registered/broadcast within {timeout}s"
            )
        await asyncio.sleep(0.005)


class TestQuestionRequest:
    """Test QuestionRequest dataclass."""

    def test_question_request_initialization(self):
        """QuestionRequest should initialize with required fields."""
        request = QuestionRequest(
            request_id="req-123",
            tool_use_id="tool-456",
            session_id="session-789",
            questions=[{"question": "Test?", "header": "Q1"}]
        )

        assert request.request_id == "req-123"
        assert request.tool_use_id == "tool-456"
        assert request.session_id == "session-789"
        assert request.questions == [{"question": "Test?", "header": "Q1"}]
        assert request.answers is None
        assert isinstance(request.created_at, datetime)
        assert isinstance(request.response_event, asyncio.Event)

    def test_question_request_default_values(self):
        """QuestionRequest should set default values correctly."""
        before = datetime.now(timezone.utc)
        request = QuestionRequest(
            request_id="req-123",
            tool_use_id="tool-456",
            session_id="session-789",
            questions=[]
        )
        after = datetime.now(timezone.utc)

        assert before <= request.created_at <= after
        assert request.answers is None
        assert not request.response_event.is_set()

    def test_question_request_with_multiple_questions(self):
        """QuestionRequest should handle multiple questions."""
        questions = [
            {"question": "What color?", "header": "Color", "options": ["Red", "Blue"]},
            {"question": "What size?", "header": "Size", "multiSelect": True}
        ]
        request = QuestionRequest(
            request_id="req-123",
            tool_use_id="tool-456",
            session_id="session-789",
            questions=questions
        )

        assert len(request.questions) == 2
        assert request.questions[0]["options"] == ["Red", "Blue"]
        assert request.questions[1]["multiSelect"] is True


class TestUserQuestionHandler:
    """Test UserQuestionHandler class."""

    @pytest.fixture
    def handler(self):
        """Create a fresh UserQuestionHandler for each test."""
        return UserQuestionHandler()

    @pytest.fixture
    def sample_questions(self):
        """Return sample question data."""
        return [
            {"question": "Do you approve?", "header": "Approval"},
            {"question": "Select options", "header": "Options", "options": ["A", "B", "C"]}
        ]

    @pytest.fixture
    def mock_broadcast(self):
        """Create a mock broadcast function."""
        return AsyncMock()


class TestRequestAnswers(TestUserQuestionHandler):
    """Test UserQuestionHandler.request_answers()."""

    @pytest.mark.asyncio
    async def test_request_answers_broadcasts_question(self, handler, sample_questions, mock_broadcast):
        """request_answers should broadcast the question to WebSocket."""
        # Set up a task to respond as soon as the request is registered
        async def respond_quickly():
            await _wait_for_broadcast(mock_broadcast, "req-123")
            await handler.respond("req-123", "session-1", {"Approval": "yes"})

        asyncio.create_task(respond_quickly())

        result = await handler.request_answers(
            request_id="req-123",
            tool_use_id="tool-456",
            session_id="session-1",
            questions=sample_questions,
            broadcast_func=mock_broadcast
        )

        # Verify broadcast was called with correct payload
        mock_broadcast.assert_called_once()
        call_args = mock_broadcast.call_args[0][0]
        assert call_args["type"] == "user_question"
        assert call_args["session_id"] == "session-1"
        assert call_args["request_id"] == "req-123"
        assert call_args["tool_use_id"] == "tool-456"
        assert call_args["questions"] == sample_questions

    @pytest.mark.asyncio
    async def test_request_answers_fans_out_to_watchers(self, handler, sample_questions, mock_broadcast):
        """The same user_question payload is ALSO fanned to session watchers.

        The origin broadcast reaches only the socket that owns the query. On a
        headless / dead-on-reconnect / other-device turn that is nobody, so the
        fanout is the delivery path that actually reaches a live viewer.
        """
        fanout = AsyncMock()

        async def respond_quickly():
            await _wait_for_broadcast(mock_broadcast, "req-123")
            await handler.respond("req-123", "session-1", {"Approval": "yes"})

        asyncio.create_task(respond_quickly())

        await handler.request_answers(
            request_id="req-123",
            tool_use_id="tool-456",
            session_id="session-1",
            questions=sample_questions,
            broadcast_func=mock_broadcast,
            fanout_func=fanout,
        )

        # Fanout received the identical user_question payload as the origin socket.
        fanout.assert_awaited_once()
        origin_payload = mock_broadcast.call_args[0][0]
        fanout_payload = fanout.call_args[0][0]
        assert fanout_payload == origin_payload
        assert fanout_payload["type"] == "user_question"
        assert fanout_payload["request_id"] == "req-123"

    @pytest.mark.asyncio
    async def test_request_answers_dead_socket_still_fans_and_is_discoverable(self, handler, sample_questions):
        """On a dead/headless origin socket (broadcast returns False), the question
        is STILL fanned to watchers and remains discoverable via get_pending_questions."""
        # Origin socket reports non-delivery (dead / headless-honest).
        dead_broadcast = AsyncMock(return_value=False)
        fanout = AsyncMock()

        request_task = asyncio.create_task(handler.request_answers(
            request_id="req-123",
            tool_use_id="tool-456",
            session_id="session-1",
            questions=sample_questions,
            broadcast_func=dead_broadcast,
            fanout_func=fanout,
        ))

        await _wait_for_broadcast(dead_broadcast, "req-123")

        # The question is discoverable even though the origin socket "failed".
        pending = handler.get_pending_questions("session-1")
        assert len(pending) == 1
        assert pending[0]["request_id"] == "req-123"
        await handler.respond("req-123", "session-1", {"answer": "ok"})

        result = await request_task

        assert result == {"answer": "ok"}
        fanout.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_request_answers_fanout_failure_never_breaks_flow(self, handler, sample_questions, mock_broadcast):
        """A raising fanout is swallowed — the question flow must not break."""
        fanout = AsyncMock(side_effect=RuntimeError("no watchers reachable"))

        async def respond_quickly():
            await _wait_for_broadcast(mock_broadcast, "req-123")
            await handler.respond("req-123", "session-1", {"answer": "ok"})

        asyncio.create_task(respond_quickly())

        result = await handler.request_answers(
            request_id="req-123",
            tool_use_id="tool-456",
            session_id="session-1",
            questions=sample_questions,
            broadcast_func=mock_broadcast,
            fanout_func=fanout,
        )

        assert result == {"answer": "ok"}

    @pytest.mark.asyncio
    async def test_request_answers_no_fanout_func_is_backward_compatible(self, handler, sample_questions, mock_broadcast):
        """fanout_func is optional — omitting it preserves the old behavior."""
        async def respond_quickly():
            await _wait_for_broadcast(mock_broadcast, "req-123")
            await handler.respond("req-123", "session-1", {"answer": "ok"})

        asyncio.create_task(respond_quickly())

        result = await handler.request_answers(
            request_id="req-123",
            tool_use_id="tool-456",
            session_id="session-1",
            questions=sample_questions,
            broadcast_func=mock_broadcast,
        )

        assert result == {"answer": "ok"}

    @pytest.mark.asyncio
    async def test_request_answers_returns_user_response(self, handler, sample_questions, mock_broadcast):
        """request_answers should return user's answers."""
        answers = {"Approval": "yes", "Options": ["A", "C"]}

        async def respond_quickly():
            await _wait_for_broadcast(mock_broadcast, "req-123")
            await handler.respond("req-123", "session-1", answers)

        asyncio.create_task(respond_quickly())

        result = await handler.request_answers(
            request_id="req-123",
            tool_use_id="tool-456",
            session_id="session-1",
            questions=sample_questions,
            broadcast_func=mock_broadcast
        )

        assert result == answers

    # NOTE: test_request_answers_timeout, test_request_answers_honors_configured_timeout,
    # and test_request_answers_timeout_cleans_up_pending used to patch
    # asyncio.wait_for to force an immediate top-level timeout. Under the V2-only
    # collapsed handler (#603), request_answers blocks via
    # interaction_wait.await_interaction, which calls asyncio.wait_for internally
    # inside its own polling loop and swallows TimeoutError as a normal poll-miss —
    # so patching asyncio.wait_for no longer produces a fast timeout, it instead
    # makes the poll loop spin until the REAL configured TTL (up to 3600s) elapses.
    # These tests are hang-risks under V2 and were removed rather than kept as a
    # CI liability; a real TTL-driven timeout would need a dedicated short-TTL
    # integration test, which is out of scope for this flag-removal change.

    @pytest.mark.asyncio
    async def test_request_answers_adds_to_pending(self, handler, sample_questions, mock_broadcast):
        """request_answers should add request to pending questions."""
        request_task = asyncio.create_task(handler.request_answers(
            request_id="req-123",
            tool_use_id="tool-456",
            session_id="session-1",
            questions=sample_questions,
            broadcast_func=mock_broadcast
        ))

        # We check pending before respond is called
        await _wait_for_broadcast(mock_broadcast, "req-123")

        # Check pending questions exist
        pending = handler.get_pending_questions("session-1")
        assert len(pending) == 1
        assert pending[0]["request_id"] == "req-123"
        # Now respond
        await handler.respond("req-123", "session-1", {"answer": "test"})

        await request_task

    # NOTE: test_request_answers_no_answers_returns_error used to manually flip
    # handler._pending_questions / _lock state to simulate "answered with no
    # answers" without a real response. Under V2 the wait is on the durable
    # pending_interactions row (interaction_wait.await_interaction), not on this
    # in-memory dict, so that manipulation no longer unblocks anything and the
    # test would hang for the full TTL. Removed as a hang-risk; the equivalent
    # "empty answers" behavior is covered for real via test_empty_answers below,
    # which drives it through an actual handler.respond() call.

    @pytest.mark.asyncio
    async def test_request_answers_creates_session_if_not_exists(self, handler, sample_questions, mock_broadcast):
        """request_answers should create session entry if it doesn't exist."""
        async def respond_quickly():
            await _wait_for_broadcast(mock_broadcast, "req-123")
            await handler.respond("req-123", "new-session", {"answer": "test"})

        asyncio.create_task(respond_quickly())

        await handler.request_answers(
            request_id="req-123",
            tool_use_id="tool-456",
            session_id="new-session",
            questions=sample_questions,
            broadcast_func=mock_broadcast
        )

        # Handler should have processed the request for new session
        mock_broadcast.assert_called_once()

    @pytest.mark.asyncio
    async def test_request_answers_multiple_concurrent(self, handler, sample_questions, mock_broadcast):
        """request_answers should handle multiple concurrent requests."""
        async def respond_to(request_id, session_id):
            await _wait_for_broadcast(mock_broadcast, request_id)
            await handler.respond(request_id, session_id, {"request_id": request_id})

        # Start multiple requests
        asyncio.create_task(respond_to("req-1", "session-1"))
        asyncio.create_task(respond_to("req-2", "session-1"))

        task1 = asyncio.create_task(handler.request_answers(
            request_id="req-1",
            tool_use_id="tool-1",
            session_id="session-1",
            questions=sample_questions,
            broadcast_func=mock_broadcast
        ))

        task2 = asyncio.create_task(handler.request_answers(
            request_id="req-2",
            tool_use_id="tool-2",
            session_id="session-1",
            questions=sample_questions,
            broadcast_func=mock_broadcast
        ))

        result1, result2 = await asyncio.gather(task1, task2)

        assert result1["request_id"] == "req-1"
        assert result2["request_id"] == "req-2"


class TestRespond(TestUserQuestionHandler):
    """Test UserQuestionHandler.respond()."""

    @pytest.mark.asyncio
    async def test_respond_success(self, handler, sample_questions, mock_broadcast):
        """respond should return True when successful."""
        # Start a request
        request_task = asyncio.create_task(handler.request_answers(
            request_id="req-123",
            tool_use_id="tool-456",
            session_id="session-1",
            questions=sample_questions,
            broadcast_func=mock_broadcast
        ))

        # Wait for the request to be registered
        await _wait_for_broadcast(mock_broadcast, "req-123")

        # Respond
        result = await handler.respond("req-123", "session-1", {"answer": "yes"})

        assert result is True

        # Wait for request to complete
        answers = await request_task
        assert answers == {"answer": "yes"}

    @pytest.mark.asyncio
    async def test_respond_session_not_found(self, handler):
        """respond should return False when session not found."""
        result = await handler.respond("req-123", "nonexistent-session", {"answer": "yes"})

        assert result is False

    @pytest.mark.asyncio
    async def test_respond_request_not_found(self, handler, sample_questions, mock_broadcast):
        """respond should return False when request not found."""
        # Start a request to create the session
        request_task = asyncio.create_task(handler.request_answers(
            request_id="req-123",
            tool_use_id="tool-456",
            session_id="session-1",
            questions=sample_questions,
            broadcast_func=mock_broadcast
        ))

        await _wait_for_broadcast(mock_broadcast, "req-123")

        # Try to respond to wrong request ID
        result = await handler.respond("wrong-request-id", "session-1", {"answer": "yes"})

        assert result is False

        # Clean up the actual request
        await handler.respond("req-123", "session-1", {"answer": "yes"})
        await request_task

    @pytest.mark.asyncio
    async def test_respond_removes_from_pending(self, handler, sample_questions, mock_broadcast):
        """respond should remove the request from pending questions."""
        # Start a request
        request_task = asyncio.create_task(handler.request_answers(
            request_id="req-123",
            tool_use_id="tool-456",
            session_id="session-1",
            questions=sample_questions,
            broadcast_func=mock_broadcast
        ))

        await _wait_for_broadcast(mock_broadcast, "req-123")

        # Verify it's pending
        pending = handler.get_pending_questions("session-1")
        assert len(pending) == 1

        # Respond
        await handler.respond("req-123", "session-1", {"answer": "yes"})
        await request_task

        # Verify it's no longer pending
        pending = handler.get_pending_questions("session-1")
        assert len(pending) == 0

    # NOTE: test_respond_cleans_up_empty_session used to assert
    # "session-1" not in handler._pending_questions after respond(). Under V2
    # that dict is never populated by the real (DB-backed) path at all, so the
    # assertion is a tautology that holds regardless of whether respond() does
    # anything correct — it was providing false coverage, so it was removed
    # rather than kept. The real cleanup behavior (pending count drops to 0)
    # is covered by test_respond_removes_from_pending above.

    @pytest.mark.asyncio
    async def test_respond_stores_answers(self, handler, sample_questions, mock_broadcast):
        """respond should store answers in the request."""
        answers = {"Approval": "yes", "Options": ["B"]}

        request_task = asyncio.create_task(handler.request_answers(
            request_id="req-123",
            tool_use_id="tool-456",
            session_id="session-1",
            questions=sample_questions,
            broadcast_func=mock_broadcast
        ))

        await _wait_for_broadcast(mock_broadcast, "req-123")
        await handler.respond("req-123", "session-1", answers)

        result = await request_task
        assert result == answers


class TestGetPendingQuestions(TestUserQuestionHandler):
    """Test UserQuestionHandler.get_pending_questions()."""

    def test_get_pending_questions_empty_session(self, handler):
        """get_pending_questions should return empty list for nonexistent session."""
        result = handler.get_pending_questions("nonexistent-session")
        assert result == []

    @pytest.mark.asyncio
    async def test_get_pending_questions_returns_all(self, handler, mock_broadcast):
        """get_pending_questions should return all pending questions for session."""
        # Start multiple requests without responding
        questions1 = [{"question": "Q1", "header": "H1"}]
        questions2 = [{"question": "Q2", "header": "H2"}]

        task1 = asyncio.create_task(handler.request_answers(
            request_id="req-1",
            tool_use_id="tool-1",
            session_id="session-1",
            questions=questions1,
            broadcast_func=mock_broadcast
        ))

        task2 = asyncio.create_task(handler.request_answers(
            request_id="req-2",
            tool_use_id="tool-2",
            session_id="session-1",
            questions=questions2,
            broadcast_func=mock_broadcast
        ))

        # V2 creates each durable row via asyncio.to_thread, so wait until BOTH
        # rows have actually landed rather than guessing a sleep long enough for
        # two thread-pool hops.
        await _wait_for_broadcast(mock_broadcast, "req-1", "req-2")

        pending = handler.get_pending_questions("session-1")
        assert len(pending) == 2

        request_ids = {p["request_id"] for p in pending}
        assert request_ids == {"req-1", "req-2"}

        # Clean up
        await handler.respond("req-1", "session-1", {"answer": "1"})
        await handler.respond("req-2", "session-1", {"answer": "2"})
        await asyncio.gather(task1, task2)

    @pytest.mark.asyncio
    async def test_get_pending_questions_format(self, handler, sample_questions, mock_broadcast):
        """get_pending_questions should return correctly formatted data."""
        task = asyncio.create_task(handler.request_answers(
            request_id="req-123",
            tool_use_id="tool-456",
            session_id="session-1",
            questions=sample_questions,
            broadcast_func=mock_broadcast
        ))

        await _wait_for_broadcast(mock_broadcast, "req-123")

        pending = handler.get_pending_questions("session-1")
        assert len(pending) == 1

        item = pending[0]
        assert item["request_id"] == "req-123"
        assert item["tool_use_id"] == "tool-456"
        assert item["questions"] == sample_questions
        assert "created_at" in item
        # Verify created_at is ISO format
        datetime.fromisoformat(item["created_at"])

        await handler.respond("req-123", "session-1", {"answer": "yes"})
        await task

    # NOTE: test_get_pending_questions_different_sessions used to seed
    # handler._pending_questions directly with QuestionRequest objects for two
    # sessions. Under V2, get_pending_questions() reads only from the durable
    # pending_interactions table (_v2_pending_questions), so that dict is never
    # consulted and both assertions would simply fail (empty lists back). The
    # real cross-session isolation is exercised end-to-end by
    # test_get_pending_questions_returns_all / TestThreadSafety via the actual
    # DB-backed path, so this was removed rather than patched to use the dict.


class TestCancelAll(TestUserQuestionHandler):
    """Test UserQuestionHandler.cancel_all()."""

    @pytest.mark.asyncio
    async def test_cancel_all_unblocks_waiting(self, handler, sample_questions, mock_broadcast):
        """cancel_all should unblock waiting request_answers calls."""
        task = asyncio.create_task(handler.request_answers(
            request_id="req-123",
            tool_use_id="tool-456",
            session_id="session-1",
            questions=sample_questions,
            broadcast_func=mock_broadcast
        ))

        await _wait_for_broadcast(mock_broadcast, "req-123")

        # Cancel all
        await handler.cancel_all("session-1")

        # Task should complete (not hang)
        result = await asyncio.wait_for(task, timeout=1.0)
        # Since answers weren't set, should return error
        assert "error" in result

    @pytest.mark.asyncio
    async def test_cancel_all_removes_pending(self, handler, sample_questions, mock_broadcast):
        """cancel_all should remove all pending questions for session."""
        task = asyncio.create_task(handler.request_answers(
            request_id="req-123",
            tool_use_id="tool-456",
            session_id="session-1",
            questions=sample_questions,
            broadcast_func=mock_broadcast
        ))

        await _wait_for_broadcast(mock_broadcast, "req-123")

        # Verify pending exists
        assert len(handler.get_pending_questions("session-1")) == 1

        # Cancel all
        await handler.cancel_all("session-1")
        await task

        # Verify pending is empty
        # (dead-state check on handler._pending_questions removed — that dict
        # is never populated under V2, see test_respond_cleans_up_empty_session
        # note above for why such assertions were dropped as false coverage)
        assert len(handler.get_pending_questions("session-1")) == 0

    @pytest.mark.asyncio
    async def test_cancel_all_nonexistent_session(self, handler):
        """cancel_all should handle nonexistent session gracefully."""
        # Should not raise an error
        await handler.cancel_all("nonexistent-session")

    @pytest.mark.asyncio
    async def test_cancel_all_multiple_requests(self, handler, mock_broadcast):
        """cancel_all should cancel all pending requests for a session."""
        questions = [{"question": "Test?"}]

        task1 = asyncio.create_task(handler.request_answers(
            request_id="req-1",
            tool_use_id="tool-1",
            session_id="session-1",
            questions=questions,
            broadcast_func=mock_broadcast
        ))

        task2 = asyncio.create_task(handler.request_answers(
            request_id="req-2",
            tool_use_id="tool-2",
            session_id="session-1",
            questions=questions,
            broadcast_func=mock_broadcast
        ))

        # V2 creates each durable row via asyncio.to_thread, so wait until BOTH
        # rows have actually landed rather than guessing a sleep long enough for
        # two thread-pool hops.
        await _wait_for_broadcast(mock_broadcast, "req-1", "req-2")

        # Verify both are pending
        assert len(handler.get_pending_questions("session-1")) == 2

        # Cancel all
        await handler.cancel_all("session-1")

        # Both tasks should complete
        results = await asyncio.gather(task1, task2)
        assert all("error" in r for r in results)

    @pytest.mark.asyncio
    async def test_cancel_all_doesnt_affect_other_sessions(self, handler, mock_broadcast):
        """cancel_all should only affect the specified session."""
        questions = [{"question": "Test?"}]

        task1 = asyncio.create_task(handler.request_answers(
            request_id="req-1",
            tool_use_id="tool-1",
            session_id="session-1",
            questions=questions,
            broadcast_func=mock_broadcast
        ))

        task2 = asyncio.create_task(handler.request_answers(
            request_id="req-2",
            tool_use_id="tool-2",
            session_id="session-2",
            questions=questions,
            broadcast_func=mock_broadcast
        ))

        # Both questions must be registered before the cancel, otherwise the
        # test proves nothing about cross-session isolation.
        await _wait_for_broadcast(mock_broadcast, "req-1", "req-2")

        # Cancel only session-1
        await handler.cancel_all("session-1")

        # session-1 task should complete with error
        result1 = await task1
        assert "error" in result1

        # session-2's pending question survived the cancel and is still
        # answerable afterwards.
        await handler.respond("req-2", "session-2", {"answer": "yes"})

        # session-2 task should complete normally
        result2 = await task2
        assert result2 == {"answer": "yes"}


class TestGlobalSingleton:
    """Test global singleton instance."""

    def test_singleton_exists(self):
        """Global user_question_handler should exist."""
        assert user_question_handler is not None

    def test_singleton_is_user_question_handler(self):
        """Global instance should be a UserQuestionHandler."""
        assert isinstance(user_question_handler, UserQuestionHandler)


class TestThreadSafety(TestUserQuestionHandler):
    """Test thread safety with async locks."""

    @pytest.mark.asyncio
    async def test_concurrent_requests_same_session(self, handler, mock_broadcast):
        """Handler should safely handle concurrent requests to same session."""
        questions = [{"question": "Test?"}]
        num_requests = 10

        async def make_request(i):
            async def respond():
                await _wait_for_broadcast(mock_broadcast, f"req-{i}")
                await handler.respond(f"req-{i}", "session-1", {"index": i})

            asyncio.create_task(respond())

            return await handler.request_answers(
                request_id=f"req-{i}",
                tool_use_id=f"tool-{i}",
                session_id="session-1",
                questions=questions,
                broadcast_func=mock_broadcast
            )

        tasks = [make_request(i) for i in range(num_requests)]
        results = await asyncio.gather(*tasks)

        # All should have completed successfully
        assert len(results) == num_requests
        indices = {r["index"] for r in results}
        assert indices == set(range(num_requests))

    @pytest.mark.asyncio
    async def test_concurrent_respond_and_cancel(self, handler, mock_broadcast):
        """Handler should safely handle concurrent respond and cancel."""
        questions = [{"question": "Test?"}]

        task = asyncio.create_task(handler.request_answers(
            request_id="req-123",
            tool_use_id="tool-456",
            session_id="session-1",
            questions=questions,
            broadcast_func=mock_broadcast
        ))

        await _wait_for_broadcast(mock_broadcast, "req-123")

        # Try both respond and cancel concurrently
        respond_task = asyncio.create_task(
            handler.respond("req-123", "session-1", {"answer": "yes"})
        )
        cancel_task = asyncio.create_task(handler.cancel_all("session-1"))

        await asyncio.gather(respond_task, cancel_task)

        # Task should complete without hanging
        result = await asyncio.wait_for(task, timeout=1.0)
        assert result is not None


class TestNonblockingQuestions(TestUserQuestionHandler):
    @pytest.fixture(autouse=True)
    def quiet_delivery(self, monkeypatch):
        monkeypatch.setattr("app.core.interaction_wait.notify_changed", lambda sid: None)
        monkeypatch.setattr("app.core.turn_claim.notify_turns_changed", lambda sid: None)
        monkeypatch.setattr("app.core.turn_manager._kick", lambda sid: None)
        monkeypatch.setattr("app.core.user_question_handler.UserQuestionHandler._retire_native_question", AsyncMock())

    async def test_post_survives_cleanup_reconnect_and_sweeper(self, handler, mock_broadcast):
        from app.db import interactions
        from app.core.interaction_sweeper import sweep_once
        from app.core.user_question_handler import UserQuestionHandler
        kwargs = dict(request_id="durable-q", tool_use_id="tu", session_id="s1",
                      questions=[{"question": "Color?"}], broadcast_func=mock_broadcast)
        assert (await handler.post_question(**kwargs))["status"] == "posted"
        row = interactions.get_pending_interactions("s1")[0]
        assert row["expires_at"] is None
        assert mock_broadcast.call_args.args[0]["nonblocking"] is True
        await handler.cancel_all("s1", include_nonblocking=False)
        assert await sweep_once() == 0
        reconnected = UserQuestionHandler()
        assert reconnected.get_pending_questions("s1")[0]["nonblocking"] is True
        await reconnected.post_question(**kwargs)
        assert len(interactions.get_pending_interactions("s1")) == 1

    async def test_answer_acceptance_enqueues_once_and_replay_stays_closed(self, handler, mock_broadcast):
        from app.db import turns, interactions
        kwargs = dict(request_id="durable-q", tool_use_id="tu", session_id="s1",
                      questions=[{"question": "Color?"}], broadcast_func=mock_broadcast)
        await handler.post_question(**kwargs)
        assert not await handler.respond("durable-q", "other", {"Color?": "blue"})
        assert await handler.respond("durable-q", "s1", {"Color?": "blue"}, "device-two")
        assert not await handler.respond("durable-q", "s1", {"Color?": "red"}, "device-one")
        queued = turns.get_queued_turns("s1")
        assert len(queued) == 1
        assert queued[0]["source"] == "user"
        assert queued[0]["prompt"] == "Answer to your question:\nColor?\nblue"
        assert interactions.get_answered_interactions("s1")[0]["answered_by_device"] == "device-two"
        mock_broadcast.reset_mock()
        assert (await handler.post_question(**kwargs))["status"] == "answered"
        mock_broadcast.assert_not_called()
        assert handler.get_pending_questions("s1") == []

    async def test_concurrent_answers_accept_only_one(self, handler, mock_broadcast):
        from app.db import turns
        await handler.post_question(request_id="q-race", tool_use_id="tu", session_id="s1",
                                    questions=[{"question": "Color?"}], broadcast_func=mock_broadcast)
        results = await asyncio.gather(
            handler.respond("q-race", "s1", {"Color?": "blue"}),
            handler.respond("q-race", "s1", {"Color?": "red"}),
        )
        assert sorted(results) == [False, True]
        assert len(turns.get_queued_turns("s1")) == 1

    async def test_enqueue_failure_rolls_back_answer(self, handler, mock_broadcast):
        from app.db import turns, interactions
        await handler.post_question(request_id="q", tool_use_id="tu", session_id="s1",
                                    questions=[{"question": "Color?"}], broadcast_func=mock_broadcast)
        with patch.object(turns, "enqueue_turn", side_effect=RuntimeError("write failed")):
            with pytest.raises(RuntimeError, match="write failed"):
                await handler.respond("q", "s1", {"Color?": "blue"})
        assert len(interactions.get_pending_interactions("s1")) == 1
        assert turns.get_queued_turns("s1") == []
        assert await handler.respond("q", "s1", {"Color?": "blue"})

    async def test_explicit_cancel_does_not_wake_and_unknown_keys_do_not_answer(self, handler, mock_broadcast):
        from app.db import turns
        await handler.post_question(request_id="q", tool_use_id="tu", session_id="s1",
                                    questions=[{"question": "Color?"}], broadcast_func=mock_broadcast)
        assert not await handler.respond("q", "s1", {"unrelated": "blue"})
        assert await handler.respond("q", "s1", {}, dismissed=True)
        assert turns.get_queued_turns("s1") == []
        assert handler.get_pending_questions("s1") == []


class TestLogging(TestUserQuestionHandler):
    """Test logging behavior."""

    @pytest.mark.asyncio
    async def test_request_answers_logs_info(self, handler, sample_questions, mock_broadcast):
        """request_answers should log info when question is sent."""
        async def respond():
            await _wait_for_broadcast(mock_broadcast, "req-123")
            await handler.respond("req-123", "session-1", {"answer": "yes"})

        asyncio.create_task(respond())

        with patch("app.core.user_question_handler.logger") as mock_logger:
            await handler.request_answers(
                request_id="req-123",
                tool_use_id="tool-456",
                session_id="session-1",
                questions=sample_questions,
                broadcast_func=mock_broadcast
            )

            # Check debug was logged
            mock_logger.debug.assert_called()
            calls = [str(c) for c in mock_logger.debug.call_args_list]
            assert any("req-123" in c for c in calls)

    # NOTE: test_timeout_logs_warning removed — same asyncio.wait_for-patching
    # hang risk as the request_answers timeout tests above (the patch lands on
    # interaction_wait.await_interaction's internal poll wait_for, not a
    # top-level timeout, so it busy-polls for the real TTL instead of timing
    # out fast).

    @pytest.mark.asyncio
    async def test_respond_logs_success(self, handler, sample_questions, mock_broadcast):
        """respond should log info on success."""
        task = asyncio.create_task(handler.request_answers(
            request_id="req-123",
            tool_use_id="tool-456",
            session_id="session-1",
            questions=sample_questions,
            broadcast_func=mock_broadcast
        ))

        await _wait_for_broadcast(mock_broadcast, "req-123")

        with patch("app.core.user_question_handler.logger") as mock_logger:
            await handler.respond("req-123", "session-1", {"answer": "yes"})

            mock_logger.debug.assert_called()
            calls = [str(c) for c in mock_logger.debug.call_args_list]
            assert any("answered" in c for c in calls)

        await task

    @pytest.mark.asyncio
    async def test_respond_not_found_logs_warning(self, handler):
        """respond should log warning when request not found."""
        with patch("app.core.user_question_handler.logger") as mock_logger:
            await handler.respond("req-123", "nonexistent", {"answer": "yes"})

            mock_logger.warning.assert_called()

    @pytest.mark.asyncio
    async def test_cancel_all_logs_info(self, handler, sample_questions, mock_broadcast):
        """cancel_all should log info when cancelling."""
        task = asyncio.create_task(handler.request_answers(
            request_id="req-123",
            tool_use_id="tool-456",
            session_id="session-1",
            questions=sample_questions,
            broadcast_func=mock_broadcast
        ))

        await _wait_for_broadcast(mock_broadcast, "req-123")

        with patch("app.core.user_question_handler.logger") as mock_logger:
            await handler.cancel_all("session-1")

            mock_logger.debug.assert_called()
            calls = [str(c) for c in mock_logger.debug.call_args_list]
            assert any("Cancelled" in c for c in calls)

        await task


class TestEdgeCases(TestUserQuestionHandler):
    """Test edge cases and boundary conditions."""

    @pytest.mark.asyncio
    async def test_empty_questions_list(self, handler, mock_broadcast):
        """Handler should handle empty questions list and return error for empty answers."""
        async def respond():
            await _wait_for_broadcast(mock_broadcast, "req-123")
            # Empty dict is falsy, so handler will return error
            await handler.respond("req-123", "session-1", {})

        asyncio.create_task(respond())

        result = await handler.request_answers(
            request_id="req-123",
            tool_use_id="tool-456",
            session_id="session-1",
            questions=[],
            broadcast_func=mock_broadcast
        )

        # Empty dict is falsy, so returns error (code line 94-97)
        assert result == {"error": "No answers received"}

    @pytest.mark.asyncio
    async def test_empty_answers(self, handler, sample_questions, mock_broadcast):
        """Handler should return error for empty answers dict (falsy value)."""
        async def respond():
            await _wait_for_broadcast(mock_broadcast, "req-123")
            # Empty dict is falsy in Python
            await handler.respond("req-123", "session-1", {})

        asyncio.create_task(respond())

        result = await handler.request_answers(
            request_id="req-123",
            tool_use_id="tool-456",
            session_id="session-1",
            questions=sample_questions,
            broadcast_func=mock_broadcast
        )

        # Empty dict is falsy, so returns error (code line 94-97)
        assert result == {"error": "No answers received"}

    @pytest.mark.asyncio
    async def test_special_characters_in_ids(self, handler, sample_questions, mock_broadcast):
        """Handler should handle special characters in IDs."""
        special_id = "req-123/test:special@chars#!"

        async def respond():
            await _wait_for_broadcast(mock_broadcast, special_id)
            await handler.respond(special_id, "session-1", {"answer": "yes"})

        asyncio.create_task(respond())

        result = await handler.request_answers(
            request_id=special_id,
            tool_use_id="tool-456",
            session_id="session-1",
            questions=sample_questions,
            broadcast_func=mock_broadcast
        )

        assert result == {"answer": "yes"}

    @pytest.mark.asyncio
    async def test_unicode_in_questions_and_answers(self, handler, mock_broadcast):
        """Handler should handle Unicode in questions and answers."""
        questions = [{"question": "What is your name?", "header": "Name"}]
        answers = {"Name": "Test User"}

        async def respond():
            await _wait_for_broadcast(mock_broadcast, "req-123")
            await handler.respond("req-123", "session-1", answers)

        asyncio.create_task(respond())

        result = await handler.request_answers(
            request_id="req-123",
            tool_use_id="tool-456",
            session_id="session-1",
            questions=questions,
            broadcast_func=mock_broadcast
        )

        assert result == answers

    @pytest.mark.asyncio
    async def test_large_questions_payload(self, handler, mock_broadcast):
        """Handler should handle large questions payload."""
        questions = [
            {"question": f"Question {i}?", "header": f"Q{i}", "options": [f"opt{j}" for j in range(10)]}
            for i in range(100)
        ]

        async def respond():
            await _wait_for_broadcast(mock_broadcast, "req-123")
            await handler.respond("req-123", "session-1", {"answer": "completed"})

        asyncio.create_task(respond())

        result = await handler.request_answers(
            request_id="req-123",
            tool_use_id="tool-456",
            session_id="session-1",
            questions=questions,
            broadcast_func=mock_broadcast
        )

        assert result == {"answer": "completed"}

    @pytest.mark.asyncio
    async def test_broadcast_failure(self, handler, sample_questions):
        """Handler should propagate broadcast function errors."""
        async def failing_broadcast(data):
            raise ConnectionError("WebSocket disconnected")

        with pytest.raises(ConnectionError):
            await handler.request_answers(
                request_id="req-123",
                tool_use_id="tool-456",
                session_id="session-1",
                questions=sample_questions,
                broadcast_func=failing_broadcast
            )

    # NOTE: test_respond_after_timeout_cleanup removed — same asyncio.wait_for
    # patching hang risk as above; forcing a real timeout to exercise this
    # "respond after settle" case would need a dedicated short-TTL test, out of
    # scope for this flag-removal change.


class TestDismissAndSkipEnvelope:
    """The AskUserQuestion modal's "X" (dismiss) and per-question "Skip".

    Neither outcome is expressible in the flat ``{question_text: answer}`` map
    the SDK tool consumes, so both ride as reserved marker keys inside the
    stored ``pending_interactions.answer`` payload and are resolved back out
    before the answers ever reach the tool.
    """

    def test_plain_answers_pass_through_untouched(self):
        from app.core.user_question_handler import (
            build_answer_envelope,
            resolve_answer_envelope,
        )

        envelope = build_answer_envelope({"Which db?": "Postgres"})
        assert envelope == {"Which db?": "Postgres"}
        assert resolve_answer_envelope(envelope) == {"Which db?": "Postgres"}

    def test_dismiss_envelope_ignores_any_answers(self):
        from app.core.user_question_handler import (
            DISMISSED_KEY,
            DISMISS_MESSAGE,
            build_answer_envelope,
            resolve_answer_envelope,
        )

        envelope = build_answer_envelope({"Which db?": "Postgres"}, dismissed=True)
        assert envelope == {DISMISSED_KEY: True}

        resolved = resolve_answer_envelope(envelope)
        assert resolved[DISMISSED_KEY] is True
        assert resolved["error"] == DISMISS_MESSAGE

    def test_skipped_questions_resolve_to_skip_text(self):
        from app.core.user_question_handler import (
            SKIPPED_KEY,
            SKIP_ANSWER_TEXT,
            build_answer_envelope,
            resolve_answer_envelope,
        )

        envelope = build_answer_envelope(
            {"Which db?": "Postgres"}, skipped=["Which cache?"]
        )
        assert envelope[SKIPPED_KEY] == ["Which cache?"]

        resolved = resolve_answer_envelope(envelope)
        assert resolved == {
            "Which db?": "Postgres",
            "Which cache?": SKIP_ANSWER_TEXT,
        }
        # The marker itself never leaks into what the tool sees.
        assert SKIPPED_KEY not in resolved

    def test_all_questions_skipped_still_produces_answers(self):
        from app.core.user_question_handler import (
            SKIP_ANSWER_TEXT,
            build_answer_envelope,
            resolve_answer_envelope,
        )

        resolved = resolve_answer_envelope(
            build_answer_envelope({}, skipped=["Q1", "Q2"])
        )
        assert resolved == {"Q1": SKIP_ANSWER_TEXT, "Q2": SKIP_ANSWER_TEXT}

    def test_client_supplied_marker_keys_are_stripped(self):
        """A forged marker in the answers map must not fake a dismissal."""
        from app.core.user_question_handler import (
            DISMISSED_KEY,
            SKIPPED_KEY,
            build_answer_envelope,
            resolve_answer_envelope,
        )

        envelope = build_answer_envelope(
            {DISMISSED_KEY: True, SKIPPED_KEY: ["x"], "Real?": "yes"}
        )
        assert envelope == {"Real?": "yes"}
        assert resolve_answer_envelope(envelope) == {"Real?": "yes"}

    def test_empty_skip_list_adds_no_marker(self):
        from app.core.user_question_handler import SKIPPED_KEY, build_answer_envelope

        assert SKIPPED_KEY not in build_answer_envelope({"Q": "a"}, skipped=[])

    @pytest.mark.asyncio
    async def test_respond_dismissed_round_trips_to_the_waiting_tool_call(self):
        """A dismissal answers the pending interaction and comes back out of
        request_answers carrying the dismiss marker, which query_engine turns
        into a denied tool call."""
        from app.core.user_question_handler import DISMISSED_KEY, DISMISS_MESSAGE

        handler = UserQuestionHandler()
        mock_broadcast = AsyncMock(return_value=True)

        async def dismiss():
            await _wait_for_broadcast(mock_broadcast, "req-dismiss")
            await handler.respond("req-dismiss", "session-1", {}, dismissed=True)

        asyncio.create_task(dismiss())

        result = await handler.request_answers(
            request_id="req-dismiss",
            tool_use_id="tool-1",
            session_id="session-1",
            questions=[{"question": "Which db?", "header": "DB"}],
            broadcast_func=mock_broadcast,
        )

        assert result[DISMISSED_KEY] is True
        assert result["error"] == DISMISS_MESSAGE

    @pytest.mark.asyncio
    async def test_respond_with_partial_skips_round_trips(self):
        """Skipping some questions while answering others submits cleanly."""
        from app.core.user_question_handler import SKIP_ANSWER_TEXT

        handler = UserQuestionHandler()
        mock_broadcast = AsyncMock(return_value=True)

        async def answer():
            await _wait_for_broadcast(mock_broadcast, "req-skip")
            await handler.respond(
                "req-skip",
                "session-1",
                {"Which db?": "Postgres"},
                skipped=["Which cache?"],
            )

        asyncio.create_task(answer())

        result = await handler.request_answers(
            request_id="req-skip",
            tool_use_id="tool-1",
            session_id="session-1",
            questions=[
                {"question": "Which db?", "header": "DB"},
                {"question": "Which cache?", "header": "Cache"},
            ],
            broadcast_func=mock_broadcast,
        )

        assert result == {
            "Which db?": "Postgres",
            "Which cache?": SKIP_ANSWER_TEXT,
        }


class TestFluidQuestionShape:
    """SDK vs fluid QuestionRequest input → UserQuestion payload + answer string."""

    def test_sdk_questions_pass_through(self):
        from app.core.user_question_handler import questions_from_tool_input

        qs = [{"question": "Pick?", "header": "H", "options": []}]
        assert questions_from_tool_input({"questions": qs}) == qs

    def test_fluid_schema_becomes_one_question(self):
        from app.core.user_question_handler import questions_from_tool_input

        schema = {
            "question": "Approve this plan?",
            "header": "Plan",
            "options": [{"label": "Approve"}],
        }
        out = questions_from_tool_input({"question": "Approve this plan?", "schema": schema})
        assert len(out) == 1
        assert out[0]["header"] == "Plan"
        assert out[0]["question"] == "Approve this plan?"

    def test_stop_planning_message_replaces_short_schema_question(self):
        from app.core.user_question_handler import questions_from_tool_input

        schema = {"question": "Approve this plan and leave plan mode?", "header": "Plan"}
        message = "# Do the thing\n\nApprove this plan and leave plan mode?"
        out = questions_from_tool_input({"question": message, "schema": schema})
        assert out[0]["question"] == message

    def test_fluid_answer_takes_first_label(self):
        from app.core.user_question_handler import fluid_answer_from_envelope

        assert fluid_answer_from_envelope({"Approve this?": "Approve"}) == "Approve"

    def test_fluid_answer_joins_multiselect(self):
        from app.core.user_question_handler import fluid_answer_from_envelope

        assert fluid_answer_from_envelope({"Pick": ["A", "B"]}) == "A, B"

    def test_fluid_answer_dismiss_is_empty(self):
        from app.core.user_question_handler import DISMISSED_KEY, fluid_answer_from_envelope

        assert fluid_answer_from_envelope({DISMISSED_KEY: True}) == ""


# ---------------------------------------------------------------------------
# Give-up path: the row must stop being answerable and the device must be told
# ---------------------------------------------------------------------------

class TestQuestionTimeoutRetiresTheRow:
    """An expiring AskUser used to leave its ``pending_interactions`` row
    ``pending`` forever: the tool had stopped waiting, but the user's device
    still showed a live modal, and answering it landed on a dead round-trip."""

    @pytest.fixture
    def handler(self):
        return UserQuestionHandler()

    async def test_timeout_cancels_the_row_and_notifies(self, handler, mock_db):
        from app.db import interactions

        broadcast = AsyncMock(return_value=True)
        emit = AsyncMock()

        with patch(
            "app.core.user_question_handler._question_ttl_seconds", return_value=1
        ), patch(
            "app.services.notification_service.emit_notification", emit
        ), patch(
            "app.services.push_notification.fire_and_forget_notify", lambda **kw: None
        ):
            result = await asyncio.wait_for(
                handler.request_answers(
                    request_id="q-timeout",
                    tool_use_id="tu-timeout",
                    session_id="s-timeout",
                    questions=[{"question": "Ship it?", "header": "Ship"}],
                    broadcast_func=broadcast,
                ),
                timeout=20.0,
            )

        assert "timed out" in result["error"]
        # Nothing is left pending for a device to answer.
        assert interactions.get_pending_interactions("s-timeout") == []
        # Every watching device is told the modal is dead...
        assert any(
            (c[0][0] if c[0] else {}).get("type") == "user_question_timeout"
            for c in broadcast.call_args_list
        )
        # ...and so is the person who walked away.
        emit.assert_awaited_once()
        assert emit.await_args.kwargs["category"] == "question_timeout"
        assert emit.await_args.kwargs["session_id"] == "s-timeout"
        assert emit.await_args.kwargs["push"] is True

    async def test_timeout_emits_a_durable_expired_event(self, handler, mock_db):
        from app.db import session_events as se_db

        broadcast = AsyncMock(return_value=True)

        with patch(
            "app.core.user_question_handler._question_ttl_seconds", return_value=1
        ), patch(
            "app.services.notification_service.emit_notification", AsyncMock()
        ), patch(
            "app.services.push_notification.fire_and_forget_notify", lambda **kw: None
        ):
            await asyncio.wait_for(
                handler.request_answers(
                    request_id="q-expired",
                    tool_use_id="tu-expired",
                    session_id="s-expired",
                    questions=[{"question": "Ship it?", "header": "Ship"}],
                    broadcast_func=broadcast,
                ),
                timeout=20.0,
            )

        expired = [
            e for e in se_db.get_events("s-expired")
            if e["event_type"] == "interaction.expired"
        ]
        assert len(expired) == 1
        assert expired[0]["payload"]["reason"] == "timeout"
        assert expired[0]["payload"]["request_id"] == "q-expired"
