"""Template database operations."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, TypedDict

from app.db.base import get_db, row_to_dict, rows_to_list, logger


# ============================================================================
# TypedDict definitions
# ============================================================================

class TemplateRow(TypedDict):
    """Full template row from SELECT * FROM templates."""
    id: str
    name: str
    description: Optional[str]
    prompt: str
    profile_id: Optional[str]
    icon: Optional[str]
    category: Optional[str]
    is_builtin: bool
    created_at: str
    updated_at: str


# ============================================================================
# Template Operations
# ============================================================================

def get_template(template_id: str) -> Optional[TemplateRow]:
    """Get a template by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM templates WHERE id = %s", (template_id,))
        return row_to_dict(cursor.fetchone())


def get_all_templates(
    profile_id: Optional[str] = None,
    category: Optional[str] = None
) -> List[TemplateRow]:
    """Get all templates with optional filters"""
    query = "SELECT * FROM templates WHERE 1=1"
    params = []

    if profile_id:
        # Return templates for this profile OR templates with no profile (global)
        query += " AND (profile_id = %s OR profile_id IS NULL)"
        params.append(profile_id)

    if category:
        query += " AND category = %s"
        params.append(category)

    query += " ORDER BY is_builtin DESC, category ASC, name ASC"

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        return rows_to_list(cursor.fetchall())


def create_template(
    template_id: str,
    name: str,
    prompt: str,
    description: Optional[str] = None,
    profile_id: Optional[str] = None,
    icon: Optional[str] = None,
    category: Optional[str] = None,
    is_builtin: bool = False
) -> TemplateRow:
    """Create a new template"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO templates (id, name, description, prompt, profile_id, icon, category, is_builtin, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (template_id, name, description, prompt, profile_id, icon, category, is_builtin, now, now)
        )
    return get_template(template_id)


def update_template(
    template_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    prompt: Optional[str] = None,
    profile_id: Optional[str] = None,
    icon: Optional[str] = None,
    category: Optional[str] = None
) -> Optional[TemplateRow]:
    """Update a template"""
    existing = get_template(template_id)
    if not existing:
        return None

    # Don't allow updating builtin templates
    if existing.get("is_builtin"):
        return None

    updates = []
    values = []

    if name is not None:
        updates.append("name = %s")
        values.append(name)
    if description is not None:
        updates.append("description = %s")
        values.append(description if description else None)
    if prompt is not None:
        updates.append("prompt = %s")
        values.append(prompt)
    if profile_id is not None:
        updates.append("profile_id = %s")
        values.append(profile_id if profile_id else None)
    if icon is not None:
        updates.append("icon = %s")
        values.append(icon if icon else None)
    if category is not None:
        updates.append("category = %s")
        values.append(category if category else None)

    if updates:
        updates.append("updated_at = %s")
        values.append(datetime.now(timezone.utc).isoformat())
        values.append(template_id)

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE templates SET {', '.join(updates)} WHERE id = %s",
                values
            )

    return get_template(template_id)


def delete_template(template_id: str) -> bool:
    """Delete a template"""
    existing = get_template(template_id)
    if not existing:
        return False

    # Don't allow deleting builtin templates
    if existing.get("is_builtin"):
        return False

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM templates WHERE id = %s", (template_id,))
        return cursor.rowcount > 0


def get_template_categories() -> List[str]:
    """Get all unique template categories"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT DISTINCT category FROM templates WHERE category IS NOT NULL ORDER BY category ASC"
        )
        return [row["category"] for row in cursor.fetchall()]


def set_template_builtin(template_id: str, is_builtin: bool) -> bool:
    """Set the is_builtin flag for a template"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE templates SET is_builtin = %s, updated_at = %s WHERE id = %s",
            (is_builtin, datetime.now(timezone.utc).isoformat(), template_id)
        )
        return cursor.rowcount > 0
