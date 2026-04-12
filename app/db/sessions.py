"""Session and message database operations."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, TypedDict

from app.db.base import get_db, get_cursor, row_to_dict, rows_to_list, logger
from app.db.compat import ph, sql_ilike


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
        cursor = get_cursor(conn)
        cursor.execute(f"SELECT * FROM sessions WHERE id = {ph()}", (session_id,))
        return row_to_dict(cursor.fetchone())


def get_session_by_sdk_id(sdk_session_id: str) -> Optional[SessionRow]:
    """Get a session by SDK session ID (the Claude Code session ID)"""
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(f"SELECT * FROM sessions WHERE sdk_session_id = {ph()}", (sdk_session_id,))
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
    _ph = ph()
    if tag_id:
        # Join with session_tags when filtering by tag
        query = f"""SELECT DISTINCT s.* FROM sessions s
                   INNER JOIN session_tags st ON s.id = st.session_id
                   WHERE st.tag_id = {_ph}"""
        params = [tag_id]
    else:
        query = "SELECT * FROM sessions WHERE 1=1"
        params = []

    if project_id:
        query += f" AND s.project_id = {_ph}" if tag_id else f" AND project_id = {_ph}"
        params.append(project_id)
    if profile_id:
        query += f" AND s.profile_id = {_ph}" if tag_id else f" AND profile_id = {_ph}"
        params.append(profile_id)
    if status:
        query += f" AND s.status = {_ph}" if tag_id else f" AND status = {_ph}"
        params.append(status)
    if favorites_only:
        query += " AND s.is_favorite = TRUE" if tag_id else " AND is_favorite = TRUE"
    if api_users_only:
        # Filter for sessions that have an API user (exclude admin sessions)
        query += " AND s.api_user_id IS NOT NULL" if tag_id else " AND api_user_id IS NOT NULL"
    elif api_user_id is not None:
        if api_user_id:
            query += f" AND s.api_user_id = {_ph}" if tag_id else f" AND api_user_id = {_ph}"
            params.append(api_user_id)
        else:
            query += " AND s.api_user_id IS NULL" if tag_id else " AND api_user_id IS NULL"

    query += f" ORDER BY s.updated_at DESC LIMIT {_ph} OFFSET {_ph}" if tag_id else f" ORDER BY updated_at DESC LIMIT {_ph} OFFSET {_ph}"
    params.extend([limit, offset])

    with get_db() as conn:
        cursor = get_cursor(conn)
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
    _ph = ph()
    if tag_id:
        base_query = f"""FROM sessions s
                       INNER JOIN session_tags st ON s.id = st.session_id
                       WHERE st.tag_id = {_ph}"""
        params: List[Any] = [tag_id]
        col_prefix = "s."
    else:
        base_query = "FROM sessions WHERE 1=1"
        params = []
        col_prefix = ""

    if project_id:
        base_query += f" AND {col_prefix}project_id = {_ph}"
        params.append(project_id)
    if profile_id:
        base_query += f" AND {col_prefix}profile_id = {_ph}"
        params.append(profile_id)
    if status:
        base_query += f" AND {col_prefix}status = {_ph}"
        params.append(status)
    if favorites_only:
        base_query += f" AND {col_prefix}is_favorite = TRUE"
    if api_users_only:
        base_query += f" AND {col_prefix}api_user_id IS NOT NULL"
    elif api_user_id is not None:
        if api_user_id:
            base_query += f" AND {col_prefix}api_user_id = {_ph}"
            params.append(api_user_id)
        else:
            base_query += f" AND {col_prefix}api_user_id IS NULL"

    with get_db() as conn:
        cursor = get_cursor(conn)

        # Get total count
        if tag_id:
            count_query = f"SELECT COUNT(DISTINCT s.id) as cnt {base_query}"
        else:
            count_query = f"SELECT COUNT(*) as cnt {base_query}"
        cursor.execute(count_query, params)
        row = cursor.fetchone()
        total = row["cnt"]

        # Get paginated data
        if tag_id:
            data_query = f"SELECT DISTINCT s.* {base_query} ORDER BY s.updated_at DESC LIMIT {_ph} OFFSET {_ph}"
        else:
            data_query = f"SELECT * {base_query} ORDER BY updated_at DESC LIMIT {_ph} OFFSET {_ph}"
        cursor.execute(data_query, params + [limit, offset])
        sessions = rows_to_list(cursor.fetchall())

        return sessions, total


def get_sessions_cursor(
    project_id: Optional[str] = None,
    profile_id: Optional[str] = None,
    status: Optional[str] = None,
    api_user_id: Optional[str] = None,
    api_users_only: bool = False,
    favorites_only: bool = False,
    tag_id: Optional[str] = None,
    limit: int = 50,
    cursor: Optional[str] = None,
    cursor_id: Optional[str] = None,
) -> tuple[List[Dict[str, Any]], Optional[str], Optional[str], bool]:
    """Get sessions using cursor-based pagination (keyset pagination).

    Uses (updated_at, id) as the cursor key for deterministic ordering.

    Returns:
        Tuple of (sessions, next_cursor, next_cursor_id, has_more)
    """
    from app.core.config import settings
    _ph = ph()
    # Sessions are instance-scoped; NULL instance_id is treated as shared/legacy.
    instance_id = settings.effective_instance_id
    if tag_id:
        base_query = f"""FROM sessions s
                       INNER JOIN session_tags st ON s.id = st.session_id
                       WHERE st.tag_id = {_ph}
                         AND (s.instance_id = {_ph} OR s.instance_id IS NULL)"""
        params: List[Any] = [tag_id, instance_id]
        col_prefix = "s."
    else:
        base_query = f"FROM sessions WHERE (instance_id = {_ph} OR instance_id IS NULL)"
        params = [instance_id]
        col_prefix = ""

    if project_id:
        base_query += f" AND {col_prefix}project_id = {_ph}"
        params.append(project_id)
    if profile_id:
        base_query += f" AND {col_prefix}profile_id = {_ph}"
        params.append(profile_id)
    if status:
        base_query += f" AND {col_prefix}status = {_ph}"
        params.append(status)
    if favorites_only:
        base_query += f" AND {col_prefix}is_favorite = TRUE"
    if api_users_only:
        base_query += f" AND {col_prefix}api_user_id IS NOT NULL"
    elif api_user_id is not None:
        if api_user_id:
            base_query += f" AND {col_prefix}api_user_id = {_ph}"
            params.append(api_user_id)
        else:
            base_query += f" AND {col_prefix}api_user_id IS NULL"

    # Cursor condition: seek past the last seen (updated_at, id)
    if cursor and cursor_id:
        base_query += f" AND ({col_prefix}updated_at < {_ph} OR ({col_prefix}updated_at = {_ph} AND {col_prefix}id < {_ph}))"
        params.extend([cursor, cursor, cursor_id])

    with get_db() as conn:
        db_cursor = get_cursor(conn)

        # Request limit+1 to detect has_more without a COUNT query
        fetch_limit = limit + 1
        if tag_id:
            data_query = f"SELECT DISTINCT s.* {base_query} ORDER BY s.updated_at DESC, s.id DESC LIMIT {_ph}"
        else:
            data_query = f"SELECT * {base_query} ORDER BY updated_at DESC, id DESC LIMIT {_ph}"
        db_cursor.execute(data_query, params + [fetch_limit])
        rows = rows_to_list(db_cursor.fetchall())

        has_more = len(rows) > limit
        if has_more:
            rows = rows[:limit]

        next_cursor: Optional[str] = None
        next_cursor_id: Optional[str] = None
        if rows and has_more:
            last = rows[-1]
            next_cursor = last["updated_at"]
            next_cursor_id = last["id"]

        return rows, next_cursor, next_cursor_id, has_more


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
    _ph = ph()
    _phs12 = ", ".join([_ph] * 12)
    sql = f"""INSERT INTO sessions (id, profile_id, project_id, title, api_user_id, parent_session_id, fork_point_message_index, worktree_id, worktree_branch, worktree_path, created_at, updated_at)
               VALUES ({_phs12})"""
    params = (session_id, profile_id, project_id, title, api_user_id, parent_session_id, fork_point_message_index, worktree_id, worktree_branch, worktree_path, now, now)
    if _conn is not None:
        cursor = get_cursor(_conn)
        cursor.execute(sql, params)
    else:
        with get_db() as conn:
            cursor = get_cursor(conn)
            cursor.execute(sql, params)
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
    _ph = ph()
    _phs12 = ", ".join([_ph] * 12)
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"""INSERT INTO sessions (
                id, profile_id, project_id, title, api_user_id,
                sdk_session_id, total_cost_usd, total_tokens_in, total_tokens_out, turn_count,
                created_at, updated_at
            ) VALUES ({_phs12})""",
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
    _ph = ph()
    updates = [f"updated_at = {_ph}"]
    values = [datetime.now(timezone.utc).isoformat()]

    if sdk_session_id is not None:
        updates.append(f"sdk_session_id = {_ph}")
        values.append(sdk_session_id)
    if title is not None:
        updates.append(f"title = {_ph}")
        values.append(title)
    if status is not None:
        updates.append(f"status = {_ph}")
        values.append(status)
    if cost_increment != 0:
        updates.append(f"total_cost_usd = total_cost_usd + {_ph}")
        values.append(cost_increment)
    if tokens_in_increment != 0:
        updates.append(f"total_tokens_in = total_tokens_in + {_ph}")
        values.append(tokens_in_increment)
    if tokens_out_increment != 0:
        updates.append(f"total_tokens_out = total_tokens_out + {_ph}")
        values.append(tokens_out_increment)
    if turn_increment != 0:
        updates.append(f"turn_count = turn_count + {_ph}")
        values.append(turn_increment)
    if worktree_id is not None:
        updates.append(f"worktree_id = {_ph}")
        # Empty string means unlink (set to NULL)
        values.append(worktree_id if worktree_id else None)

    values.append(session_id)

    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"UPDATE sessions SET {', '.join(updates)} WHERE id = {_ph}",
            values
        )

    return get_session(session_id)


def delete_session(session_id: str) -> bool:
    """Delete a session and its messages"""
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(f"DELETE FROM sessions WHERE id = {ph()}", (session_id,))
        return cursor.rowcount > 0


def toggle_session_favorite(session_id: str) -> Optional[SessionRow]:
    """Toggle the is_favorite flag for a session and return the updated session"""
    _ph = ph()
    with get_db() as conn:
        cursor = get_cursor(conn)
        # Toggle the favorite status
        cursor.execute(
            f"""UPDATE sessions SET is_favorite = NOT is_favorite, updated_at = {_ph}
               WHERE id = {_ph}""",
            (datetime.now(timezone.utc).isoformat(), session_id)
        )
        if cursor.rowcount == 0:
            return None
    return get_session(session_id)


def set_session_favorite(session_id: str, is_favorite: bool) -> Optional[SessionRow]:
    """Set the is_favorite flag for a session and return the updated session"""
    _ph = ph()
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"""UPDATE sessions SET is_favorite = {_ph}, updated_at = {_ph}
               WHERE id = {_ph}""",
            (is_favorite, datetime.now(timezone.utc).isoformat(), session_id)
        )
        if cursor.rowcount == 0:
            return None
    return get_session(session_id)


def session_has_forks(session_id: str) -> bool:
    """Check if a session has any forked children"""
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"SELECT COUNT(*) as count FROM sessions WHERE parent_session_id = {ph()}",
            (session_id,)
        )
        row = cursor.fetchone()
        if row is None:
            return False
        count = row["count"]
        return count > 0


def get_fork_info_for_sessions(session_ids: list[str]) -> dict[str, ForkInfo]:
    """Get fork parent/child info for multiple sessions in a single query.

    Returns {session_id: {'has_forks': bool, 'fork_parent_id': str|None}}
    for all requested session_ids.
    """
    if not session_ids:
        return {}
    placeholders = ','.join([ph()] * len(session_ids))

    # Initialize defaults
    result: dict[str, ForkInfo] = {
        sid: {'has_forks': False, 'fork_parent_id': None}
        for sid in session_ids
    }

    with get_db() as conn:
        cursor = get_cursor(conn)

        # Find which sessions have children (i.e., are fork parents)
        cursor.execute(
            f'''SELECT parent_session_id, COUNT(*) as fork_count
                FROM sessions
                WHERE parent_session_id IN ({placeholders})
                GROUP BY parent_session_id''',
            session_ids
        )
        for row in cursor.fetchall():
            row = row_to_dict(row)
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
            row = row_to_dict(row)
            result[row['id']]['fork_parent_id'] = row['parent_session_id']

    return result


def get_session_forks(session_id: str) -> List[SessionRow]:
    """Get all forked children of a session"""
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"""SELECT * FROM sessions WHERE parent_session_id = {ph()}
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
        cursor = get_cursor(conn)
        cursor.execute(
            f"SELECT * FROM session_messages WHERE session_id = {ph()} ORDER BY created_at ASC",
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
    _ph = ph()
    _phs8 = ", ".join([_ph] * 8)
    sql = f"""INSERT INTO session_messages (session_id, role, content, tool_name, tool_input, metadata, profile_id, created_at)
             VALUES ({_phs8}) RETURNING id"""

    if _conn is not None:
        cursor = get_cursor(_conn)
        cursor.execute(sql, params)
        lastrowid = cursor.fetchone()["id"]
    else:
        with get_db() as conn:
            cursor = get_cursor(conn)
            cursor.execute(sql, params)
            lastrowid = cursor.fetchone()["id"]

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
    _ph = ph()
    _phs8 = ", ".join([_ph] * 8)
    sql = f"""INSERT INTO session_messages
             (session_id, role, content, tool_name, tool_input, metadata, profile_id, created_at)
             VALUES ({_phs8}) RETURNING id"""

    results: List[SessionMessageRow] = []

    def _do_insert(conn):
        cursor = get_cursor(conn)
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
            lastrowid = cursor.fetchone()["id"]
            results.append({
                "id": lastrowid,
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
    _ph = ph()
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"DELETE FROM session_messages WHERE session_id = {_ph} AND id = {_ph}",
            (session_id, message_id)
        )
        return cursor.rowcount > 0


def delete_session_messages_after(session_id: str, message_id: int) -> int:
    """Delete all messages after a specific message ID (for rewind)"""
    _ph = ph()
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"DELETE FROM session_messages WHERE session_id = {_ph} AND id > {_ph}",
            (session_id, message_id)
        )
        return cursor.rowcount


def _fts_query_term(query: str) -> str:
    """Convert a user search query into a Postgres FTS query string.

    Returns the cleaned query string as-is since plainto_tsquery handles
    tokenization natively.
    """
    cleaned = query.strip()
    if not cleaned:
        return ""
    return cleaned


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
    Uses Postgres tsvector for content search (fast indexed lookup) with ILIKE
    for title search (titles are short, always fast).
    Returns sessions with matching snippets showing where the match was found.
    """
    if not query or not query.strip():
        return []

    from app.core.config import settings
    instance_id = settings.effective_instance_id

    search_term = f"%{query.strip()}%"
    fts_query = _fts_query_term(query)
    _ph = ph()

    with get_db() as conn:
        cursor = get_cursor(conn)

        # Search titles with ILIKE, content with tsvector FTS.
        # Use DISTINCT ON (id) to deduplicate sessions that match in both
        # title and content, keeping the best match (title > content).
        # Sessions are instance-scoped; legacy rows with NULL instance_id
        # remain visible everywhere.
        sql = f"""
        WITH title_matches AS (
            SELECT
                s.id, s.project_id, s.profile_id, s.api_user_id, s.title,
                s.status, s.total_cost_usd, s.total_tokens_in, s.total_tokens_out,
                s.turn_count, s.created_at, s.updated_at,
                'title' as match_type,
                s.title as match_snippet,
                s.updated_at as match_time
            FROM sessions s
            WHERE {sql_ilike('s.title')}
            AND s.status != 'archived'
            AND (s.instance_id = {_ph} OR s.instance_id IS NULL)
        ),
        content_matches AS (
            SELECT DISTINCT ON (s.id)
                s.id, s.project_id, s.profile_id, s.api_user_id, s.title,
                s.status, s.total_cost_usd, s.total_tokens_in, s.total_tokens_out,
                s.turn_count, s.created_at, s.updated_at,
                'content' as match_type,
                ts_headline('english', sm.content, plainto_tsquery('english', {_ph}),
                    'MaxFragments=1,MaxWords=20,MinWords=5') as match_snippet,
                sm.created_at as match_time
            FROM session_messages sm
            INNER JOIN sessions s ON s.id = sm.session_id
            WHERE sm.content_tsv @@ plainto_tsquery('english', {_ph})
            AND sm.role IN ('user', 'assistant')
            AND s.status != 'archived'
            AND (s.instance_id = {_ph} OR s.instance_id IS NULL)
            ORDER BY s.id, sm.created_at DESC
        ),
        all_matches AS (
            SELECT * FROM title_matches
            UNION ALL
            SELECT * FROM content_matches
        )
        SELECT DISTINCT ON (id) *
        FROM all_matches
        WHERE 1=1
        """
        # title ILIKE needs search_term + instance_id, ts_headline needs
        # fts_query, WHERE @@ needs fts_query + instance_id.
        params: List[Any] = [search_term, instance_id, fts_query, fts_query, instance_id]

        # Add filters
        if project_id:
            sql += f" AND project_id = {_ph}"
            params.append(project_id)
        if profile_id:
            sql += f" AND profile_id = {_ph}"
            params.append(profile_id)
        if admin_only:
            sql += " AND api_user_id IS NULL"
        elif api_user_id:
            sql += f" AND api_user_id = {_ph}"
            params.append(api_user_id)

        sql += f"""
        ORDER BY id,
            CASE WHEN match_type = 'title' THEN 0 ELSE 1 END
        LIMIT {_ph}
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
    Uses Postgres tsvector for content search (fast indexed lookup) with ILIKE
    for title search.

    Returns:
        Tuple of (results list, total count)
    """
    if not query or not query.strip():
        return [], 0

    from app.core.config import settings
    instance_id = settings.effective_instance_id

    search_term = f"%{query.strip()}%"
    fts_query = _fts_query_term(query)
    _ph = ph()

    with get_db() as conn:
        cursor = get_cursor(conn)

        # Build the base CTE using Postgres tsvector FTS.
        # Uses DISTINCT ON (id) to deduplicate sessions, preferring title
        # matches over content matches. Sessions are instance-scoped; NULL
        # instance_id is treated as shared/legacy.
        base_cte = f"""
        WITH title_matches AS (
            SELECT
                s.*,
                'title' as match_type,
                s.title as match_snippet,
                s.updated_at as match_time
            FROM sessions s
            WHERE {sql_ilike('s.title')}
            AND s.status != 'archived'
            AND (s.instance_id = {_ph} OR s.instance_id IS NULL)
        ),
        content_matches AS (
            SELECT DISTINCT ON (s.id)
                s.*,
                'content' as match_type,
                ts_headline('english', sm.content, plainto_tsquery('english', {_ph}),
                    'MaxFragments=1,MaxWords=20,MinWords=5') as match_snippet,
                sm.created_at as match_time
            FROM session_messages sm
            INNER JOIN sessions s ON s.id = sm.session_id
            WHERE sm.content_tsv @@ plainto_tsquery('english', {_ph})
            AND sm.role IN ('user', 'assistant')
            AND s.status != 'archived'
            AND (s.instance_id = {_ph} OR s.instance_id IS NULL)
            ORDER BY s.id, sm.created_at DESC
        ),
        all_matches AS (
            SELECT * FROM title_matches
            UNION ALL
            SELECT * FROM content_matches
        ),
        deduped_matches AS (
            SELECT DISTINCT ON (id) *
            FROM all_matches
            WHERE 1=1
        """
        # title ILIKE needs search_term + instance_id, ts_headline needs
        # fts_query, WHERE @@ needs fts_query + instance_id.
        base_params: List[Any] = [search_term, instance_id, fts_query, fts_query, instance_id]

        # Build filter conditions
        filter_sql = ""
        filter_params: List[Any] = []

        if project_id:
            filter_sql += f" AND project_id = {_ph}"
            filter_params.append(project_id)
        if profile_id:
            filter_sql += f" AND profile_id = {_ph}"
            filter_params.append(profile_id)
        if admin_only:
            filter_sql += " AND api_user_id IS NULL"
        elif api_user_id:
            filter_sql += f" AND api_user_id = {_ph}"
            filter_params.append(api_user_id)

        # Count query - count unique deduplicated sessions
        count_sql = base_cte + filter_sql + """
            ORDER BY id, CASE WHEN match_type = 'title' THEN 0 ELSE 1 END
        )
        SELECT COUNT(*) as cnt FROM deduped_matches
        """
        cursor.execute(count_sql, base_params + filter_params)
        row = cursor.fetchone()
        total = row["cnt"]

        # Data query with pagination
        data_sql = base_cte + filter_sql + f"""
            ORDER BY id, CASE WHEN match_type = 'title' THEN 0 ELSE 1 END
        )
        SELECT * FROM deduped_matches
        ORDER BY
            CASE WHEN match_type = 'title' THEN 0 ELSE 1 END,
            updated_at DESC
        LIMIT {_ph} OFFSET {_ph}
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
    _ph = ph()
    _phs6 = ", ".join([_ph] * 6)
    sql = f"""INSERT INTO sync_log (session_id, event_type, entity_type, entity_id, data, created_at)
               VALUES ({_phs6}) RETURNING id"""
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            sql,
            (session_id, event_type, entity_type, entity_id,
             json.dumps(data) if data else None, now)
        )
        lastrowid = cursor.fetchone()["id"]
        return {
            "id": lastrowid,
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
    _ph = ph()
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"""SELECT * FROM sync_log
               WHERE session_id = {_ph} AND id > {_ph}
               ORDER BY id ASC
               LIMIT {_ph}""",
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
        cursor = get_cursor(conn)
        cursor.execute(
            f"SELECT MAX(id) as max_id FROM sync_log WHERE session_id = {ph()}",
            (session_id,)
        )
        row = cursor.fetchone()
        if row is None:
            return 0
        max_id = row["max_id"]
        return max_id or 0


def update_message_content(
    message_id: int,
    content: str,
    metadata: Optional[Dict[str, Any]] = None
) -> Optional[SessionMessageRow]:
    """Update message content (used during streaming)"""
    _ph = ph()
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = get_cursor(conn)

        updates = [f"content = {_ph}", f"updated_at = {_ph}", "version = version + 1"]
        values = [content, now]

        if metadata is not None:
            updates.append(f"metadata = {_ph}")
            values.append(json.dumps(metadata))

        values.append(message_id)

        cursor.execute(
            f"UPDATE session_messages SET {', '.join(updates)} WHERE id = {_ph}",
            values
        )

        if cursor.rowcount > 0:
            cursor.execute(f"SELECT * FROM session_messages WHERE id = {_ph}", (message_id,))
            row = row_to_dict(cursor.fetchone())
            if row and row.get("metadata"):
                row["metadata"] = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else row["metadata"]
            return row

    return None
