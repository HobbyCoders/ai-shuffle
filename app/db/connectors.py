"""Connector, connector session, and connector event log database operations."""

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

class ConnectorRow(TypedDict):
    """Full connector row from SELECT * FROM connectors."""
    id: str
    type: str
    name: str
    api_user_id: Optional[str]
    config: Dict[str, Any]
    events: List[str]
    is_active: bool
    status: str
    status_message: Optional[str]
    last_connected_at: Optional[str]
    last_error: Optional[str]
    last_event_at: Optional[str]
    failure_count: int
    created_at: str
    updated_at: str


class ConnectorEventLogRow(TypedDict):
    """Connector event log entry."""
    id: str
    connector_id: str
    event_type: str
    direction: str
    payload: Optional[Dict[str, Any]]
    status: str
    error: Optional[str]
    created_at: str


class ConnectorSessionRow(TypedDict):
    """Connector session mapping row."""
    id: str
    connector_id: str
    platform_chat_id: str
    platform_user_id: Optional[str]
    platform_user_name: Optional[str]
    session_id: Optional[str]
    is_active: bool
    created_at: str
    updated_at: str


# ============================================================================
# Connector Operations
# ============================================================================

def _parse_connector_row(row: Optional[Dict[str, Any]]) -> Optional[ConnectorRow]:
    """Parse JSON fields in a connector row."""
    if not row:
        return None
    if row.get("events") and isinstance(row["events"], str):
        row["events"] = json.loads(row["events"])
    if row.get("config") and isinstance(row["config"], str):
        row["config"] = json.loads(row["config"])
    return row


def create_connector(
    connector_id: str,
    connector_type: str,
    name: str,
    config: Dict[str, Any],
    events: List[str],
    api_user_id: Optional[str] = None,
    is_active: bool = False,
) -> Optional[ConnectorRow]:
    """Create a new connector."""
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO connectors (id, type, name, api_user_id, config, events, is_active, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (connector_id, connector_type, name, api_user_id,
             json.dumps(config), json.dumps(events), is_active, now, now)
        )
    return get_connector(connector_id)


def get_connector(connector_id: str) -> Optional[ConnectorRow]:
    """Get a connector by ID."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM connectors WHERE id = ?", (connector_id,))
        return _parse_connector_row(row_to_dict(cursor.fetchone()))


def get_all_connectors() -> List[ConnectorRow]:
    """Get all connectors."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM connectors ORDER BY created_at DESC")
        rows = rows_to_list(cursor.fetchall())
        return [_parse_connector_row(r) for r in rows]


def get_active_connectors() -> List[ConnectorRow]:
    """Get all active connectors."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM connectors WHERE is_active = TRUE ORDER BY created_at DESC")
        rows = rows_to_list(cursor.fetchall())
        return [_parse_connector_row(r) for r in rows]


def get_connectors_for_event(event_type: str) -> List[ConnectorRow]:
    """Get all active connectors subscribed to a specific event type."""
    connectors = get_active_connectors()
    return [c for c in connectors if event_type in c.get("events", [])]


def update_connector(connector_id: str, **kwargs) -> Optional[ConnectorRow]:
    """Update a connector. Pass keyword arguments for fields to update."""
    existing = get_connector(connector_id)
    if not existing:
        return None

    updates = []
    values = []

    for key in ("name", "is_active", "status", "status_message", "last_connected_at",
                "last_error", "last_event_at", "api_user_id"):
        if key in kwargs:
            updates.append(f"{key} = ?")
            values.append(kwargs[key])

    if "config" in kwargs:
        updates.append("config = ?")
        values.append(json.dumps(kwargs["config"]))
    if "events" in kwargs:
        updates.append("events = ?")
        values.append(json.dumps(kwargs["events"]))

    if updates:
        updates.append("updated_at = ?")
        values.append(datetime.utcnow().isoformat())
        values.append(connector_id)
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE connectors SET {', '.join(updates)} WHERE id = ?",
                values
            )

    return get_connector(connector_id)


def update_connector_status(
    connector_id: str,
    status: str,
    status_message: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    """Update the runtime status of a connector."""
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        if status == "connected":
            cursor.execute(
                """UPDATE connectors
                   SET status = ?, status_message = ?, last_error = NULL,
                       last_connected_at = ?, failure_count = 0, updated_at = ?
                   WHERE id = ?""",
                (status, status_message, now, now, connector_id)
            )
        elif status == "error":
            cursor.execute(
                """UPDATE connectors
                   SET status = ?, status_message = ?, last_error = ?,
                       failure_count = failure_count + 1, updated_at = ?
                   WHERE id = ?""",
                (status, status_message, error, now, connector_id)
            )
        else:
            cursor.execute(
                """UPDATE connectors
                   SET status = ?, status_message = ?, updated_at = ?
                   WHERE id = ?""",
                (status, status_message, now, connector_id)
            )


def delete_connector(connector_id: str) -> bool:
    """Delete a connector and all related data (cascades)."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM connectors WHERE id = ?", (connector_id,))
        return cursor.rowcount > 0


def update_connector_event_dispatched(connector_id: str, success: bool) -> None:
    """Update last_event_at and failure_count after event dispatch."""
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        if success:
            cursor.execute(
                "UPDATE connectors SET last_event_at = ?, failure_count = 0, updated_at = ? WHERE id = ?",
                (now, now, connector_id)
            )
        else:
            cursor.execute(
                "UPDATE connectors SET last_event_at = ?, failure_count = failure_count + 1, updated_at = ? WHERE id = ?",
                (now, now, connector_id)
            )


# ---- Connector Event Log ----

def create_connector_event_log(
    log_id: str,
    connector_id: str,
    event_type: str,
    direction: str = "outbound",
    payload: Optional[Dict[str, Any]] = None,
    status: str = "pending",
    error: Optional[str] = None,
) -> None:
    """Create a connector event log entry."""
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO connector_event_log (id, connector_id, event_type, direction, payload, status, error, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (log_id, connector_id, event_type, direction,
             json.dumps(payload) if payload else None, status, error, now)
        )


def get_connector_event_log(connector_id: str, limit: int = 50) -> List[ConnectorEventLogRow]:
    """Get recent event log entries for a connector."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM connector_event_log WHERE connector_id = ? ORDER BY created_at DESC LIMIT ?",
            (connector_id, limit)
        )
        rows = rows_to_list(cursor.fetchall())
        for row in rows:
            if row.get("payload") and isinstance(row["payload"], str):
                try:
                    row["payload"] = json.loads(row["payload"])
                except (json.JSONDecodeError, TypeError):
                    pass
        return rows


# ---- Connector Sessions ----

def create_connector_session(
    session_id: str,
    connector_id: str,
    platform_chat_id: str,
    platform_user_id: Optional[str] = None,
    platform_user_name: Optional[str] = None,
    ai_session_id: Optional[str] = None,
) -> Optional[ConnectorSessionRow]:
    """Create a new connector session mapping."""
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT OR REPLACE INTO connector_sessions
               (id, connector_id, platform_chat_id, platform_user_id, platform_user_name, session_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, connector_id, platform_chat_id, platform_user_id,
             platform_user_name, ai_session_id, now, now)
        )
    return get_connector_session_by_id(session_id)


def get_connector_session(connector_id: str, platform_chat_id: str) -> Optional[ConnectorSessionRow]:
    """Get a connector session by connector ID and platform chat ID."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM connector_sessions WHERE connector_id = ? AND platform_chat_id = ?",
            (connector_id, platform_chat_id)
        )
        return row_to_dict(cursor.fetchone())


def get_connector_session_by_id(session_id: str) -> Optional[ConnectorSessionRow]:
    """Get a connector session by its ID."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM connector_sessions WHERE id = ?", (session_id,))
        return row_to_dict(cursor.fetchone())


def get_connector_sessions_for_ai_session(ai_session_id: str) -> List[ConnectorSessionRow]:
    """Get all connector sessions linked to a given AI session."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM connector_sessions WHERE session_id = ? AND is_active = TRUE",
            (ai_session_id,)
        )
        return rows_to_list(cursor.fetchall())


def update_connector_session_ai_session(
    session_id: str, ai_session_id: Optional[str]
) -> None:
    """Update the AI session linked to a connector session."""
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE connector_sessions SET session_id = ?, updated_at = ? WHERE id = ?",
            (ai_session_id, now, session_id)
        )
