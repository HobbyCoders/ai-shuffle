"""Non-executable delivery receipts and peer notification outbox; never watch leases."""

from __future__ import annotations

import json
from typing import Any

from app.core.watch_values import encode
from app.db.base import get_db


def create_schema(cursor: Any) -> None:
    cursor.execute("""CREATE TABLE IF NOT EXISTS watch_origins (
        watch_id TEXT PRIMARY KEY, device_id TEXT NOT NULL
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS watch_receipts (
        event_id TEXT PRIMARY KEY, sender TEXT NOT NULL, body JSONB NOT NULL
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS watch_notification_outbox (
        event_id TEXT NOT NULL, target TEXT NOT NULL, body JSONB NOT NULL,
        acknowledged BOOLEAN NOT NULL DEFAULT FALSE, PRIMARY KEY(event_id,target)
    )""")


def origin(watch_id: str) -> str | None:
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT device_id FROM watch_origins WHERE watch_id=%s", (watch_id,))
        row = c.fetchone()
        return row["device_id"] if row else None


def set_origin(cursor: Any, watch_id: str, device_id: str) -> None:
    cursor.execute(
        "INSERT INTO watch_origins(watch_id,device_id) VALUES (%s,%s) "
        "ON CONFLICT(watch_id) DO UPDATE SET device_id=excluded.device_id",
        (watch_id, device_id),
    )


def receipt(event_id: str) -> dict[str, Any] | None:
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM watch_receipts WHERE event_id=%s", (event_id,))
        row = c.fetchone()
        if not row:
            return None
        body = row["body"]
        return {
            "sender": row["sender"],
            "body": json.loads(body) if isinstance(body, str) else body,
        }


def receive(event_id: str, sender: str, body: dict[str, Any]) -> dict[str, Any]:
    with get_db() as conn:
        conn.cursor().execute(
            "INSERT INTO watch_receipts(event_id,sender,body) VALUES (%s,%s,%s) "
            "ON CONFLICT(event_id) DO NOTHING",
            (event_id, sender, encode(body)),
        )
    saved = receipt(event_id)
    if saved is None:
        raise ValueError("Watch receipt did not persist")
    return saved


def enqueue(event_id: str, targets: list[str], body: dict[str, Any]) -> None:
    with get_db() as conn:
        c = conn.cursor()
        for target in targets:
            c.execute(
                "INSERT INTO watch_notification_outbox(event_id,target,body) VALUES (%s,%s,%s) "
                "ON CONFLICT(event_id,target) DO NOTHING",
                (event_id, target, encode(body)),
            )


def report(event_id: str, target: str) -> dict[str, Any]:
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT body FROM watch_notification_outbox WHERE event_id=%s AND target=%s",
            (event_id, target),
        )
        row = c.fetchone()
        if not row:
            raise PermissionError("No report addressed to this peer")
        return json.loads(row["body"]) if isinstance(row["body"], str) else row["body"]


def pending() -> list[dict[str, Any]]:
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM watch_notification_outbox WHERE acknowledged=FALSE")
        return [
            dict(row, body=json.loads(row["body"]) if isinstance(row["body"], str) else row["body"])
            for row in c.fetchall()
        ]


def batch(limit: int, excluded: list[str]) -> list[dict[str, Any]]:
    """One oldest pending report per eligible peer, with a bounded result set."""
    with get_db() as conn:
        c = conn.cursor()
        exclusion = ""
        if excluded:
            exclusion = " AND target NOT IN (" + ",".join(["%s"] * len(excluded)) + ")"
        c.execute(
            "SELECT event_id,target,body FROM (SELECT event_id,target,body,"
            " ROW_NUMBER() OVER (PARTITION BY target ORDER BY event_id) AS ordinal"
            " FROM watch_notification_outbox WHERE acknowledged=FALSE"
            + exclusion
            + ") pending WHERE ordinal=1 ORDER BY target LIMIT %s",
            (*excluded, limit),
        )
        return [
            dict(row, body=json.loads(row["body"]) if isinstance(row["body"], str) else row["body"])
            for row in c.fetchall()
        ]


def acknowledge(event_id: str, target: str) -> None:
    with get_db() as conn:
        conn.cursor().execute(
            "UPDATE watch_notification_outbox SET acknowledged=TRUE WHERE event_id=%s "
            "AND target=%s",
            (event_id, target),
        )
