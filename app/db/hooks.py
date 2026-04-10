"""Project hook database operations."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, TypedDict

from app.db.base import get_db, row_to_dict, rows_to_list, logger


# ============================================================================
# TypedDict definitions
# ============================================================================

class HookRow(TypedDict):
    """Full project hook row from SELECT * FROM project_hooks."""
    id: str
    project_id: Optional[str]
    name: str
    event: str
    hook_type: str
    command: Optional[str]
    timeout: int
    url: Optional[str]
    secret: Optional[str]
    enabled: bool
    tool_matcher: Optional[str]
    message_template: Optional[str]
    created_at: str
    updated_at: str
    last_triggered_at: Optional[str]
    failure_count: int


def get_hook(hook_id: str) -> Optional[HookRow]:
    """Get a project hook by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM project_hooks WHERE id = %s", (hook_id,))
        return row_to_dict(cursor.fetchone())


def get_hooks_for_project(project_id: str) -> List[HookRow]:
    """Get all hooks for a project"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM project_hooks WHERE project_id = %s ORDER BY created_at DESC",
            (project_id,)
        )
        return rows_to_list(cursor.fetchall())


def get_app_hooks() -> List[HookRow]:
    """Get all app-level hooks (project_id is NULL)"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM project_hooks WHERE project_id IS NULL ORDER BY created_at DESC"
        )
        return rows_to_list(cursor.fetchall())


def get_all_hooks_for_project(project_id: str) -> List[HookRow]:
    """Get all hooks relevant to a project: project-scoped + app-level hooks."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM project_hooks WHERE (project_id = %s OR project_id IS NULL) ORDER BY created_at DESC",
            (project_id,)
        )
        return rows_to_list(cursor.fetchall())


def get_enabled_hooks_for_project(project_id: str) -> List[HookRow]:
    """Get all enabled hooks for a project"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM project_hooks WHERE project_id = %s AND enabled = TRUE ORDER BY created_at DESC",
            (project_id,)
        )
        return rows_to_list(cursor.fetchall())


def get_hooks_for_project_and_event(project_id: str, event: str) -> List[HookRow]:
    """Get all enabled hooks for a project (+ app hooks) that match a specific event"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM project_hooks WHERE (project_id = %s OR project_id IS NULL) AND event = %s AND enabled = TRUE ORDER BY created_at",
            (project_id, event)
        )
        return rows_to_list(cursor.fetchall())


def get_app_hooks_for_event(event: str) -> List[HookRow]:
    """Get all enabled app-level hooks that match a specific event"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM project_hooks WHERE project_id IS NULL AND event = %s AND enabled = TRUE ORDER BY created_at",
            (event,)
        )
        return rows_to_list(cursor.fetchall())


def create_hook(
    hook_id: str,
    name: str,
    event: str,
    hook_type: str,
    project_id: Optional[str] = None,
    command: Optional[str] = None,
    timeout: int = 30000,
    url: Optional[str] = None,
    secret: Optional[str] = None,
    enabled: bool = True,
    tool_matcher: Optional[str] = None,
    message_template: Optional[str] = None
) -> Optional[HookRow]:
    """Create a new hook (project-scoped or app-level)"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO project_hooks
               (id, project_id, name, event, hook_type, command, timeout, url, secret, enabled, tool_matcher, message_template, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (hook_id, project_id, name, event, hook_type, command, timeout, url, secret, enabled, tool_matcher, message_template, now, now)
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
    tool_matcher: Optional[str] = None,
    message_template: Optional[str] = None
) -> Optional[HookRow]:
    """Update a hook"""
    existing = get_hook(hook_id)
    if not existing:
        return None

    updates = []
    values = []

    if name is not None:
        updates.append("name = %s")
        values.append(name)
    if event is not None:
        updates.append("event = %s")
        values.append(event)
    if hook_type is not None:
        updates.append("hook_type = %s")
        values.append(hook_type)
    if command is not None:
        updates.append("command = %s")
        values.append(command if command else None)
    if timeout is not None:
        updates.append("timeout = %s")
        values.append(timeout)
    if url is not None:
        updates.append("url = %s")
        values.append(url if url else None)
    if secret is not None:
        updates.append("secret = %s")
        values.append(secret if secret else None)
    if enabled is not None:
        updates.append("enabled = %s")
        values.append(enabled)
    if tool_matcher is not None:
        updates.append("tool_matcher = %s")
        values.append(tool_matcher if tool_matcher else None)
    if message_template is not None:
        updates.append("message_template = %s")
        values.append(message_template if message_template else None)

    if updates:
        updates.append("updated_at = %s")
        values.append(datetime.now(timezone.utc).isoformat())
        values.append(hook_id)
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE project_hooks SET {', '.join(updates)} WHERE id = %s",
                values
            )

    return get_hook(hook_id)


def delete_hook(hook_id: str) -> bool:
    """Delete a hook"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM project_hooks WHERE id = %s", (hook_id,))
        return cursor.rowcount > 0


def update_hook_triggered(hook_id: str, success: bool) -> None:
    """Update the last_triggered_at timestamp and failure_count for a hook"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        if success:
            # Reset failure count on success
            cursor.execute(
                "UPDATE project_hooks SET last_triggered_at = %s, failure_count = 0 WHERE id = %s",
                (now, hook_id)
            )
        else:
            # Increment failure count
            cursor.execute(
                "UPDATE project_hooks SET last_triggered_at = %s, failure_count = failure_count + 1 WHERE id = %s",
                (now, hook_id)
            )
