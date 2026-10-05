"""Session and message database operations."""

from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any, TypedDict

from app.core.session_modes import normalize_session_mode, storage_session_mode
from app.db.base import get_db, get_cursor, row_to_dict, rows_to_list, logger
from app.db.compat import ph, sql_ilike


# ============================================================================
# Cross-install session sharing
# ============================================================================

def _share_sessions_across_installs() -> bool:
    """True when sessions are shared across every install on a shared database.

    Default ON: a shared-database fleet sees every install's sessions in one
    place. Set the ``cluster_share_sessions`` system setting to a falsey value
    ("off"/"false"/"0") for per-install isolation.

    Read through the short-TTL memo: this runs on EVERY ``get_sessions*`` /
    ``search_sessions*`` call — i.e. on every sidebar refresh — for a value
    that changes approximately never. Writes
    through ``app.db.settings`` invalidate the memo immediately, so a toggle
    from this install takes effect at once; another install's toggle is picked
    up within the memo TTL.
    """
    from app.db import database
    val = database.get_system_setting_cached("cluster_share_sessions")
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
    # Owning install. Stamped at creation — this install's
    # ``effective_instance_id``, or the composer's device pick.
    instance_id: Optional[str]
    # Explicit placement, e.g. ``{"where": "runner:<id>"}``. NULL means the
    # implicit "follows its owner" default; a non-NULL value is a deliberate pin
    # (device pick / /move / /adopt) and is never auto-overridden.
    placement: Optional[Dict[str, Any]]
    working_set_ref: Optional[str]
    context_usage_json: Optional[Dict[str, Any]]
    provider_id: Optional[str]
    # Effective model the SDK/CLI session was last launched (or switched) with.
    # A record of what RAN — distinct from ``model`` below, which is what the
    # user picked.
    launched_model: Optional[str]
    # The FIRST model this session ever ran on, written once and never
    # overwritten. ``launched_model`` answers "what is it on now"; a session
    # that switched mid-flight lost the answer to "what did it start on"
    # entirely. NULL on rows created before the column existed.
    first_model: Optional[str]
    # The run binding chosen when this session was created. ``model`` is
    # the raw pick: a bare id/alias, or a ``prov:<providerId>:<modelId>``
    # composite that also settles ``provider_id``. NULL on older rows, which
    # fall back to ``launched_model`` and then the agent's legacy config.
    model: Optional[str]
    effort: Optional[str]
    permission_mode: Optional[str]
    # A delegated worker's dispatch ceiling, fixed at creation; NULL for every
    # other session. See ``session_modes.turn_permission_mode``.
    permission_ceiling: Optional[str]
    context_window: Optional[int]
    # Per-session fast mode: TRUE asks the leg for its expedited service,
    # NULL/FALSE runs standard.
    fast: Optional[bool]
    # Sidepanel mode: NULL for a 'code' session (the full agentic surface, and
    # every row predating this column), 'chat' for the web-only chat surface,
    # 'voice' for an OpenAI Realtime call. See
    # ``app.core.session_modes``.
    mode: Optional[str]
    voice_config: Optional[Dict[str, Any]]
    # List-visibility flag: TRUE keeps the row out of the sessions-list
    # queries (wake-word "command" voice sessions); NULL/FALSE = visible.
    hidden: Optional[bool]
    # Persisted PR lifecycle. NULL = unknown; the sidepanel may still
    # infer an open PR from GitHub. pr_state is 'open' / 'merged' / 'closed'.
    pr_number: Optional[int]
    pr_url: Optional[str]
    pr_state: Optional[str]
    # The native leg's "# Workspace facts" snapshot, rendered once and reused
    # byte-for-byte on every later connect (see
    # ``set_session_workspace_facts``). NULL until the first native connect.
    workspace_facts: Optional[Dict[str, Any]]


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


def _assistant_home_filter(prefix=""):
    """Hide designated homes, including legacy mirrors whose owner is offline.

    Explicit include_hidden callers and direct get_session retain history.
    """
    return (f" AND {prefix}id NOT IN (SELECT home_session_id FROM assistant_state WHERE home_session_id IS NOT NULL"
            " UNION SELECT home_session_id FROM assistant_account WHERE home_session_id IS NOT NULL)")


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

    Hidden sessions (wake-word "command" voice sessions) are excluded
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
        # NULL = visible (older rows), so coalesce rather than = FALSE.
        # ``is_worker`` is the MATERIALIZED delegated-worker flag, so the list
        # filter needs no correlated ``NOT EXISTS (SELECT 1 FROM turns ...)``
        # probe per row (older workers were backfilled once by
        # _backfill_sessions_is_worker).
        query += " AND COALESCE(s.hidden, FALSE) = FALSE" if tag_id else " AND COALESCE(hidden, FALSE) = FALSE"
        query += " AND COALESCE(s.is_worker, FALSE) = FALSE" if tag_id else " AND COALESCE(is_worker, FALSE) = FALSE"
        query += _assistant_home_filter("s." if tag_id else "")

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

    Hidden sessions are excluded from BOTH the items and the total
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
        # NULL = visible (older rows). Shared by the COUNT query below,
        # so hidden rows never inflate the total. ``is_worker`` is the
        # materialized delegated-worker flag, so neither query needs a per-row
        # ``NOT EXISTS (... turns ...)`` probe.
        base_query += f" AND COALESCE({col_prefix}hidden, FALSE) = FALSE"
        base_query += f" AND COALESCE({col_prefix}is_worker, FALSE) = FALSE"
        base_query += _assistant_home_filter(col_prefix)

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
    exclude_archived: bool = False,
) -> tuple[List[Dict[str, Any]], Optional[str], Optional[str], bool]:
    """Get sessions using cursor-based pagination (keyset pagination).

    Uses (updated_at, id) as the cursor key for deterministic ordering.

    Hidden sessions are excluded unless ``include_hidden`` is True —
    the filter is part of the shared WHERE clause, so the limit+1 has_more
    probe never counts hidden rows. ``exclude_archived`` works the same way
    for archived sessions: a list that never shows them (the sidebar) must
    not be told more rows exist when only archived ones remain.

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
    if exclude_archived:
        # NULL status is a live row, so it must survive the inequality.
        base_query += f" AND ({col_prefix}status IS NULL OR {col_prefix}status <> 'archived')"
    if api_users_only:
        base_query += f" AND {col_prefix}api_user_id IS NOT NULL"
    elif api_user_id is not None:
        if api_user_id:
            base_query += f" AND {col_prefix}api_user_id = {_ph}"
            params.append(api_user_id)
        else:
            base_query += f" AND {col_prefix}api_user_id IS NULL"
    if not include_hidden:
        # NULL = visible (older rows). ``is_worker`` is the materialized
        # delegated-worker flag, so no per-row ``NOT EXISTS (... turns ...)``
        # probe is needed.
        base_query += f" AND COALESCE({col_prefix}hidden, FALSE) = FALSE"
        base_query += f" AND COALESCE({col_prefix}is_worker, FALSE) = FALSE"
        base_query += _assistant_home_filter(col_prefix)

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
    derive_provider_from_profile: bool = True,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    permission_mode: Optional[str] = None,
    permission_ceiling: Optional[str] = None,
    context_window: Optional[int] = None,
    fast: Optional[bool] = None,
    mode: Optional[str] = None,
    hidden: bool = False,
    is_worker: bool = False,
    target_instance_id: Optional[str] = None,
    project_v2_id: Optional[str] = None,
    voice_config: Optional[Dict[str, Any]] = None,
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
            about (ProviderAgent cross-provider workers). ``None`` (or "")
            falls back to a ``prov:`` model composite, then the profile.
        derive_provider_from_profile: When false, preserve an explicit native
            binding (``provider_id=None``) instead of falling back to the
            profile's top-level provider. Task workers use this because their
            provider comes from the caller, not the target profile.
        model: The model chosen for THIS session. Agents do not pin
            one, so this is the session's binding: a bare id/alias, or a
            ``prov:<providerId>:<modelId>`` composite. A composite also
            supplies the session's provider when no explicit ``provider_id``
            was passed, so the pick binds model and provider together.
            ``None`` leaves the row unbound and resolution falls back (see
            ``query_options.build_options_from_profile``).
        effort: Reasoning effort chosen for this session, or ``None``.
        permission_mode: Permission mode chosen for this session, or ``None``.
        permission_ceiling: A delegated worker's dispatch ceiling, or ``None``.
        context_window: Explicit input-window limit in tokens, or ``None`` to use
            the selected model/provider's registered default.
        fast: Fast mode chosen for this session, or ``None`` to leave the
            row unbound (the session runs at standard speed).
        mode: Sidepanel mode for this session — ``'chat'`` for the web-only
            chat surface, ``'voice'`` for an OpenAI Realtime voice call,
            ``'code'``/``None`` for the ordinary agentic surface.
            ``'code'`` (and anything unrecognized) persists as NULL; the API
            model coalesces it back to ``'code'``.
        hidden: Keep this session out of the sessions-list queries —
            wake-word "command" voice sessions are born hidden so they never
            flash into "recent sessions". List-visibility only: the row is
            still fetchable by id.
        is_worker: Mark this session as a delegated ProviderAgent worker.
            Workers are also created ``hidden=True``; this flag is the durable,
            queryable fact the list queries filter on. ``False`` (the default)
            = a normal session.
        target_instance_id: Create this session OWNED BY another install (the
            composer's device picker). The row is stamped with that install as
            ``instance_id`` AND with an EXPLICIT sticky ``placement`` pin
            (``{"where": "runner:<id>"}``), exactly like
            :func:`pin_session_placement` writes — the user chose this device,
            so the choice must survive sends from any other device (an implicit
            NULL-placement pin would be auto-adoptable). ``None`` (the default):
            this install owns the row and ``placement`` stays NULL.
        _conn: Optional existing DB connection for transaction grouping
    """
    from app.core.config import settings
    now = datetime.now(timezone.utc).isoformat()
    target_instance_id = (target_instance_id or "").strip() or None
    project_v2_id = (project_v2_id or "").strip() or None
    project_id = project_id or None
    if api_user_id and project_id is None and project_v2_id is None:
        # A project-limited API key never owns a projectless session (its cwd
        # would be the workspace root holding every project): file it under
        # the key's project, exactly as the REST create routes do. Every
        # create path — chat WebSocket, REST, workers — goes through here.
        from app.db.api_users import get_api_user
        _api_user = get_api_user(api_user_id)
        if _api_user and _api_user.get("project_id"):
            project_id = _api_user["project_id"]
    instance_id = target_instance_id or settings.effective_instance_id
    # Per-session provider binding: resolve the profile's
    # configured provider ONCE, at creation, and stamp it into
    # sessions.provider_id. Every subsequent turn/spawn reads THIS binding (see
    # query_options.build_options_from_profile), so editing the profile later
    # never reroutes a live session. NULL = no provider binding;
    # a native profile (no config.provider) and an unresolvable profile both
    # stay NULL.
    #
    # An EXPLICIT ``provider_id`` argument short-circuits that resolution and
    # wins: a ProviderAgent worker session is bound to the provider the
    # tool call named, not to whatever its (inherited) profile happens to
    # configure.
    resolved_provider_id = (provider_id or "").strip() or None
    # The session's own run binding. A ``prov:`` composite carries the
    # provider with the model, so an explicit provider_id aside, the user's
    # model pick is what binds the session to a provider — the agent's legacy
    # config.provider is only consulted when neither says anything.
    selected_model = (model or "").strip() or None
    selected_effort = (effort or "").strip() or None
    selected_permission_mode = (permission_mode or "").strip() or None
    selected_permission_ceiling = (permission_ceiling or "").strip() or None
    try:
        selected_context_window = int(context_window) if context_window else None
    except (TypeError, ValueError):
        selected_context_window = None
    if selected_context_window is not None and selected_context_window <= 0:
        selected_context_window = None
    selected_fast = None if fast is None else bool(fast)
    if resolved_provider_id is None and selected_model:
        from app.core.query_options import decode_provider_model

        decoded = decode_provider_model(selected_model)
        if decoded:
            resolved_provider_id = decoded[0]
            derive_provider_from_profile = False
    if resolved_provider_id is None and derive_provider_from_profile:
        try:
            from app.db.profiles import get_profile
            _profile = get_profile(profile_id)
            if _profile:
                resolved_provider_id = (_profile.get("config") or {}).get("provider") or None
        except Exception:  # noqa: BLE001 — binding is best-effort; NULL = native
            resolved_provider_id = None
    # ``'code'`` is stored as NULL so a new code session is indistinguishable
    # from every row written before modes existed; normalizing first means an
    # unknown string is filed as code rather than persisted verbatim.
    resolved_mode = storage_session_mode(normalize_session_mode(mode))
    _ph = ph()
    # Worker network: the device this session executes on. A target that is
    # a network device id (the composer's runner picker) owns the row; a
    # legacy instance id maps through the network's legacy map; otherwise the
    # row is ours.
    owner_device_id = None
    try:
        from app.network import runners as _runners
        if target_instance_id:
            resolved = _runners.resolve_runner_id(target_instance_id)
            owner_device_id = resolved if _runners.find(resolved) else None
        else:
            owner_device_id = _runners.self_device_id()
    except Exception:  # noqa: BLE001 — stamping is best-effort; NULL means "this worker"
        owner_device_id = None
    columns = ["id", "profile_id", "project_id", "project_v2_id", "title", "api_user_id", "parent_session_id", "fork_point_message_index", "worktree_id", "worktree_branch", "worktree_path", "automation_id", "instance_id", "owner_device_id", "is_active", "v2_stream", "created_at", "updated_at", "provider_id", "model", "effort", "permission_mode", "permission_ceiling", "context_window", "fast", "mode", "hidden", "is_worker"]
    params = [session_id, profile_id, project_id, project_v2_id, title, api_user_id, parent_session_id, fork_point_message_index, worktree_id, worktree_branch, worktree_path, automation_id, instance_id, owner_device_id, is_active, True, now, now, resolved_provider_id, selected_model, selected_effort, selected_permission_mode, selected_permission_ceiling, selected_context_window, selected_fast, resolved_mode, hidden, bool(is_worker)]
    if voice_config is not None:
        columns.append("voice_config")
        params.append(json.dumps(voice_config))
    if target_instance_id:
        # Serialized to a JSON string (not a dict) so the SQLite test shim, which
        # stores JSONB columns as TEXT, round-trips identically to Postgres —
        # same handling as pin_session_placement / adopt_session.
        columns.append("placement")
        params.append(json.dumps({"where": f"runner:{target_instance_id}"}))
    # RETURNING * instead of a follow-up get_session(): one round trip, and the
    # row returned is exactly the one just written.
    sql = f"""INSERT INTO sessions ({", ".join(columns)})
               VALUES ({", ".join([_ph] * len(columns))})
               RETURNING *"""
    params = tuple(params)
    if _conn is not None:
        cursor = get_cursor(_conn)
        cursor.execute(sql, params)
        return row_to_dict(cursor.fetchone())
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(sql, params)
        return row_to_dict(cursor.fetchone())


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
            ) VALUES ({_phs13}) RETURNING *""",
            (session_id, profile_id, project_id, title, api_user_id,
             sdk_session_id, total_cost_usd, total_tokens_in, total_tokens_out, turn_count,
             instance_id, ts_created, now)
        )
        return row_to_dict(cursor.fetchone())


def update_session(
    session_id: str,
    sdk_session_id: Optional[str] = None,
    title: Optional[str] = None,
    status: Optional[str] = None,
    cost_increment: float = 0,
    tokens_in_increment: int = 0,
    tokens_out_increment: int = 0,
    turn_increment: int = 0,
    cache_read_increment: int = 0,
    cache_creation_increment: int = 0,
    model_calls_increment: int = 0,
    worktree_id: Optional[str] = None,
    last_input_tokens: Optional[int] = None,
    cache_creation_tokens: Optional[int] = None,
    cache_read_tokens: Optional[int] = None,
    launched_model: Optional[str] = None,
    v2_stream: Optional[bool] = None,
    _conn=None,
) -> Optional[SessionRow]:
    """Update a session with usage stats

    Args:
        sdk_session_id: Set to an SDK session ID, or empty string "" to clear (NULL)
        worktree_id: Set to a worktree ID to link, or empty string "" to unlink
        cache_read_increment / cache_creation_increment: per-turn cache tokens
            ADDED to the cumulative ``total_cache_*`` columns. ``tokens_in_increment``
            carries FRESH input only (usage is normalized Anthropic-style), so
            these are the rest of the prompt — without them a cache-heavy session's
            cumulative totals miss most of what it actually sent.
        model_calls_increment: API hops inside the turn, added to
            ``total_model_calls``. Distinct from ``turn_increment``, which counts
            USER-VISIBLE turns (one per completed turn).
        last_input_tokens / cache_creation_tokens / cache_read_tokens: absolute
            per-turn context-window values for the latest turn. These overwrite
            (not increment) so the context bar reflects the most recent turn —
            they are a SNAPSHOT, which is exactly why the cumulative columns above
            have to exist separately.
        launched_model: Effective model the session was launched (or switched)
            with, persisted at spawn/link time so the host-op guard can read it
            without waiting for turn-end message commits. Also seeds
            ``first_model`` on the FIRST write only (COALESCE) — that column
            records what the session started on and is never overwritten.
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
    if cache_read_increment:
        updates.append(
            f"total_cache_read_tokens = COALESCE(total_cache_read_tokens, 0) + {_ph}"
        )
        values.append(cache_read_increment)
    if cache_creation_increment:
        updates.append(
            f"total_cache_creation_tokens = "
            f"COALESCE(total_cache_creation_tokens, 0) + {_ph}"
        )
        values.append(cache_creation_increment)
    if model_calls_increment:
        updates.append(
            f"total_model_calls = COALESCE(total_model_calls, 0) + {_ph}"
        )
        values.append(model_calls_increment)
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
        # Same write, second column, set-once semantics: COALESCE keeps the
        # FIRST value and ignores every later one, so no caller has to remember
        # to pass it and no mid-session switch can clobber it. Doing it here
        # rather than at the call sites is deliberate — ``launched_model`` is
        # persisted from four separate spawn/switch paths, and an attribution
        # column that depends on all of them remembering is one that will
        # eventually be wrong.
        updates.append(f"first_model = COALESCE(first_model, {_ph})")
        values.append(launched_model)
    if v2_stream is not None:
        updates.append(f"v2_stream = {_ph}")
        values.append(bool(v2_stream))

    values.append(session_id)

    # RETURNING * makes this ONE statement instead of UPDATE-then-SELECT, on a
    # path every turn hits several times.
    with (nullcontext(_conn) if _conn is not None else get_db()) as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"UPDATE sessions SET {', '.join(updates)} WHERE id = {_ph} RETURNING *",
            values
        )
        return row_to_dict(cursor.fetchone())


def _provider_for_model_pick(
    session_id: str, pick: str
) -> tuple[Optional[str], bool]:
    """Resolve what a model pick means for a session's ``provider_id``.

    Returns ``(provider_id, should_write)``. Mirrors the two rebind shapes the
    turn itself applies: a ``prov:<providerId>:<modelId>`` composite names its
    provider outright, and a plain native-Claude pick that the currently bound
    provider does not curate drops the session back to the native leg. Anything
    else (a bare model belonging to the bound provider, a custom id) leaves the
    binding untouched — as does any lookup failure, since a stale binding is far
    better than one wrongly cleared.
    """
    from app.core.query_options import decode_provider_model, is_native_claude_model

    decoded = decode_provider_model(pick)
    if decoded:
        return decoded[0], True
    if not is_native_claude_model(pick):
        return None, False

    row = get_session(session_id)
    bound = (row or {}).get("provider_id")
    if not bound:
        return None, False
    try:
        from app.db.model_providers import get_model_provider
        from app.core.provider_agent import curated_model_ids

        provider_row = get_model_provider(bound, include_token=False)
        if pick in curated_model_ids(provider_row):
            return None, False
    except Exception:  # noqa: BLE001 — never clear a binding on a lookup failure
        logger.debug(
            "run-binding: could not check provider %s for model %s", bound, pick,
            exc_info=True,
        )
        return None, False
    return None, True


def set_session_run_binding(
    session_id: str,
    *,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    permission_mode: Optional[str] = None,
    context_window: Optional[int] = None,
    fast: Optional[bool] = None,
    workspace_project_ids: Optional[List[str]] = None,
    preserve_provider: bool = False,
    clear_effort: bool = False,
    away_mode: Optional[bool] = None,
) -> None:
    """Persist the run binding a turn actually used onto its session.

    Agents do not pin a model, so the session row carries the choice. The
    composer sends its current selection with every send; writing it back here
    makes a mid-session switch stick server-side, which is what lets another
    device — and a server-initiated turn such as a wakeup, automation, or
    connector message — run on the model the user last chose rather than on a
    default.

    The model also settles ``provider_id``, mirroring the rebind the turn itself
    performs (see ``query_options.build_options_from_profile``) so the row never
    disagrees with the leg the turn ran on: a ``prov:<providerId>:<modelId>``
    composite binds that provider, and a plain native-Claude pick the bound
    provider does not curate clears the binding back to native. Voice context
    updates pass ``preserve_provider`` because that provider funds Realtime,
    while the model binding selects a worker.

    ``None`` leaves a field alone; only non-empty values are written (a turn
    that sent no override must not erase the binding). ``fast`` is the one
    exception to "non-empty": it is a BOOLEAN, so ``False`` is a real value the
    user picked and is written through — only ``None`` (the field was absent)
    leaves the row alone. ``clear_effort`` explicitly removes the session's
    effort binding (the live settings endpoint's model-default choice).
    """
    _ph = ph()
    updates: List[str] = []
    values: List[Any] = []

    clean_model = (model or "").strip()
    if clean_model:
        updates.append(f"model = {_ph}")
        values.append(clean_model)
        if not preserve_provider:
            rebound_provider, rebind = _provider_for_model_pick(session_id, clean_model)
            if rebind:
                updates.append(f"provider_id = {_ph}")
                values.append(rebound_provider)
    clean_effort = (effort or "").strip()
    if clean_effort or clear_effort:
        updates.append(f"effort = {_ph}")
        values.append(clean_effort or None)
    clean_permission = (permission_mode or "").strip()
    if clean_permission:
        updates.append(f"permission_mode = {_ph}")
        values.append(clean_permission)
    try:
        clean_context_window = int(context_window) if context_window else None
    except (TypeError, ValueError):
        clean_context_window = None
    if clean_context_window is not None and clean_context_window > 0:
        updates.append(f"context_window = {_ph}")
        values.append(clean_context_window)
    # Boolean binding: False is a real pick ("turn fast mode off"), so it
    # must reach the row. Only an absent field (None) is a no-op.
    if fast is not None:
        updates.append(f"fast = {_ph}")
        values.append(bool(fast))
    # Away mode is boolean the same way: False is "the owner is back".
    if away_mode is not None:
        updates.append(f"away_mode = {_ph}")
        values.append(bool(away_mode))

    # An empty list explicitly removes every additional folder.
    if workspace_project_ids is not None:
        from app.core.workspace_folders import normalize_workspace_project_ids
        updates.append(f"workspace_project_ids = {_ph}")
        values.append(json.dumps(normalize_workspace_project_ids(workspace_project_ids)))

    if not updates:
        return
    values.append(session_id)
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"UPDATE sessions SET {', '.join(updates)} WHERE id = {_ph}",
            values,
        )


def set_session_voice_binding(session_id: str, config: Dict[str, Any]) -> None:
    """Pin call transport separately from the conversation's text model/provider."""
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"UPDATE sessions SET voice_config = {ph()} WHERE id = {ph()}",
            (json.dumps(config), session_id),
        )


def patch_voice_session_context(session_id: str, changes: Dict[str, Any]) -> None:
    """Apply a validated sparse voice PATCH: missing is unchanged, null clears.

    The Realtime funding provider is deliberately outside this allowlist. Turn
    binding retains its separate None-means-inherit contract.
    """
    allowed = {"model", "effort", "permission_mode", "context_window", "fast"}
    if changes.keys() - allowed:
        raise ValueError("Unsupported voice context field")
    if not changes:
        return
    placeholder = ph()
    assignments = ", ".join(f"{field} = {placeholder}" for field in changes)
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"UPDATE sessions SET {assignments} WHERE id = {placeholder}",
            [*changes.values(), session_id],
        )


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


def set_session_workspace_facts(session_id: str, snapshot: Dict[str, Any]) -> None:
    """Persist the session's "# Workspace facts" snapshot, replacing any prior one.

    ``snapshot`` is the adapter's ``{"v", "card", "cwd", "preview_expires_at",
    "captured_at"}`` dict. Serialized to a JSON string so the SQLite test shim
    (which stores JSONB as TEXT) round-trips the card's exact bytes, the same
    way ``set_session_context_usage`` does.
    """
    _ph = ph()
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"UPDATE sessions SET workspace_facts = {_ph} WHERE id = {_ph}",
            (json.dumps(snapshot), session_id),
        )


def get_session_workspace_facts(session_id: str) -> Optional[Dict[str, Any]]:
    """The persisted "# Workspace facts" snapshot, or None when the column is
    empty, unreadable, or the session does not exist."""
    _ph = ph()
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"SELECT workspace_facts FROM sessions WHERE id = {_ph}",
            (session_id,),
        )
        row = cursor.fetchone()
    if not row:
        return None
    raw = row["workspace_facts"] if not isinstance(row, (tuple, list)) else row[0]
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return None
    return raw if isinstance(raw, dict) else None


def adopt_session(session_id: str, instance_id: str) -> Optional[SessionRow]:
    """Re-point a session's owning install to ``instance_id`` (cross-device adopt).

    Backs the sessions-board "Resume here" action: on a shared database a
    session created by another install can be taken over locally by rewriting
    its ``instance_id`` to this install's id. The same UPDATE also dual-writes ``placement = {"where": "runner:<instance_id>"}`` —
    unconditionally, matching the unconditional ``instance_id`` rewrite and
    applying even to ``"anywhere"`` sessions, because adopt MEANS "resume
    locally". Without this, an existing ``runner:<old-owner>`` pin would
    survive the adopt and the placement resolver would keep routing turns to
    the OLD owner. ``sdk_session_id`` / ``worktree_id`` / ``worktree_branch``
    stay put so history resumes and the existing ``recreate_worktree``
    adopt-on-disk path (worktree_manager) rebinds any worktree; ``cloud_branch``
    is deliberately NEVER written. ``updated_at`` IS bumped so
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
            f"updated_at = {_ph} WHERE id = {_ph} RETURNING *",
            (instance_id, placement_json, now, session_id),
        )
        return row_to_dict(cursor.fetchone())


def adopt_session_follow(session_id: str, instance_id: str) -> Optional[SessionRow]:
    """Take over a session's ownership WITHOUT pinning it (send-path auto-adopt).

    The quiet counterpart of :func:`adopt_session`: the sending install becomes
    the owner (``instance_id``) so the turn runs here, but ``placement`` is
    deliberately LEFT ALONE. Callers only ever reach this for a session whose
    placement is already NULL — an IMPLICIT pin — so not writing one keeps
    the session implicitly pinned to whoever last sent from it, i.e. still
    auto-adoptable by the next device. Writing a ``runner:<id>`` pin here would
    silently upgrade a "follows me" session into a sticky one.

    ``sdk_session_id`` / ``worktree_*`` / ``cloud_branch`` stay untouched exactly
    like ``adopt_session`` (history resumes; ``cloud_branch`` is never written).
    ``updated_at`` IS bumped, mirroring adopt. Returns the updated row, or
    ``None`` if no such session exists.
    """
    _ph = ph()
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"UPDATE sessions SET instance_id = {_ph}, updated_at = {_ph} "
            f"WHERE id = {_ph} RETURNING *",
            (instance_id, now, session_id),
        )
        return row_to_dict(cursor.fetchone())


def unpin_session(session_id: str, instance_id: str) -> Optional[SessionRow]:
    """Take ownership and drop the sticky pin (follow-me / unpin).

    Writes ``instance_id`` AND ``placement = NULL`` so the session follows
    whoever sends next (send-path auto-adopt). Used by ``POST /adopt {follow: true}``.
    ``sdk_session_id`` is left for the caller to apply the keep-predicate.
    ``updated_at`` is bumped, mirroring adopt. Returns the updated row, or
    ``None`` if no such session exists.
    """
    _ph = ph()
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"UPDATE sessions SET instance_id = {_ph}, placement = {_ph}, "
            f"updated_at = {_ph} WHERE id = {_ph} RETURNING *",
            (instance_id, None, now, session_id),
        )
        return row_to_dict(cursor.fetchone())


def pin_session_placement(session_id: str, runner_id: str) -> bool:
    """Sticky-pin a session to ``runner_id``.

    One UPDATE writes both halves of the dual-write: ``placement`` becomes the
    canonical ``{"where": "runner:<id>"}`` pin AND the legacy ``instance_id``
    owner column is re-pointed so every ``instance_id`` reader (board filters)
    agrees with the new truth. ``cloud_branch`` is deliberately NEVER written.

    Called by the FIRST winner of ``start_turn`` on an "anywhere"/capability
    placement — after this the session is runner-pinned and stays put (CLI
    state now lives on that runner's disk). Any capability ``constraints`` in
    the prior placement are INTENTIONALLY dropped at pin time: a pin means
    "the CLI conversation state lives here", and pins are validated by
    online + capability only at resolve time — capability re-validation does
    not apply to pins, so carrying the constraints forward
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
            f"UPDATE sessions SET project_id = {_ph}, updated_at = {_ph} "
            f"WHERE id = {_ph} RETURNING *",
            (project_id, now, session_id),
        )
        return row_to_dict(cursor.fetchone())


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
                f"updated_at = {_ph} WHERE id = {_ph} RETURNING *",
                (branch, project_v2_id, now, session_id),
            )
        else:
            cursor.execute(
                f"UPDATE sessions SET branch = {_ph}, updated_at = {_ph} "
                f"WHERE id = {_ph} RETURNING *",
                (branch, now, session_id),
            )
        return row_to_dict(cursor.fetchone())


def resync_session_branch(session_id: str, branch: str) -> Optional[SessionRow]:
    """Correct a session's recorded branch to the one its checkout is really on.

    :func:`set_session_branch` is the *intake* write — the user picks a branch,
    the app records it. Nothing wrote it again afterwards, so a session whose
    agent ran a plain ``git checkout -b`` inside its worktree kept the old name
    in its row forever, and everything reading that row (the sidebar's PR state
    most visibly) kept answering about the branch the session had left. The
    session that shipped the sidebar's second-PR fix desynced its own row this
    way.

    Writes ``branch`` and — only when the session already carries the legacy
    ``worktree_branch`` — that column too, since PR reconciliation prefers it.
    A session that never had one does not acquire one here: an empty
    ``worktree_branch`` is how the v2 branch-intake path marks itself, and
    filling it in would make a v2 session look legacy. Returns the updated row,
    or ``None`` when no such session exists.
    """
    _ph = ph()
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"UPDATE sessions SET branch = {_ph}, "
            "worktree_branch = CASE "
            "WHEN worktree_branch IS NULL OR worktree_branch = '' "
            f"THEN worktree_branch ELSE {_ph} END, "
            f"updated_at = {_ph} WHERE id = {_ph} RETURNING *",
            (branch, branch, now, session_id),
        )
        return row_to_dict(cursor.fetchone())


def update_session_pr_state(
    session_id: str,
    *,
    pr_number: Optional[int] = None,
    pr_url: Optional[str] = None,
    pr_state: Optional[str] = None,
) -> Optional[SessionRow]:
    """Write persisted PR lifecycle fields on a session. Does not touch is_active.

    Only columns whose argument is not None are updated, so a merge can flip
    ``pr_state`` without re-supplying number/url. Returns the updated row, or
    None when no such session exists.
    """
    _ph = ph()
    now = datetime.now(timezone.utc).isoformat()
    updates = [f"updated_at = {_ph}"]
    values: list = [now]
    if pr_number is not None:
        updates.append(f"pr_number = {_ph}")
        values.append(pr_number)
    if pr_url is not None:
        updates.append(f"pr_url = {_ph}")
        values.append(pr_url)
    if pr_state is not None:
        updates.append(f"pr_state = {_ph}")
        values.append(pr_state)
    if len(updates) == 1:
        return get_session(session_id)
    values.append(session_id)
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"UPDATE sessions SET {', '.join(updates)} WHERE id = {_ph} RETURNING *",
            values,
        )
        return row_to_dict(cursor.fetchone())


def list_sessions_by_pr_number(pr_number: int) -> list:
    """Sessions that already carry this pull-request number."""
    _ph = ph()
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"SELECT * FROM sessions WHERE pr_number = {_ph}",
            (pr_number,),
        )
        return [row_to_dict(r) for r in cursor.fetchall()]


def list_sessions_for_cleanup(project_id: str, project_v2_id: Optional[str] = None) -> list:
    """All potential checkout users, including hidden workers and older sessions.

    Unlike the sidebar list, this safety query has no page or install filter.
    V2 sessions may have no legacy project_id, or a different legacy alias.
    """
    _ph = ph()
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"SELECT * FROM sessions WHERE project_id = {_ph} OR project_v2_id = {_ph}",
            (project_id, project_v2_id or project_id),
        )
        return [row_to_dict(r) for r in cursor.fetchall()]


def list_sessions_for_branch(project_id: str, branch: str) -> list:
    """Sessions on this project whose logical branch matches ``branch``."""
    _ph = ph()
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"SELECT * FROM sessions WHERE project_id = {_ph}"
            f" AND (worktree_branch = {_ph} OR branch = {_ph})",
            (project_id, branch, branch),
        )
        return [row_to_dict(r) for r in cursor.fetchall()]


def list_sessions_for_v2_branch(project_v2_id: str, branch: str) -> list:
    """Sessions bound to ``branch`` of origin-keyed project ``project_v2_id``."""
    _ph = ph()
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"SELECT * FROM sessions WHERE project_v2_id = {_ph}"
            f" AND (worktree_branch = {_ph} OR branch = {_ph})",
            (project_v2_id, branch, branch),
        )
        return [row_to_dict(r) for r in cursor.fetchall()]


def latest_pr_number_for_branch(
    branch: str, project_v2_id: Optional[str] = None
) -> Optional[int]:
    """Most recently updated PR number recorded on a session for ``branch``."""
    if not branch:
        return None
    _ph = ph()
    with get_db() as conn:
        cursor = get_cursor(conn)
        if project_v2_id:
            cursor.execute(
                f"SELECT pr_number FROM sessions WHERE pr_number IS NOT NULL "
                f"AND (branch = {_ph} OR worktree_branch = {_ph}) "
                f"AND project_v2_id = {_ph} "
                f"ORDER BY updated_at DESC LIMIT 1",
                (branch, branch, project_v2_id),
            )
        else:
            cursor.execute(
                f"SELECT pr_number FROM sessions WHERE pr_number IS NOT NULL "
                f"AND (branch = {_ph} OR worktree_branch = {_ph}) "
                f"ORDER BY updated_at DESC LIMIT 1",
                (branch, branch),
            )
        row = cursor.fetchone()
    if not row:
        return None
    d = row_to_dict(row)
    n = d.get("pr_number")
    try:
        return int(n) if n is not None else None
    except (TypeError, ValueError):
        return None


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
    pointer.

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


#: Session-scoped RUNTIME tables that carry a ``session_id`` but no foreign key
#: into ``sessions``, so nothing cascades for them. Every one of these describes
#: how a session RAN — its turn queue, its event log and seq counter, its runner
#: leases, its pending prompts, its background-task mirror. None of it means
#: anything once the session is gone, and all of it is what the live DB was
#: found holding for sessions deleted months earlier. Deleting them here gives
#: them the same lifetime the FK-carrying tables (``session_messages``,
#: ``sync_log``, ``session_tags``, ``queued_messages``) already get for free.
_SESSION_RUNTIME_TABLES = (
    "turns",
    "session_events",
    "session_event_counters",
    "runs",
    "pending_interactions",
    "background_tasks",
)


def delete_session(session_id: str) -> bool:
    """Delete a session, its messages, and its session-scoped runtime rows.

    Three different lifetimes, deliberately:

    * **Runtime state is deleted** (``_SESSION_RUNTIME_TABLES``). It has no
      meaning without the session and no FK to remove it, so it simply
      accumulated — orphan ``turns`` / ``session_events`` / ``runs`` rows for
      sessions that no longer exist.
    * **The usage ledger is UNLINKED, not deleted** (``usage_log.session_id ->
      NULL``). Those rows are the spend history the analytics endpoints
      aggregate; deleting them would silently rewrite finished cost reports for
      past days. This is exactly what ``delete_profile`` already does with
      ``usage_log.profile_id``, and what the schema's own soft links
      (``connector_sessions``, ``automation_runs``) express as
      ``ON DELETE SET NULL``.
    * **Child sessions SURVIVE, unlinked** (``parent_session_id -> NULL``).
      A child here is a fork or a ProviderAgent worker: an independently
      readable transcript with its own row in the session lists. Cascading the
      delete down the tree would destroy conversations the user never asked to
      remove, so the pointer is cleared instead — the same ``ON DELETE SET
      NULL`` shape ``sessions`` already uses for ``project_id`` /
      ``profile_id`` / ``worktree_id``. Their own runtime rows stay valid
      because their session rows do.

    All of it runs in the single transaction that drops the session row, so a
    failure leaves the session and its dependents intact together.
    """
    # Best-effort scratch-ref cleanup before the row dies, so a
    # hard-deleted session leaves no dangling working-set ref on origin. Fully
    # flag-gated and failure-swallowed — flag OFF (or no ref) issues ZERO git
    # commands, and any git failure never blocks the delete.
    _cleanup_working_set_ref(session_id)
    _cleanup_pending_wakeups(session_id)
    _ph = ph()
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"UPDATE sessions SET parent_session_id = NULL "
            f"WHERE parent_session_id = {_ph}",
            (session_id,),
        )
        cursor.execute(
            f"UPDATE usage_log SET session_id = NULL WHERE session_id = {_ph}",
            (session_id,),
        )
        for table in _SESSION_RUNTIME_TABLES:
            # Table names are a module-level literal tuple, never user input.
            cursor.execute(
                f"DELETE FROM {table} WHERE session_id = {_ph}", (session_id,)
            )
        cursor.execute(f"DELETE FROM sessions WHERE id = {_ph}", (session_id,))
        return cursor.rowcount > 0


def _cleanup_pending_wakeups(session_id: str) -> None:
    """Drop the session's pending wakeups (and disarm their local timers).

    ``pending_wakeups`` carries no FK to ``sessions``, so without this a
    deleted session's armed wake survives and later fires a turn against a row
    that no longer exists. Best-effort: a failure here never blocks the delete.
    """
    try:
        from app.core import wakeup_service

        wakeup_service.cancel_session_wakeups(session_id)
    except Exception:  # pragma: no cover - best-effort cleanup
        logger.debug(
            "pending-wakeup cleanup failed for session %s", session_id,
            exc_info=True,
        )


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


def set_session_active(
    session_id: str, is_active: bool, archived: Optional[bool] = None
) -> Optional[SessionRow]:
    """Set the is_active flag (and optionally the archive state) for a session
    and return the updated session. Archiving always leaves Active; restoring
    only touches a row that is actually archived. Persistent assistant homes
    can be neither settled nor archived.

    On a networked worker a plain membership change is shared sidebar state,
    committed independently of execution ownership."""
    from app.network import engine, session_active
    if archived is None and engine.context() is not None:
        return session_active.write_shared(session_id, is_active)
    _ph = ph()
    with get_db() as conn:
        if archived or not is_active:
            from app.db.assistant import assert_can_settle
            assert_can_settle(conn, session_id)
        cursor = get_cursor(conn)
        status_sql, params = archive_status_sql(archived, _ph)
        cursor.execute(
            f"""UPDATE sessions SET is_active = {_ph}, updated_at = {_ph}{status_sql}
               WHERE id = {_ph} RETURNING *""",
            (False if archived else is_active, datetime.now(timezone.utc).isoformat(), *params, session_id)
        )
        return row_to_dict(cursor.fetchone())


def archive_status_sql(archived: Optional[bool], placeholder: str) -> tuple[str, tuple]:
    """The ``status`` assignment for an archive change, as SQL plus params."""
    if archived is None:
        return "", ()
    if archived:
        return f", status = {placeholder}", ("archived",)
    return (f", status = CASE WHEN status = {placeholder} THEN {placeholder} ELSE status END",
            ("archived", "active"))


def set_sessions_active(
    session_ids: List[str], is_active: bool, archived: Optional[bool] = None
) -> List[SessionRow]:
    """Set-based ``set_session_active`` for a worker that is not networked.

    One UPDATE covers every id. When the change settles or archives, persistent
    assistant homes are excluded inside the same statement, so a home never
    appears in the returned rows; callers report missing ids individually.
    Networked workers must use the per-session membership paths instead.
    """
    if not session_ids:
        return []
    _ph = ph()
    marks = ", ".join([_ph] * len(session_ids))
    status_sql, status_params = archive_status_sql(archived, _ph)
    guard = _assistant_home_filter() if archived or not is_active else ""
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"""UPDATE sessions SET is_active = {_ph}, updated_at = {_ph}{status_sql}
               WHERE id IN ({marks}){guard} RETURNING *""",
            (False if archived else is_active, datetime.now(timezone.utc).isoformat(),
             *status_params, *session_ids)
        )
        return rows_to_list(cursor.fetchall())


def get_sessions_by_ids(session_ids: List[str]) -> Dict[str, SessionRow]:
    """Fetch many sessions in one query, keyed by id; unknown ids are absent."""
    if not session_ids:
        return {}
    _ph = ph()
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(f"SELECT * FROM sessions WHERE id IN ({', '.join([_ph] * len(session_ids))})",
                       tuple(session_ids))
        return {row["id"]: row for row in rows_to_list(cursor.fetchall())}


def get_settled_sessions(api_user_id: str, limit: int, offset: int = 0) -> List[SessionRow]:
    """Settled (inactive, not archived) list-visible top-level rows, newest first.

    A coarse prefilter for bulk sidebar management. ``api_user_id`` follows
    ``get_sessions``: an empty string selects admin rows (NULL). Callers still
    apply the per-row visibility, owner and project checks ListSessions uses.
    """
    _ph = ph()
    inst_sql, params = _instance_filter(_ph, "")
    query = (f"SELECT * FROM sessions WHERE 1=1{inst_sql}"
             f" AND (is_active IS NULL OR is_active = FALSE) AND (status IS NULL OR status <> {_ph})"
             " AND COALESCE(hidden, FALSE) = FALSE AND COALESCE(is_worker, FALSE) = FALSE"
             " AND parent_session_id IS NULL" + _assistant_home_filter())
    params = [*params, "archived"]
    if api_user_id:
        query += f" AND api_user_id = {_ph}"
        params.append(api_user_id)
    else:
        query += " AND api_user_id IS NULL"
    query += f" ORDER BY updated_at DESC, id LIMIT {_ph} OFFSET {_ph}"
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(query, (*params, limit, offset))
        return rows_to_list(cursor.fetchall())


def set_session_hidden(session_id: str, hidden: bool) -> Optional[SessionRow]:
    """Set the list-visibility flag for a session and return the updated
    session. Hidden sessions drop out of the list queries but stay fully
    functional and fetchable by id."""
    _ph = ph()
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"""UPDATE sessions SET hidden = {_ph}, updated_at = {_ph}
               WHERE id = {_ph} RETURNING *""",
            (hidden, datetime.now(timezone.utc).isoformat(), session_id)
        )
        return row_to_dict(cursor.fetchone())


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
    legacy ``session_messages`` rows, zero the persisted usage counters, and —
    because the v2 SSE feed renders the transcript by replaying
    ``session_events`` from seq 0 — drop the event log and its seq counter too
    (see the inline comment below for why the counter resets rather than
    continuing).

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

        # Drop the v2 event log AND its seq counter — in this same transaction.
        # The SSE feed replays ``session_events`` from seq 0 as the transcript
        # authority, so rows left here resurrect the "cleared" conversation on
        # every fresh hydration (hard reload, new tab, transcript-cache miss)
        # no matter what the wipes above did. Deleting the counter row too puts
        # the session in exactly a brand-new session's state: the next append
        # re-seeds the counter and allocates from seq 1, and ``get_max_seq``
        # (which reads ONLY the counter) reports 0 instead of claiming events
        # an empty log doesn't hold. That reset is safe for live SSE
        # subscribers — every read path filters ``seq > cursor``, so a tab
        # still tailing the old high cursor simply goes quiet until the
        # ``user_session_cleared`` broadcast restarts its feed from seq 0;
        # continuing the old counter instead would splice the NEXT
        # conversation's events onto the stale transcript of any tab that
        # missed that broadcast. Same treatment ``delete_session`` gives both
        # tables via ``_SESSION_RUNTIME_TABLES``. Deleting every row also
        # changes the log epoch (``session_events.get_log_epoch``, the id of the
        # first row), which is how a tab resuming with ``from_seq`` + ``epoch``
        # learns its cursor is stale and gets a ``reset`` instead of silence.
        cursor.execute(
            f"DELETE FROM session_events WHERE session_id = {_ph}",
            (session_id,),
        )
        cursor.execute(
            f"DELETE FROM session_event_counters WHERE session_id = {_ph}",
            (session_id,),
        )
        # The worker network publishes only ``seq > published_seq``; with the
        # log restarting at seq 1 the cursor must restart too, or mirrors stay
        # frozen until the new transcript outgrows the old one.
        cursor.execute(
            f"DELETE FROM network_catalog_cursors WHERE session_id = {_ph}",
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


def _search_literal_pattern(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _native_search_cte(projection: str, placeholder: str, instance_sql: str) -> str:
    """Search visible user/assistant text, never tool payloads or system prompts.

    The raw JSON prefilter uses the existing trigram index. The extracted text
    predicate is authoritative so metadata cannot produce a false match.
    """
    return f"""
        native_matches AS (
            SELECT DISTINCT ON (s.id)
                {projection},
                'content' as match_type,
                substring(texts.content FROM greatest(1, strpos(lower(texts.content), lower({placeholder})) - 60)
                    FOR 240) as match_snippet,
                to_timestamp(n.ts) as match_time
            FROM fluid_session_nodes n
            INNER JOIN sessions s ON s.sdk_session_id = n.session_id
            CROSS JOIN LATERAL (
                SELECT string_agg(block->>'text', ' ' ORDER BY ord) AS content
                FROM jsonb_array_elements(
                    CASE WHEN jsonb_typeof(n.message->'content') = 'array'
                    THEN n.message->'content' ELSE '[]'::jsonb END
                ) WITH ORDINALITY AS blocks(block, ord)
                WHERE block->>'kind' = 'text'
            ) texts
            WHERE n.message::text ILIKE {placeholder}
            AND texts.content ILIKE {placeholder}
            AND n.message->>'role' IN ('user', 'assistant')
            AND s.status NOT IN ('archived','failed'){instance_sql}
            ORDER BY s.id, n.ts DESC
        ),
    """


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

    literal_pattern = _search_literal_pattern(query.strip())
    raw_pattern = _search_literal_pattern(json.dumps(query.split()[0], ensure_ascii=False)[1:-1])
    search_term = literal_pattern
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
        native_cte = _native_search_cte("s.id, s.project_id, s.profile_id, s.api_user_id, s.title, s.status, s.is_active, s.total_cost_usd, s.total_tokens_in, s.total_tokens_out, s.turn_count, s.created_at, s.updated_at", _ph, inst_sql)
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
            WHERE (sm.content_tsv @@ plainto_tsquery('english', {_ph}) OR sm.content ILIKE {_ph})
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
            AND (to_tsvector('english', sse_entry_text(sse.entry)) @@ plainto_tsquery('english', {_ph})
                 OR sse_entry_text(sse.entry) ILIKE {_ph})
            AND s.status NOT IN ('archived','failed'){inst_sql}
            ORDER BY s.id, sse.created_at DESC
        ),
        {native_cte}
        all_matches AS (
            SELECT *, COUNT(*) OVER (PARTITION BY id) as match_count
            FROM (
                SELECT * FROM title_matches
                UNION ALL
                SELECT * FROM content_matches
                UNION ALL
                SELECT * FROM store_matches
                UNION ALL
                SELECT * FROM native_matches
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
            fts_query, fts_query, literal_pattern, *inst_params,
            fts_query, fts_query, literal_pattern, *inst_params,
            query.strip(), raw_pattern, literal_pattern, *inst_params,
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
    Matches titles and user/assistant messages across legacy, SDK and native
    transcripts. Literal substring matching supplements legacy full-text search.

    Excludes ``status IN ('archived','failed')`` in every CTE, for the reasons
    spelled out on :func:`search_sessions`.

    Returns:
        Tuple of (results list, total count)
    """
    if not query or not query.strip():
        return [], 0

    literal_pattern = _search_literal_pattern(query.strip())
    raw_pattern = _search_literal_pattern(json.dumps(query.split()[0], ensure_ascii=False)[1:-1])
    search_term = literal_pattern
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
        native_cte = _native_search_cte("s.*", _ph, inst_sql)
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
            WHERE (sm.content_tsv @@ plainto_tsquery('english', {_ph}) OR sm.content ILIKE {_ph})
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
            AND (to_tsvector('english', sse_entry_text(sse.entry)) @@ plainto_tsquery('english', {_ph})
                 OR sse_entry_text(sse.entry) ILIKE {_ph})
            AND s.status NOT IN ('archived','failed'){inst_sql}
            ORDER BY s.id, sse.created_at DESC
        ),
        {native_cte}
        all_matches AS (
            SELECT *, COUNT(*) OVER (PARTITION BY id) as match_count
            FROM (
                SELECT * FROM title_matches
                UNION ALL
                SELECT * FROM content_matches
                UNION ALL
                SELECT * FROM store_matches
                UNION ALL
                SELECT * FROM native_matches
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
            fts_query, fts_query, literal_pattern, *inst_params,
            fts_query, fts_query, literal_pattern, *inst_params,
            query.strip(), raw_pattern, literal_pattern, *inst_params,
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


from app.core.resource_sync import install_database_mutations as _install_resource_sync

_install_resource_sync(
    globals(), 'sessions',
    'create_session create_session_with_stats update_session set_session_run_binding patch_voice_session_context adopt_session adopt_session_follow unpin_session pin_session_placement set_session_project set_session_branch resync_session_branch update_session_pr_state set_session_parked_task_kind set_working_set_ref delete_session set_session_active set_session_hidden clear_session_conversation delete_session_message delete_orphan_user_turn discard_orphan_first_turn delete_session_messages_after_jsonl_uuid',
    scope='session',
)
