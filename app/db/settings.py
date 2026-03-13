"""User preferences and system settings database operations."""

from __future__ import annotations

import sqlite3
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
            "SELECT * FROM user_preferences WHERE user_type = ? AND user_id = ? AND key = ?",
            (user_type, user_id, key)
        )
        row = cursor.fetchone()
        if row:
            result = row_to_dict(row)
            # Parse JSON value
            if result and result.get("value"):
                result["value"] = json.loads(result["value"])
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
               VALUES (?, ?, ?, ?, ?)
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
            "DELETE FROM user_preferences WHERE user_type = ? AND user_id = ? AND key = ?",
            (user_type, user_id, key)
        )
        return cursor.rowcount > 0


def get_all_user_preferences(user_type: str, user_id: str) -> List[UserPreferenceRow]:
    """Get all preferences for a user"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM user_preferences WHERE user_type = ? AND user_id = ?",
            (user_type, user_id)
        )
        rows = rows_to_list(cursor.fetchall())
        # Parse JSON values
        for row in rows:
            if row.get("value"):
                row["value"] = json.loads(row["value"])
        return rows


# ============================================================================
# System Settings Operations
# ============================================================================

def get_system_setting(key: str) -> Optional[str]:
    """Get a system setting by key"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM system_settings WHERE key = ?", (key,))
        row = cursor.fetchone()
        return row["value"] if row else None


def set_system_setting(key: str, value: str) -> None:
    """Set a system setting (upsert)"""
    with get_db() as conn:
        cursor = conn.cursor()
        now = datetime.now(timezone.utc).isoformat()
        cursor.execute(
            """INSERT INTO system_settings (key, value, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                   value = excluded.value,
                   updated_at = excluded.updated_at""",
            (key, value, now)
        )


def delete_system_setting(key: str) -> bool:
    """Delete a system setting"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM system_settings WHERE key = ?", (key,))
        return cursor.rowcount > 0


def get_all_system_settings() -> Dict[str, str]:
    """Get all system settings as a dictionary"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT key, value FROM system_settings")
        rows = cursor.fetchall()
        return {row["key"]: row["value"] for row in rows}
