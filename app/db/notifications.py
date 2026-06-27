"""
Notification database operations — the user-facing notification history (the bell).

The persisted backing store for the unified notification system. Every banner /
toast that should survive a reload or reach the NotificationCenter lands here:

- **Server-originated** notifications written by
  ``app/services/notification_service.py`` (the ``emit_notification`` funnel).
- **Client-originated** persisted notifications POSTed by the frontend store
  (``frontend/src/lib/stores/notifications.ts``) to ``/api/v1/notifications``.

Distinct from ``error_log`` (debug/observability, read by the GetAppErrors MCP
tool): this is the *user-facing message layer*, with per-row ``read`` /
``dismissed`` state.

Rows are serialized to the wire shape the frontend store consumes directly:
``created_at`` becomes epoch milliseconds and ``action_kind`` / ``action_payload``
collapse into a single ``action`` object. Read paths are defensive (return
``[]`` / ``0`` rather than raising) so the bell can never crash the page.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from app.db.base import get_db, row_to_dict, rows_to_list, logger

# Bound a single fetch — the NotificationCenter only ever shows recent history.
DEFAULT_LIMIT = 100
MAX_LIMIT = 200


def _serialize(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Convert a DB row into the frontend-facing wire shape.

    ``created_at`` → epoch milliseconds (the store's ``AppNotification.created_at``
    is a JS timestamp); ``action_kind`` + ``action_payload`` → a single ``action``
    object. Never raises — bad rows degrade gracefully.
    """
    if not row:
        return None

    created = row.get("created_at")
    created_ms = 0
    try:
        if hasattr(created, "timestamp"):
            created_ms = int(created.timestamp() * 1000)
        elif isinstance(created, str) and created:
            created_ms = int(datetime.fromisoformat(created).timestamp() * 1000)
    except (ValueError, TypeError, OverflowError):
        created_ms = 0

    action = None
    if row.get("action_kind"):
        payload = row.get("action_payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (ValueError, TypeError):
                payload = None
        action = {"kind": row["action_kind"], "payload": payload}

    return {
        "id": row["id"],
        "user_id": row.get("user_id"),
        "severity": row.get("severity") or "info",
        "surface": row.get("surface") or "toast",
        "category": row.get("category"),
        "source": row.get("source") or "server",
        "title": row.get("title"),
        "message": row.get("message") or "",
        "session_id": row.get("session_id"),
        "read": bool(row.get("read")),
        "dismissed": bool(row.get("dismissed")),
        # Everything that reaches this table is persisted history by definition.
        "persist": True,
        "created_at": created_ms,
        "action": action,
    }


def create_notification(
    user_id: Optional[str],
    *,
    severity: str = "info",
    message: str,
    surface: str = "toast",
    category: Optional[str] = None,
    source: str = "server",
    title: Optional[str] = None,
    action_kind: Optional[str] = None,
    action_payload: Optional[Dict[str, Any]] = None,
    session_id: Optional[str] = None,
    read: bool = False,
    dismissed: bool = False,
    notification_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist a notification and return it in wire shape.

    ``notification_id`` lets a client-originated notification reuse the id it
    already generated locally (so optimistic UI + the persisted row line up).
    Idempotent on id collision: an existing row is returned untouched.
    """
    nid = notification_id or str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    payload_json = json.dumps(action_payload) if action_payload is not None else None

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO notifications
                  (id, user_id, severity, surface, category, source, title,
                   message, action_kind, action_payload, session_id, read,
                   dismissed, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (id) DO NOTHING
               RETURNING *""",
            (nid, user_id, severity, surface, category, source, title,
             message, action_kind, payload_json, session_id, read,
             dismissed, created_at),
        )
        row = cursor.fetchone()
        if row:
            return _serialize(row_to_dict(row))
        # id already existed — return the existing row.
        cursor.execute("SELECT * FROM notifications WHERE id = %s", (nid,))
        return _serialize(row_to_dict(cursor.fetchone()))


def get_notification(user_id: Optional[str], notification_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a single notification scoped to its owner."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM notifications WHERE id = %s AND user_id IS NOT DISTINCT FROM %s",
                (notification_id, user_id),
            )
            return _serialize(row_to_dict(cursor.fetchone()))
    except Exception:
        logger.debug("get_notification failed", exc_info=True)
        return None


def list_notifications(
    user_id: Optional[str],
    *,
    limit: int = DEFAULT_LIMIT,
    include_dismissed: bool = False,
) -> List[Dict[str, Any]]:
    """Return a user's recent notifications, newest first (wire shape).

    Defensive: returns ``[]`` on any failure rather than crashing the page that
    hydrates the bell.
    """
    try:
        limit = max(1, min(limit, MAX_LIMIT))
        query = "SELECT * FROM notifications WHERE user_id IS NOT DISTINCT FROM %s"
        params: List[Any] = [user_id]
        if not include_dismissed:
            query += " AND dismissed = FALSE"
        query += " ORDER BY created_at DESC LIMIT %s"
        params.append(limit)

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            rows = rows_to_list(cursor.fetchall())
        return [_serialize(r) for r in rows if r]
    except Exception:
        logger.debug("list_notifications failed", exc_info=True)
        return []


def unread_count(user_id: Optional[str]) -> int:
    """Count a user's unread, non-dismissed notifications. Returns 0 on failure."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """SELECT COUNT(*) AS count FROM notifications
                    WHERE user_id IS NOT DISTINCT FROM %s
                      AND read = FALSE AND dismissed = FALSE""",
                (user_id,),
            )
            row = cursor.fetchone()
            if not row:
                return 0
            return row["count"] if isinstance(row, dict) else row[0]
    except Exception:
        logger.debug("unread_count failed", exc_info=True)
        return 0


def mark_read(user_id: Optional[str], notification_id: str) -> bool:
    """Mark a single notification read. Returns True if a row changed."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """UPDATE notifications SET read = TRUE
                WHERE id = %s AND user_id IS NOT DISTINCT FROM %s""",
            (notification_id, user_id),
        )
        return cursor.rowcount > 0


def mark_all_read(user_id: Optional[str]) -> int:
    """Mark every unread notification for a user read. Returns rows changed."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """UPDATE notifications SET read = TRUE
                WHERE user_id IS NOT DISTINCT FROM %s AND read = FALSE""",
            (user_id,),
        )
        return cursor.rowcount


def dismiss(user_id: Optional[str], notification_id: str) -> bool:
    """Dismiss (soft-hide) a single notification. Returns True if a row changed."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """UPDATE notifications SET dismissed = TRUE
                WHERE id = %s AND user_id IS NOT DISTINCT FROM %s""",
            (notification_id, user_id),
        )
        return cursor.rowcount > 0


def clear_all(user_id: Optional[str]) -> int:
    """Delete every notification for a user (clear the bell). Returns rows deleted."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM notifications WHERE user_id IS NOT DISTINCT FROM %s",
            (user_id,),
        )
        return cursor.rowcount
