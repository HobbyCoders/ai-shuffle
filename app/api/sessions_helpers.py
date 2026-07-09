"""
Session helper functions, models, and shared state.

Extracted from sessions.py for maintainability. All symbols are
re-exported from sessions.py for backward compatibility.
"""

from typing import List, Optional

from fastapi import HTTPException, Request, status
from pydantic import BaseModel

import time

from app.db import database
from app.db.async_database import run_sync
from app.db.tags import get_tags_for_sessions
from app.db.sessions import get_fork_info_for_sessions
from app.api.auth import get_api_user_from_request

import logging
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Throttle for session sync — at most once per 60 s per project
# ---------------------------------------------------------------------------
_SESSION_SYNC_THROTTLE_S = 60.0
_last_sync_times: dict[str, float] = {}  # project_id -> monotonic timestamp


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class BatchDeleteRequest(BaseModel):
    """Request body for batch delete operation"""
    session_ids: List[str]


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


class ForkSessionRequest(BaseModel):
    """Request body for forking a session.

    ``message_index`` is optional: omit it (or send ``null``) to fork the
    ENTIRE conversation — the sessions-board "Fork" action, which duplicates a
    whole session rather than branching from a specific message. When provided,
    the fork happens up to and including that 0-based display-message index.
    """
    message_index: Optional[int] = None


class ForkSessionResponse(BaseModel):
    """Response for session fork"""
    session_id: str
    title: Optional[str]
    parent_session_id: str
    fork_point_message_index: int
    message_count: int
    status: str


class AdoptSessionResponse(BaseModel):
    """Response for the cross-device 'Resume here' adopt action.

    ``adopted`` is True when ownership was re-pointed to this install; False
    when the session was already local (a no-op — the caller just opens it).
    """
    session_id: str
    instance_id: str
    adopted: bool
    status: str


class CreateWorktreeSessionRequest(BaseModel):
    """Request body for creating a session with a worktree"""
    project_id: str
    branch_name: str
    base_branch: str = "main"
    profile_id: Optional[str] = None
    create_new_branch: bool = False  # False = use existing branch, True = create new branch
    # Optional first-prompt kickoff — when ``prompt`` is set the backend
    # persists the user message, migrates any pending uploads into the new
    # worktree, and launches ``run_query`` in a detached task. This lets the
    # user lock their phone / switch sessions during creation without losing
    # the in-flight query (it runs server-side regardless of WS state).
    prompt: Optional[str] = None
    overrides: Optional[dict] = None
    pending_upload_paths: Optional[List[str]] = None


class WorktreeSessionResponse(BaseModel):
    """Response for worktree session creation"""
    session_id: str
    worktree_id: str
    branch_name: str
    base_branch: str
    worktree_path: str
    status: str
    # True when ``prompt`` was provided and the backend has already spawned
    # a ``run_query`` task for the new session. Frontend uses this flag to
    # skip sending a redundant WS ``query`` message.
    query_started: bool = False
    upload_migration_errors: Optional[List[str]] = None


# ---------------------------------------------------------------------------
# SDK message transformation
# ---------------------------------------------------------------------------

def parse_session_history_via_store(
    sdk_session_id: str, working_dir: str, *, use_cache: bool = True
) -> tuple[list, dict]:
    """Parse a session transcript from the Postgres session store (postgres_only).

    Synchronous (call via ``run_sync``). Builds a ``PgSessionStore`` and a sync
    ``store_loader`` that matches entries on ``session_id`` (+subpath) ALONE —
    the stored ``project_key`` is the on-disk folder name and diverges from any
    cwd-derived key, so we never reconstruct it (see
    ``app/core/pg_session_store.py``). Delegates to
    :func:`app.core.jsonl_parser.parse_session_history_from_store`, which runs
    the entries through the SAME transform as the JSONL path. Returns the same
    ``(messages, usage_data)`` shape as ``parse_session_history``.

    ``use_cache=False`` forces a fresh parse straight from the store entries,
    bypassing ``_store_parse_cache``. Used by the post-turn reseed to obtain an
    authoritative, ``uuid``-bearing transcript (the in-memory live-store
    messages carry no JSONL uuid, which breaks rewind anchoring).
    """
    from app.core.pg_session_store import (
        _load_entries_by_session_id_sync,
        _store_version_by_session_id_sync,
        lazy_backfill_session_from_jsonl,
    )
    from app.core.jsonl_parser import parse_session_history_from_store

    # Store miss → almost always a pre-cutover session whose JSONL was never
    # bulk-backfilled before the postgres_only flip. Heal the store from the
    # local JSONL before parsing so the chat renders its history instead of
    # empty. Guarded inside (count, summary tombstone, JSONL idle-mtime,
    # advisory lock) so it is a cheap no-op for new/live/stored sessions.
    count, _ = _store_version_by_session_id_sync(sdk_session_id)
    if count == 0:
        lazy_backfill_session_from_jsonl(sdk_session_id, working_dir)

    def _store_loader(session_id: str, subpath: Optional[str] = None) -> list:
        return _load_entries_by_session_id_sync(session_id, subpath)

    def _store_version_loader(session_id: str) -> tuple[int, int]:
        # Cheap (count, max_id) stamp so a cached parse from before a rewind on
        # THIS or another install (shared Postgres) is dropped on read rather
        # than served verbatim — the cross-install "rewind still broken" bug.
        return _store_version_by_session_id_sync(session_id)

    return parse_session_history_from_store(
        sdk_session_id,
        working_dir,
        _store_loader,
        use_cache=use_cache,
        store_version_loader=_store_version_loader,
    )


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


# ---------------------------------------------------------------------------
# Access control helpers
# ---------------------------------------------------------------------------

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


async def require_session(request: Request, session_id: str) -> dict:
    """Fetch a session, verify it exists, and check access in one call.

    Replaces the repeated pattern of:
        existing = await run_sync(database.get_session, session_id)
        if not existing: raise HTTPException(404)
        check_session_access(request, existing)

    Returns the session dict if valid.
    Raises HTTPException(404) if not found, HTTPException(403) if access denied.
    """
    session = await run_sync(database.get_session, session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}"
        )
    check_session_access(request, session)
    return session


# ---------------------------------------------------------------------------
# Enrichment helpers
# ---------------------------------------------------------------------------

def _annotate_cluster_fields(sessions: List[dict]) -> None:
    """Annotate each session (in place) with owning-device + project info.

    On a shared database every install's sessions are now visible together, so
    each row needs to say *which* device owns it, whether that device is online,
    and whether it can be controlled from here. We also resolve project names
    server-side: the frontend ``$projects`` map is instance-filtered, so a
    cross-install session's project would otherwise render as unknown.

    Cheap and batched: one registry read (``get_installs_map``) plus one
    ``get_project`` per distinct project id (the non-instance-filtered lookup,
    so cross-install projects resolve too).
    """
    if not sessions:
        return

    from app.core.config import settings
    me = settings.effective_instance_id

    # Batch: installs registry (id -> row, each carrying an in-DB ``online`` flag
    # and the peer-reachable base_url that gates control).
    try:
        installs_map = database.get_installs_map()
    except Exception:  # noqa: BLE001 — annotation must never break the listing
        logger.debug("Failed to load installs registry for enrichment", exc_info=True)
        installs_map = {}

    # Batch: resolve project names + link groups via the NON-instance-filtered
    # get_project so projects owned by another install still resolve. The link
    # group lets the board fold same-repo clones from every install into one
    # logical project.
    project_names: dict[str, str] = {}
    project_link_groups: dict[str, Optional[str]] = {}
    for pid in {s.get("project_id") for s in sessions if s.get("project_id")}:
        try:
            proj = database.get_project(pid)
        except Exception:  # noqa: BLE001
            proj = None
        if proj:
            project_names[pid] = proj.get("name") or proj.get("path") or pid
            project_link_groups[pid] = proj.get("link_group_id")

    my_install = installs_map.get(me)
    for session in sessions:
        inst = session.get("instance_id")
        is_remote = bool(inst) and inst != me
        session["is_remote"] = is_remote

        if is_remote:
            install = installs_map.get(inst)
            online = bool(install.get("online")) if install else False
            session["device_name"] = install.get("device_name") if install else None
            session["device_online"] = online
            # Controllable only when the owner is online AND advertises a URL we
            # can relay to; otherwise history stays readable but read-only.
            session["controllable"] = bool(online and install and install.get("base_url"))
        else:
            # Local or legacy (NULL instance_id): always controllable from here.
            session["device_name"] = my_install.get("device_name") if my_install else None
            session["device_online"] = True
            session["controllable"] = True

        pid = session.get("project_id")
        if pid and pid in project_names:
            session["project_name"] = project_names[pid]
        if pid and project_link_groups.get(pid):
            session["link_group_id"] = project_link_groups[pid]


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

    # Annotate owning device + project (cross-install sharing).
    _annotate_cluster_fields(sessions)
    return sessions


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

    # Annotate owning device + project (cross-install sharing).
    _annotate_cluster_fields(results)
    return results


# ---------------------------------------------------------------------------
# Shared filter logic
# ---------------------------------------------------------------------------

async def _apply_session_filters(
    request: Request,
    project_id: Optional[str],
    profile_id: Optional[str],
    api_user_id: Optional[str],
    admin_only: bool,
    api_users_only: bool,
    sync_claude_sessions: bool,
) -> tuple:
    """Apply API user restrictions and sync Claude sessions.

    Shared logic for list_sessions / list_sessions_paginated / list_sessions_cursor:
    1. Force project/profile/api_user_id from API user restrictions
    2. Auto-sync Claude Code sessions for the project (disk scan offloaded
       to a worker thread so it never blocks the event loop)
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
                # Offload the disk scan + N DB lookups to a worker thread.
                # Run inline (as before) it blocked the event loop for the full
                # scan, freezing EVERY concurrent request — the observed "all
                # endpoints return at the same elapsed time" stall. Throttle is
                # stamped before the await so a concurrent list request inside
                # the same 60 s window still skips the scan.
                sync_result = await run_sync(
                    session_sync_service.sync_project_sessions,
                    project_id=project_id,
                    profile_id=profile_id or "default",
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
