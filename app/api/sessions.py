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
    _annotate_cluster_fields,
    _apply_session_filters,
    BatchDeleteRequest,
    SessionImportData,
    SessionImportResponse,
    ImportJSONLRequest,
    ImportJSONLResponse,
    ForkSessionRequest,
    ForkSessionResponse,
    AdoptSessionResponse,
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
# Cross-device adopt ("Resume here")
# ============================================================================

def _adopt_device_name(instance_id: Optional[str]) -> Optional[str]:
    """Display name of the install owning a row, via the install registry.

    Mirrors the streaming-guard 409 lookup below. Returns ``None`` when there is
    no owner or no registered install, so callers fall back to a generic phrase.
    """
    if not instance_id:
        return None
    try:
        install = database.get_install(instance_id)
    except Exception:  # noqa: BLE001 — registry trouble must not break adopt
        return None
    return (install or {}).get("device_name")


def _adopt_preflight_sync(session: dict, project_id: str) -> tuple[Optional[str], Optional[str]]:
    """Cross-device rebind preflight for adopt (runs in a worker thread).

    Returns ``(warning, local_project_id)``. Raises ``HTTPException`` 409 — the
    ONE deliberate blocker — when the session's repository has no clone on this
    device. All other trouble is left to the caller's fail-open wrapper.
    """
    from app.core.project_links import resolve_runnable_local_project
    from app.core.git_service import git_service
    from app.core.config import settings

    stored, local = resolve_runnable_local_project(project_id)
    if local is None:
        name = (stored or {}).get("name") or "unknown"
        path = get_project_working_dir(stored) if stored else "another device"
        stored_instance = (stored or {}).get("instance_id")
        if stored_instance and stored_instance == settings.effective_instance_id:
            # The project row belongs to THIS install but its folder is gone —
            # naming "another device" would be misleading and unactionable.
            detail = (
                f"This session's project '{name}' points at {path}, which no "
                f"longer exists on this device. Restore the project folder or "
                f"re-add the project, then try again."
            )
        else:
            device_name = _adopt_device_name(stored_instance)
            detail = (
                f"This session's project '{name}' isn't available on this device — "
                f"its files live at {path} on {device_name or 'another device'}. "
                f"Clone the repository on this device and add it as a project, then "
                f"try again."
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=detail,
        )

    warning: Optional[str] = None
    # Best-effort push check: only meaningful when we resolved onto a DIFFERENT
    # local clone (a sibling) for a worktree-flagged session.
    try:
        resolved_via_sibling = local["id"] != project_id
        is_worktree_session = bool(session.get("worktree_branch") or session.get("worktree_id"))
        branch = session.get("worktree_branch")
        if resolved_via_sibling and is_worktree_session and branch:
            local_dir = get_project_working_dir(local)
            # Freshen origin refs before deciding "unpushed". Without a fetch, a
            # branch that WAS pushed from the other device but hasn't been fetched
            # into this clone yet looks absent and yields a FALSE "hasn't been
            # pushed" warning (PR #493 follow-up #4). This runs at adopt time (off
            # the send hot path) and primes the rebind fetch cache so the first
            # send doesn't fetch again (follow-up #1). A fetch failure here is
            # non-fatal — fall through to the offline listing rather than block.
            from app.core.worktree_manager import fetch_repo_cached
            try:
                fetch_repo_cached(git_service, local_dir, force=True)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "Adopt preflight fetch failed for %s; using offline refs",
                    local_dir, exc_info=True,
                )
            # One listing covers both checks: include_remote=True returns local
            # branches by their bare name AND remote-tracking ones as "origin/<b>",
            # so local presence is `branch in all_names` and origin presence is
            # `origin/{branch} in all_names`.
            all_names = {b["name"] for b in git_service.list_branches(local_dir, include_remote=True)}
            if branch not in all_names and f"origin/{branch}" not in all_names:
                device_name = _adopt_device_name((stored or {}).get("instance_id"))
                warning = (
                    f"Branch '{branch}' hasn't been pushed to origin — resuming here "
                    f"will fail until you push it from "
                    f"{device_name or 'the original device'} "
                    f"(git push -u origin {branch})."
                )
    except Exception:  # noqa: BLE001 — a warning-check bug must never block adopt
        logger.warning("Adopt preflight branch-check failed for project %s", project_id, exc_info=True)

    return warning, local["id"]


@router.post("/{session_id}/adopt", response_model=AdoptSessionResponse)
async def adopt_session_endpoint(
    request: Request,
    session_id: str,
    token: str = Depends(require_auth),
):
    """Take over a session created by another install so it can resume locally.

    On a shared database every install's sessions are visible, but a session is
    "owned" by the install whose ``instance_id`` is on the row — the send /
    interrupt path resolves the working dir and worktree against the local
    machine, so a foreign session can't actually be driven from here until it is
    adopted. This re-points ``instance_id`` to this install.

    Semantics:
      * Already local (this install, or a legacy NULL owner) → no-op, returns
        ``adopted=False`` so the caller just opens it.
      * Owned elsewhere and **actively streaming** → refuse with 409: two
        installs driving one live SDK session at once corrupts the transcript.
        The user should stop it on that device first. Per-session streaming
        state is fanned out across the cluster (``apply_remote_streaming_change``)
        and stale remote entries are swept when the owner goes offline, so
        ``is_session_streaming`` is authoritative even for a remote-owned
        session — and, unlike the install-level "online" heartbeat, it does not
        block adoption of an idle session just because the owning install
        happens to run 24/7.
      * Owned elsewhere but idle (not streaming) → adopt it.

    Worktree sessions keep working after adoption: only the owner column changes,
    so the existing ``recreate_worktree`` adopt-on-disk path rebinds the worktree
    on the next resume (``sdk_session_id`` is preserved, so history survives).
    """
    from app.core.config import settings

    session = await require_session(request, session_id)

    me = settings.effective_instance_id
    owner = session.get("instance_id")

    # Already ours (or legacy NULL) — nothing to adopt.
    if not owner or owner == me:
        return AdoptSessionResponse(
            session_id=session_id,
            instance_id=me,
            adopted=False,
            status="already_local",
        )

    # Owned by another install: refuse only while the session is actually live
    # (streaming) so we never wrestle an in-flight SDK turn away mid-stream. The
    # install-level "online" heartbeat is the wrong granularity — a 24/7 install
    # would make all of its idle sessions permanently unadoptable.
    from app.core.sync_engine import sync_engine

    if sync_engine.is_session_streaming(session_id):
        device = await run_sync(_adopt_device_name, owner) or "another device"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This session is currently running on {device}. "
                "Stop it there before resuming it here."
            ),
        )

    # Cross-device rebind preflight: refuse (409) when the session's repository
    # isn't cloned on this device — otherwise the first send would crash on git
    # against a foreign path. Everything else fails open: a preflight bug must
    # never make an adoptable session impossible to adopt.
    warning: Optional[str] = None
    local_project_id: Optional[str] = None
    project_id = session.get("project_id")
    if project_id:
        try:
            warning, local_project_id = await run_sync(
                _adopt_preflight_sync, session, project_id
            )
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001 — fail open on any non-409 preflight error
            logger.warning(
                "Adopt preflight errored for session %s; proceeding fail-open",
                session_id,
                exc_info=True,
            )

    updated = await run_sync(database.adopt_session, session_id, me)
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}",
        )

    # Persist the cross-device rebind onto this install's local clone right after
    # the adopt write succeeds, so search / commands / fork resolve the LOCAL path
    # immediately — closing the adopt→first-send window where they would otherwise
    # still see the foreign project_id (and fork would copy it). Wrapped fail-open:
    # adoption already succeeded and must not be undone by a persist hiccup; the
    # send-path lazy rebind still heals sessions adopted before this shipped.
    if local_project_id and local_project_id != project_id:
        try:
            await run_sync(database.set_session_project, session_id, local_project_id)
            logger.info(
                "Rebound adopted session %s onto local project %s",
                session_id, local_project_id,
            )
        except Exception:  # noqa: BLE001 — persist failure must not fail the adopt
            logger.warning(
                "Failed to persist rebind of session %s to local project %s; "
                "adoption still succeeded",
                session_id, local_project_id, exc_info=True,
            )

    logger.info("Adopted session %s from install %s -> %s", session_id, owner, me)
    return AdoptSessionResponse(
        session_id=session_id,
        instance_id=me,
        adopted=True,
        status="adopted",
        warning=warning,
        local_project_id=local_project_id,
    )


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
    store_parse_failed = False

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
                # Per-message profile resolution. JSONL entries carry no DB row id,
                # so each user message's profile is read from the DB user rows by
                # matching content and consuming the match in order (cached to avoid
                # re-loading all DB messages). Popping left-to-right assigns the
                # correct profile even when the profile changed mid-session and the
                # same prompt text repeats. (This must reach the response: the
                # SessionMessage model now carries profile_id/profileId, which the
                # frontend reads to label each bubble.)
                from app.core.cost_utils import build_profile_queues
                profile_queues = build_profile_queues(session_id)

                # Transform to expected format for SessionWithMessages
                # Use camelCase for frontend compatibility (toolName, toolInput, toolId)
                current_turn_profile = None
                for i, m in enumerate(jsonl_messages):
                    # Resolve this message's profile. A user row reads its OWN
                    # profile from the ordered per-content queue built from the DB
                    # user rows; that profile then carries to the turn's
                    # assistant/thinking rows (which the JSONL transcript stores
                    # without a profile) so each bubble keeps the profile that
                    # produced it instead of falling back to the session's CURRENT
                    # profile when the user switches selection.
                    if m.get("role") == "user":
                        queue = profile_queues.get(m.get("content", ""))
                        current_turn_profile = queue.popleft() if queue else None
                    msg_profile_id = current_turn_profile
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
                        "profile_id": msg_profile_id,
                        "profileId": msg_profile_id,
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
            # A RAISED store parse (transient DB error) is not an EMPTY store:
            # the legacy fallback below must not fire, or a post-cutover
            # session would render as the user-rows-only legacy view.
            store_parse_failed = _postgres_only

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
        # postgres_only: the store stays the single source of truth and the SDK
        # JSONL disk reader (Fallback 2) stays hard-cut. But an EMPTY result —
        # after the lazy JSONL backfill inside parse_session_history_via_store
        # had its chance — means the transcript predates the cutover and lives
        # ONLY in the legacy session_messages table (shared across installs;
        # e.g. the JSONL sits on another machine). Serve that instead of
        # rendering an existing chat as empty. Post-cutover sessions always
        # have store rows, so this can never shadow the store with the
        # user-rows-only legacy view (the "vanishing replies" trap) — EXCEPT
        # when the store parse RAISED: then "no messages" proves nothing about
        # the store, so serve empty (a transient blank beats a wrong
        # transcript) rather than fall through to the legacy view.
        if not messages and not store_parse_failed:
            if sdk_session_id:
                logger.warning(
                    "[Session API] postgres_only: no entries in session_store for "
                    "session %s (sdk_session_id=%s) — serving legacy "
                    "session_messages transcript.",
                    session_id, sdk_session_id,
                )
            messages = await _legacy_db_messages(session_id)
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
            messages = await _legacy_db_messages(session_id)

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

    # Annotate owning device + controllability so the chat view can gate live
    # send/interrupt when the session belongs to another (possibly offline) install.
    await run_sync(_annotate_cluster_fields, [session])

    logger.debug(f"Returning session {session_id} with {len(messages)} messages")
    return session


async def _legacy_db_messages(session_id: str) -> list:
    """Legacy ``session_messages`` rows transformed for the detail response.

    The least-rich source (Fallback 3): type is inferred from the role/tool
    columns. Used in mirror/disabled when JSONL yields nothing, and in
    postgres_only as the last-resort read for pre-cutover sessions whose
    transcript was never backfilled into the store.
    """
    db_messages = await run_sync(database.get_session_messages, session_id)
    messages = []
    for m in db_messages:
        msg = dict(m)
        # Infer type from role for legacy DB messages
        if msg.get("tool_name"):
            msg["type"] = "tool_use"
            msg["toolName"] = msg.get("tool_name")
            msg["toolInput"] = msg.get("tool_input")
        elif msg.get("role") == "assistant":
            msg["type"] = "text"
        # Each session_messages row already carries its own profile_id column;
        # surface the camelCase alias the frontend reads (the snake_case key
        # alone was silently dropped before the model exposed profile_id).
        msg["profileId"] = msg.get("profile_id")
        # Surface each row's own created_at as metadata.timestamp so the UI
        # renders each message's ORIGINAL send time on reload instead of
        # falling back to "now" (which made every message show the same
        # wall-clock time). Mirror db_message_loader: normalize datetime →
        # ISO string so the value stays JSON-serializable and matches the
        # /live path, and merge into any existing metadata rather than clobber.
        created_at = msg.get("created_at")
        if hasattr(created_at, "isoformat"):
            created_at = created_at.isoformat()
        if created_at:
            existing_meta = msg.get("metadata")
            existing_meta = dict(existing_meta) if isinstance(existing_meta, dict) else {}
            existing_meta.setdefault("timestamp", created_at)
            msg["metadata"] = existing_meta
        messages.append(msg)
    return messages


def _describe_relay_error(err: Exception) -> str:
    """Map a live-relay failure to a short, human-readable, secret-free reason.

    Used to stamp ``session_meta.relay_error`` when the owning install is
    registry-online but the relay itself fails, so the UI can explain WHY control
    is unavailable. Never includes URLs, secrets, or raw peer bodies — just a
    coarse category derived from the error's ``status_code`` when present.
    (InstallOffline is handled by the caller; this maps the peer-error and
    transport-failure CloudRelayError cases.)
    """
    status = getattr(err, "status_code", None)
    if status in (401, 403):
        return "authentication rejected by device"
    if status is not None:
        return f"device returned an error ({status})"
    # No HTTP status → transport-level failure (timeout / connection refused) or a
    # non-JSON body from a proxy sitting in front of the peer.
    return "device is unreachable"


def _overlay_subagent_progress(
    store_messages: list, buffer_messages: list
) -> None:
    """Graft fresher sub-agent CARD fields from the in-memory buffer onto the
    store-loaded transcript, matched by ``toolId``.

    Used on the ``/live`` fresh-load path where the STORE transcript wins the
    completeness heal (the in-memory buffer is otherwise discarded). Sub-agent
    progress mutates a card IN PLACE — ``agentStatus``/``agentChildren``/
    ``agentUsage`` advance without changing the message count — so for those
    cards the buffer is strictly fresher than the store snapshot. This mutates
    ``store_messages`` fields ONLY; it never adds/removes messages, so the heal's
    count/tie-break semantics are untouched. ``content`` is grafted only when the
    buffer's is non-empty (never blank out a persisted body).
    """
    if not buffer_messages or not store_messages:
        return
    buffer_by_tool = {}
    for bm in buffer_messages:
        if not isinstance(bm, dict) or bm.get("type") != "subagent":
            continue
        tid = bm.get("toolId")
        if tid:
            buffer_by_tool[tid] = bm
    if not buffer_by_tool:
        return
    for sm in store_messages:
        if not isinstance(sm, dict) or sm.get("type") != "subagent":
            continue
        bm = buffer_by_tool.get(sm.get("toolId"))
        if bm is None:
            continue
        # to_dict()/db loader emit these keys only when present; graft on
        # presence so a store value is preserved when the buffer lacks one.
        for field in ("agentStatus", "agentChildren", "agentUsage"):
            if field in bm:
                sm[field] = bm[field]
        buf_content = bm.get("content")
        if buf_content:
            sm["content"] = buf_content


def _reconcile_subagent_status(store_messages: list, active_tasks: list) -> None:
    """Force ``agentStatus = "running"`` on store-loaded sub-agent cards whose
    ``toolId`` matches a still-running registry task.

    Covers the buffer-gone-but-tasks-alive case: on a fresh load the in-memory
    buffer may already be gone (or never existed) while a background sub-agent is
    still in flight, so a store/legacy transcript would default the card to
    "completed" and freeze it. ``active_tasks`` is the ``get_visible`` wire view;
    a task's ``toolUseId`` equals the launch tool_use id, which is the sub-agent
    card's ``toolId``. Only matching cards are touched — a legacy transcript with
    no live task stays "completed"/"pending" exactly as loaded.
    """
    running_tool_ids = {
        t.get("toolUseId")
        for t in (active_tasks or [])
        if isinstance(t, dict) and t.get("status") == "running" and t.get("toolUseId")
    }
    if not running_tool_ids:
        return
    for m in store_messages:
        if (
            isinstance(m, dict)
            and m.get("type") == "subagent"
            and m.get("toolId") in running_tool_ids
        ):
            m["agentStatus"] = "running"


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

    # Active background tasks (SDK agent tasks + Monitor shell subprocesses) PLUS
    # any recently-failed task still inside the retention window. ``get_visible``
    # (not ``get_active``) is the DISPLAY view: it surfaces a task that died —
    # e.g. reaped to "killed" when its pooled SDK client was destroyed — for a
    # short window so the taskbar shows the failure instead of the row silently
    # vanishing. Running-only control-flow predicates still use ``get_active``.
    # Attached fresh to EVERY local response path (delta or full, streaming or
    # not) — never routed through the live store's last_update_id delta logic, so
    # a task finishing/failing is reflected on the very next poll. [] when none.
    active_tasks = task_registry.get_visible(session_id)

    # "Unpark pending": a background sub-agent completed (so it has already dropped
    # out of ``active_tasks``) and the parent's unpark follow-up turn is committed
    # but has NOT yet flipped ``is_streaming`` true. Surfaced on every response body
    # so a polling client keeps its park-poll alive across that gap instead of
    # seeing a transient (is_streaming=false, activeTasks=[]) and STOPPING — which
    # stranded the parked UI until a full reload. Clears the instant the follow-up
    # streams. See task_registry / the session poller.
    unpark_pending = task_registry.is_unpark_pending(session_id)

    # Keep-polling ALSO stays up while undrained queued messages await the drain
    # pump: a follow-up turn is imminent even though nothing is streaming this
    # instant. Without this, a switch-back that lands in the micro-gap BETWEEN
    # queue-driven follow-up turns reads a truthful (is_streaming=false,
    # activeTasks=[], unpark_pending=false) and the client concludes permanent
    # idle → stops polling and the UI freezes until a manual refresh. Reuse ``unpark_pending`` as the keep-alive
    # channel so no client contract change is needed; scope it narrowly to the
    # exact "a queue row still exists" condition. Does NOT touch the drain pump
    # or task_registry semantics.
    if not unpark_pending:
        try:
            if await run_sync(database.count_queued_messages, session_id) > 0:
                unpark_pending = True
        except Exception:
            logger.debug(
                "count_queued_messages failed for %s", session_id, exc_info=True
            )

    # Effective "a plain Stop spares background sub-agents" state. Only true when
    # the operator opted in AND the deploy-time CLI binary patch actually landed
    # (the flag alone is intent, not effect). Computed ONCE and attached to every
    # local response body so the client can label the Stop control accordingly.
    from app.core.config import settings as _pb_settings
    from app.core.cli_interrupt_patch import is_patch_active
    preserve_background_on_interrupt = bool(
        _pb_settings.preserve_background_on_interrupt and is_patch_active()
    )

    # Cross-install session: owned by another install on the shared DB. History
    # is already in the shared DB, so there is ALWAYS something to show. When the
    # owning install is reachable we relay its live stream (fully controllable);
    # when it is offline/view-only we fall through to serve the persisted history
    # flagged view-only, so the session opens with a "device offline" badge
    # instead of an unopenable error / 503 poll spam.
    from app.core import cloud_relay
    remote_unreachable = False
    relay_error: Optional[str] = None
    if cloud_relay.is_remote_session(session):
        try:
            code, data = await cloud_relay.relay_live(session, since_update_id)
        except cloud_relay.InstallOffline as e:
            # Owner is offline/unregistered/view-only per the registry — a clean,
            # expected "not reachable" state. The message is already user-safe
            # (device name only, no URLs/secrets). Degrade to read-only history.
            logger.debug(
                "Cluster live unavailable for %s: %s — serving shared-DB history",
                session_id, e,
            )
            remote_unreachable = True
            relay_error = str(e)
        except cloud_relay.CloudRelayError as e:
            # Owner is registry-online but the relay itself failed: a peer HTTP
            # error (auth mismatch, 5xx), a non-JSON body, or a connect/read
            # timeout wrapped as a transport error. Degrade to read-only history
            # rather than 503 so the chat stays openable, and surface a short,
            # secret-free reason so the UI can say WHY control is unavailable.
            logger.debug(
                "Cluster live relay failed for %s: %s — serving shared-DB history",
                session_id, e,
            )
            remote_unreachable = True
            relay_error = _describe_relay_error(e)
        except Exception as e:  # noqa: BLE001
            # Belt-and-suspenders: any UNEXPECTED relay failure (bad JSON, DB
            # hiccup reading the registry/secret, etc.) must NOT 500 the load.
            # Cross-install history lives in the shared DB, so we can always fall
            # back to read-only rather than leave the chat unopenable.
            logger.warning(
                "Unexpected cluster live error for %s: %s — serving shared-DB history",
                session_id, e,
            )
            remote_unreachable = True
            relay_error = "unexpected error reaching device"
        else:
            if code == 304 or data is None:
                return Response(status_code=304)
            # Live relay succeeded: the device is online and controllable.
            meta = data.get("session_meta") or {}
            meta["execution_mode"] = "cloud"
            meta["device_online"] = True
            meta["controllable"] = True
            if session.get("title"):
                meta.setdefault("title", session.get("title"))
            # Surface the branch the worker runs on. cloud+worktree sessions carry
            # it in the relayed worktree_branch; for no-worktree cloud, fall back to
            # the branch persisted on the shared-DB row at intake (cloud_branch).
            cloud_branch = session.get("cloud_branch")
            if cloud_branch and not meta.get("worktree_branch"):
                meta["cloud_branch"] = cloud_branch
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

    # No-worktree cloud branch persisted at intake — surfaced so the UI can show
    # which branch the session runs on even when served from shared-DB history
    # (e.g. the owning device is offline). cloud+worktree uses worktree_branch.
    cloud_branch = session.get("cloud_branch")
    if cloud_branch and not session_meta.get("worktree_branch"):
        session_meta["cloud_branch"] = cloud_branch

    # Cross-install session served from local history because the owner is
    # unreachable: flag it view-only so the UI shows a "device offline" badge and
    # disables sending instead of erroring the load.
    if remote_unreachable:
        session_meta["execution_mode"] = "cloud"
        session_meta["device_online"] = False
        session_meta["controllable"] = False
        # Short, secret-free reason WHY the owning device couldn't be reached, so
        # the UI can say "Could not reach <device> — showing read-only history
        # (<reason>)" instead of the ambiguous plain "Device offline — view only".
        if relay_error:
            session_meta["relay_error"] = relay_error

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
                # live store's update-id delta, so if any are running — OR an
                # unpark follow-up is mid-handoff — we still return a lightweight
                # body carrying the fresh activeTasks + unpark_pending rather than a
                # 304 that would hide the state from the poller.
                if active_tasks or unpark_pending:
                    body = {
                        "session_id": session_id,
                        "is_streaming": live_session_store.is_streaming(session_id),
                        "last_update_id": since_update_id,
                        "messages": [],
                        "initial_message_count": since_update_id,
                        "activeTasks": active_tasks,
                        "unpark_pending": unpark_pending,
                        "preserve_background_on_interrupt": preserve_background_on_interrupt,
                    }
                    if session_meta:
                        body["session_meta"] = session_meta
                    return body
                return Response(status_code=304)
            live_data["activeTasks"] = active_tasks
            live_data["unpark_pending"] = unpark_pending
            live_data["preserve_background_on_interrupt"] = preserve_background_on_interrupt
            if session_meta:
                live_data["session_meta"] = session_meta
            return live_data
        elif streaming_now:
            # Fresh load of an actively-streaming session — in-memory is authoritative.
            live_data = live_session_store.get_session_dict(session_id)
            if live_data:
                live_data["activeTasks"] = active_tasks
                live_data["unpark_pending"] = unpark_pending
                live_data["preserve_background_on_interrupt"] = preserve_background_on_interrupt
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
    store_parse_failed = False
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
                    "uuid",  # Raw JSONL entry uuid — lets the UI rewind by uuid directly
                    "subtype", "systemSubtype", "systemData",
                    "toolName", "toolId", "toolInput", "toolResult", "toolStatus",
                    "toolScreenshot",  # inline screenshot (Chrome/computer-use, Read of image)
                    "agentId", "agentType", "agentDescription", "agentPrompt",
                    "agentStatus", "agentChildren", "agentUsage",
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

                # Per-message profile resolution — same ordered-queue merge as
                # GET /{id}: JSONL entries carry no DB row id, so each user
                # message consumes its content's queue built from the DB rows,
                # and that profile carries to the turn's assistant/thinking rows
                # so each bubble keeps the profile that produced it (rather than
                # re-labelling to the session's CURRENT profile on a switch).
                from app.core.cost_utils import attach_turn_profile_ids
                attach_turn_profile_ids(session_id, messages)

        except Exception as e:
            logger.error(f"Failed to parse JSONL for live session {session_id}: {e}")
            # A RAISED store parse is not an EMPTY store — see GET /{id}.
            store_parse_failed = _postgres_only

    # postgres_only legacy rescue: the store yielded nothing even after the
    # lazy JSONL backfill inside parse_session_history_via_store had its
    # chance — a pre-cutover session whose transcript lives ONLY in the legacy
    # session_messages table (shared across installs). Serve that instead of
    # an empty chat. Fires ONLY on an empty store result — NOT on a raised
    # parse (a transient DB error proves nothing about the store; serving the
    # legacy view then would resurface the user-rows-only "vanishing replies"
    # trap). Post-cutover sessions always have store rows, so the legacy view
    # can never shadow the store. Mirrors GET /{id}.
    if _postgres_only and not messages and not store_parse_failed:
        from app.core.db_message_loader import load_messages_from_db
        try:
            db_messages, has_db = await run_sync(load_messages_from_db, session_id)
            if has_db:
                messages = db_messages
                logger.warning(
                    "Live endpoint: postgres_only store empty for %s — serving "
                    "legacy DB transcript (%d messages)",
                    session_id, len(messages),
                )
        except Exception as e:
            logger.warning(f"Legacy DB fallback failed for live {session_id}: {e}")

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
        # On a COUNT TIE while background tasks are still RUNNING (a parked
        # session), the buffer wins: sub-agent progress mutates agentChildren
        # IN PLACE on an existing live message, so the message COUNT never
        # changes while the buffer is strictly fresher than the store
        # transcript. Serving the store here would also echo last_update_id=0,
        # pinning a re-attaching poller to since_update_id=0 fresh loads —
        # locked out of the delta path that carries the progress — which froze
        # the agent cards after a switch-away/switch-back. The snapshot carries
        # the buffer's real last_update_id, putting the client back on deltas.
        # A SHORTER buffer still loses, preserving the crash/empty-seed heal.
        parked_running = bool(task_registry.get_active(session_id))
        if len(live_msgs) > len(messages) or (
            parked_running and live_msgs and len(live_msgs) == len(messages)
        ):
            logger.debug(
                f"Live endpoint: serving in-memory snapshot for {session_id} "
                f"({len(live_msgs)} msgs vs store {len(messages)} msgs, "
                f"parked_running={parked_running})"
            )
            pending_live_full["activeTasks"] = active_tasks
            pending_live_full["unpark_pending"] = unpark_pending
            pending_live_full["preserve_background_on_interrupt"] = preserve_background_on_interrupt
            if session_meta:
                pending_live_full["session_meta"] = session_meta
            return pending_live_full
        # STORE WINS: the buffer is discarded for completeness, but sub-agent
        # cards mutate IN PLACE (progress advances agentChildren/agentStatus/
        # agentUsage without changing counts), so the buffer is strictly fresher
        # for THOSE cards. Graft them by toolId — fields only, never counts — so a
        # parked session's live sub-agent progress isn't frozen to the store's
        # snapshot on a fresh load.
        _overlay_subagent_progress(messages, live_msgs)
        logger.debug(
            f"Live endpoint: store transcript for {session_id} is complete "
            f"({len(messages)} msgs >= snapshot {len(live_msgs)} msgs) — "
            f"healed any stale buffer"
        )

    # Registry reconcile: even with the buffer gone (or never present), a
    # sub-agent still running in the task registry must not render "completed".
    # Force its card back to "running" by matching the launch tool_use id;
    # non-matching cards (legacy transcripts) are left exactly as loaded.
    _reconcile_subagent_status(messages, active_tasks)

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
        "unpark_pending": unpark_pending,
        "preserve_background_on_interrupt": preserve_background_on_interrupt,
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

    if sdk_sid:
        # Purge the Postgres session store: sessions has NO FK into
        # session_store_entries/_summaries, so skipping this leaks the deleted
        # session's full transcript forever (Bug E). session_id-only match —
        # the stored project_key cannot be reconstructed here. Runs in every
        # store mode (cheap no-op when the session has no rows).
        try:
            from app.core.pg_session_store import delete_session_data_by_session_id
            removed = await delete_session_data_by_session_id(sdk_sid)
            if removed:
                logger.debug(f"Deleted {removed} session store rows for {sdk_sid}")
        except Exception as e:
            # Non-fatal — but the transcript rows stay orphaned until a future
            # delete retries, so make the leak visible.
            logger.warning(f"Failed to delete session store rows for {sdk_sid}: {e}")

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
