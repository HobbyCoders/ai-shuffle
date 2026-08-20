"""Session and message database operations."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any, TypedDict

from app.db.base import get_db, get_cursor, row_to_dict, rows_to_list, logger
from app.db.compat import ph, sql_ilike


# ============================================================================
# Cross-install session sharing
# ============================================================================

def _share_sessions_across_installs() -> bool:
    """True when sessions are shared across every install on a shared database.

    Default ON: a shared-database fleet sees every install's sessions in one
    place. Set the ``cluster_share_sessions`` system setting to a falsey value
    ("off"/"false"/"0") to restore the old per-install isolation.
    """
    from app.db import database
    val = database.get_system_setting("cluster_share_sessions")
    if val is None:
        return True
    return val.strip().lower() not in ("0", "false", "off", "no")


def _instance_filter(_ph: str, col_prefix: str = "") -> tuple[str, list]:
    """SQL fragment + params restricting session rows to this install.

    Returns ``("", [])`` when sessions are shared cluster-wide (the default), so
    the caller's query returns every install's sessions. Otherwise returns an
    " AND (<col>instance_id = ? OR <col>instance_id IS NULL)" fragment plus the
    bound instance id. The fragment is written to slot in after an existing
    WHERE clause (callers use ``WHERE 1=1`` or ``WHERE st.tag_id = ?``).
    """
    if _share_sessions_across_installs():
        return "", []
    from app.core.config import settings
    return (
        f" AND ({col_prefix}instance_id = {_ph} OR {col_prefix}instance_id IS NULL)",
        [settings.effective_instance_id],
    )


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
    cloud_branch: Optional[str]
    automation_id: Optional[str]
    title: Optional[str]
    status: str
    is_active: bool
    total_cost_usd: float
    total_tokens_in: int
    total_tokens_out: int
    turn_count: int
    created_at: str
    updated_at: str
    parent_session_id: Optional[str]
    fork_point_message_index: Optional[int]
    v2_stream: bool
    # Owning install (v40). Stamped at creation — this install's
    # ``effective_instance_id``, or the composer's device pick.
    instance_id: Optional[str]
    # Explicit placement (v61), e.g. ``{"where": "runner:<id>"}``. NULL means the
    # implicit "follows its owner" default; a non-NULL value is a deliberate pin
    # (device pick / /move / /adopt) and is never auto-overridden.
    placement: Optional[Dict[str, Any]]
    working_set_ref: Optional[str]
    context_usage_json: Optional[Dict[str, Any]]
    provider_id: Optional[str]
    # Interaction mode (v74, ruling VM4): 'voice' for an OpenAI Realtime call,
    # NULL/'chat' for a normal chat session.
    mode: Optional[str]
    # List-visibility flag (v75): TRUE keeps the row out of the sessions-list
    # queries (wake-word "command" voice sessions); NULL/FALSE = visible.
    hidden: Optional[bool]


class SessionSearchResult(TypedDict):
    """Session search result with match info."""
    id: str
    project_id: Optional[str]
    profile_id: Optional[str]
    api_user_id: Optional[str]
    title: Optional[str]
    status: str
    is_active: bool
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
    is_active: bool
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
    match_count: int


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
    active_only: bool = False,
    tag_id: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    include_hidden: bool = False
) -> List[SessionRow]:
    """Get sessions with optional filters.

    Hidden sessions (v75 — wake-word "command" voice sessions) are excluded
    unless ``include_hidden`` is True; they stay fetchable via get_session.
    """
    _ph = ph()
    if tag_id:
        # Join with session_tags when filtering by tag
        inst_sql, inst_params = _instance_filter(_ph, "s.")
        query = f"""SELECT DISTINCT s.* FROM sessions s
                   INNER JOIN session_tags st ON s.id = st.session_id
                   WHERE st.tag_id = {_ph}{inst_sql}"""
        params = [tag_id, *inst_params]
    else:
        inst_sql, inst_params = _instance_filter(_ph, "")
        query = f"SELECT * FROM sessions WHERE 1=1{inst_sql}"
        params = [*inst_params]

    if project_id:
        query += f" AND s.project_id = {_ph}" if tag_id else f" AND project_id = {_ph}"
        params.append(project_id)
    if profile_id:
        query += f" AND s.profile_id = {_ph}" if tag_id else f" AND profile_id = {_ph}"
        params.append(profile_id)
    if status:
        query += f" AND s.status = {_ph}" if tag_id else f" AND status = {_ph}"
        params.append(status)
    if active_only:
        query += " AND s.is_active = TRUE" if tag_id else " AND is_active = TRUE"
    if api_users_only:
        # Filter for sessions that have an API user (exclude admin sessions)
        query += " AND s.api_user_id IS NOT NULL" if tag_id else " AND api_user_id IS NOT NULL"
    elif api_user_id is not None:
        if api_user_id:
            query += f" AND s.api_user_id = {_ph}" if tag_id else f" AND api_user_id = {_ph}"
            params.append(api_user_id)
        else:
            query += " AND s.api_user_id IS NULL" if tag_id else " AND api_user_id IS NULL"
    if not include_hidden:
        # NULL = visible (every pre-v75 row), so coalesce rather than = FALSE.
        query += " AND COALESCE(s.hidden, FALSE) = FALSE" if tag_id else " AND COALESCE(hidden, FALSE) = FALSE"

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
    active_only: bool = False,
    tag_id: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    include_hidden: bool = False
) -> tuple[List[SessionRow], int]:
    """Get sessions with optional filters, returning items and total count.

    Hidden sessions (v75) are excluded from BOTH the items and the total
    unless ``include_hidden`` is True — the filter lives in the shared WHERE
    clause so hidden rows never inflate the count.

    Returns:
        Tuple of (sessions list, total count)
    """
    # Build the WHERE clause (shared between count and data queries)
    _ph = ph()
    if tag_id:
        inst_sql, inst_params = _instance_filter(_ph, "s.")
        base_query = f"""FROM sessions s
                       INNER JOIN session_tags st ON s.id = st.session_id
                       WHERE st.tag_id = {_ph}{inst_sql}"""
        params: List[Any] = [tag_id, *inst_params]
        col_prefix = "s."
    else:
        inst_sql, inst_params = _instance_filter(_ph, "")
        base_query = f"FROM sessions WHERE 1=1{inst_sql}"
        params = [*inst_params]
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
    if active_only:
        base_query += f" AND {col_prefix}is_active = TRUE"
    if api_users_only:
        base_query += f" AND {col_prefix}api_user_id IS NOT NULL"
    elif api_user_id is not None:
        if api_user_id:
            base_query += f" AND {col_prefix}api_user_id = {_ph}"
            params.append(api_user_id)
        else:
            base_query += f" AND {col_prefix}api_user_id IS NULL"
    if not include_hidden:
        # NULL = visible (every pre-v75 row). Shared by the COUNT query below,
        # so hidden rows never inflate the total.
        base_query += f" AND COALESCE({col_prefix}hidden, FALSE) = FALSE"

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
    active_only: bool = False,
    tag_id: Optional[str] = None,
    limit: int = 50,
    cursor: Optional[str] = None,
    cursor_id: Optional[str] = None,
    include_hidden: bool = False,
) -> tuple[List[Dict[str, Any]], Optional[str], Optional[str], bool]:
    """Get sessions using cursor-based pagination (keyset pagination).

    Uses (updated_at, id) as the cursor key for deterministic ordering.

    Hidden sessions (v75) are excluded unless ``include_hidden`` is True —
    the filter is part of the shared WHERE clause, so the limit+1 has_more
    probe never counts hidden rows.

    Returns:
        Tuple of (sessions, next_cursor, next_cursor_id, has_more)
    """
    _ph = ph()
    if tag_id:
        inst_sql, inst_params = _instance_filter(_ph, "s.")
        base_query = f"""FROM sessions s
                       INNER JOIN session_tags st ON s.id = st.session_id
                       WHERE st.tag_id = {_ph}{inst_sql}"""
        params: List[Any] = [tag_id, *inst_params]
        col_prefix = "s."
    else:
        inst_sql, inst_params = _instance_filter(_ph, "")
        base_query = f"FROM sessions WHERE 1=1{inst_sql}"
        params = [*inst_params]
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
    if active_only:
        base_query += f" AND {col_prefix}is_active = TRUE"
    if api_users_only:
        base_query += f" AND {col_prefix}api_user_id IS NOT NULL"
    elif api_user_id is not None:
        if api_user_id:
            base_query += f" AND {col_prefix}api_user_id = {_ph}"
            params.append(api_user_id)
        else:
            base_query += f" AND {col_prefix}api_user_id IS NULL"
    if not include_hidden:
        # NULL = visible (every pre-v75 row).
        base_query += f" AND COALESCE({col_prefix}hidden, FALSE) = FALSE"

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
            last_updated = last["updated_at"]
            next_cursor = last_updated.isoformat() if hasattr(last_updated, "isoformat") else last_updated
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
    automation_id: Optional[str] = None,
    *,
    is_active: bool = False,
    provider_id: Optional[str] = None,
    mode: Optional[str] = None,
    hidden: bool = False,
    target_instance_id: Optional[str] = None,
    project_v2_id: Optional[str] = None,
    _conn=None
) -> SessionRow:
    """Create a new session

    Every session is created v2_stream=TRUE: the V2 event log is the only
    streaming path, so there is no non-V2 creation mode to distinguish.

    Args:
        session_id: Unique session identifier
        profile_id: Agent profile to use
        project_id: Optional legacy project to work in. NULL is a valid INSERT
            when composing by ``project_v2_id`` alone (no local ``projects`` row).
        title: Optional session title
        api_user_id: Optional API user owner
        parent_session_id: For forked sessions
        fork_point_message_index: Fork point index
        worktree_id: Optional worktree to link this session to
        worktree_branch: Optional branch name (persisted for display after worktree deletion)
        worktree_path: Optional worktree path (persisted for display after worktree deletion)
        automation_id: Automation that created this session (NULL = user/API session)
        project_v2_id: Optional origin-keyed project. Stamped at create so a
            remote-only compose is a DIRECT resolver hit on the first send.
        is_active: Whether the session starts in the Active group (user-initiated
            sessions are created active; imported/background ones are not)
        provider_id: Explicit model_providers binding for this session. When
            supplied it WINS over the profile-derived value below — the caller
            is binding the session to a provider the profile knows nothing
            about (ProviderAgent cross-provider workers, phase-3 chunk P3-A).
            ``None`` (or "") keeps the pre-existing profile-derived behaviour
            exactly.
        mode: Interaction mode for this session (ruling VM4) — ``'voice'`` for an
            OpenAI Realtime voice call. ``None`` (the default, and every
            pre-existing row) means chat; the API model coalesces NULL to
            ``'chat'``.
        hidden: Keep this session out of the sessions-list queries (v75) —
            wake-word "command" voice sessions are born hidden so they never
            flash into "recent sessions". List-visibility only: the row is
            still fetchable by id.
        target_instance_id: Create this session OWNED BY another install (the
            composer's device picker). The row is stamped with that install as
            ``instance_id`` AND with an EXPLICIT sticky ``placement`` pin
            (``{"where": "runner:<id>"}``), exactly like
            :func:`pin_session_placement` writes — the user chose this device,
            so the choice must survive sends from any other device (an implicit
            NULL-placement pin would be auto-adoptable). ``None`` (the default)
            keeps the pre-existing behaviour byte-for-byte: this install owns
            the row and ``placement`` stays NULL.
        _conn: Optional existing DB connection for transaction grouping
    """
    from app.core.config import settings
    now = datetime.now(timezone.utc).isoformat()
    target_instance_id = (target_instance_id or "").strip() or None
    project_v2_id = (project_v2_id or "").strip() or None
    instance_id = target_instance_id or settings.effective_instance_id
    # Per-session provider binding (Chunk E / G2-r): resolve the profile's
    # configured provider ONCE, at creation, and stamp it into
    # sessions.provider_id. Every subsequent turn/spawn reads THIS binding (see
    # query_options.build_options_from_profile), so editing the profile later
    # never reroutes a live session. NULL = native Claude (gateway bypass, F1);
    # a native profile (no config.provider) and an unresolvable profile both
    # stay NULL.
    #
    # An EXPLICIT ``provider_id`` argument short-circuits that resolution and
    # wins (P3-A): a ProviderAgent worker session is bound to the provider the
    # tool call named, not to whatever its (inherited) profile happens to
    # configure. With no argument the code below is byte-identical to the
    # pre-parameter behaviour.
    resolved_provider_id = (provider_id or "").strip() or None
    if resolved_provider_id is None:
        try:
            from app.db.profiles import get_profile
            _profile = get_profile(profile_id)
            if _profile:
                resolved_provider_id = (_profile.get("config") or {}).get("provider") or None
        except Exception:  # noqa: BLE001 — binding is best-effort; NULL = native
            resolved_provider_id = None
    resolved_mode = (mode or "").strip() or None
    _ph = ph()
    columns = ["id", "profile_id", "project_id", "project_v2_id", "title", "api_user_id", "parent_session_id", "fork_point_message_index", "worktree_id", "worktree_branch", "worktree_path", "automation_id", "instance_id", "is_active", "v2_stream", "created_at", "updated_at", "provider_id", "mode", "hidden"]
    params = [session_id, profile_id, project_id, project_v2_id, title, api_user_id, parent_session_id, fork_point_message_index, worktree_id, worktree_branch, worktree_path, automation_id, instance_id, is_active, True, now, now, resolved_provider_id, resolved_mode, hidden]
    if target_instance_id:
        # Serialized to a JSON string (not a dict) so the SQLite test shim, which
        # stores JSONB columns as TEXT, round-trips identically to Postgres —
        # same handling as pin_session_placement / adopt_session.
        columns.append("placement")
        params.append(json.dumps({"where": f"runner:{target_instance_id}"}))
    sql = f"""INSERT INTO sessions ({", ".join(columns)})
               VALUES ({", ".join([_ph] * len(columns))})"""
    params = tuple(params)
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
    from app.core.config import settings
    now = datetime.now(timezone.utc).isoformat()
    ts_created = created_at or now
    instance_id = settings.effective_instance_id
    _ph = ph()
    _phs13 = ", ".join([_ph] * 13)
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"""INSERT INTO sessions (
                id, profile_id, project_id, title, api_user_id,
                sdk_session_id, total_cost_usd, total_tokens_in, total_tokens_out, turn_count,
                instance_id, created_at, updated_at
            ) VALUES ({_phs13})""",
            (session_id, profile_id, project_id, title, api_user_id,
             sdk_session_id, total_cost_usd, total_tokens_in, total_tokens_out, turn_count,
             instance_id, ts_created, now)
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
    worktree_id: Optional[str] = None,
    last_input_tokens: Optional[int] = None,
    cache_creation_tokens: Optional[int] = None,
    cache_read_tokens: Optional[int] = None,
    launched_model: Optional[str] = None,
    v2_stream: Optional[bool] = None,
) -> Optional[SessionRow]:
    """Update a session with usage stats

    Args:
        sdk_session_id: Set to an SDK session ID, or empty string "" to clear (NULL)
        worktree_id: Set to a worktree ID to link, or empty string "" to unlink
        last_input_tokens / cache_creation_tokens / cache_read_tokens: absolute
            per-turn context-window values for the latest turn. These overwrite
            (not increment) so the context bar reflects the most recent turn.
        launched_model: Effective model the session was launched (or switched)
            with, persisted at spawn/link time so the host-op guard can read it
            without waiting for turn-end message commits.
    """
    _ph = ph()
    updates = [f"updated_at = {_ph}"]
    values = [datetime.now(timezone.utc).isoformat()]

    if sdk_session_id is not None:
        updates.append(f"sdk_session_id = {_ph}")
        # Empty string means clear (set to NULL) — used after a full rewind
        # empties the transcript, so the next turn starts a fresh SDK session
        # instead of resuming a conversation that no longer exists.
        values.append(sdk_session_id if sdk_session_id else None)
    if title is not None:
        updates.append(f"title = {_ph}")
        values.append(title)
    if status is not None:
        updates.append(f"status = {_ph}")
        values.append(status)
    # Truthiness (not `!= 0`) on purpose: a None increment passes `!= 0` and
    # emits `col = col + NULL`, which NULLs the column for good — that is what
    # broke GET /api/v1/sessions/cursor (pydantic float validation on
    # total_cost_usd) once a provider started reporting None usage/cost.
    if cost_increment:
        updates.append(f"total_cost_usd = total_cost_usd + {_ph}")
        values.append(cost_increment)
    if tokens_in_increment:
        updates.append(f"total_tokens_in = total_tokens_in + {_ph}")
        values.append(tokens_in_increment)
    if tokens_out_increment:
        updates.append(f"total_tokens_out = total_tokens_out + {_ph}")
        values.append(tokens_out_increment)
    if turn_increment:
        updates.append(f"turn_count = turn_count + {_ph}")
        values.append(turn_increment)
    if worktree_id is not None:
        updates.append(f"worktree_id = {_ph}")
        # Empty string means unlink (set to NULL)
        values.append(worktree_id if worktree_id else None)
    if last_input_tokens is not None:
        updates.append(f"last_input_tokens = {_ph}")
        values.append(last_input_tokens)
    if cache_creation_tokens is not None:
        updates.append(f"cache_creation_tokens = {_ph}")
        values.append(cache_creation_tokens)
    if cache_read_tokens is not None:
        updates.append(f"cache_read_tokens = {_ph}")
        values.append(cache_read_tokens)
    if launched_model is not None:
        updates.append(f"launched_model = {_ph}")
        values.append(launched_model)
    if v2_stream is not None:
        updates.append(f"v2_stream = {_ph}")
        values.append(bool(v2_stream))

    values.append(session_id)

    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"UPDATE sessions SET {', '.join(updates)} WHERE id = {_ph}",
            values
        )

    return get_session(session_id)


def set_session_context_usage(session_id: str, data: Dict[str, Any]) -> None:
    """Persist the latest SDK context-window usage breakdown for a session.

    ``data`` is the raw SDK ``get_context_usage()`` payload. It is wrapped as
    ``{"captured_at": <iso>, "data": <data>}`` and stored in the nullable
    ``sessions.context_usage_json`` JSONB column, overwriting any prior
    snapshot. Serialized to a JSON string so the SQLite test shim (which stores
    JSONB columns as TEXT) round-trips identically to Postgres.
    """
    _ph = ph()
    wrapped = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "data": data,
    }
    payload_json = json.dumps(wrapped)
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"UPDATE sessions SET context_usage_json = {_ph} WHERE id = {_ph}",
            (payload_json, session_id),
        )


def get_session_context_usage_snapshot(session_id: str) -> Optional[Dict[str, Any]]:
    """Read the persisted context-usage snapshot for a session.

    Returns the wrapped ``{"captured_at": <iso>, "data": {...}}`` dict, or
    ``None`` when the column is empty / the session does not exist. Decodes the
    JSONB value (Postgres returns a dict; the SQLite test shim stores TEXT).
    """
    _ph = ph()
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"SELECT context_usage_json FROM sessions WHERE id = {_ph}",
            (session_id,),
        )
        row = cursor.fetchone()
    if not row:
        return None
    raw = row["context_usage_json"] if not isinstance(row, (tuple, list)) else row[0]
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None
    return raw


def adopt_session(session_id: str, instance_id: str) -> Optional[SessionRow]:
    """Re-point a session's owning install to ``instance_id`` (cross-device adopt).

    Backs the sessions-board "Resume here" action: on a shared database a
    session created by another install can be taken over locally by rewriting
    its ``instance_id`` to this install's id. V2 Phase 4 (A12): the same UPDATE
    also dual-writes ``placement = {"where": "runner:<instance_id>"}`` —
    unconditionally, matching the unconditional ``instance_id`` rewrite and
    applying even to ``"anywhere"`` sessions, because adopt MEANS "resume
    locally". Without this, the v61 backfill's ``runner:<old-owner>`` pin would
    survive the adopt and the placement resolver would keep routing turns to
    the OLD owner. ``sdk_session_id`` / ``worktree_id`` / ``worktree_branch``
    stay put so history resumes and the existing ``recreate_worktree``
    adopt-on-disk path (worktree_manager) rebinds any worktree; ``cloud_branch``
    is deliberately NEVER written (OD-9 amendment). ``updated_at`` IS bumped so
    the adopted session surfaces at the top of the board on the new device.
    Returns the updated row, or ``None`` if no such session exists.

    The push-direction ``POST /sessions/{id}/move`` ("Move to device…") issues
    the SAME write with a peer's install id instead of the local one — the
    ownership + placement dual-write is identical whichever direction the user
    drives it from.
    """
    _ph = ph()
    now = datetime.now(timezone.utc).isoformat()
    placement_json = json.dumps({"where": f"runner:{instance_id}"})
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"UPDATE sessions SET instance_id = {_ph}, placement = {_ph}, "
            f"updated_at = {_ph} WHERE id = {_ph}",
            (instance_id, placement_json, now, session_id),
        )
        if cursor.rowcount == 0:
            return None
    return get_session(session_id)


def adopt_session_follow(session_id: str, instance_id: str) -> Optional[SessionRow]:
    """Take over a session's ownership WITHOUT pinning it (send-path auto-adopt).

    The quiet counterpart of :func:`adopt_session`: the sending install becomes
    the owner (``instance_id``) so the turn runs here, but ``placement`` is
    deliberately LEFT ALONE. Callers only ever reach this for a session whose
    placement is already NULL — an IMPLICIT A11 pin — so not writing one keeps
    the session implicitly pinned to whoever last sent from it, i.e. still
    auto-adoptable by the next device. Writing a ``runner:<id>`` pin here would
    silently upgrade a "follows me" session into a sticky one.

    ``sdk_session_id`` / ``worktree_*`` / ``cloud_branch`` stay untouched exactly
    like ``adopt_session`` (history resumes; OD-9 keeps ``cloud_branch`` unwritten).
    ``updated_at`` IS bumped, mirroring adopt. Returns the updated row, or
    ``None`` if no such session exists.
    """
    _ph = ph()
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"UPDATE sessions SET instance_id = {_ph}, updated_at = {_ph} "
            f"WHERE id = {_ph}",
            (instance_id, now, session_id),
        )
        if cursor.rowcount == 0:
            return None
    return get_session(session_id)


def pin_session_placement(session_id: str, runner_id: str) -> bool:
    """Sticky-pin a session to ``runner_id`` (V2 Phase 4, OD-6 + OD-9).

    One UPDATE writes both halves of the dual-write: ``placement`` becomes the
    canonical ``{"where": "runner:<id>"}`` pin AND the legacy ``instance_id``
    owner column is re-pointed so every un-migrated reader (board filters,
    relay routing while the flag is off elsewhere) agrees with the new truth.
    ``cloud_branch`` is deliberately NEVER written (OD-9 amendment).

    Called by the FIRST winner of ``start_turn`` on an "anywhere"/capability
    placement — after this the session is runner-pinned and stays put (CLI
    state now lives on that runner's disk). Any capability ``constraints`` in
    the prior placement are INTENTIONALLY dropped at pin time (rF3): per OD-6 a
    pin means "the CLI conversation state lives here", and per A10 pins are
    validated by online + capability only at resolve time — capability
    re-validation does not apply to pins, so carrying the constraints forward
    would add nothing and could wedge a session whose winner later loses an
    incidental capability. ``updated_at`` is not bumped: a claim is not a
    user-visible edit and must not resort the board. Returns True if a row was
    updated.
    """
    _ph = ph()
    placement_json = json.dumps({"where": f"runner:{runner_id}"})
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"UPDATE sessions SET placement = {_ph}, instance_id = {_ph} "
            f"WHERE id = {_ph}",
            (placement_json, runner_id, session_id),
        )
        return cursor.rowcount > 0


def set_session_project(session_id: str, project_id: str) -> Optional[SessionRow]:
    """Re-point a session at a different ``project_id`` (cross-device rebind).

    On a shared database a session's project row is machine-specific; when the
    session is adopted / resumed on an install that only has its OWN clone of
    the same repo (a link-group sibling), the worktree/cwd resolution rebinds
    onto that local project and records the swap here so subsequent resolution
    is stable. Deliberately kept separate from the stats-oriented
    :func:`update_session` — this is a targeted column write that bumps
    ``updated_at`` and nothing else. Returns the updated row, or ``None`` when
    no such session exists.
    """
    _ph = ph()
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"UPDATE sessions SET project_id = {_ph}, updated_at = {_ph} WHERE id = {_ph}",
            (project_id, now, session_id),
        )
        if cursor.rowcount == 0:
            return None
    return get_session(session_id)


def set_session_branch(
    session_id: str, branch: Optional[str], project_v2_id: Optional[str] = None
) -> Optional[SessionRow]:
    """Bind a session to a logical ``branch`` (and optionally its origin-keyed
    ``project_v2_id``) — the v2 branch-intake path.

    Replaces the legacy ``worktrees`` row + md5-hash dir for a NEW branch session:
    the session records only its logical ``sessions.branch`` and ``project_v2_id``;
    the deterministic per-runner checkout is materialized lazily by the resolver
    at spawn (``project_resolver.resolve_cwd`` → ``checkout_manager.ensure_checkout``).
    ``worktree_id``/``worktree_branch``/``worktree_path`` are left NULL. A targeted
    column write (like :func:`set_session_project`), not a stats update. Returns the
    updated row, or ``None`` when no such session exists.
    """
    _ph = ph()
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = get_cursor(conn)
        if project_v2_id is not None:
            cursor.execute(
                f"UPDATE sessions SET branch = {_ph}, project_v2_id = {_ph}, "
                f"updated_at = {_ph} WHERE id = {_ph}",
                (branch, project_v2_id, now, session_id),
            )
        else:
            cursor.execute(
                f"UPDATE sessions SET branch = {_ph}, updated_at = {_ph} WHERE id = {_ph}",
                (branch, now, session_id),
            )
        if cursor.rowcount == 0:
            return None
    return get_session(session_id)


def set_session_parked_task_kind(session_id: str, kind: Optional[str]) -> bool:
    """Persist (or clear, with ``None``) the durable parked marker.

    Non-NULL means the parent turn ended while background work was still
    running — the session is "parked" awaiting an unpark follow-up (``kind``
    is the task kind pinning it, e.g. ``"agent"`` or ``"shell"``). Written at
    turn end by ``turn_finalize.persist_turn_end_state``, cleared at the next
    turn start, and force-cleared at boot by
    ``app.core.startup_reconcile`` since no background work survives a server
    restart. Deliberately does NOT touch ``updated_at`` — a park/unpark marker
    write must not reorder the sessions list. Returns True if a row was
    updated.
    """
    _ph = ph()
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"UPDATE sessions SET parked_task_kind = {_ph} WHERE id = {_ph}",
            (kind, session_id),
        )
        return cursor.rowcount > 0


def set_working_set_ref(session_id: str, value: Optional[str]) -> bool:
    """Persist (or clear, with ``None``) the session's working-set scratch-ref
    pointer (V2 Phase 4, Q6).

    Non-NULL is a JSON blob (``{"ref", "sha", "runner_id"}``) recording the
    dangling stash the last runner pushed to ``origin`` for this session;
    ``None`` clears it once a restore consumes the changes. Like
    :func:`set_session_parked_task_kind` this deliberately does NOT touch
    ``updated_at`` — a working-set pointer write is machinery, not a
    user-visible edit, and must not reorder the sessions board. Returns True if
    a row was updated."""
    _ph = ph()
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"UPDATE sessions SET working_set_ref = {_ph} WHERE id = {_ph}",
            (value, session_id),
        )
        return cursor.rowcount > 0


def delete_session(session_id: str) -> bool:
    """Delete a session and its messages"""
    # V2 Phase 4 (Q6): best-effort scratch-ref cleanup before the row dies, so a
    # hard-deleted session leaves no dangling working-set ref on origin. Fully
    # flag-gated and failure-swallowed — flag OFF (or no ref) issues ZERO git
    # commands, and any git failure never blocks the delete.
    _cleanup_working_set_ref(session_id)
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(f"DELETE FROM sessions WHERE id = {ph()}", (session_id,))
        return cursor.rowcount > 0


def _cleanup_working_set_ref(session_id: str) -> None:
    """Delete the session's working-set scratch ref (local + origin) on hard
    delete. No-op unless the row actually carries a ref, so sessions without
    one make no new git calls. Swallows everything."""
    try:
        row = get_session(session_id)
        if not row or not row.get("working_set_ref"):
            return
        from app.core import project_resolver
        from app.core import working_set

        cwd = project_resolver.resolve_existing_cwd(row)
        if not cwd:
            return
        working_set.delete_scratch_ref(session_id, Path(cwd))
    except Exception:  # pragma: no cover - best-effort cleanup
        logger.debug(
            "sessions: working-set ref cleanup failed for %s",
            session_id, exc_info=True,
        )


def set_session_active(session_id: str, is_active: bool) -> Optional[SessionRow]:
    """Set the is_active flag for a session and return the updated session"""
    _ph = ph()
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"""UPDATE sessions SET is_active = {_ph}, updated_at = {_ph}
               WHERE id = {_ph}""",
            (is_active, datetime.now(timezone.utc).isoformat(), session_id)
        )
        if cursor.rowcount == 0:
            return None
    return get_session(session_id)


def set_session_hidden(session_id: str, hidden: bool) -> Optional[SessionRow]:
    """Set the list-visibility flag (v75) for a session and return the updated
    session. Hidden sessions drop out of the list queries but stay fully
    functional and fetchable by id."""
    _ph = ph()
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"""UPDATE sessions SET hidden = {_ph}, updated_at = {_ph}
               WHERE id = {_ph}""",
            (hidden, datetime.now(timezone.utc).isoformat(), session_id)
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
            f"SELECT * FROM session_messages WHERE session_id = {ph()} ORDER BY created_at ASC, id ASC",
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
    jsonl_uuid: Optional[str] = None,
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
        jsonl_uuid: Optional Claude SDK JSONL entry uuid. Persisting this
            enables rewind to truncate DB rows and JSONL entries together.
            For user rows written before the SDK echoes them, leave this
            None and backfill later via ``set_message_jsonl_uuid``.
        _conn: Optional existing DB connection for transaction grouping
    """
    now = datetime.now(timezone.utc).isoformat()
    params = (session_id, role, content, tool_name,
              json.dumps(tool_input) if tool_input else None,
              json.dumps(metadata) if metadata else None,
              profile_id,
              jsonl_uuid,
              now)
    _ph = ph()
    _phs9 = ", ".join([_ph] * 9)
    sql = f"""INSERT INTO session_messages
             (session_id, role, content, tool_name, tool_input, metadata, profile_id, jsonl_uuid, created_at)
             VALUES ({_phs9}) RETURNING id"""

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
        "jsonl_uuid": jsonl_uuid,
        "created_at": now
    }

    # Update in-memory profile map cache (avoids DB re-read on next load)
    if role == "user" and profile_id and content:
        from app.core.cost_utils import update_profile_map
        update_profile_map(session_id, content, profile_id)

    # Same for the metadata map cache — every user row, even metadata-less
    # ones, so the per-content queues stay aligned with the DB rows.
    if role == "user" and content:
        from app.core.cost_utils import update_user_metadata_map
        update_user_metadata_map(session_id, content, metadata)

    return msg


def add_session_messages_batch(
    messages: List[Dict[str, Any]],
    _conn=None
) -> List[SessionMessageRow]:
    """Insert multiple session messages in a single transaction.

    Each dict in *messages* must have keys: session_id, role, content.
    Optional keys: tool_name, tool_input, metadata, profile_id, jsonl_uuid.

    Returns the list of inserted message dicts (with ids assigned).
    """
    if not messages:
        return []

    now = datetime.now(timezone.utc).isoformat()
    _ph = ph()
    _phs9 = ", ".join([_ph] * 9)
    sql = f"""INSERT INTO session_messages
             (session_id, role, content, tool_name, tool_input, metadata, profile_id, jsonl_uuid, created_at)
             VALUES ({_phs9}) RETURNING id"""

    results: List[SessionMessageRow] = []

    def _do_insert(conn):
        cursor = get_cursor(conn)
        for msg in messages:
            tool_input = msg.get("tool_input")
            metadata = msg.get("metadata")
            jsonl_uuid = msg.get("jsonl_uuid")
            params = (
                msg["session_id"],
                msg["role"],
                msg["content"],
                msg.get("tool_name"),
                json.dumps(tool_input) if tool_input else None,
                json.dumps(metadata) if metadata else None,
                msg.get("profile_id"),
                jsonl_uuid,
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
                "jsonl_uuid": jsonl_uuid,
                "created_at": now,
            })

    if _conn is not None:
        _do_insert(_conn)
    else:
        with get_db() as conn:
            _do_insert(conn)

    return results


def upsert_team_member_message(
    session_id: str,
    team_name: str,
    teammate_name: str,
    content: str,
    metadata: Dict[str, Any],
    profile_id: Optional[str] = None,
) -> SessionMessageRow:
    """Insert or update the team_member row for (session, team, teammate).

    Uniqueness is enforced at the app layer by matching on
    metadata.team_name + metadata.teammate_name rather than a unique index,
    so the DB schema stays additive. Returns the resulting row.
    """
    # Ensure identifying fields are always present in metadata.
    meta = dict(metadata or {})
    meta["team_name"] = team_name
    meta["teammate_name"] = teammate_name

    _ph = ph()
    now = datetime.now(timezone.utc).isoformat()

    with get_db() as conn:
        cursor = get_cursor(conn)
        # Find the most recent existing row for this teammate.
        cursor.execute(
            f"""SELECT id FROM session_messages
                 WHERE session_id = {_ph} AND role = 'team_member'
                 ORDER BY id DESC""",
            (session_id,),
        )
        rows = cursor.fetchall() or []
        target_id: Optional[int] = None
        for r in rows:
            # Re-read metadata column to filter by teammate name.
            cursor.execute(
                f"SELECT metadata FROM session_messages WHERE id = {_ph}",
                (r["id"],),
            )
            m_row = cursor.fetchone()
            if not m_row or not m_row.get("metadata"):
                continue
            try:
                raw = m_row["metadata"]
                m = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, ValueError):
                continue
            if (
                m.get("team_name") == team_name
                and m.get("teammate_name") == teammate_name
            ):
                target_id = r["id"]
                break

        if target_id is not None:
            cursor.execute(
                f"""UPDATE session_messages
                     SET content = {_ph}, metadata = {_ph}
                     WHERE id = {_ph}""",
                (content, json.dumps(meta), target_id),
            )
            return {
                "id": target_id,
                "session_id": session_id,
                "role": "team_member",
                "content": content,
                "metadata": meta,
                "profile_id": profile_id,
                "created_at": now,
            }

        cursor.execute(
            f"""INSERT INTO session_messages
                 (session_id, role, content, tool_name, tool_input, metadata, profile_id, created_at)
                 VALUES ({_ph}, {_ph}, {_ph}, NULL, NULL, {_ph}, {_ph}, {_ph}) RETURNING id""",
            (session_id, "team_member", content, json.dumps(meta), profile_id, now),
        )
        new_id = cursor.fetchone()["id"]

    return {
        "id": new_id,
        "session_id": session_id,
        "role": "team_member",
        "content": content,
        "metadata": meta,
        "profile_id": profile_id,
        "created_at": now,
    }


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


def delete_orphan_user_turn(session_id: str) -> Optional[str]:
    """Delete the trailing user message when a turn was stopped before any output.

    When the user presses Stop before the model emits any text/thinking/tool/
    subagent output, the prompt was eagerly persisted (written before the SDK
    runs) but the turn produced nothing — so the lone trailing ``user`` row is
    misleading and re-surfaces on the next poll under ``postgres_only``. Remove
    just that row so the chat reverts to its pre-prompt state.

    Authoritative cleanup driven off the durable store (the source of truth the
    ``/live`` poller reads once streaming ends), independent of whatever happened
    to the in-memory live-session copy. The SESSION ROW IS LEFT INTACT — for a
    worktree-backed session, deleting the session would orphan its worktree link.

    Only acts when the most recent row is a real ``user`` prompt (not a
    ``<task-notification>`` callback): any assistant/tool output this turn would
    sit after it (higher id), proving the turn must be kept.

    Returns the discarded prompt, or None when there is nothing to discard.
    """
    _ph = ph()
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"""SELECT id, role, content FROM session_messages
                 WHERE session_id = {_ph}
                 ORDER BY created_at DESC, id DESC LIMIT 1""",
            (session_id,)
        )
        row = cursor.fetchone()
        if not row:
            return None
        # RealDictCursor (postgres) returns dict-likes; sqlite returns tuples.
        if isinstance(row, dict):
            msg_id, role, content = row["id"], row["role"], row["content"]
        else:
            msg_id, role, content = row[0], row[1], row[2]
        if role != "user" or "<task-notification>" in (content or ""):
            return None
        cursor.execute(
            f"DELETE FROM session_messages WHERE session_id = {_ph} AND id = {_ph}",
            (session_id, msg_id)
        )
        if cursor.rowcount > 0:
            return content
        return None


def discard_orphan_first_turn(session_id: str) -> Optional[str]:
    """Fully revert a brand-new session whose FIRST turn was stopped before output.

    Under ``SESSION_STORE_MODE=postgres_only`` the chat view is read back from
    ``session_store_entries`` (the SDK transcript mirror, keyed by the session's
    ``sdk_session_id``) — NOT from ``session_messages``. So when the user presses
    Stop before any output on the very first turn of a new session, deleting the
    eager ``session_messages`` user row is not enough: the SDK may have already
    mirrored the user line into the store, and the next poll/reload would
    re-surface it. This wipes the whole (empty) SDK transcript for the turn and
    NULLs ``sessions.sdk_session_id`` so the session looks untouched and the next
    prompt starts a fresh SDK session.

    Safe ONLY for a first turn — the caller must confirm the session had no
    ``sdk_session_id`` *before* this turn. A follow-up turn shares its transcript
    with the prior conversation; wiping it would destroy real history (use
    :func:`delete_orphan_user_turn` there instead).

    The SESSION ROW (and its ``worktree_id`` link) IS KEPT — deleting the session
    would orphan a worktree-backed session's worktree.

    Returns the discarded prompt text (from the trailing user row), or None.
    """
    _ph = ph()
    with get_db() as conn:
        cursor = get_cursor(conn)

        # The interrupted turn may have minted + mirrored an SDK session before
        # being stopped. Read it so we can wipe its (empty) transcript.
        cursor.execute(
            f"SELECT sdk_session_id FROM sessions WHERE id = {_ph}", (session_id,)
        )
        srow = cursor.fetchone()
        sdk_sid = None
        if srow is not None:
            sdk_sid = srow["sdk_session_id"] if isinstance(srow, dict) else srow[0]

        if sdk_sid:
            # Wipe the empty SDK transcript so postgres_only reads return nothing.
            # Matched by session_id (the SDK UUID) ALONE — consistent with the
            # session_id-only read path (project_key derivation from cwd is
            # unreliable). The UUID is unique per session, so this can't touch
            # another session's rows; subagent transcripts (subpath != NULL) for
            # this same empty turn are correctly swept too.
            cursor.execute(
                f"DELETE FROM session_store_entries WHERE session_id = {_ph}",
                (sdk_sid,),
            )
            cursor.execute(
                f"DELETE FROM session_store_summaries WHERE session_id = {_ph}",
                (sdk_sid,),
            )
            # Unlink the SDK session so the next prompt starts clean. update_session()
            # can't express this — its ``sdk_session_id=None`` means "leave unchanged".
            cursor.execute(
                f"UPDATE sessions SET sdk_session_id = NULL WHERE id = {_ph}",
                (session_id,),
            )

        # Drop the eager session_messages user row (legacy-table hygiene). Same
        # guard as delete_orphan_user_turn: only a real trailing ``user`` prompt.
        cursor.execute(
            f"""SELECT id, role, content FROM session_messages
                 WHERE session_id = {_ph}
                 ORDER BY created_at DESC, id DESC LIMIT 1""",
            (session_id,),
        )
        row = cursor.fetchone()
        discarded = None
        if row is not None:
            if isinstance(row, dict):
                msg_id, role, content = row["id"], row["role"], row["content"]
            else:
                msg_id, role, content = row[0], row[1], row[2]
            if role == "user" and "<task-notification>" not in (content or ""):
                cursor.execute(
                    f"DELETE FROM session_messages WHERE session_id = {_ph} AND id = {_ph}",
                    (session_id, msg_id),
                )
                if cursor.rowcount > 0:
                    discarded = content
        return discarded


def clear_session_conversation(session_id: str) -> Optional[str]:
    """Clear a session's conversation IN PLACE — wipe its transcript/messages but
    KEEP the session row (and its ``worktree_id`` link).

    Semantics: this is the "Clear session" row action. Unlike
    :func:`delete_session` (which removes the row entirely) or a rewind (which
    truncates to a checkpoint), this resets the whole conversation to empty while
    preserving the session's identity (id / title / project / worktree) so the
    user stays put and the next prompt starts a fresh SDK conversation.

    Under ``SESSION_STORE_MODE=postgres_only`` the chat view is read back from
    ``session_store_entries`` keyed by the session's ``sdk_session_id`` — NOT from
    ``session_messages`` (see :func:`discard_orphan_first_turn`). So a durable
    clear must: wipe the SDK transcript store rows, NULL ``sdk_session_id`` so the
    read path skips the (now stale) transcript and returns nothing, drop the
    legacy ``session_messages`` rows, and zero the persisted usage counters.

    Returns the previous ``sdk_session_id`` (or None) so the caller can
    background-clean its JSONL file / pooled subprocess.
    """
    _ph = ph()
    with get_db() as conn:
        cursor = get_cursor(conn)

        cursor.execute(
            f"SELECT sdk_session_id FROM sessions WHERE id = {_ph}", (session_id,)
        )
        srow = cursor.fetchone()
        if srow is None:
            return None
        prev_sdk_sid = srow["sdk_session_id"] if isinstance(srow, dict) else srow[0]

        if prev_sdk_sid:
            # Wipe the SDK transcript mirror so postgres_only reads return nothing.
            # session_id-only match (the SDK UUID is unique per session); subagent
            # transcripts (subpath != NULL) for this session are swept too.
            cursor.execute(
                f"DELETE FROM session_store_entries WHERE session_id = {_ph}",
                (prev_sdk_sid,),
            )
            cursor.execute(
                f"DELETE FROM session_store_summaries WHERE session_id = {_ph}",
                (prev_sdk_sid,),
            )

        # Drop the legacy chat-history rows for this session.
        cursor.execute(
            f"DELETE FROM session_messages WHERE session_id = {_ph}",
            (session_id,),
        )

        # Unlink the SDK session and zero the persisted usage counters. Nulling
        # sdk_session_id makes the read path skip the transcript block entirely
        # (so even before the background JSONL cleanup runs, a reload is empty),
        # and the next prompt mints a fresh SDK session. update_session() can't
        # express the NULL (its ``sdk_session_id=None`` means "leave unchanged"),
        # so write it directly here alongside the counter reset.
        cursor.execute(
            f"""UPDATE sessions
                   SET sdk_session_id = NULL,
                       total_cost_usd = 0,
                       total_tokens_in = 0,
                       total_tokens_out = 0,
                       turn_count = 0,
                       updated_at = {_ph}
                 WHERE id = {_ph}""",
            (datetime.now(timezone.utc).isoformat(), session_id),
        )
        return prev_sdk_sid


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


def set_message_jsonl_uuid(message_id: int, jsonl_uuid: str) -> bool:
    """Backfill the JSONL uuid on an existing message row.

    User messages are persisted synchronously (before the SDK sees them), so
    their uuid is not known at insert time. When the SDK later streams the
    echoed ``UserMessage``, the caller invokes this to link the DB row back
    to its JSONL entry.

    Returns True if a row was updated, False if the id did not match.
    """
    _ph = ph()
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"UPDATE session_messages SET jsonl_uuid = {_ph} WHERE id = {_ph}",
            (jsonl_uuid, message_id)
        )
        return cursor.rowcount > 0


def delete_session_messages_after_jsonl_uuid(
    session_id: str,
    target_uuid: str,
    include_response: bool = False,
) -> int:
    """Truncate DB rows to mirror a JSONL rewind checkpoint.

    Finds the row whose ``jsonl_uuid`` matches ``target_uuid`` in
    ``session_id`` and deletes rows to match JSONL semantics:

    - ``include_response=False``: delete the anchor row **and** everything
      after it (mirrors JSONL's ``entries[:target_index]`` truncation,
      where the target message itself is dropped).
    - ``include_response=True``: keep the anchor row and widen to the first
      following assistant row; delete strictly after that so the anchored
      message and its reply survive.

    Returns the number of rows deleted. Returns 0 if ``target_uuid`` is not
    found; the caller is responsible for any fallback (e.g. timestamp cut
    for legacy rows that predate uuid persistence).
    """
    _ph = ph()
    with get_db() as conn:
        cursor = get_cursor(conn)

        # Locate the anchor row by (session_id, jsonl_uuid).
        cursor.execute(
            f"SELECT id FROM session_messages "
            f"WHERE session_id = {_ph} AND jsonl_uuid = {_ph}",
            (session_id, target_uuid)
        )
        row = cursor.fetchone()
        if row is None:
            return 0
        anchor_id = row["id"] if isinstance(row, dict) else row[0]

        if include_response:
            # Widen the anchor to include the first following assistant row,
            # then delete strictly after the widened anchor.
            cursor.execute(
                f"SELECT id FROM session_messages "
                f"WHERE session_id = {_ph} AND id > {_ph} AND role = 'assistant' "
                f"ORDER BY id ASC LIMIT 1",
                (session_id, anchor_id)
            )
            next_asst = cursor.fetchone()
            if next_asst is not None:
                anchor_id = next_asst["id"] if isinstance(next_asst, dict) else next_asst[0]
            cursor.execute(
                f"DELETE FROM session_messages WHERE session_id = {_ph} AND id > {_ph}",
                (session_id, anchor_id)
            )
        else:
            # Drop the anchor row and everything after it — JSONL drops the
            # target entry when include_response is False, so the DB must too.
            cursor.execute(
                f"DELETE FROM session_messages WHERE session_id = {_ph} AND id >= {_ph}",
                (session_id, anchor_id)
            )
        return cursor.rowcount or 0


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

    Every CTE excludes ``status IN ('archived','failed')``. ``'failed'`` is the
    ProviderAgent verdict on a worker session whose dispatch errored (see
    ``app.core.provider_agent.WORKER_FAILED_STATUS``): the transcript stays
    readable from the session lists, but a dead worker must not compete with
    real conversations for search hits. A new turn on such a session revives it
    to ``'active'`` and it becomes searchable again.
    """
    if not query or not query.strip():
        return []

    search_term = f"%{query.strip()}%"
    fts_query = _fts_query_term(query)
    _ph = ph()
    # Cross-install sharing (default): no per-install clause. Isolation mode:
    # restrict both CTEs to this install (legacy NULL rows visible everywhere).
    inst_sql, inst_params = _instance_filter(_ph, "s.")

    with get_db() as conn:
        cursor = get_cursor(conn)

        # Search titles with ILIKE, legacy message content + full store history
        # with tsvector FTS. Use DISTINCT ON (id) to deduplicate sessions that
        # match in more than one place, keeping the best match (title > content).
        # store_matches searches session_store_entries (the JSONB transcript
        # mirror) via the IMMUTABLE sse_entry_text() + idx_sse_fts GIN index —
        # under postgres_only the assistant/tool history lives there, not in
        # session_messages. Joined to sessions on sdk_session_id.
        # match_count = COUNT(*) OVER (PARTITION BY id) taken BEFORE the final
        # dedupe, i.e. how many of {title, legacy-content, store-content} hit.
        sql = f"""
        WITH title_matches AS (
            SELECT
                s.id, s.project_id, s.profile_id, s.api_user_id, s.title,
                s.status, s.is_active, s.total_cost_usd, s.total_tokens_in, s.total_tokens_out,
                s.turn_count, s.created_at, s.updated_at,
                'title' as match_type,
                s.title as match_snippet,
                s.updated_at as match_time
            FROM sessions s
            WHERE {sql_ilike('s.title')}
            AND s.status NOT IN ('archived','failed'){inst_sql}
        ),
        content_matches AS (
            SELECT DISTINCT ON (s.id)
                s.id, s.project_id, s.profile_id, s.api_user_id, s.title,
                s.status, s.is_active, s.total_cost_usd, s.total_tokens_in, s.total_tokens_out,
                s.turn_count, s.created_at, s.updated_at,
                'content' as match_type,
                ts_headline('english', sm.content, plainto_tsquery('english', {_ph}),
                    'MaxFragments=1,MaxWords=20,MinWords=5') as match_snippet,
                sm.created_at as match_time
            FROM session_messages sm
            INNER JOIN sessions s ON s.id = sm.session_id
            WHERE sm.content_tsv @@ plainto_tsquery('english', {_ph})
            AND sm.role IN ('user', 'assistant')
            AND s.status NOT IN ('archived','failed'){inst_sql}
            ORDER BY s.id, sm.created_at DESC
        ),
        store_matches AS (
            SELECT DISTINCT ON (s.id)
                s.id, s.project_id, s.profile_id, s.api_user_id, s.title,
                s.status, s.is_active, s.total_cost_usd, s.total_tokens_in, s.total_tokens_out,
                s.turn_count, s.created_at, s.updated_at,
                'content' as match_type,
                ts_headline('english', sse_entry_text(sse.entry), plainto_tsquery('english', {_ph}),
                    'MaxFragments=1,MaxWords=20,MinWords=5') as match_snippet,
                sse.created_at as match_time
            FROM session_store_entries sse
            INNER JOIN sessions s ON s.sdk_session_id = sse.session_id
            WHERE sse.subpath IS NULL
            AND to_tsvector('english', sse_entry_text(sse.entry)) @@ plainto_tsquery('english', {_ph})
            AND s.status NOT IN ('archived','failed'){inst_sql}
            ORDER BY s.id, sse.created_at DESC
        ),
        all_matches AS (
            SELECT *, COUNT(*) OVER (PARTITION BY id) as match_count
            FROM (
                SELECT * FROM title_matches
                UNION ALL
                SELECT * FROM content_matches
                UNION ALL
                SELECT * FROM store_matches
            ) unioned
        )
        SELECT DISTINCT ON (id) *
        FROM all_matches
        WHERE 1=1
        """
        # Param order follows CTE order: title ILIKE needs search_term (+
        # instance_id when isolated); content ts_headline + WHERE @@ each need
        # fts_query (+ instance_id); store ts_headline + WHERE @@ each need
        # fts_query (+ instance_id).
        params: List[Any] = [
            search_term, *inst_params,
            fts_query, fts_query, *inst_params,
            fts_query, fts_query, *inst_params,
        ]

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

    Excludes ``status IN ('archived','failed')`` in every CTE, for the reasons
    spelled out on :func:`search_sessions`.

    Returns:
        Tuple of (results list, total count)
    """
    if not query or not query.strip():
        return [], 0

    search_term = f"%{query.strip()}%"
    fts_query = _fts_query_term(query)
    _ph = ph()
    # Cross-install sharing (default): no per-install clause. Isolation mode:
    # restrict both CTEs to this install (legacy NULL rows visible everywhere).
    inst_sql, inst_params = _instance_filter(_ph, "s.")

    with get_db() as conn:
        cursor = get_cursor(conn)

        # Build the base CTE using Postgres tsvector FTS.
        # Uses DISTINCT ON (id) to deduplicate sessions, preferring title
        # matches over content matches. store_matches searches the JSONB
        # transcript mirror (session_store_entries) via the IMMUTABLE
        # sse_entry_text() + idx_sse_fts GIN index — under postgres_only the
        # assistant/tool history lives there, not in session_messages. Joined to
        # sessions on sdk_session_id. match_count = COUNT(*) OVER (PARTITION BY
        # id) taken BEFORE the final dedupe, i.e. how many of {title,
        # legacy-content, store-content} hit.
        base_cte = f"""
        WITH title_matches AS (
            SELECT
                s.*,
                'title' as match_type,
                s.title as match_snippet,
                s.updated_at as match_time
            FROM sessions s
            WHERE {sql_ilike('s.title')}
            AND s.status NOT IN ('archived','failed'){inst_sql}
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
            AND s.status NOT IN ('archived','failed'){inst_sql}
            ORDER BY s.id, sm.created_at DESC
        ),
        store_matches AS (
            SELECT DISTINCT ON (s.id)
                s.*,
                'content' as match_type,
                ts_headline('english', sse_entry_text(sse.entry), plainto_tsquery('english', {_ph}),
                    'MaxFragments=1,MaxWords=20,MinWords=5') as match_snippet,
                sse.created_at as match_time
            FROM session_store_entries sse
            INNER JOIN sessions s ON s.sdk_session_id = sse.session_id
            WHERE sse.subpath IS NULL
            AND to_tsvector('english', sse_entry_text(sse.entry)) @@ plainto_tsquery('english', {_ph})
            AND s.status NOT IN ('archived','failed'){inst_sql}
            ORDER BY s.id, sse.created_at DESC
        ),
        all_matches AS (
            SELECT *, COUNT(*) OVER (PARTITION BY id) as match_count
            FROM (
                SELECT * FROM title_matches
                UNION ALL
                SELECT * FROM content_matches
                UNION ALL
                SELECT * FROM store_matches
            ) unioned
        ),
        deduped_matches AS (
            SELECT DISTINCT ON (id) *
            FROM all_matches
            WHERE 1=1
        """
        # Param order follows CTE order: title ILIKE needs search_term (+
        # instance_id when isolated); content ts_headline + WHERE @@ each need
        # fts_query (+ instance_id); store ts_headline + WHERE @@ each need
        # fts_query (+ instance_id).
        base_params: List[Any] = [
            search_term, *inst_params,
            fts_query, fts_query, *inst_params,
            fts_query, fts_query, *inst_params,
        ]

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
             json.dumps(data, default=str) if data else None, now)
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
