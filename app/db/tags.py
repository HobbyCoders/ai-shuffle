"""Tag and session tag database operations."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, TypedDict

from app.db.base import get_db, row_to_dict, rows_to_list, logger


# ============================================================================
# TypedDict definitions
# ============================================================================

class TagRow(TypedDict):
    """Full tag row from SELECT * FROM tags."""
    id: str
    name: str
    color: str
    created_at: str
    updated_at: str


class TagSummary(TypedDict):
    """Minimal tag info returned by get_tags_for_sessions."""
    id: str
    name: str
    color: str


# ============================================================================
# Tag Operations
# ============================================================================

def get_tag(tag_id: str) -> Optional[TagRow]:
    """Get a tag by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tags WHERE id = %s", (tag_id,))
        return row_to_dict(cursor.fetchone())


def get_all_tags() -> List[TagRow]:
    """Get all tags ordered by name"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tags ORDER BY name ASC")
        return rows_to_list(cursor.fetchall())


def create_tag(
    tag_id: str,
    name: str,
    color: str = "#6366f1"
) -> TagRow:
    """Create a new tag"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO tags (id, name, color, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s)""",
            (tag_id, name, color, now, now)
        )
    return get_tag(tag_id)


def update_tag(
    tag_id: str,
    name: Optional[str] = None,
    color: Optional[str] = None
) -> Optional[TagRow]:
    """Update a tag"""
    existing = get_tag(tag_id)
    if not existing:
        return None

    updates = []
    values = []
    if name is not None:
        updates.append("name = %s")
        values.append(name)
    if color is not None:
        updates.append("color = %s")
        values.append(color)

    if updates:
        updates.append("updated_at = %s")
        values.append(datetime.now(timezone.utc).isoformat())
        values.append(tag_id)

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE tags SET {', '.join(updates)} WHERE id = %s",
                values
            )

    return get_tag(tag_id)


def delete_tag(tag_id: str) -> bool:
    """Delete a tag (session_tags will cascade delete)"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM tags WHERE id = %s", (tag_id,))
        return cursor.rowcount > 0


# ============================================================================
# Session Tag Operations
# ============================================================================

def get_session_tags(session_id: str) -> List[TagRow]:
    """Get all tags for a session"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT t.* FROM tags t
               INNER JOIN session_tags st ON t.id = st.tag_id
               WHERE st.session_id = %s
               ORDER BY t.name ASC""",
            (session_id,)
        )
        return rows_to_list(cursor.fetchall())


def add_session_tag(session_id: str, tag_id: str) -> bool:
    """Add a tag to a session"""
    try:
        now = datetime.now(timezone.utc).isoformat()
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """INSERT INTO session_tags (session_id, tag_id, created_at)
                   VALUES (%s, %s, %s) ON CONFLICT DO NOTHING""",
                (session_id, tag_id, now)
            )
            return cursor.rowcount > 0
    except Exception as e:
        logger.error(f"Failed to add tag {tag_id} to session {session_id}: {e}")
        return False


def remove_session_tag(session_id: str, tag_id: str) -> bool:
    """Remove a tag from a session"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM session_tags WHERE session_id = %s AND tag_id = %s",
            (session_id, tag_id)
        )
        return cursor.rowcount > 0


def set_session_tags(session_id: str, tag_ids: List[str]) -> List[TagRow]:
    """Set all tags for a session (replaces existing)"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        # Remove existing tags
        cursor.execute("DELETE FROM session_tags WHERE session_id = %s", (session_id,))
        # Add new tags
        for tag_id in tag_ids:
            cursor.execute(
                """INSERT INTO session_tags (session_id, tag_id, created_at)
                   VALUES (%s, %s, %s) ON CONFLICT DO NOTHING""",
                (session_id, tag_id, now)
            )
    return get_session_tags(session_id)


def get_tags_for_sessions(session_ids: list[str]) -> dict[str, list[TagSummary]]:
    """Get tags for multiple sessions in a single query.

    Returns {session_id: [tag_dicts]} for all requested session_ids.
    Sessions with no tags will have an empty list.
    """
    if not session_ids:
        return {}
    placeholders = ','.join('%s' for _ in session_ids)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f'''SELECT st.session_id, t.id, t.name, t.color
                FROM session_tags st
                JOIN tags t ON st.tag_id = t.id
                WHERE st.session_id IN ({placeholders})
                ORDER BY t.name ASC''',
            session_ids
        )
        rows = cursor.fetchall()
    result: dict[str, list[TagSummary]] = {sid: [] for sid in session_ids}
    for row in rows:
        result[row['session_id']].append({
            'id': row['id'], 'name': row['name'], 'color': row['color']
        })
    return result


def get_sessions_by_tag(tag_id: str, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    """Get all sessions with a specific tag"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT s.* FROM sessions s
               INNER JOIN session_tags st ON s.id = st.session_id
               WHERE st.tag_id = %s
               ORDER BY s.updated_at DESC
               LIMIT %s OFFSET %s""",
            (tag_id, limit, offset)
        )
        return rows_to_list(cursor.fetchall())


from app.core.resource_sync import install_database_mutations as _install_resource_sync

_install_resource_sync(
    globals(), 'tags',
    'create_tag update_tag delete_tag add_session_tag remove_session_tag set_session_tags',
    scope='shared',
)
