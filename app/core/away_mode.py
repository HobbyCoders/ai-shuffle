"""Away mode: the owner stepped away, so the agent decides and reports back.

A per-session flag (``sessions.away_mode``), changed mid-session the same way
as the permission mode. While it is on:

* A question tool (fluid ``AskUser``, Claude's ``AskUserQuestion``) does not
  park on the owner. It returns at once with each question's recommended
  option (the label marked "(Recommended)", else the first option), and every
  such decision is recorded on the session as an ``away_decision`` system row.
* An approval prompt (tool permission, computer-use access, a private-access
  confirmation) is NOT granted. It is deferred with a clear result instead of
  waiting on a card nobody is watching. Away mode never widens permissions.
* Each turn carries a short reminder telling the model the owner is away.

Chats the owner's assistant starts with StartSession get Away-style handling
without the flag (:func:`assistant_work`): their questions are answered the
same way, and their approval prompts are raised in the assistant's
conversation (``provider_agent_approvals``) where the owner answers the real
card. Anything that cannot be routed there, such as a private-access request,
is deferred exactly as Away mode defers it. Nothing more is ever granted.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

AWAY_NOTE = (
    "Owner is away — recommended default applied; list this decision in your "
    "final 'Decisions made for you' table."
)

CONTEXT_NOTE = (
    "<system-reminder>\n"
    "The owner is away. Do not ask questions; take recommended defaults; end "
    "with a 'Decisions made for you' table (decision, what you chose, how to "
    "reverse).\n"
    "</system-reminder>"
)

DEFERRED_MESSAGE = (
    "Owner away; deferred. This action needs the owner's approval and Away mode "
    "never grants it. Do not retry it or reach the same effect another way: "
    "continue with work that does not need it, and list it as 'deferred — "
    "needs owner approval' in your final 'Decisions made for you' table."
)

ASSISTANT_WORK_NOTE = (
    "<system-reminder>\n"
    "The owner's assistant started this chat. Do not ask questions: any question "
    "is answered with its recommended option, so state the assumptions you make. "
    "Approvals you need are shown to the owner in the assistant's conversation. "
    "Your final reply goes back to the assistant automatically; do not also send "
    "it with MessageSession.\n"
    "</system-reminder>"
)

REPORTED_AWAY_NOTE = (
    "<system-reminder>\n"
    "Your final reply goes back to the owner's assistant automatically; list any "
    "deferred approvals in it.\n"
    "</system-reminder>"
)

OPEN_ENDED_ANSWER = (
    "No answer (the owner is away). Use your best judgment and state the "
    "assumption you made."
)

DECISION_SUBTYPE = "away_decision"

_RECOMMENDED = re.compile(r"\(\s*recommended\s*\)", re.IGNORECASE)


def is_away(session_id: Optional[str]) -> bool:
    """Whether Away mode governs ``session_id``. Never raises (False on error).

    A delegated worker session inherits its parent's flag: its approval
    prompts are raised on the parent's UI, which nobody is watching either.
    """
    if not session_id:
        return False
    try:
        from app.db.base import get_db

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT s.away_mode AS away, s.is_worker AS is_worker, "
                "p.away_mode AS parent_away FROM sessions s "
                "LEFT JOIN sessions p ON p.id = s.parent_session_id "
                "WHERE s.id = %s",
                (session_id,),
            )
            row = cursor.fetchone()
    except Exception:  # noqa: BLE001 — absent flag means present owner
        logger.debug("away_mode read failed for %s", session_id, exc_info=True)
        return False
    if not row:
        return False
    return bool(row["away"]) or (bool(row["is_worker"]) and bool(row["parent_away"]))


async def is_away_async(session_id: Optional[str]) -> bool:
    return await asyncio.to_thread(is_away, session_id)


def assistant_work(session_id: Optional[str]) -> bool:
    """Whether ``session_id`` is a local chat the owner's assistant started.

    Never raises (False on error): an unknown chat keeps ordinary behaviour.
    """
    if not session_id:
        return False
    try:
        from app.core.session_orchestration import started_by_home

        return started_by_home(session_id) is not None
    except Exception:  # noqa: BLE001 — absent marker means an ordinary chat
        logger.debug("assistant_work check failed for %s", session_id, exc_info=True)
        return False


def answers_questions(session_id: Optional[str]) -> bool:
    """Whether questions in ``session_id`` get recommended defaults at once."""
    return is_away(session_id) or assistant_work(session_id)


async def answers_questions_async(session_id: Optional[str]) -> bool:
    if await is_away_async(session_id):
        return True
    return await asyncio.to_thread(assistant_work, session_id)


def turn_note(session_id: Optional[str]) -> Optional[str]:
    """The per-turn reminder: Away mode, assistant-started work, the owner's
    pending yes/no questions in the assistant's home, or None."""
    notes = [note for note in (_mode_note(session_id), _confirm_note(session_id)) if note]
    return "\n\n".join(notes) if notes else None


def _confirm_note(session_id: Optional[str]) -> Optional[str]:
    try:
        from app.core import assistant_confirm
        from app.core.assistant_service import is_primary_session

        return assistant_confirm.context_note(session_id) if is_primary_session(session_id) else None
    except Exception:  # noqa: BLE001 — the reminder is best-effort
        logger.debug("confirmation note failed for %s", session_id, exc_info=True)
        return None


def _mode_note(session_id: Optional[str]) -> Optional[str]:
    if is_away(session_id):
        try:
            from app.core.session_orchestration import _start_turn

            start = _start_turn(session_id) if session_id else None
        except Exception:  # noqa: BLE001 — the reminder is best-effort
            start = None
        if ((start or {}).get("payload") or {}).get("orchestration_report_device"):
            return CONTEXT_NOTE + "\n\n" + REPORTED_AWAY_NOTE
        return CONTEXT_NOTE
    return ASSISTANT_WORK_NOTE if assistant_work(session_id) else None


def _labels(question: Dict[str, Any]) -> List[str]:
    labels = []
    for option in question.get("options") or []:
        if isinstance(option, dict):
            label = str(option.get("label") or "").strip()
        else:
            label = str(option or "").strip()
        if label:
            labels.append(label)
    return labels


def recommended_answer(question: Dict[str, Any]) -> str:
    """The option an away owner gets: "(Recommended)" ones, else the first."""
    labels = _labels(question)
    if not labels:
        return OPEN_ENDED_ANSWER
    picks = [label for label in labels if _RECOMMENDED.search(label)]
    if not picks:
        return labels[0]
    return ", ".join(picks) if question.get("multiSelect") else picks[0]


def auto_answers(questions: List[Dict[str, Any]]) -> Dict[str, str]:
    """``{question text: chosen answer}`` in the order the questions came."""
    answers: Dict[str, str] = {}
    for question in questions:
        if not isinstance(question, dict):
            continue
        text = str(question.get("question") or "").strip()
        if text:
            answers[text] = recommended_answer(question)
    return answers


def format_answers(answers: Dict[str, str]) -> str:
    """The tool result the model reads in place of the owner's answer."""
    lines = [AWAY_NOTE]
    lines.extend(f'- "{question}" → {answer}' for question, answer in answers.items())
    return "\n".join(lines)


async def record_decision(session_id: str, data: Dict[str, Any], content: str) -> None:
    """Append one ``away_decision`` system row to the session. Never raises."""
    try:
        from app.core.turn_recorder import record_drained_event

        await record_drained_event(session_id, {
            "type": "system",
            "subtype": DECISION_SUBTYPE,
            "content": content,
            "data": {**data, "content": content},
        })
    except Exception:  # noqa: BLE001 — the row is a report, never a gate
        logger.warning("Could not record away decision for %s", session_id, exc_info=True)


async def record_answers(
    session_id: str, questions: List[Dict[str, Any]], answers: Dict[str, str]
) -> None:
    decisions = []
    for question in questions:
        if not isinstance(question, dict):
            continue
        text = str(question.get("question") or "").strip()
        if text in answers:
            decisions.append({
                "question": text,
                "header": str(question.get("header") or ""),
                "choice": answers[text],
            })
    if not decisions:
        return
    content = "; ".join(f"{d['header'] or d['question']} → {d['choice']}" for d in decisions)
    await record_decision(session_id, {"kind": "answered", "decisions": decisions}, content)


async def answer_if_away(
    session_id: Optional[str], questions: List[Dict[str, Any]]
) -> Optional[str]:
    """The immediate tool result for ``questions`` when away, else ``None``."""
    if not questions or not await answers_questions_async(session_id):
        return None
    answers = auto_answers(questions)
    if not answers:
        return None
    await record_answers(str(session_id), questions, answers)
    return format_answers(answers)


async def defer_if_away(session_id: Optional[str], action: str) -> Optional[str]:
    """``DEFERRED_MESSAGE`` (recorded on the session) when away, else ``None``.

    Callers use this only where a PERSON would otherwise be asked; it never
    allows anything. A chat the owner's assistant started is deferred the same
    way when a prompt reaches it here instead of the assistant's conversation.
    """
    if not await answers_questions_async(session_id):
        return None
    await record_decision(
        str(session_id),
        {"kind": "deferred", "action": action},
        f"Deferred until you are back: {action}",
    )
    return DEFERRED_MESSAGE


def make_question_auto_answer(
    resolve_session_id: Callable[[], Optional[str]],
) -> Callable[[List[Dict[str, Any]]], Awaitable[Optional[str]]]:
    """Fluid's ``ctx.extra["auto_answer_questions"]`` hook for one session."""

    async def _auto_answer(questions: List[Dict[str, Any]]) -> Optional[str]:
        try:
            # The resolver may fall back to a DB lookup: keep it off the loop.
            session_id = await asyncio.to_thread(resolve_session_id)
            return await answer_if_away(session_id, questions)
        except Exception:  # noqa: BLE001 — fall back to posting the question
            logger.warning("Away auto-answer failed", exc_info=True)
            return None

    return _auto_answer


async def release_pending_questions(session_id: str) -> int:
    """Answer every question already waiting on the owner with its default.

    Used when Away mode is switched on after a question was posted, so a
    session parked on the owner resumes. Returns how many requests answered.
    """
    from app.core.user_question_handler import user_question_handler

    pending = await asyncio.to_thread(user_question_handler.get_pending_questions, session_id)
    released = 0
    for item in pending:
        questions = [q for q in (item.get("questions") or []) if isinstance(q, dict)]
        answers = auto_answers(questions)
        if not answers or not item.get("request_id"):
            continue
        if await user_question_handler.respond(item["request_id"], session_id, answers):
            released += 1
            await record_answers(session_id, questions, answers)
    return released
