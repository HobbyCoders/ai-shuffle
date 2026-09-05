"""Unified agent database operations.

The profile-named functions and ProfileRow are compatibility names used by
existing callers and foreign-key columns; all rows live in the agents table.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, TypedDict

from app.db.base import get_db, row_to_dict, rows_to_list


# ============================================================================
# TypedDict definitions
# ============================================================================

class ProfileRow(TypedDict):
    """Full profile row from SELECT * FROM agents."""
    id: str
    name: str
    description: Optional[str]
    is_builtin: bool
    config: Dict[str, Any]
    mcp_tools: List[str]
    created_at: str
    updated_at: str


def get_profile(profile_id: str) -> Optional[ProfileRow]:
    """Get a profile by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM agents WHERE id = %s", (profile_id,))
        row = row_to_dict(cursor.fetchone())
        if row:
            row["config"] = json.loads(row["config"]) if isinstance(row["config"], str) else (row["config"] if row["config"] is not None else {})
            row["mcp_tools"] = json.loads(row["mcp_tools"]) if isinstance(row["mcp_tools"], str) else (row["mcp_tools"] if row["mcp_tools"] is not None else [])
        return row


def get_all_profiles() -> List[ProfileRow]:
    """Get all profiles"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM agents ORDER BY is_builtin DESC, name ASC")
        rows = rows_to_list(cursor.fetchall())
        for row in rows:
            row["config"] = json.loads(row["config"]) if isinstance(row["config"], str) else (row["config"] if row["config"] is not None else {})
            row["mcp_tools"] = json.loads(row["mcp_tools"]) if isinstance(row["mcp_tools"], str) else (row["mcp_tools"] if row["mcp_tools"] is not None else [])
        return rows


def create_profile(
    profile_id: str,
    name: str,
    description: Optional[str],
    config: Dict[str, Any],
    is_builtin: bool = False,
    mcp_tools: Optional[List[str]] = None
) -> ProfileRow:
    """Create a new profile"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO agents (id, name, description, config, is_builtin, mcp_tools, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (profile_id, name, description, json.dumps(config), is_builtin, json.dumps(mcp_tools or []), now, now)
        )
    return get_profile(profile_id)


def update_profile(
    profile_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
    allow_builtin: bool = False,
    mcp_tools: Optional[List[str]] = None,
) -> Optional[ProfileRow]:
    """Update a profile

    Args:
        profile_id: The profile ID to update
        name: Optional new name
        description: Optional new description
        config: Optional new config
        allow_builtin: If True, allows updating builtin profiles (for migrations)
    """
    existing = get_profile(profile_id)
    if not existing:
        return None
    if existing["is_builtin"] and not allow_builtin:
        return None

    updates = []
    values = []
    if name is not None:
        updates.append("name = %s")
        values.append(name)
    if description is not None:
        updates.append("description = %s")
        values.append(description)
    if config is not None:
        updates.append("config = %s")
        values.append(json.dumps(config))

    if mcp_tools is not None:
        updates.append("mcp_tools = %s")
        values.append(json.dumps(mcp_tools))

    if updates:
        updates.append("updated_at = %s")
        values.append(datetime.now(timezone.utc).isoformat())
        values.append(profile_id)

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE agents SET {', '.join(updates)} WHERE id = %s",
                values
            )

    return get_profile(profile_id)


def delete_profile(profile_id: str) -> bool:
    """Delete a profile and unlink sessions.

    Sessions are preserved when a profile is deleted - their profile_id is set to NULL.
    This allows users to keep their chat history even after deleting a profile.
    """
    existing = get_profile(profile_id)
    if not existing:
        return False

    with get_db() as conn:
        cursor = conn.cursor()
        # Unlink sessions from this profile (sessions are preserved with NULL profile_id)
        cursor.execute("UPDATE sessions SET profile_id = NULL WHERE profile_id = %s", (profile_id,))
        # Unlink session_messages from this profile
        cursor.execute("UPDATE session_messages SET profile_id = NULL WHERE profile_id = %s", (profile_id,))
        # Unlink usage_log entries from this profile
        cursor.execute("UPDATE usage_log SET profile_id = NULL WHERE profile_id = %s", (profile_id,))
        # Now delete the profile
        cursor.execute("DELETE FROM agents WHERE id = %s", (profile_id,))
        return cursor.rowcount > 0


def get_profile_session_count(profile_id: str) -> int:
    """Get the number of sessions associated with a profile"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as cnt FROM sessions WHERE profile_id = %s", (profile_id,))
        return cursor.fetchone()["cnt"]


def get_all_profile_session_counts() -> Dict[str, int]:
    """Get session counts for all profiles in a single query"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT profile_id, COUNT(*) as count
            FROM sessions
            GROUP BY profile_id
        """)
        return {row["profile_id"]: row["count"] for row in cursor.fetchall()}


def set_profile_builtin(profile_id: str, is_builtin: bool) -> bool:
    """Set the is_builtin flag for a profile"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE agents SET is_builtin = %s, updated_at = %s WHERE id = %s",
            (is_builtin, datetime.now(timezone.utc).isoformat(), profile_id)
        )
        return cursor.rowcount > 0
