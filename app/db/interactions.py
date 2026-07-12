"""Pending interaction state machine — the ``pending_interactions`` table.

An *interaction* is a durable prompt awaiting a user answer (permission prompt,
tool approval, …) with a TTL. This module is pure CRUD over the state machine
``pending -> answered | cancelled | expired``; every transition is a single
guarded UPDATE so an already-answered or lapsed interaction cannot be answered
twice. Emitting session events on these transitions is Phase-1 wiring and lives
elsewhere — this module deliberately does not touch the event log.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from app.db.base import get_db, rows_to_list

logger = logging.getLogger(__name__)


def _parse_json(raw: Any) -> Any:
    """Decode a JSONB column that the test shim stores as TEXT (dict/list pass
    through untouched; None stays None)."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None
    return raw


def _parse_row(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    row["payload"] = _parse_json(row.get("payload"))
    row["answer"] = _parse_json(row.get("answer"))
    return row


def create_interaction(
    session_id: str,
    kind: str,
    payload: Dict[str, Any],
    ttl_seconds: int,
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a pending interaction with a TTL. Returns the new row."""
    interaction_id = str(uuid.uuid4())
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO pending_interactions "
            "(id, session_id, run_id, kind, payload, state, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, 'pending', "
            "NOW() + (%s * INTERVAL '1 second')) "
            "RETURNING *",
            (
                interaction_id,
                session_id,
                run_id,
                kind,
                json.dumps(payload or {}),
                ttl_seconds,
            ),
        )
        row = cursor.fetchone()
    return _parse_row(dict(row))


def answer_interaction(
    interaction_id: str,
    answer: Dict[str, Any],
    answered_by_device: Optional[str] = None,
) -> bool:
    """Answer a still-pending, unexpired interaction. False otherwise."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE pending_interactions SET state='answered', answer=%s, "
            "answered_by_device=%s, answered_at=NOW() "
            "WHERE id=%s AND state='pending' AND expires_at >= NOW()",
            (json.dumps(answer or {}), answered_by_device, interaction_id),
        )
        return cursor.rowcount > 0


def cancel_interaction(interaction_id: str) -> bool:
    """Cancel a pending interaction. False if it is no longer pending."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE pending_interactions SET state='cancelled' "
            "WHERE id=%s AND state='pending'",
            (interaction_id,),
        )
        return cursor.rowcount > 0


def expire_due_interactions() -> int:
    """Expire every pending interaction past its TTL. Returns the count."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE pending_interactions SET state='expired' "
            "WHERE state='pending' AND expires_at < NOW()"
        )
        return cursor.rowcount


def get_pending_interactions(session_id: str) -> List[Dict[str, Any]]:
    """List a session's pending, unexpired interactions oldest-first."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM pending_interactions "
            "WHERE session_id=%s AND state='pending' AND expires_at >= NOW() "
            "ORDER BY created_at",
            (session_id,),
        )
        rows = rows_to_list(cursor.fetchall())
    return [_parse_row(row) for row in rows]


def get_interaction(interaction_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a single interaction by id, or None."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM pending_interactions WHERE id=%s", (interaction_id,)
        )
        row = cursor.fetchone()
    return _parse_row(dict(row) if row else None)
