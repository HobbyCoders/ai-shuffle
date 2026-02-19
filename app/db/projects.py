"""Project database operations."""

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

class ProjectRow(TypedDict):
    """Full project row from SELECT * FROM projects."""
    id: str
    name: str
    description: Optional[str]
    path: str
    settings: Dict[str, Any]
    created_at: str
    updated_at: str


def get_project(project_id: str) -> Optional[ProjectRow]:
    """Get a project by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
        row = row_to_dict(cursor.fetchone())
        if row:
            row["settings"] = json.loads(row["settings"]) if isinstance(row["settings"], str) else row["settings"]
        return row


def get_all_projects() -> List[ProjectRow]:
    """Get all projects"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM projects ORDER BY name ASC")
        rows = rows_to_list(cursor.fetchall())
        for row in rows:
            row["settings"] = json.loads(row["settings"]) if isinstance(row["settings"], str) else row["settings"]
        return rows


def create_project(
    project_id: str,
    name: str,
    description: Optional[str],
    path: str,
    settings_dict: Optional[Dict[str, Any]] = None
) -> ProjectRow:
    """Create a new project"""
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO projects (id, name, description, path, settings, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (project_id, name, description, path, json.dumps(settings_dict or {}), now, now)
        )
    return get_project(project_id)


def update_project(
    project_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    settings_dict: Optional[Dict[str, Any]] = None
) -> Optional[ProjectRow]:
    """Update a project"""
    existing = get_project(project_id)
    if not existing:
        return None

    updates = []
    values = []
    if name is not None:
        updates.append("name = ?")
        values.append(name)
    if description is not None:
        updates.append("description = ?")
        values.append(description)
    if settings_dict is not None:
        updates.append("settings = ?")
        values.append(json.dumps(settings_dict))

    if updates:
        updates.append("updated_at = ?")
        values.append(datetime.utcnow().isoformat())
        values.append(project_id)

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE projects SET {', '.join(updates)} WHERE id = ?",
                values
            )

    return get_project(project_id)


def get_project_session_count(project_id: str) -> int:
    """Get the number of sessions associated with a project"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM sessions WHERE project_id = ?", (project_id,))
        return cursor.fetchone()[0]


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
        cursor.execute("DELETE FROM sessions WHERE project_id = ?", (project_id,))
        # Then delete the project
        cursor.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        return cursor.rowcount > 0
