"""Pending interaction state machine — the ``pending_interactions`` table.

An *interaction* is a durable prompt awaiting a user answer (permission prompt,
tool approval, …) with a TTL. This module is pure CRUD over the state machine
``pending -> answered | cancelled | expired``; every transition is a single
guarded UPDATE so an already-answered or lapsed interaction cannot be answered
twice. Emitting session events on these transitions is Phase-1 wiring and lives
elsewhere — this module deliberately does not touch the event log.
"""

from __future__ import annotations

from contextlib import nullcontext
import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from app.db.base import get_db, parse_jsonb as _parse_json, rows_to_list

logger = logging.getLogger(__name__)


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
    ttl_seconds: Optional[int],
    run_id: Optional[str] = None,
    *,
    interaction_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a pending interaction; a None TTL means no automatic expiry."""
    interaction_id = interaction_id or str(uuid.uuid4())
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO pending_interactions "
            "(id, session_id, run_id, kind, payload, state, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, 'pending', "
            "NOW() + (%s * INTERVAL '1 second')) "
            "ON CONFLICT (id) DO UPDATE SET id=EXCLUDED.id RETURNING *",
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
    parsed = _parse_row(dict(row))
    assert parsed is not None  # RETURNING * always yields exactly one row
    return parsed


def answer_interaction(
    interaction_id: str,
    answer: Dict[str, Any],
    answered_by_device: Optional[str] = None,
    *,
    _conn: Any = None,
) -> bool:
    """Answer a still-pending, unexpired interaction. False otherwise."""
    with (nullcontext(_conn) if _conn is not None else get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE pending_interactions SET state='answered', answer=%s, "
            "answered_by_device=%s, answered_at=NOW() "
            "WHERE id=%s AND state='pending' AND (expires_at IS NULL OR expires_at >= NOW())",
            (json.dumps(answer or {}), answered_by_device, interaction_id),
        )
        return cursor.rowcount > 0


def answer_question_and_enqueue(
    row: Dict[str, Any], answer: Dict[str, Any], answered_by_device: Optional[str],
    prompt: str, context: Dict[str, Any],
) -> bool:
    """Accept an answer and its user turn in one commit; retries cannot enqueue twice."""
    from app.db import turns

    with get_db() as conn:
        if not answer_interaction(row["id"], answer, answered_by_device, _conn=conn):
            return False
        turns.enqueue_turn(
            row["session_id"], "user", prompt=prompt,
            profile_id=context.get("profile_id"), project_id=context.get("project_id"),
            worktree_id=context.get("worktree_id"), overrides=context.get("overrides"),
            payload={**(context.get("payload") or {}), "deferred_user_record": True,
                     "question_request_id": (row.get("payload") or {}).get("request_id")},
            command_id=f"question-answer-{row['id']}", _conn=conn,
        )
    return True


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


def expire_due_interactions_detailed() -> List[Dict[str, Any]]:
    """Expire every pending interaction past its TTL, returning the swept rows
    (id, session_id, kind, parsed payload) so the sweeper can NOTIFY each
    affected session and emit a durable ``interaction.expired`` (reason=ttl)
    carrying the row's ``request_id`` (nested in ``payload``)."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE pending_interactions SET state='expired' "
            "WHERE state='pending' AND expires_at < NOW() "
            "RETURNING id, session_id, kind, payload"
        )
        rows = rows_to_list(cursor.fetchall())
    for row in rows:
        row["payload"] = _parse_json(row.get("payload"))
    return rows


def get_answered_interactions(session_id: str, kind: Optional[str] = None) -> List[Dict[str, Any]]:
    """List a session's answered interactions oldest-first (optionally by kind).

    Used by the v2 permission adapter to derive the *deferred* set: rows
    answered with ``decision == 'defer'`` that have not been requeued.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        if kind is None:
            cursor.execute(
                "SELECT * FROM pending_interactions "
                "WHERE session_id=%s AND state='answered' ORDER BY created_at",
                (session_id,),
            )
        else:
            cursor.execute(
                "SELECT * FROM pending_interactions "
                "WHERE session_id=%s AND state='answered' AND kind=%s "
                "ORDER BY created_at",
                (session_id, kind),
            )
        rows = rows_to_list(cursor.fetchall())
    return [r for r in (_parse_row(row) for row in rows) if r is not None]


def mark_interaction_requeued(interaction_id: str) -> bool:
    """Stamp ``requeued: true`` into an answered interaction's answer JSON.

    Requeue of a deferred permission creates a NEW
    pending row carrying the same FE request_id; this stamp on the OLD
    answered-defer row keeps it out of the derived deferred set so the FE
    never sees the same request as both deferred and pending. Read-modify-write
    in Python (not a JSONB ``||``) so the SQLite test shim stays compatible.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT answer FROM pending_interactions "
            "WHERE id=%s AND state='answered'",
            (interaction_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return False
        answer = _parse_json(row["answer"]) or {}
        answer["requeued"] = True
        cursor.execute(
            "UPDATE pending_interactions SET answer=%s "
            "WHERE id=%s AND state='answered'",
            (json.dumps(answer), interaction_id),
        )
        return cursor.rowcount > 0


def get_pending_interactions(session_id: str) -> List[Dict[str, Any]]:
    """List a session's pending, unexpired interactions oldest-first."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM pending_interactions "
            "WHERE session_id=%s AND state='pending' AND (expires_at IS NULL OR expires_at >= NOW()) "
            "ORDER BY created_at",
            (session_id,),
        )
        rows = rows_to_list(cursor.fetchall())
    return [r for r in (_parse_row(row) for row in rows) if r is not None]


def get_interaction(interaction_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a single interaction by id, or None."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM pending_interactions WHERE id=%s", (interaction_id,)
        )
        row = cursor.fetchone()
    return _parse_row(dict(row) if row else None)
