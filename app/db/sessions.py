"""Session and message database operations."""

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

class SessionRow(TypedDict):
    """Full session row from SELECT * FROM sessions."""
    id: str
    project_id: Optional[str]
    profile_id: Optional[str]
    api_user_id: Optional[str]
    sdk_session_id: Optional[str]
    worktree_id: Optional[str]
    worktree_branch: Optional[str]
    worktree_path: Optional[str]
    title: Optional[str]
    status: str
    is_favorite: bool
    total_cost_usd: float
    total_tokens_in: int
    total_tokens_out: int
    turn_count: int
    created_at: str
    updated_at: str
    parent_session_id: Optional[str]
    fork_point_message_index: Optional[int]


class SessionSearchResult(TypedDict):
    """Session search result with match info."""
    id: str
    project_id: Optional[str]
    profile_id: Optional[str]
    api_user_id: Optional[str]
    title: Optional[str]
    status: str
    total_cost_usd: float
    total_tokens_in: int
    total_tokens_out: int
    turn_count: int
    created_at: str
    updated_at: str
    match_type: str
    match_snippet: Optional[str]
    match_time: Optional[str]


class SessionSearchResultPaginated(TypedDict):
    """Session search result with match info (paginated query includes extra columns)."""
    id: str
    project_id: Optional[str]
    profile_id: Optional[str]
    api_user_id: Optional[str]
    title: Optional[str]
    status: str
    is_favorite: bool
    total_cost_usd: float
    total_tokens_in: int
    total_tokens_out: int
    cache_creation_tokens: Optional[int]
    cache_read_tokens: Optional[int]
    context_tokens: Optional[int]
    turn_count: int
    parent_session_id: Optional[str]
    fork_point_message_index: Optional[int]
    created_at: str
    updated_at: str
    match_type: str
    match_snippet: Optional[str]
    match_time: Optional[str]


class SessionMessageRow(TypedDict):
    """Full session message row from SELECT * FROM session_messages."""
    id: int
    session_id: str
    role: str
    content: str
    tool_name: Optional[str]
    tool_input: Optional[Dict[str, Any]]
    metadata: Optional[Dict[str, Any]]
    profile_id: Optional[str]
    version: int
    created_at: str
    updated_at: str


class SyncLogRow(TypedDict):
    """Sync log entry."""
    id: int
    session_id: str
    event_type: str
    entity_type: str
    entity_id: Optional[str]
    data: Optional[Dict[str, Any]]
    created_at: str


class ForkInfo(TypedDict):
    """Fork relationship info for a session."""
    has_forks: bool
    fork_parent_id: Optional[str]


# ============================================================================
# Session Operations
# ============================================================================

def get_session(session_id: str) -> Optional[SessionRow]:
    """Get a session by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
        return row_to_dict(cursor.fetchone())


def get_session_by_sdk_id(sdk_session_id: str) -> Optional[SessionRow]:
    """Get a session by SDK session ID (the Claude Code session ID)"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM sessions WHERE sdk_session_id = ?", (sdk_session_id,))
        return row_to_dict(cursor.fetchone())


def get_sessions(
    project_id: Optional[str] = None,
    profile_id: Optional[str] = None,
    status: Optional[str] = None,
    api_user_id: Optional[str] = None,
    api_users_only: bool = False,
    favorites_only: bool = False,
    tag_id: Optional[str] = None,
    limit: int = 50,
    offset: int = 0
) -> List[SessionRow]:
    """Get sessions with optional filters"""
    if tag_id:
        # Join with session_tags when filtering by tag
        query = """SELECT DISTINCT s.* FROM sessions s
                   INNER JOIN session_tags st ON s.id = st.session_id
                   WHERE st.tag_id = ?"""
        params = [tag_id]
    else:
        query = "SELECT * FROM sessions WHERE 1=1"
        params = []

    if project_id:
        query += " AND s.project_id = ?" if tag_id else " AND project_id = ?"
        params.append(project_id)
    if profile_id:
        query += " AND s.profile_id = ?" if tag_id else " AND profile_id = ?"
        params.append(profile_id)
    if status:
        query += " AND s.status = ?" if tag_id else " AND status = ?"
        params.append(status)
    if favorites_only:
        query += " AND s.is_favorite = TRUE" if tag_id else " AND is_favorite = TRUE"
    if api_users_only:
        # Filter for sessions that have an API user (exclude admin sessions)
        query += " AND s.api_user_id IS NOT NULL" if tag_id else " AND api_user_id IS NOT NULL"
    elif api_user_id is not None:
        if api_user_id:
            query += " AND s.api_user_id = ?" if tag_id else " AND api_user_id = ?"
            params.append(api_user_id)
        else:
            query += " AND s.api_user_id IS NULL" if tag_id else " AND api_user_id IS NULL"

    query += " ORDER BY s.updated_at DESC LIMIT ? OFFSET ?" if tag_id else " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        return rows_to_list(cursor.fetchall())


def get_sessions_paginated(
    project_id: Optional[str] = None,
    profile_id: Optional[str] = None,
    status: Optional[str] = None,
    api_user_id: Optional[str] = None,
    api_users_only: bool = False,
    favorites_only: bool = False,
    tag_id: Optional[str] = None,
    limit: int = 50,
    offset: int = 0
) -> tuple[List[SessionRow], int]:
    """Get sessions with optional filters, returning items and total count.

    Returns:
        Tuple of (sessions list, total count)
    """
    # Build the WHERE clause (shared between count and data queries)
    if tag_id:
        base_query = """FROM sessions s
                       INNER JOIN session_tags st ON s.id = st.session_id
                       WHERE st.tag_id = ?"""
        params: List[Any] = [tag_id]
        col_prefix = "s."
    else:
        base_query = "FROM sessions WHERE 1=1"
        params = []
        col_prefix = ""

    if project_id:
        base_query += f" AND {col_prefix}project_id = ?"
        params.append(project_id)
    if profile_id:
        base_query += f" AND {col_prefix}profile_id = ?"
        params.append(profile_id)
    if status:
        base_query += f" AND {col_prefix}status = ?"
        params.append(status)
    if favorites_only:
        base_query += f" AND {col_prefix}is_favorite = TRUE"
    if api_users_only:
        base_query += f" AND {col_prefix}api_user_id IS NOT NULL"
    elif api_user_id is not None:
        if api_user_id:
            base_query += f" AND {col_prefix}api_user_id = ?"
            params.append(api_user_id)
        else:
            base_query += f" AND {col_prefix}api_user_id IS NULL"

    with get_db() as conn:
        cursor = conn.cursor()

        # Single query with window function for count + data
        if tag_id:
            # Subquery to get distinct sessions first, then count via window
            data_query = f"""
                WITH filtered AS (
                    SELECT DISTINCT s.* {base_query}
                )
                SELECT *, COUNT(*) OVER() as _total FROM filtered
                ORDER BY updated_at DESC LIMIT ? OFFSET ?
            """
        else:
            data_query = f"""SELECT *, COUNT(*) OVER() as _total
                {base_query} ORDER BY updated_at DESC LIMIT ? OFFSET ?"""

        cursor.execute(data_query, params + [limit, offset])
        rows = cursor.fetchall()

        if not rows:
            return [], 0

        total = rows[0]["_total"]
        sessions = [{k: v for k, v in dict(row).items() if k != "_total"} for row in rows]
        return sessions, total


def create_session(
    session_id: str,
    profile_id: str,
    project_id: Optional[str] = None,
    title: Optional[str] = None,
    api_user_id: Optional[str] = None,
    parent_session_id: Optional[str] = None,
    fork_point_message_index: Optional[int] = None,
    worktree_id: Optional[str] = None,
    worktree_branch: Optional[str] = None,
    worktree_path: Optional[str] = None,
    _conn=None
) -> SessionRow:
    """Create a new session

    Args:
        session_id: Unique session identifier
        profile_id: Agent profile to use
        project_id: Optional project to work in
        title: Optional session title
        api_user_id: Optional API user owner
        parent_session_id: For forked sessions
        fork_point_message_index: Fork point index
        worktree_id: Optional worktree to link this session to
        worktree_branch: Optional branch name (persisted for display after worktree deletion)
        worktree_path: Optional worktree path (persisted for display after worktree deletion)
        _conn: Optional existing DB connection for transaction grouping
    """
    now = datetime.now(timezone.utc).isoformat()
    if _conn is not None:
        cursor = _conn.cursor()
        cursor.execute(
            """INSERT INTO sessions (id, profile_id, project_id, title, api_user_id, parent_session_id, fork_point_message_index, worktree_id, worktree_branch, worktree_path, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, profile_id, project_id, title, api_user_id, parent_session_id, fork_point_message_index, worktree_id, worktree_branch, worktree_path, now, now)
        )
    else:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """INSERT INTO sessions (id, profile_id, project_id, title, api_user_id, parent_session_id, fork_point_message_index, worktree_id, worktree_branch, worktree_path, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (session_id, profile_id, project_id, title, api_user_id, parent_session_id, fork_point_message_index, worktree_id, worktree_branch, worktree_path, now, now)
            )
    return get_session(session_id)


def create_session_with_stats(
    session_id: str,
    profile_id: str,
    project_id: Optional[str] = None,
    title: Optional[str] = None,
    api_user_id: Optional[str] = None,
    sdk_session_id: Optional[str] = None,
    total_cost_usd: float = 0.0,
    total_tokens_in: int = 0,
    total_tokens_out: int = 0,
    turn_count: int = 0,
    created_at: Optional[str] = None
) -> SessionRow:
    """Create a new session with all stats in a single atomic transaction.

    This is used for importing sessions where all metadata is known upfront.
    Using a single transaction ensures no orphaned sessions if any part fails.

    Args:
        session_id: Unique session identifier
        profile_id: Agent profile to use
        project_id: Optional project to work in
        title: Optional session title
        api_user_id: Optional API user owner
        sdk_session_id: Optional Claude SDK session ID for imported sessions
        total_cost_usd: Total cost in USD
        total_tokens_in: Total input tokens
        total_tokens_out: Total output tokens
        turn_count: Number of conversation turns
        created_at: Optional creation timestamp (ISO format), defaults to now
    """
    now = datetime.now(timezone.utc).isoformat()
    ts_created = created_at or now
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO sessions (
                id, profile_id, project_id, title, api_user_id,
                sdk_session_id, total_cost_usd, total_tokens_in, total_tokens_out, turn_count,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, profile_id, project_id, title, api_user_id,
             sdk_session_id, total_cost_usd, total_tokens_in, total_tokens_out, turn_count,
             ts_created, now)
        )
    return get_session(session_id)


def update_session(
    session_id: str,
    sdk_session_id: Optional[str] = None,
    title: Optional[str] = None,
    status: Optional[str] = None,
    cost_increment: float = 0,
    tokens_in_increment: int = 0,
    tokens_out_increment: int = 0,
    turn_increment: int = 0,
    worktree_id: Optional[str] = None
) -> Optional[SessionRow]:
    """Update a session with usage stats

    Args:
        worktree_id: Set to a worktree ID to link, or empty string "" to unlink
    """
    updates = ["updated_at = ?"]
    values = [datetime.now(timezone.utc).isoformat()]

    if sdk_session_id is not None:
        updates.append("sdk_session_id = ?")
        values.append(sdk_session_id)
    if title is not None:
        updates.append("title = ?")
        values.append(title)
    if status is not None:
        updates.append("status = ?")
        values.append(status)
    if cost_increment != 0:
        updates.append("total_cost_usd = total_cost_usd + ?")
        values.append(cost_increment)
    if tokens_in_increment != 0:
        updates.append("total_tokens_in = total_tokens_in + ?")
        values.append(tokens_in_increment)
    if tokens_out_increment != 0:
        updates.append("total_tokens_out = total_tokens_out + ?")
        values.append(tokens_out_increment)
    if turn_increment != 0:
        updates.append("turn_count = turn_count + ?")
        values.append(turn_increment)
    if worktree_id is not None:
        updates.append("worktree_id = ?")
        # Empty string means unlink (set to NULL)
        values.append(worktree_id if worktree_id else None)

    values.append(session_id)

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE sessions SET {', '.join(updates)} WHERE id = ?",
            values
        )

    return get_session(session_id)


def delete_session(session_id: str) -> bool:
    """Delete a session and its messages"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        return cursor.rowcount > 0


def toggle_session_favorite(session_id: str) -> Optional[SessionRow]:
    """Toggle the is_favorite flag for a session and return the updated session"""
    with get_db() as conn:
        cursor = conn.cursor()
        # Toggle the favorite status
        cursor.execute(
            """UPDATE sessions SET is_favorite = NOT is_favorite, updated_at = ?
               WHERE id = ?""",
            (datetime.now(timezone.utc).isoformat(), session_id)
        )
        if cursor.rowcount == 0:
            return None
    return get_session(session_id)


def set_session_favorite(session_id: str, is_favorite: bool) -> Optional[SessionRow]:
    """Set the is_favorite flag for a session and return the updated session"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """UPDATE sessions SET is_favorite = ?, updated_at = ?
               WHERE id = ?""",
            (is_favorite, datetime.now(timezone.utc).isoformat(), session_id)
        )
        if cursor.rowcount == 0:
            return None
    return get_session(session_id)


def session_has_forks(session_id: str) -> bool:
    """Check if a session has any forked children"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) as count FROM sessions WHERE parent_session_id = ?",
            (session_id,)
        )
        row = cursor.fetchone()
        return row["count"] > 0 if row else False


def get_fork_info_for_sessions(session_ids: list[str]) -> dict[str, ForkInfo]:
    """Get fork parent/child info for multiple sessions in a single query.

    Returns {session_id: {'has_forks': bool, 'fork_parent_id': str|None}}
    for all requested session_ids.
    """
    if not session_ids:
        return {}
    placeholders = ','.join('?' * len(session_ids))

    # Initialize defaults
    result: dict[str, ForkInfo] = {
        sid: {'has_forks': False, 'fork_parent_id': None}
        for sid in session_ids
    }

    with get_db() as conn:
        cursor = conn.cursor()

        # Find which sessions have children (i.e., are fork parents)
        cursor.execute(
            f'''SELECT parent_session_id, COUNT(*) as fork_count
                FROM sessions
                WHERE parent_session_id IN ({placeholders})
                GROUP BY parent_session_id''',
            session_ids
        )
        for row in cursor.fetchall():
            parent_id = row['parent_session_id']
            if parent_id in result:
                result[parent_id]['has_forks'] = row['fork_count'] > 0

        # Get fork parent info for sessions that are themselves forks
        cursor.execute(
            f'''SELECT id, parent_session_id
                FROM sessions
                WHERE id IN ({placeholders})
                AND parent_session_id IS NOT NULL''',
            session_ids
        )
        for row in cursor.fetchall():
            result[row['id']]['fork_parent_id'] = row['parent_session_id']

    return result


def get_session_forks(session_id: str) -> List[SessionRow]:
    """Get all forked children of a session"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT * FROM sessions WHERE parent_session_id = ?
               ORDER BY created_at DESC""",
            (session_id,)
        )
        return rows_to_list(cursor.fetchall())


# ============================================================================
# Session Message Operations
# ============================================================================

def get_session_messages(session_id: str) -> List[SessionMessageRow]:
    """Get all messages for a session"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM session_messages WHERE session_id = ? ORDER BY created_at ASC",
            (session_id,)
        )
        rows = rows_to_list(cursor.fetchall())
        for row in rows:
            if row.get("tool_input"):
                row["tool_input"] = json.loads(row["tool_input"]) if isinstance(row["tool_input"], str) else row["tool_input"]
            if row.get("metadata"):
                row["metadata"] = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else row["metadata"]
        return rows


def add_session_message(
    session_id: str,
    role: str,
    content: str,
    tool_name: Optional[str] = None,
    tool_input: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    profile_id: Optional[str] = None,
    _conn=None
) -> SessionMessageRow:
    """Add a message to a session

    Args:
        session_id: The session ID
        role: Message role (user, assistant, system)
        content: Message content
        tool_name: Optional tool name for tool_use messages
        tool_input: Optional tool input for tool_use messages
        metadata: Optional metadata dict
        profile_id: Optional profile ID that created this message (for per-query tracking)
        _conn: Optional existing DB connection for transaction grouping
    """
    now = datetime.now(timezone.utc).isoformat()
    params = (session_id, role, content, tool_name,
              json.dumps(tool_input) if tool_input else None,
              json.dumps(metadata) if metadata else None,
              profile_id,
              now)
    sql = """INSERT INTO session_messages (session_id, role, content, tool_name, tool_input, metadata, profile_id, created_at)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?)"""

    if _conn is not None:
        cursor = _conn.cursor()
        cursor.execute(sql, params)
        lastrowid = cursor.lastrowid
    else:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            lastrowid = cursor.lastrowid

    msg = {
        "id": lastrowid,
        "session_id": session_id,
        "role": role,
        "content": content,
        "tool_name": tool_name,
        "tool_input": tool_input,
        "metadata": metadata,
        "profile_id": profile_id,
        "created_at": now
    }

    # Update in-memory profile map cache (avoids DB re-read on next load)
    if role == "user" and profile_id and content:
        from app.core.cost_utils import update_profile_map
        update_profile_map(session_id, content, profile_id)

    return msg


def add_session_messages_batch(
    messages: List[Dict[str, Any]],
    _conn=None
) -> List[SessionMessageRow]:
    """Insert multiple session messages in a single transaction.

    Each dict in *messages* must have keys: session_id, role, content.
    Optional keys: tool_name, tool_input, metadata, profile_id.

    Returns the list of inserted message dicts (with ids assigned).
    """
    if not messages:
        return []

    now = datetime.now(timezone.utc).isoformat()
    sql = """INSERT INTO session_messages
             (session_id, role, content, tool_name, tool_input, metadata, profile_id, created_at)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?)"""

    results: List[SessionMessageRow] = []

    def _do_insert(conn):
        cursor = conn.cursor()
        for msg in messages:
            tool_input = msg.get("tool_input")
            metadata = msg.get("metadata")
            params = (
                msg["session_id"],
                msg["role"],
                msg["content"],
                msg.get("tool_name"),
                json.dumps(tool_input) if tool_input else None,
                json.dumps(metadata) if metadata else None,
                msg.get("profile_id"),
                now,
            )
            cursor.execute(sql, params)
            results.append({
                "id": cursor.lastrowid,
                "session_id": msg["session_id"],
                "role": msg["role"],
                "content": msg["content"],
                "tool_name": msg.get("tool_name"),
                "tool_input": tool_input,
                "metadata": metadata,
                "profile_id": msg.get("profile_id"),
                "created_at": now,
            })

    if _conn is not None:
        _do_insert(_conn)
    else:
        with get_db() as conn:
            _do_insert(conn)

    return results


def delete_session_message(session_id: str, message_id: int) -> bool:
    """Delete a specific message from a session"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM session_messages WHERE session_id = ? AND id = ?",
            (session_id, message_id)
        )
        return cursor.rowcount > 0


def delete_session_messages_after(session_id: str, message_id: int) -> int:
    """Delete all messages after a specific message ID (for rewind)"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM session_messages WHERE session_id = ? AND id > ?",
            (session_id, message_id)
        )
        return cursor.rowcount


def _fts_query_term(query: str) -> str:
    """Convert a user search query into an FTS5 query string.

    Handles quoting so special FTS5 characters (*, -, etc.) don't cause
    parse errors.  Multi-word queries are joined with implicit AND.
    """
    # Split into words and quote each to prevent FTS5 syntax errors
    words = query.strip().split()
    if not words:
        return '""'
    # Quote each word individually; FTS5 treats adjacent quoted terms as AND
    return " ".join(f'"{w}"' for w in words)


def search_sessions(
    query: str,
    project_id: Optional[str] = None,
    profile_id: Optional[str] = None,
    api_user_id: Optional[str] = None,
    admin_only: bool = False,
    limit: int = 20
) -> List[SessionSearchResult]:
    """
    Search sessions by title and message content.
    Uses FTS5 for content search (fast indexed lookup) with LIKE fallback
    for title search (titles are short, always fast).
    Returns sessions with matching snippets showing where the match was found.
    """
    if not query or not query.strip():
        return []

    search_term = f"%{query.strip()}%"
    fts_query = _fts_query_term(query)

    with get_db() as conn:
        cursor = conn.cursor()

        # Search titles with LIKE (titles are short, always fast)
        # Search content with FTS5 (indexed, handles large message bodies)
        sql = """
        WITH title_matches AS (
            SELECT
                s.id,
                s.project_id,
                s.profile_id,
                s.api_user_id,
                s.title,
                s.status,
                s.total_cost_usd,
                s.total_tokens_in,
                s.total_tokens_out,
                s.turn_count,
                s.created_at,
                s.updated_at,
                'title' as match_type,
                s.title as match_snippet,
                s.updated_at as match_time
            FROM sessions s
            WHERE s.title LIKE ? COLLATE NOCASE
            AND s.status != 'archived'
        ),
        content_matches AS (
            SELECT DISTINCT
                s.id,
                s.project_id,
                s.profile_id,
                s.api_user_id,
                s.title,
                s.status,
                s.total_cost_usd,
                s.total_tokens_in,
                s.total_tokens_out,
                s.turn_count,
                s.created_at,
                s.updated_at,
                'content' as match_type,
                snippet(session_messages_fts, 0, '', '', '...', 20) as match_snippet,
                s.updated_at as match_time
            FROM session_messages_fts fts
            INNER JOIN sessions s ON s.id = fts.session_id
            WHERE session_messages_fts MATCH ?
            AND s.status != 'archived'
        ),
        all_matches AS (
            SELECT * FROM title_matches
            UNION ALL
            SELECT * FROM content_matches
        )
        SELECT
            id,
            project_id,
            profile_id,
            api_user_id,
            title,
            status,
            total_cost_usd,
            total_tokens_in,
            total_tokens_out,
            turn_count,
            created_at,
            updated_at,
            match_type,
            match_snippet,
            match_time
        FROM all_matches
        WHERE 1=1
        """

        params: List[Any] = [search_term, fts_query]

        # Add filters
        if project_id:
            sql += " AND project_id = ?"
            params.append(project_id)
        if profile_id:
            sql += " AND profile_id = ?"
            params.append(profile_id)
        if admin_only:
            sql += " AND api_user_id IS NULL"
        elif api_user_id:
            sql += " AND api_user_id = ?"
            params.append(api_user_id)

        # Group by session to avoid duplicates, keeping the best match
        sql += """
        GROUP BY id
        ORDER BY
            CASE WHEN match_type = 'title' THEN 0 ELSE 1 END,
            updated_at DESC
        LIMIT ?
        """
        params.append(limit)

        cursor.execute(sql, params)
        results = rows_to_list(cursor.fetchall())

        return results


def search_sessions_paginated(
    query: str,
    project_id: Optional[str] = None,
    profile_id: Optional[str] = None,
    api_user_id: Optional[str] = None,
    admin_only: bool = False,
    limit: int = 20,
    offset: int = 0
) -> tuple[List[SessionSearchResultPaginated], int]:
    """
    Search sessions by title and message content with pagination.
    Uses FTS5 for content search (fast indexed lookup) with LIKE fallback
    for title search.

    Returns:
        Tuple of (results list, total count)
    """
    if not query or not query.strip():
        return [], 0

    search_term = f"%{query.strip()}%"
    fts_query = _fts_query_term(query)

    with get_db() as conn:
        cursor = conn.cursor()

        # Build the base CTE using FTS5 for content matching
        base_cte = """
        WITH title_matches AS (
            SELECT
                s.*,
                'title' as match_type,
                s.title as match_snippet,
                s.updated_at as match_time
            FROM sessions s
            WHERE s.title LIKE ? COLLATE NOCASE
            AND s.status != 'archived'
        ),
        content_matches AS (
            SELECT DISTINCT
                s.*,
                'content' as match_type,
                snippet(session_messages_fts, 0, '', '', '...', 20) as match_snippet,
                s.updated_at as match_time
            FROM session_messages_fts fts
            INNER JOIN sessions s ON s.id = fts.session_id
            WHERE session_messages_fts MATCH ?
            AND s.status != 'archived'
        ),
        all_matches AS (
            SELECT * FROM title_matches
            UNION ALL
            SELECT * FROM content_matches
        ),
        grouped_matches AS (
            SELECT *
            FROM all_matches
            WHERE 1=1
        """

        base_params: List[Any] = [search_term, fts_query]

        # Build filter conditions
        filter_sql = ""
        filter_params: List[Any] = []

        if project_id:
            filter_sql += " AND project_id = ?"
            filter_params.append(project_id)
        if profile_id:
            filter_sql += " AND profile_id = ?"
            filter_params.append(profile_id)
        if admin_only:
            filter_sql += " AND api_user_id IS NULL"
        elif api_user_id:
            filter_sql += " AND api_user_id = ?"
            filter_params.append(api_user_id)

        # Count query - count unique session IDs
        count_sql = base_cte + filter_sql + """
            GROUP BY id
        )
        SELECT COUNT(*) FROM grouped_matches
        """
        cursor.execute(count_sql, base_params + filter_params)
        total = cursor.fetchone()[0]

        # Data query with pagination
        data_sql = base_cte + filter_sql + """
            GROUP BY id
            ORDER BY
                CASE WHEN match_type = 'title' THEN 0 ELSE 1 END,
                updated_at DESC
            LIMIT ? OFFSET ?
        )
        SELECT * FROM grouped_matches
        ORDER BY
            CASE WHEN match_type = 'title' THEN 0 ELSE 1 END,
            updated_at DESC
        """
        cursor.execute(data_sql, base_params + filter_params + [limit, offset])
        results = rows_to_list(cursor.fetchall())

        return results, total


# ============================================================================
# Sync Log Operations (for cross-device synchronization)
# ============================================================================

def add_sync_log(
    session_id: str,
    event_type: str,
    entity_type: str,
    entity_id: Optional[str] = None,
    data: Optional[Dict[str, Any]] = None
) -> SyncLogRow:
    """Add an entry to the sync log for polling fallback"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO sync_log (session_id, event_type, entity_type, entity_id, data, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (session_id, event_type, entity_type, entity_id,
             json.dumps(data) if data else None, now)
        )
        return {
            "id": cursor.lastrowid,
            "session_id": session_id,
            "event_type": event_type,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "data": data,
            "created_at": now
        }


def get_sync_logs(
    session_id: str,
    since_id: int = 0,
    limit: int = 100
) -> List[SyncLogRow]:
    """Get sync log entries for a session since a given ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT * FROM sync_log
               WHERE session_id = ? AND id > ?
               ORDER BY id ASC
               LIMIT ?""",
            (session_id, since_id, limit)
        )
        rows = rows_to_list(cursor.fetchall())
        for row in rows:
            if row.get("data"):
                row["data"] = json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
        return rows


def get_latest_sync_id(session_id: str) -> int:
    """Get the latest sync log ID for a session"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT MAX(id) as max_id FROM sync_log WHERE session_id = ?",
            (session_id,)
        )
        row = cursor.fetchone()
        return row["max_id"] or 0


def update_message_content(
    message_id: int,
    content: str,
    metadata: Optional[Dict[str, Any]] = None
) -> Optional[SessionMessageRow]:
    """Update message content (used during streaming)"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()

        updates = ["content = ?", "updated_at = ?", "version = version + 1"]
        values = [content, now]

        if metadata is not None:
            updates.append("metadata = ?")
            values.append(json.dumps(metadata))

        values.append(message_id)

        cursor.execute(
            f"UPDATE session_messages SET {', '.join(updates)} WHERE id = ?",
            values
        )

        if cursor.rowcount > 0:
            cursor.execute("SELECT * FROM session_messages WHERE id = ?", (message_id,))
            row = row_to_dict(cursor.fetchone())
            if row and row.get("metadata"):
                row["metadata"] = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else row["metadata"]
            return row

    return None
