"""app/core/turn_outcome.py — the shared reader that derives a settled turn's
outcome and text from the V2 ``session_events`` log.

Every non-WebSocket intake path enrols its prompt through
:mod:`app.core.turn_manager` (``submit`` → ``wait_session_idle``) and then has to
answer the same two questions from the durable log rather than by sniffing
frames off a live stream: *did the turn succeed, and what did the assistant
say?* Automation asked them first; the REST ``/api/v1/conversation`` endpoint and
the connector inbound path now ask them too, so the three helpers live here
instead of on ``AutomationManager``.

The log is the record: :func:`final_turn_outcome` reads the session's LAST
terminal turn event and :func:`extract_final_assistant_text` concatenates the
text deltas of the last completed turn. Neither raises — an unreadable log
yields an empty outcome, which callers treat as "nothing derivable".
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def load_session_events(session_id: str) -> List[Dict[str, Any]]:
    """Read the whole V2 ``session_events`` log for a session in seq order.
    Never raises — a reader error yields ``[]`` (the callers treat an empty
    log as "no derivable outcome")."""
    from app.db import session_events as session_events_db

    events: List[Dict[str, Any]] = []
    try:
        from_seq = 0
        while True:
            batch = session_events_db.get_events(
                session_id, from_seq=from_seq, limit=500
            )
            if not batch:
                break
            events.extend(batch)
            if len(batch) < 500:
                break
            from_seq = batch[-1].get("seq") or from_seq
    except Exception:  # noqa: BLE001
        return []
    return events


def final_turn_outcome(session_id: str) -> Dict[str, Any]:
    """Derive a settled turn's outcome from the session's LAST terminal turn
    event (``turn.completed`` / ``turn.aborted`` / ``turn.parked``).

    Replaces the headless handler's WS-frame sniffing — the durable log is
    now the record. Returns ``{"usage", "error", "interrupted"}``:

    * ``turn.completed`` → ``usage`` = its usage summary, no error;
    * ``turn.aborted`` with ``reason == 'interrupt'`` → ``interrupted`` True
      (a genuine stop that settled before the timeout);
    * any other ``turn.aborted`` (error / crash / escalation / lease) →
      ``error`` = the aborted ``message`` or a reason-derived string;
    * ``turn.parked`` / no terminal event → treated as a clean, empty run.
    """
    events = load_session_events(session_id)
    terminal = None
    for ev in reversed(events):
        if ev.get("event_type") in ("turn.completed", "turn.aborted", "turn.parked"):
            terminal = ev
            break
    result: Dict[str, Any] = {"usage": {}, "error": None, "interrupted": False}
    if terminal is None:
        return result
    etype = terminal.get("event_type")
    payload = terminal.get("payload") or {}
    if etype == "turn.completed":
        usage = payload.get("usage")
        result["usage"] = usage if isinstance(usage, dict) else {}
    elif etype == "turn.aborted":
        reason = payload.get("reason")
        if reason == "interrupt":
            result["interrupted"] = True
        else:
            result["error"] = (
                payload.get("message")
                or (f"Turn aborted: {reason}" if reason else "Turn aborted")
            )
    return result


def extract_final_assistant_text(session_id: str) -> str:
    """Final assistant text of the turn — feeds output delivery and the
    runs-list preview. Derived from the V2 session event log: the turn
    recorder streams assistant text as ``assistant.text_delta`` events and
    the v2 path no longer dual-writes ``session_messages``, so this
    concatenates the LAST completed turn's text-delta payloads in seq order
    exactly as replay would render them. Never raises (returns "")."""
    events = load_session_events(session_id)

    # Target the LAST completed turn; if none completed (an aborted or
    # interrupted run), fall back to the turn of the last assistant text
    # event so a partial turn still surfaces its text.
    target_turn_id = None
    for ev in reversed(events):
        if ev.get("event_type") == "turn.completed":
            target_turn_id = ev.get("turn_id")
            break
    if target_turn_id is None:
        for ev in reversed(events):
            if ev.get("event_type") in ("assistant.text_delta", "message.assistant"):
                target_turn_id = ev.get("turn_id")
                break
    if target_turn_id is None:
        return ""

    parts: List[str] = []
    for ev in events:
        if ev.get("turn_id") != target_turn_id:
            continue
        etype = ev.get("event_type")
        payload = ev.get("payload") or {}
        if etype == "assistant.text_delta":
            text = payload.get("text")
        elif etype == "message.assistant":
            # Consolidated assistant event (history backfill) — text kind only.
            if payload.get("kind") not in (None, "text"):
                continue
            text = payload.get("content")
        else:
            continue
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts).strip()
