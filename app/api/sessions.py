"""
Session management API routes

This module was split for maintainability. Sub-modules:
- sessions_helpers: shared helpers, models, constants
- sessions_export: export/import endpoints
- sessions_fork: fork endpoint
- sessions_worktree: worktree session endpoint

All public symbols are re-exported here for backward compatibility.
"""

from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Depends, Query, status, Request

try:
    from claude_agent_sdk import get_session_messages as sdk_get_session_messages
except ImportError:
    sdk_get_session_messages = None

try:
    from claude_agent_sdk import rename_session as sdk_rename_session
except ImportError:
    sdk_rename_session = None

try:
    from claude_agent_sdk import tag_session as sdk_tag_session
except ImportError:
    sdk_tag_session = None

try:
    from claude_agent_sdk import get_session_info as sdk_get_session_info
except ImportError:
    sdk_get_session_info = None

try:
    from claude_agent_sdk import delete_session as sdk_delete_session
except ImportError:
    sdk_delete_session = None

try:
    from claude_agent_sdk import (
        list_subagents as sdk_list_subagents,
        get_subagent_messages as sdk_get_subagent_messages,
    )
except ImportError:
    sdk_list_subagents = None
    sdk_get_subagent_messages = None

from app.core.models import (
    Session,
    SessionWithMessages,
    SessionSearchResult,
    PaginatedSessions,
    CursorPaginatedSessions,
    PaginatedSessionSearchResults
)
from app.db import database
from app.db.async_database import run_sync
from app.api.auth import require_auth, get_api_user_from_request
from app.api.common import api_error_handler
from app.api.worktree_session_meta import enrich_session_worktree_meta
from app.core.project_utils import get_project_working_dir

import logging
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Re-export all public symbols from sub-modules for backward compatibility
# ---------------------------------------------------------------------------
from app.api.sessions_helpers import (  # noqa: F401 — re-exports
    _SESSION_SYNC_THROTTLE_S,
    _last_sync_times,
    _transform_sdk_messages,
    parse_session_history_via_store,
    check_session_access,
    require_session,
    enrich_sessions_with_tags,
    enrich_search_results_with_tags,
    _apply_session_filters,
    BatchDeleteRequest,
    SessionImportData,
    SessionImportResponse,
    ImportJSONLRequest,
    ImportJSONLResponse,
    ForkSessionRequest,
    ForkSessionResponse,
    CreateWorktreeSessionRequest,
    WorktreeSessionResponse,
)

from app.api.sessions_export import (  # noqa: F401 — re-exports
    export_router,
    export_session,
    import_session,
    import_jsonl_session,
)

from app.api.sessions_fork import (  # noqa: F401 — re-exports
    fork_router,
    fork_session,
)

from app.api.sessions_worktree import (  # noqa: F401 — re-exports
    worktree_router,
    create_session_with_worktree,
)

from app.api.sessions_cloud import (  # noqa: F401 — re-exports
    cloud_router,
    create_cloud_session,
)

# ---------------------------------------------------------------------------
# Router setup — include sub-routers so all endpoints are served
# ---------------------------------------------------------------------------
router = APIRouter(prefix="/api/v1/sessions", tags=["Sessions"])
router.include_router(export_router)
router.include_router(fork_router)
router.include_router(worktree_router)
router.include_router(cloud_router)


# ============================================================================
# CRUD / List endpoints (kept in this file)
# ============================================================================

@router.get("", response_model=List[Session])
async def list_sessions(
    request: Request,
    project_id: Optional[str] = Query(None, description="Filter by project"),
    profile_id: Optional[str] = Query(None, description="Filter by profile"),
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by status"),
    api_user_id: Optional[str] = Query(None, description="Filter by API user ID (admin only)"),
    admin_only: bool = Query(False, description="Show only admin sessions (no API user)"),
    api_users_only: bool = Query(False, description="Show only API user sessions (exclude admin sessions)"),
    favorites_only: bool = Query(False, description="Show only favorited sessions"),
    tag_id: Optional[str] = Query(None, description="Filter by tag ID"),
    sync_claude_sessions: bool = Query(True, description="Auto-import Claude Code sessions for project"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    token: str = Depends(require_auth)
):
    """
    List sessions with optional filters. API users only see sessions for their assigned project/profile.

    When filtering by project_id and sync_claude_sessions=true (default), this endpoint will
    automatically discover and import any Claude Code sessions that exist on disk but are not
    yet in the database. This ensures users see all their sessions regardless of whether they
    were created in Claude Code or AI Shuffle.
    """
    project_id, profile_id, filter_api_user_id, filter_api_users_only = await _apply_session_filters(
        request, project_id, profile_id, api_user_id, admin_only, api_users_only, sync_claude_sessions
    )

    sessions = await run_sync(
        database.get_sessions,
        project_id=project_id,
        profile_id=profile_id,
        status=status_filter,
        api_user_id=filter_api_user_id,
        api_users_only=filter_api_users_only,
        favorites_only=favorites_only,
        tag_id=tag_id,
        limit=limit,
        offset=offset
    )
    return enrich_sessions_with_tags(sessions)


@router.get("/paginated", response_model=PaginatedSessions)
async def list_sessions_paginated(
    request: Request,
    project_id: Optional[str] = Query(None, description="Filter by project"),
    profile_id: Optional[str] = Query(None, description="Filter by profile"),
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by status"),
    api_user_id: Optional[str] = Query(None, description="Filter by API user ID (admin only)"),
    admin_only: bool = Query(False, description="Show only admin sessions (no API user)"),
    api_users_only: bool = Query(False, description="Show only API user sessions (exclude admin sessions)"),
    favorites_only: bool = Query(False, description="Show only favorited sessions"),
    tag_id: Optional[str] = Query(None, description="Filter by tag ID"),
    sync_claude_sessions: bool = Query(True, description="Auto-import Claude Code sessions for project"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    token: str = Depends(require_auth)
):
    """
    List sessions with pagination metadata (total count, has_more).
    Use this endpoint for infinite scroll UIs that need to know the total count.
    """
    project_id, profile_id, filter_api_user_id, filter_api_users_only = await _apply_session_filters(
        request, project_id, profile_id, api_user_id, admin_only, api_users_only, sync_claude_sessions
    )

    sessions, total = await run_sync(
        database.get_sessions_paginated,
        project_id=project_id,
        profile_id=profile_id,
        status=status_filter,
        api_user_id=filter_api_user_id,
        api_users_only=filter_api_users_only,
        favorites_only=favorites_only,
        tag_id=tag_id,
        limit=limit,
        offset=offset
    )

    enriched = enrich_sessions_with_tags(sessions)
    has_more = offset + len(sessions) < total

    return PaginatedSessions(
        items=enriched,
        total=total,
        offset=offset,
        limit=limit,
        has_more=has_more
    )


@router.get("/cursor", response_model=CursorPaginatedSessions)
async def list_sessions_cursor(
    request: Request,
    project_id: Optional[str] = Query(None, description="Filter by project"),
    profile_id: Optional[str] = Query(None, description="Filter by profile"),
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by status"),
    api_user_id: Optional[str] = Query(None, description="Filter by API user ID (admin only)"),
    admin_only: bool = Query(False, description="Show only admin sessions (no API user)"),
    api_users_only: bool = Query(False, description="Show only API user sessions (exclude admin sessions)"),
    favorites_only: bool = Query(False, description="Show only favorited sessions"),
    tag_id: Optional[str] = Query(None, description="Filter by tag ID"),
    sync_claude_sessions: bool = Query(True, description="Auto-import Claude Code sessions for project"),
    limit: int = Query(50, ge=1, le=100),
    cursor: Optional[str] = Query(None, description="Cursor from previous response (updated_at timestamp)"),
    cursor_id: Optional[str] = Query(None, description="Cursor ID from previous response (session ID tiebreaker)"),
    token: str = Depends(require_auth)
):
    """
    List sessions with cursor-based pagination (keyset pagination).
    More efficient than offset-based pagination for deep scrolling — O(1) seek
    instead of O(n) scan-and-discard.
    """
    project_id, profile_id, filter_api_user_id, filter_api_users_only = await _apply_session_filters(
        request, project_id, profile_id, api_user_id, admin_only, api_users_only, sync_claude_sessions
    )

    sessions, next_cursor, next_cursor_id, has_more = await run_sync(
        database.get_sessions_cursor,
        project_id=project_id,
        profile_id=profile_id,
        status=status_filter,
        api_user_id=filter_api_user_id,
        api_users_only=filter_api_users_only,
        favorites_only=favorites_only,
        tag_id=tag_id,
        limit=limit,
        cursor=cursor,
        cursor_id=cursor_id,
    )

    enriched = enrich_sessions_with_tags(sessions)

    return CursorPaginatedSessions(
        items=enriched,
        next_cursor=next_cursor,
        next_cursor_id=next_cursor_id,
        has_more=has_more,
    )


@router.get("/search/query", response_model=List[SessionSearchResult])
async def search_sessions(
    request: Request,
    q: str = Query(..., min_length=1, description="Search query"),
    project_id: Optional[str] = Query(None, description="Filter by project"),
    profile_id: Optional[str] = Query(None, description="Filter by profile"),
    admin_only: bool = Query(False, description="Show only admin sessions"),
    limit: int = Query(20, ge=1, le=50),
    token: str = Depends(require_auth)
):
    """
    Search sessions by title and message content.
    Returns sessions with snippets showing where the match was found.
    API users only see sessions for their assigned project/profile.
    """
    api_user = get_api_user_from_request(request)

    # Force API user restrictions
    filter_api_user_id = None
    if api_user:
        if api_user.get("project_id"):
            project_id = api_user["project_id"]
        if api_user.get("profile_id"):
            profile_id = api_user["profile_id"]
        filter_api_user_id = api_user["id"]
        admin_only = False
    elif admin_only:
        # Admin requesting only admin sessions
        filter_api_user_id = None

    results = await run_sync(
        database.search_sessions,
        query=q,
        project_id=project_id,
        profile_id=profile_id,
        api_user_id=filter_api_user_id,
        admin_only=admin_only,
        limit=limit
    )

    return results


@router.get("/search/paginated", response_model=PaginatedSessionSearchResults)
async def search_sessions_paginated(
    request: Request,
    q: str = Query(..., min_length=1, description="Search query"),
    project_id: Optional[str] = Query(None, description="Filter by project"),
    profile_id: Optional[str] = Query(None, description="Filter by profile"),
    admin_only: bool = Query(False, description="Show only admin sessions"),
    limit: int = Query(20, ge=1, le=50),
    offset: int = Query(0, ge=0),
    token: str = Depends(require_auth)
):
    """
    Search sessions by title and message content with pagination metadata.
    Use this endpoint for infinite scroll search UIs.
    """
    api_user = get_api_user_from_request(request)

    # Force API user restrictions
    filter_api_user_id = None
    if api_user:
        if api_user.get("project_id"):
            project_id = api_user["project_id"]
        if api_user.get("profile_id"):
            profile_id = api_user["profile_id"]
        filter_api_user_id = api_user["id"]
        admin_only = False
    elif admin_only:
        filter_api_user_id = None

    results, total = await run_sync(
        database.search_sessions_paginated,
        query=q,
        project_id=project_id,
        profile_id=profile_id,
        api_user_id=filter_api_user_id,
        admin_only=admin_only,
        limit=limit,
        offset=offset
    )

    enriched = enrich_search_results_with_tags(results)
    has_more = offset + len(results) < total

    return PaginatedSessionSearchResults(
        items=enriched,
        total=total,
        offset=offset,
        limit=limit,
        has_more=has_more
    )


@router.get("/{session_id}", response_model=SessionWithMessages)
async def get_session(request: Request, session_id: str, token: str = Depends(require_auth)):
    """Get a session with its message history. API users can only access their assigned sessions."""
    import traceback

    logger.debug(f"Loading session: {session_id}")

    # Try direct lookup by AI Shuffle session ID first
    session = await run_sync(database.get_session, session_id)

    # If not found, try fallback lookup by SDK session ID (Claude Code UUID)
    # This handles cases where old references might use the SDK session ID
    if not session:
        session = await run_sync(database.get_session_by_sdk_id, session_id)
        if session:
            logger.debug(f"Session found via SDK session ID fallback: {session_id} -> {session['id']}")

    if not session:
        logger.warning(f"Session not found in database: {session_id}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}"
        )

    check_session_access(request, session)

    # Try to load messages from JSONL file first (source of truth for consistency)
    sdk_session_id = session.get("sdk_session_id")
    messages = []
    working_dir = "/workspace"

    from app.core.config import settings as _store_settings
    _postgres_only = _store_settings.session_store_mode == "postgres_only"

    if sdk_session_id:
        try:
            from app.core.jsonl_parser import parse_session_history
            from app.core.message_resolver import resolve_working_dir
            from app.core.config import settings

            # Resolve working dir preferring worktree → project → default.
            # Worktree sessions store JSONL under the worktree's encoded path;
            # falling straight to the project path misses the direct lookup
            # and relies on a cross-dir scan to recover.
            working_dir = await run_sync(resolve_working_dir, dict(session))

            if _postgres_only:
                # postgres_only read cutover: Postgres (session_store_entries) is
                # the source of truth. Load the rich transcript from the store and
                # run it through the SAME transform as the JSONL path (so
                # messages/usage are identical). The JSONL block is skipped
                # entirely. In mirror/disabled this branch is never entered.
                jsonl_messages, usage_data = await run_sync(
                    parse_session_history_via_store, sdk_session_id, working_dir
                )
            else:
                jsonl_messages, usage_data = await run_sync(parse_session_history, sdk_session_id, working_dir)
            if jsonl_messages:
                # Get profile_id map (cached to avoid loading all DB messages every time)
                from app.core.cost_utils import get_cached_profile_map
                db_profile_map = get_cached_profile_map(session_id)

                # Transform to expected format for SessionWithMessages
                # Use camelCase for frontend compatibility (toolName, toolInput, toolId)
                for i, m in enumerate(jsonl_messages):
                    # Timestamps live in metadata (not Pydantic datetime fields);
                    # the frontend reads them directly from metadata for display.
                    msg_data = {
                        "id": m.get("id", i),
                        "role": m.get("role", "user"),
                        "content": m.get("content", ""),
                        "type": m.get("type"),  # Critical for tool_use/tool_result rendering
                        "subtype": m.get("subtype"),  # For system messages (e.g., local_command)
                        "systemSubtype": m.get("systemSubtype"),  # Frontend-compatible system subtype
                        "systemData": m.get("systemData"),  # Structured data for system messages
                        "toolName": m.get("toolName"),  # camelCase for frontend
                        "toolInput": m.get("toolInput"),  # camelCase for frontend
                        "toolId": m.get("toolId"),
                        "toolResult": m.get("toolResult"),  # Tool output grouped with tool_use
                        "toolStatus": m.get("toolStatus"),  # Status: running, complete, error
                        "toolScreenshot": m.get("toolScreenshot"),  # Inline screenshot (Chrome/computer-use, Read of image)
                        "tool_name": m.get("toolName"),  # Also include snake_case for compatibility
                        "tool_input": m.get("toolInput"),  # Also include snake_case for compatibility
                        "metadata": m.get("metadata"),
                        "profile_id": db_profile_map.get(m.get("content", "")) if m.get("role") == "user" else None,
                        "created_at": None  # Let Pydantic use default; timestamp is in metadata
                    }
                    # Include subagent-specific fields if present
                    if m.get("type") == "subagent":
                        msg_data["agentId"] = m.get("agentId")
                        msg_data["agentType"] = m.get("agentType")
                        msg_data["agentDescription"] = m.get("agentDescription")
                        msg_data["agentStatus"] = m.get("agentStatus")
                        msg_data["agentChildren"] = m.get("agentChildren")
                    messages.append(msg_data)

            # Token usage from JSONL (extracted in same pass as messages)
            logger.debug(f"[Session API] JSONL usage_data for {session_id}: {usage_data}")
            if usage_data:
                # Cache tokens are only in JSONL, always use those
                cache_creation = usage_data.get("cache_creation_tokens", 0)
                cache_read = usage_data.get("cache_read_tokens", 0)
                last_input = usage_data.get("last_input_tokens", 0)

                session["cache_creation_tokens"] = cache_creation
                session["cache_read_tokens"] = cache_read
                # Context window = last turn's input + cache creation + cache read
                session["context_tokens"] = last_input + cache_creation + cache_read

                # Input/output tokens - use JSONL if DB doesn't have them
                if session.get("total_tokens_in", 0) == 0 and session.get("total_tokens_out", 0) == 0:
                    session["total_tokens_in"] = usage_data.get("total_tokens_in", 0)
                    session["total_tokens_out"] = usage_data.get("total_tokens_out", 0)
            else:
                # No JSONL usage available (file missing, worktree removed, etc.)
                # Compute context from the per-turn DB columns that
                # _persist_turn_results maintains. These hold the last API
                # call's absolute values (not cumulative).
                last_input = session.get("last_input_tokens", 0) or 0
                cache_creation = session.get("cache_creation_tokens", 0) or 0
                cache_read = session.get("cache_read_tokens", 0) or 0
                session["context_tokens"] = last_input + cache_creation + cache_read
            logger.debug(f"[Session API] Final token values: tokens_in={session.get('total_tokens_in')}, tokens_out={session.get('total_tokens_out')}, cache_creation={session.get('cache_creation_tokens')}, cache_read={session.get('cache_read_tokens')}, context={session.get('context_tokens')}")
        except Exception as e:
            # Log the error but don't fail - fall back to database
            logger.error(f"Failed to parse JSONL for session {session_id}: {e}")
            messages = []

    # Mirror-mode drift check — no-op unless session_store_mode == "mirror".
    # Pure observability: compares the Postgres transcript mirror against the
    # JSONL we just read and logs any divergence (see app/core/session_store_verify).
    # Read-only; offloaded to a worker thread so it never blocks the loop, and
    # wrapped so a verification glitch can't affect the response.
    if sdk_session_id:
        from app.core.config import settings as _settings
        if _settings.session_store_mode == "mirror":
            try:
                from app.core.session_store_verify import log_session_store_drift
                await run_sync(log_session_store_drift, sdk_session_id, working_dir)
            except Exception:
                pass

    if _postgres_only:
        # postgres_only HARD-CUT: the store is the single source of truth. An
        # empty result here returns empty messages with a loud WARNING — it must
        # NEVER fall through to the SDK disk reader (Fallback 2, reads JSONL) or
        # the legacy session_messages table (Fallback 3, the least-rich "TRAP").
        # Those JSONL/legacy fallbacks below run ONLY in mirror/disabled.
        if not messages and sdk_session_id:
            logger.warning(
                "[Session API] postgres_only: no entries in session_store for "
                "session %s (sdk_session_id=%s) — returning empty messages; "
                "NOT falling back to JSONL/legacy DB.",
                session_id, sdk_session_id,
            )
    else:
        # Fallback 2: Try SDK get_session_messages() if custom parser failed.
        # The SDK handles JSONL parsing + conversation chain building independently,
        # so it may succeed where our parser has edge-case issues.
        if not messages and sdk_session_id and sdk_get_session_messages:
            try:
                sdk_msgs = sdk_get_session_messages(
                    session_id=sdk_session_id,
                    directory=working_dir,
                )
                if sdk_msgs:
                    messages = _transform_sdk_messages(sdk_msgs)
                    logger.debug(
                        f"Loaded {len(messages)} messages via SDK get_session_messages "
                        f"for session {session_id}"
                    )
            except Exception as e:
                logger.warning(f"SDK get_session_messages fallback failed for {session_id}: {e}")

        # Fallback 3: Database messages (oldest data, least rich)
        if not messages:
            db_messages = await run_sync(database.get_session_messages, session_id)
            # Transform DB messages to include type field for frontend compatibility
            for m in db_messages:
                msg = dict(m)
                # Infer type from role for legacy DB messages
                if msg.get("tool_name"):
                    msg["type"] = "tool_use"
                    msg["toolName"] = msg.get("tool_name")
                    msg["toolInput"] = msg.get("tool_input")
                elif msg.get("role") == "assistant":
                    msg["type"] = "text"
                messages.append(msg)

    # Attach cost/duration metadata to final assistant messages (cached, O(N))
    from app.core.cost_utils import attach_cost_metadata
    attach_cost_metadata(messages, session_id)

    session["messages"] = messages
    session["tags"] = await run_sync(database.get_session_tags, session_id)
    session["has_forks"] = await run_sync(database.session_has_forks, session_id)

    # Add worktree info if session is linked to a worktree. Worktree sessions are
    # always reopenable — we never null worktree_id; worktree_deleted only means
    # "not materialized right now" (recreated on next send). See helper.
    worktree_id = session.get("worktree_id")
    worktree = await run_sync(database.get_worktree, worktree_id) if worktree_id else None
    wt_meta = enrich_session_worktree_meta(session, worktree)
    if wt_meta:
        session["worktree_id"] = wt_meta["worktree_id"]
        session["worktree_branch"] = wt_meta["worktree_branch"]
        session["worktree_path"] = wt_meta["worktree_path"]
        session["worktree_deleted"] = wt_meta["worktree_deleted"]

    logger.debug(f"Returning session {session_id} with {len(messages)} messages")
    return session


@router.get("/{session_id}/live")
async def get_live_session(
    request: Request,
    session_id: str,
    since_update_id: int = Query(0, description="Only return if updated since this ID (for efficient polling)"),
    token: str = Depends(require_auth)
):
    """
    Get live session state for streaming sessions.

    This endpoint provides server-authoritative state during streaming.
    Frontend polls this endpoint instead of maintaining local message state.

    - If session is actively streaming: returns live state from memory
    - If session is not streaming: returns historical state from JSONL
    - If since_update_id provided and no updates: returns 304 Not Modified

    Response includes:
    - messages: Current message state (including in-progress streaming)
    - is_streaming: Whether session is currently streaming
    - last_update_id: For efficient polling (only fetch if updated)
    - usage: Token usage counts
    """
    from starlette.responses import Response
    from app.core.live_session_store import live_session_store
    from app.core.task_registry import task_registry

    # Check session exists and user has access
    session = await require_session(request, session_id)

    # Active background tasks (SDK agent tasks + Monitor shell subprocesses).
    # Attached fresh to EVERY local response path (delta or full, streaming or
    # not) — never routed through the live store's last_update_id delta logic, so
    # a task finishing is reflected on the very next poll. [] when none.
    active_tasks = task_registry.get_active(session_id)

    # Cloud sessions run on a second install — proxy the worker's live state so
    # the user sees the remote stream (and history) exactly like a local one.
    if session.get("cloud_remote_url"):
        from app.core import cloud_relay
        try:
            code, data = await cloud_relay.get_live(session, since_update_id)
        except cloud_relay.CloudRelayError as e:
            logger.warning("Cloud live proxy failed for %s: %s", session_id, e)
            return Response(status_code=503)
        if code == 304 or data is None:
            return Response(status_code=304)
        meta = data.get("session_meta") or {}
        meta["execution_mode"] = "cloud"
        if session.get("title"):
            meta.setdefault("title", session.get("title"))
        data["session_meta"] = meta
        return data

    # Compute session metadata from DB session — included in all response
    # paths so the frontend can correct any stale state on switch.
    session_meta = {}

    # Always include the title so the frontend header stays in sync
    session_title = session.get("title")
    if session_title:
        session_meta["title"] = session_title

    wt_id = session.get("worktree_id")
    wt = await run_sync(database.get_worktree, wt_id) if wt_id else None
    wt_meta = enrich_session_worktree_meta(session, wt)
    if wt_meta:
        session_meta["worktree_id"] = wt_meta["worktree_id"]
        session_meta["worktree_branch"] = wt_meta["worktree_branch"]
        session_meta["worktree_deleted"] = wt_meta["worktree_deleted"]
        session_meta["execution_mode"] = wt_meta["execution_mode"]

    # Check if session is in the live store.
    #
    # A session stays in the live store AFTER its turn ends: end_streaming() keeps
    # it in memory with is_streaming=False as a fast-reconnect snapshot. That
    # snapshot is normally the full transcript, but it can be LESS complete than
    # the authoritative Postgres store when the turn that produced it crashed
    # mid-stream (OOM/SIGKILL) or was seeded with empty history because the session
    # had no sdk_session_id yet. Serving such a stale buffer renders the chat with
    # missing messages — the orphaned-session symptom.
    #
    # Rule: while a turn is ACTIVELY streaming, the in-memory buffer is the source
    # of truth (it holds in-progress deltas the mirror hasn't written yet) and we
    # serve it directly. Once the turn has ENDED, on a fresh load (since_update_id
    # == 0) we fall through to the store loader and serve whichever source is MORE
    # complete (see the completeness check before the response is built). This
    # heals stale/truncated buffers left by a crashed or empty-seeded turn while
    # preserving the brief post-turn window where the buffer leads the not-yet-
    # flushed mirror. Parked delta polls (since_update_id > 0) keep their existing
    # 304/delta behavior — a fresh open always resets to 0, which is where the
    # completeness check runs.
    pending_live_full = None
    if live_session_store.has_session(session_id):
        streaming_now = live_session_store.is_streaming(session_id)
        if since_update_id > 0:
            live_data = live_session_store.get_session_since(session_id, since_update_id)
            if live_data is None:
                # No MESSAGE updates since last check. Tasks live outside the
                # live store's update-id delta, so if any are running we still
                # return a lightweight body carrying the fresh activeTasks rather
                # than a 304 that would hide a task change from the poller.
                if active_tasks:
                    body = {
                        "session_id": session_id,
                        "is_streaming": live_session_store.is_streaming(session_id),
                        "last_update_id": since_update_id,
                        "messages": [],
                        "initial_message_count": since_update_id,
                        "activeTasks": active_tasks,
                    }
                    if session_meta:
                        body["session_meta"] = session_meta
                    return body
                return Response(status_code=304)
            live_data["activeTasks"] = active_tasks
            if session_meta:
                live_data["session_meta"] = session_meta
            return live_data
        elif streaming_now:
            # Fresh load of an actively-streaming session — in-memory is authoritative.
            live_data = live_session_store.get_session_dict(session_id)
            if live_data:
                live_data["activeTasks"] = active_tasks
                if session_meta:
                    live_data["session_meta"] = session_meta
                return live_data
        else:
            # Fresh load of an ENDED session that still has an in-memory snapshot.
            # Hold it as a candidate and fall through to the store loader; the
            # completeness check below serves whichever is more complete.
            pending_live_full = live_session_store.get_session_dict(session_id)

    # Not streaming — load from DB first (fast path), fall back to JSONL
    sdk_session_id = session.get("sdk_session_id")
    messages = []
    working_dir = "/workspace"
    usage_data = None

    from app.core.config import settings as _store_settings
    _postgres_only = _store_settings.session_store_mode == "postgres_only"

    # Fast path: DB message loader (~10-60ms vs 6-20s JSONL parse).
    #
    # SKIPPED under postgres_only: load_messages_from_db reads the legacy
    # `session_messages` table, which in postgres_only only holds the USER rows —
    # assistant/system turns now live exclusively in the session store
    # (session_store_entries, written by the SDK mirror). Reading the legacy table
    # here returned a user-only transcript (the "assistant replies vanish on
    # reopen" bug): has_db was True from the user rows, so this short-circuited
    # before the store-backed parser (Fallback 1) could run. The session-detail
    # read (GET /{id}) was already cut over to the store in PR C.1; this brings the
    # /live read path to the same single source of truth.
    if not _postgres_only:
        from app.core.db_message_loader import load_messages_from_db
        try:
            db_messages, has_db = await run_sync(load_messages_from_db, session_id)
            if has_db:
                messages = db_messages
                logger.debug(f"Live endpoint: loaded {len(messages)} messages from DB for {session_id}")
        except Exception as e:
            logger.warning(f"DB message load failed for live {session_id}: {e}")

    # Fallback 1: JSONL parsing (legacy sessions without DB messages).
    # In postgres_only this loads from the session store instead of JSONL.
    if not messages and sdk_session_id:
        try:
            from app.core.jsonl_parser import parse_session_history
            from app.core.message_resolver import resolve_working_dir
            from app.core.config import settings

            # Worktree-aware working dir (worktree → project → default).
            working_dir = await run_sync(resolve_working_dir, dict(session))

            if _postgres_only:
                # postgres_only: replace the JSONL fallback with the store-backed
                # parser (SAME transform). The DB fast path above is unchanged.
                jsonl_messages, usage_data = await run_sync(
                    parse_session_history_via_store, sdk_session_id, working_dir
                )
            else:
                jsonl_messages, usage_data = await run_sync(parse_session_history, sdk_session_id, working_dir)
            if jsonl_messages:
                # Optional fields to include when present (system, tool, subagent, metadata)
                _OPTIONAL_LIVE_FIELDS = (
                    "subtype", "systemSubtype", "systemData",
                    "toolName", "toolId", "toolInput", "toolResult", "toolStatus",
                    "toolScreenshot",  # inline screenshot (Chrome/computer-use, Read of image)
                    "agentId", "agentType", "agentDescription", "agentStatus", "agentChildren",
                    "metadata",
                )

                # Transform to live format
                for m in jsonl_messages:
                    msg = {
                        "id": m.get("id", ""),
                        "role": m.get("role", "user"),
                        "type": m.get("type", "text"),
                        "content": m.get("content", ""),
                        "streaming": False,
                    }
                    for field in _OPTIONAL_LIVE_FIELDS:
                        value = m.get(field)
                        if value is not None:
                            msg[field] = value

                    messages.append(msg)

        except Exception as e:
            logger.error(f"Failed to parse JSONL for live session {session_id}: {e}")

    # Fallback 2: SDK get_session_messages() — JSONL disk reader. HARD-CUT in
    # postgres_only (the store is the single source of truth; the DB fast path
    # + store-backed parser above already cover it). Runs in mirror/disabled.
    if not _postgres_only and not messages and sdk_session_id and sdk_get_session_messages:
        try:
            sdk_msgs = sdk_get_session_messages(
                session_id=sdk_session_id,
                directory=working_dir,
            )
            if sdk_msgs:
                messages = [
                    {**m, "streaming": False}
                    for m in _transform_sdk_messages(sdk_msgs)
                ]
                logger.debug(
                    f"Live endpoint: loaded {len(messages)} messages via SDK "
                    f"for session {session_id}"
                )
        except Exception as e:
            logger.warning(f"SDK get_session_messages fallback failed for live {session_id}: {e}")

    # Heal stale/truncated live-store snapshots. If a kept (non-streaming) buffer
    # exists and is MORE complete than the store transcript we just loaded, prefer
    # it — this covers the brief post-turn window before the Postgres mirror has
    # flushed the final messages. Otherwise the store wins, which is what recovers
    # a session whose in-memory buffer was left truncated/empty by a mid-stream
    # crash (OOM/SIGKILL) or an empty history seed. Without this, that stale buffer
    # shadows the full transcript and the chat renders with missing messages.
    if pending_live_full is not None:
        live_msgs = pending_live_full.get("messages") or []
        if len(live_msgs) > len(messages):
            logger.debug(
                f"Live endpoint: serving in-memory snapshot for {session_id} "
                f"({len(live_msgs)} msgs > store {len(messages)} msgs)"
            )
            pending_live_full["activeTasks"] = active_tasks
            if session_meta:
                pending_live_full["session_meta"] = session_meta
            return pending_live_full
        logger.debug(
            f"Live endpoint: store transcript for {session_id} is complete "
            f"({len(messages)} msgs >= snapshot {len(live_msgs)} msgs) — "
            f"healed any stale buffer"
        )

    # Attach cost/duration metadata to final assistant messages (cached, O(N))
    # This ensures the poller gets cost data on the first poll after streaming ends
    from app.core.cost_utils import attach_cost_metadata
    attach_cost_metadata(messages, session_id)

    # Build response in live format
    # IMPORTANT: For context window calculation, the frontend needs per-turn
    # input tokens (last API call), NOT cumulative total_tokens_in across all turns.
    # During streaming, the live store provides per-turn values from update_usage().
    # For non-streaming (historical) sessions, use last_input_tokens from JSONL
    # which is the last turn's input_tokens (= context window non-cached portion).
    output_tokens = session.get("total_tokens_out", 0)
    cache_creation = session.get("cache_creation_tokens", 0) or 0
    cache_read = session.get("cache_read_tokens", 0) or 0
    # Per-turn input (last turn), persisted on sessions row by _persist_turn_results
    input_tokens = session.get("last_input_tokens", 0) or 0

    # Override with JSONL data if available (legacy sessions without
    # persisted per-turn columns — usage_data only populated when JSONL fallback ran)
    if sdk_session_id and usage_data:
        # Use last_input_tokens (per-turn) for context window calculation,
        # NOT total_tokens_in (cumulative sum across all turns)
        input_tokens = usage_data.get("last_input_tokens", 0)
        output_tokens = usage_data.get("total_tokens_out", output_tokens)
        cache_creation = usage_data.get("cache_creation_tokens", cache_creation)
        cache_read = usage_data.get("cache_read_tokens", cache_read)

    result = {
        "session_id": session_id,
        "is_streaming": False,
        "last_update_id": 0,
        "messages": messages,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_tokens": cache_creation,
            "cache_read_tokens": cache_read,
        },
        "activeTasks": active_tasks,
    }
    if session_meta:
        result["session_meta"] = session_meta

    return result


@router.patch("/{session_id}")
async def update_session(
    request: Request,
    session_id: str,
    title: Optional[str] = None,
    session_status: Optional[str] = Query(None, alias="status"),
    token: str = Depends(require_auth)
):
    """Update session title or status. API users can only modify their accessible sessions."""
    existing = await require_session(request, session_id)

    # Sync title rename to Claude SDK session if available
    sdk_sid = existing.get("sdk_session_id")
    if title and sdk_sid and sdk_rename_session:
        try:
            sdk_rename_session(session_id=sdk_sid, title=title)
            logger.debug(f"Renamed SDK session {sdk_sid} to '{title}'")
        except Exception as e:
            logger.warning(f"Failed to rename SDK session {sdk_sid}: {e}")

    session = await run_sync(
        database.update_session,
        session_id=session_id,
        title=title,
        status=session_status
    )

    return session


@router.post("/{session_id}/sdk-tag")
async def tag_sdk_session(
    request: Request,
    session_id: str,
    tag: Optional[str] = Query(None, description="Tag to set (null to clear)"),
    token: str = Depends(require_auth)
):
    """Tag a session in the Claude SDK. Requires the session to have an SDK session ID."""
    if not sdk_tag_session:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="SDK tag_session not available (requires SDK 0.1.49+)"
        )

    existing = await require_session(request, session_id)

    sdk_sid = existing.get("sdk_session_id")
    if not sdk_sid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Session has no SDK session ID"
        )

    async with api_error_handler("tag session"):
        sdk_tag_session(session_id=sdk_sid, tag=tag)
        return {"status": "ok", "session_id": session_id, "tag": tag}


@router.get("/{session_id}/sdk-info")
async def get_sdk_session_info(
    request: Request,
    session_id: str,
    token: str = Depends(require_auth)
):
    """Get session metadata from the Claude SDK (tag, created_at, git_branch, etc.)."""
    if not sdk_get_session_info:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="SDK get_session_info not available (requires SDK 0.1.50+)"
        )

    existing = await require_session(request, session_id)

    sdk_sid = existing.get("sdk_session_id")
    if not sdk_sid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Session has no SDK session ID"
        )

    async with api_error_handler("get session info"):
        info = sdk_get_session_info(session_id=sdk_sid)
        if not info:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"SDK session not found: {sdk_sid}"
            )
        return {
            "sdk_session_id": info.session_id,
            "summary": info.summary,
            "custom_title": info.custom_title,
            "first_prompt": info.first_prompt,
            "git_branch": info.git_branch,
            "cwd": info.cwd,
            "tag": info.tag,
            "created_at": info.created_at,
            "last_modified": info.last_modified,
            "file_size": info.file_size,
        }


@router.post("/{session_id}/tasks/{task_id}/stop")
async def stop_session_background_task(
    request: Request,
    session_id: str,
    task_id: str,
    token: str = Depends(require_auth),
):
    """Stop ONE in-flight background task (sub-agent or Monitor shell) by id.

    Powers the "x" on the live background-tasks strip. Shell tasks are
    terminated directly; background sub-agents are stopped via the SDK, which
    then reports the terminal status that drops the task from the next /live
    poll. Returns ``{"ok": bool, ...}`` — a failed stop is a 200 with ok=False
    (e.g. the task already finished) rather than an error.
    """
    await require_session(request, session_id)

    from app.core.query_engine import stop_session_task

    async with api_error_handler("stop background task"):
        result = await stop_session_task(session_id, task_id)
        return {"session_id": session_id, "task_id": task_id, **result}


@router.get("/{session_id}/subagents")
async def list_session_subagents(
    request: Request,
    session_id: str,
    token: str = Depends(require_auth)
):
    """List subagent IDs spawned during a session (SDK 0.1.60+)."""
    if not sdk_list_subagents:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="SDK list_subagents not available (requires SDK 0.1.60+)"
        )

    existing = await require_session(request, session_id)

    sdk_sid = existing.get("sdk_session_id")
    if not sdk_sid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Session has no SDK session ID"
        )

    async with api_error_handler("list session subagents"):
        project = await run_sync(database.get_project, existing.get("project_id")) if existing.get("project_id") else None
        working_dir = get_project_working_dir(project) if project else None
        agent_ids = await run_sync(sdk_list_subagents, session_id=sdk_sid, directory=working_dir)
        return {"session_id": session_id, "subagents": agent_ids}


@router.get("/{session_id}/subagents/{agent_id}/messages")
async def get_session_subagent_messages(
    request: Request,
    session_id: str,
    agent_id: str,
    limit: Optional[int] = None,
    offset: int = 0,
    token: str = Depends(require_auth)
):
    """Read a subagent's transcript messages in chronological order (SDK 0.1.60+)."""
    if not sdk_get_subagent_messages:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="SDK get_subagent_messages not available (requires SDK 0.1.60+)"
        )

    existing = await require_session(request, session_id)

    sdk_sid = existing.get("sdk_session_id")
    if not sdk_sid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Session has no SDK session ID"
        )

    async with api_error_handler("get subagent messages"):
        project = await run_sync(database.get_project, existing.get("project_id")) if existing.get("project_id") else None
        working_dir = get_project_working_dir(project) if project else None
        messages = await run_sync(
            sdk_get_subagent_messages,
            session_id=sdk_sid,
            agent_id=agent_id,
            directory=working_dir,
            limit=limit,
            offset=offset,
        )
        return {
            "session_id": session_id,
            "agent_id": agent_id,
            "messages": [
                {
                    "type": m.type,
                    "uuid": m.uuid,
                    "session_id": m.session_id,
                    "message": m.message,
                    "parent_tool_use_id": m.parent_tool_use_id,
                }
                for m in messages
            ],
        }


async def _cleanup_session_artifacts(session_id: str, sdk_sid: Optional[str], project_id: Optional[str]):
    """Background cleanup: terminate pooled subprocess + delete SDK JSONL.

    Runs after the HTTP response returns so clients don't block on subprocess
    teardown (which can hold worktree CWD locks and take time to exit).
    """
    try:
        from app.core.session_pool import get_session_pool
        await get_session_pool().remove(session_id)
    except Exception as e:
        logger.warning(f"Background session_pool.remove failed for {session_id}: {e}")

    if sdk_sid and sdk_delete_session:
        try:
            project = await run_sync(database.get_project, project_id) if project_id else None
            working_dir = get_project_working_dir(project) if project else None
            await run_sync(sdk_delete_session, session_id=sdk_sid, directory=working_dir)
            logger.debug(f"Deleted SDK session JSONL for {sdk_sid}")
        except Exception as e:
            # Non-fatal — DB record is the primary source of truth
            logger.warning(f"Failed to delete SDK session JSONL for {sdk_sid}: {e}")


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    request: Request,
    session_id: str,
    background_tasks: BackgroundTasks,
    token: str = Depends(require_auth),
):
    """Delete a session and its messages. API users can only delete their accessible sessions.

    DB row is deleted synchronously so the client sees the session gone immediately.
    Subprocess teardown + SDK JSONL cleanup run as background tasks to avoid blocking
    the HTTP response on slow worktree/subprocess lock release.
    """
    existing = await require_session(request, session_id)

    # Delete DB row first — cheap and authoritative for the UI.
    await run_sync(database.delete_session, session_id)

    # Defer slow cleanup (subprocess termination, JSONL file I/O) to background.
    background_tasks.add_task(
        _cleanup_session_artifacts,
        session_id,
        existing.get("sdk_session_id"),
        existing.get("project_id"),
    )


@router.post("/batch-delete", status_code=status.HTTP_200_OK)
async def batch_delete_sessions(
    request: Request,
    body: BatchDeleteRequest,
    background_tasks: BackgroundTasks,
    token: str = Depends(require_auth)
):
    """
    Delete multiple sessions at once.
    API users can only delete sessions they have access to.
    Returns count of successfully deleted sessions.

    DB rows deleted synchronously; subprocess teardown + SDK JSONL cleanup run
    as background tasks to avoid blocking the response.
    """
    deleted_count = 0
    errors = []

    for session_id in body.session_ids:
        try:
            existing = await require_session(request, session_id)
            await run_sync(database.delete_session, session_id)
            background_tasks.add_task(
                _cleanup_session_artifacts,
                session_id,
                existing.get("sdk_session_id"),
                existing.get("project_id"),
            )
            deleted_count += 1
        except HTTPException as e:
            errors.append(f"Access denied for session {session_id}: {e.detail}")
        except Exception as e:
            errors.append(f"Error deleting session {session_id}: {str(e)}")

    return {
        "deleted_count": deleted_count,
        "total_requested": len(body.session_ids),
        "errors": errors if errors else None
    }


@router.post("/{session_id}/archive")
async def archive_session(request: Request, session_id: str, token: str = Depends(require_auth)):
    """Archive a session. API users can only archive their accessible sessions."""
    existing = await require_session(request, session_id)

    # Update session - result not needed for response
    await run_sync(
        database.update_session,
        session_id=session_id,
        status="archived"
    )

    return {"status": "ok", "message": "Session archived"}


@router.patch("/{session_id}/favorite", response_model=Session)
async def toggle_session_favorite(request: Request, session_id: str, token: str = Depends(require_auth)):
    """Toggle the favorite status of a session. Returns the updated session."""
    existing = await require_session(request, session_id)

    session = await run_sync(database.toggle_session_favorite, session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to toggle favorite status"
        )

    return session


# ============================================================================
# Sync endpoints for cross-device synchronization (polling fallback)
# ============================================================================

@router.get("/{session_id}/sync")
async def get_sync_changes(
    request: Request,
    session_id: str,
    since_id: int = Query(0, description="Get changes after this sync ID"),
    token: str = Depends(require_auth)
):
    """
    Get sync changes for a session since a specific sync ID.
    Used as a polling fallback when WebSocket is unavailable.

    Returns:
        - changes: List of sync events since since_id
        - latest_id: The most recent sync ID (use for next poll)
        - is_streaming: Whether the session is currently streaming
    """
    existing = await require_session(request, session_id)

    changes = await run_sync(database.get_sync_logs, session_id, since_id=since_id)
    latest_id = await run_sync(database.get_latest_sync_id, session_id)

    # Check if streaming (import here to avoid circular import)
    from app.core.sync_engine import sync_engine
    is_streaming = sync_engine.is_session_streaming(session_id)

    return {
        "changes": changes,
        "latest_id": latest_id,
        "is_streaming": is_streaming,
        "connected_devices": sync_engine.get_device_count(session_id)
    }


@router.get("/{session_id}/state")
async def get_session_state(
    request: Request,
    session_id: str,
    token: str = Depends(require_auth)
):
    """
    Get current session state for client reconnection.

    This endpoint is used by clients to check the session state when reconnecting,
    including whether work is in progress and if there are buffered streaming messages.

    Returns:
        - session_id: The session ID
        - title: Session title
        - status: Session status (active, archived, etc.)
        - is_streaming: Whether work is currently in progress
        - connected_devices: Number of devices watching this session
        - streaming_messages: Buffered messages if streaming (for late-joiners)
    """
    session = await require_session(request, session_id)

    # Get current streaming state from SyncEngine
    from app.core.sync_engine import sync_engine
    sync_state = await sync_engine.get_session_state(session_id)

    # Combine session metadata with streaming state
    return {
        "session_id": session_id,
        "title": session.get("title"),
        "status": session.get("status"),
        "is_streaming": sync_state["is_streaming"],
        "connected_devices": sync_state["connected_devices"],
        "streaming_messages": sync_state.get("streaming_messages", [])
    }
