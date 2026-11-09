"""Devices the owner linked to this worker for peer terminals (#674).

Local to this worker: nothing here is synced, because a link is this worker's
own record of which key it trusts.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.db.base import get_db


def create_schema(cursor: Any) -> None:
    cursor.execute("""CREATE TABLE IF NOT EXISTS terminal_device_links (
        key_id TEXT PRIMARY KEY,
        public_key TEXT NOT NULL,
        label TEXT NOT NULL,
        via_device TEXT NOT NULL,
        via_name TEXT NOT NULL,
        approved_by TEXT NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
        last_used_at TIMESTAMP WITH TIME ZONE
    )""")


def get(key_id: str) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM terminal_device_links WHERE key_id=%s", (key_id,))
        row = cursor.fetchone()
    return dict(row) if row else None


def list_links() -> List[Dict[str, Any]]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM terminal_device_links ORDER BY created_at DESC, key_id")
        return [dict(row) for row in cursor.fetchall()]


def insert(*, key_id: str, public_key: str, label: str, via_device: str, via_name: str, approved_by: str) -> None:
    with get_db() as conn:
        conn.cursor().execute(
            """INSERT INTO terminal_device_links (key_id, public_key, label, via_device, via_name, approved_by)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (key_id) DO UPDATE SET public_key=EXCLUDED.public_key, label=EXCLUDED.label,
                   via_device=EXCLUDED.via_device, via_name=EXCLUDED.via_name,
                   approved_by=EXCLUDED.approved_by, created_at=NOW(), last_used_at=NULL""",
            (key_id, public_key, label, via_device, via_name, approved_by))


def touch(key_id: str) -> None:
    with get_db() as conn:
        conn.cursor().execute("UPDATE terminal_device_links SET last_used_at=NOW() WHERE key_id=%s", (key_id,))


def delete(key_id: str) -> bool:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM terminal_device_links WHERE key_id=%s", (key_id,))
        return cursor.rowcount > 0
