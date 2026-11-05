"""Durable records for Task subagents that run on another worker.

Two worker-local tables (never replicated):

* ``remote_tasks`` (parent side): one row per child this worker sent to
  another worker, written before ``task-start`` is called. A ``task-report``
  is accepted only for a row here, only from its ``target_device_id``, and
  only once (``completion_id`` set). Until then the row is tracked: the
  parent re-attaches it after a restart and checks the target with
  ``task-status``, recording the last contact, since when the target has been
  unreachable (``unreachable_since``) and the child's state as the target last
  reported it (``target_state``). A stop asked for here is durable
  (``stop_requested_at``, ``stop_state`` pending -> sent or refused) and is
  sent again until the target answers it. ``offline_notice_at`` records that
  the caller was told the target had been unreachable too long (S12): sent
  once per run, and a report that still arrives later is accepted as its own
  wake.
* ``remote_task_children`` (target side): one row per child another worker
  started here, created with the child. It goes ``running`` -> ``owed`` (the
  final report is built and stored) -> ``delivered`` (the parent
  acknowledged) or ``refused`` (the parent refused it; not retried).
  ``stop_requested_at`` records that the parent stopped it.

Each function creates its table on first use, so a worker that never runs a
remote subagent pays nothing at boot.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from app.db.base import column_exists, get_db, parse_jsonb, utc_now_iso

_PARENT_DDL = """CREATE TABLE IF NOT EXISTS remote_tasks (
    worker_session_id TEXT PRIMARY KEY,
    target_device_id TEXT NOT NULL,
    owner_session_id TEXT NOT NULL,
    root_session_id TEXT NOT NULL,
    tool_use_id TEXT,
    mode TEXT,
    label TEXT,
    agent TEXT,
    created_at TEXT NOT NULL,
    completion_id TEXT,
    status TEXT,
    report_path TEXT,
    delivered_at TEXT,
    target_name TEXT,
    last_contact_at TEXT,
    unreachable_since TEXT,
    target_state TEXT,
    stop_requested_at TEXT,
    stop_reason TEXT,
    stop_origin TEXT,
    stop_state TEXT,
    stop_error TEXT,
    offline_notice_at TEXT,
    unconfirmed_start_at TEXT,
    published_base TEXT)"""

#: Columns added after the table first shipped: tracking (S7), stop (S8),
#: the notice after a long silence (S12).
_PARENT_ADDED = ("target_name", "last_contact_at", "unreachable_since", "target_state",
                 "stop_requested_at", "stop_reason", "stop_origin", "stop_state", "stop_error",
                 "offline_notice_at", "unconfirmed_start_at", "published_base")

_TARGET_DDL = """CREATE TABLE IF NOT EXISTS remote_task_children (
    worker_session_id TEXT PRIMARY KEY,
    parent_device_id TEXT NOT NULL,
    state TEXT NOT NULL,
    completion_id TEXT,
    report TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    stop_requested_at TEXT)"""

#: Columns added after the table first shipped (S8).
_TARGET_ADDED = ("stop_requested_at", "scope_generation", "scope_epoch", "scope_input_order")

RUNNING = "running"
OWED = "owed"
DELIVERED = "delivered"
REFUSED = "refused"

#: ``remote_tasks.stop_state``: asked here and not yet answered by the target,
#: answered (stopped, already finished or unknown there), or refused by it.
STOP_PENDING = "pending"
STOP_SENT = "sent"
STOP_REFUSED = "refused"
STOP_INCOMPLETE = "incomplete"
STOP_INCOMPLETE_NOTICE = "pending_incomplete"

# Positive, worker-local provenance, written only by a fresh individual Task
# Stop. Missing/unknown values on pre-upgrade requests confer no replay authority.
EXPLICIT_TASK_STOP = "explicit_task_stop_v1"


def init_schema(conn=None) -> None:
    if conn is None:
        with get_db() as connection:
            return init_schema(connection)
    cursor = conn.cursor()
    cursor.execute(_PARENT_DDL)
    cursor.execute(_TARGET_DDL)
    for name in _PARENT_ADDED:
        if not column_exists(cursor, "remote_tasks", name):
            cursor.execute(f"ALTER TABLE remote_tasks ADD COLUMN {name} TEXT")
    for name in _TARGET_ADDED:
        if not column_exists(cursor, "remote_task_children", name):
            cursor.execute(f"ALTER TABLE remote_task_children ADD COLUMN {name} TEXT")


def _row(row) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    data = dict(row)
    if "report" in data:
        data["report"] = parse_jsonb(data["report"])
    return data


# --- parent side -------------------------------------------------------------

def record_dispatch(*, worker_session_id: str, target_device_id: str, owner_session_id: str,
                    root_session_id: str, tool_use_id: Optional[str], mode: str, label: str,
                    agent: str, target_name: Optional[str] = None,
                    published_base: Optional[str] = None) -> None:
    with get_db() as conn:
        init_schema(conn)
        conn.cursor().execute(
            "INSERT INTO remote_tasks (worker_session_id, target_device_id, owner_session_id, "
            "root_session_id, tool_use_id, mode, label, agent, target_name, created_at, published_base) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (worker_session_id) DO NOTHING",
            (worker_session_id, target_device_id, owner_session_id, root_session_id, tool_use_id,
             mode, label, agent, target_name, utc_now_iso(), published_base))


def forget_dispatch(worker_session_id: str) -> None:
    """Drop a record whose child provably never started."""
    with get_db() as conn:
        init_schema(conn)
        conn.cursor().execute(
            "DELETE FROM remote_tasks WHERE worker_session_id=%s AND completion_id IS NULL",
            (worker_session_id,))


def mark_start_unconfirmed(worker_session_id: str) -> None:
    """A Swarm kept this uncertain admission pending; bound its unknown grace."""
    with get_db() as conn:
        init_schema(conn)
        conn.cursor().execute(
            "UPDATE remote_tasks SET unconfirmed_start_at=COALESCE(unconfirmed_start_at, %s) "
            "WHERE worker_session_id=%s AND completion_id IS NULL",
            (utc_now_iso(), worker_session_id))


def get_dispatch(worker_session_id: str) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        init_schema(conn)
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM remote_tasks WHERE worker_session_id=%s", (worker_session_id,))
        return _row(cursor.fetchone())


def mark_delivered(worker_session_id: str, *, completion_id: str, status: str,
                   report_path: Optional[str]) -> bool:
    """Record the report; False when one was already recorded (a duplicate)."""
    with get_db() as conn:
        init_schema(conn)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE remote_tasks SET completion_id=%s, status=%s, report_path=%s, delivered_at=%s "
            "WHERE worker_session_id=%s AND completion_id IS NULL RETURNING worker_session_id",
            (completion_id, status, report_path, utc_now_iso(), worker_session_id))
        return cursor.fetchone() is not None


def tracked_dispatches() -> List[Dict[str, Any]]:
    """Every child sent from here whose report has not arrived yet, oldest first."""
    with get_db() as conn:
        init_schema(conn)
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM remote_tasks WHERE completion_id IS NULL ORDER BY created_at")
        return [_row(row) for row in cursor.fetchall()]


def note_contact(target_states: Dict[str, Optional[str]]) -> None:
    """The target answered for these children: clear ``unreachable_since``.

    ``target_states`` maps a worker session id to the child's state as the
    target reported it (None keeps the last one, for a target that answered
    without one). A known child also confirms a previously uncertain start;
    unknown/missing states must not keep extending that start's grace.
    """
    now = utc_now_iso()
    with get_db() as conn:
        init_schema(conn)
        cursor = conn.cursor()
        for worker_session_id, state in target_states.items():
            cursor.execute(
                "UPDATE remote_tasks SET last_contact_at=%s, unreachable_since=NULL, "
                "target_state=COALESCE(%s, target_state), "
                "unconfirmed_start_at=CASE WHEN %s THEN NULL ELSE unconfirmed_start_at END "
                "WHERE worker_session_id=%s AND completion_id IS NULL",
                (now, state, state in (RUNNING, OWED, DELIVERED, REFUSED), worker_session_id))


def note_unreachable(worker_session_ids: List[str]) -> Dict[str, str]:
    """The target could not be reached: keep the FIRST time it went quiet.

    Returns ``{worker_session_id: unreachable_since}`` for the rows still
    tracked. The time is never moved forward while the target stays quiet, so
    a notice after a fixed silence (S12) can be computed from it.
    """
    now = utc_now_iso()
    since: Dict[str, str] = {}
    with get_db() as conn:
        init_schema(conn)
        cursor = conn.cursor()
        for worker_session_id in worker_session_ids:
            cursor.execute(
                "UPDATE remote_tasks SET unreachable_since=COALESCE(unreachable_since, %s) "
                "WHERE worker_session_id=%s AND completion_id IS NULL "
                "RETURNING worker_session_id, unreachable_since",
                (now, worker_session_id))
            row = cursor.fetchone()
            if row is not None:
                since[row["worker_session_id"]] = row["unreachable_since"]
    return since


def claim_offline_notice(worker_session_id: str, *, unknown_start: bool = False) -> Optional[Dict[str, Any]]:
    """Claim the one notice that the target has been quiet too long.

    Only for a child still owed its report, not stopped here and not noticed
    yet in this run; the claim is atomic, so two passes never both send it.
    Returns the row, or None when there is nothing to notice.
    """
    with get_db() as conn:
        init_schema(conn)
        cursor = conn.cursor()
        guard = "AND unconfirmed_start_at IS NOT NULL AND target_state='unknown' " if unknown_start else ""
        cursor.execute(
            "UPDATE remote_tasks SET offline_notice_at=%s WHERE worker_session_id=%s "
            "AND completion_id IS NULL AND offline_notice_at IS NULL AND stop_requested_at IS NULL "
            + guard + "RETURNING *", (utc_now_iso(), worker_session_id))
        return _row(cursor.fetchone())


def noticed_unstopped(session_id: str) -> List[Dict[str, Any]]:
    """Children of ``session_id`` (as root or caller) past the offline notice.

    Still owed their report and not stopped here yet: the notice took them out
    of the registry and the supervisor, so a Stop on the session finds them
    here (S12). Oldest first.
    """
    with get_db() as conn:
        init_schema(conn)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM remote_tasks WHERE completion_id IS NULL "
            "AND offline_notice_at IS NOT NULL AND stop_requested_at IS NULL "
            "AND (root_session_id=%s OR owner_session_id=%s) ORDER BY created_at",
            (session_id, session_id))
        return [_row(row) for row in cursor.fetchall()]


def request_stop(worker_session_id: str, reason: str, *, explicit_task_stop: bool = False) -> Optional[Dict[str, Any]]:
    """Record that this worker stopped the child: the stop is owed to its target.

    The first request's time and reason stand; a stop the target already
    answered stays answered, anything else goes (back) to pending. Returns the
    row, or None for a child this worker never sent. Only a fresh individual
    Task Stop may set positive replay provenance (including a new gesture after
    an ambiguous old request); ordinary retries never upgrade that authority.
    """
    origin = EXPLICIT_TASK_STOP if explicit_task_stop else None
    with get_db() as conn:
        init_schema(conn)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE remote_tasks SET stop_requested_at=COALESCE(stop_requested_at, %s), "
            "stop_reason=COALESCE(stop_reason, %s), stop_origin=COALESCE(%s, stop_origin), "
            "stop_state=CASE WHEN stop_state=%s THEN stop_state ELSE %s END "
            "WHERE worker_session_id=%s RETURNING *",
            (utc_now_iso(), reason[:200], origin, STOP_SENT, STOP_PENDING, worker_session_id))
        return _row(cursor.fetchone())


def pending_stops() -> List[Dict[str, Any]]:
    """Stops the target has not answered yet, oldest first.

    A child whose report already arrived is at rest there, so its stop is no
    longer owed.
    """
    with get_db() as conn:
        init_schema(conn)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM remote_tasks WHERE stop_state IN (%s,%s) AND completion_id IS NULL "
            "ORDER BY stop_requested_at", (STOP_PENDING, STOP_INCOMPLETE_NOTICE))
        return [_row(row) for row in cursor.fetchall()]


def settle_stop(worker_session_ids: List[str], state: str, *, error: Optional[str] = None) -> None:
    """The target answered these stops (``sent``) or refused them (``refused``)."""
    with get_db() as conn:
        init_schema(conn)
        cursor = conn.cursor()
        for worker_session_id in worker_session_ids:
            cursor.execute(
                "UPDATE remote_tasks SET stop_state=%s, stop_error=%s "
                "WHERE worker_session_id=%s AND stop_state=%s",
                (state, error[:500] if error else None, worker_session_id, STOP_PENDING))


def defer_unscoped_user_stop(worker_id: str, detail: str, *, noted: bool = False) -> bool:
    """Pre-upgrade user Stops lack a boundary; never replay their stable-id RPC.

    Keep the notice pending until a durable transcript note was actually written.
    This is not STOP_SENT and must never be displayed as an ended remote task.
    A fresh individual Stop may have superseded the caller's old snapshot; never
    quarantine that positive authority. Return whether this request was deferred.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE remote_tasks SET stop_state=%s,stop_error=%s "
            "WHERE worker_session_id=%s AND stop_state IN (%s,%s) "
            "AND (stop_origin IS NULL OR stop_origin<>%s) RETURNING worker_session_id",
            (STOP_INCOMPLETE if noted else STOP_INCOMPLETE_NOTICE, detail[:500], worker_id,
             STOP_PENDING, STOP_INCOMPLETE_NOTICE, EXPLICIT_TASK_STOP))
        return cursor.fetchone() is not None


def record_scoped_stop(worker_id: str, reason: str) -> None:
    """Positive receiver enforcement: atomically settle WITHOUT a legacy retry."""
    with get_db() as conn:
        conn.cursor().execute("UPDATE remote_tasks SET stop_requested_at=COALESCE(stop_requested_at,%s), "
            "stop_reason=%s,stop_state=%s,stop_error=NULL WHERE worker_session_id=%s",
            (utc_now_iso(), reason[:200], STOP_SENT, worker_id))


def note_stop_failed(worker_session_ids: List[str], error: str) -> None:
    """The target could not be reached: the stops stay pending, with why."""
    with get_db() as conn:
        init_schema(conn)
        cursor = conn.cursor()
        for worker_session_id in worker_session_ids:
            cursor.execute(
                "UPDATE remote_tasks SET stop_error=%s WHERE worker_session_id=%s AND stop_state=%s",
                (error[:500], worker_session_id, STOP_PENDING))


# --- target side -------------------------------------------------------------

def record_child(worker_session_id: str, parent_device_id: str, *, conn, input_order: Optional[int] = None) -> None:
    """Called inside the transaction that creates the child."""
    init_schema(conn)
    now = utc_now_iso()
    conn.cursor().execute(
        "INSERT INTO remote_task_children (worker_session_id, parent_device_id, state, created_at, "
        "updated_at, scope_generation, scope_epoch, scope_input_order) "
        "VALUES (%s, %s, %s, %s, %s, %s, '1', %s) ON CONFLICT (worker_session_id) DO NOTHING",
        (worker_session_id, parent_device_id, RUNNING, now, now,
         worker_session_id if input_order is not None else None, str(input_order) if input_order is not None else None))


def get_child(worker_session_id: str) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        init_schema(conn)
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM remote_task_children WHERE worker_session_id=%s",
                       (worker_session_id,))
        return _row(cursor.fetchone())


def children_for(worker_session_ids: List[str], parent_device_id: str) -> Dict[str, Dict[str, Any]]:
    """This worker's rows for ``worker_session_ids`` that ``parent_device_id`` started."""
    if not worker_session_ids:
        return {}
    with get_db() as conn:
        init_schema(conn)
        cursor = conn.cursor()
        placeholders = ", ".join(["%s"] * len(worker_session_ids))
        cursor.execute(
            f"SELECT worker_session_id, state, completion_id FROM remote_task_children "
            f"WHERE parent_device_id=%s AND worker_session_id IN ({placeholders})",
            (parent_device_id, *worker_session_ids))
        return {row["worker_session_id"]: dict(row) for row in cursor.fetchall()}


def children_by_id(worker_session_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """This worker's rows for ``worker_session_ids``, whichever worker started them."""
    if not worker_session_ids:
        return {}
    with get_db() as conn:
        init_schema(conn)
        cursor = conn.cursor()
        placeholders = ", ".join(["%s"] * len(worker_session_ids))
        cursor.execute(
            f"SELECT worker_session_id, parent_device_id, state, stop_requested_at "
            f"FROM remote_task_children WHERE worker_session_id IN ({placeholders})",
            tuple(worker_session_ids))
        return {row["worker_session_id"]: dict(row) for row in cursor.fetchall()}


def mark_child_stopped(worker_session_id: str) -> None:
    """The parent stopped this child: keep the first time it asked."""
    with get_db() as conn:
        init_schema(conn)
        now = utc_now_iso()
        conn.cursor().execute(
            "UPDATE remote_task_children SET stop_requested_at=COALESCE(stop_requested_at, %s), "
            "updated_at=%s WHERE worker_session_id=%s", (now, now, worker_session_id))


def running_children() -> List[str]:
    with get_db() as conn:
        init_schema(conn)
        cursor = conn.cursor()
        cursor.execute("SELECT worker_session_id FROM remote_task_children WHERE state=%s", (RUNNING,))
        return [row["worker_session_id"] for row in cursor.fetchall()]


def owe_report(worker_session_id: str, completion_id: str, report: Dict[str, Any]) -> bool:
    """Store the final report once; False when it was already stored."""
    with get_db() as conn:
        init_schema(conn)
        cursor = conn.cursor()
        now = utc_now_iso()
        cursor.execute(
            "UPDATE remote_task_children SET state=%s, completion_id=%s, report=%s, "
            "next_attempt_at=%s, updated_at=%s WHERE worker_session_id=%s AND state=%s "
            "RETURNING worker_session_id",
            (OWED, completion_id, json.dumps(report), now, now, worker_session_id, RUNNING))
        return cursor.fetchone() is not None


def replace_owed_report(worker_session_id: str, old_completion_id: Optional[str],
                        completion_id: str, report: Dict[str, Any]) -> bool:
    """A report not sent yet is replaced by the child's newer one (#1812)."""
    with get_db() as conn:
        init_schema(conn)
        cursor = conn.cursor()
        now = utc_now_iso()
        cursor.execute(
            "UPDATE remote_task_children SET completion_id=%s, report=%s, attempts=0, "
            "next_attempt_at=%s, last_error=NULL, updated_at=%s "
            "WHERE worker_session_id=%s AND state=%s AND completion_id=%s RETURNING worker_session_id",
            (completion_id, json.dumps(report), now, now, worker_session_id, OWED, old_completion_id))
        return cursor.fetchone() is not None


def reowe_report(worker_session_id: str, state: str, completion_id: str,
                 report: Dict[str, Any]) -> bool:
    """A child ran again after its report was delivered or refused: owe the new one."""
    with get_db() as conn:
        init_schema(conn)
        cursor = conn.cursor()
        now = utc_now_iso()
        cursor.execute(
            "UPDATE remote_task_children SET state=%s, completion_id=%s, report=%s, attempts=0, "
            "next_attempt_at=%s, last_error=NULL, updated_at=%s "
            "WHERE worker_session_id=%s AND state=%s RETURNING worker_session_id",
            (OWED, completion_id, json.dumps(report), now, now, worker_session_id, state))
        return cursor.fetchone() is not None


def due_reports(now: Optional[str] = None) -> List[Dict[str, Any]]:
    with get_db() as conn:
        init_schema(conn)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM remote_task_children WHERE state=%s AND "
            "(next_attempt_at IS NULL OR next_attempt_at <= %s) ORDER BY created_at",
            (OWED, now or utc_now_iso()))
        return [_row(row) for row in cursor.fetchall()]


def settle_report(worker_session_id: str, state: str, *, error: Optional[str] = None,
                  completion_id: Optional[str] = None) -> None:
    """``completion_id``: settle only that report, not a newer one that replaced it."""
    with get_db() as conn:
        init_schema(conn)
        if completion_id is None:
            conn.cursor().execute(
                "UPDATE remote_task_children SET state=%s, last_error=%s, updated_at=%s "
                "WHERE worker_session_id=%s AND state=%s",
                (state, error, utc_now_iso(), worker_session_id, OWED))
        else:
            conn.cursor().execute(
                "UPDATE remote_task_children SET state=%s, last_error=%s, updated_at=%s "
                "WHERE worker_session_id=%s AND state=%s AND completion_id=%s",
                (state, error, utc_now_iso(), worker_session_id, OWED, completion_id))


def retry_report_later(worker_session_id: str, *, next_attempt_at: str, error: str) -> None:
    with get_db() as conn:
        init_schema(conn)
        conn.cursor().execute(
            "UPDATE remote_task_children SET attempts=attempts+1, next_attempt_at=%s, last_error=%s, "
            "updated_at=%s WHERE worker_session_id=%s AND state=%s",
            (next_attempt_at, error[:500], utc_now_iso(), worker_session_id, OWED))


# --- messages and resumes (S9) -------------------------------------------------
#
# Two more worker-local tables, created on first use like the ones above:
#
# * ``remote_task_messages`` (both sides): one row per ``task-message`` this
#   worker applied, keyed by the sender's message id, with the answer it gave.
#   A retried message gets the same answer back and is never applied twice.
# * ``remote_task_completions`` (parent side): the reports of a child that
#   were already delivered before a message resumed it. A late retry of one of
#   them is a duplicate, never a new report.

_MESSAGES_DDL = """CREATE TABLE IF NOT EXISTS remote_task_messages (
    message_id TEXT PRIMARY KEY,
    worker_session_id TEXT NOT NULL,
    from_device_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    answer TEXT NOT NULL,
    resume_turn_id TEXT,
    created_at TEXT NOT NULL)"""

_COMPLETIONS_DDL = """CREATE TABLE IF NOT EXISTS remote_task_completions (
    worker_session_id TEXT NOT NULL,
    completion_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (worker_session_id, completion_id))"""


def _init_messaging(conn) -> None:
    init_schema(conn)
    cursor = conn.cursor()
    cursor.execute(_MESSAGES_DDL)
    cursor.execute(_COMPLETIONS_DDL)


def message_answer(message_id: str) -> Optional[Dict[str, Any]]:
    """The stored row for an applied message, or None."""
    with get_db() as conn:
        _init_messaging(conn)
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM remote_task_messages WHERE message_id=%s", (message_id,))
        row = cursor.fetchone()
    if row is None:
        return None
    data = dict(row)
    data["answer"] = parse_jsonb(data["answer"])
    return data


def reserve_message_scope(message_id: str, worker_id: str, from_device: str) -> dict:
    return _reserve_message_scope(message_id, worker_id, from_device, undoable=False)[0]


def reserve_message_scope_for_delivery(message_id: str, worker_id: str, from_device: str) -> tuple[dict, Optional[dict]]:
    """Also return this call's fresh reservation proof, never a pending retry's."""
    return _reserve_message_scope(message_id, worker_id, from_device, undoable=True)


def _reserve_message_scope(message_id: str, worker_id: str, from_device: str, *, undoable: bool) -> tuple[dict, Optional[dict]]:
    """Fence a new remote instruction BEFORE delivery, without replaying old intent.

    A pending intent survives restart. Its retry may continue only while it is
    still the child's current generation; a newer instruction cannot be replaced
    by retrying that old id. Final delivery replaces this pending answer.
    """
    from uuid import uuid4

    pending: dict = {"_scope_pending": True}
    if undoable:
        pending["_scope_reservation"] = uuid4().hex
    with get_db() as conn:
        _init_messaging(conn)
        cursor = conn.cursor()
        # Same admission lock as enqueue_turn: capture the previous tuple and
        # the reserved input cutoff without racing an admitted local input.
        cursor.execute("SELECT id FROM sessions WHERE id=%s FOR UPDATE", (worker_id,))
        cursor.execute("INSERT INTO remote_task_messages "
                       "(message_id,worker_session_id,from_device_id,direction,answer,created_at) "
                       "VALUES (%s,%s,%s,'child',%s,%s) ON CONFLICT(message_id) DO NOTHING RETURNING message_id",
                       (message_id, worker_id, from_device, json.dumps(pending), utc_now_iso()))
        fresh = cursor.fetchone() is not None
        previous = None
        if fresh:
            cursor.execute("SELECT scope_generation,scope_epoch,scope_input_order, "
                           "COALESCE((SELECT stop_seq FROM session_stops WHERE session_id=%s),0) AS stop_seq "
                           "FROM remote_task_children WHERE worker_session_id=%s AND parent_device_id=%s "
                           "AND stop_requested_at IS NULL FOR UPDATE", (worker_id, worker_id, from_device))
            previous = cursor.fetchone()
            cursor.execute("UPDATE remote_task_children SET scope_generation=%s, "
                           "scope_epoch=CAST(COALESCE(CAST(scope_epoch AS BIGINT),0)+1 AS TEXT), "
                           "scope_input_order=CAST((SELECT COALESCE(MAX(insert_order),0) FROM turns WHERE session_id=%s) AS TEXT) "
                           "WHERE worker_session_id=%s AND parent_device_id=%s AND stop_requested_at IS NULL "
                           "RETURNING scope_generation,scope_epoch,scope_input_order", (message_id, worker_id, worker_id, from_device))
        else:
            # A retry may already have delivered to a live engine without a new
            # turn row. Revoke the first caller's undo proof BEFORE it can do so.
            cursor.execute("UPDATE remote_task_messages SET answer=%s WHERE message_id=%s "
                           "AND worker_session_id=%s AND from_device_id=%s AND direction='child' "
                           "AND CAST(answer AS JSONB) ->> '_scope_pending' IS NOT NULL RETURNING message_id",
                           (json.dumps({"_scope_pending": True}), message_id, worker_id, from_device))
            if cursor.fetchone() is None:
                raise ValueError("This message id was already used for another message.")
            cursor.execute("SELECT scope_generation,scope_epoch,scope_input_order FROM remote_task_children "
                           "WHERE worker_session_id=%s AND parent_device_id=%s AND scope_generation=%s "
                           "AND stop_requested_at IS NULL", (worker_id, from_device, message_id))
        row = cursor.fetchone()
        if not row:
            raise ValueError("The child was stopped or this pending instruction was superseded; it was not delivered.")
        scope = {"generation": row["scope_generation"], "epoch": int(row["scope_epoch"])}
        undo = ({"message_id": message_id, "worker_id": worker_id, "from_device": from_device,
                 "pending": pending, "previous": dict(previous), "reserved": dict(row)}
                if fresh and undoable else None)
        return scope, undo


def undo_refused_message_scope(reservation: dict) -> bool:
    """Undo ONLY a fresh, unused reservation after definite pre-enqueue refusal.

    A retry, finalized answer, Stop, newer generation or admitted input makes
    the proof unusable. Never undo uncertain delivery or a recovered pending id.
    """
    worker_id, message_id = reservation["worker_id"], reservation["message_id"]
    previous, reserved = reservation["previous"], reservation["reserved"]
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM sessions WHERE id=%s FOR UPDATE", (worker_id,))
        cursor.execute("SELECT message_id FROM remote_task_messages WHERE message_id=%s "
                       "AND worker_session_id=%s AND from_device_id=%s AND direction='child' "
                       "AND CAST(answer AS JSONB)=%s::jsonb FOR UPDATE",
                       (message_id, worker_id, reservation["from_device"], json.dumps(reservation["pending"])))
        if cursor.fetchone() is None:
            return False
        cursor.execute("UPDATE remote_task_children SET scope_generation=%s,scope_epoch=%s,scope_input_order=%s "
                       "WHERE worker_session_id=%s AND parent_device_id=%s AND stop_requested_at IS NULL "
                       "AND scope_generation=%s AND scope_epoch=%s AND scope_input_order=%s "
                       "AND COALESCE((SELECT stop_seq FROM session_stops WHERE session_id=%s),0)=%s "
                       "AND NOT EXISTS (SELECT 1 FROM turns WHERE session_id=%s AND insert_order>%s) "
                       "RETURNING worker_session_id",
                       (previous["scope_generation"], previous["scope_epoch"], previous["scope_input_order"],
                        worker_id, reservation["from_device"], reserved["scope_generation"], reserved["scope_epoch"],
                        reserved["scope_input_order"], worker_id, previous["stop_seq"], worker_id,
                        int(reserved["scope_input_order"])))
        if cursor.fetchone() is None:
            return False
        # Delete just our refused intent, in the same transaction as the CAS.
        # The same id can retry honestly; it is not a fabricated delivery answer.
        cursor.execute("DELETE FROM remote_task_messages WHERE message_id=%s", (message_id,))
        return True


def record_message(message_id: str, *, worker_session_id: str, from_device_id: str,
                   direction: str, answer: Dict[str, Any],
                   resume_turn_id: Optional[str] = None) -> bool:
    """Remember an applied message and its answer; False when already recorded."""
    with get_db() as conn:
        _init_messaging(conn)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO remote_task_messages (message_id, worker_session_id, from_device_id, "
            "direction, answer, resume_turn_id, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (message_id) DO UPDATE SET answer=EXCLUDED.answer, resume_turn_id=EXCLUDED.resume_turn_id "
            "WHERE CAST(remote_task_messages.answer AS JSONB) ->> '_scope_pending' IS NOT NULL "
            "AND remote_task_messages.worker_session_id=EXCLUDED.worker_session_id "
            "AND remote_task_messages.from_device_id=EXCLUDED.from_device_id "
            "AND remote_task_messages.direction=EXCLUDED.direction RETURNING message_id",
            (message_id, worker_session_id, from_device_id, direction, json.dumps(answer),
             resume_turn_id, utc_now_iso()))
        return cursor.fetchone() is not None


def latest_resume_turn(worker_session_id: str) -> Optional[str]:
    """Target side: the turn the newest message-resume of this child queued."""
    with get_db() as conn:
        _init_messaging(conn)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT resume_turn_id FROM remote_task_messages WHERE worker_session_id=%s "
            "AND resume_turn_id IS NOT NULL ORDER BY created_at DESC LIMIT 1",
            (worker_session_id,))
        row = cursor.fetchone()
    return row["resume_turn_id"] if row else None


def resume_child(worker_session_id: str, parent_device_id: str,
                 known_completion_id: Optional[str]) -> bool:
    """Target side: a message resumed a finished child, so a new report is owed.

    Back to ``running`` from ``delivered`` or ``refused``, or from ``owed``
    when the parent already has that report (its acknowledgement was lost).
    False when the row is in none of those states.
    """
    with get_db() as conn:
        _init_messaging(conn)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE remote_task_children SET state=%s, completion_id=NULL, report=NULL, attempts=0, "
            "next_attempt_at=NULL, last_error=NULL, updated_at=%s "
            "WHERE worker_session_id=%s AND parent_device_id=%s AND "
            "(state IN (%s, %s) OR (state=%s AND completion_id=%s)) RETURNING worker_session_id",
            (RUNNING, utc_now_iso(), worker_session_id, parent_device_id, DELIVERED, REFUSED,
             OWED, known_completion_id))
        return cursor.fetchone() is not None


def reopen_dispatch(worker_session_id: str, completion_id: str) -> bool:
    """Parent side: the child was resumed after the report ``completion_id``.

    That report moves to ``remote_task_completions`` and the row is tracked
    again until the next one; the resumed run may get its own offline notice. False when the row no longer carries that
    report (already reopened, or a newer report already arrived).
    """
    with get_db() as conn:
        _init_messaging(conn)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE remote_tasks SET completion_id=NULL, status=NULL, report_path=NULL, "
            "delivered_at=NULL, unreachable_since=NULL, target_state=NULL, offline_notice_at=NULL, "
            "unconfirmed_start_at=NULL "
            "WHERE worker_session_id=%s AND completion_id=%s RETURNING worker_session_id",
            (worker_session_id, completion_id))
        if cursor.fetchone() is None:
            return False
        cursor.execute(
            "INSERT INTO remote_task_completions (worker_session_id, completion_id, created_at) "
            "VALUES (%s, %s, %s) ON CONFLICT (worker_session_id, completion_id) DO NOTHING",
            (worker_session_id, completion_id, utc_now_iso()))
        return True


def past_completions(worker_session_id: str) -> List[str]:
    """Parent side: reports of this child delivered before a resume, oldest first."""
    with get_db() as conn:
        _init_messaging(conn)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT completion_id FROM remote_task_completions WHERE worker_session_id=%s "
            "ORDER BY created_at", (worker_session_id,))
        return [row["completion_id"] for row in cursor.fetchall()]
