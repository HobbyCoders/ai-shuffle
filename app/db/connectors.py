"""Connector, connector session, and connector event log database operations."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
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
    raw_events = row.get("events")
    if raw_events:
        row["events"] = json.loads(raw_events) if isinstance(raw_events, str) else raw_events
    raw_config = row.get("config")
    if raw_config:
        row["config"] = json.loads(raw_config) if isinstance(raw_config, str) else raw_config
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
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO connectors (id, type, name, api_user_id, config, events, is_active, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (connector_id, connector_type, name, api_user_id,
             json.dumps(config), json.dumps(events), is_active, now, now)
        )
    return get_connector(connector_id)


def get_connector(connector_id: str) -> Optional[ConnectorRow]:
    """Get a connector by ID."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM connectors WHERE id = %s", (connector_id,))
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
            updates.append(f"{key} = %s")
            values.append(kwargs[key])

    if "config" in kwargs:
        updates.append("config = %s")
        values.append(json.dumps(kwargs["config"]))
    if "events" in kwargs:
        updates.append("events = %s")
        values.append(json.dumps(kwargs["events"]))

    if updates:
        updates.append("updated_at = %s")
        values.append(datetime.now(timezone.utc).isoformat())
        values.append(connector_id)
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE connectors SET {', '.join(updates)} WHERE id = %s",
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
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        if status == "connected":
            cursor.execute(
                """UPDATE connectors
                   SET status = %s, status_message = %s, last_error = NULL,
                       last_connected_at = %s, failure_count = 0, updated_at = %s
                   WHERE id = %s""",
                (status, status_message, now, now, connector_id)
            )
        elif status == "error":
            cursor.execute(
                """UPDATE connectors
                   SET status = %s, status_message = %s, last_error = %s,
                       failure_count = failure_count + 1, updated_at = %s
                   WHERE id = %s""",
                (status, status_message, error, now, connector_id)
            )
        else:
            cursor.execute(
                """UPDATE connectors
                   SET status = %s, status_message = %s, updated_at = %s
                   WHERE id = %s""",
                (status, status_message, now, connector_id)
            )


def delete_connector(connector_id: str) -> bool:
    """Delete a connector and all related data (cascades)."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM connectors WHERE id = %s", (connector_id,))
        return cursor.rowcount > 0


def update_connector_event_dispatched(connector_id: str, success: bool) -> None:
    """Update last_event_at and failure_count after event dispatch."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        if success:
            cursor.execute(
                "UPDATE connectors SET last_event_at = %s, failure_count = 0, updated_at = %s WHERE id = %s",
                (now, now, connector_id)
            )
        else:
            cursor.execute(
                "UPDATE connectors SET last_event_at = %s, failure_count = failure_count + 1, updated_at = %s WHERE id = %s",
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
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO connector_event_log (id, connector_id, event_type, direction, payload, status, error, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (log_id, connector_id, event_type, direction,
             json.dumps(payload) if payload else None, status, error, now)
        )


def get_connector_event_log(connector_id: str, limit: int = 50) -> List[ConnectorEventLogRow]:
    """Get recent event log entries for a connector."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM connector_event_log WHERE connector_id = %s ORDER BY created_at DESC LIMIT %s",
            (connector_id, limit)
        )
        rows = rows_to_list(cursor.fetchall())
        for row in rows:
            raw_payload = row.get("payload")
            if raw_payload:
                if isinstance(raw_payload, str):
                    try:
                        row["payload"] = json.loads(raw_payload)
                    except (json.JSONDecodeError, TypeError):
                        pass
                # If already a dict (JSONB), leave as-is
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
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO connector_sessions
               (id, connector_id, platform_chat_id, platform_user_id, platform_user_name, session_id, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (connector_id, platform_chat_id) DO UPDATE SET
                   platform_user_id = EXCLUDED.platform_user_id,
                   platform_user_name = EXCLUDED.platform_user_name,
                   session_id = EXCLUDED.session_id,
                   updated_at = EXCLUDED.updated_at""",
            (session_id, connector_id, platform_chat_id, platform_user_id,
             platform_user_name, ai_session_id, now, now)
        )
    return get_connector_session_by_id(session_id)


def get_connector_session(connector_id: str, platform_chat_id: str) -> Optional[ConnectorSessionRow]:
    """Get a connector session by connector ID and platform chat ID."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM connector_sessions WHERE connector_id = %s AND platform_chat_id = %s",
            (connector_id, platform_chat_id)
        )
        return row_to_dict(cursor.fetchone())


def get_connector_session_by_id(session_id: str) -> Optional[ConnectorSessionRow]:
    """Get a connector session by its ID."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM connector_sessions WHERE id = %s", (session_id,))
        return row_to_dict(cursor.fetchone())


def get_connector_sessions_for_ai_session(ai_session_id: str) -> List[ConnectorSessionRow]:
    """Get all connector sessions linked to a given AI session."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM connector_sessions WHERE session_id = %s AND is_active = TRUE",
            (ai_session_id,)
        )
        return rows_to_list(cursor.fetchall())


def update_connector_session_ai_session(
    session_id: str, ai_session_id: Optional[str]
) -> None:
    """Update the AI session linked to a connector session."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE connector_sessions SET session_id = %s, updated_at = %s WHERE id = %s",
            (ai_session_id, now, session_id)
        )
