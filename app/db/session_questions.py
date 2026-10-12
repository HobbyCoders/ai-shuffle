"""Durable trailing-prose question projection for session-list consumers.

The string predicate is a literal port of work-log.ts's asksQuestion; shared
fixtures live in tests/fixtures/prose_questions.json. Read the persisted tail,
not a process-local stream buffer, so recorder flushes and restarts agree.
"""
from __future__ import annotations

import re


# JavaScript \s/trim include BOM but not Python's extra control separators.
_JS_SPACE = "\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_SPACE = f"[{_JS_SPACE}]"
_NONSPACE = f"[^{_JS_SPACE}]"
_TRIM = re.compile(f"^{_SPACE}+|{_SPACE}+$")


def asks_question(content: str) -> bool:
    """Question punctuation outside inline code and URL/link targets."""
    prose = re.sub(r"`[^`]*`", "", content)
    prose = re.sub(r"\]\([^\)" + _JS_SPACE + r"]*\)", "]", prose)
    prose = re.sub(r"<https?://[^>" + _JS_SPACE + r"]*>", "", prose)
    prose = re.sub(r"(?<![a-zA-Z0-9_])https?://" + _NONSPACE + r"*[^" + _JS_SPACE + r".,;:!?)\]'\"]", "", prose)
    return "?" in prose


# These events close the reducer's open text bubble. Other cards/usage do not
# become prose, and must not hide an earlier question.
_CLOSE_TEXT = {
    "turn.started", "turn.completed", "turn.aborted", "turn.parked",
    "message.assistant", "assistant.thinking_delta",
    "tool.use", "tool.input_delta", "subagent.started", "system.note",
    "system.compaction_boundary", "team.member_started",
}


def _tail(cursor, session_id):
    """Newest surviving events first, bounded pages; skip rewound branches."""
    before = None
    while True:
        sql = "SELECT seq, event_type, payload FROM session_events WHERE session_id = %s"
        params = [session_id]
        if before is not None:
            sql += " AND seq < %s"
            params.append(before)
        cursor.execute(sql + " ORDER BY seq DESC LIMIT 128", tuple(params))
        rows = cursor.fetchall()
        if not rows:
            return
        from app.db.session_events import _parse_payload
        for row in rows:
            seq = int(row["seq"])
            if before is not None and seq >= before:
                continue
            before = seq
            payload = _parse_payload(row["payload"])
            if row["event_type"] == "transcript.rewound":
                cut = payload.get("from_seq")
                if isinstance(cut, int) and not isinstance(cut, bool):
                    before = min(before, cut)
                continue
            yield seq, row["event_type"], payload


def _attempt_start(cursor, session_id, retry_seq):
    """The reducer's attemptStartSeq: user, turn start, result or retry note."""
    for seq, kind, payload in _tail(cursor, session_id):
        if seq >= retry_seq:
            continue
        if kind in ("turn.started", "message.user", "tool.result") or (
            kind == "system.note" and payload.get("subtype") == "provider_retry"
        ):
            return seq
    return 0


def trailing_question_seq(cursor, session_id):
    """Newest nonempty rendered prose, respecting bubble/attempt origins.

    Deliveries and steers do not close the reducer's openTextId. A steer can
    answer a bubble even when subsequent deltas advance that earlier bubble's
    event position. A dropped attempt hides only bubbles CREATED after the
    attempt boundary, not a bubble spanning a mid-stream delivery/steer.
    """
    from app.db.sessions import is_user_message

    closed = False
    pieces = []
    position = origin = None
    user_seq = None
    dropped_since = None

    def finish():
        text = _TRIM.sub("", "".join(reversed(pieces)))
        if not text or (dropped_since is not None and origin > dropped_since):
            return False, None
        answered_mid_stream = user_seq is not None and origin < user_seq < position
        return True, position if closed and not answered_mid_stream and asks_question(text) else None

    for seq, kind, payload in _tail(cursor, session_id):
        if kind == "system.note" and payload.get("subtype") in ("background_tasks_changed", "model_info"):
            continue
        if pieces and kind in _CLOSE_TEXT:
            found, question = finish()
            if found:
                return question
            pieces = []
            position = origin = None
        if kind == "message.user" and is_user_message("user", payload):
            user_seq = seq if user_seq is None else user_seq
        if kind == "system.note" and payload.get("subtype") == "provider_retry":
            if (payload.get("data") or {}).get("cause") == "connection_dropped":
                dropped_since = _attempt_start(cursor, session_id, seq)
        hidden = dropped_since is not None and seq > dropped_since
        if kind in ("tool.use", "tool.input_delta") and payload.get("name") in ("AskUser", "AskUserQuestion"):
            if not hidden:
                return None
        if kind == "message.assistant" and payload.get("kind") in (None, "text"):
            text = _TRIM.sub("", payload.get("content") or "")
            if text and not hidden:
                return seq if asks_question(text) else None
        if kind == "assistant.text_delta":
            # A live tail cannot ask yet. Read a stream only once it closes.
            if not closed and _TRIM.sub("", payload.get("text") or ""):
                return None
            if position is None:
                position = seq
            origin = seq
            pieces.append(payload.get("text") or "")
        elif kind in _CLOSE_TEXT:
            closed = True
    return finish()[1]


def record_question(cursor, session_id, *, force_notify=False):
    """Called inside append's counter-serialized transaction; return a wake."""
    seq = trailing_question_seq(cursor, session_id)
    cursor.execute(
        "SELECT id FROM session_events WHERE session_id = %s ORDER BY seq LIMIT 1",
        (session_id,),
    )
    first = cursor.fetchone()
    epoch = str(first["id"]) if first else None
    cursor.execute(
        "SELECT api_user_id, last_assistant_question_seq, last_assistant_question_epoch, "
        "last_user_message_seq, last_user_message_epoch FROM sessions WHERE id = %s",
        (session_id,),
    )
    row = cursor.fetchone()
    if row is None or (not force_notify and (
        row["last_assistant_question_seq"], row["last_assistant_question_epoch"]
    ) == (seq, epoch)):
        return None
    from datetime import datetime, timezone

    # A first, still-streaming delta only establishes a null projection's
    # epoch. It must not turn a backfilled/idle session into recent activity.
    # Actual question changes do refresh the catalog's incremental scan key.
    changed = row["last_assistant_question_seq"] != seq
    cursor.execute(
        "UPDATE sessions SET last_assistant_question_seq = %s, last_assistant_question_epoch = %s, "
        "updated_at = CASE WHEN %s THEN %s ELSE updated_at END WHERE id = %s RETURNING updated_at",
        (seq, epoch, changed, datetime.now(timezone.utc).isoformat(), session_id),
    )
    from app.core.session_payload import serialize_session_updated_at

    updated_at = serialize_session_updated_at(cursor.fetchone()["updated_at"])
    owner = row["api_user_id"]
    updates = {
        "updated_at": updated_at,
        "last_assistant_question_seq": seq,
        "last_assistant_question_epoch": epoch,
        "last_user_message_seq": row["last_user_message_seq"],
        "last_user_message_epoch": row["last_user_message_epoch"],
    }

    def notify():
        from app.core.global_events import global_events
        global_events.lifecycle(owner, {
            "type": "user_session_updated", "session_id": session_id, "updates": updates,
        }, user_type="admin" if owner is None else "api_user")

    return notify
