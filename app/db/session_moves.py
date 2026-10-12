"""Queued moves to another worker — the ``session_moves`` table (#1819).

One row per session whose move waits for its turn and background work to end
(``app.core.session_handoff``). The row outlives a restart of the owning
worker, which resumes the move on boot. ``held`` lists the owner's messages
sent while the move waits, oldest first; they are delivered after the move,
or on this worker when it is cancelled, fails or expires. Times are unix
epochs (DOUBLE PRECISION), as in ``pending_wakeups``.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

from app.db.base import get_cursor, get_db, parse_jsonb, rows_to_list
from app.db.compat import ph, phs

_COLUMNS = ("session_id", "target_id", "label", "note", "requested_by", "requested_at", "expires_at", "handoff_id")


def _parse(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    row = dict(row)
    held = parse_jsonb(row.get("held"))
    row["held"] = held if isinstance(held, list) else []
    return row


def save(session_id: str, *, target_id: str, label: str, note: str, requested_by: str,
         requested_at: float, expires_at: float, handoff_id: Optional[str] = None) -> None:
    """Insert or replace the queued move of ``session_id``; held messages are kept."""
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"INSERT INTO session_moves ({', '.join(_COLUMNS)}, held) "
            f"VALUES ({phs(len(_COLUMNS))}, '[]') "
            "ON CONFLICT (session_id) DO UPDATE SET target_id=EXCLUDED.target_id, "
            "label=EXCLUDED.label, note=EXCLUDED.note, requested_by=EXCLUDED.requested_by, "
            "requested_at=EXCLUDED.requested_at, expires_at=EXCLUDED.expires_at, handoff_id=EXCLUDED.handoff_id",
            (session_id, target_id, label, note or "", requested_by,
             float(requested_at), float(expires_at), handoff_id or str(uuid.uuid4())),
        )


def get(session_id: str) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(f"SELECT * FROM session_moves WHERE session_id={ph()}", (session_id,))
        row = cursor.fetchone()
    return _parse(dict(row) if row else None)


def list_all() -> List[Dict[str, Any]]:
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute("SELECT * FROM session_moves ORDER BY requested_at")
        rows = rows_to_list(list(cursor.fetchall() or []))
    return [r for r in (_parse(row) for row in rows) if r is not None]


def append_held(session_id: str, message: Dict[str, Any]) -> bool:
    """Add ``message`` to the held list. False when no move is queued.

    Read-modify-write: the caller serialises calls for one session (the
    session_handoff hold lock), so no message is lost between the two.
    """
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(f"SELECT held FROM session_moves WHERE session_id={ph()}", (session_id,))
        row = cursor.fetchone()
        if not row:
            return False
        held = parse_jsonb(row["held"])
        held = held if isinstance(held, list) else []
        held.append(message)
        cursor.execute(f"UPDATE session_moves SET held={ph()}, wakes_superseded=TRUE WHERE session_id={ph()}",
                       (json.dumps(held), session_id))
        return cursor.rowcount > 0


def set_wake_token(session_id: str, token: str) -> None:
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(f"UPDATE session_moves SET wake_token={ph()} WHERE session_id={ph()}",
                       (token, session_id))


def remove_held(session_id: str, held_id: str) -> bool:
    """Drop one held message by its id. False when it is not held."""
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(f"SELECT held FROM session_moves WHERE session_id={ph()}", (session_id,))
        row = cursor.fetchone()
        if not row:
            return False
        held = parse_jsonb(row["held"])
        held = held if isinstance(held, list) else []
        removed = next((entry for entry in held
                        if isinstance(entry, dict) and entry.get("id") == held_id), None)
        if removed is None:
            return False
        kept = [entry for entry in held if not (isinstance(entry, dict) and entry.get("id") == held_id)]
        # A content-free withdrawal receipt commits WITH the removal and
        # outlives the move. Use the session event log rather than a fake turn:
        # this message never ran and must not affect turn/permission selectors.
        from app.db.session_events import append_events

        append_events(session_id, [{
            "id": "held-withdrawal:" + held_id,
            "type": "queued_message.withdrawn",
            "command_id": removed.get("command_id"),
            "payload": {"message_id": held_id},
        }], _conn=conn)
        cursor.execute(f"UPDATE session_moves SET held={ph()} WHERE session_id={ph()}",
                       (json.dumps(kept), session_id))
        return True


def held_withdrawal(
    session_id: str, *, held_id: Optional[str] = None, command_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Find a durable withdrawal by either identity, scoped to its session."""
    if not held_id and not command_id:
        return None
    field, value = ("id", "held-withdrawal:" + held_id) if held_id else ("command_id", command_id)
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"SELECT payload, command_id FROM session_events WHERE session_id={ph()} "
            f"AND event_type='queued_message.withdrawn' AND {field}={ph()} LIMIT 1",
            (session_id, value),
        )
        row = cursor.fetchone()
    if not row:
        return None
    payload = parse_jsonb(row["payload"]) or {}
    return {"id": payload.get("message_id"), "command_id": row["command_id"]}


def take(session_id: str) -> Optional[Dict[str, Any]]:
    """Delete the queued move of ``session_id`` and return it (held messages included)."""
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(f"SELECT * FROM session_moves WHERE session_id={ph()}", (session_id,))
        row = cursor.fetchone()
        if not row:
            return None
        cursor.execute(f"DELETE FROM session_moves WHERE session_id={ph()}", (session_id,))
    return _parse(dict(row))
