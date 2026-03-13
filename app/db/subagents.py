"""Subagent and permission rule database operations."""

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

class SubagentRow(TypedDict):
    """Full subagent row from SELECT * FROM subagents."""
    id: str
    name: str
    description: str
    prompt: str
    tools: Optional[List[str]]
    model: Optional[str]
    is_builtin: bool
    default_name: Optional[str]
    default_description: Optional[str]
    default_prompt: Optional[str]
    default_tools: Optional[List[str]]
    default_model: Optional[str]
    created_at: str
    updated_at: str


class PermissionRuleRow(TypedDict):
    """Full permission rule row from SELECT * FROM permission_rules."""
    id: str
    profile_id: Optional[str]
    tool_name: str
    tool_pattern: Optional[str]
    decision: str
    created_at: str


# ============================================================================
# Subagent Operations
# ============================================================================

def get_subagent(subagent_id: str) -> Optional[SubagentRow]:
    """Get a subagent by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM subagents WHERE id = ?", (subagent_id,))
        row = row_to_dict(cursor.fetchone())
        if row and row.get("tools"):
            row["tools"] = json.loads(row["tools"]) if isinstance(row["tools"], str) else row["tools"]
        return row


def get_all_subagents() -> List[SubagentRow]:
    """Get all subagents"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM subagents ORDER BY name ASC")
        rows = rows_to_list(cursor.fetchall())
        for row in rows:
            if row.get("tools"):
                row["tools"] = json.loads(row["tools"]) if isinstance(row["tools"], str) else row["tools"]
        return rows


def create_subagent(
    subagent_id: str,
    name: str,
    description: str,
    prompt: str,
    tools: Optional[List[str]] = None,
    model: Optional[str] = None,
    is_builtin: bool = False,
    store_defaults: bool = False
) -> SubagentRow:
    """
    Create a new subagent.

    Args:
        subagent_id: Unique identifier for the subagent
        name: Display name
        description: When to use this subagent
        prompt: System prompt for the subagent
        tools: List of allowed tools (None = inherit all)
        model: Model override (None = inherit from profile)
        is_builtin: Whether this is a built-in subagent (cannot be deleted)
        store_defaults: If True, also store current values as defaults (for built-in subagents)
    """
    now = datetime.now(timezone.utc).isoformat()
    tools_json = json.dumps(tools) if tools else None

    with get_db() as conn:
        cursor = conn.cursor()
        if store_defaults:
            # For built-in subagents, store both current values and defaults
            cursor.execute(
                """INSERT INTO subagents (
                    id, name, description, prompt, tools, model, is_builtin,
                    default_name, default_description, default_prompt, default_tools, default_model,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (subagent_id, name, description, prompt, tools_json, model, is_builtin,
                 name, description, prompt, tools_json, model,
                 now, now)
            )
        else:
            cursor.execute(
                """INSERT INTO subagents (id, name, description, prompt, tools, model, is_builtin, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (subagent_id, name, description, prompt, tools_json, model, is_builtin, now, now)
            )
    return get_subagent(subagent_id)


def update_subagent(
    subagent_id: str,
    name: Optional[str] = ...,  # type: ignore[assignment]
    description: Optional[str] = ...,  # type: ignore[assignment]
    prompt: Optional[str] = ...,  # type: ignore[assignment]
    tools: Optional[List[str]] = ...,  # type: ignore[assignment]
    model: Optional[str] = ...  # type: ignore[assignment]
) -> Optional[SubagentRow]:
    """
    Update a subagent.

    Uses Ellipsis (...) as default to distinguish between "not provided" and "explicitly set to None".
    This allows clearing fields by passing None explicitly.
    """
    existing = get_subagent(subagent_id)
    if not existing:
        return None

    updates = []
    values = []
    if name is not ...:
        updates.append("name = ?")
        values.append(name)
    if description is not ...:
        updates.append("description = ?")
        values.append(description)
    if prompt is not ...:
        updates.append("prompt = ?")
        values.append(prompt)
    if tools is not ...:
        updates.append("tools = ?")
        values.append(json.dumps(tools) if tools else None)
    if model is not ...:
        updates.append("model = ?")
        values.append(model)

    if updates:
        updates.append("updated_at = ?")
        values.append(datetime.now(timezone.utc).isoformat())
        values.append(subagent_id)

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE subagents SET {', '.join(updates)} WHERE id = ?",
                values
            )

    return get_subagent(subagent_id)


def delete_subagent(subagent_id: str) -> bool:
    """Delete a subagent"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM subagents WHERE id = ?", (subagent_id,))
        return cursor.rowcount > 0


def set_subagent_builtin(subagent_id: str, is_builtin: bool) -> bool:
    """Set the is_builtin flag for a subagent"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE subagents SET is_builtin = ?, updated_at = ? WHERE id = ?",
            (is_builtin, datetime.now(timezone.utc).isoformat(), subagent_id)
        )
        return cursor.rowcount > 0


def revert_subagent_to_defaults(subagent_id: str) -> Optional[SubagentRow]:
    """
    Revert a built-in subagent to its original default values.

    Only works for built-in subagents that have stored defaults.
    Returns the reverted subagent or None if not found/not built-in.
    """
    subagent = get_subagent(subagent_id)
    if not subagent:
        return None

    if not subagent.get("is_builtin"):
        return None

    # Check if defaults exist
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT default_name, default_description, default_prompt, default_tools, default_model
               FROM subagents WHERE id = ?""",
            (subagent_id,)
        )
        row = cursor.fetchone()
        if not row or not row["default_prompt"]:
            # No defaults stored
            return None

        # Revert to defaults
        cursor.execute(
            """UPDATE subagents SET
                name = default_name,
                description = default_description,
                prompt = default_prompt,
                tools = default_tools,
                model = default_model,
                updated_at = ?
               WHERE id = ?""",
            (datetime.now(timezone.utc).isoformat(), subagent_id)
        )

    return get_subagent(subagent_id)


def has_subagent_been_modified(subagent_id: str) -> bool:
    """
    Check if a built-in subagent has been modified from its defaults.

    Returns True if any field differs from the stored default.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT
                name != default_name OR
                description != default_description OR
                prompt != default_prompt OR
                COALESCE(tools, '') != COALESCE(default_tools, '') OR
                COALESCE(model, '') != COALESCE(default_model, '')
               AS is_modified
               FROM subagents WHERE id = ? AND is_builtin = 1""",
            (subagent_id,)
        )
        row = cursor.fetchone()
        return bool(row and row["is_modified"])


# ============================================================================
# Permission Rules Operations
# ============================================================================

def add_permission_rule(
    profile_id: str,
    tool_name: str,
    tool_pattern: Optional[str],
    decision: str
) -> PermissionRuleRow:
    """Add a permission rule for a profile"""
    import uuid
    rule_id = f"rule-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO permission_rules (id, profile_id, tool_name, tool_pattern, decision, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (rule_id, profile_id, tool_name, tool_pattern, decision, now)
        )
    return get_permission_rule(rule_id)


def get_permission_rule(rule_id: str) -> Optional[PermissionRuleRow]:
    """Get a permission rule by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM permission_rules WHERE id = ?", (rule_id,))
        return row_to_dict(cursor.fetchone())


def get_permission_rules(
    profile_id: Optional[str] = None,
    tool_name: Optional[str] = None
) -> List[PermissionRuleRow]:
    """Get permission rules with optional filters"""
    query = "SELECT * FROM permission_rules WHERE 1=1"
    params = []

    if profile_id:
        query += " AND profile_id = ?"
        params.append(profile_id)
    if tool_name:
        query += " AND tool_name = ?"
        params.append(tool_name)

    query += " ORDER BY created_at DESC"

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        return rows_to_list(cursor.fetchall())


def delete_permission_rule(rule_id: str) -> bool:
    """Delete a permission rule"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM permission_rules WHERE id = ?", (rule_id,))
        return cursor.rowcount > 0


def delete_profile_permission_rules(profile_id: str) -> int:
    """Delete all permission rules for a profile"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM permission_rules WHERE profile_id = ?", (profile_id,))
        return cursor.rowcount
