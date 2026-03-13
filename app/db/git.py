"""Git repository and worktree database operations."""

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

class GitRepositoryRow(TypedDict):
    """Full git repository row from SELECT * FROM git_repositories."""
    id: str
    project_id: str
    remote_url: Optional[str]
    default_branch: str
    github_repo_name: Optional[str]
    last_synced_at: Optional[str]
    created_at: str


class WorktreeRow(TypedDict):
    """Full worktree row from SELECT * FROM worktrees."""
    id: str
    repository_id: str
    session_id: Optional[str]
    branch_name: str
    worktree_path: str
    base_branch: Optional[str]
    status: str
    created_at: str


class WorktreeSessionSummary(TypedDict):
    """Session summary nested in worktree data."""
    id: str
    title: Optional[str]
    status: str
    updated_at: str


class WorktreeWithSessions(TypedDict):
    """Worktree with sessions data from get_worktrees_with_sessions."""
    id: str
    repository_id: str
    session_id: Optional[str]
    branch_name: str
    worktree_path: str
    base_branch: Optional[str]
    status: str
    created_at: str
    sessions: List[WorktreeSessionSummary]
    session_count: int
    active_session: Optional[WorktreeSessionSummary]


# ============================================================================
# Git Repository Operations
# ============================================================================

def get_git_repository(repository_id: str) -> Optional[GitRepositoryRow]:
    """Get a git repository by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM git_repositories WHERE id = ?", (repository_id,))
        return row_to_dict(cursor.fetchone())


def get_git_repository_by_project(project_id: str) -> Optional[GitRepositoryRow]:
    """Get a git repository by project ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM git_repositories WHERE project_id = ?", (project_id,))
        return row_to_dict(cursor.fetchone())


def get_all_git_repositories() -> List[GitRepositoryRow]:
    """Get all git repositories"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM git_repositories ORDER BY created_at DESC")
        return rows_to_list(cursor.fetchall())


def create_git_repository(
    repository_id: str,
    project_id: str,
    remote_url: Optional[str] = None,
    default_branch: str = "main",
    github_repo_name: Optional[str] = None
) -> Optional[GitRepositoryRow]:
    """Create a new git repository record"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO git_repositories (id, project_id, remote_url, default_branch, github_repo_name, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (repository_id, project_id, remote_url, default_branch, github_repo_name, now)
        )
    return get_git_repository(repository_id)


def update_git_repository(
    repository_id: str,
    remote_url: Optional[str] = None,
    default_branch: Optional[str] = None,
    github_repo_name: Optional[str] = None,
    last_synced_at: Optional[str] = None
) -> Optional[GitRepositoryRow]:
    """Update a git repository"""
    existing = get_git_repository(repository_id)
    if not existing:
        return None

    updates = []
    values = []

    if remote_url is not None:
        updates.append("remote_url = ?")
        values.append(remote_url if remote_url else None)
    if default_branch is not None:
        updates.append("default_branch = ?")
        values.append(default_branch)
    if github_repo_name is not None:
        updates.append("github_repo_name = ?")
        values.append(github_repo_name if github_repo_name else None)
    if last_synced_at is not None:
        updates.append("last_synced_at = ?")
        values.append(last_synced_at)

    if updates:
        values.append(repository_id)
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE git_repositories SET {', '.join(updates)} WHERE id = ?",
                values
            )

    return get_git_repository(repository_id)


def update_git_repository_synced(repository_id: str) -> Optional[GitRepositoryRow]:
    """Update the last_synced_at timestamp for a git repository"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE git_repositories SET last_synced_at = ? WHERE id = ?",
            (now, repository_id)
        )
    return get_git_repository(repository_id)


def delete_git_repository(repository_id: str) -> bool:
    """Delete a git repository (worktrees will be cascade deleted)"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM git_repositories WHERE id = ?", (repository_id,))
        return cursor.rowcount > 0


# ============================================================================
# Worktree Operations
# ============================================================================

def get_worktree(worktree_id: str) -> Optional[WorktreeRow]:
    """Get a worktree by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM worktrees WHERE id = ?", (worktree_id,))
        return row_to_dict(cursor.fetchone())


def get_worktree_by_session(session_id: str) -> Optional[WorktreeRow]:
    """Get a worktree linked to a session.

    Uses the new sessions.worktree_id relationship (preferred) with fallback
    to the deprecated worktrees.session_id for backward compatibility.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        # First try the new relationship: sessions.worktree_id -> worktrees.id
        cursor.execute("""
            SELECT w.* FROM worktrees w
            INNER JOIN sessions s ON s.worktree_id = w.id
            WHERE s.id = ?
        """, (session_id,))
        result = cursor.fetchone()
        if result:
            return row_to_dict(result)

        # Fallback to deprecated worktrees.session_id for backward compatibility
        cursor.execute("SELECT * FROM worktrees WHERE session_id = ?", (session_id,))
        return row_to_dict(cursor.fetchone())


def get_sessions_for_worktree(worktree_id: str) -> list[dict[str, Any]]:
    """Get all sessions that have used this worktree (current and historical)"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM sessions WHERE worktree_id = ? ORDER BY updated_at DESC",
            (worktree_id,)
        )
        return rows_to_list(cursor.fetchall())


def get_worktrees_with_sessions(repository_id: str) -> List[WorktreeWithSessions]:
    """Get all worktrees for a repository with their sessions in a single query.

    This avoids the N+1 query pattern by fetching all data in one query.
    Returns worktrees with a 'sessions' field containing list of session dicts.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        # Get all worktrees with their sessions using a LEFT JOIN
        cursor.execute("""
            SELECT
                w.id as worktree_id,
                w.repository_id,
                w.session_id as legacy_session_id,
                w.branch_name,
                w.worktree_path,
                w.base_branch,
                w.status,
                w.created_at as worktree_created_at,
                s.id as session_id,
                s.title as session_title,
                s.status as session_status,
                s.updated_at as session_updated_at
            FROM worktrees w
            LEFT JOIN sessions s ON s.worktree_id = w.id
            WHERE w.repository_id = ?
            ORDER BY w.created_at DESC, s.updated_at DESC
        """, (repository_id,))

        rows = cursor.fetchall()

        # Group by worktree
        worktrees_map = {}
        for row in rows:
            wt_id = row["worktree_id"]
            if wt_id not in worktrees_map:
                worktrees_map[wt_id] = {
                    "id": wt_id,
                    "repository_id": row["repository_id"],
                    "session_id": row["legacy_session_id"],  # Deprecated field
                    "branch_name": row["branch_name"],
                    "worktree_path": row["worktree_path"],
                    "base_branch": row["base_branch"],
                    "status": row["status"],
                    "created_at": row["worktree_created_at"],
                    "sessions": [],
                    "session_count": 0,
                    "active_session": None
                }

            # Add session if present
            if row["session_id"]:
                session = {
                    "id": row["session_id"],
                    "title": row["session_title"],
                    "status": row["session_status"],
                    "updated_at": row["session_updated_at"]
                }
                worktrees_map[wt_id]["sessions"].append(session)
                worktrees_map[wt_id]["session_count"] += 1

                if row["session_status"] == "active" and not worktrees_map[wt_id]["active_session"]:
                    worktrees_map[wt_id]["active_session"] = session

        return list(worktrees_map.values())


def get_worktree_by_branch(repository_id: str, branch_name: str) -> Optional[WorktreeRow]:
    """Get a worktree by repository and branch name"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM worktrees WHERE repository_id = ? AND branch_name = ?",
            (repository_id, branch_name)
        )
        return row_to_dict(cursor.fetchone())


def get_worktrees_for_repository(repository_id: str) -> List[WorktreeRow]:
    """Get all worktrees for a repository"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM worktrees WHERE repository_id = ? ORDER BY created_at DESC",
            (repository_id,)
        )
        return rows_to_list(cursor.fetchall())


def get_active_worktrees_for_repository(repository_id: str) -> List[WorktreeRow]:
    """Get all active worktrees for a repository"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM worktrees WHERE repository_id = ? AND status = 'active' ORDER BY created_at DESC",
            (repository_id,)
        )
        return rows_to_list(cursor.fetchall())


def create_worktree(
    worktree_id: str,
    repository_id: str,
    branch_name: str,
    worktree_path: str,
    session_id: Optional[str] = None,
    base_branch: Optional[str] = None,
    status: str = "active"
) -> Optional[WorktreeRow]:
    """Create a new worktree record"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO worktrees (id, repository_id, session_id, branch_name, worktree_path, base_branch, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (worktree_id, repository_id, session_id, branch_name, worktree_path, base_branch, status, now)
        )
    return get_worktree(worktree_id)


def update_worktree(
    worktree_id: str,
    session_id: Optional[str] = None,
    status: Optional[str] = None
) -> Optional[WorktreeRow]:
    """Update a worktree"""
    existing = get_worktree(worktree_id)
    if not existing:
        return None

    updates = []
    values = []

    if session_id is not None:
        updates.append("session_id = ?")
        values.append(session_id if session_id else None)
    if status is not None:
        updates.append("status = ?")
        values.append(status)

    if updates:
        values.append(worktree_id)
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE worktrees SET {', '.join(updates)} WHERE id = ?",
                values
            )

    return get_worktree(worktree_id)


def delete_worktree(worktree_id: str) -> bool:
    """Delete a worktree record"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM worktrees WHERE id = ?", (worktree_id,))
        return cursor.rowcount > 0


def delete_worktrees_for_repository(repository_id: str) -> int:
    """Delete all worktrees for a repository. Returns count of deleted worktrees."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM worktrees WHERE repository_id = ?", (repository_id,))
        return cursor.rowcount
