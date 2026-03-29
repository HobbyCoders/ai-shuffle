"""
Session management API routes
"""

from typing import List, Optional

from fastapi import APIRouter, HTTPException, Depends, Query, status, Request, UploadFile, File
from pydantic import BaseModel

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

from app.core.models import (
    Session,
    SessionWithMessages,
    SessionSearchResult,
    PaginatedSessions,
    PaginatedSessionSearchResults
)
import time
from app.db import database
from app.db.async_database import run_sync
from app.db.tags import get_tags_for_sessions
from app.db.sessions import get_fork_info_for_sessions
from app.api.auth import require_auth, get_api_user_from_request
from app.core.project_utils import get_project_working_dir

import logging
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Throttle for session sync — at most once per 60 s per project
# ---------------------------------------------------------------------------
_SESSION_SYNC_THROTTLE_S = 60.0
_last_sync_times: dict[str, float] = {}  # project_id -> monotonic timestamp


def _transform_sdk_messages(sdk_messages: list) -> list:
    """Transform SDK SessionMessage objects into our frontend-compatible format.

    The SDK's get_session_messages() returns SessionMessage objects with
    type ("user"/"assistant"), uuid, session_id, and message (raw Anthropic API dict).
    This transforms them into the same shape our frontend expects.
    """
    messages = []
    tool_id_to_name: dict[str, str] = {}  # Track tool_use_id → tool_name

    for i, sdk_msg in enumerate(sdk_messages):
        raw = sdk_msg.message  # Raw Anthropic API message dict
        role = raw.get("role", sdk_msg.type)
        content = raw.get("content", "")

        if role == "assistant" and isinstance(content, list):
            # Assistant messages contain content blocks (text, tool_use, thinking, etc.)
            for block in content:
                block_type = block.get("type")

                if block_type == "text":
                    messages.append({
                        "id": f"{sdk_msg.uuid}-text-{len(messages)}",
                        "role": "assistant",
                        "content": block.get("text", ""),
                        "type": "text",
                        "created_at": None,
                    })

                elif block_type == "tool_use":
                    tool_id = block.get("id")
                    tool_name = block.get("name", "unknown")
                    if tool_id:
                        tool_id_to_name[tool_id] = tool_name
                    messages.append({
                        "id": f"{sdk_msg.uuid}-tool-{tool_id}",
                        "role": "assistant",
                        "content": f"Using tool: {tool_name}",
                        "type": "tool_use",
                        "toolName": tool_name,
                        "toolInput": block.get("input"),
                        "toolId": tool_id,
                        "tool_name": tool_name,
                        "tool_input": block.get("input"),
                        "created_at": None,
                    })

                elif block_type == "thinking":
                    messages.append({
                        "id": f"{sdk_msg.uuid}-thinking-{len(messages)}",
                        "role": "assistant",
                        "content": block.get("thinking", ""),
                        "type": "thinking",
                        "created_at": None,
                    })

        elif role == "user":
            if isinstance(content, list):
                # User messages with tool results
                for block in content:
                    block_type = block.get("type")
                    if block_type == "tool_result":
                        tool_use_id = block.get("tool_use_id", "")
                        tool_name = tool_id_to_name.get(tool_use_id, "unknown")
                        result_content = block.get("content", "")
                        if isinstance(result_content, list):
                            # Flatten content blocks to text
                            result_content = "\n".join(
                                b.get("text", "") for b in result_content
                                if b.get("type") == "text"
                            ) or str(result_content)
                        messages.append({
                            "id": f"{sdk_msg.uuid}-result-{tool_use_id}",
                            "role": "tool_result",
                            "content": str(result_content)[:2000],
                            "type": "tool_result",
                            "toolName": tool_name,
                            "toolId": tool_use_id,
                            "tool_name": tool_name,
                            "created_at": None,
                        })
                    elif block_type == "text":
                        messages.append({
                            "id": f"{sdk_msg.uuid}-{len(messages)}",
                            "role": "user",
                            "content": block.get("text", ""),
                            "type": "text",
                            "created_at": None,
                        })
            elif isinstance(content, str):
                messages.append({
                    "id": sdk_msg.uuid,
                    "role": "user",
                    "content": content,
                    "type": "text",
                    "created_at": None,
                })

    return messages

router = APIRouter(prefix="/api/v1/sessions", tags=["Sessions"])


class BatchDeleteRequest(BaseModel):
    """Request body for batch delete operation"""
    session_ids: List[str]


def check_session_access(request: Request, session: dict) -> None:
    """Check if API user has access to a session based on project/profile restrictions."""
    api_user = get_api_user_from_request(request)
    if not api_user:
        return  # Admin has full access

    # Check project restriction
    if api_user.get("project_id") and session.get("project_id"):
        if api_user["project_id"] != session["project_id"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this session"
            )

    # Check profile restriction
    if api_user.get("profile_id"):
        if api_user["profile_id"] != session.get("profile_id"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this session"
            )


def enrich_sessions_with_tags(sessions: List[dict]) -> List[dict]:
    """Add tags array, has_forks field, and api_user_name to each session.

    Uses batch DB queries (2 total) instead of per-session queries to avoid
    O(N) round-trips that slow down with session count.
    """
    if not sessions:
        return sessions

    session_ids = [s["id"] for s in sessions]

    # Batch-lookup API user names
    api_user_ids = {s["api_user_id"] for s in sessions if s.get("api_user_id")}
    api_user_names: dict[str, str] = {}
    for uid in api_user_ids:
        user = database.get_api_user(uid)
        if user:
            api_user_names[uid] = user.get("name") or user.get("username") or uid

    # Batch: tags for all sessions (1 query instead of N)
    tags_by_session = get_tags_for_sessions(session_ids)

    # Batch: fork info for all sessions (2 queries instead of N)
    fork_info_by_session = get_fork_info_for_sessions(session_ids)

    for session in sessions:
        sid = session["id"]
        session["tags"] = tags_by_session.get(sid, [])
        fork_info = fork_info_by_session.get(sid)
        session["has_forks"] = fork_info["has_forks"] if fork_info else False
        if session.get("api_user_id"):
            session["api_user_name"] = api_user_names.get(session["api_user_id"])
    return sessions


def _apply_session_filters(
    request: Request,
    project_id: Optional[str],
    profile_id: Optional[str],
    api_user_id: Optional[str],
    admin_only: bool,
    api_users_only: bool,
    sync_claude_sessions: bool,
) -> tuple:
    """Apply API user restrictions and sync Claude sessions.

    Shared logic for list_sessions and list_sessions_paginated:
    1. Force project/profile/api_user_id from API user restrictions
    2. Auto-sync Claude Code sessions for the project
    3. Build filter_api_user_id and filter_api_users_only from the flags

    Returns (project_id, profile_id, filter_api_user_id, filter_api_users_only).
    """
    api_user = get_api_user_from_request(request)

    # Force API user restrictions
    if api_user:
        if api_user.get("project_id"):
            project_id = api_user["project_id"]
        if api_user.get("profile_id"):
            profile_id = api_user["profile_id"]
        # API users can only see their own sessions
        api_user_id = api_user["id"]
        admin_only = False
        api_users_only = False

    # Auto-sync Claude Code sessions (only for specific project, not global).
    # Throttled to at most once per 60 s per project — disk scan + N DB
    # lookups is too expensive to run on every list request.
    if sync_claude_sessions and project_id:
        now = time.monotonic()
        last = _last_sync_times.get(project_id, 0.0)
        if now - last >= _SESSION_SYNC_THROTTLE_S:
            try:
                from app.core.session_sync import session_sync_service
                _last_sync_times[project_id] = now
                sync_result = session_sync_service.sync_project_sessions(
                    project_id=project_id,
                    profile_id=profile_id or "default"
                )
                if sync_result["imported_count"] > 0:
                    logger.debug(
                        f"Auto-imported {sync_result['imported_count']} Claude Code sessions "
                        f"for project {project_id}"
                    )
            except Exception as e:
                # Don't fail the request if sync fails - just log and continue
                logger.warning(f"Failed to auto-sync Claude Code sessions: {e}")

    # Determine api_user_id filter value:
    # - admin_only=True  -> filter for sessions with api_user_id IS NULL
    # - api_users_only=True -> filter for sessions with api_user_id IS NOT NULL
    # - api_user_id set  -> filter for that specific user
    # - otherwise        -> show all sessions
    filter_api_user_id = None
    filter_api_users_only = False
    if admin_only:
        filter_api_user_id = ""  # Empty string signals "IS NULL" in database.get_sessions
    elif api_users_only:
        filter_api_users_only = True
    elif api_user_id:
        filter_api_user_id = api_user_id

    return project_id, profile_id, filter_api_user_id, filter_api_users_only


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
    project_id, profile_id, filter_api_user_id, filter_api_users_only = _apply_session_filters(
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
    project_id, profile_id, filter_api_user_id, filter_api_users_only = _apply_session_filters(
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


def enrich_search_results_with_tags(results: list) -> list:
    """Add tags array and has_forks field to each search result.

    Uses batch DB queries (2 total) instead of per-result queries to avoid
    O(N) round-trips that slow down with result count.
    """
    if not results:
        return results

    session_ids = [r["id"] for r in results]

    # Batch: tags for all results (1 query instead of N)
    tags_by_session = get_tags_for_sessions(session_ids)

    # Batch: fork info for all results (2 queries instead of N)
    fork_info_by_session = get_fork_info_for_sessions(session_ids)

    for result in results:
        sid = result["id"]
        result["tags"] = tags_by_session.get(sid, [])
        fork_info = fork_info_by_session.get(sid)
        result["has_forks"] = fork_info["has_forks"] if fork_info else False
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

    if sdk_session_id:
        try:
            from app.core.jsonl_parser import parse_session_history
            from app.core.config import settings

            # Get working dir from project if available
            project_id = session.get("project_id")
            if project_id:
                project = await run_sync(database.get_project, project_id)
                if project:
                    working_dir = get_project_working_dir(project)

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
            logger.debug(f"[Session API] Final token values: tokens_in={session.get('total_tokens_in')}, tokens_out={session.get('total_tokens_out')}, cache_creation={session.get('cache_creation_tokens')}, cache_read={session.get('cache_read_tokens')}, context={session.get('context_tokens')}")
        except Exception as e:
            # Log the error but don't fail - fall back to database
            logger.error(f"Failed to parse JSONL for session {session_id}: {e}")
            messages = []

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

    # Add worktree info if session is linked to a worktree
    worktree_id = session.get("worktree_id")
    if worktree_id:
        worktree = await run_sync(database.get_worktree, worktree_id)
        if worktree:
            session["worktree_id"] = worktree_id
            session["worktree_branch"] = worktree.get("branch_name")
            session["worktree_path"] = worktree.get("worktree_path")
            session["worktree_deleted"] = False
        else:
            # Worktree was deleted - use stored values from session columns
            session["worktree_id"] = None
            # worktree_branch and worktree_path are already in session from DB columns
            session["worktree_deleted"] = True
    elif session.get("worktree_branch"):
        # Worktree was fully unlinked (ON DELETE SET NULL) but stored info remains
        session["worktree_deleted"] = True

    logger.debug(f"Returning session {session_id} with {len(messages)} messages")
    try:
        return session
    except Exception as e:
        logger.exception(f"Failed to serialize session {session_id}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to serialize session"
        )


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

    # Check session exists and user has access
    session = await run_sync(database.get_session, session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}"
        )

    check_session_access(request, session)

    # Compute session metadata from DB session — included in all response
    # paths so the frontend can correct any stale state on switch.
    session_meta = {}

    # Always include the title so the frontend header stays in sync
    session_title = session.get("title")
    if session_title:
        session_meta["title"] = session_title

    wt_id = session.get("worktree_id")
    if wt_id:
        wt = await run_sync(database.get_worktree, wt_id)
        if wt:
            session_meta["worktree_id"] = wt_id
            session_meta["worktree_branch"] = wt.get("branch_name")
            session_meta["worktree_deleted"] = False
            session_meta["execution_mode"] = "worktree"
        else:
            session_meta["worktree_id"] = None
            session_meta["worktree_branch"] = session.get("worktree_branch")
            session_meta["worktree_deleted"] = True
            session_meta["execution_mode"] = "worktree"
    elif session.get("worktree_branch"):
        session_meta["worktree_id"] = None
        session_meta["worktree_branch"] = session.get("worktree_branch")
        session_meta["worktree_deleted"] = True
        session_meta["execution_mode"] = "worktree"

    # Check if session is in live store (actively streaming)
    if live_session_store.has_session(session_id):
        # Check if there are updates since last poll
        if since_update_id > 0:
            live_data = live_session_store.get_session_since(session_id, since_update_id)
            if live_data is None:
                # No updates since last check
                return Response(status_code=304)
            if session_meta:
                live_data["session_meta"] = session_meta
            return live_data
        else:
            live_data = live_session_store.get_session_dict(session_id)
            if live_data:
                if session_meta:
                    live_data["session_meta"] = session_meta
                return live_data

    # Not streaming - load from JSONL (same as get_session but simpler response)
    sdk_session_id = session.get("sdk_session_id")
    messages = []
    working_dir = "/workspace"
    usage_data = None

    if sdk_session_id:
        try:
            from app.core.jsonl_parser import parse_session_history
            from app.core.config import settings

            project_id = session.get("project_id")
            if project_id:
                project = await run_sync(database.get_project, project_id)
                if project:
                    working_dir = get_project_working_dir(project)

            jsonl_messages, usage_data = await run_sync(parse_session_history, sdk_session_id, working_dir)
            if jsonl_messages:
                # Optional fields to include when present (system, tool, subagent, metadata)
                _OPTIONAL_LIVE_FIELDS = (
                    "subtype", "systemSubtype", "systemData",
                    "toolName", "toolId", "toolInput", "toolResult", "toolStatus",
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

    # Fallback 2: SDK get_session_messages()
    if not messages and sdk_session_id and sdk_get_session_messages:
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

    # Fallback 3: DB messages
    if not messages:
        db_messages = await run_sync(database.get_session_messages, session_id)
        for m in db_messages:
            msg = {
                "id": str(m.get("id", "")),
                "role": m.get("role", "user"),
                "type": "tool_use" if m.get("tool_name") else "text",
                "content": m.get("content", ""),
                "streaming": False,
            }
            if m.get("tool_name"):
                msg["toolName"] = m.get("tool_name")
            if m.get("tool_id"):
                msg["toolId"] = m.get("tool_id")
            if m.get("tool_input"):
                msg["toolInput"] = m.get("tool_input")
            messages.append(msg)

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
    cache_creation = session.get("cache_creation_tokens", 0)
    cache_read = session.get("cache_read_tokens", 0)
    # Default to 0 — we want per-turn, not cumulative
    input_tokens = 0

    # Override with JSONL data if available
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
        }
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
    existing = await run_sync(database.get_session, session_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}"
        )

    check_session_access(request, existing)

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

    existing = await run_sync(database.get_session, session_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}"
        )

    check_session_access(request, existing)

    sdk_sid = existing.get("sdk_session_id")
    if not sdk_sid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Session has no SDK session ID"
        )

    try:
        sdk_tag_session(session_id=sdk_sid, tag=tag)
        return {"status": "ok", "session_id": session_id, "tag": tag}
    except Exception as e:
        logger.error(f"Failed to tag SDK session {sdk_sid}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to tag session: {e}"
        )


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

    existing = await run_sync(database.get_session, session_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}"
        )

    check_session_access(request, existing)

    sdk_sid = existing.get("sdk_session_id")
    if not sdk_sid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Session has no SDK session ID"
        )

    try:
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
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get SDK session info for {sdk_sid}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get session info: {e}"
        )


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(request: Request, session_id: str, token: str = Depends(require_auth)):
    """Delete a session and its messages. API users can only delete their accessible sessions."""
    existing = await run_sync(database.get_session, session_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}"
        )

    check_session_access(request, existing)

    # Evict from session pool before deleting — terminates subprocess if alive
    from app.core.session_pool import get_session_pool
    await get_session_pool().remove(session_id)

    await run_sync(database.delete_session, session_id)


@router.post("/batch-delete", status_code=status.HTTP_200_OK)
async def batch_delete_sessions(
    request: Request,
    body: BatchDeleteRequest,
    token: str = Depends(require_auth)
):
    """
    Delete multiple sessions at once.
    API users can only delete sessions they have access to.
    Returns count of successfully deleted sessions.
    """
    deleted_count = 0
    errors = []

    for session_id in body.session_ids:
        try:
            existing = await run_sync(database.get_session, session_id)
            if not existing:
                errors.append(f"Session not found: {session_id}")
                continue

            check_session_access(request, existing)
            # Evict from session pool before deleting
            from app.core.session_pool import get_session_pool
            await get_session_pool().remove(session_id)
            await run_sync(database.delete_session, session_id)
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
    existing = await run_sync(database.get_session, session_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}"
        )

    check_session_access(request, existing)

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
    existing = await run_sync(database.get_session, session_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}"
        )

    check_session_access(request, existing)

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
    existing = await run_sync(database.get_session, session_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}"
        )

    check_session_access(request, existing)

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
    session = await run_sync(database.get_session, session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}"
        )

    check_session_access(request, session)

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


@router.get("/{session_id}/export")
async def export_session(
    request: Request,
    session_id: str,
    format: str = Query("markdown", description="Export format: markdown or json"),
    token: str = Depends(require_auth)
):
    """
    Export a session in the specified format.

    Args:
        session_id: The session ID to export
        format: Export format - 'markdown' or 'json'

    Returns:
        - For markdown: Plain text Markdown representation
        - For JSON: Full session data including metadata and messages
    """
    from datetime import datetime, timezone
    from fastapi.responses import Response
    import json

    # Validate format
    if format not in ("markdown", "json"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid format. Must be 'markdown' or 'json'"
        )

    session = await run_sync(database.get_session, session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}"
        )

    check_session_access(request, session)

    # Load messages from JSONL (same logic as get_session)
    sdk_session_id = session.get("sdk_session_id")
    messages = []
    working_dir = "/workspace"

    if sdk_session_id:
        try:
            from app.core.jsonl_parser import parse_session_history
            from app.core.config import settings

            # Get working dir from project if available
            project_id = session.get("project_id")
            if project_id:
                project = await run_sync(database.get_project, project_id)
                if project:
                    working_dir = get_project_working_dir(project)

            jsonl_messages, _usage = await run_sync(parse_session_history, sdk_session_id, working_dir)
            if jsonl_messages:
                messages = jsonl_messages
        except Exception as e:
            logger.error(f"Failed to parse JSONL for session {session_id}: {e}")
            messages = []

    # Fall back to database if JSONL not available
    if not messages:
        db_messages = await run_sync(database.get_session_messages, session_id)
        for m in db_messages:
            msg = dict(m)
            if msg.get("tool_name"):
                msg["type"] = "tool_use"
                msg["toolName"] = msg.get("tool_name")
                msg["toolInput"] = msg.get("tool_input")
            elif msg.get("role") == "assistant":
                msg["type"] = "text"
            messages.append(msg)

    # Generate filename
    title = session.get("title") or "untitled-session"
    # Sanitize title for filename
    safe_title = "".join(c if c.isalnum() or c in "-_ " else "" for c in title)
    safe_title = safe_title.strip().replace(" ", "-")[:50]
    date_str = datetime.now().strftime("%Y%m%d")

    if format == "json":
        # JSON export - include full session data
        export_data = {
            "session": {
                "id": session.get("id"),
                "title": session.get("title"),
                "status": session.get("status"),
                "profile_id": session.get("profile_id"),
                "project_id": session.get("project_id"),
                "created_at": session.get("created_at"),
                "updated_at": session.get("updated_at"),
                "total_cost_usd": session.get("total_cost_usd"),
                "total_tokens_in": session.get("total_tokens_in"),
                "total_tokens_out": session.get("total_tokens_out"),
                "turn_count": session.get("turn_count"),
            },
            "messages": messages,
            "exported_at": datetime.now().isoformat(),
            "format_version": "1.0"
        }

        filename = f"{safe_title}-{date_str}.json"
        content = json.dumps(export_data, indent=2, default=str)

        return Response(
            content=content,
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"'
            }
        )

    else:
        # Markdown export
        md_lines = []

        # Header
        md_lines.append(f"# {session.get('title') or 'Untitled Session'}")
        md_lines.append("")

        # Metadata
        md_lines.append("## Session Info")
        md_lines.append("")
        md_lines.append(f"- **Created:** {session.get('created_at')}")
        md_lines.append(f"- **Updated:** {session.get('updated_at')}")
        if session.get("total_cost_usd"):
            md_lines.append(f"- **Cost:** ${session.get('total_cost_usd'):.4f}")
        if session.get("turn_count"):
            md_lines.append(f"- **Turns:** {session.get('turn_count')}")
        md_lines.append("")

        # Messages
        md_lines.append("## Conversation")
        md_lines.append("")

        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            msg_type = msg.get("type")
            timestamp = msg.get("metadata", {}).get("timestamp") if isinstance(msg.get("metadata"), dict) else None

            # Skip empty messages and certain system messages
            if not content and msg_type not in ("tool_use", "subagent"):
                continue

            if role == "user":
                md_lines.append("### Human")
                if timestamp:
                    md_lines.append(f"*{timestamp}*")
                md_lines.append("")
                md_lines.append(content)
                md_lines.append("")

            elif role == "assistant":
                if msg_type == "tool_use":
                    tool_name = msg.get("toolName") or msg.get("tool_name", "Unknown Tool")
                    tool_input = msg.get("toolInput") or msg.get("tool_input", {})
                    tool_result = msg.get("toolResult", "")

                    md_lines.append(f"### Tool: {tool_name}")
                    if timestamp:
                        md_lines.append(f"*{timestamp}*")
                    md_lines.append("")

                    # Format tool input
                    if tool_input:
                        md_lines.append("**Input:**")
                        md_lines.append("```json")
                        md_lines.append(json.dumps(tool_input, indent=2, default=str))
                        md_lines.append("```")
                        md_lines.append("")

                    # Format tool result
                    if tool_result:
                        # Coerce to string (tool_result can be a dict from JSONL)
                        if not isinstance(tool_result, str):
                            tool_result = json.dumps(tool_result, indent=2, default=str)
                        md_lines.append("**Result:**")
                        # Check if result looks like code or file content
                        if "\n" in tool_result or len(tool_result) > 100:
                            md_lines.append("```")
                            md_lines.append(tool_result)
                            md_lines.append("```")
                        else:
                            md_lines.append(tool_result)
                        md_lines.append("")

                elif msg_type == "subagent":
                    agent_type = msg.get("agentType", "Agent")
                    agent_desc = msg.get("agentDescription", "")

                    md_lines.append(f"### Subagent: {agent_type}")
                    if timestamp:
                        md_lines.append(f"*{timestamp}*")
                    md_lines.append("")
                    if agent_desc:
                        md_lines.append(f"**Task:** {agent_desc}")
                        md_lines.append("")
                    if content:
                        md_lines.append(content)
                        md_lines.append("")

                else:
                    # Regular text message
                    md_lines.append("### Assistant")
                    if timestamp:
                        md_lines.append(f"*{timestamp}*")
                    md_lines.append("")
                    md_lines.append(content)
                    md_lines.append("")

            elif role == "system":
                # Include system messages as notes
                subtype = msg.get("subtype", "")
                if content:
                    md_lines.append("---")
                    md_lines.append(f"*System ({subtype}):* {content[:500]}")
                    md_lines.append("---")
                    md_lines.append("")

        # Footer
        md_lines.append("---")
        md_lines.append(f"*Exported from AI Hub on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")

        filename = f"{safe_title}-{date_str}.md"
        content = "\n".join(md_lines)

        return Response(
            content=content,
            media_type="text/markdown",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"'
            }
        )


class SessionImportData(BaseModel):
    """Data structure for imported session"""
    session: dict
    messages: List[dict]
    exported_at: Optional[str] = None
    format_version: Optional[str] = None


class SessionImportResponse(BaseModel):
    """Response for session import"""
    session_id: str
    title: Optional[str]
    message_count: int
    status: str


@router.post("/import", response_model=SessionImportResponse)
async def import_session(
    request: Request,
    file: UploadFile = File(...),
    token: str = Depends(require_auth)
):
    """
    Import a session from a JSON file export.

    Accepts AI Hub JSON export format with session metadata and messages.
    Creates a new session with a new ID to avoid conflicts.

    Args:
        file: JSON file containing exported session data

    Returns:
        Information about the imported session
    """
    import json
    import uuid
    from datetime import datetime, timezone
    from app.core.config import settings

    # Validate file type
    if not file.filename or not file.filename.endswith('.json'):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid file type. Please upload a JSON file."
        )

    # Read and parse file content (with size limit to prevent memory exhaustion)
    max_size = 50 * 1024 * 1024  # 50MB limit
    try:
        # Check Content-Length header first (fast reject)
        content_length = file.size
        if content_length and content_length > max_size:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="File too large. Maximum size is 50MB."
            )

        # Stream-read with size cap to avoid unbounded memory use
        chunks = []
        total = 0
        while True:
            chunk = await file.read(1024 * 1024)  # 1MB chunks
            if not chunk:
                break
            total += len(chunk)
            if total > max_size:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail="File too large. Maximum size is 50MB."
                )
            chunks.append(chunk)
        content = b"".join(chunks)

        import_data = json.loads(content.decode('utf-8'))
    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid JSON file: {str(e)}"
        )
    except Exception as e:
        logger.exception("Failed to read session import file")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to read file"
        )

    # Validate import data structure
    if 'session' not in import_data or 'messages' not in import_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid export format. Missing 'session' or 'messages' field."
        )

    session_data = import_data['session']
    messages_data = import_data['messages']

    if not isinstance(session_data, dict) or not isinstance(messages_data, list):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid export format. 'session' must be an object and 'messages' must be an array."
        )

    # Generate new session ID
    new_session_id = f"ses-{uuid.uuid4().hex[:16]}"
    new_sdk_session_id = str(uuid.uuid4())

    # Get profile ID - use from import or default to 'default'
    profile_id = session_data.get('profile_id') or 'default'

    # Verify profile exists, fall back to default if not
    profile = await run_sync(database.get_profile, profile_id)
    if not profile:
        profile_id = 'default'
        profile = await run_sync(database.get_profile, profile_id)
        if not profile:
            # Create default profile if it doesn't exist
            profiles = await run_sync(database.get_all_profiles)
            if profiles:
                profile_id = profiles[0]['id']
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="No profiles available. Please create a profile first."
                )

    # Get API user if applicable
    api_user = get_api_user_from_request(request)
    api_user_id = api_user['id'] if api_user else None

    # Create session in database
    title = session_data.get('title') or 'Imported Session'
    _now = datetime.now(timezone.utc).isoformat()  # Available for future metadata use

    # Create session - result used implicitly by subsequent operations
    await run_sync(
        database.create_session,
        session_id=new_session_id,
        profile_id=profile_id,
        project_id=session_data.get('project_id'),
        title=title,
        api_user_id=api_user_id
    )

    # Update session with SDK session ID and stats
    await run_sync(
        database.update_session,
        session_id=new_session_id,
        sdk_session_id=new_sdk_session_id,
        cost_increment=session_data.get('total_cost_usd') or 0,
        tokens_in_increment=session_data.get('total_tokens_in') or 0,
        tokens_out_increment=session_data.get('total_tokens_out') or 0,
        turn_increment=session_data.get('turn_count') or len([m for m in messages_data if m.get('role') == 'user'])
    )

    # Create JSONL file for the session
    try:
        from app.core.jsonl_parser import get_project_dir_name

        # Get Claude projects directory
        claude_projects_dir = settings.get_claude_projects_dir

        # Determine project directory - use /workspace by default
        working_dir = "/workspace"
        if session_data.get('project_id'):
            project = await run_sync(database.get_project, session_data['project_id'])
            if project:
                working_dir = get_project_working_dir(project)

        # Convert working dir to Claude's project directory format
        project_dir_name = get_project_dir_name(working_dir)
        project_dir = claude_projects_dir / project_dir_name

        # Ensure project directory exists
        project_dir.mkdir(parents=True, exist_ok=True)

        jsonl_path = project_dir / f"{new_sdk_session_id}.jsonl"

        # Convert messages to JSONL format
        with open(jsonl_path, 'w', encoding='utf-8') as f:
            for msg in messages_data:
                role = msg.get('role', 'user')
                content = msg.get('content', '')
                msg_type = msg.get('type')
                timestamp = msg.get('metadata', {}).get('timestamp') or datetime.now(timezone.utc).isoformat()
                msg_uuid = msg.get('id') or str(uuid.uuid4())

                if role == 'user' and msg_type != 'tool_result':
                    # User message
                    entry = {
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": content
                        },
                        "uuid": msg_uuid,
                        "timestamp": timestamp
                    }
                    f.write(json.dumps(entry) + '\n')

                elif role == 'assistant':
                    if msg_type == 'tool_use':
                        # Tool use message
                        tool_name = msg.get('toolName') or msg.get('tool_name')
                        tool_input = msg.get('toolInput') or msg.get('tool_input') or {}
                        tool_id = msg.get('toolId') or msg.get('tool_id') or str(uuid.uuid4())

                        content_blocks = []
                        if content:
                            content_blocks.append({"type": "text", "text": content})
                        content_blocks.append({
                            "type": "tool_use",
                            "id": tool_id,
                            "name": tool_name,
                            "input": tool_input
                        })

                        entry = {
                            "type": "assistant",
                            "message": {
                                "role": "assistant",
                                "content": content_blocks
                            },
                            "uuid": msg_uuid,
                            "timestamp": timestamp
                        }
                        f.write(json.dumps(entry) + '\n')

                        # Write tool result if present
                        tool_result = msg.get('toolResult')
                        if tool_result:
                            result_entry = {
                                "type": "user",
                                "message": {
                                    "role": "user",
                                    "content": [{
                                        "type": "tool_result",
                                        "tool_use_id": tool_id,
                                        "content": tool_result
                                    }]
                                },
                                "uuid": str(uuid.uuid4()),
                                "timestamp": timestamp
                            }
                            f.write(json.dumps(result_entry) + '\n')

                    elif msg_type == 'subagent':
                        # Subagent message - write as tool use
                        tool_id = msg.get('toolId') or str(uuid.uuid4())
                        agent_type = msg.get('agentType') or msg.get('agentDescription') or 'Agent'
                        description = msg.get('agentDescription', '')

                        content_blocks = [{
                            "type": "tool_use",
                            "id": tool_id,
                            "name": "Agent",
                            "input": {
                                "subagent_type": agent_type,
                                "description": description
                            }
                        }]

                        entry = {
                            "type": "assistant",
                            "message": {
                                "role": "assistant",
                                "content": content_blocks
                            },
                            "uuid": msg_uuid,
                            "timestamp": timestamp
                        }
                        f.write(json.dumps(entry) + '\n')

                    else:
                        # Text message
                        if content:
                            entry = {
                                "type": "assistant",
                                "message": {
                                    "role": "assistant",
                                    "content": [{"type": "text", "text": content}]
                                },
                                "uuid": msg_uuid,
                                "timestamp": timestamp
                            }
                            f.write(json.dumps(entry) + '\n')

                elif role == 'system':
                    # System message
                    subtype = msg.get('subtype', '')
                    entry = {
                        "type": "system",
                        "subtype": subtype,
                        "content": content,
                        "uuid": msg_uuid,
                        "timestamp": timestamp
                    }
                    f.write(json.dumps(entry) + '\n')

        logger.debug(f"Created JSONL file for imported session: {jsonl_path}")

    except Exception as e:
        logger.error(f"Failed to create JSONL file for imported session: {e}")
        # Continue anyway - the session is in the database

    logger.debug(f"Imported session {new_session_id} with {len(messages_data)} messages")

    return SessionImportResponse(
        session_id=new_session_id,
        title=title,
        message_count=len(messages_data),
        status="success"
    )


# ============================================================================
# Import Claude Code JSONL Session
# ============================================================================

class ImportJSONLRequest(BaseModel):
    """Request body for importing a JSONL session"""
    jsonl_path: str
    profile_id: Optional[str] = None
    project_id: Optional[str] = None


class ImportJSONLResponse(BaseModel):
    """Response for JSONL session import"""
    session_id: str
    sdk_session_id: str
    title: Optional[str]
    message_count: int
    status: str


@router.post("/import-jsonl", response_model=ImportJSONLResponse)
async def import_jsonl_session(
    request: Request,
    body: ImportJSONLRequest,
    token: str = Depends(require_auth)
):
    """
    Import an existing Claude Code JSONL session into AI Shuffle.

    This allows resuming sessions that were created directly in Claude Code
    without going through AI Shuffle. The session will be registered in the
    AI Shuffle database, allowing it to be resumed and managed like any other session.

    Args:
        body: Request containing:
            - jsonl_path: Full path to the JSONL file
            - profile_id: Optional profile to associate (defaults to 'default')
            - project_id: Optional project to associate

    Returns:
        Information about the imported session
    """
    import uuid
    from pathlib import Path
    from app.core.jsonl_parser import (
        get_session_cost_from_jsonl,
        _get_session_title_and_preview
    )

    jsonl_path = Path(body.jsonl_path)

    # Validate the file exists and is a JSONL file
    if not jsonl_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"JSONL file not found: {body.jsonl_path}"
        )

    if not jsonl_path.suffix == '.jsonl':
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File must be a JSONL file"
        )

    # Skip agent files
    if jsonl_path.name.startswith("agent-"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot import agent files (subagent logs)"
        )

    # Get SDK session ID from filename
    sdk_session_id = jsonl_path.stem

    # Check if this session is already imported
    existing_session = await run_sync(database.get_session_by_sdk_id, sdk_session_id)
    if existing_session:
        return ImportJSONLResponse(
            session_id=existing_session["id"],
            sdk_session_id=sdk_session_id,
            title=existing_session.get("title"),
            message_count=existing_session.get("turn_count", 0),
            status="already_exists"
        )

    # Get title and preview from JSONL
    title, preview, message_count = _get_session_title_and_preview(jsonl_path)
    if not title:
        title = f"Imported: {sdk_session_id[:8]}..."

    # Get profile (default to 'default')
    profile_id = body.profile_id or 'default'
    profile = await run_sync(database.get_profile, profile_id)
    if not profile:
        profile_id = 'default'

    # Validate project if specified
    project_id = body.project_id
    if project_id:
        project = await run_sync(database.get_project, project_id)
        if not project:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Project not found: {project_id}"
            )

    # Get working dir from project or from the JSONL path
    working_dir = str(jsonl_path.parent.parent)  # Go up from session file to project dir

    # Parse JSONL to get token usage
    try:
        usage_data = get_session_cost_from_jsonl(sdk_session_id, working_dir)
        total_tokens_in = usage_data.get("total_tokens_in", 0) if usage_data else 0
        total_tokens_out = usage_data.get("total_tokens_out", 0) if usage_data else 0
        total_cost = usage_data.get("total_cost", 0.0) if usage_data else 0.0
    except Exception as e:
        logger.warning(f"Could not get token usage from JSONL: {e}")
        total_tokens_in = 0
        total_tokens_out = 0
        total_cost = 0.0

    # Create new session in database
    new_session_id = f"ses-{uuid.uuid4().hex[:16]}"

    await run_sync(
        database.create_session,
        session_id=new_session_id,
        profile_id=profile_id,
        project_id=project_id,
        title=title,
        api_user_id=None  # Local import
    )

    # Update session with SDK session ID and stats
    await run_sync(
        database.update_session,
        session_id=new_session_id,
        sdk_session_id=sdk_session_id,
        cost_increment=total_cost,
        tokens_in_increment=total_tokens_in,
        tokens_out_increment=total_tokens_out,
        turn_increment=message_count
    )

    logger.debug(f"Imported JSONL session {new_session_id} (SDK: {sdk_session_id}) with {message_count} messages")

    return ImportJSONLResponse(
        session_id=new_session_id,
        sdk_session_id=sdk_session_id,
        title=title,
        message_count=message_count,
        status="success"
    )


# ============================================================================
# Session Fork Endpoint
# ============================================================================

class ForkSessionRequest(BaseModel):
    """Request body for forking a session"""
    message_index: int


class ForkSessionResponse(BaseModel):
    """Response for session fork"""
    session_id: str
    title: Optional[str]
    parent_session_id: str
    fork_point_message_index: int
    message_count: int
    status: str


@router.post("/{session_id}/fork", response_model=ForkSessionResponse)
async def fork_session(
    request: Request,
    session_id: str,
    body: ForkSessionRequest,
    token: str = Depends(require_auth)
):
    """
    Fork a session from a specific message index.

    Creates a new session that contains messages up to and including the
    specified message index. The new session references the original as its parent.

    Args:
        session_id: The session ID to fork from
        body: Request body containing message_index (0-based index to fork after)

    Returns:
        Information about the forked session
    """
    import uuid
    import json
    from app.core.config import settings
    from app.core.jsonl_parser import get_session_jsonl_path, parse_jsonl_file

    # Get the original session
    original_session = await run_sync(database.get_session, session_id)
    if not original_session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}"
        )

    check_session_access(request, original_session)

    sdk_session_id = original_session.get("sdk_session_id")
    if not sdk_session_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot fork session without SDK session ID"
        )

    # Get working directory
    working_dir = "/workspace"
    project_id = original_session.get("project_id")
    if project_id:
        project = await run_sync(database.get_project, project_id)
        if project:
            working_dir = get_project_working_dir(project)

    # Find the original JSONL file
    original_jsonl_path = get_session_jsonl_path(sdk_session_id, working_dir)
    if not original_jsonl_path:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot fork session: JSONL file not found"
        )

    # Parse original JSONL to get messages up to fork point
    original_entries = list(parse_jsonl_file(original_jsonl_path))

    # Count actual display messages (filter out queue-operation, file-history-snapshot, etc.)
    display_entries = []
    for entry in original_entries:
        entry_type = entry.get("type")
        # Skip non-message entries
        if entry_type in ("queue-operation", "file-history-snapshot"):
            continue
        # Skip meta messages
        if entry.get("isMeta"):
            continue
        # Skip sidechain messages
        if entry.get("isSidechain"):
            continue
        display_entries.append(entry)

    # Validate message_index
    if body.message_index < 0 or body.message_index >= len(display_entries):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid message_index: {body.message_index}. Session has {len(display_entries)} messages (0-{len(display_entries)-1})"
        )

    # Find the actual index in original_entries that corresponds to the message_index in display_entries
    display_count = 0
    fork_entry_index = 0
    for i, entry in enumerate(original_entries):
        entry_type = entry.get("type")
        if entry_type in ("queue-operation", "file-history-snapshot"):
            continue
        if entry.get("isMeta"):
            continue
        if entry.get("isSidechain"):
            continue

        if display_count == body.message_index:
            fork_entry_index = i
            break
        display_count += 1

    # Generate new IDs
    new_session_id = f"ses-{uuid.uuid4().hex[:16]}"
    new_sdk_session_id = str(uuid.uuid4())

    # Get API user if applicable
    api_user = get_api_user_from_request(request)
    api_user_id = api_user['id'] if api_user else None

    # Create title for fork
    original_title = original_session.get('title') or 'Untitled'
    fork_title = f"Fork of {original_title}"

    # Create the forked session in database - result used implicitly
    await run_sync(
        database.create_session,
        session_id=new_session_id,
        profile_id=original_session.get('profile_id'),
        project_id=original_session.get('project_id'),
        title=fork_title,
        api_user_id=api_user_id,
        parent_session_id=session_id,
        fork_point_message_index=body.message_index
    )

    # Update with SDK session ID
    await run_sync(
        database.update_session,
        session_id=new_session_id,
        sdk_session_id=new_sdk_session_id
    )

    # Create new JSONL file with entries up to and including fork point
    try:
        from app.core.jsonl_parser import get_project_dir_name

        # Get Claude projects directory
        claude_projects_dir = settings.get_claude_projects_dir

        # Convert working dir to Claude's project directory format
        project_dir_name = get_project_dir_name(working_dir)
        project_dir = claude_projects_dir / project_dir_name

        # Ensure project directory exists
        project_dir.mkdir(parents=True, exist_ok=True)

        new_jsonl_path = project_dir / f"{new_sdk_session_id}.jsonl"

        # Write entries up to and including fork point
        with open(new_jsonl_path, 'w', encoding='utf-8') as f:
            for i, entry in enumerate(original_entries):
                if i > fork_entry_index:
                    break
                f.write(json.dumps(entry) + '\n')

        logger.debug(f"Created forked JSONL file: {new_jsonl_path} with {fork_entry_index + 1} entries")

    except Exception as e:
        logger.exception("Failed to create JSONL file for forked session")
        # Delete the session we just created since fork failed
        await run_sync(database.delete_session, new_session_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create forked session"
        )

    logger.debug(f"Forked session {session_id} to {new_session_id} at message index {body.message_index}")

    return ForkSessionResponse(
        session_id=new_session_id,
        title=fork_title,
        parent_session_id=session_id,
        fork_point_message_index=body.message_index,
        message_count=fork_entry_index + 1,
        status="success"
    )


# ============================================================================
# Worktree Session Endpoint
# ============================================================================

class CreateWorktreeSessionRequest(BaseModel):
    """Request body for creating a session with a worktree"""
    project_id: str
    branch_name: str
    base_branch: str = "main"
    profile_id: Optional[str] = None
    create_new_branch: bool = False  # False = use existing branch, True = create new branch


class WorktreeSessionResponse(BaseModel):
    """Response for worktree session creation"""
    session_id: str
    worktree_id: str
    branch_name: str
    base_branch: str
    worktree_path: str
    status: str


@router.post("/with-worktree", response_model=WorktreeSessionResponse)
async def create_session_with_worktree(
    request: Request,
    body: CreateWorktreeSessionRequest,
    token: str = Depends(require_auth)
):
    """
    Create a new session with an associated git worktree.

    This endpoint creates both a git worktree (for isolated branch development)
    and a linked chat session. The worktree allows parallel development on a
    feature branch while keeping the main repository intact.

    Args:
        project_id: The project to create the worktree in
        branch_name: Name for the new branch
        base_branch: Branch to base the new branch on (default: main)
        profile_id: Optional profile ID for the session

    Returns:
        Session and worktree information
    """
    from app.core.worktree_manager import worktree_manager

    # Verify project exists and user has access
    project = await run_sync(database.get_project, body.project_id)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project not found: {body.project_id}"
        )

    # Check API user restrictions
    api_user = get_api_user_from_request(request)
    if api_user and api_user.get("project_id"):
        if api_user["project_id"] != body.project_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this project"
            )

    # Create worktree and session
    from app.core.worktree_manager import WorktreeError

    try:
        worktree, session = worktree_manager.create_worktree_session(
            project_id=body.project_id,
            branch_name=body.branch_name,
            create_new_branch=body.create_new_branch,
            base_branch=body.base_branch,
            profile_id=body.profile_id
        )
    except WorktreeError as e:
        logger.warning(f"Worktree creation failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        logger.exception("Unexpected error creating worktree session")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create worktree session"
        )

    logger.debug(f"Created worktree session: {session['id']} with worktree: {worktree['id']}")

    return WorktreeSessionResponse(
        session_id=session["id"],
        worktree_id=worktree["id"],
        branch_name=worktree["branch_name"],
        base_branch=worktree.get("base_branch") or body.base_branch,
        worktree_path=worktree["worktree_path"],
        status="active"
    )
