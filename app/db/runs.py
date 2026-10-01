"""Fenced, leased session ownership — the ``runs`` table (v2 core).

A *run* is one runner's leased claim on a session. At most one run per session
is ``active`` at a time (enforced by a partial unique index), and each run
carries a strictly monotonic ``fence_token`` that stamps every event append
(see ``app.db.session_events``) so a superseded runner can never write after it
has lost ownership.

Ownership is time-bounded: a claim sets ``lease_expires_at`` from a ttl, and the
runner must ``renew_run`` (heartbeat) before it lapses. A lapsed lease is
reaped at the next ``claim_run``, which frees the session for a fresh claim with
an incremented fence token.

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


_LIVE_TURN_STATES = "('running','interrupting','draining')"


def activity_summary(local_runner_id: str) -> Dict[str, Any]:
    """Content-free, point-in-time view of session work across every runner
    that shares this database (this worker and other registered installs).

    Only counts leave this function: no session, runner or instance id.

    * A run counts while its lease is unexpired; the runner's heartbeat
      renewing that lease is its proof of life.
    * A live turn counts when it belongs to such a run, attributed by the
      run's runner. A live turn without one is ``stale_live_turns``: its
      runner stopped renewing and nothing is writing for it.
    * Running background tasks carry no lease, so they are counted even when
      their worker may be gone. A caller deciding "idle" must treat them as
      activity: a wrong "active" is safe, a wrong "idle" is not.
    """
    summary: Dict[str, Any] = {
        "scope": "shared_database",
        "local_active_runs": 0, "other_active_runs": 0, "other_active_runners": 0,
        "local_live_turns": 0, "other_live_turns": 0, "stale_live_turns": 0,
        "local_running_background_tasks": 0, "other_running_background_tasks": 0,
        "unattributed_running_background_tasks": 0,
    }
    with get_db() as conn:
        cursor = conn.cursor()
        # Active runs and live turns are each covered by a partial unique
        # index, so these stay cheap however long the history grows.
        cursor.execute(
            "SELECT runner_id, COUNT(*) AS n FROM runs "
            "WHERE state='active' AND lease_expires_at >= NOW() GROUP BY runner_id"
        )
        for row in cursor.fetchall():
            if row["runner_id"] == local_runner_id:
                summary["local_active_runs"] += int(row["n"])
            else:
                summary["other_active_runs"] += int(row["n"])
                summary["other_active_runners"] += 1
        cursor.execute(
            "SELECT r.runner_id AS runner_id, COUNT(*) AS n FROM turns t "
            "JOIN runs r ON r.id = t.run_id "
            f"WHERE t.state IN {_LIVE_TURN_STATES} AND r.state='active' "
            "AND r.lease_expires_at >= NOW() GROUP BY r.runner_id"
        )
        leased = 0
        for row in cursor.fetchall():
            leased += int(row["n"])
            local = row["runner_id"] == local_runner_id
            summary["local_live_turns" if local else "other_live_turns"] += int(row["n"])
        cursor.execute(f"SELECT COUNT(*) AS n FROM turns WHERE state IN {_LIVE_TURN_STATES}")
        row = cursor.fetchone()
        summary["stale_live_turns"] = max(0, int(row["n"] if row else 0) - leased)
        cursor.execute(
            "SELECT instance_id, COUNT(*) AS n FROM background_tasks "
            "WHERE status='running' GROUP BY instance_id"
        )
        for row in cursor.fetchall():
            if not row["instance_id"]:
                key = "unattributed_running_background_tasks"
            elif row["instance_id"] == local_runner_id:
                key = "local_running_background_tasks"
            else:
                key = "other_running_background_tasks"
            summary[key] += int(row["n"])
    return summary
