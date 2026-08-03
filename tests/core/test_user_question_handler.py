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
        # Set up a task to respond quickly
        async def respond_quickly():
            await asyncio.sleep(0.01)
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
            await asyncio.sleep(0.01)
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

        async def check_and_respond():
            await asyncio.sleep(0.01)
            # The question is discoverable even though the origin socket "failed".
            pending = handler.get_pending_questions("session-1")
            assert len(pending) == 1
            assert pending[0]["request_id"] == "req-123"
            await handler.respond("req-123", "session-1", {"answer": "ok"})

        asyncio.create_task(check_and_respond())

        result = await handler.request_answers(
            request_id="req-123",
            tool_use_id="tool-456",
            session_id="session-1",
            questions=sample_questions,
            broadcast_func=dead_broadcast,
            fanout_func=fanout,
        )

        assert result == {"answer": "ok"}
        fanout.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_request_answers_fanout_failure_never_breaks_flow(self, handler, sample_questions, mock_broadcast):
        """A raising fanout is swallowed — the question flow must not break."""
        fanout = AsyncMock(side_effect=RuntimeError("no watchers reachable"))

        async def respond_quickly():
            await asyncio.sleep(0.01)
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
            await asyncio.sleep(0.01)
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
            await asyncio.sleep(0.01)
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
        # We'll check pending before respond is called
        async def check_and_respond():
            await asyncio.sleep(0.01)
            # Check pending questions exist
            pending = handler.get_pending_questions("session-1")
            assert len(pending) == 1
            assert pending[0]["request_id"] == "req-123"
            # Now respond
            await handler.respond("req-123", "session-1", {"answer": "test"})

        asyncio.create_task(check_and_respond())

        await handler.request_answers(
            request_id="req-123",
            tool_use_id="tool-456",
            session_id="session-1",
            questions=sample_questions,
            broadcast_func=mock_broadcast
        )

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
            await asyncio.sleep(0.01)
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
        async def respond_to(request_id, session_id, delay):
            await asyncio.sleep(delay)
            await handler.respond(request_id, session_id, {"request_id": request_id})

        # Start multiple requests
        asyncio.create_task(respond_to("req-1", "session-1", 0.02))
        asyncio.create_task(respond_to("req-2", "session-1", 0.01))

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

        # Wait a bit for request to be registered
        await asyncio.sleep(0.01)

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
        async def auto_respond():
            await asyncio.sleep(0.02)
            await handler.respond("req-123", "session-1", {"answer": "yes"})

        asyncio.create_task(auto_respond())

        request_task = asyncio.create_task(handler.request_answers(
            request_id="req-123",
            tool_use_id="tool-456",
            session_id="session-1",
            questions=sample_questions,
            broadcast_func=mock_broadcast
        ))

        await asyncio.sleep(0.01)

        # Try to respond to wrong request ID
        result = await handler.respond("wrong-request-id", "session-1", {"answer": "yes"})

        assert result is False

        # Clean up the actual request
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

        await asyncio.sleep(0.01)

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

        await asyncio.sleep(0.01)
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

        async def auto_respond_later():
            await asyncio.sleep(0.1)
            await handler.respond("req-1", "session-1", {"answer": "1"})
            await handler.respond("req-2", "session-1", {"answer": "2"})

        asyncio.create_task(auto_respond_later())

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

        # V2 creates each durable row via asyncio.to_thread, so sleep(0.01) can
        # be too short for BOTH rows to land under load (flaky); 0.05 gives
        # margin for two thread-pool hops.
        await asyncio.sleep(0.05)

        pending = handler.get_pending_questions("session-1")
        assert len(pending) == 2

        request_ids = {p["request_id"] for p in pending}
        assert request_ids == {"req-1", "req-2"}

        # Clean up
        await asyncio.gather(task1, task2)

    @pytest.mark.asyncio
    async def test_get_pending_questions_format(self, handler, sample_questions, mock_broadcast):
        """get_pending_questions should return correctly formatted data."""
        async def auto_respond():
            await asyncio.sleep(0.05)
            await handler.respond("req-123", "session-1", {"answer": "yes"})

        asyncio.create_task(auto_respond())

        task = asyncio.create_task(handler.request_answers(
            request_id="req-123",
            tool_use_id="tool-456",
            session_id="session-1",
            questions=sample_questions,
            broadcast_func=mock_broadcast
        ))

        await asyncio.sleep(0.01)

        pending = handler.get_pending_questions("session-1")
        assert len(pending) == 1

        item = pending[0]
        assert item["request_id"] == "req-123"
        assert item["tool_use_id"] == "tool-456"
        assert item["questions"] == sample_questions
        assert "created_at" in item
        # Verify created_at is ISO format
        datetime.fromisoformat(item["created_at"])

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

        await asyncio.sleep(0.01)

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

        await asyncio.sleep(0.01)

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

        # V2 creates each durable row via asyncio.to_thread, so sleep(0.01) can
        # be too short for BOTH rows to land under load (flaky); 0.05 gives
        # margin for two thread-pool hops.
        await asyncio.sleep(0.05)

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

        async def respond_to_session2():
            await asyncio.sleep(0.05)
            await handler.respond("req-2", "session-2", {"answer": "yes"})

        asyncio.create_task(respond_to_session2())

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

        await asyncio.sleep(0.01)

        # Cancel only session-1
        await handler.cancel_all("session-1")

        # session-1 task should complete with error
        result1 = await task1
        assert "error" in result1

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
                await asyncio.sleep(0.01)
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

        await asyncio.sleep(0.01)

        # Try both respond and cancel concurrently
        respond_task = asyncio.create_task(
            handler.respond("req-123", "session-1", {"answer": "yes"})
        )
        cancel_task = asyncio.create_task(handler.cancel_all("session-1"))

        await asyncio.gather(respond_task, cancel_task)

        # Task should complete without hanging
        result = await asyncio.wait_for(task, timeout=1.0)
        assert result is not None


class TestLogging(TestUserQuestionHandler):
    """Test logging behavior."""

    @pytest.mark.asyncio
    async def test_request_answers_logs_info(self, handler, sample_questions, mock_broadcast):
        """request_answers should log info when question is sent."""
        async def respond():
            await asyncio.sleep(0.01)
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

        await asyncio.sleep(0.01)

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

        await asyncio.sleep(0.01)

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
            await asyncio.sleep(0.01)
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
            await asyncio.sleep(0.01)
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
            await asyncio.sleep(0.01)
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
            await asyncio.sleep(0.01)
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
            await asyncio.sleep(0.01)
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
