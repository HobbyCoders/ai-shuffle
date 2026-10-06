"""
User Question Handler

Handles AskUserQuestion tool calls by:
1. Broadcasting questions to the frontend via WebSocket
2. Waiting for user responses
3. Returning answers back to the adapter
"""

import asyncio
import logging
import uuid
from typing import Dict, Any, List, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

# AskUserQuestion prompts persist as ``pending_interactions`` rows of this
# kind, so any install answers them relay-free. TTL is the durable form of the
# legacy user_question_timeout auto-reap (default 3600s).
QUESTION_KIND = "question"

# Dismiss / skip envelope (AskUserQuestion modal "X" and per-question "Skip").
#
# The stored ``pending_interactions.answer`` payload is normally a flat
# ``{question_text: answer_string}`` map — exactly the shape the CLI's
# AskUserQuestion tool consumes. Dismissal and per-question skips are two
# outcomes that map cannot express, so they ride as reserved marker keys that
# ``_request_answers_v2`` resolves before the answers ever reach the tool:
#
#   ``{DISMISSED_KEY: True}``            → whole dialog closed unanswered
#   ``{..., SKIPPED_KEY: [q1, q3]}``     → those questions answered with
#                                          SKIP_ANSWER_TEXT, the rest verbatim
#
# The markers are deliberately dunder-ish so they cannot collide with a real
# question's text (the map is keyed by the full question string).
DISMISSED_KEY = "__dismissed__"
SKIPPED_KEY = "__skipped__"
DISMISS_MESSAGE = "User dismissed the question dialog without answering."
SKIP_ANSWER_TEXT = "User skipped this question."


def build_answer_envelope(
    answers: Optional[Dict[str, Any]],
    dismissed: bool = False,
    skipped: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Normalize a frontend question response into the stored answer envelope."""
    if dismissed:
        return {DISMISSED_KEY: True}
    envelope: Dict[str, Any] = dict(answers or {})
    # Never let a client-supplied key masquerade as a marker.
    envelope.pop(DISMISSED_KEY, None)
    envelope.pop(SKIPPED_KEY, None)
    skipped_keys = [str(k) for k in (skipped or []) if str(k)]
    if skipped_keys:
        envelope[SKIPPED_KEY] = skipped_keys
    return envelope


def resolve_answer_envelope(envelope: Any) -> Dict[str, Any]:
    """Expand a stored envelope into what the AskUserQuestion tool should see.

    Returns either the flat answer map (skips materialized as
    ``SKIP_ANSWER_TEXT``) or, for a dismissal, a dict still carrying
    ``DISMISSED_KEY`` so the caller can deny the tool call outright.
    """
    if not isinstance(envelope, dict):
        return {"error": "No answers received"}
    if envelope.get(DISMISSED_KEY):
        return {DISMISSED_KEY: True, "error": DISMISS_MESSAGE}
    resolved = {k: v for k, v in envelope.items() if k != SKIPPED_KEY}
    for key in envelope.get(SKIPPED_KEY) or []:
        resolved[str(key)] = SKIP_ANSWER_TEXT
    return resolved


# Fluid's AskUser rides QuestionRequest events, not the CLI's
# AskUserQuestion permission gate. The adapter translates each event into the
# same {question, schema} shape and expects a single string back on
# RespondQuestion. These helpers normalize that shape onto the existing
# UserQuestion payload and extract the answer the engine wants.
FLUID_ANSWER_KEY = "_fluid_answer"
# Set truthy beside FLUID_ANSWER_KEY when a PERSON produced the answer (the
# question was answered in the UI) rather than a host policy. The adapter forwards it as ``RespondQuestion(
# user_answered=True)``; absent means unattributed.
HOST_USER_ANSWERED_KEY = "_host_user_answered"
FLUID_QUESTION_TOOLS = frozenset({"AskUser"})
QUESTION_TOOLS = frozenset({"AskUserQuestion"}) | FLUID_QUESTION_TOOLS


def questions_from_tool_input(tool_input: Any) -> List[Dict[str, Any]]:
    """Build the UserQuestion payload from either CLI or fluid tool input.

    The CLI's AskUserQuestion already carries questions: [...]. Fluid
    QuestionRequest carries a single question dict or {questions: [...]} in
    schema. Preserve that structure, including choices; question is only the
    terminal's plain-text rendering, not the UI question text.
    """
    if not isinstance(tool_input, dict):
        return []
    questions = tool_input.get("questions")
    if isinstance(questions, list) and questions:
        return [q for q in questions if isinstance(q, dict)]
    schema = tool_input.get("schema")
    if isinstance(schema, dict) and isinstance(schema.get("questions"), list):
        return [q for q in schema["questions"] if isinstance(q, dict)]
    if isinstance(schema, dict) and str(schema.get("question") or "").strip():
        q = dict(schema)
        message = str(tool_input.get("question") or "").strip()
        if not tool_input.get("_nonblocking") and message and message != str(q.get("question") or "").strip():
            # Generic MCP elicitation may supply extra host context. AskUser's
            # message is its terminal rendering; the tray already renders options.
            q["question"] = message
        return [q]
    message = str(tool_input.get("question") or "").strip()
    if message:
        return [{"question": message, "header": "", "options": []}]
    return []


def fluid_answer_from_envelope(answers: Any) -> str:
    """The single string fluid RespondQuestion wants from a modal envelope.

    Option labels (and free text) pass through as-is — fluid AskUser maps
    numeric picks to labels, and a label is already the choice. Multi-select
    joins with a comma. Dismiss / empty becomes "" (AskUser records
    (no answer)).
    """
    if not isinstance(answers, dict) or answers.get(DISMISSED_KEY):
        return ""
    for key, value in answers.items():
        if key in (DISMISSED_KEY, SKIPPED_KEY, FLUID_ANSWER_KEY):
            continue
        if isinstance(value, list):
            return ", ".join(str(item) for item in value if str(item).strip())
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _question_ttl_seconds() -> int:
    return int(getattr(settings, "user_question_timeout_seconds", 3600))


# ---------------------------------------------------------------------------
# Durable "awaiting input" state for the sidebar
#
# The sidebar's Needs permission / Has question label is fed by
# ``session_attention`` frames. Those frames must carry the durable count of
# pending ``pending_interactions`` rows, not a per-turn in-memory counter: a
# posted (nonblocking) question outlives the turn that asked it, and a page
# load or reconnect has no other way to learn it exists.
# ---------------------------------------------------------------------------

_PENDING_ROW_SQL = (
    "pi.state='pending' AND (pi.expires_at IS NULL OR pi.expires_at >= NOW())"
)


def _fold_attention(counts: Dict[str, int]) -> tuple:
    """(count, kind) from per-kind counts; a permission outranks a question."""
    total = sum(counts.values())
    kind = "permission" if counts.get("permission") else QUESTION_KIND
    return total, kind


def get_session_attention(session_id: str) -> Optional[tuple]:
    """``(owner_user_id, count, kind)`` for one session, or None if it is gone.

    ``owner_user_id`` is the id user-level frames for this session are
    addressed to: ``"admin"`` for the owner (which sync_engine also fans to
    the owner's paired devices) or the API user's id.
    """
    from app.db.base import get_db

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT api_user_id FROM sessions WHERE id=%s", (session_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        cursor.execute(
            "SELECT pi.kind AS kind, COUNT(*) AS n FROM pending_interactions pi "
            f"WHERE pi.session_id=%s AND {_PENDING_ROW_SQL} GROUP BY pi.kind",
            (session_id,),
        )
        counts = {r["kind"]: int(r["n"]) for r in cursor.fetchall() or []}
    count, kind = _fold_attention(counts)
    return (row["api_user_id"] or "admin", count, kind)


def get_user_attention_snapshot(user_id: str) -> List[Dict[str, Any]]:
    """Every session of this user with pending prompts: session_id, count, kind.

    Ownership matches ``streaming_status.get_user_streaming_sessions``: the
    owner (``"admin"`` or a paired device) sees sessions whose ``api_user_id``
    is NULL, an API key sees its own. Fails open (empty list) on a DB error.
    """
    from app.db.base import get_db

    owner = user_id == "admin" or user_id.startswith("paired:")
    sql = (
        "SELECT pi.session_id AS session_id, pi.kind AS kind, COUNT(*) AS n "
        "FROM pending_interactions pi JOIN sessions s ON s.id = pi.session_id "
        f"WHERE {_PENDING_ROW_SQL} AND "
        + ("s.api_user_id IS NULL" if owner else "s.api_user_id = %s")
        + " GROUP BY pi.session_id, pi.kind"
    )
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, () if owner else (user_id,))
            rows = cursor.fetchall() or []
    except Exception:
        logger.warning("Attention snapshot failed for %s", user_id, exc_info=True)
        return []
    per_session: Dict[str, Dict[str, int]] = {}
    for r in rows:
        per_session.setdefault(r["session_id"], {})[r["kind"]] = int(r["n"])
    snapshot = []
    for sid, counts in per_session.items():
        count, kind = _fold_attention(counts)
        snapshot.append({"session_id": sid, "count": count, "kind": kind})
    return snapshot


async def publish_session_attention(session_id: str) -> None:
    """Fan a session's durable pending-prompt count to its owner. Never raises."""
    try:
        info = await asyncio.to_thread(get_session_attention, session_id)
        if info is None:
            return
        user_id, count, kind = info
        from app.core.sync_engine import sync_engine

        await sync_engine.broadcast_session_attention(user_id, session_id, count, kind=kind)
    except Exception:  # noqa: BLE001 — the indicator must never break a prompt
        logger.debug("session_attention publish failed for %s", session_id, exc_info=True)


class UserQuestionHandler:
    """Handles user questions from Claude with a queue system."""

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
            tool_use_id: The tool use ID from the adapter
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
        # Away mode: the owner is not watching, so answer with the recommended
        # options at once instead of posting a card nobody will see.
        try:
            from app.core.away_mode import answers_questions_async, auto_answers, record_answers
            if await answers_questions_async(session_id):
                answers = auto_answers(questions)
                if answers:
                    await record_answers(session_id, questions, answers)
                    return answers
        except Exception:  # noqa: BLE001 — fall back to asking as usual
            logger.warning("Away-mode answer failed for %s", session_id, exc_info=True)
        return await self._request_answers_v2(
            request_id=request_id,
            tool_use_id=tool_use_id,
            session_id=session_id,
            questions=questions,
            broadcast_func=broadcast_func,
            fanout_func=fanout_func,
        )

    # ------------------------------------------------------------------
    # pending_interactions-backed question flow
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
            "nonblocking": bool(payload.get("nonblocking")),
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
        *,
        nonblocking: bool = False,
        ttl_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Persist and publish a question, optionally waiting for its answer.

        ``ttl_seconds`` shortens a blocking question's lifetime below the
        configured question timeout (never lengthens it).
        """
        from app.core import interaction_wait
        from app.db import interactions

        payload = {
            "request_id": request_id,
            "tool_use_id": tool_use_id,
            "questions": questions,
        }
        if nonblocking:
            payload["nonblocking"] = True
        ttl = None if nonblocking else _question_ttl_seconds()
        if ttl is not None and ttl_seconds:
            ttl = max(1, min(ttl, int(ttl_seconds)))
        create_kwargs = {}
        if nonblocking:
            # Engine replay and reconnect must recover one card, not post a copy.
            create_kwargs["interaction_id"] = str(uuid.uuid5(
                uuid.NAMESPACE_URL, f"question:{session_id}:{request_id}"
            ))
        row = await asyncio.to_thread(
            interactions.create_interaction, session_id, QUESTION_KIND, payload, ttl,
            **create_kwargs,
        )
        interaction_id = row["id"]
        if nonblocking and row["state"] != "pending":
            return {"status": row["state"], "request_id": request_id}

        question_payload = {
            "type": "user_question",
            "session_id": session_id,
            "request_id": request_id,
            "tool_use_id": tool_use_id,
            "questions": questions,
            "nonblocking": nonblocking,
        }
        try:
            delivered = await broadcast_func(question_payload)
        except Exception:
            if not nonblocking:
                raise
            logger.warning("Question %s persisted; socket broadcast failed", request_id, exc_info=True)
            delivered = False
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

        # Durable lifecycle event: emit AFTER the WS broadcast + fanout,
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

        if nonblocking:
            # Nothing in the turn counts a posted question, so publish the
            # durable count or no other device ever shows "Has question".
            await publish_session_attention(session_id)
            return {"status": "posted", "request_id": request_id}

        result_row = await interaction_wait.await_interaction(
            interaction_id, session_id, float(ttl)
        )

        state = (result_row or {}).get("state")
        if result_row is None or state in ("expired", "pending"):
            # The tool is giving up, so the ROW has to stop being answerable and
            # every device has to be told. Without this a still-``pending`` row
            # left a live modal on the user's phone for a question the session
            # was no longer waiting on: they answered it, the answer landed on a
            # dead round-trip, and nothing said so. Cancel first, then fan the
            # timeout out — a device that answers between the two loses the
            # guarded UPDATE and re-renders from the notification.
            await self._retire_unanswered(session_id, interaction_id, request_id)
            try:
                await broadcast_func({
                    "type": "user_question_timeout",
                    "session_id": session_id,
                    "request_id": request_id,
                })
            except Exception:
                pass
            await self._notify_question_expired(session_id, request_id, questions)
            logger.warning(f"User question request timed out: {request_id}")
            return {"error": "Question timed out - no response from user"}
        if state == "cancelled":
            return {"error": "No answers received"}

        answers = result_row.get("answer")
        if answers:
            return resolve_answer_envelope(answers)
        return {"error": "No answers received"}

    @staticmethod
    async def _retire_unanswered(
        session_id: str, interaction_id: str, request_id: Optional[str]
    ) -> None:
        """Cancel a question row the tool has stopped waiting on. Never raises.

        Emits the same ``interaction.expired`` the sweeper would, so the durable
        lifecycle log tells one story whether the TTL was reached here or by the
        cross-install sweep, and ticks local waiters so any peer blocked on the
        same row unblocks immediately instead of polling it out.
        """
        from app.core import interaction_wait
        from app.db import interactions

        try:
            cancelled = await asyncio.to_thread(
                interactions.cancel_interaction, interaction_id
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "Could not cancel timed-out question row %s", interaction_id,
                exc_info=True,
            )
            return
        if not cancelled:
            return  # answered or already retired in the meantime
        try:
            interaction_wait.notify_changed(session_id)
        except Exception:  # noqa: BLE001
            logger.debug("notify_changed failed after question timeout", exc_info=True)
        try:
            from app.core import interaction_events

            await interaction_events.emit_interaction_expired(
                session_id, interaction_id, request_id, "timeout",
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "interaction.expired emit failed after question timeout",
                exc_info=True,
            )
        await publish_session_attention(session_id)

    @staticmethod
    async def _notify_question_expired(
        session_id: str, request_id: str, questions: List[Dict[str, Any]]
    ) -> None:
        """Tell the user's devices the question they are staring at is dead.

        The modal is dismissed by the ``user_question_timeout`` frame on the
        session channel, which only reaches devices currently watching THIS
        session. The notification is user-scoped and pushes, so the person who
        walked away learns the session stopped waiting rather than coming back
        to a question that silently stopped mattering. Never raises.
        """
        try:
            from app.services.notification_service import emit_notification

            first = ""
            if questions:
                first = str(questions[0].get("question") or "").strip()
            detail = f" ({first[:120]})" if first else ""
            await emit_notification(
                "admin",
                severity="warning",
                title="Question expired",
                message=(
                    "A session stopped waiting for your answer"
                    f"{detail}. It carried on without one."
                ),
                category="question_timeout",
                surface="notification",
                session_id=session_id,
                persist=True,
                push=True,
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "question-timeout notification failed for %s", request_id,
                exc_info=True,
            )

    async def post_question(self, **kwargs: Any) -> Dict[str, Any]:
        """Publish a durable AskUser prompt without holding the provider callback."""
        return await self._request_answers_v2(**kwargs, nonblocking=True)

    async def _retire_native_question(self, session_id: str) -> None:
        try:
            from app.core.session_pool import get_session_pool
            state = get_session_pool().peek(session_id)
            adapter = getattr(state, "adapter", None) if state is not None else None
            reconcile = getattr(adapter, "reconcile_user_questions", None)
            if reconcile is not None:
                await reconcile()
        except Exception:
            # The durable host answer is authoritative; reconnect retries this.
            logger.warning("Native question retirement failed for %s", session_id, exc_info=True)

    async def _answer_nonblocking(
        self, row: Dict[str, Any], envelope: Dict[str, Any],
        answered_by_device: Optional[str],
    ) -> bool:
        from app.core import interaction_events, interaction_wait, turn_claim, turn_manager
        from app.core import turn_scheduler
        from app.core.session_runtime import _broadcast_queue_after_start, try_steer_pending
        from app.db import interactions

        session_id = row["session_id"]
        request_id = row["payload"]["request_id"]
        if envelope.get(DISMISSED_KEY):
            cancelled = await asyncio.to_thread(interactions.cancel_interaction, row["id"])
            if cancelled:
                await self._retire_native_question(session_id)
                interaction_wait.notify_changed(session_id)
                await interaction_events.emit_interaction_expired(
                    session_id, row["id"], request_id, reason="cancelled",
                )
                await publish_session_attention(session_id)
            return cancelled

        # Only the questions this request actually posted can become user input.
        keys = [str(q.get("question") or "") for q in row["payload"].get("questions", [])]
        resolved = resolve_answer_envelope(envelope)
        answers = {key: resolved[key] for key in keys if key in resolved}
        if not answers:
            return False
        prompt = "Answer to your question:\n" + "\n".join(
            f"{key}\n{', '.join(map(str, value)) if isinstance(value, list) else value}"
            for key, value in answers.items()
        )
        # Inherit the latest runnable context, not settings from when a days-old
        # question was posted. Keep only transport attribution from its payload.
        context = await asyncio.to_thread(turn_scheduler._latest_context_turn, session_id) or {}
        context = {**context, "payload": turn_scheduler._context_payload(context)}
        accepted = await asyncio.to_thread(
            interactions.answer_question_and_enqueue, row, envelope,
            answered_by_device, prompt, context,
        )
        if not accepted:
            return False
        interaction_wait.notify_changed(session_id)
        # Match an ordinary human send: release Stop and supersede stale wakes.
        try:
            from app.core import wakeup_service
            from app.core.task_registry import task_registry
            await asyncio.to_thread(
                wakeup_service.cancel_session_wakeups, session_id,
                exclude_source=wakeup_service.HELD_PEER_SOURCE,
            )
            task_registry.clear_user_stop(session_id)
        except Exception:
            logger.warning("Question answer wake cleanup failed for %s", session_id, exc_info=True)
        # Steer first, then ALWAYS kick. If the turn ends during steering, the
        # durable queued row is still there for the drain/claim loop.
        await self._retire_native_question(session_id)
        await try_steer_pending(session_id)
        turn_manager._kick(session_id)
        # Until the model reads it, the answer is a queued user message: show
        # it as one on this worker's devices, like a send into a busy chat.
        # Delivery records it in the transcript and clears the bubble.
        await _broadcast_queue_after_start(session_id)
        try:
            await asyncio.to_thread(turn_claim.notify_turns_changed, session_id)
        except Exception:
            logger.warning("Question answer claim notification failed for %s", session_id, exc_info=True)
        await interaction_events.emit_interaction_answered(
            session_id, row["id"], request_id, envelope,
        )
        await publish_session_attention(session_id)
        return True

    async def _respond_v2(
        self,
        request_id: str,
        session_id: str,
        answers: Dict[str, Any],
        answered_by_device: Optional[str],
        dismissed: bool = False,
        skipped: Optional[List[str]] = None,
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
        envelope = build_answer_envelope(answers, dismissed=dismissed, skipped=skipped)
        if (row.get("payload") or {}).get("nonblocking"):
            return await self._answer_nonblocking(row, envelope, answered_by_device)
        answered = await asyncio.to_thread(
            interactions.answer_interaction, row["id"], envelope, answered_by_device
        )
        if answered:
            interaction_wait.notify_changed(session_id)
            logger.debug(
                f"User question answered: {request_id}"
                + (" (dismissed)" if dismissed else "")
            )
            # Durable lifecycle event: guarded answer won → emit exactly
            # one interaction.answered. AFTER notify_changed.
            from app.core import interaction_events

            await interaction_events.emit_interaction_answered(
                session_id, row["id"], request_id, envelope,
            )
            await publish_session_attention(session_id)
        return answered

    async def respond(
        self,
        request_id: str,
        session_id: str,
        answers: Dict[str, Any],
        answered_by_device: Optional[str] = None,
        dismissed: bool = False,
        skipped: Optional[List[str]] = None,
    ) -> bool:
        """
        Respond to a user question request.

        Args:
            request_id: The question request ID
            session_id: The session ID
            answers: Dict of answers keyed by the full question text
            answered_by_device: Device id of the answering install.
            dismissed: The user closed the dialog (the modal's "X") without
                answering. Resolves the tool call with ``DISMISS_MESSAGE``;
                ``answers`` is ignored.
            skipped: Question keys the user explicitly skipped. Each is answered
                with ``SKIP_ANSWER_TEXT`` so a partially-answered multi-question
                form still submits cleanly.

        Returns:
            True if response was processed, False if request not found
        """
        return await self._respond_v2(
            request_id,
            session_id,
            answers,
            answered_by_device,
            dismissed=dismissed,
            skipped=skipped,
        )

    def get_pending_questions(self, session_id: str) -> List[Dict[str, Any]]:
        """Get all pending questions for a session."""
        return [
            self._question_row_to_dict(row)
            for row in self._v2_pending_questions(session_id)
        ]

    async def cancel_all(self, session_id: str, *, include_nonblocking: bool = True):
        """Cancel pending questions, retaining durable prompts on pool eviction."""
        from app.core import interaction_wait
        from app.db import interactions

        cancelled = 0
        cancelled_rows: List[tuple] = []
        for row in self._v2_pending_questions(session_id):
            if not include_nonblocking and (row.get("payload") or {}).get("nonblocking"):
                continue
            if await asyncio.to_thread(interactions.cancel_interaction, row["id"]):
                cancelled += 1
                cancelled_rows.append(
                    (row["id"], (row.get("payload") or {}).get("request_id"))
                )
        if cancelled:
            interaction_wait.notify_changed(session_id)
            logger.debug(f"Cancelled all pending questions for session {session_id}")
            # Durable lifecycle events: one interaction.expired
            # (reason="cancelled") per row whose guarded cancel won.
            from app.core import interaction_events

            for cancelled_interaction_id, rid in cancelled_rows:
                await interaction_events.emit_interaction_expired(
                    session_id, cancelled_interaction_id, rid, "cancelled",
                )
            await publish_session_attention(session_id)


# Global singleton instance
user_question_handler = UserQuestionHandler()
