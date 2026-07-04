"""Project database operations."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, TypedDict

from app.db.base import get_db, row_to_dict, rows_to_list, logger
from app.core.config import settings


# ============================================================================
# TypedDict definitions
# ============================================================================

class ProjectRow(TypedDict):
    """Full project row from SELECT * FROM projects."""
    id: str
    name: str
    description: Optional[str]
    path: str
    settings: Dict[str, Any]
    instance_id: Optional[str]
    link_group_id: Optional[str]
    created_at: str
    updated_at: str


def _parse_project_row(row: Optional[Dict[str, Any]]) -> Optional[ProjectRow]:
    """Normalize a raw projects row: decode the settings JSON column."""
    if row:
        row["settings"] = json.loads(row["settings"]) if isinstance(row["settings"], str) else (row["settings"] if row["settings"] is not None else {})
    return row


def get_project(project_id: str) -> Optional[ProjectRow]:
    """Get a project by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM projects WHERE id = %s", (project_id,))
        return _parse_project_row(row_to_dict(cursor.fetchone()))


def get_all_projects() -> List[ProjectRow]:
    """Get all projects, filtered to the current instance if instance_id is set."""
    instance_id = settings.effective_instance_id
    with get_db() as conn:
        cursor = conn.cursor()
        # Show projects belonging to this instance OR projects with no instance_id (legacy)
        cursor.execute(
            "SELECT * FROM projects WHERE instance_id = %s OR instance_id IS NULL ORDER BY name ASC",
            (instance_id,)
        )
        return [_parse_project_row(row) for row in rows_to_list(cursor.fetchall())]


def create_project(
    project_id: str,
    name: str,
    description: Optional[str],
    path: str,
    settings_dict: Optional[Dict[str, Any]] = None
) -> ProjectRow:
    """Create a new project"""
    now = datetime.now(timezone.utc).isoformat()
    instance_id = settings.effective_instance_id
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO projects (id, name, description, path, settings, instance_id, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (project_id, name, description, path, json.dumps(settings_dict or {}), instance_id, now, now)
        )
    return get_project(project_id)


def update_project(
    project_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    path: Optional[str] = None,
    settings_dict: Optional[Dict[str, Any]] = None
) -> Optional[ProjectRow]:
    """Update a project"""
    existing = get_project(project_id)
    if not existing:
        return None

    updates = []
    values = []
    if name is not None:
        updates.append("name = %s")
        values.append(name)
    if description is not None:
        updates.append("description = %s")
        values.append(description)
    if path is not None:
        updates.append("path = %s")
        values.append(path)
    if settings_dict is not None:
        updates.append("settings = %s")
        values.append(json.dumps(settings_dict))

    if updates:
        updates.append("updated_at = %s")
        values.append(datetime.now(timezone.utc).isoformat())
        values.append(project_id)

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE projects SET {', '.join(updates)} WHERE id = %s",
                values
            )

    return get_project(project_id)


def set_project_link_group(project_id: str, link_group_id: Optional[str]) -> Optional[ProjectRow]:
    """Set (or clear, with ``None``) a project's link group id.

    Unlike ``update_project`` this writes the value directly so it can be
    cleared back to NULL. Touches ``updated_at`` so peers notice the change.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE projects SET link_group_id = %s, updated_at = %s WHERE id = %s",
            (link_group_id, datetime.now(timezone.utc).isoformat(), project_id),
        )
    return get_project(project_id)


def get_projects_in_link_group(link_group_id: str) -> List[ProjectRow]:
    """All project rows sharing a link group — one row per install for the same repo.

    Deliberately NOT instance-filtered: callers use this to find which installs
    have a clone of a logical project (see app/core/project_links.py).
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM projects WHERE link_group_id = %s ORDER BY id ASC",
            (link_group_id,),
        )
        return [_parse_project_row(row) for row in rows_to_list(cursor.fetchall())]


def get_project_session_count(project_id: str) -> int:
    """Get the number of sessions associated with a project"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as cnt FROM sessions WHERE project_id = %s", (project_id,))
        return cursor.fetchone()["cnt"]


def get_all_project_session_counts() -> Dict[str, int]:
    """Get session counts for all projects in a single query"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT project_id, COUNT(*) as count
            FROM sessions
            WHERE project_id IS NOT NULL
            GROUP BY project_id
        """)
        return {row["project_id"]: row["count"] for row in cursor.fetchall()}


def delete_project(project_id: str) -> bool:
    """Delete a project and all associated sessions (sessions are cwd-dependent)"""
    with get_db() as conn:
        cursor = conn.cursor()
        # First delete all sessions associated with this project
        cursor.execute("DELETE FROM sessions WHERE project_id = %s", (project_id,))
        # Then delete the project
        cursor.execute("DELETE FROM projects WHERE id = %s", (project_id,))
        return cursor.rowcount > 0
