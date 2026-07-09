"""Queued messages database operations.

Stores messages that users queue while a session is streaming.
The backend auto-processes these when the current query completes.
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional, List, Dict, Any, TypedDict

from app.db.base import get_db, row_to_dict


class QueuedMessageRow(TypedDict):
    """Full queued message row."""
    id: str
    session_id: str
    content: str
    profile_id: Optional[str]
    project_id: Optional[str]
    position: int
    created_at: str


def _normalize_row(row: Any) -> QueuedMessageRow:
    """Convert a DB row to a JSON-safe dict.

    Postgres returns ``created_at`` as a ``datetime`` object, but this row gets
    broadcast over the WebSocket (in ``queue_updated`` events) where the JSON
    encoder can't serialize raw datetimes. Coerce it to an ISO string so the
    row matches the declared ``created_at: str`` contract and never breaks a
    send. (A non-serializable datetime here previously poisoned the whole chat
    connection, silently dropping "Send later" messages.)
    """
    d = row_to_dict(row)
    created = d.get("created_at")
    if created is not None and hasattr(created, "isoformat"):
        d["created_at"] = created.isoformat()
    return d


def add_queued_message(
    session_id: str,
    content: str,
    profile_id: Optional[str] = None,
    project_id: Optional[str] = None,
) -> QueuedMessageRow:
    """Add a message to the queue for a session.

    Returns the created row.
    """
    msg_id = f"queued-{uuid.uuid4().hex[:12]}"
    with get_db() as conn:
        cursor = conn.cursor()
        # Auto-increment position within this session
        cursor.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 as next_pos FROM queued_messages WHERE session_id = %s",
            (session_id,)
        )
        position = cursor.fetchone()["next_pos"]
        cursor.execute(
            """INSERT INTO queued_messages (id, session_id, content, profile_id, project_id, position)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (msg_id, session_id, content, profile_id, project_id, position)
        )
        cursor.execute("SELECT * FROM queued_messages WHERE id = %s", (msg_id,))
        return _normalize_row(cursor.fetchone())


def get_queued_messages(session_id: str) -> List[QueuedMessageRow]:
    """Get all queued messages for a session, ordered by position."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM queued_messages WHERE session_id = %s ORDER BY position ASC",
            (session_id,)
        )
        return [_normalize_row(r) for r in cursor.fetchall()]


def count_queued_messages(session_id: str) -> int:
    """Cheap count of undrained queued messages for a session.

    Used by ``/live`` to keep the client's poll-alive signal up while a
    follow-up turn is imminent (queue rows await the drain pump) even though
    nothing is streaming this instant. Sibling reader of
    ``pop_next_queued_message`` — a COUNT(*) rather than a full row fetch.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) AS n FROM queued_messages WHERE session_id = %s",
            (session_id,)
        )
        row = cursor.fetchone()
        if row is None:
            return 0
        # Tolerate dict-row (RealDictCursor / pg_shim) or tuple-row cursors.
        try:
            return int(row["n"])
        except (TypeError, KeyError, IndexError):
            return int(row[0])


def pop_next_queued_message(session_id: str) -> Optional[QueuedMessageRow]:
    """Atomically get and delete the first queued message for a session.

    Returns the message dict, or None if queue is empty.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM queued_messages WHERE session_id = %s ORDER BY position ASC LIMIT 1",
            (session_id,)
        )
        row = cursor.fetchone()
        if not row:
            return None
        msg = _normalize_row(row)
        cursor.execute("DELETE FROM queued_messages WHERE id = %s", (msg["id"],))
        return msg


def requeue_message_at_front(
    session_id: str,
    content: str,
    profile_id: Optional[str] = None,
    project_id: Optional[str] = None,
) -> QueuedMessageRow:
    """Re-insert a message at the front of the queue (position 0).

    Used for error recovery when a popped message fails to start a query.
    Existing messages have their positions shifted up by 1.
    """
    msg_id = f"queued-{uuid.uuid4().hex[:12]}"
    with get_db() as conn:
        cursor = conn.cursor()
        # Shift all existing positions up by 1 to make room at front
        cursor.execute(
            "UPDATE queued_messages SET position = position + 1 WHERE session_id = %s",
            (session_id,)
        )
        cursor.execute(
            """INSERT INTO queued_messages (id, session_id, content, profile_id, project_id, position)
               VALUES (%s, %s, %s, %s, %s, 0)""",
            (msg_id, session_id, content, profile_id, project_id)
        )
        cursor.execute("SELECT * FROM queued_messages WHERE id = %s", (msg_id,))
        return _normalize_row(cursor.fetchone())


def remove_queued_message(message_id: str) -> bool:
    """Remove a single queued message by ID. Returns True if deleted."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM queued_messages WHERE id = %s", (message_id,))
        return cursor.rowcount > 0


def clear_queued_messages(session_id: str) -> int:
    """Delete all queued messages for a session. Returns count deleted."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM queued_messages WHERE session_id = %s", (session_id,))
        return cursor.rowcount
