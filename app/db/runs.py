"""Fenced, leased session ownership — the ``runs`` table (v2 core).

A *run* is one runner's leased claim on a session. At most one run per session
is ``active`` at a time (enforced by a partial unique index), and each run
carries a strictly monotonic ``fence_token`` that stamps every event append
(see ``app.db.session_events``) so a superseded runner can never write after it
has lost ownership.

Ownership is time-bounded: a claim sets ``lease_expires_at`` from a ttl, and the
runner must ``renew_run`` (heartbeat) before it lapses. Lapsed leases are reaped
by ``expire_due_runs`` (or opportunistically at the next ``claim_run``), which
frees the session for a fresh claim with an incremented fence token.

Every transition is a single guarded UPDATE/INSERT so concurrency is resolved by
the database, not by read-modify-write races in Python.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from typing import Any, Dict, Optional

import psycopg2

from app.db.base import get_db

logger = logging.getLogger(__name__)

# A second active run for the same session trips the partial unique index
# (idx_runs_one_active_per_session). psycopg2 raises UniqueViolation
# (IntegrityError) in production; the SQLite test shim raises its own
# IntegrityError. Catch both so a lost claim returns None either way.
_INTEGRITY_ERRORS = (psycopg2.IntegrityError, sqlite3.IntegrityError)


def claim_run(
    session_id: str, runner_id: str, ttl_seconds: int
) -> Optional[Dict[str, Any]]:
    """Claim active ownership of a session, reaping an expired lease first.

    Returns the new run row, or ``None`` if another live run already owns the
    session (partial-unique-index conflict).
    """
    with get_db() as conn:
        cursor = conn.cursor()
        # (a) Reap an expired active lease so a fresh claim can proceed.
        cursor.execute(
            "UPDATE runs SET state='expired', updated_at=NOW() "
            "WHERE session_id=%s AND state='active' AND lease_expires_at < NOW()",
            (session_id,),
        )
        # (b) Insert a fresh active run with the next fence token.
        run_id = str(uuid.uuid4())
        try:
            cursor.execute(
                "INSERT INTO runs "
                "(id, session_id, runner_id, fence_token, state, lease_expires_at) "
                "VALUES (%s, %s, %s, "
                "(SELECT COALESCE(MAX(fence_token), 0) + 1 FROM runs WHERE session_id=%s), "
                "'active', NOW() + (%s * INTERVAL '1 second')) "
                "RETURNING *",
                (run_id, session_id, runner_id, session_id, ttl_seconds),
            )
            row = cursor.fetchone()
        except _INTEGRITY_ERRORS:
            conn.rollback()
            return None
    return dict(row) if row else None


def renew_run(
    run_id: str, runner_id: str, fence_token: int, ttl_seconds: int
) -> bool:
    """Extend a still-live lease (heartbeat). False if the run is not the
    caller's active, unexpired run."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE runs SET lease_expires_at=NOW() + (%s * INTERVAL '1 second'), "
            "heartbeat_at=NOW(), updated_at=NOW() "
            "WHERE id=%s AND runner_id=%s AND fence_token=%s AND state='active' "
            "AND lease_expires_at >= NOW()",
            (ttl_seconds, run_id, runner_id, fence_token),
        )
        return cursor.rowcount > 0


def release_run(run_id: str, fence_token: int) -> bool:
    """Release an active run (voluntary hand-back). False if it is not active
    or the token does not match."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE runs SET state='released', updated_at=NOW() "
            "WHERE id=%s AND fence_token=%s AND state='active'",
            (run_id, fence_token),
        )
        return cursor.rowcount > 0


def transfer_run(
    session_id: str, new_runner_id: str, ttl_seconds: int
) -> Optional[Dict[str, Any]]:
    """Transfer ownership to a new runner, superseding any active run.

    Marks the current active run (if any) ``transferred`` and inserts a fresh
    active run for ``new_runner_id`` with an incremented fence token. Returns
    the new run row.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE runs SET state='transferred', updated_at=NOW() "
            "WHERE session_id=%s AND state='active'",
            (session_id,),
        )
        run_id = str(uuid.uuid4())
        cursor.execute(
            "INSERT INTO runs "
            "(id, session_id, runner_id, fence_token, state, lease_expires_at) "
            "VALUES (%s, %s, %s, "
            "(SELECT COALESCE(MAX(fence_token), 0) + 1 FROM runs WHERE session_id=%s), "
            "'active', NOW() + (%s * INTERVAL '1 second')) "
            "RETURNING *",
            (run_id, session_id, new_runner_id, session_id, ttl_seconds),
        )
        row = cursor.fetchone()
    return dict(row) if row else None


def get_active_run(session_id: str) -> Optional[Dict[str, Any]]:
    """Return the session's active run, or None."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM runs WHERE session_id=%s AND state='active'",
            (session_id,),
        )
        row = cursor.fetchone()
    return dict(row) if row else None


def expire_due_runs() -> int:
    """Expire every active run whose lease has lapsed. Returns the count."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE runs SET state='expired', updated_at=NOW() "
            "WHERE state='active' AND lease_expires_at < NOW()"
        )
        return cursor.rowcount
