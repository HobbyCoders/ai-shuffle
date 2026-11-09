"""Durable receipts for non-scheduler queries; never a runnable/live turn.

Commit the receipt before starting provider work. Terminal insertion and receipt
removal share a transaction in turns.record_adhoc_turn. A killed process leaves
either the terminal or the receipt, never a gap between them.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from app.db.base import get_db

_PROCESS_ID = uuid.uuid4().hex
# Durable recovery identity, including receipts replayed before unknown-end timing.
RECOVERY_ERROR = "Worker stopped before query finalization finished"


def create_schema(cursor):
    cursor.execute("""CREATE TABLE IF NOT EXISTS query_recovery (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        instance_id TEXT NOT NULL,
        process_id TEXT NOT NULL,
        record TEXT NOT NULL
    )""")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_query_recovery_instance ON query_recovery(instance_id)")


def begin(session_id, source, *, turn_id=None, instance_id=None, process_id=None, **record):
    """Return a stable terminal identity only after its recovery receipt commits."""
    from app.core.config import settings

    turn_id = turn_id or "turn-" + uuid.uuid4().hex
    record.setdefault("started_at", datetime.now(timezone.utc))
    record.update(outcome="aborted", stop_reason="server_shutdown",
                  error=RECOVERY_ERROR)
    with get_db() as conn:
        conn.cursor().execute(
            "INSERT INTO query_recovery (id, session_id, instance_id, process_id, record) "
            "VALUES (%s, %s, %s, %s, %s)",
            (turn_id, session_id, instance_id or settings.effective_instance_id,
             process_id or _PROCESS_ID, json.dumps({"source": source, **record}, default=str)),
        )
    return {"id": turn_id}


def recover(*, instance_id=None, process_id=None):
    """Prior-process receipts only; current queries and other installs are untouched.

    Replay uses the original terminal ID. A terminal committed by the normal path
    wins unchanged, including when recovery is retried after another crash.
    Pre-upgrade history has no receipts and is never inferred/reclassified here.
    Recovery knows the original start, not a completion time: replay must not
    become newer execution context or charge the interval spent offline.
    """
    from app.core.config import settings
    from app.db.turns import record_adhoc_turn

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, session_id, record FROM query_recovery "
            "WHERE instance_id=%s AND process_id<>%s ORDER BY id",
            (instance_id or settings.effective_instance_id, process_id or _PROCESS_ID),
        )
        rows = cursor.fetchall()
    for row in rows:
        record = json.loads(row["record"])
        source = record.pop("source")
        record_adhoc_turn(row["session_id"], source, turn_id=row["id"], recovered=True, **record)
    return len(rows)
