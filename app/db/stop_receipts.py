"""Per-Stop cleanup authority, independent of a logical turn's mutable attempts.

No foreign keys: publishing a Stop must not wait on a locked turn/session row,
and deleting/rewinding that row must not erase cleanup the owner has not tried.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from app.db.base import get_db, parse_jsonb


def create_schema(cursor: Any) -> None:
    cursor.execute("""CREATE TABLE IF NOT EXISTS turn_stop_receipts (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        turn_id TEXT NOT NULL,
        instance_id TEXT NOT NULL,
        stop_seq BIGINT NOT NULL,
        input_cutoff BIGINT NOT NULL,
        receipt JSONB NOT NULL,
        result JSONB,
        consumed_at TIMESTAMP WITH TIME ZONE,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
    )""")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_stop_receipts_owner "
                   "ON turn_stop_receipts(instance_id, consumed_at, id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_stop_receipts_turn "
                   "ON turn_stop_receipts(turn_id, instance_id)")


def publish(cursor: Any, session_id: str, receipt: dict) -> list[str]:
    # All reads are non-locking snapshots. An uncommitted resume is invisible;
    # its previously committed owner still receives this Stop's authority.
    cursor.execute("SELECT id, instance_id FROM turns WHERE session_id=%s "
                   "AND state IN ('running','interrupting','draining','parked') "
                   "AND instance_id IS NOT NULL", (session_id,))
    owners = list(cursor.fetchall())
    if not owners:
        return []
    cursor.execute("SELECT task_id, session_id, kind, tool_use_id, status, started_at, "
                   "remote_device_id, assignment_owner, turn_order, turn_order_upper_bound, "
                   "turn_order_incarnation, remote_stop_scope FROM background_tasks "
                   "WHERE session_id=%s AND status='running'", (session_id,))
    tasks = [{k: v.isoformat() if isinstance(v, datetime) else v for k, v in dict(row).items()}
             for row in cursor.fetchall()]
    saved = json.dumps({**receipt, "tasks": tasks})
    ids = []
    for owner in owners:
        receipt_id = uuid.uuid4().hex
        ids.append(receipt_id)
        cursor.execute("INSERT INTO turn_stop_receipts "
                       "(id,session_id,turn_id,instance_id,stop_seq,input_cutoff,receipt) "
                       "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                       (receipt_id, session_id, owner["id"], owner["instance_id"],
                        receipt["stop_seq"], receipt["input_cutoff"], saved))
    return ids


def _rows(cursor: Any) -> list[dict]:
    rows = []
    for row in cursor.fetchall():
        item = dict(row)
        item["receipt"] = parse_jsonb(item["receipt"]) or {}
        item["result"] = parse_jsonb(item.get("result"))
        rows.append(item)
    return rows


def pending(instance_id: str | None, *, turn_id: str | None = None) -> list[dict]:
    if instance_id is None and turn_id is None:
        raise ValueError("Stop receipt reads require an owner or a turn")
    clauses, params = ["consumed_at IS NULL"], []
    if instance_id is not None:
        clauses.append("instance_id=%s")
        params.append(instance_id)
    if turn_id is not None:
        clauses.append("turn_id=%s")
        params.append(turn_id)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM turn_stop_receipts WHERE " + " AND ".join(clauses) +
                       " ORDER BY id LIMIT 16", tuple(params))
        return _rows(cursor)


def get(receipt_id: str) -> dict | None:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM turn_stop_receipts WHERE id=%s", (receipt_id,))
        rows = _rows(cursor)
        return rows[0] if rows else None


def record_result(receipt_id: str, result: dict) -> None:
    # Checkpoint the bounded cleanup separately from its warning delivery. A
    # restart must retry only the notice, not acquire authority over newer work.
    with get_db() as conn:
        conn.cursor().execute("UPDATE turn_stop_receipts SET result=%s "
                              "WHERE id=%s AND consumed_at IS NULL AND result IS NULL",
                              (json.dumps(result), receipt_id))


def finish(receipt_id: str, result: dict) -> None:
    # Cleanup can be replayed after a crash, but only its first committed result
    # consumes the authority. Unknown attribution remains durable, never 'done'.
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE turn_stop_receipts SET result=%s, consumed_at=NOW() "
                       "WHERE id=%s AND consumed_at IS NULL RETURNING instance_id",
                       (json.dumps(result), receipt_id))
        owner = cursor.fetchone()
        if owner:
            prune_consumed(cursor, instance_id=owner["instance_id"])


def prune_consumed(cursor: Any, *, session_id: str | None = None,
                   instance_id: str | None = None) -> None:
    if session_id is not None:
        cursor.execute("DELETE FROM turn_stop_receipts WHERE session_id=%s AND consumed_at IS NOT NULL",
                       (session_id,))
    elif instance_id is not None:
        cursor.execute("DELETE FROM turn_stop_receipts WHERE instance_id=%s AND consumed_at<%s",
                       (instance_id, datetime.now(timezone.utc) - timedelta(days=7)))
