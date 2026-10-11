"""Unified agent database operations.

The profile-named functions and ProfileRow are compatibility names used by
existing callers and foreign-key columns; all rows live in the agents table.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, TypedDict

from app.db.base import get_db, row_to_dict, rows_to_list

# ``update_profile`` leaves a field alone when it is not passed; this marks
# "not passed" for ``avatar_color``, whose None means "clear it".
_UNSET: Any = object()


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
    avatar_color: Optional[str]
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
    """Get all profiles.

    The persistent assistant's built-in agent is left out: it runs only the
    assistant's home and research, so it never appears in agent pickers,
    the Agents page, Task rosters or "first agent" fallbacks. Look it up by
    id with ``get_profile``.
    """
    from app.db.assistant import AGENT_ID as ASSISTANT_AGENT_ID

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM agents WHERE id <> %s ORDER BY is_builtin DESC, name ASC",
                       (ASSISTANT_AGENT_ID,))
        rows = rows_to_list(cursor.fetchall())
        for row in rows:
            row["config"] = json.loads(row["config"]) if isinstance(row["config"], str) else (row["config"] if row["config"] is not None else {})
            row["mcp_tools"] = json.loads(row["mcp_tools"]) if isinstance(row["mcp_tools"], str) else (row["mcp_tools"] if row["mcp_tools"] is not None else [])
        return rows


def _refuse_assistant_agent(profile_id: str) -> None:
    """The assistant's built-in agent is app-defined: no owner edits or deletes.

    ``assistant_service.ensure_agent`` writes it directly, outside these helpers.
    """
    from app.db.assistant import AGENT_ID as ASSISTANT_AGENT_ID

    if profile_id == ASSISTANT_AGENT_ID:
        raise PermissionError("The assistant's built-in agent cannot be changed or deleted")


def create_profile(
    profile_id: str,
    name: str,
    description: Optional[str],
    config: Dict[str, Any],
    is_builtin: bool = False,
    mcp_tools: Optional[List[str]] = None,
    if_absent: bool = False,
    avatar_color: Optional[str] = None,
) -> ProfileRow:
    """Create a new profile.

    ``if_absent`` (start-up seeding) leaves an existing row with this id alone
    instead of raising on the duplicate key; the returned row is the stored one.
    """
    now = datetime.now(timezone.utc).isoformat()
    on_conflict = " ON CONFLICT (id) DO NOTHING" if if_absent else ""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO agents (id, name, description, config, is_builtin, mcp_tools, avatar_color, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""" + on_conflict,
            (profile_id, name, description, json.dumps(config), is_builtin, json.dumps(mcp_tools or []),
             avatar_color, now, now)
        )
        if cursor.rowcount > 0:
            # A deleted agent's API-key assignments must not attach to this one.
            from app.db.api_users import release_grants_for_new_agent
            release_grants_for_new_agent(cursor, profile_id)
    return get_profile(profile_id)


def update_profile(
    profile_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
    allow_builtin: bool = False,
    mcp_tools: Optional[List[str]] = None,
    avatar_color: Any = _UNSET,
) -> Optional[ProfileRow]:
    """Update a profile

    Args:
        profile_id: The profile ID to update
        name: Optional new name
        description: Optional new description
        config: Optional new config
        allow_builtin: If True, allows updating builtin profiles (for migrations)
        avatar_color: New colour token, or None to clear it; omit to keep it
    """
    _refuse_assistant_agent(profile_id)
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

    if avatar_color is not _UNSET:
        updates.append("avatar_color = %s")
        values.append(avatar_color)

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
    _refuse_assistant_agent(profile_id)
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


RETIRED_BUILTIN_IDS = ("Explore", "Plan")


def retire_builtin_subagent(subagent_id: str) -> bool:
    """Retire a removed built-in, never an owner-created agent with this id.

    Automations have a required/restricted agent FK. Preserve their definitions
    on General Purpose, but disable them rather than silently run a different
    prompt or tool surface. Session/history references are unlinked as on delete.
    """
    if subagent_id not in RETIRED_BUILTIN_IDS:
        return False

    from app.core.config import settings

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM agents WHERE id = %s AND is_builtin = TRUE FOR UPDATE", (subagent_id,))
        if cursor.fetchone() is None:
            return False
        cursor.execute(
            "UPDATE automations SET profile_id = %s, is_enabled = FALSE, last_error = %s, updated_at = %s "
            "WHERE profile_id = %s",
            ("general-purpose", f"Built-in {subagent_id} was removed; review the General Purpose agent before re-enabling.",
             datetime.now(timezone.utc).isoformat(), subagent_id),
        )
        # Persist cancellation notices until the wake service can record them in
        # the chat. They carry no runnable profile/prompt, and never submit a turn.
        reason = f"Built-in {subagent_id} was removed; its scheduled wake/parked continuation was cancelled."
        cursor.execute(
            "UPDATE pending_wakeups SET source = %s, reason = %s, profile_id = NULL, prompt = '', model = NULL, fire_at = %s, "
            "instance_id = COALESCE(instance_id, %s) "
            "WHERE profile_id = %s OR (profile_id IS NULL AND session_id IN "
            "(SELECT id FROM sessions WHERE profile_id = %s))",
            ("profile_cancelled", reason, 0, settings.effective_instance_id, subagent_id, subagent_id),
        )
        cursor.execute(
            "UPDATE turns SET state = 'aborted', stop_reason = 'profile_removed', error = %s, ended_at = NOW() "
            "WHERE state IN ('parked', 'queued') AND (profile_id = %s OR "
            "(profile_id IS NULL AND session_id IN (SELECT id FROM sessions WHERE profile_id = %s))) "
            "RETURNING id, session_id, instance_id",
            (reason, subagent_id, subagent_id),
        )
        for turn in cursor.fetchall():
            cursor.execute(
                "INSERT INTO pending_wakeups (id, session_id, prompt, reason, source, instance_id, fire_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                ("profile-cancelled-" + turn["id"], turn["session_id"], "", reason,
                 "profile_cancelled", turn["instance_id"] or settings.effective_instance_id, 0),
            )
        cursor.execute(
            "UPDATE sessions SET parked_task_kind = NULL WHERE profile_id = %s", (subagent_id,)
        )
        for table in ("sessions", "session_messages", "usage_log"):
            cursor.execute(f"UPDATE {table} SET profile_id = NULL WHERE profile_id = %s", (subagent_id,))
        # Forget the local sync version, not a network tombstone. Publishing an
        # agent delete ahead of the automation rewrite would hit RESTRICT on
        # an older worker. Updated peers retire the row independently.
        cursor.execute("DELETE FROM sync_records WHERE collection = %s AND record_id = %s", ("agents", subagent_id))
        cursor.execute("DELETE FROM agents WHERE id = %s AND is_builtin = TRUE", (subagent_id,))
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
    _refuse_assistant_agent(profile_id)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE agents SET is_builtin = %s, updated_at = %s WHERE id = %s",
            (is_builtin, datetime.now(timezone.utc).isoformat(), profile_id)
        )
        return cursor.rowcount > 0


from app.core.resource_sync import install_database_mutations as _install_resource_sync

_install_resource_sync(
    globals(), 'agents',
    'create_profile update_profile delete_profile set_profile_builtin',
    scope='shared',
)
