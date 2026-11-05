"""Durable warning-only outbox. Replaying a notice never grants Stop authority.

No foreign keys: a stopped/deleted or locked session must not block recording
that work might still be running. Delivery and its acknowledgement commit in
one event transaction; retries use the same event id after uncertain commits.
"""
from __future__ import annotations

import json
from typing import Any

from app.db.base import get_db, get_event_db, parse_jsonb


def create_schema(cursor: Any) -> None:
    cursor.execute("""CREATE TABLE IF NOT EXISTS stop_incomplete_notices (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        data JSONB NOT NULL,
        delivered_at TIMESTAMP WITH TIME ZONE,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
    )""")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_stop_notices_pending "
                   "ON stop_incomplete_notices(created_at) WHERE delivered_at IS NULL")


def save(notice_id: str, session_id: str, data: dict) -> None:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SET LOCAL lock_timeout = '100ms'")
        cursor.execute("SET LOCAL statement_timeout = '1s'")
        cursor.execute("INSERT INTO stop_incomplete_notices (id,session_id,data) "
                       "VALUES (%s,%s,%s) ON CONFLICT (id) DO NOTHING",
                       (notice_id, session_id, json.dumps(data)))


def pending() -> list[str]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM stop_incomplete_notices WHERE delivered_at IS NULL "
                       "ORDER BY created_at LIMIT 16")
        return [row["id"] for row in cursor.fetchall()]


def deliver(notice_id: str) -> bool:
    from app.db import session_events

    sid = None
    with get_event_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SET LOCAL lock_timeout = '100ms'")
        cursor.execute("SET LOCAL statement_timeout = '1s'")
        # Non-locking read; concurrent deliveries serialize at the event counter,
        # and append_events' stable-id conflict handling prevents duplicate notes.
        cursor.execute("SELECT * FROM stop_incomplete_notices WHERE id=%s", (notice_id,))
        row = cursor.fetchone()
        if row is None:
            return False
        if row["delivered_at"] is not None:
            return True
        sid = row["session_id"]
        session_events.append_events(sid, [{"id": notice_id, "type": "system.note",
            "payload": {"subtype": "stop_incomplete", "data": parse_jsonb(row["data"])}}], _conn=conn)
        cursor.execute("UPDATE stop_incomplete_notices SET delivered_at=NOW() WHERE id=%s", (notice_id,))
    session_events.notify_session_write(sid, "session_events")
    return True
