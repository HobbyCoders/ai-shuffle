"""Queued messages database operations.

Stores messages that users queue while a session is streaming.
The backend auto-processes these when the current query completes.
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional, List, Dict, Any, TypedDict

from app.db.base import get_db, row_to_dict, rows_to_list, logger


class QueuedMessageRow(TypedDict):
    """Full queued message row."""
    id: str
    session_id: str
    content: str
    profile_id: Optional[str]
    project_id: Optional[str]
    position: int
    created_at: str


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
        return row_to_dict(cursor.fetchone())


def get_queued_messages(session_id: str) -> List[QueuedMessageRow]:
    """Get all queued messages for a session, ordered by position."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM queued_messages WHERE session_id = %s ORDER BY position ASC",
            (session_id,)
        )
        return rows_to_list(cursor.fetchall())


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
        msg = row_to_dict(row)
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
            "UPDATE queued_messages SET position = position + 1 WHERE session_id = ?",
            (session_id,)
        )
        cursor.execute(
            """INSERT INTO queued_messages (id, session_id, content, profile_id, project_id, position)
               VALUES (?, ?, ?, ?, ?, 0)""",
            (msg_id, session_id, content, profile_id, project_id)
        )
        cursor.execute("SELECT * FROM queued_messages WHERE id = ?", (msg_id,))
        return row_to_dict(cursor.fetchone())


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
