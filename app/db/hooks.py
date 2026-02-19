"""Project hook database operations."""

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

class HookRow(TypedDict):
    """Full project hook row from SELECT * FROM project_hooks."""
    id: str
    project_id: str
    name: str
    event: str
    hook_type: str
    command: Optional[str]
    timeout: int
    url: Optional[str]
    secret: Optional[str]
    enabled: bool
    tool_matcher: Optional[str]
    created_at: str
    updated_at: str
    last_triggered_at: Optional[str]
    failure_count: int


def get_hook(hook_id: str) -> Optional[HookRow]:
    """Get a project hook by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM project_hooks WHERE id = ?", (hook_id,))
        return row_to_dict(cursor.fetchone())


def get_hooks_for_project(project_id: str) -> List[HookRow]:
    """Get all hooks for a project"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM project_hooks WHERE project_id = ? ORDER BY created_at DESC",
            (project_id,)
        )
        return rows_to_list(cursor.fetchall())


def get_enabled_hooks_for_project(project_id: str) -> List[HookRow]:
    """Get all enabled hooks for a project"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM project_hooks WHERE project_id = ? AND enabled = TRUE ORDER BY created_at DESC",
            (project_id,)
        )
        return rows_to_list(cursor.fetchall())


def get_hooks_for_project_and_event(project_id: str, event: str) -> List[HookRow]:
    """Get all enabled hooks for a project that match a specific event"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM project_hooks WHERE project_id = ? AND event = ? AND enabled = TRUE ORDER BY created_at",
            (project_id, event)
        )
        return rows_to_list(cursor.fetchall())


def create_hook(
    hook_id: str,
    project_id: str,
    name: str,
    event: str,
    hook_type: str,
    command: Optional[str] = None,
    timeout: int = 30000,
    url: Optional[str] = None,
    secret: Optional[str] = None,
    enabled: bool = True,
    tool_matcher: Optional[str] = None
) -> Optional[HookRow]:
    """Create a new project hook"""
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO project_hooks
               (id, project_id, name, event, hook_type, command, timeout, url, secret, enabled, tool_matcher, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (hook_id, project_id, name, event, hook_type, command, timeout, url, secret, enabled, tool_matcher, now, now)
        )
    return get_hook(hook_id)


def update_hook(
    hook_id: str,
    name: Optional[str] = None,
    event: Optional[str] = None,
    hook_type: Optional[str] = None,
    command: Optional[str] = None,
    timeout: Optional[int] = None,
    url: Optional[str] = None,
    secret: Optional[str] = None,
    enabled: Optional[bool] = None,
    tool_matcher: Optional[str] = None
) -> Optional[HookRow]:
    """Update a project hook"""
    existing = get_hook(hook_id)
    if not existing:
        return None

    updates = []
    values = []

    if name is not None:
        updates.append("name = ?")
        values.append(name)
    if event is not None:
        updates.append("event = ?")
        values.append(event)
    if hook_type is not None:
        updates.append("hook_type = ?")
        values.append(hook_type)
    if command is not None:
        updates.append("command = ?")
        values.append(command if command else None)
    if timeout is not None:
        updates.append("timeout = ?")
        values.append(timeout)
    if url is not None:
        updates.append("url = ?")
        values.append(url if url else None)
    if secret is not None:
        updates.append("secret = ?")
        values.append(secret if secret else None)
    if enabled is not None:
        updates.append("enabled = ?")
        values.append(enabled)
    if tool_matcher is not None:
        updates.append("tool_matcher = ?")
        values.append(tool_matcher if tool_matcher else None)

    if updates:
        updates.append("updated_at = ?")
        values.append(datetime.utcnow().isoformat())
        values.append(hook_id)
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE project_hooks SET {', '.join(updates)} WHERE id = ?",
                values
            )

    return get_hook(hook_id)


def delete_hook(hook_id: str) -> bool:
    """Delete a project hook"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM project_hooks WHERE id = ?", (hook_id,))
        return cursor.rowcount > 0


def update_hook_triggered(hook_id: str, success: bool) -> None:
    """Update the last_triggered_at timestamp and failure_count for a hook"""
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        if success:
            # Reset failure count on success
            cursor.execute(
                "UPDATE project_hooks SET last_triggered_at = ?, failure_count = 0 WHERE id = ?",
                (now, hook_id)
            )
        else:
            # Increment failure count
            cursor.execute(
                "UPDATE project_hooks SET last_triggered_at = ?, failure_count = failure_count + 1 WHERE id = ?",
                (now, hook_id)
            )
