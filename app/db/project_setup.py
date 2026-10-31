"""Cross-worker project setup: the requesting worker's durable records.

Two small tables, both local to this worker (never replicated):

``project_setup_requests``
    One row per setup this worker asked ANOTHER worker to run. A request for
    an offline worker waits here as ``queued_offline`` until that worker is
    back, then is dispatched (``dispatched``). A request the peer refused
    before queueing it (an older worker) is ``error``; the owner can withdraw
    a waiting request (``cancelled``). The setup itself runs as a durable
    ``git_ops`` row (op ``setup_project``) on the TARGET worker, keyed by the
    same id, so the requester reads its live status from there.

``project_peer_catalogs``
    The last project catalog (the projects a peer reported it can run) read
    from each peer. Legacy ``projects`` rows do not replicate between network
    workers, so this is how an offline worker is still shown as having (or
    not having) a project.

PostgreSQL SQL that the test pg_shim translates to SQLite.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from app.db.base import get_db, parse_jsonb

REQUEST_QUEUED_OFFLINE = "queued_offline"
REQUEST_DISPATCHED = "dispatched"
REQUEST_ERROR = "error"
REQUEST_CANCELLED = "cancelled"


def create_schema(cursor) -> None:
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS project_setup_requests (
            id TEXT PRIMARY KEY,
            project_v2_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            params JSONB NOT NULL DEFAULT '{}',
            status TEXT NOT NULL,
            error_code TEXT,
            error TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_project_setup_requests_status "
        "ON project_setup_requests(status)"
    )
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS project_peer_catalogs (
            device_id TEXT PRIMARY KEY,
            project_ids JSONB NOT NULL DEFAULT '[]',
            read_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)


def _row(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    out = dict(row)
    out["params"] = parse_jsonb(out.get("params")) or {}
    return out


def insert_request(op_id: str, project_v2_id: str, target_id: str, params: Dict[str, Any],
                   status: str, *, error_code: Optional[str] = None,
                   error: Optional[str] = None) -> Dict[str, Any]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO project_setup_requests "
            "(id, project_v2_id, target_id, params, status, error_code, error) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, "
            "error_code = EXCLUDED.error_code, error = EXCLUDED.error, updated_at = NOW() "
            "RETURNING *",
            (op_id, project_v2_id, target_id, json.dumps(params or {}), status, error_code, error),
        )
        return _row(cursor.fetchone())


def get_request(op_id: str) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM project_setup_requests WHERE id = %s", (op_id,))
        return _row(cursor.fetchone())


def set_request_status(op_id: str, status: str, *, expect: Optional[str] = None,
                       error_code: Optional[str] = None,
                       error: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Move a request to ``status``; with ``expect``, only from that status."""
    with get_db() as conn:
        cursor = conn.cursor()
        if expect is None:
            cursor.execute(
                "UPDATE project_setup_requests SET status = %s, error_code = %s, error = %s, "
                "updated_at = NOW() WHERE id = %s RETURNING *",
                (status, error_code, error, op_id),
            )
        else:
            cursor.execute(
                "UPDATE project_setup_requests SET status = %s, error_code = %s, error = %s, "
                "updated_at = NOW() WHERE id = %s AND status = %s RETURNING *",
                (status, error_code, error, op_id, expect),
            )
        return _row(cursor.fetchone())


def list_requests(*, project_v2_id: Optional[str] = None,
                  status: Optional[str] = None) -> List[Dict[str, Any]]:
    """Requests, newest first, optionally for one project and/or status."""
    clauses, params = [], []
    if project_v2_id is not None:
        clauses.append("project_v2_id = %s")
        params.append(project_v2_id)
    if status is not None:
        clauses.append("status = %s")
        params.append(status)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT * FROM project_setup_requests{where} ORDER BY created_at DESC, id DESC",
            tuple(params),
        )
        return [r for r in (_row(row) for row in cursor.fetchall()) if r is not None]


def delete_requests_for(project_v2_id: str, target_id: str) -> int:
    """Forget every request for one project on one worker (its record was removed)."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM project_setup_requests WHERE project_v2_id = %s AND target_id = %s",
            (project_v2_id, target_id),
        )
        return cursor.rowcount or 0


def purge_settled(older_than_days: int = 7) -> int:
    """Drop settled requests older than the git_ops retention; waiting ones stay."""
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM project_setup_requests WHERE status <> %s AND updated_at < %s",
            (REQUEST_QUEUED_OFFLINE, cutoff),
        )
        return cursor.rowcount or 0


def save_peer_catalog(device_id: str, project_ids: List[str]) -> None:
    ids = sorted({str(pid) for pid in project_ids if pid})
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO project_peer_catalogs (device_id, project_ids, read_at) "
            "VALUES (%s, %s, NOW()) "
            "ON CONFLICT (device_id) DO UPDATE SET project_ids = EXCLUDED.project_ids, "
            "read_at = NOW()",
            (device_id, json.dumps(ids)),
        )


def peer_catalogs() -> Dict[str, List[str]]:
    """``{device_id: [project_v2_id, ...]}`` as last read from each peer."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT device_id, project_ids FROM project_peer_catalogs")
        return {row["device_id"]: list(parse_jsonb(row["project_ids"]) or [])
                for row in cursor.fetchall()}
