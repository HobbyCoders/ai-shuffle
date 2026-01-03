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
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

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
        before = datetime.utcnow()
        request = QuestionRequest(
            request_id="req-123",
            tool_use_id="tool-456",
            session_id="session-789",
            questions=[]
        )
        after = datetime.utcnow()

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
        assert call_args["request_id"] == "req-123"
        assert call_args["tool_use_id"] == "tool-456"
        assert call_args["questions"] == sample_questions

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

    @pytest.mark.asyncio
    async def test_request_answers_timeout(self, handler, sample_questions, mock_broadcast):
        """request_answers should return timeout error when no response."""
        # Patch the timeout to be very short for testing
        with patch.object(asyncio, 'wait_for', side_effect=asyncio.TimeoutError()):
            result = await handler.request_answers(
                request_id="req-123",
                tool_use_id="tool-456",
                session_id="session-1",
                questions=sample_questions,
                broadcast_func=mock_broadcast
            )

        assert "error" in result
        assert "timed out" in result["error"]

    @pytest.mark.asyncio
    async def test_request_answers_timeout_cleans_up_pending(self, handler, sample_questions, mock_broadcast):
        """request_answers should remove pending request on timeout."""
        with patch.object(asyncio, 'wait_for', side_effect=asyncio.TimeoutError()):
            await handler.request_answers(
                request_id="req-123",
                tool_use_id="tool-456",
                session_id="session-1",
                questions=sample_questions,
                broadcast_func=mock_broadcast
            )

        # Verify the pending question was cleaned up
        pending = handler.get_pending_questions("session-1")
        assert len(pending) == 0

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

    @pytest.mark.asyncio
    async def test_request_answers_no_answers_returns_error(self, handler, sample_questions, mock_broadcast):
        """request_answers should return error if no answers set."""
        async def respond_without_answers():
            await asyncio.sleep(0.01)
            # Manually set the event without providing answers
            async with handler._lock:
                if "session-1" in handler._pending_questions:
                    request = handler._pending_questions["session-1"].get("req-123")
                    if request:
                        request.response_event.set()
                        del handler._pending_questions["session-1"]["req-123"]

        asyncio.create_task(respond_without_answers())

        result = await handler.request_answers(
            request_id="req-123",
            tool_use_id="tool-456",
            session_id="session-1",
            questions=sample_questions,
            broadcast_func=mock_broadcast
        )

        assert "error" in result
        assert "No answers received" in result["error"]

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

    @pytest.mark.asyncio
    async def test_respond_cleans_up_empty_session(self, handler, sample_questions, mock_broadcast):
        """respond should remove session dict when last request is removed."""
        # Start a request
        request_task = asyncio.create_task(handler.request_answers(
            request_id="req-123",
            tool_use_id="tool-456",
            session_id="session-1",
            questions=sample_questions,
            broadcast_func=mock_broadcast
        ))

        await asyncio.sleep(0.01)

        # Respond and wait
        await handler.respond("req-123", "session-1", {"answer": "yes"})
        await request_task

        # Session should be removed entirely
        assert "session-1" not in handler._pending_questions

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

        await asyncio.sleep(0.01)

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

    def test_get_pending_questions_different_sessions(self, handler):
        """get_pending_questions should only return questions for specified session."""
        # Manually add questions to different sessions for testing
        handler._pending_questions = {
            "session-1": {
                "req-1": QuestionRequest(
                    request_id="req-1",
                    tool_use_id="tool-1",
                    session_id="session-1",
                    questions=[{"question": "Q1"}]
                )
            },
            "session-2": {
                "req-2": QuestionRequest(
                    request_id="req-2",
                    tool_use_id="tool-2",
                    session_id="session-2",
                    questions=[{"question": "Q2"}]
                )
            }
        }

        pending1 = handler.get_pending_questions("session-1")
        pending2 = handler.get_pending_questions("session-2")

        assert len(pending1) == 1
        assert pending1[0]["request_id"] == "req-1"

        assert len(pending2) == 1
        assert pending2[0]["request_id"] == "req-2"


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
        assert len(handler.get_pending_questions("session-1")) == 0
        assert "session-1" not in handler._pending_questions

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

        await asyncio.sleep(0.01)

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

            # Check info was logged
            mock_logger.info.assert_called()
            calls = [str(c) for c in mock_logger.info.call_args_list]
            assert any("req-123" in c for c in calls)

    @pytest.mark.asyncio
    async def test_timeout_logs_warning(self, handler, sample_questions, mock_broadcast):
        """request_answers should log warning on timeout."""
        with patch.object(asyncio, 'wait_for', side_effect=asyncio.TimeoutError()):
            with patch("app.core.user_question_handler.logger") as mock_logger:
                await handler.request_answers(
                    request_id="req-123",
                    tool_use_id="tool-456",
                    session_id="session-1",
                    questions=sample_questions,
                    broadcast_func=mock_broadcast
                )

                mock_logger.warning.assert_called()
                calls = [str(c) for c in mock_logger.warning.call_args_list]
                assert any("timed out" in c for c in calls)

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

            mock_logger.info.assert_called()
            calls = [str(c) for c in mock_logger.info.call_args_list]
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

            mock_logger.info.assert_called()
            calls = [str(c) for c in mock_logger.info.call_args_list]
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

    @pytest.mark.asyncio
    async def test_respond_after_timeout_cleanup(self, handler, sample_questions, mock_broadcast):
        """Responding after timeout cleanup should return False."""
        with patch.object(asyncio, 'wait_for', side_effect=asyncio.TimeoutError()):
            await handler.request_answers(
                request_id="req-123",
                tool_use_id="tool-456",
                session_id="session-1",
                questions=sample_questions,
                broadcast_func=mock_broadcast
            )

        # Now try to respond - should fail since request was cleaned up
        result = await handler.respond("req-123", "session-1", {"answer": "late"})
        assert result is False
