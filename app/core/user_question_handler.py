"""
User Question Handler

Handles AskUserQuestion tool calls by:
1. Broadcasting questions to the frontend via WebSocket
2. Waiting for user responses
3. Returning answers back to the SDK
"""

import asyncio
import logging
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class QuestionRequest:
    """A pending user question request."""
    request_id: str
    tool_use_id: str
    session_id: str
    questions: List[Dict[str, Any]]
    created_at: datetime = field(default_factory=datetime.utcnow)
    response_event: asyncio.Event = field(default_factory=asyncio.Event)
    answers: Optional[Dict[str, Any]] = None


class UserQuestionHandler:
    """Handles user questions from Claude with a queue system."""

    def __init__(self):
        # Pending question requests by session_id -> request_id -> request
        self._pending_questions: Dict[str, Dict[str, QuestionRequest]] = {}
        self._lock = asyncio.Lock()

    async def request_answers(
        self,
        request_id: str,
        tool_use_id: str,
        session_id: str,
        questions: List[Dict[str, Any]],
        broadcast_func: callable
    ) -> Dict[str, Any]:
        """
        Request answers from the user for a set of questions.

        Args:
            request_id: Unique ID for this request
            tool_use_id: The tool use ID from the SDK
            session_id: The chat session ID
            questions: List of question objects with question, header, options, multiSelect
            broadcast_func: Async function to broadcast to WebSocket

        Returns:
            Dict with user's answers
        """
        # Create pending request
        request = QuestionRequest(
            request_id=request_id,
            tool_use_id=tool_use_id,
            session_id=session_id,
            questions=questions
        )

        async with self._lock:
            if session_id not in self._pending_questions:
                self._pending_questions[session_id] = {}
            self._pending_questions[session_id][request_id] = request

        # Broadcast question request to frontend
        await broadcast_func({
            "type": "user_question",
            "request_id": request_id,
            "tool_use_id": tool_use_id,
            "questions": questions
        })

        logger.info(f"User question request sent: {request_id} with {len(questions)} questions")

        # Wait for response (with timeout)
        try:
            await asyncio.wait_for(request.response_event.wait(), timeout=600.0)  # 10 min timeout
        except asyncio.TimeoutError:
            async with self._lock:
                if session_id in self._pending_questions:
                    self._pending_questions[session_id].pop(request_id, None)
            logger.warning(f"User question request timed out: {request_id}")
            return {"error": "Question timed out - no response from user"}

        # Return the answers
        if request.answers:
            return request.answers
        else:
            return {"error": "No answers received"}

    async def respond(
        self,
        request_id: str,
        session_id: str,
        answers: Dict[str, Any]
    ) -> bool:
        """
        Respond to a user question request.

        Args:
            request_id: The question request ID
            session_id: The session ID
            answers: Dict of answers keyed by question header

        Returns:
            True if response was processed, False if request not found
        """
        async with self._lock:
            if session_id not in self._pending_questions:
                logger.warning(f"No pending questions for session {session_id}")
                return False

            request = self._pending_questions[session_id].get(request_id)
            if not request:
                logger.warning(f"Question request not found: {request_id}")
                return False

            # Store the answers
            request.answers = answers

            # Remove from pending
            del self._pending_questions[session_id][request_id]
            if not self._pending_questions[session_id]:
                del self._pending_questions[session_id]

        # Signal that we have a response
        request.response_event.set()
        logger.info(f"User question answered: {request_id}")

        return True

    def get_pending_questions(self, session_id: str) -> List[Dict[str, Any]]:
        """Get all pending questions for a session."""
        if session_id not in self._pending_questions:
            return []

        return [
            {
                "request_id": req.request_id,
                "tool_use_id": req.tool_use_id,
                "questions": req.questions,
                "created_at": req.created_at.isoformat()
            }
            for req in self._pending_questions[session_id].values()
        ]

    async def cancel_all(self, session_id: str):
        """Cancel all pending questions for a session."""
        async with self._lock:
            if session_id in self._pending_questions:
                for request in self._pending_questions[session_id].values():
                    request.response_event.set()  # Unblock waiting coroutines
                del self._pending_questions[session_id]
                logger.info(f"Cancelled all pending questions for session {session_id}")


# Global singleton instance
user_question_handler = UserQuestionHandler()
