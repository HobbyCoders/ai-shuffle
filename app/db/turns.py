"""The ``turns`` table — durable per-session turn queue + live-turn state
machine (v2 core).

A *turn* is one unit of work for a session: a user prompt, a queued follow-up,
an unpark wake, a failover retry, an automation intake, and so on. Turns move
through::

    queued -> running -> interrupting -> draining -> completed | aborted
              running --------------------------> parked
              parked ---------------------------> completed
                  (settle_parked_turns — stale-park settlement / CLI-wake at rest)
              running ---------------------------> requeued (back to queued)
                  (dead-lease crash recovery; interrupting/draining dead-lease
                   turns are aborted instead — see ``reap_expired``)
    queued ------------------------------------> completed (stop_reason='steered')
                  (absorb_queued_turn — the queued prompt was handed to the LIVE
                   turn mid-stream as a fluid steer, so it never runs as a turn
                   of its own; its user message is recorded at delivery instead)

At most one turn per session may occupy a *live* state
(``running``/``interrupting``/``draining``) at a time — enforced by the partial
unique index ``idx_turns_one_live_per_session``, exactly the DB-raced ownership
discipline used by ``app.db.runs``. Every transition is a single guarded
UPDATE/INSERT so concurrency is resolved by the database, never by
read-modify-write races in Python: two racers that both try to make a turn live
for the same session will trip the unique index, and the loser returns cleanly.

This module is a PURE DB layer. It deliberately does NOT append ``turn.*``
events to ``session_events`` — the log is the record, but that mirroring is
wired at the runtime/command layer, not here.
"""

from __future__ import annotations

from contextlib import nullcontext
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Collection, Dict, List, Optional

import psycopg2

from app.core.config import settings
from app.db.base import get_db, parse_jsonb as _parse_json
from app.db.compat import phs

logger = logging.getLogger(__name__)

# A second live turn for the same session trips the partial unique index
# (idx_turns_one_live_per_session). psycopg2 raises UniqueViolation
# (IntegrityError) in production; the SQLite test shim raises its own
# IntegrityError. Catch both so the loser of a start/transition race returns
# cleanly (None) either way.
_INTEGRITY_ERRORS = (psycopg2.IntegrityError, sqlite3.IntegrityError)

# Terminal states — settle/park are idempotent no-ops once a turn is here.
TERMINAL_STATES = ("completed", "aborted", "parked")


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
# Session working time (sessions.work_ms / sessions.work_started_at)
# ---------------------------------------------------------------------------
#
# The sidebar shows how long a session has actually worked: the sum of its
# turns' live intervals (start -> settle/park), never the idle gaps between
# them. The total lives on the SESSION row rather than being summed from this
# table on read because session rows travel to peer workers and turns do not
# (app/network/policy.py), so a mirrored session could never show it otherwise.
#
# Each transition that ends a live interval banks it exactly once: the guarded
# UPDATE only returns a row on the transition that actually happened. The
# session write runs in its OWN transaction after the turn transition commits,
# so a failure there can never roll back or deadlock the transition; a crash
# between the two loses one interval, nothing more. A requeued crash turn banks
# nothing (its partial run is discarded along with its started_at).
#
# Time a live turn spends blocked on the USER (a permission prompt or an
# AskUser question) is not working time either (owner decision, 2026-10-01).
# ``work_started_at`` is therefore the start of the turn's current COUNTED
# segment, not of the turn: ``pause_session_work`` banks the segment and
# clears it when the runner starts waiting on an answer, and
# ``resume_session_work`` opens a new one when the answer arrives. A turn
# that ends banks from that segment start (clamped to its own start), so the
# pre-prompt part, banked at pause time, is never counted twice.

def _to_utc(value: Any) -> Optional[datetime]:
    """A DB timestamp (aware/naive datetime or text) as an aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def interval_ms(started_at: Any, ended_at: Any) -> int:
    """Milliseconds between two DB timestamps; 0 when either is missing."""
    start, end = _to_utc(started_at), _to_utc(ended_at)
    if start is None or end is None or end <= start:
        return 0
    return int((end - start).total_seconds() * 1000)


_LIVE_TURN_START_SQL = (
    "(SELECT MAX(t.started_at) FROM turns t "
    " WHERE t.session_id = %s AND t.state IN ('running','interrupting','draining'))"
)


def _record_session_work(session_id: Optional[str], banked_ms: int = 0) -> None:
    """Bank ``banked_ms`` onto the session and re-derive its live-turn start.

    ``work_started_at`` is re-read from the turn holding the session's live
    slot, so a missed write self-corrects on the next start. Never raises:
    the working-time display must never break a turn transition.
    """
    if not session_id:
        return
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE sessions SET work_ms = COALESCE(work_ms, 0) + %s, "
                f"work_started_at = {_LIVE_TURN_START_SQL} "
                "WHERE id = %s",
                (max(0, int(banked_ms)), session_id, session_id),
            )
    except Exception:  # noqa: BLE001 — display bookkeeping only
        logger.warning("Session work-time update failed for %s", session_id, exc_info=True)


def _bank_live_segment(session_id: Optional[str], turn_started_at: Any, end: Any) -> None:
    """Bank the counted segment of a turn that just left the live slot.

    The segment runs from the session's ``work_started_at`` (the turn's start,
    or the answer that last resumed it, clamped to the turn's own start) to
    ``end``. A NULL segment start means the turn ended while blocked on the
    user: everything before the prompt was banked at pause time, so nothing
    more is added. Never raises.
    """
    if not session_id:
        return
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT work_started_at FROM sessions WHERE id = %s FOR UPDATE",
                (session_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return
            segment_start = _to_utc(row["work_started_at"])
            banked = 0
            if segment_start is not None:
                turn_start = _to_utc(turn_started_at)
                if turn_start is not None and turn_start > segment_start:
                    segment_start = turn_start
                banked = interval_ms(segment_start, end)
            cursor.execute(
                "UPDATE sessions SET work_ms = COALESCE(work_ms, 0) + %s, "
                f"work_started_at = {_LIVE_TURN_START_SQL} "
                "WHERE id = %s",
                (banked, session_id, session_id),
            )
    except Exception:  # noqa: BLE001 — display bookkeeping only
        logger.warning("Session work-time update failed for %s", session_id, exc_info=True)


def pause_session_work(session_id: Optional[str]) -> bool:
    """Stop counting while a live turn blocks on the user.

    Banks the open segment up to now and clears ``work_started_at``. Returns
    True when the row changed (a segment was open). Never raises.
    """
    if not session_id:
        return False
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT work_started_at, NOW() AS db_now, "
                f"{_LIVE_TURN_START_SQL} AS live_started "
                "FROM sessions WHERE id = %s FOR UPDATE",
                (session_id, session_id),
            )
            row = cursor.fetchone()
            if row is None or row["work_started_at"] is None:
                return False
            segment_start = _to_utc(row["work_started_at"])
            live_start = _to_utc(row["live_started"])
            banked = 0
            # No live turn means a stale start: clear it and bank nothing.
            if segment_start is not None and live_start is not None:
                banked = interval_ms(max(segment_start, live_start), row["db_now"])
            cursor.execute(
                "UPDATE sessions SET work_ms = COALESCE(work_ms, 0) + %s, "
                "work_started_at = NULL WHERE id = %s",
                (banked, session_id),
            )
            return True
    except Exception:  # noqa: BLE001 — display bookkeeping only
        logger.warning("Session work-time pause failed for %s", session_id, exc_info=True)
        return False


def resume_session_work(session_id: Optional[str]) -> bool:
    """Start counting again once the user has answered.

    Opens a new segment at now, but only while a turn still holds the live
    slot and no segment is open. Returns True when the row changed. Never
    raises.
    """
    if not session_id:
        return False
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE sessions SET work_started_at = NOW() "
                "WHERE id = %s AND work_started_at IS NULL "
                "AND EXISTS (SELECT 1 FROM turns t WHERE t.session_id = %s "
                "  AND t.state IN ('running','interrupting','draining'))",
                (session_id, session_id),
            )
            return cursor.rowcount > 0
    except Exception:  # noqa: BLE001 — display bookkeeping only
        logger.warning("Session work-time resume failed for %s", session_id, exc_info=True)
        return False


def _dead_runner_end(row: Dict[str, Any]) -> Optional[datetime]:
    """End of a turn whose runner died: its run's lease expiry, if earlier.

    The reap and the orphan Stop stamp ``ended_at=NOW()``, which can be long
    after the runner stopped (a worker that was down for an hour reaps at
    boot). The run's ``lease_expires_at`` bounds when it last worked. Runs
    after the transition commits, like the session write; never raises.
    """
    end = _to_utc(row.get("ended_at"))
    run_id = row.get("run_id")
    if run_id:
        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT lease_expires_at FROM runs WHERE id=%s", (run_id,))
                run = cursor.fetchone()
            lease_end = _to_utc(run["lease_expires_at"]) if run else None
            if lease_end is not None and (end is None or lease_end < end):
                end = lease_end
        except Exception:  # noqa: BLE001 — display bookkeeping only
            logger.debug("Run lease lookup failed for %s", run_id, exc_info=True)
    return end


def _bank_ended(row: Optional[Dict[str, Any]]) -> None:
    """Bank a turn row that just left the live slot (settled, parked, aborted).

    A row that never started (a queued turn removed or aborted) never held
    the slot, so the segment of the turn that does hold it is left alone.
    """
    if row is None or row.get("started_at") is None:
        return
    _bank_live_segment(row.get("session_id"), row.get("started_at"), row.get("ended_at"))


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
    explicit_permission: bool = False,
    _conn: Any = None,
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

    Returns the created turn row. Only a producer's explicit_permission choice
    may stamp a grant; copying overrides must never copy that authority.
    """
    overrides = dict(overrides or {})
    overrides.pop("_permission_explicit", None)
    if explicit_permission and overrides.get("permission_mode"):
        overrides["_permission_explicit"] = True
    turn_id = f"turn-{uuid.uuid4().hex}"
    with (nullcontext(_conn) if _conn is not None else get_db()) as conn:
        cursor = conn.cursor()
        # All producers reach this boundary, including scheduler retries/unpark
        # and atomic AskUser answers that bypass turn_manager. Snapshot the
        # acknowledged pin, not an older turn's window. Human sends persist
        # explicit choices before enqueueing, so their new binding still wins.
        cursor.execute("SELECT context_window FROM sessions WHERE id = %s", (session_id,))
        binding = cursor.fetchone()
        if binding and binding.get("context_window"):
            overrides = {**(overrides or {}), "context_window": binding["context_window"]}
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


def set_live_turn_overrides(turn_id: str, overrides: Dict[str, Any]) -> bool:
    """Store the run binding a LIVE turn switched to mid-turn (effort, Fast
    mode, permission mode changed while it runs).

    The runtime already runs under the new values; without this the row keeps
    the ones it was claimed with. Work that inherits this turn's context (an
    answer to its question, a peer message, a wake) copies the row's binding,
    so it would differ from the live turn and never be steered into it.
    Guarded to live states so a settled turn's history is never rewritten.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE turns SET overrides=%s "
            "WHERE id=%s AND state IN ('running','draining','interrupting')",
            (json.dumps(overrides or {}), turn_id),
        )
        return cursor.rowcount > 0


def record_adhoc_turn(
    session_id: str,
    source: str,
    *,
    outcome: str = "completed",
    prompt: Optional[str] = None,
    profile_id: Optional[str] = None,
    project_id: Optional[str] = None,
    stop_reason: Optional[str] = None,
    error: Optional[str] = None,
    started_at=None,
    payload: Optional[Dict[str, Any]] = None,
    overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Write a TERMINAL history row for a turn that already ran outside the
    scheduler (ad-hoc REST calls, automation steps, headless background runs).

    These paths spend provider tokens but never take a run lease, so they must
    NOT be represented by a live or queued row. A ``running`` row with no lease
    is aborted mid-flight by the 15s reap sweep
    (``streaming_status._reap_and_kick_turns`` -> ``reap_expired``), and a
    ``queued`` row can be picked up and executed a second time by the
    cross-runner claim loop. Writing the row already terminal — after the turn
    actually ran — makes it inert to both: the scheduler reads only ``queued``,
    the reaper touches only live states, the partial unique index
    ``idx_turns_one_live_per_session`` covers only live states (so this insert
    succeeds even while a real turn is live for the same session), and
    lifecycle computation ignores it. ``run_id``/``instance_id`` stay NULL
    because there was never a lease to point at.

    ``started_at`` is the caller's recorded turn start (defaults to now);
    ``ended_at`` is stamped at insert. Returns the created turn row.
    """
    if outcome not in ("completed", "aborted"):
        raise ValueError(
            f"record_adhoc_turn outcome must be completed|aborted, got {outcome!r}"
        )
    turn_id = f"turn-{uuid.uuid4().hex}"
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 AS pos FROM turns WHERE session_id = %s",
            (session_id,),
        )
        pos_row = cursor.fetchone()
        position = pos_row["pos"] if pos_row and pos_row.get("pos") is not None else 1
        cursor.execute(
            "INSERT INTO turns "
            "(id, session_id, state, source, prompt, profile_id, project_id, "
            "overrides, payload, position, stop_reason, error, started_at, ended_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "COALESCE(%s, NOW()), NOW()) "
            "RETURNING *",
            (
                turn_id,
                session_id,
                outcome,
                source,
                prompt,
                profile_id,
                project_id,
                json.dumps(overrides or {}),
                json.dumps(payload or {}),
                position,
                stop_reason,
                error,
                started_at,
            ),
        )
        row = _row(cursor.fetchone())
        if row is None:  # INSERT ... RETURNING always yields the new row
            raise RuntimeError(f"record_adhoc_turn: INSERT returned no row for {turn_id}")
    # The ad-hoc paths refuse to run while a turn is live (busy-write guard),
    # so their interval never overlaps one the scheduler already banked.
    _record_session_work(
        row.get("session_id"), interval_ms(row.get("started_at"), row.get("ended_at"))
    )
    return row


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
            row = _row(cursor.fetchone())
            if row is not None:
                # A pick may be acknowledged after this turn was queued (or
                # before a crash recovery retries it). Refresh at the claim,
                # in the same transaction, so children inherit what it runs.
                cursor.execute("SELECT context_window FROM sessions WHERE id=%s", (row["session_id"],))
                binding = cursor.fetchone()
                if (binding and binding.get("context_window")
                        and row["overrides"].get("context_window") != binding["context_window"]):
                    row["overrides"] = {**row["overrides"], "context_window": binding["context_window"]}
                    cursor.execute("UPDATE turns SET overrides=%s WHERE id=%s",
                                   (json.dumps(row["overrides"]), turn_id))
        except _INTEGRITY_ERRORS:
            conn.rollback()
            return None
    result = _row(row)
    if result is not None:
        _record_session_work(result.get("session_id"))
    return result


def stop_orphaned_turns(
    session_id: str, turn_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Stop stranded live turns, never a session with a valid execution lease.

    The lease guard is part of the UPDATE, not an earlier read. A late Stop
    remains bound to its original turn and cannot stop a successor.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        sql = (
            "UPDATE turns SET state='aborted', stop_reason='interrupted', "
            "interrupt_mode='stop', stop_background=TRUE, ended_at=NOW() "
            "WHERE session_id=%s AND state IN ('running','interrupting','draining') "
            "AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.session_id=turns.session_id "
            "AND r.state='active' AND r.lease_expires_at >= NOW())"
        )
        params = [session_id]
        if turn_id is not None:
            sql += " AND id=%s"
            params.append(turn_id)
        cursor.execute(sql + " RETURNING *", tuple(params))
        stopped = _rows(cursor.fetchall())
    for row in stopped:
        _bank_live_segment(row.get("session_id"), row.get("started_at"), _dead_runner_end(row))
    return stopped


def interrupt_turn(
    session_id: str,
    mode: str,
    stop_background: bool = False,
    turn_id: Optional[str] = None,
    *, source: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Transition a session's ``running`` turn to ``interrupting``.

    ``mode`` is ``'stop'`` or ``'send_now'``; ``stop_background`` records
    whether background children should be torn down. Returns the interrupting
    turn row, or ``None`` if there is no running turn to interrupt.

    When ``turn_id`` is provided the UPDATE also requires ``id=turn_id`` so a
    stop whose target turn is no longer the running one (it settled and a
    successor took the live slot) matches zero rows and returns ``None`` — a
    no-op, never touching the successor. When ``turn_id`` is absent this is
    the legacy session-scoped stop.
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
        if row is not None and source:
            # The UPDATE above owns the row lock. Publish provenance in this
            # same transaction before another runner can observe the interrupt.
            result = _row(row)
            result["payload"]["interrupt_source"] = source
            cursor.execute("UPDATE turns SET payload=%s WHERE id=%s",
                           (json.dumps(result["payload"]), result["id"]))
            return result
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
    run_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Settle a turn to a terminal ``outcome`` (``'completed'`` or
    ``'aborted'``). Idempotent: a turn already in a terminal state
    (completed/aborted/parked) is left untouched and ``None`` is returned.

    ``run_id`` narrows the settle to a turn still bound to that run: a runner
    that lost its lease must not settle a row the reap sweep requeued or a new
    owner started.

    Returns the settled turn row on the transition that actually settles it.
    """
    if outcome not in ("completed", "aborted"):
        raise ValueError(f"settle_turn outcome must be completed|aborted, got {outcome!r}")
    sql = (
        "UPDATE turns SET state=%s, stop_reason=%s, error=%s, ended_at=NOW() "
        "WHERE id=%s AND state NOT IN ('completed','aborted','parked') "
    )
    params: tuple = (outcome, stop_reason, error, turn_id)
    if run_id is not None:
        sql += "AND run_id=%s "
        params += (run_id,)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(sql + "RETURNING *", params)
        row = cursor.fetchone()
    result = _row(row)
    # A queued row settled without ever starting has no interval to bank.
    _bank_ended(result)
    return result


def remove_queued_turn(
    turn_id: str, stop_reason: str = "removed_from_queue",
) -> Optional[Dict[str, Any]]:
    """Atomically abort a still-``queued`` turn (queue-chip removal).

    The ``state='queued'`` guard lives in the UPDATE itself so a turn that
    races to ``running`` between the caller's intent and this statement is
    left alone (``None`` returned) instead of being aborted mid-flight.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE turns SET state='aborted', stop_reason=%s, "
            "ended_at=NOW() "
            "WHERE id=%s AND state='queued' "
            "RETURNING *",
            (stop_reason, turn_id),
        )
        row = cursor.fetchone()
    return _row(row)


def absorb_queued_turn(turn_id: str) -> bool:
    """Retire a still-``queued`` turn whose prompt was delivered mid-stream.

    A queued user message can reach the model without ever running as a turn:
    the live turn's engine accepts it as a fluid ``Steer`` and folds the text
    into the next tool result. Once fluid receipts that delivery the queued row
    must never run again, so it settles ``completed`` with
    ``stop_reason='steered'`` — the same terminal bookkeeping ``settle_turn``
    writes (``ended_at=NOW()``), which keeps the row out of ``pop_ready`` and
    out of the queue view while preserving it as history.

    The ``state='queued'`` guard lives in the UPDATE, so a row that raced to
    live (the drain started it before the receipt landed) is left alone and
    ``False`` is returned. Idempotent by the same guard: a second call after a
    successful absorb matches nothing. Returns True only on the transition that
    actually absorbed the turn.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE turns SET state='completed', stop_reason='steered', "
            "ended_at=NOW() "
            "WHERE id=%s AND state='queued' "
            "RETURNING id",
            (turn_id,),
        )
        return cursor.fetchone() is not None


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
    # Parking ends the foreground interval: the wait on background work is
    # not counted, and the wake turn that follows banks its own interval.
    result = _row(row)
    _bank_ended(result)
    return result


def list_stalled_parked_turns(min_parked_seconds: int) -> List[Dict[str, Any]]:
    """This install's parked turns with NO turn of any kind since the park.

    Feed for the park watchdog (``startup_reconcile.sweep_stalled_parks``). A
    park is supposed to be brief: background work settles, a wake turn is
    enqueued, the session moves. A park with nothing after it is either a
    healthy long wait or a lost wake — indistinguishable from outside, so a
    lost wake can sit unnoticed. The "no turn since" predicate is what makes it a
    STALL and not merely an old row: any wake, user message or queued turn
    after the park excludes the session.

    Returns dicts with ``session_id``, ``turn_id`` and ``parked_seconds``,
    oldest park first.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT t.session_id AS session_id, t.id AS turn_id, "
            "       EXTRACT(EPOCH FROM (NOW() - t.ended_at)) AS parked_seconds "
            "  FROM turns t "
            " WHERE t.state='parked' "
            "   AND t.instance_id=%s "
            "   AND t.ended_at IS NOT NULL "
            "   AND t.ended_at < NOW() - make_interval(secs => %s) "
            "   AND NOT EXISTS ("
            "         SELECT 1 FROM turns t2 "
            "          WHERE t2.session_id = t.session_id "
            "            AND t2.created_at > t.ended_at) "
            " ORDER BY t.ended_at ASC",
            (settings.effective_instance_id, min_parked_seconds),
        )
        return _rows(cursor.fetchall() or [])


def settle_parked_turns(
    session_id: str, exclude_turn_id: Optional[str] = None
) -> int:
    """Settle every ``parked`` turn row for ``session_id`` to ``completed``.

    ``'parked'`` is a TERMINAL state that ``settle_turn`` deliberately refuses
    to move — so once the background work a turn parked on has finished, nothing
    transitions the row out of ``parked`` and ``lifecycle_state.compute`` shows
    the session parked forever. This is the escape hatch: called ONLY when no
    background work remains (a CLI-owned wake has come to rest, or a fresh
    app-turn is starting), it flips the stale parked rows to ``completed``,
    mirroring ``settle_turn``'s column handling (``stop_reason``/``ended_at``).
    Does NOT loosen ``settle_turn``'s own guard.

    ``exclude_turn_id`` protects the turn currently going live (the self-heal
    call site) from settling itself. Returns the number of rows settled.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        if exclude_turn_id is not None:
            cursor.execute(
                "UPDATE turns SET state='completed', "
                "stop_reason=COALESCE(stop_reason, 'parked_settled'), "
                "ended_at=NOW() "
                "WHERE session_id=%s AND state='parked' AND id != %s "
                "RETURNING id",
                (session_id, exclude_turn_id),
            )
        else:
            cursor.execute(
                "UPDATE turns SET state='completed', "
                "stop_reason=COALESCE(stop_reason, 'parked_settled'), "
                "ended_at=NOW() "
                "WHERE session_id=%s AND state='parked' "
                "RETURNING id",
                (session_id,),
            )
        rows = cursor.fetchall()
    return len(rows or [])


def requeue_turn(turn_id: str) -> Optional[Dict[str, Any]]:
    """Return a live turn to the ``queued`` state, releasing the session's live
    slot while KEEPING its ``position`` (so it re-enters at the front, ahead of
    anything queued after it). Clears the run binding. Returns the re-queued
    turn row, or ``None`` if the turn was not live.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT overrides FROM turns WHERE id=%s FOR UPDATE", (turn_id,))
        original = cursor.fetchone()
        overrides = _row(original).get("overrides", {}) if original else {}
        overrides.pop("_permission_explicit", None)
        cursor.execute(
            "UPDATE turns SET state='queued', run_id=NULL, instance_id=NULL, "
            "started_at=NULL, interrupt_mode=NULL, stop_background=FALSE, overrides=%s "
            "WHERE id=%s AND state IN ('running','interrupting','draining') "
            "RETURNING *",
            (json.dumps(overrides), turn_id),
        )
        row = cursor.fetchone()
    result = _row(row)
    if result is not None:
        _record_session_work(result.get("session_id"))
    return result


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


def get_latest_turn(session_id: str) -> Optional[Dict[str, Any]]:
    """Return the session's most recently created turn (any state), or ``None``.

    Used to read the caller's most recent per-turn ``overrides`` (e.g. the
    permission mode the user picked in the composer) when a Task-dispatched
    worker inherits its parent's effective permission level.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM turns WHERE session_id=%s "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (session_id,),
        )
        row = cursor.fetchone()
    return _row(row)


def get_latest_run_turn(session_id: str) -> Optional[Dict[str, Any]]:
    """The session's most recent turn that ran (or will run), or ``None``.

    Like :func:`get_latest_turn`, minus the rows :func:`absorb_queued_turn`
    retired: a message steered into a live turn settles its queue row
    ``completed`` at once, while the turn that read it is still running.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM turns WHERE session_id=%s "
            "AND NOT (state='completed' AND COALESCE(stop_reason, '')='steered') "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
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


def session_has_unsettled_turns(session_id: str) -> bool:
    """True iff the session has ANY turn row outside the terminal states
    (i.e. ``queued``, ``running``, ``interrupting`` or ``draining``) — the
    busy predicate behind ``turn_manager.wait_session_idle``.

    ONE statement on purpose. Two separate reads ("no live turn" via
    :func:`get_live_turn`, then "no queued turns" via :func:`get_queued_turns`),
    each on its own connection/transaction, would race the queued→running
    handoff: ``start_turn`` flips the row out of the second-checked set (queued)
    into the first-checked set (live), so a flip landing between the two reads
    is invisible to both and a waiter declares the session idle while its turn
    is actually running. A single SELECT sees one consistent snapshot, so a turn is always in
    exactly one of the counted states or terminal — there is no in-between to
    fall through.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM turns WHERE session_id=%s "
            "AND state NOT IN ('completed','aborted','parked') LIMIT 1",
            (session_id,),
        )
        return cursor.fetchone() is not None


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


def list_claimable_sessions(session_ids: Optional[List[str]] = None) -> List[str]:
    """Return the DISTINCT session ids that have at least one ``queued`` turn and
    NO live run lease — the candidate set for the cross-runner claim loop.

    The anti-join treats an ``active`` run with a LAPSED lease as absent: a
    crashed owner's stale row must not block another runner from claiming — the
    claimer's ``runs.claim_run`` reaps it atomically on the way in. Sessions
    only (not turns): the claim loop resolves placement per SESSION and kicks
    the scheduler, which orders the queue itself.

    ``session_ids`` (optional) narrows the anti-join to just those sessions.
    The claim loop wants the whole cluster-wide candidate set and passes
    nothing; a read-path caller that only needs "which of THESE rows are
    claimable" (the session-list ``degraded`` derivation, which runs on every
    sidebar poll) passes its page and gets the same answer without scanning
    every queued turn in the database.

    The narrowing is a pure WHERE filter on ``t.session_id``, applied to the
    identical join and predicates, so the result is EXACTLY
    ``set(list_claimable_sessions()) & set(session_ids)`` — never a different
    rule. Duplicates and falsy ids are dropped; an explicitly empty list is
    answered with ``[]`` and no query at all (``IN ()`` is a syntax error in
    Postgres). Passing ``None`` (the default) keeps the full scan.
    """
    sql = (
        "SELECT DISTINCT t.session_id FROM turns t "
        "LEFT JOIN runs r ON r.session_id = t.session_id "
        "AND r.state = 'active' AND r.lease_expires_at >= NOW() "
        "WHERE t.state = 'queued' AND r.id IS NULL"
    )
    params: Optional[tuple] = None
    if session_ids is not None:
        wanted = [sid for sid in dict.fromkeys(session_ids) if sid]
        if not wanted:
            return []
        sql += f" AND t.session_id IN ({phs(len(wanted))})"
        params = tuple(wanted)

    with get_db() as conn:
        cursor = conn.cursor()
        if params is None:
            cursor.execute(sql)
        else:
            cursor.execute(sql, params)
        rows = cursor.fetchall()
    return [r["session_id"] if isinstance(r, dict) else r[0] for r in rows]


def list_interrupting_for_runner(runner_id: str) -> List[Dict[str, Any]]:
    """Return the ``interrupting`` turns this runner is executing (the
    interrupt-observe pass).

    ``instance_id`` on a live turn is stamped exclusively by ``start_turn`` with
    the EXECUTING runner's id, so this filter is exactly "my
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

def reap_expired(skip_turn_ids: Collection[str] = ()) -> List[Dict[str, Any]]:
    """Resolve every live turn whose run lease is dead (run missing, not active,
    or lapsed) — the crash/kill recovery sweep.

    ``skip_turn_ids`` are turns the calling process is still executing. Their
    lease can be dead while the task runs on (it stops itself once the
    heartbeat notices), and requeueing one would let the next claim start it a
    second time. They are left live and not returned.

    A dead-lease live turn is resolved by its ``source`` and ``state``:

    * ``running`` ``user`` / ``queue`` / ``agent_message`` / ``promotion`` turns
      are **re-queued**
      at their original position so accepted messages survive a runner crash;
    * ``interrupting`` / ``draining`` turns are **aborted** regardless of
      source: the user already asked this turn to stop, so retrying it would
      re-run the very prompt they interrupted (replayed user message,
      re-spawned Task workers) on a session the UI shows as stopped;
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
        skip = set(skip_turn_ids)
        dead = [t for t in _rows(cursor.fetchall()) if t["id"] not in skip]
        banked: List[tuple] = []
        for turn in dead:
            if turn["state"] == "running" and turn["source"] in (
                "user", "queue", "agent_message", "promotion",
            ):
                # Recovery is a continuation, not a new owner send. We cannot
                # establish that the old grant is still current after a crash.
                overrides = dict(turn.get("overrides") or {})
                overrides.pop("_permission_explicit", None)
                cursor.execute(
                    "UPDATE turns SET state='queued', run_id=NULL, instance_id=NULL, "
                    "started_at=NULL, interrupt_mode=NULL, stop_background=FALSE, overrides=%s "
                    "WHERE id=%s AND state IN ('running','interrupting','draining')",
                    (json.dumps(overrides), turn["id"]),
                )
                banked.append((turn["session_id"], None))
            else:
                cursor.execute(
                    "UPDATE turns SET state='aborted', stop_reason='lease_expired', "
                    "ended_at=NOW() "
                    "WHERE id=%s AND state IN ('running','interrupting','draining') "
                    "RETURNING started_at, ended_at",
                    (turn["id"],),
                )
                ended = cursor.fetchone()
                banked.append((
                    turn["session_id"],
                    {"started_at": ended["started_at"], "ended_at": ended["ended_at"],
                     "run_id": turn.get("run_id")} if ended else None,
                ))
    for session_id, ended_row in banked:
        if ended_row is None:
            _record_session_work(session_id)
        else:
            _bank_live_segment(
                session_id, ended_row["started_at"], _dead_runner_end(ended_row)
            )
    return dead
