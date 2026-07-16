"""The ``turns`` table — durable per-session turn queue + live-turn state
machine (v2 core, Phase 2).

A *turn* is one unit of work for a session: a user prompt, a queued follow-up,
an unpark wake, a failover retry, an automation intake, and so on. Turns move
through::

    queued -> running -> interrupting -> draining -> completed | aborted
              running --------------------------> parked
              running/interrupting/draining ----> requeued (back to queued)

At most one turn per session may occupy a *live* state
(``running``/``interrupting``/``draining``) at a time — enforced by the partial
unique index ``idx_turns_one_live_per_session``, exactly the DB-raced ownership
discipline used by ``app.db.runs``. Every transition is a single guarded
UPDATE/INSERT so concurrency is resolved by the database, never by
read-modify-write races in Python: two racers that both try to make a turn live
for the same session will trip the unique index, and the loser returns cleanly.

This module is a PURE DB layer. It deliberately does NOT append ``turn.*``
events to ``session_events`` — the log is the record, but that mirroring is
wired at the runtime/command layer (Phase 2 A4), not here.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from typing import Any, Dict, List, Optional

import psycopg2

from app.db.base import get_db

logger = logging.getLogger(__name__)

# A second live turn for the same session trips the partial unique index
# (idx_turns_one_live_per_session). psycopg2 raises UniqueViolation
# (IntegrityError) in production; the SQLite test shim raises its own
# IntegrityError. Catch both so the loser of a start/transition race returns
# cleanly (None) either way.
_INTEGRITY_ERRORS = (psycopg2.IntegrityError, sqlite3.IntegrityError)

# States considered "live" — a turn actively owning the session's single runner
# slot. The partial unique index is defined over exactly this set.
LIVE_STATES = ("running", "interrupting", "draining")
# Terminal states — settle/park are idempotent no-ops once a turn is here.
TERMINAL_STATES = ("completed", "aborted", "parked")


def _parse_json(raw: Any) -> Any:
    """Decode a JSONB column that the test shim stores as TEXT (dict/list pass
    through untouched; None stays None)."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None
    return raw


def _row(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Normalize a fetched turn row: decode JSONB columns to Python objects."""
    if row is None:
        return None
    d = dict(row)
    d["overrides"] = _parse_json(d.get("overrides")) or {}
    d["payload"] = _parse_json(d.get("payload")) or {}
    return d


def _rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [d for r in rows if (d := _row(r)) is not None]


# ---------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------

def enqueue_turn(
    session_id: str,
    source: str,
    *,
    prompt: Optional[str] = None,
    profile_id: Optional[str] = None,
    project_id: Optional[str] = None,
    worktree_id: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
    payload: Optional[Dict[str, Any]] = None,
    command_id: Optional[str] = None,
    parent_turn_id: Optional[str] = None,
    instance_id: Optional[str] = None,
    at_front: bool = False,
) -> Dict[str, Any]:
    """Append a new ``queued`` turn to a session's queue.

    ``position`` is assigned strictly after every existing turn for the session
    (``MAX(position) + 1``) so a new item always sorts to the back. A crashed
    live turn re-queued by ``requeue_turn`` keeps its original (lower) position
    and therefore jumps ahead of anything queued after it — front-of-queue by
    construction, no position rewrite needed.

    Two concurrent enqueues can read the same MAX and tie on ``position``; the
    queue reads break ties deterministically (created_at, then id), so a tie
    reorders nothing across processes — it just fixes the order.

    When ``at_front`` is True the turn is instead placed strictly BEFORE every
    currently-queued row for the session (``MIN(position) - 1`` over the queued
    rows; empty queue → ``0``). This is the failover-retry case: the retry is a
    continuation of the settled turn and must run ahead of anything a user
    queued during the failed turn. ``position`` is ``DOUBLE PRECISION`` (both
    the Postgres schema and the pg_shim SQLite path), so the resulting zero /
    negative values sort correctly.

    Returns the created turn row.
    """
    turn_id = f"turn-{uuid.uuid4().hex}"
    with get_db() as conn:
        cursor = conn.cursor()
        if at_front:
            cursor.execute(
                "SELECT COALESCE(MIN(position), 1) - 1 AS pos FROM turns "
                "WHERE session_id = %s AND state = 'queued'",
                (session_id,),
            )
        else:
            cursor.execute(
                "SELECT COALESCE(MAX(position), 0) + 1 AS pos FROM turns WHERE session_id = %s",
                (session_id,),
            )
        pos_row = cursor.fetchone()
        position = pos_row["pos"] if pos_row and pos_row.get("pos") is not None else (0 if at_front else 1)
        cursor.execute(
            "INSERT INTO turns "
            "(id, session_id, state, source, prompt, profile_id, project_id, "
            "worktree_id, overrides, payload, command_id, parent_turn_id, "
            "instance_id, position) "
            "VALUES (%s, %s, 'queued', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "RETURNING *",
            (
                turn_id,
                session_id,
                source,
                prompt,
                profile_id,
                project_id,
                worktree_id,
                json.dumps(overrides or {}),
                json.dumps(payload or {}),
                command_id,
                parent_turn_id,
                instance_id,
                position,
            ),
        )
        row = _row(cursor.fetchone())
        if row is None:  # INSERT ... RETURNING always yields the new row
            raise RuntimeError(f"enqueue_turn: INSERT returned no row for {turn_id}")
        return row


def update_queued_turn_prompt(
    turn_id: str, prompt: str, payload: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Rewrite a still-``queued`` turn's ``prompt`` + ``payload`` in place.

    Guarded (``WHERE id=%s AND state='queued'``) so a merge into an unpark wake
    that has since started (or otherwise left the queue) loses cleanly: the
    UPDATE matches no row and ``None`` is returned, and the caller falls back to
    enqueueing a fresh turn. Returns the updated turn row on success.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE turns SET prompt=%s, payload=%s "
            "WHERE id=%s AND state='queued' "
            "RETURNING *",
            (prompt, json.dumps(payload or {}), turn_id),
        )
        row = cursor.fetchone()
    return _row(row)


# ---------------------------------------------------------------------------
# Live-state transitions (all DB-raced via the partial unique index)
# ---------------------------------------------------------------------------

def start_turn(
    turn_id: str,
    run_id: Optional[str] = None,
    instance_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Transition a ``queued`` turn to ``running`` (claim the session's live
    slot).

    Returns the now-running turn row, or ``None`` if the caller lost the race:
    either another racer already took this turn out of ``queued`` (rowcount 0),
    or another turn is already live for the session (partial-unique-index
    conflict). Both losers return ``None`` without raising — the caller retries
    or aborts.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "UPDATE turns SET state='running', run_id=%s, instance_id=%s, "
                "started_at=NOW() "
                "WHERE id=%s AND state='queued' "
                "RETURNING *",
                (run_id, instance_id, turn_id),
            )
            row = cursor.fetchone()
        except _INTEGRITY_ERRORS:
            conn.rollback()
            return None
    return _row(row)


def interrupt_turn(
    session_id: str,
    mode: str,
    stop_background: bool = False,
    turn_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Transition a session's ``running`` turn to ``interrupting``.

    ``mode`` is ``'stop'`` or ``'send_now'``; ``stop_background`` records
    whether background children should be torn down. Returns the interrupting
    turn row, or ``None`` if there is no running turn to interrupt.

    When ``turn_id`` is provided the UPDATE also requires ``id=turn_id`` so a
    stop whose target turn is no longer the running one (it settled and a
    successor took the live slot) matches zero rows and returns ``None`` — a
    no-op, never touching the successor (GitHub #585). When ``turn_id`` is
    absent the behavior is byte-identical to the legacy session-scoped stop.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        if turn_id is not None:
            cursor.execute(
                "UPDATE turns SET state='interrupting', interrupt_mode=%s, "
                "stop_background=%s "
                "WHERE session_id=%s AND state='running' AND id=%s "
                "RETURNING *",
                (mode, bool(stop_background), session_id, turn_id),
            )
        else:
            cursor.execute(
                "UPDATE turns SET state='interrupting', interrupt_mode=%s, "
                "stop_background=%s "
                "WHERE session_id=%s AND state='running' "
                "RETURNING *",
                (mode, bool(stop_background), session_id),
            )
        row = cursor.fetchone()
    return _row(row)


def escalate_to_draining(turn_id: str) -> Optional[Dict[str, Any]]:
    """Transition an ``interrupting`` turn to ``draining`` (a wedged runner that
    did not settle promptly keeps the session busy while its reader drains).

    Returns the draining turn row, or ``None`` if the turn was not
    interrupting.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE turns SET state='draining' "
            "WHERE id=%s AND state='interrupting' "
            "RETURNING *",
            (turn_id,),
        )
        row = cursor.fetchone()
    return _row(row)


def settle_turn(
    turn_id: str,
    outcome: str,
    stop_reason: Optional[str] = None,
    error: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Settle a turn to a terminal ``outcome`` (``'completed'`` or
    ``'aborted'``). Idempotent: a turn already in a terminal state
    (completed/aborted/parked) is left untouched and ``None`` is returned.

    Returns the settled turn row on the transition that actually settles it.
    """
    if outcome not in ("completed", "aborted"):
        raise ValueError(f"settle_turn outcome must be completed|aborted, got {outcome!r}")
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE turns SET state=%s, stop_reason=%s, error=%s, ended_at=NOW() "
            "WHERE id=%s AND state NOT IN ('completed','aborted','parked') "
            "RETURNING *",
            (outcome, stop_reason, error, turn_id),
        )
        row = cursor.fetchone()
    return _row(row)


def remove_queued_turn(turn_id: str) -> Optional[Dict[str, Any]]:
    """Atomically abort a still-``queued`` turn (queue-chip removal).

    The ``state='queued'`` guard lives in the UPDATE itself so a turn that
    races to ``running`` between the caller's intent and this statement is
    left alone (``None`` returned) instead of being aborted mid-flight.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE turns SET state='aborted', stop_reason='removed_from_queue', "
            "ended_at=NOW() "
            "WHERE id=%s AND state='queued' "
            "RETURNING *",
            (turn_id,),
        )
        row = cursor.fetchone()
    return _row(row)


def park_turn(
    turn_id: str, task_ids: Optional[List[str]] = None
) -> Optional[Dict[str, Any]]:
    """Park a live turn (it finished the foreground stream but left background
    work running). Records the surviving ``task_ids`` in ``payload.task_ids``.
    Idempotent-ish: only a live turn parks; an already-terminal turn returns
    ``None``.
    """
    task_ids = list(task_ids or [])
    with get_db() as conn:
        cursor = conn.cursor()
        # Merge task_ids into the existing payload rather than clobbering it.
        cursor.execute("SELECT payload FROM turns WHERE id=%s", (turn_id,))
        cur = cursor.fetchone()
        payload = _parse_json(cur.get("payload")) if cur else None
        payload = dict(payload or {})
        payload["task_ids"] = task_ids
        cursor.execute(
            "UPDATE turns SET state='parked', payload=%s, ended_at=NOW() "
            "WHERE id=%s AND state IN ('running','interrupting','draining') "
            "RETURNING *",
            (json.dumps(payload), turn_id),
        )
        row = cursor.fetchone()
    return _row(row)


def requeue_turn(turn_id: str) -> Optional[Dict[str, Any]]:
    """Return a live turn to the ``queued`` state, releasing the session's live
    slot while KEEPING its ``position`` (so it re-enters at the front, ahead of
    anything queued after it). Clears the run binding. Returns the re-queued
    turn row, or ``None`` if the turn was not live.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE turns SET state='queued', run_id=NULL, instance_id=NULL, "
            "started_at=NULL, interrupt_mode=NULL, stop_background=FALSE "
            "WHERE id=%s AND state IN ('running','interrupting','draining') "
            "RETURNING *",
            (turn_id,),
        )
        row = cursor.fetchone()
    return _row(row)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def get_turn(turn_id: str) -> Optional[Dict[str, Any]]:
    """Return a single turn by id, or ``None``."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM turns WHERE id=%s", (turn_id,))
        row = cursor.fetchone()
    return _row(row)


def get_live_turn(session_id: str) -> Optional[Dict[str, Any]]:
    """Return the session's single live (running|interrupting|draining) turn, or
    ``None``."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM turns "
            "WHERE session_id=%s AND state IN ('running','interrupting','draining') "
            "LIMIT 1",
            (session_id,),
        )
        row = cursor.fetchone()
    return _row(row)


def get_queued_turns(session_id: str) -> List[Dict[str, Any]]:
    """Return all ``queued`` turns for a session, ordered front-to-back by
    ``position``."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM turns WHERE session_id=%s AND state='queued' "
            "ORDER BY position ASC, created_at ASC, id ASC",
            (session_id,),
        )
        rows = cursor.fetchall()
    return _rows(rows)


def pop_ready(session_id: str) -> Optional[Dict[str, Any]]:
    """Return the front (lowest ``position``) ``queued`` turn for a session, or
    ``None`` if the queue is empty.

    Despite the name, this does NOT mutate — it is a peek. Ownership is taken by
    a subsequent ``start_turn`` guarded transition, so a queued turn is never
    dropped before it is actually claimed (a crashed claim leaves the row queued
    for the next ``pop_ready``).
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM turns WHERE session_id=%s AND state='queued' "
            "ORDER BY position ASC, created_at ASC, id ASC LIMIT 1",
            (session_id,),
        )
        row = cursor.fetchone()
    return _row(row)


def list_claimable_sessions() -> List[str]:
    """Return the DISTINCT session ids that have at least one ``queued`` turn and
    NO live run lease — the candidate set for the cross-runner claim loop
    (V2 Phase 4).

    The anti-join treats an ``active`` run with a LAPSED lease as absent: a
    crashed owner's stale row must not block another runner from claiming — the
    claimer's ``runs.claim_run`` reaps it atomically on the way in. Sessions
    only (not turns): the claim loop resolves placement per SESSION and kicks
    the scheduler, which orders the queue itself.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT DISTINCT t.session_id FROM turns t "
            "LEFT JOIN runs r ON r.session_id = t.session_id "
            "AND r.state = 'active' AND r.lease_expires_at >= NOW() "
            "WHERE t.state = 'queued' AND r.id IS NULL",
        )
        rows = cursor.fetchall()
    return [r["session_id"] if isinstance(r, dict) else r[0] for r in rows]


def list_interrupting_for_runner(runner_id: str) -> List[Dict[str, Any]]:
    """Return the ``interrupting`` turns this runner is executing (V2 Phase 4
    interrupt-observe pass).

    ``instance_id`` on a live turn is stamped exclusively by ``start_turn`` with
    the EXECUTING runner's id (verified Q3-E1), so this filter is exactly "my
    in-flight turns whose durable interrupt intent was flipped" — including
    flips made by a FOREIGN install's WS handler, which the in-process interrupt
    executor cannot deliver across installs.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM turns WHERE state='interrupting' AND instance_id=%s",
            (runner_id,),
        )
        rows = cursor.fetchall()
    return _rows(rows)


# ---------------------------------------------------------------------------
# Reaping dead-lease live turns
# ---------------------------------------------------------------------------

def reap_expired() -> List[Dict[str, Any]]:
    """Resolve every live turn whose run lease is dead (run missing, not active,
    or lapsed) — the crash/kill recovery sweep.

    A dead-lease live turn is resolved by its ``source``:

    * ``user`` / ``queue`` turns are **re-queued** at their original position so
      the work is retried (front of queue), and
    * every other source is **aborted** (``stop_reason='lease_expired'``).

    Returns the list of turn rows as they were BEFORE resolution (so a caller
    can see what was reaped and how each was routed via its ``source``).
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT t.* FROM turns t "
            "LEFT JOIN runs r ON t.run_id = r.id "
            "WHERE t.state IN ('running','interrupting','draining') "
            "AND (r.id IS NULL OR r.state <> 'active' OR r.lease_expires_at < NOW())",
        )
        dead = _rows(cursor.fetchall())
        for turn in dead:
            if turn["source"] in ("user", "queue"):
                cursor.execute(
                    "UPDATE turns SET state='queued', run_id=NULL, instance_id=NULL, "
                    "started_at=NULL, interrupt_mode=NULL, stop_background=FALSE "
                    "WHERE id=%s AND state IN ('running','interrupting','draining')",
                    (turn["id"],),
                )
            else:
                cursor.execute(
                    "UPDATE turns SET state='aborted', stop_reason='lease_expired', "
                    "ended_at=NOW() "
                    "WHERE id=%s AND state IN ('running','interrupting','draining')",
                    (turn["id"],),
                )
    return dead
