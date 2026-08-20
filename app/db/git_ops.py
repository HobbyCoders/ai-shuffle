"""The ``git_ops`` table — the durable cross-install git-operation queue (D5).

A *git op* is one unit of git work targeted at ONE install (``runner_id`` = that
install's ``effective_instance_id``). It exists because git state lives on a
specific machine's disk and there is no device-to-device HTTP in this cluster:
the install serving the UI writes a ``queued`` row into the shared database, and
the TARGET install's claim loop (``app.core.git_ops_runner``) notices it, claims
it, executes it locally, and records the outcome.

Claiming is a single conditional UPDATE (``queued`` -> ``running`` guarded on
id + runner_id + status), so concurrency is resolved by the database exactly
like ``app.db.turns`` / ``app.db.runs``: two racers both try, one gets the row
back, the other gets ``None`` and moves on. There are no in-memory locks.

Known ops: ``ensure_worktree`` (``params`` carry
``{branch, base_branch, create_new_branch}`` — the DURABLE CARRIER of
create-new-branch intent, which is why the spawn path
(:mod:`app.core.query_options`) claims and runs a matching queued op INLINE
before failing a branch session) and ``refresh_status`` (stamp ``head_sha``/
``dirty`` on this runner's checkouts; omitted ``branch`` = every active
checkout of the project on this runner). An unknown ``op`` is failed, never
silently dropped.

This module is a PURE DB layer: plain functions over ``get_db()``, PostgreSQL
SQL that the test pg_shim translates to SQLite.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.db.base import get_db

logger = logging.getLogger(__name__)

# Terminal statuses — a claim never touches these.
TERMINAL_STATUSES = ("done", "error")

# A ``running`` row older than this is treated as a crashed-loop zombie, not a
# live ``git worktree add``. The executor heartbeats ``updated_at`` at start so
# a long add is not skipped. Metadata GET must never fail these; cleanup lives
# only in ``ensure_pending_checkout`` and only for this runner.
STALE_RUNNING_SECONDS = 15 * 60


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
    if row is None:
        return None
    d = dict(row)
    d["params"] = _parse_json(d.get("params")) or {}
    d["result"] = _parse_json(d.get("result"))
    return d


def _rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [d for r in rows if (d := _row(r)) is not None]


def _as_aware_utc(value: Any) -> Optional[datetime]:
    """Coerce a DB timestamp (datetime or ISO string, naive or aware) to UTC.

    The SQLite test shim stores ``CURRENT_TIMESTAMP`` as a naive string;
    Postgres returns aware datetimes. Missing/unparseable → None.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _is_stale_running(row: Dict[str, Any]) -> bool:
    if row.get("status") != "running":
        return False
    updated = _as_aware_utc(row.get("updated_at"))
    if updated is None:
        return True
    age = (datetime.now(timezone.utc) - updated).total_seconds()
    return age > STALE_RUNNING_SECONDS


def _matching_inflight(
    runner_id: str,
    project_v2_id: Optional[str],
    op: str,
    branch: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Queued + running rows for (runner, op[, project, branch]), oldest first."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM git_ops "
            "WHERE runner_id = %s AND op = %s AND status IN ('queued', 'running') "
            "ORDER BY created_at ASC, id ASC",
            (runner_id, op),
        )
        candidates = _rows(cursor.fetchall())
    matched: List[Dict[str, Any]] = []
    for row in candidates:
        if project_v2_id is not None and row.get("project_v2_id") != project_v2_id:
            continue
        if branch is not None and (row.get("params") or {}).get("branch") != branch:
            continue
        matched.append(row)
    return matched


def insert_op(
    runner_id: str,
    op: str,
    *,
    project_v2_id: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Enqueue a ``queued`` op targeted at ``runner_id``. Returns the new row."""
    op_id = f"gitop-{uuid.uuid4().hex}"
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO git_ops (id, runner_id, project_v2_id, op, params, status) "
            "VALUES (%s, %s, %s, %s, %s, 'queued') "
            "RETURNING *",
            (op_id, runner_id, project_v2_id, op, json.dumps(params or {})),
        )
        row = _row(cursor.fetchone())
    if row is None:  # INSERT ... RETURNING always yields the row
        raise RuntimeError(f"insert_op: no row returned for {op!r} -> {runner_id!r}")
    return row


def get_op(op_id: str) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM git_ops WHERE id = %s", (op_id,))
        return _row(cursor.fetchone())


def claim_op(op_id: str, runner_id: str) -> Optional[Dict[str, Any]]:
    """Atomically take ownership of a queued op: ``queued`` -> ``running``.

    The whole guard lives inside ONE conditional UPDATE (id + runner_id +
    ``status = 'queued'``), so the database resolves the race: exactly one
    caller gets the row back, every loser gets ``None``. This is what lets the
    claim loop and the spawn-path inline claim compete for the same op safely.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE git_ops SET status = 'running', updated_at = NOW() "
            "WHERE id = %s AND runner_id = %s AND status = 'queued' "
            "RETURNING *",
            (op_id, runner_id),
        )
        return _row(cursor.fetchone())


def complete_op(op_id: str, result: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Settle a running op as ``done`` with its ``result`` payload."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE git_ops SET status = 'done', result = %s, updated_at = NOW() "
            "WHERE id = %s "
            "RETURNING *",
            (json.dumps(result or {}), op_id),
        )
        return _row(cursor.fetchone())


def fail_op(op_id: str, error: str) -> Optional[Dict[str, Any]]:
    """Settle a running op as ``error``, recording the message in ``result``."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE git_ops SET status = 'error', result = %s, updated_at = NOW() "
            "WHERE id = %s "
            "RETURNING *",
            (json.dumps({"error": str(error)}), op_id),
        )
        return _row(cursor.fetchone())


def touch_op(op_id: str) -> Optional[Dict[str, Any]]:
    """Heartbeat ``updated_at`` so a live executor is not treated as stale."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE git_ops SET updated_at = NOW() WHERE id = %s RETURNING *",
            (op_id,),
        )
        return _row(cursor.fetchone())


def list_queued_for_runner(runner_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    """Queued ops targeted at ``runner_id``, oldest first (the loop's read)."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM git_ops WHERE runner_id = %s AND status = 'queued' "
            "ORDER BY created_at ASC, id ASC LIMIT %s",
            (runner_id, limit),
        )
        return _rows(cursor.fetchall())


def find_queued(
    runner_id: str,
    project_v2_id: Optional[str],
    op: str,
    branch: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Find the oldest queued op matching (runner, project, op[, params.branch]).

    ``branch`` is matched in Python against ``params['branch']`` rather than in
    SQL: JSONB containment has no SQLite equivalent in the test shim, and the
    per-runner queued set is tiny (an install has at most a handful of pending
    checkouts). Returns ``None`` when nothing matches.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM git_ops "
            "WHERE runner_id = %s AND op = %s AND status = 'queued' "
            "ORDER BY created_at ASC, id ASC",
            (runner_id, op),
        )
        candidates = _rows(cursor.fetchall())
    for row in candidates:
        if project_v2_id is not None and row.get("project_v2_id") != project_v2_id:
            continue
        if branch is not None and (row.get("params") or {}).get("branch") != branch:
            continue
        return row
    return None


def find_matching(
    runner_id: str,
    project_v2_id: Optional[str],
    op: str,
    branch: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Oldest queued or fresh-running match. Pure read — never writes.

    Stale ``running`` (``updated_at`` older than ``STALE_RUNNING_SECONDS``) is
    skipped so a crashed-loop zombie cannot hide a newer queued retry. Callers
    that need to fail those zombies (spawn preflight on this runner) do so
    themselves; metadata GET must not.
    """
    for row in _matching_inflight(runner_id, project_v2_id, op, branch):
        if _is_stale_running(row):
            continue
        return row
    return None


def list_stale_running(
    runner_id: str,
    project_v2_id: Optional[str],
    op: str,
    branch: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Zombie ``running`` rows ``find_matching`` skipped. Pure read."""
    return [
        row
        for row in _matching_inflight(runner_id, project_v2_id, op, branch)
        if _is_stale_running(row)
    ]


def list_recent_for_project(
    project_v2_id: str, limit: int = 20
) -> List[Dict[str, Any]]:
    """Recent ops for a project across every runner (git-state visibility, D6)."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM git_ops WHERE project_v2_id = %s "
            "ORDER BY created_at DESC LIMIT %s",
            (project_v2_id, limit),
        )
        return _rows(cursor.fetchall())


def purge_terminal(older_than_days: int = 7) -> int:
    """Delete ``done``/``error`` rows older than ``older_than_days``.

    Ops are transport, not history: once settled and read, the row is dead
    weight. In-flight (``queued``/``running``) rows are NEVER swept. Returns the
    number of rows removed.
    """
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM git_ops WHERE status IN ('done', 'error') AND updated_at < %s",
            (cutoff,),
        )
        return cursor.rowcount or 0
