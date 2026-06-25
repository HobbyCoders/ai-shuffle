"""User preferences and system settings database operations."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, TypedDict

from app.db.base import get_db, row_to_dict, rows_to_list, logger


# ============================================================================
# TypedDict definitions
# ============================================================================

class UserPreferenceRow(TypedDict):
    """User preference row."""
    user_type: str
    user_id: str
    key: str
    value: Any
    updated_at: str


# ============================================================================
# User Preferences Operations
# ============================================================================

def get_user_preference(user_type: str, user_id: str, key: str) -> Optional[UserPreferenceRow]:
    """Get a user preference by key"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM user_preferences WHERE user_type = %s AND user_id = %s AND key = %s",
            (user_type, user_id, key)
        )
        row = cursor.fetchone()
        if row:
            result = row_to_dict(row)
            # Parse JSON value
            if result and result.get("value"):
                raw = result["value"]
                if isinstance(raw, str):
                    try:
                        result["value"] = json.loads(raw)
                    except (json.JSONDecodeError, ValueError):
                        logger.warning("Invalid JSON in preference %s for %s/%s: %r", key, user_type, user_id, raw)
                        result["value"] = raw
            return result
        return None


def set_user_preference(user_type: str, user_id: str, key: str, value: Any) -> UserPreferenceRow:
    """Set a user preference (upsert)"""
    with get_db() as conn:
        cursor = conn.cursor()
        now = datetime.now(timezone.utc).isoformat()
        value_json = json.dumps(value)

        cursor.execute(
            """INSERT INTO user_preferences (user_type, user_id, key, value, updated_at)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT(user_type, user_id, key) DO UPDATE SET
                   value = excluded.value,
                   updated_at = excluded.updated_at""",
            (user_type, user_id, key, value_json, now)
        )

        return {
            "user_type": user_type,
            "user_id": user_id,
            "key": key,
            "value": value,
            "updated_at": now
        }


def delete_user_preference(user_type: str, user_id: str, key: str) -> bool:
    """Delete a user preference"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM user_preferences WHERE user_type = %s AND user_id = %s AND key = %s",
            (user_type, user_id, key)
        )
        return cursor.rowcount > 0


def get_all_user_preferences(user_type: str, user_id: str) -> List[UserPreferenceRow]:
    """Get all preferences for a user"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM user_preferences WHERE user_type = %s AND user_id = %s",
            (user_type, user_id)
        )
        rows = rows_to_list(cursor.fetchall())
        # Parse JSON values
        for row in rows:
            if row.get("value"):
                raw = row["value"]
                if isinstance(raw, str):
                    try:
                        row["value"] = json.loads(raw)
                    except (json.JSONDecodeError, ValueError):
                        logger.warning("Invalid JSON in preference %s: %r", row.get("key"), raw)
                        row["value"] = raw
        return rows


# ============================================================================
# System Settings Operations
# ============================================================================

def get_system_setting(key: str) -> Optional[str]:
    """Get a system setting by key"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM system_settings WHERE key = %s", (key,))
        row = cursor.fetchone()
        return row["value"] if row else None


def set_system_setting(key: str, value: str) -> None:
    """Set a system setting (upsert)"""
    with get_db() as conn:
        cursor = conn.cursor()
        now = datetime.now(timezone.utc).isoformat()
        cursor.execute(
            """INSERT INTO system_settings (key, value, updated_at)
               VALUES (%s, %s, %s)
               ON CONFLICT(key) DO UPDATE SET
                   value = excluded.value,
                   updated_at = excluded.updated_at""",
            (key, value, now)
        )


def delete_system_setting(key: str) -> bool:
    """Delete a system setting"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM system_settings WHERE key = %s", (key,))
        return cursor.rowcount > 0


def get_or_create_system_setting(key: str, default_factory) -> str:
    """Return the stored value for ``key``, atomically seeding it if absent.

    Unlike ``set_system_setting`` (which clobbers via DO UPDATE), this uses
    ON CONFLICT DO NOTHING then re-reads, so concurrent installs booting against
    the same shared database converge on a single first-written value (used for
    the auto-generated cluster secret). ``default_factory`` is a zero-arg
    callable producing the seed value; it is only consulted when seeding.
    """
    existing = get_system_setting(key)
    if existing:
        return existing
    now = datetime.now(timezone.utc).isoformat()
    value = default_factory()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO system_settings (key, value, updated_at)
               VALUES (%s, %s, %s)
               ON CONFLICT(key) DO NOTHING""",
            (key, value, now),
        )
    # Re-read: another install may have won the insert race, so return the
    # value that actually landed rather than our candidate.
    return get_system_setting(key) or value


def get_all_system_settings() -> Dict[str, str]:
    """Get all system settings as a dictionary"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT key, value FROM system_settings")
        rows = cursor.fetchall()
        return {row["key"]: row["value"] for row in rows}
