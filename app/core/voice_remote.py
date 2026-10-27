"""Voice calls on a conversation another network worker runs (#700).

The call runs on the worker the browser is connected to, with that worker's
voice settings and login. The conversation itself belongs to its owner: here
it is a read-only mirror (``session_events`` and the session row are
owner-ordered, ``app.network.policy``). So a call on a mirror never writes the
spoken exchange, tool records, usage counters or the active flag into it.
What the caller asks for is forwarded to the owner the way a typed message is
(``app.network.control``), and the owner records the request. The agent's
replies, progress and approval prompts reach the call as the owner's events
sync back into the mirror, where ``VoiceProgress`` already reads them; "is it
done?" reads the owner's replicated runtime summary (``work_state``), since a
mirror never gets the owner's turns.

One write does stay local: attach stores this call's binding in the mirror's
``voice_config`` column. That column is not replicated (it is not a field of
the ``sessions`` collection), so it never leaves this worker; it is the
call's own state, kept where the call runs.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# The forwarded turn's source, as a typed message from this worker's UI.
SOURCE = "user"


def owner_of(row: Optional[Dict[str, Any]]) -> Optional[str]:
    """Device id of the network worker that runs ``row``, or None when it is ours."""
    if not row:
        return None
    try:
        from app.network import control
        return control.owner_of(dict(row))
    except Exception:  # noqa: BLE001 - no network layer means the session is ours
        logger.debug("Voice: owner lookup failed for %s", (row or {}).get("id"), exc_info=True)
        return None


def owner_of_session(session_id: str) -> Optional[str]:
    """:func:`owner_of` for a session id (synchronous; use ``run_sync``)."""
    from app.db import database
    return owner_of(database.get_session(session_id))


def request_prompt(spoken: Optional[str], transcript: str) -> str:
    """The words the owner records as the caller's message.

    A streamed call hands off a context block (the recent exchange) rather
    than the request itself; the caller's own words are what belongs in the
    owner's transcript. A sideband call's ``submit_to_chat`` request is
    already the request.
    """
    spoken = (spoken or "").strip()
    return spoken or transcript.strip()


def command_id(call_key: str, request_key: str) -> str:
    """The forwarded request's ``command_id``: one per request of one call.

    The owner answers a repeat of the same id with the turn it already has
    instead of queueing the request again (``control._turns``), so a resend
    after a lost answer never runs the work twice.
    """
    return f"voice:{call_key}:{request_key}"


async def submit(owner: str, row: Dict[str, Any], transcript: str, *, instructions: str,
                 spoken: Optional[str] = None, command: Optional[str] = None) -> Optional[str]:
    """Forward one voice request to the owner. Returns the owner's turn id.

    Nothing is written here. The owner records the message (the payload does
    not claim it is already recorded), runs it in the session's own model and
    mode, and steers it into a running turn as it does any forwarded send.
    The call's context rides the engine-only voice note, never the stored
    message. ``command`` (:func:`command_id`) makes a repeat safe.
    Raises ``control.MaybeApplied`` (it may have been queued),
    ``control.RunnerUnreachable`` or ``ControlRefused``.
    """
    from app.core import turn_manager
    from app.core.voice_prompts import VOICE_INSTRUCTIONS
    from app.network import control

    prompt = request_prompt(spoken, transcript)
    note = instructions
    if prompt != transcript.strip():
        note = f"{instructions}\n\n{transcript.strip()}"
    payload: Dict[str, Any] = {turn_manager.VOICE_DELEGATION: True, VOICE_INSTRUCTIONS: note}
    result = await control.submit_turn(
        owner, row["id"], prompt=prompt, source=SOURCE,
        profile_id=row.get("profile_id"), project_id=row.get("project_id"),
        payload=payload, command_id=command,
    )
    return (result or {}).get("turn_id")


async def answer_permission(owner: str, session_id: str, request_id: str, decision: str) -> Dict[str, Any]:
    """Forward a spoken yes or no to the owner, as a tap on the card would be.

    Returns the owner's ``permission_handler.respond`` result (``{"error": ...}``
    when it was already settled). An answer is keyed by its ``request_id``, so
    a repeat can never decide a request twice. Raises ``control.MaybeApplied``
    (the answer may have been recorded), ``control.RunnerUnreachable`` or
    ``ControlRefused``.
    """
    from app.network import control, engine

    ctx = engine.context()
    forwarded = await control.answer_permission(
        owner, request_id=request_id, session_id=session_id, decision=decision,
        remember=None, pattern=None, answered_by_device=ctx.device_id if ctx else None)
    result = (forwarded or {}).get("result")
    return result if isinstance(result, dict) else {"error": "No answer from the owning worker."}


def unreachable_note(exc: BaseException) -> str:
    """What the voice tells the caller when the owner could not take a request."""
    return ("The worker that runs this conversation could not take the request, so nothing was "
            f"sent ({exc}). Tell the caller briefly and do not claim the work started.")


def maybe_applied_note(exc: BaseException) -> str:
    """What the voice says when the owner got the request but no answer came back."""
    return ("The request reached the worker that runs this conversation, but no answer came back "
            f"({exc}), so it may already be in the chat. Tell the caller to check the chat before "
            "asking again, and do not claim the work started or finished.")


SENT_NOTE = "The request was sent to the chat on the worker that runs it. Its reply shows in chat."


def work_state(session_id: str, owner: str) -> Optional[str]:
    """The owner's replicated runtime state for ``session_id`` (synchronous).

    One of ``app.network.session_runtime.STATES``, or None when the owner has
    published nothing for it yet. Only a summary the owner itself published
    counts: a stale one from an earlier owner says nothing about this one.
    """
    from app.network.session_runtime import STATES, for_sessions
    summary = for_sessions([session_id]).get(session_id) or {}
    state = summary.get("state")
    return state if summary.get("owner_device_id") == owner and state in STATES else None
