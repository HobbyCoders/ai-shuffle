"""Git repository and worktree database operations."""

from __future__ import annotations

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
        cursor.execute("SELECT * FROM git_repositories WHERE id = %s", (repository_id,))
        return row_to_dict(cursor.fetchone())


def get_git_repository_by_project(project_id: str) -> Optional[GitRepositoryRow]:
    """Get a git repository by project ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM git_repositories WHERE project_id = %s", (project_id,))
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
               VALUES (%s, %s, %s, %s, %s, %s)""",
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
        updates.append("remote_url = %s")
        values.append(remote_url if remote_url else None)
    if default_branch is not None:
        updates.append("default_branch = %s")
        values.append(default_branch)
    if github_repo_name is not None:
        updates.append("github_repo_name = %s")
        values.append(github_repo_name if github_repo_name else None)
    if last_synced_at is not None:
        updates.append("last_synced_at = %s")
        values.append(last_synced_at)

    if updates:
        values.append(repository_id)
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE git_repositories SET {', '.join(updates)} WHERE id = %s",
                values
            )

    return get_git_repository(repository_id)


def update_git_repository_synced(repository_id: str) -> Optional[GitRepositoryRow]:
    """Update the last_synced_at timestamp for a git repository"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE git_repositories SET last_synced_at = %s WHERE id = %s",
            (now, repository_id)
        )
    return get_git_repository(repository_id)


def delete_git_repository(repository_id: str) -> bool:
    """Delete a git repository (worktrees will be cascade deleted)"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM git_repositories WHERE id = %s", (repository_id,))
        return cursor.rowcount > 0


# ============================================================================
# Worktree Operations
# ============================================================================

def get_worktree(worktree_id: str) -> Optional[WorktreeRow]:
    """Get a worktree by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM worktrees WHERE id = %s", (worktree_id,))
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
            WHERE s.id = %s
        """, (session_id,))
        result = cursor.fetchone()
        if result:
            return row_to_dict(result)

        # Fallback to deprecated worktrees.session_id for backward compatibility
        cursor.execute("SELECT * FROM worktrees WHERE session_id = %s", (session_id,))
        return row_to_dict(cursor.fetchone())


def get_sessions_for_worktree(worktree_id: str) -> list[dict[str, Any]]:
    """Get all sessions that have used this worktree (current and historical)"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM sessions WHERE worktree_id = %s ORDER BY updated_at DESC",
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
            WHERE w.repository_id = %s
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

        # Collapse rows that point to the SAME on-disk worktree. The worktrees
        # table has no uniqueness constraint, so repeated registration/sync of a
        # branch can leave several rows with distinct ids but an identical
        # worktree_path. Without this, every duplicate renders as its own entry
        # in the worktree picker. Keep one representative per path (newest first,
        # matching the ORDER BY) and union the sessions across the group.
        by_path: Dict[str, Dict] = {}
        for wt in worktrees_map.values():
            key = wt["worktree_path"] or wt["id"]
            rep = by_path.get(key)
            if rep is None:
                by_path[key] = wt
                continue
            seen_ids = {s["id"] for s in rep["sessions"]}
            for session in wt["sessions"]:
                if session["id"] in seen_ids:
                    continue
                rep["sessions"].append(session)
                seen_ids.add(session["id"])
                if session["status"] == "active" and not rep["active_session"]:
                    rep["active_session"] = session
            rep["session_count"] = len(rep["sessions"])

        return list(by_path.values())


def get_worktree_by_branch(repository_id: str, branch_name: str) -> Optional[WorktreeRow]:
    """Get a worktree by repository and branch name"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM worktrees WHERE repository_id = %s AND branch_name = %s",
            (repository_id, branch_name)
        )
        return row_to_dict(cursor.fetchone())


def get_worktrees_for_repository(repository_id: str) -> List[WorktreeRow]:
    """Get all worktrees for a repository"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM worktrees WHERE repository_id = %s ORDER BY created_at DESC",
            (repository_id,)
        )
        return rows_to_list(cursor.fetchall())


def get_active_worktrees_for_repository(repository_id: str) -> List[WorktreeRow]:
    """Get all active worktrees for a repository"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM worktrees WHERE repository_id = %s AND status = 'active' ORDER BY created_at DESC",
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
    """Create a worktree record, or return the existing one for this path.

    The worktrees table has no uniqueness constraint, and several flows
    (session start, sync, recreate) can ask to register the same on-disk
    worktree. A physical worktree is uniquely identified by its path within a
    repository, so reuse an existing row instead of inserting a duplicate —
    otherwise the table accumulates redundant rows that each surface as a
    separate entry in the worktree picker.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM worktrees WHERE repository_id = %s AND worktree_path = %s LIMIT 1",
            (repository_id, worktree_path),
        )
        existing = row_to_dict(cursor.fetchone())
        if existing:
            return existing
        cursor.execute(
            """INSERT INTO worktrees (id, repository_id, session_id, branch_name, worktree_path, base_branch, status, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (worktree_id, repository_id, session_id, branch_name, worktree_path, base_branch, status, now)
        )
    return get_worktree(worktree_id)


def update_worktree(
    worktree_id: str,
    session_id: Optional[str] = None,
    status: Optional[str] = None,
    worktree_path: Optional[str] = None,
    branch_name: Optional[str] = None,
) -> Optional[WorktreeRow]:
    """Update a worktree"""
    existing = get_worktree(worktree_id)
    if not existing:
        return None

    updates = []
    values = []

    if session_id is not None:
        updates.append("session_id = %s")
        values.append(session_id if session_id else None)
    if status is not None:
        updates.append("status = %s")
        values.append(status)
    if worktree_path is not None:
        updates.append("worktree_path = %s")
        values.append(worktree_path)
    if branch_name is not None:
        updates.append("branch_name = %s")
        values.append(branch_name)

    if updates:
        values.append(worktree_id)
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE worktrees SET {', '.join(updates)} WHERE id = %s",
                values
            )

    return get_worktree(worktree_id)


def delete_worktree(worktree_id: str) -> bool:
    """Delete a worktree record"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM worktrees WHERE id = %s", (worktree_id,))
        return cursor.rowcount > 0


def delete_worktrees_for_repository(repository_id: str) -> int:
    """Delete all worktrees for a repository. Returns count of deleted worktrees."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM worktrees WHERE repository_id = %s", (repository_id,))
        return cursor.rowcount
