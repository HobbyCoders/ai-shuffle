"""Webhook database operations."""

from __future__ import annotations

import sqlite3
import json
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any, TypedDict

from app.db.base import get_db, row_to_dict, rows_to_list, logger


# ============================================================================
# TypedDict definitions
# ============================================================================

class WebhookRow(TypedDict):
    """Full webhook row from SELECT * FROM webhooks."""
    id: str
    url: str
    secret: Optional[str]
    events: List[str]
    is_active: bool
    created_at: str
    last_triggered_at: Optional[str]
    failure_count: int


# ============================================================================
# Webhook Operations
# ============================================================================

def get_all_webhooks() -> List[WebhookRow]:
    """Get all webhooks"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM webhooks ORDER BY created_at DESC")
        rows = rows_to_list(cursor.fetchall())
        for row in rows:
            if row.get("events"):
                row["events"] = json.loads(row["events"]) if isinstance(row["events"], str) else row["events"]
        return rows


def get_active_webhooks() -> List[WebhookRow]:
    """Get all active webhooks"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM webhooks WHERE is_active = TRUE ORDER BY created_at DESC")
        rows = rows_to_list(cursor.fetchall())
        for row in rows:
            if row.get("events"):
                row["events"] = json.loads(row["events"]) if isinstance(row["events"], str) else row["events"]
        return rows


def get_webhook(webhook_id: str) -> Optional[WebhookRow]:
    """Get a webhook by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM webhooks WHERE id = ?", (webhook_id,))
        row = row_to_dict(cursor.fetchone())
        if row and row.get("events"):
            row["events"] = json.loads(row["events"]) if isinstance(row["events"], str) else row["events"]
        return row


def create_webhook(
    webhook_id: str,
    url: str,
    events: List[str],
    secret: Optional[str] = None,
    is_active: bool = True
) -> Optional[WebhookRow]:
    """Create a new webhook"""
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO webhooks (id, url, secret, events, is_active, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (webhook_id, url, secret, json.dumps(events), is_active, now)
        )
    return get_webhook(webhook_id)


def update_webhook(
    webhook_id: str,
    url: Optional[str] = None,
    events: Optional[List[str]] = None,
    secret: Optional[str] = None,
    is_active: Optional[bool] = None
) -> Optional[WebhookRow]:
    """Update a webhook"""
    existing = get_webhook(webhook_id)
    if not existing:
        return None

    updates = []
    values = []

    if url is not None:
        updates.append("url = ?")
        values.append(url)
    if events is not None:
        updates.append("events = ?")
        values.append(json.dumps(events))
    if secret is not None:
        updates.append("secret = ?")
        values.append(secret if secret else None)
    if is_active is not None:
        updates.append("is_active = ?")
        values.append(is_active)

    if updates:
        values.append(webhook_id)
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE webhooks SET {', '.join(updates)} WHERE id = ?",
                values
            )

    return get_webhook(webhook_id)


def delete_webhook(webhook_id: str) -> bool:
    """Delete a webhook"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM webhooks WHERE id = ?", (webhook_id,))
        return cursor.rowcount > 0


def update_webhook_triggered(webhook_id: str, success: bool) -> None:
    """Update the last_triggered_at timestamp and failure_count for a webhook"""
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        if success:
            # Reset failure count on success
            cursor.execute(
                "UPDATE webhooks SET last_triggered_at = ?, failure_count = 0 WHERE id = ?",
                (now, webhook_id)
            )
        else:
            # Increment failure count
            cursor.execute(
                "UPDATE webhooks SET last_triggered_at = ?, failure_count = failure_count + 1 WHERE id = ?",
                (now, webhook_id)
            )


def get_webhooks_for_event(event_type: str) -> List[WebhookRow]:
    """Get all active webhooks subscribed to a specific event type"""
    webhooks = get_active_webhooks()
    return [w for w in webhooks if event_type in w.get("events", [])]
