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
    stop_state TEXT,
    stop_error TEXT,
    offline_notice_at TEXT)"""

#: Columns added after the table first shipped: tracking (S7), stop (S8),
#: the notice after a long silence (S12).
_PARENT_ADDED = ("target_name", "last_contact_at", "unreachable_since", "target_state",
                 "stop_requested_at", "stop_reason", "stop_state", "stop_error",
                 "offline_notice_at")

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
_TARGET_ADDED = ("stop_requested_at",)

RUNNING = "running"
OWED = "owed"
DELIVERED = "delivered"
REFUSED = "refused"

#: ``remote_tasks.stop_state``: asked here and not yet answered by the target,
#: answered (stopped, already finished or unknown there), or refused by it.
STOP_PENDING = "pending"
STOP_SENT = "sent"
STOP_REFUSED = "refused"


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
                    agent: str, target_name: Optional[str] = None) -> None:
    with get_db() as conn:
        init_schema(conn)
        conn.cursor().execute(
            "INSERT INTO remote_tasks (worker_session_id, target_device_id, owner_session_id, "
            "root_session_id, tool_use_id, mode, label, agent, target_name, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (worker_session_id) DO NOTHING",
            (worker_session_id, target_device_id, owner_session_id, root_session_id, tool_use_id,
             mode, label, agent, target_name, utc_now_iso()))


def forget_dispatch(worker_session_id: str) -> None:
    """Drop a record whose child provably never started."""
    with get_db() as conn:
        init_schema(conn)
        conn.cursor().execute(
            "DELETE FROM remote_tasks WHERE worker_session_id=%s AND completion_id IS NULL",
            (worker_session_id,))


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
    without one).
    """
    now = utc_now_iso()
    with get_db() as conn:
        init_schema(conn)
        cursor = conn.cursor()
        for worker_session_id, state in target_states.items():
            cursor.execute(
                "UPDATE remote_tasks SET last_contact_at=%s, unreachable_since=NULL, "
                "target_state=COALESCE(%s, target_state) "
                "WHERE worker_session_id=%s AND completion_id IS NULL",
                (now, state, worker_session_id))


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


def claim_offline_notice(worker_session_id: str) -> Optional[Dict[str, Any]]:
    """Claim the one notice that the target has been quiet too long.

    Only for a child still owed its report, not stopped here and not noticed
    yet in this run; the claim is atomic, so two passes never both send it.
    Returns the row, or None when there is nothing to notice.
    """
    with get_db() as conn:
        init_schema(conn)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE remote_tasks SET offline_notice_at=%s WHERE worker_session_id=%s "
            "AND completion_id IS NULL AND offline_notice_at IS NULL AND stop_requested_at IS NULL "
            "RETURNING *", (utc_now_iso(), worker_session_id))
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


def request_stop(worker_session_id: str, reason: str) -> Optional[Dict[str, Any]]:
    """Record that this worker stopped the child: the stop is owed to its target.

    The first request's time and reason stand; a stop the target already
    answered stays answered, anything else goes (back) to pending. Returns the
    row, or None for a child this worker never sent.
    """
    with get_db() as conn:
        init_schema(conn)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE remote_tasks SET stop_requested_at=COALESCE(stop_requested_at, %s), "
            "stop_reason=COALESCE(stop_reason, %s), "
            "stop_state=CASE WHEN stop_state=%s THEN stop_state ELSE %s END "
            "WHERE worker_session_id=%s RETURNING *",
            (utc_now_iso(), reason[:200], STOP_SENT, STOP_PENDING, worker_session_id))
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
            "SELECT * FROM remote_tasks WHERE stop_state=%s AND completion_id IS NULL "
            "ORDER BY stop_requested_at", (STOP_PENDING,))
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

def record_child(worker_session_id: str, parent_device_id: str, *, conn) -> None:
    """Called inside the transaction that creates the child."""
    init_schema(conn)
    now = utc_now_iso()
    conn.cursor().execute(
        "INSERT INTO remote_task_children (worker_session_id, parent_device_id, state, created_at, "
        "updated_at) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (worker_session_id) DO NOTHING",
        (worker_session_id, parent_device_id, RUNNING, now, now))


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
            "ON CONFLICT (message_id) DO NOTHING RETURNING message_id",
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
            "delivered_at=NULL, unreachable_since=NULL, target_state=NULL, offline_notice_at=NULL "
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
