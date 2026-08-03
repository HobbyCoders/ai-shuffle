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
from datetime import datetime, timezone

from app.core.config import settings

logger = logging.getLogger(__name__)

# V2 Phase 4 (Q4): AskUserQuestion prompts persist as ``pending_interactions``
# rows of this kind, so any install answers them relay-free. TTL is the
# durable form of the legacy user_question_timeout auto-reap (Q4-5, default
# 3600s).
QUESTION_KIND = "question"


def _question_ttl_seconds() -> int:
    return int(getattr(settings, "user_question_timeout_seconds", 3600))


@dataclass
class QuestionRequest:
    """A pending user question request."""
    request_id: str
    tool_use_id: str
    session_id: str
    questions: List[Dict[str, Any]]
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
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
        broadcast_func: callable,
        fanout_func: Optional[callable] = None
    ) -> Dict[str, Any]:
        """
        Request answers from the user for a set of questions.

        Args:
            request_id: Unique ID for this request
            tool_use_id: The tool use ID from the SDK
            session_id: The chat session ID
            questions: List of question objects with question, header, options, multiSelect
            broadcast_func: Async function to broadcast to WebSocket (origin socket)
            fanout_func: Optional async function to ALSO deliver the same question
                payload to every device watching this session (via the sync engine).
                The origin ``broadcast_func`` only reaches the one socket that owns
                this query — which is nobody on a headless turn, a dead-on-reconnect
                socket, or when the question originated on another device/install.
                The fanout is the recovery path; the frontend dedupes by request_id
                so a device that got both the direct event and the fanout renders
                the modal once.

        Returns:
            Dict with user's answers
        """
        return await self._request_answers_v2(
            request_id=request_id,
            tool_use_id=tool_use_id,
            session_id=session_id,
            questions=questions,
            broadcast_func=broadcast_func,
            fanout_func=fanout_func,
        )

    # ------------------------------------------------------------------
    # V2 Phase 4 (Q4) — pending_interactions-backed question flow
    #
    # The durable ``pending_interactions`` row is the single source of truth:
    # the executing runner blocks on it via
    # ``interaction_wait.await_interaction`` while ANY install answers it
    # directly against the shared table (no cloud_relay). All frontend WebSocket
    # shapes (``user_question`` / ``user_question_timeout``) are byte-identical
    # to the legacy path.
    # ------------------------------------------------------------------

    @staticmethod
    def _v2_pending_questions(session_id: str) -> List[Dict[str, Any]]:
        from app.db import interactions

        return [
            r
            for r in interactions.get_pending_interactions(session_id)
            if r.get("kind") == QUESTION_KIND
        ]

    @staticmethod
    def _question_row_to_dict(row: Dict[str, Any]) -> Dict[str, Any]:
        payload = row.get("payload") or {}
        created = row.get("created_at")
        if hasattr(created, "isoformat"):
            created_at = created.isoformat()
        elif created is not None:
            created_at = str(created)
        else:
            created_at = ""
        return {
            "session_id": row.get("session_id"),
            "request_id": payload.get("request_id"),
            "tool_use_id": payload.get("tool_use_id"),
            "questions": payload.get("questions"),
            "created_at": created_at,
        }

    async def _request_answers_v2(
        self,
        request_id: str,
        tool_use_id: str,
        session_id: str,
        questions: List[Dict[str, Any]],
        broadcast_func: callable,
        fanout_func: Optional[callable] = None,
    ) -> Dict[str, Any]:
        """Table-backed request_answers (V2 Phase 4)."""
        from app.core import interaction_wait
        from app.db import interactions

        payload = {
            "request_id": request_id,
            "tool_use_id": tool_use_id,
            "questions": questions,
        }
        ttl = _question_ttl_seconds()
        row = await asyncio.to_thread(
            interactions.create_interaction, session_id, QUESTION_KIND, payload, ttl
        )
        interaction_id = row["id"]

        question_payload = {
            "type": "user_question",
            "session_id": session_id,
            "request_id": request_id,
            "tool_use_id": tool_use_id,
            "questions": questions,
        }
        delivered = await broadcast_func(question_payload)
        if delivered is False:
            logger.warning(
                f"User question {request_id} could not be delivered to the origin "
                f"socket (connection dead); awaiting reconnect resync for session {session_id}"
            )
        else:
            logger.debug(f"User question request sent: {request_id} with {len(questions)} questions")

        if fanout_func is not None:
            try:
                await fanout_func(question_payload)
            except Exception:
                logger.debug(
                    f"User question {request_id} fanout to session watchers failed",
                    exc_info=True,
                )

        # Durable lifecycle event (#653): emit AFTER the WS broadcast + fanout,
        # swallowed on failure so it can never break the question flow.
        from app.core import interaction_events

        await interaction_events.emit_interaction_requested(
            session_id, interaction_id, request_id, QUESTION_KIND, payload,
        )

        try:
            from app.services.push_notification import fire_and_forget_notify

            first_q = questions[0].get("question") if questions else "A question"
            fire_and_forget_notify(
                title="Question for you",
                body=str(first_q),
                data={
                    "type": "user_question",
                    "session_id": session_id,
                    "request_id": request_id,
                },
            )
        except Exception:
            pass

        result_row = await interaction_wait.await_interaction(
            interaction_id, session_id, float(ttl)
        )

        state = (result_row or {}).get("state")
        if result_row is None or state in ("expired", "pending"):
            try:
                await broadcast_func({
                    "type": "user_question_timeout",
                    "session_id": session_id,
                    "request_id": request_id,
                })
            except Exception:
                pass
            logger.warning(f"User question request timed out: {request_id}")
            return {"error": "Question timed out - no response from user"}
        if state == "cancelled":
            return {"error": "No answers received"}

        answers = result_row.get("answer")
        if answers:
            return answers
        return {"error": "No answers received"}

    async def _respond_v2(
        self,
        request_id: str,
        session_id: str,
        answers: Dict[str, Any],
        answered_by_device: Optional[str],
    ) -> bool:
        from app.core import interaction_wait
        from app.db import interactions

        row = next(
            (
                r
                for r in self._v2_pending_questions(session_id)
                if (r.get("payload") or {}).get("request_id") == request_id
            ),
            None,
        )
        if row is None:
            logger.warning(f"Question request not found: {request_id}")
            return False
        answered = interactions.answer_interaction(row["id"], answers, answered_by_device)
        if answered:
            interaction_wait.notify_changed(session_id)
            logger.debug(f"User question answered: {request_id}")
            # Durable lifecycle event (#653): guarded answer won → emit exactly
            # one interaction.answered. AFTER notify_changed.
            from app.core import interaction_events

            await interaction_events.emit_interaction_answered(
                session_id, row["id"], request_id, answers,
            )
        return answered

    async def respond(
        self,
        request_id: str,
        session_id: str,
        answers: Dict[str, Any],
        answered_by_device: Optional[str] = None,
    ) -> bool:
        """
        Respond to a user question request.

        Args:
            request_id: The question request ID
            session_id: The session ID
            answers: Dict of answers keyed by question header
            answered_by_device: Device id of the answering install (V2 Phase 4,
                Q4-6).

        Returns:
            True if response was processed, False if request not found
        """
        return await self._respond_v2(
            request_id, session_id, answers, answered_by_device
        )

    def get_pending_questions(self, session_id: str) -> List[Dict[str, Any]]:
        """Get all pending questions for a session."""
        return [
            self._question_row_to_dict(row)
            for row in self._v2_pending_questions(session_id)
        ]

    async def cancel_all(self, session_id: str):
        """Cancel all pending questions for a session."""
        from app.core import interaction_wait
        from app.db import interactions

        cancelled = 0
        cancelled_rows: List[tuple] = []
        for row in self._v2_pending_questions(session_id):
            if interactions.cancel_interaction(row["id"]):
                cancelled += 1
                cancelled_rows.append(
                    (row["id"], (row.get("payload") or {}).get("request_id"))
                )
        if cancelled:
            interaction_wait.notify_changed(session_id)
            logger.debug(f"Cancelled all pending questions for session {session_id}")
            # Durable lifecycle events (#653): one interaction.expired
            # (reason="cancelled") per row whose guarded cancel won.
            from app.core import interaction_events

            for cancelled_interaction_id, rid in cancelled_rows:
                await interaction_events.emit_interaction_expired(
                    session_id, cancelled_interaction_id, rid, "cancelled",
                )


# Global singleton instance
user_question_handler = UserQuestionHandler()
