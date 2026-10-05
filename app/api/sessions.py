"""
Session management API routes

This module was split for maintainability. Sub-modules:
- sessions_helpers: shared helpers, models, constants
- sessions_export: export/import endpoints
- sessions_fork: fork endpoint
- sessions_worktree: worktree session endpoint
"""

import asyncio
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Depends, Query, Response, status, Request

from app.core.cli_sessions import (
    rename_session as sdk_rename_session,
    delete_session as sdk_delete_session,
)

from app.core.models import (
    Session,
    WorkspaceFoldersUpdate,
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
# Sub-module imports (check_session_access / enrich_sessions_with_tags are also
# imported from here by commands.py, tags.py and tests)
# ---------------------------------------------------------------------------
from app.api.sessions_helpers import (  # noqa: F401 — re-exports
    parse_session_history_via_store,
    check_session_access,
    require_session,
    enrich_sessions_with_tags,
    fetch_and_enrich_sessions,
    fetch_and_enrich_search_results,
    build_detail_messages,
    build_live_messages,
    finalize_transcript,
    paginate_messages,
    DEFAULT_MESSAGE_PAGE_LIMIT,
    MAX_MESSAGE_PAGE_LIMIT,
    _annotate_cluster_fields,
    _apply_session_filters,
    api_user_profile_filter,
    BatchDeleteRequest,
    SessionActiveUpdate,
    AdoptRequest,
    AdoptSessionResponse,
    MoveSessionRequest,
    MoveSessionResponse,
    QueueMoveRequest,
    apply_sdk_session_keep,
)

from app.api.sessions_export import (
    export_router,
)

from app.api.sessions_fork import (
    fork_router,
)

from app.api.sessions_worktree import (
    worktree_router,
)
from app.api.sessions_create import CreateSessionResponse, create_empty_session
from app.api.sessions_permissions import router as permissions_router
from app.api.sessions_uploads import router as uploads_router

# ---------------------------------------------------------------------------
# Router setup — include sub-routers so all endpoints are served
# ---------------------------------------------------------------------------
router = APIRouter(prefix="/api/v1/sessions", tags=["Sessions"])
router.include_router(export_router)
router.include_router(fork_router)
router.include_router(worktree_router)
router.include_router(permissions_router)
router.include_router(uploads_router)
# POST /api/v1/sessions: an empty session from the composer's picks. Added on
# this router directly because a sub-router cannot own the bare prefix path.
router.add_api_route(
    "", create_empty_session, methods=["POST"],
    response_model=CreateSessionResponse, tags=["Sessions"],
)


# ============================================================================
# Cross-device adopt ("Resume here")
# ============================================================================

def _refuse_archived(session: dict, action: str) -> None:
    """409 for an archived chat: it is read-only until the owner restores it."""
    from app.core import archive_guard

    if archive_guard.is_archived_row(session):
        archive_guard.log_dropped(session["id"], action, f"{action} refused")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This chat is archived. Restore it before you {action} it.",
        )


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


async def _parse_adopt_request(request: Request) -> AdoptRequest:
    """Read the adopt body first. Empty / omitted JSON → pin-to-here."""
    import json as json_lib

    raw_body = await request.body()
    if not raw_body:
        return AdoptRequest()
    try:
        data = json_lib.loads(raw_body)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid JSON body",
        )
    if data is None:
        return AdoptRequest()
    if not isinstance(data, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="JSON body must be an object",
        )
    try:
        return AdoptRequest.model_validate(data)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )


async def _prepare_local_checkout(session: dict, session_id: str, me: str) -> bool:
    """Prepare the canonical local checkout before transferring ownership.

    Git projects move their branch through the origin remote; project-less
    sessions transfer history only. Failures leave ownership unchanged.
    """
    from app.core import project_resolver
    from app.network import handoff, control
    has_project = bool(session.get("project_v2_id") or session.get("project_id"))

    try:
        has_base = not has_project or await run_sync(
            project_resolver.has_local_base_checkout, session, me
        )
        if not has_base:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "This session's project isn't checked out on this device "
                    "yet. Create a checkout of the repository here (clone it and "
                    "open the project), then try again."
                ),
            )
        try:
            cwd = await handoff.receive(session)
        except (control.RunnerUnreachable, control.ControlRefused, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        transferred = cwd is not None
        if not has_project:
            return transferred
        if cwd is None:
            cwd = await run_sync(project_resolver.resolve_cwd, session)
        if not cwd:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "This session's checkout could not be prepared on this device. "
                    "Make sure its branch is available locally or pushed to the "
                    "repository's remote, and check for a conflicting checkout "
                    "directory here. Then try again."
                ),
            )
        return transferred
    except HTTPException:
        raise
    except Exception:
        logger.warning(
            "Adopt checkout preparation failed for session %s",
            session_id,
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Could not prepare this session's local checkout. Ownership has "
                "not changed. Check repository access and the server logs, then "
                "try again."
            ),
        )


@router.post("/{session_id}/adopt", response_model=AdoptSessionResponse)
async def adopt_session_endpoint(
    request: Request,
    session_id: str,
    token: str = Depends(require_auth),
):
    """Take over a session created by another install so it can resume locally.

    Body is parsed first. Empty JSON / omitted fields is pin-to-here (today's
    Resume here). ``{"follow": true}`` is unpin: this install becomes the owner
    and ``placement`` is NULLed so the next send from another runner auto-adopts.

    A live run 409s in every case (including already-local unpin) so two
    installs never drive one SDK turn. ``sdk_session_id`` is kept only when
    Fluid (shared PG) or a CLI JSONL file exists on this disk.
    """
    from app.core.config import settings
    from app.core import streaming_status
    from app.api.git_helpers import invalidate_working_dir_cache

    body = await _parse_adopt_request(request)
    session = await require_session(request, session_id)
    _refuse_archived(session, "resume")

    me = settings.effective_instance_id
    from app.network import control
    owner = control.owner_of(session) or session.get("instance_id")

    if streaming_status.has_active_run(session_id):
        device = await run_sync(_adopt_device_name, owner) or "another device"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This session is currently running on {device}. "
                "Stop it there before resuming it here."
            ),
        )

    if body.follow:
        if owner and owner != me:
            await _prepare_local_checkout(session, session_id, me)
        updated = await run_sync(database.unpin_session, session_id, me)
        if not updated:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Session not found: {session_id}",
            )
        invalidate_working_dir_cache(session_id=session_id)
        await run_sync(apply_sdk_session_keep, updated, me)
        logger.info(
            "Unpinned session %s (owner %s -> %s, placement NULL)",
            session_id, owner, me,
        )
        return AdoptSessionResponse(
            session_id=session_id,
            instance_id=me,
            adopted=True,
            status="unpinned",
        )

    # pin-to-here (today)
    if not owner or owner == me:
        return AdoptSessionResponse(
            session_id=session_id,
            instance_id=me,
            adopted=False,
            status="already_local",
        )

    transferred = await _prepare_local_checkout(session, session_id, me)

    updated = await run_sync(database.adopt_session, session_id, me)
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}",
        )

    invalidate_working_dir_cache(session_id=session_id)
    if not transferred:
        await run_sync(apply_sdk_session_keep, updated, me)
    logger.info("Adopted session %s from install %s -> %s", session_id, owner, me)
    return AdoptSessionResponse(
        session_id=session_id,
        instance_id=me,
        adopted=True,
        status="adopted",
    )


# ============================================================================
# Cross-device move ("Move to device…")
# ============================================================================

@router.post("/{session_id}/move", response_model=MoveSessionResponse)
async def move_session_endpoint(
    request: Request,
    session_id: str,
    body: MoveSessionRequest,
    token: str = Depends(require_auth),
):
    """Send this session's future turns to another device — the PUSH-direction
    counterpart of ``/adopt``.

    Adopt is a pull ("resume THIS session HERE"); move is a push ("run this
    session THERE from now on"), so it can be driven from any device without
    sitting at the destination. The destination performs the authenticated
    Resume-here protocol: the source commits and pushes through Git, the target
    syncs its checkout and context, and only then does the leased handoff update
    ownership. A database-only pin would strand the branch and native history.

    Semantics:
      * Target already owns it → no-op, ``moved=False`` / ``already_there``.
      * Unknown target install → 404; target offline (or capability-incapable)
        → 409, since a pin to an unreachable runner just strands the session.
      * Session carries a project whose checkout is not materialized on the
        target → 409, mirroring adopt's base-checkout gate: an unchecked-out
        target would fail on the first send.
      * Actively streaming → 409 (same gate as /adopt). Turn-end persist
        would otherwise restore the old disk's ``sdk_session_id`` after a
        successful move. ``pending_live_run`` stays on the response as
        ``false``.
    """
    session = await require_session(request, session_id)
    return await move_session_to(session_id, session, body.target_instance_id)


async def move_session_to(session_id: str, session: dict, target_instance_id: str) -> MoveSessionResponse:
    """The move itself, shared by the endpoint and an agent's turn-end hand-off.

    Raises ``HTTPException`` with the user-facing reason on every refusal.
    """
    target = (target_instance_id or "").strip()
    if not target:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="target_instance_id is required",
        )
    _refuse_archived({**session, "id": session_id}, "move")

    # Already pinned there — nothing to write.
    if target in (session.get("instance_id"), session.get("owner_device_id")):
        return MoveSessionResponse(
            session_id=session_id,
            instance_id=target,
            moved=False,
            status="already_there",
        )

    install = await run_sync(database.get_install, target)
    if not install:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown device: {target}",
        )

    device = (install or {}).get("device_name") or target

    # Online + capability check, with NO checkout filter — the same validation
    # `resolve_placement` applies to an explicit `runner:<id>` pin.
    from app.core.placement import capable_runners

    if target not in await run_sync(capable_runners, None, {}):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{device} is offline. Bring it online or pick another device."
            ),
        )

    # Project host gate — ask the destination for its active base checkout.
    # Checkout paths are worker-local and never replicated. Capability filtering
    # alone also admits branch-only worktrees, which cannot create a new branch.
    # Projectless sessions skip this. An unresolved project or failed preflight
    # must not bypass the checkout gate.
    if session.get("project_v2_id") or session.get("project_id"):
        from app.core import project_resolver
        from app.network import git as network_git, control, engine

        try:
            project_v2_id = session.get("project_v2_id")
            if not project_v2_id:
                resolved = await run_sync(
                    project_resolver.resolve_project_v2, session
                )
                project_v2_id = (resolved or {}).get("id")
            if not project_v2_id or not await network_git.eligible(target, project_v2_id):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"This session's project isn't checked out on {device} "
                        "yet. Open the project there once (or clone it), then "
                        "try again."
                    ),
                )
        except HTTPException:
            raise
        except (control.RunnerUnreachable, control.ControlRefused, engine.NotEnrolled, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception:
            logger.warning("Move preflight failed for session %s", session_id, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Could not check the destination's project checkout. Try again.",
            )

    from app.core import streaming_status

    if streaming_status.has_active_run(session_id):
        owner_name = (
            await run_sync(_adopt_device_name, session.get("instance_id"))
            or "another device"
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This session is currently running on {owner_name}. "
                "Stop it there first."
            ),
        )

    from app.network import handoff, control, engine
    from app.api.git_helpers import invalidate_working_dir_cache

    owner = session.get("instance_id")
    try:
        moved = await handoff.move(session, target)
    except (control.RunnerUnreachable, control.ControlRefused, engine.NotEnrolled,
            PermissionError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception:
        logger.warning("Move handoff failed for session %s", session_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not confirm the session handoff. Refresh its owner before retrying.",
        )
    invalidate_working_dir_cache(session_id=session_id)

    # Wake the target's claim loop now instead of on its next 5s poll. Purely a
    # latency optimization — the poll is the correctness backstop, so a NOTIFY
    # failure must never fail the move.
    try:
        from app.core.turn_claim import notify_turns_changed

        await run_sync(notify_turns_changed, session_id)
    except Exception:  # noqa: BLE001 — best-effort wake
        logger.warning(
            "Move NOTIFY failed for session %s (poll will pick it up)",
            session_id,
            exc_info=True,
        )

    logger.info(
        "Moved session %s from install %s -> %s",
        session_id,
        owner,
        target,
    )
    return MoveSessionResponse(
        session_id=session_id,
        instance_id=target,
        moved=moved,
        status="moved" if moved else "already_there",
        pending_live_run=False,
    )


# ============================================================================
# Move when this turn ends (#1819)
# ============================================================================

async def _queued_move(session: dict, op: str, target: Optional[str] = None, *, stop: bool = False) -> dict:
    """Run a queued-move operation on the worker that owns ``session``."""
    from app.core import session_handoff
    from app.network import control, engine

    try:
        return await session_handoff.route(session, op, target, stop=stop)
    except (control.RunnerUnreachable, control.ControlRefused, engine.NotEnrolled,
            PermissionError, LookupError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/{session_id}/handoff")
async def queued_move_status(request: Request, session_id: str, token: str = Depends(require_auth)):
    """What keeps this chat from moving now, and any move already queued.

    ``{"busy": {"running": bool, "tasks": [names]}, "pending": {"worker", "target_id", "by"} | null}``,
    read on the owning worker. The worker selector and Resume here ask first:
    a running or parked chat is offered "Move when this turn ends".
    """
    session = await require_session(request, session_id)
    return await _queued_move(session, "status")


@router.post("/{session_id}/handoff")
async def queue_move(request: Request, session_id: str, body: QueueMoveRequest,
                     token: str = Depends(require_auth)):
    """Queue the agent's hand-off for this chat: it moves when its turn and background work end.

    The owner's move waits however long the work runs, until it moves or is
    cancelled. The chat records the queued state, so every device shows it.
    """
    session = await require_session(request, session_id)
    target = (body.target_instance_id or "").strip()
    if not target:
        from app.core.config import settings
        from app.network import engine

        ctx = engine.context()
        target = ctx.device_id if ctx is not None else settings.effective_instance_id
    return await _queued_move(session, "queue", target, stop=body.stop)


@router.delete("/{session_id}/handoff")
async def cancel_queued_move(request: Request, session_id: str, token: str = Depends(require_auth)):
    """Cancel a queued move (the owner's or the agent's)."""
    session = await require_session(request, session_id)
    return await _queued_move(session, "cancel")


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
    active_only: bool = Query(False, description="Show only active sessions"),
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

    # Query + enrichment in ONE thread hop: both are synchronous psycopg2 work,
    # and the enrichment pipeline (6-10 queries) on the event loop would stall
    # every concurrent request on every sidebar poll.
    return await run_sync(
        fetch_and_enrich_sessions,
        database.get_sessions,
        project_id=project_id,
        profile_id=profile_id,
        status=status_filter,
        api_user_id=filter_api_user_id,
        api_users_only=filter_api_users_only,
        active_only=active_only,
        tag_id=tag_id,
        limit=limit,
        offset=offset
    )


@router.get("/paginated", response_model=PaginatedSessions)
async def list_sessions_paginated(
    request: Request,
    project_id: Optional[str] = Query(None, description="Filter by project"),
    profile_id: Optional[str] = Query(None, description="Filter by profile"),
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by status"),
    api_user_id: Optional[str] = Query(None, description="Filter by API user ID (admin only)"),
    admin_only: bool = Query(False, description="Show only admin sessions (no API user)"),
    api_users_only: bool = Query(False, description="Show only API user sessions (exclude admin sessions)"),
    active_only: bool = Query(False, description="Show only active sessions"),
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

    # Query + enrichment in ONE thread hop (see list_sessions).
    sessions, total = await run_sync(
        fetch_and_enrich_sessions,
        database.get_sessions_paginated,
        project_id=project_id,
        profile_id=profile_id,
        status=status_filter,
        api_user_id=filter_api_user_id,
        api_users_only=filter_api_users_only,
        active_only=active_only,
        tag_id=tag_id,
        limit=limit,
        offset=offset
    )

    enriched = sessions
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
    active_only: bool = Query(False, description="Show only active sessions"),
    exclude_archived: bool = Query(False, description="Leave out archived sessions (status 'archived')"),
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

    # Query + enrichment in ONE thread hop (see list_sessions).
    sessions, next_cursor, next_cursor_id, has_more = await run_sync(
        fetch_and_enrich_sessions,
        database.get_sessions_cursor,
        project_id=project_id,
        profile_id=profile_id,
        status=status_filter,
        api_user_id=filter_api_user_id,
        api_users_only=filter_api_users_only,
        active_only=active_only,
        exclude_archived=exclude_archived,
        tag_id=tag_id,
        limit=limit,
        cursor=cursor,
        cursor_id=cursor_id,
    )

    enriched = sessions

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
        profile_id = api_user_profile_filter(api_user, profile_id)
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
        profile_id = api_user_profile_filter(api_user, profile_id)
        filter_api_user_id = api_user["id"]
        admin_only = False
    elif admin_only:
        filter_api_user_id = None

    # Query + enrichment in ONE thread hop (see list_sessions).
    results, total = await run_sync(
        fetch_and_enrich_search_results,
        database.search_sessions_paginated,
        query=q,
        project_id=project_id,
        profile_id=profile_id,
        api_user_id=filter_api_user_id,
        admin_only=admin_only,
        limit=limit,
        offset=offset
    )

    enriched = results
    has_more = offset + len(results) < total

    return PaginatedSessionSearchResults(
        items=enriched,
        total=total,
        offset=offset,
        limit=limit,
        has_more=has_more
    )


def _set_message_pagination_headers(response: Response, pagination: dict) -> None:
    """Publish the transcript window on response headers.

    ``SessionWithMessages`` is a pinned response model, so the window metadata
    rides headers rather than new body fields — additive, and invisible to
    every existing client. ``X-Message-Next-Before-Seq`` is the cursor to pass
    back as ``?before_seq=`` for the next (older) page.
    """
    response.headers["X-Message-Total"] = str(pagination["total"])
    response.headers["X-Message-Returned"] = str(pagination["returned"])
    response.headers["X-Message-Limit"] = str(pagination["limit"])
    response.headers["X-Message-Has-More"] = "true" if pagination["has_more"] else "false"
    if pagination.get("first_seq") is not None:
        response.headers["X-Message-First-Seq"] = str(pagination["first_seq"])
    if pagination.get("next_before_seq") is not None:
        response.headers["X-Message-Next-Before-Seq"] = str(pagination["next_before_seq"])


@router.get("/{session_id}/composer-suggestion")
async def get_composer_suggestion(
    request: Request, session_id: str, token: str = Depends(require_auth),
):
    """Read the latest eligible hint. Never triggers generation or changes a draft."""
    await require_session(request, session_id)
    from app.core.composer_suggestions import _eligible
    from app.db import composer_suggestions

    hint = await run_sync(composer_suggestions.get, session_id)
    if hint and await _eligible(session_id, hint["turn_id"]):
        return hint
    return None


@router.get("/{session_id}", response_model=SessionWithMessages)
async def get_session(
    request: Request,
    session_id: str,
    # Injected by FastAPI (recognised by the annotation, not the default). The
    # default keeps the handler callable directly — several tests and internal
    # callers invoke it as a plain coroutine — in which case the pagination
    # headers are simply not published.
    response: Response = None,
    message_limit: Optional[int] = Query(
        None,
        ge=1,
        le=MAX_MESSAGE_PAGE_LIMIT,
        description=(
            "Max messages to return, newest-first window (default "
            f"{DEFAULT_MESSAGE_PAGE_LIMIT}). See the X-Message-* response headers."
        ),
    ),
    before_seq: Optional[int] = Query(
        None,
        ge=0,
        description=(
            "Keyset cursor: return the window of messages ENDING before this "
            "0-based transcript position. Pass X-Message-Next-Before-Seq from "
            "the previous response to page backwards through older messages."
        ),
    ),
    token: str = Depends(require_auth),
):
    """Get a session with its message history. API users can only access their assigned sessions.

    Messages come back oldest→newest, capped to the newest ``message_limit``
    (default 2000 — above any ordinary transcript, so the default response is
    unchanged for existing clients). ``X-Message-Has-More`` /
    ``X-Message-Next-Before-Seq`` expose whether older messages exist and how
    to fetch them.
    """

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

    if sdk_session_id:
        try:
            from app.core.message_resolver import resolve_working_dir

            # Resolve working dir preferring worktree → project → default.
            # Worktree sessions store JSONL under the worktree's encoded path;
            # falling straight to the project path misses the direct lookup
            # and relies on a cross-dir scan to recover.
            working_dir = await run_sync(resolve_working_dir, dict(session))

            from app.core.provider.fluid_transcript import (
                is_fluid_session,
                fluid_live_messages,
            )

            if await run_sync(is_fluid_session, sdk_session_id):
                # fluid (flag-on) session: the transcript is fluid's node tree, so
                # neither the store parser nor the JSONL parser finds anything
                # and this read would fall through to the legacy-DB rescue and
                # render an empty (or user-rows-only) chat. Serve the node tree
                # through the SAME _transform_entries core, so the rendered
                # shape is identical. Mirrors the /live read path.
                jsonl_messages, usage_data = await run_sync(
                    fluid_live_messages, sdk_session_id, working_dir
                )
            else:
                # Postgres (session_store_entries) is the source of truth. Load
                # the rich transcript from the store and run it through the SAME
                # transform as the old JSONL path (so messages/usage are
                # identical).
                jsonl_messages, usage_data = await run_sync(
                    parse_session_history_via_store, sdk_session_id, working_dir
                )
            if jsonl_messages:
                # Transform (incl. the DB-backed per-message profile
                # resolution) in ONE thread hop — inline on the event loop it
                # would block every concurrent request for the length of the
                # transcript.
                messages = await run_sync(
                    build_detail_messages, session_id, jsonl_messages
                )

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
            store_parse_failed = True

    # The store stays the single source of truth. But an EMPTY result — after
    # the lazy JSONL backfill inside parse_session_history_via_store had its
    # chance — means the transcript predates the cutover and lives ONLY in the
    # legacy session_messages table (shared across installs; e.g. the JSONL
    # sits on another machine). Serve that instead of rendering an existing
    # chat as empty. Post-cutover sessions always have store rows, so this can
    # never shadow the store with the user-rows-only legacy view (the
    # "vanishing replies" trap) — EXCEPT when the store parse RAISED: then "no
    # messages" proves nothing about the store, so serve empty (a transient
    # blank beats a wrong transcript) rather than fall through to the legacy
    # view.
    if not messages and not store_parse_failed:
        if sdk_session_id:
            logger.warning(
                "[Session API] no entries in session_store for "
                "session %s (sdk_session_id=%s) — serving legacy "
                "session_messages transcript.",
                session_id, sdk_session_id,
            )
        messages = await _legacy_db_messages(session_id)

    # Cost/duration + user-row metadata attach, then the keyset window and the
    # display caps (tool inputs, inline screenshots) — all synchronous DB/CPU
    # work, done in ONE thread hop instead of on the event loop.
    messages, message_pagination = await run_sync(
        finalize_transcript,
        session_id,
        messages,
        limit=message_limit,
        before_seq=before_seq,
    )
    if response is not None:
        _set_message_pagination_headers(response, message_pagination)

    from app.db.background_tasks import get_running_task_presence
    presence = await run_sync(get_running_task_presence, [session["id"]])
    session["has_running_tasks"] = presence.get(session["id"], {}).get("has_running_tasks", False)

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

    The least-rich source: type is inferred from the role/tool columns. The
    last-resort read for pre-cutover sessions whose transcript was never
    backfilled into the session store.
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
    include_messages: bool = Query(
        True,
        description=(
            "Set false for a metadata-only snapshot: the transcript is skipped "
            "and `messages` comes back empty."
        ),
    ),
    message_limit: Optional[int] = Query(
        None,
        ge=1,
        le=MAX_MESSAGE_PAGE_LIMIT,
        description=(
            "Max messages to return, newest-first window (default "
            f"{DEFAULT_MESSAGE_PAGE_LIMIT}). The window is described by the "
            "`message_pagination` object on the response."
        ),
    ),
    before_seq: Optional[int] = Query(
        None,
        ge=0,
        description=(
            "Keyset cursor: return the window of messages ENDING before this "
            "0-based transcript position. Pass `message_pagination."
            "next_before_seq` from the previous response to page backwards."
        ),
    ),
    token: str = Depends(require_auth)
):
    """
    Get session state (persistent transcript + lifecycle fields).

    V2: live render deltas ride the session event feed (SSE); this endpoint
    serves the PERSISTED transcript plus lifecycle fields (activeTasks,
    unpark_pending, is_streaming from the durable runs table).

    Response includes:
    - messages: Persisted transcript
    - is_streaming: Whether the session has an active run
    - usage: Token usage counts

    ``include_messages=false`` serves everything EXCEPT the transcript. A v2
    session's transcript is owned by the SSE feed, so the switch-in metadata
    snapshot (session-switcher.ts::applyMetadataSnapshot) only wants the
    lifecycle/usage/worktree fields — building and shipping a multi-megabyte
    message list it throws away is pure cost on every session open. Defaults to
    true, so every existing caller sees the wire shape it always saw.
    """

    # Check session exists and user has access
    session = await require_session(request, session_id)

    # Derived lifecycle snapshot; ``compute`` also logs any cross-source
    # disagreement (pool, idle reader, parked marker) as ``lifecycle-drift``.
    #
    # active_tasks: agent tasks + Monitor shell subprocesses PLUS any
    # recently-failed task still inside the retention window (``get_visible``,
    # the DISPLAY view — running-only control-flow predicates still use
    # ``get_active``). Attached fresh to EVERY local response, so a task
    # finishing/failing is reflected on the very next poll. [] when none.
    #
    # unpark_pending: an unpark follow-up turn is committed but has NOT yet
    # flipped ``is_streaming`` true — OR undrained queued messages exist (the
    # keep-alive channel). Surfaced on every
    # response body so a polling client keeps its park-poll alive across the
    # gap instead of reading a transient (is_streaming=false, activeTasks=[])
    # and STOPPING.
    from app.core.lifecycle_state import compute as compute_lifecycle
    lifecycle = await compute_lifecycle(session_id, session_row=session)
    active_tasks = lifecycle.active_tasks
    unpark_pending = lifecycle.unpark_pending

    # Surfaced on every response body, like the lifecycle flags above.
    v2_stream = bool(session.get("v2_stream"))

    # Compute session metadata from DB session — included in all response
    # paths so the frontend can correct any stale state on switch.
    from app.core.workspace_folders import normalize_workspace_project_ids
    session_meta = {
        "mode": session.get("mode"),
        "project_id": session.get("project_id"),
        "project_v2_id": session.get("project_v2_id"),
        "workspace_project_ids": normalize_workspace_project_ids(session.get("workspace_project_ids")),
        # Hidden workers and older chats need not be in the sidebar's page.
        # Their composer still restores this session's own execution binding.
        "model": session.get("model") or session.get("launched_model"),
        "provider_id": session.get("provider_id"),
        "effort": session.get("effort"),
        # An archived chat opens read-only (no composer) until restored (#1822).
        "status": session.get("status"),
    }

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

    # V2: no in-memory live overlay — the persistent loader chain below is the
    # single transcript source; live render deltas ride the event feed (SSE).

    # Session store first, then the fluid/JSONL fallbacks below.
    sdk_session_id = session.get("sdk_session_id")
    messages = []
    working_dir = "/workspace"
    usage_data = None

    # NOTE: the legacy DB fast path (load_messages_from_db) is deliberately
    # absent here: the legacy `session_messages` table only holds the USER rows
    # — assistant/system turns live exclusively in the session store
    # (session_store_entries). Reading the legacy table first would return a
    # user-only transcript.

    # Primary read: the Postgres session store.
    store_parse_failed = False
    if include_messages and sdk_session_id:
        try:
            from app.core.message_resolver import resolve_working_dir

            # Worktree-aware working dir (worktree → project → default).
            working_dir = await run_sync(resolve_working_dir, dict(session))

            from app.core.provider.fluid_transcript import (
                is_fluid_session,
                fluid_live_messages,
            )

            if await run_sync(is_fluid_session, sdk_session_id):
                # fluid (flag-on) session: the transcript is fluid's node tree,
                # so neither the store parser nor the JSONL parser finds
                # anything and the read fell through to the legacy-DB rescue
                # below ("postgres_only store empty for ... — serving legacy DB
                # transcript"). Serve the node tree, run through the SAME
                # _transform_entries core, so the rendered shape is identical.
                jsonl_messages, usage_data = await run_sync(
                    fluid_live_messages, sdk_session_id, working_dir
                )
            else:
                # Store-backed parser (SAME transform as the old JSONL path).
                jsonl_messages, usage_data = await run_sync(
                    parse_session_history_via_store, sdk_session_id, working_dir
                )
            if jsonl_messages:
                # Transform + per-message profile resolution in ONE thread hop
                # (see GET /{id}).
                messages = await run_sync(
                    build_live_messages, session_id, jsonl_messages
                )

        except Exception as e:
            logger.error(f"Failed to parse transcript for live session {session_id}: {e}")
            # A RAISED store parse is not an EMPTY store — see GET /{id}.
            store_parse_failed = True

    # Legacy rescue: the store yielded nothing even after the
    # lazy JSONL backfill inside parse_session_history_via_store had its
    # chance — a pre-cutover session whose transcript lives ONLY in the legacy
    # session_messages table (shared across installs). Serve that instead of
    # an empty chat. Fires ONLY on an empty store result — NOT on a raised
    # parse (a transient DB error proves nothing about the store; serving the
    # legacy view then would resurface the user-rows-only "vanishing replies"
    # trap). Post-cutover sessions always have store rows, so the legacy view
    # can never shadow the store. Mirrors GET /{id}.
    if include_messages and not messages and not store_parse_failed:
        from app.core.db_message_loader import load_messages_from_db
        try:
            db_messages, has_db = await run_sync(load_messages_from_db, session_id)
            if has_db:
                messages = db_messages
                logger.warning(
                    "Live endpoint: session store empty for %s — serving "
                    "legacy DB transcript (%d messages)",
                    session_id, len(messages),
                )
        except Exception as e:
            logger.warning(f"Legacy DB fallback failed for live {session_id}: {e}")

    # Message post-processing — every step here reads or rewrites the transcript,
    # so all of it is skipped along with the transcript itself.
    message_pagination = paginate_messages([], message_limit)[1]
    if include_messages:
        # Registry reconcile: even with the buffer gone (or never present), a
        # sub-agent still running in the task registry must not render
        # "completed". Force its card back to "running" by matching the launch
        # tool_use id; non-matching cards (legacy transcripts) are left exactly
        # as loaded.
        _reconcile_subagent_status(messages, active_tasks)

        # Cost/duration + user-row metadata attach (so the poller gets cost data
        # on the first poll after streaming ends), then the keyset window and
        # the display caps — ONE thread hop instead of blocking DB work on the
        # event loop for every poll.
        messages, message_pagination = await run_sync(
            finalize_transcript,
            session_id,
            messages,
            limit=message_limit,
            before_seq=before_seq,
        )

    # Build response in live format
    # IMPORTANT: For context window calculation, the frontend needs per-turn
    # input tokens (last API call), NOT cumulative total_tokens_in across all turns.
    # Use last_input_tokens from the sessions row / JSONL, which is the last
    # turn's input_tokens (= context window non-cached portion).
    output_tokens = session.get("total_tokens_out", 0)
    cache_creation = session.get("cache_creation_tokens", 0) or 0
    cache_read = session.get("cache_read_tokens", 0) or 0
    # Per-turn input (last turn), persisted on sessions row by _persist_turn_results
    input_tokens = session.get("last_input_tokens", 0) or 0

    # Override with the transcript read's usage when it produced one.
    # A metadata-only request never parses the transcript, so usage_data is None
    # and the sessions-row columns above stand. That is the intended trade: the
    # only caller passing include_messages=false uses this as a pre-feed
    # placeholder gauge, and the SSE feed's turn.usage supersedes it either way.
    if sdk_session_id and usage_data:
        # Use last_input_tokens (per-turn) for context window calculation,
        # NOT total_tokens_in (cumulative sum across all turns)
        input_tokens = usage_data.get("last_input_tokens", 0)
        output_tokens = usage_data.get("total_tokens_out", output_tokens)
        cache_creation = usage_data.get("cache_creation_tokens", cache_creation)
        cache_read = usage_data.get("cache_read_tokens", cache_read)

    from app.core import streaming_status
    result = {
        "session_id": session_id,
        # V2 busy truth: the durable runs table (stale leases self-heal via TTL).
        "is_streaming": await run_sync(streaming_status.has_active_run, session_id),
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
        "v2_stream": v2_stream,
        # Transcript window. Additive: `messages` still carries the newest
        # page (the whole transcript for any ordinary session), and this says
        # whether older messages exist and how to ask for them.
        "message_pagination": message_pagination,
        "session_meta": session_meta,
    }

    # Live ProviderAgent worker attribution: per-turn worker token/spend grouped by the worker's PROVIDER, while
    # the turn is still running. Attached ONLY when workers are actually live,
    # so the pinned /live wire shape is unchanged for every ordinary session.
    try:
        from app.core import provider_agent_supervisor

        if provider_agent_supervisor.has_live_workers(session_id):
            result["providerWorkers"] = provider_agent_supervisor.attribution(session_id)
    except Exception:
        logger.debug("provider worker attribution failed", exc_info=True)

    return result


@router.put("/{session_id}/workspace-folders")
async def update_workspace_folders(
    request: Request,
    session_id: str,
    body: WorkspaceFoldersUpdate,
    token: str = Depends(require_auth),
):
    """Save only this chat's additional workspace projects."""
    from app.api.common import check_project_access
    from app.core.workspace_folders import normalize_workspace_project_ids

    existing = await require_session(request, session_id)
    if existing.get("mode") == "chat":
        raise HTTPException(400, "Workspace folders require a code or voice session")
    try:
        ids = normalize_workspace_project_ids(body.workspace_project_ids)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    ids = [item for item in ids if item != existing.get("project_id")]
    previous_ids = normalize_workspace_project_ids(existing.get("workspace_project_ids"))
    for project_id in ids:
        check_project_access(request, project_id)
        # Retaining an unavailable entry must not prevent removing another one.
        if project_id not in previous_ids and not await run_sync(database.get_project, project_id):
            raise HTTPException(404, f"Workspace project '{project_id}' is no longer available")
    await run_sync(database.set_session_run_binding, session_id, workspace_project_ids=ids)
    return {"workspace_project_ids": ids}


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
    if title and sdk_sid:
        try:
            sdk_rename_session(session_id=sdk_sid, title=title)
            logger.debug(f"Renamed SDK session {sdk_sid} to '{title}'")
        except Exception as e:
            logger.warning(f"Failed to rename SDK session {sdk_sid}: {e}")

    session = await run_sync(
        database.update_session,
        session_id=session_id,
        title=title,
        status=session_status,
    )

    return session


@router.get("/{session_id}/context-usage")
async def get_session_context_usage(
    request: Request,
    session_id: str,
    token: str = Depends(require_auth)
):
    """Get the live context-window usage breakdown for a session.

    Live path (session connected in the local pool, control call succeeds):
    ``{"available": True, "source": "live", "usage": <ContextUsageResponse>}``
    with the adapter's breakdown passed through verbatim.

    Persisted fallback applies ONLY when the session is not connected: the last
    breakdown persisted at turn end is served from ``sessions.context_usage_json``
    as ``{"available": True, "source": "persisted", "captured_at": <iso>,
    "usage": {...}}`` — the same ``usage`` shape so the frontend renders it
    identically. When no snapshot exists the response is
    ``{"available": False, "reason": "not_connected"}``.

    A CONNECTED session whose live call times out or is unsupported returns a
    plain ``{"available": False, "reason": "timeout"|"unsupported"}`` — it does
    NOT fall back to the persisted snapshot. A connected-but-slow session's
    persisted snapshot is stale relative to the turn in flight, so the frontend
    must fall back to its own live estimate rather than a stale snapshot
    mislabeled as the current window.
    """
    await require_session(request, session_id)

    async def _persisted_or_reason(reason: str) -> dict:
        """Serve the persisted snapshot, else the original unavailable reason."""
        try:
            snapshot = await run_sync(database.get_session_context_usage_snapshot, session_id)
        except Exception:
            snapshot = None
        if snapshot and snapshot.get("data") is not None:
            return {
                "available": True,
                "source": "persisted",
                "captured_at": snapshot.get("captured_at"),
                "usage": snapshot["data"],
            }
        return {"available": False, "reason": reason}

    async with api_error_handler("get context usage"):
        from app.core.session_pool import get_session_pool
        from app.core.query_persistence import shielded_get_context_usage

        # peek(): non-spawning, no LRU promotion. Returns None for parked,
        # never-opened, or remote-install sessions.
        state = get_session_pool().peek(session_id)
        if not state or not state.is_connected:
            return await _persisted_or_reason("not_connected")

        # Bounded, shielded live control call (shared with turn-end capture).
        # A timeout/unsupported result on a CONNECTED session returns a plain
        # reason — no persisted fallback — so the frontend uses its live estimate.
        try:
            result = await shielded_get_context_usage(state.adapter)
        except asyncio.TimeoutError:
            return {"available": False, "reason": "timeout"}

        if result is None:
            return {"available": False, "reason": "unsupported"}
        return {"available": True, "source": "live", "usage": result}


@router.post("/{session_id}/tasks/{task_id}/stop")
async def stop_session_background_task(
    request: Request,
    session_id: str,
    task_id: str,
    token: str = Depends(require_auth),
):
    """Stop ONE in-flight background task (sub-agent or Monitor shell) by id.

    Powers the "x" on the live background-tasks strip. Shell tasks are
    terminated directly; a provider-lane sub-agent is torn down by its
    supervisor, and any other sub-agent (the harness has no per-task stop) has
    its registry row marked killed, so the task drops from the next /live poll.
    Returns ``{"ok": bool, ...}`` — a failed stop is a 200 with ok=False
    (a shell whose processes survived) rather than an error; a task that
    already finished is ok=True with ``detail: "already finished"``.
    """
    await require_session(request, session_id)

    from app.core.query_engine import stop_session_task

    async with api_error_handler("stop background task"):
        result = await stop_session_task(session_id, task_id)
        return {"session_id": session_id, "task_id": task_id, **result}


async def _cleanup_session_artifacts(session_id: str, sdk_sid: Optional[str], project_id: Optional[str]):
    """Background cleanup: stop workers, terminate the pool, and delete SDK JSONL.

    Runs after the HTTP response returns so clients don't block on subprocess
    teardown (which can hold worktree CWD locks and take time to exit).
    """
    try:
        from app.core import provider_agent_supervisor

        await provider_agent_supervisor.stop_all_workers(session_id)
    except Exception as e:
        logger.warning(f"Background worker cleanup failed for {session_id}: {e}")

    try:
        from app.core.session_pool import get_session_pool
        await get_session_pool().remove(session_id)
    except Exception as e:
        logger.warning(f"Background session_pool.remove failed for {session_id}: {e}")

    if sdk_sid:
        # Purge the Postgres session store: sessions has NO FK into
        # session_store_entries/_summaries, so skipping this leaks the deleted
        # session's full transcript forever. session_id-only match —
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

    from app.core.provider.fluid_transcript import (
        delete_fluid_session_data,
        get_cli_session_id,
        looks_like_fluid_session_id,
    )

    if sdk_sid and looks_like_fluid_session_id(sdk_sid):
        # A fluid-native transcript is NOT a ~/.claude JSONL: it lives in the
        # fluid_* tables, plus — for a session that ever ran a delegated turn or
        # was re-seeded — a real CLI JSONL named by fluid_sessions.cli_session_id.
        # The SDK delete validates a UUID, so it may only ever be handed cli_sid;
        # the ses_ id stays a SQL bind parameter and never builds a path.
        try:
            # Read the handle first — it lives on the row the purge drops.
            cli_sid = await run_sync(get_cli_session_id, sdk_sid)
            removed = await run_sync(delete_fluid_session_data, sdk_sid)
            if removed:
                logger.debug(f"Deleted {removed} fluid transcript rows for {sdk_sid}")

            if cli_sid and sdk_delete_session:
                try:
                    project = await run_sync(database.get_project, project_id) if project_id else None
                    working_dir = get_project_working_dir(project) if project else None
                    await run_sync(sdk_delete_session, session_id=cli_sid, directory=working_dir)
                    logger.debug(f"Deleted delegated CLI JSONL {cli_sid} for {sdk_sid}")
                except FileNotFoundError:
                    pass  # already gone — nothing to clean up
                except Exception as e:
                    logger.warning(f"Failed to delete delegated CLI JSONL {cli_sid}: {e}")
        except Exception as e:
            # Non-fatal — but the transcript rows stay orphaned until a future
            # delete retries, so make the leak visible.
            logger.warning(f"Failed to delete fluid transcript for {sdk_sid}: {e}")
    elif sdk_sid and sdk_delete_session:
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
    from app.core import browser_runtime
    await browser_runtime.close_session(session_id)

    # Fan the deletion out to the user's other devices/tabs so their sidebars
    # drop the row and an open view of this session leaves it — without this a
    # second tab keeps rendering the deleted transcript. Best-effort: the
    # caller already sees the row gone via its own 204.
    try:
        from app.core.sync_engine import sync_engine
        await sync_engine.broadcast_user_session_deleted(
            user_id=existing.get("api_user_id") or "admin",
            session_id=session_id,
            source_device_id=None,
        )
    except Exception:  # noqa: BLE001 — sync fan-out is best-effort
        logger.debug("session delete broadcast failed", exc_info=True)

    # Defer slow cleanup (subprocess termination, JSONL file I/O) to background.
    background_tasks.add_task(
        _cleanup_session_artifacts,
        session_id,
        existing.get("sdk_session_id"),
        existing.get("project_id"),
    )


@router.post("/{session_id}/clear", status_code=status.HTTP_200_OK)
async def clear_session(
    request: Request,
    session_id: str,
    background_tasks: BackgroundTasks,
    token: str = Depends(require_auth),
):
    """Clear a session's conversation IN PLACE — wipe its transcript/messages but
    KEEP the session row.

    This backs the "Clear session" row action: the session (id / title / project /
    worktree link) survives, but its conversation is reset to empty so the user
    stays on the same session and the next prompt starts a fresh SDK conversation.
    Contrast with :func:`delete_session`, which removes the row entirely.

    The DB reset (unlink sdk_session_id, wipe transcript store + message rows,
    zero counters) runs synchronously so the client's reload is immediately empty.
    Slow cleanup (pooled subprocess teardown + SDK JSONL file removal for the
    now-orphaned old transcript) is deferred to a background task.
    """
    existing = await require_session(request, session_id)

    from app.core import streaming_status

    # Refuse to clear a session that is actively streaming. Wiping the DB out
    # from under an in-flight turn corrupts the transcript (the recorder keeps
    # appending events for the old conversation) and races the reap below. The
    # frontend surfaces this 409 as a "stop the turn first" toast. Busy truth
    # is the durable runs table (shared database — covers a turn live on
    # another install too).
    if streaming_status.has_active_run(session_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This session is currently responding. Stop the turn before "
                "clearing the conversation."
            ),
        )

    # Authoritative, synchronous reset — returns the OLD sdk_session_id (now
    # unlinked) so its subprocess + JSONL can be cleaned in the background.
    old_sdk_sid = await run_sync(database.clear_session_conversation, session_id)

    # Reap this session's background work (its conversation was just wiped, so
    # any tracked task can never meaningfully complete): flip still-"running"
    # task_registry rows to killed so the taskbar surfaces the failure instead
    # of showing a phantom active task forever, drop the live bucket, and reap
    # any Monitor shell subprocesses the old conversation spawned.
    try:
        from app.core.task_registry import task_registry
        for rec in task_registry.get_active(session_id):
            task_registry.complete_task(session_id, rec["id"], "killed")
        task_registry.clear_session(session_id)
    except Exception:
        logger.debug("clear-session task reap failed", exc_info=True)
    try:
        from app.mcp.builtin_tools import reap_session_background_shells
        reap_session_background_shells(session_id)
    except Exception:
        logger.debug("clear-session shell reap scheduling failed", exc_info=True)

    # Fan the wipe out to every one of the user's devices/tabs. The clear
    # appends NOTHING to the session's event log — it rewrites the log out
    # from under any connected feed's resume cursor — so without this
    # broadcast a tab with the session open keeps rendering the deleted
    # transcript (and its cached copy survives switching away and back).
    # Best-effort: the caller's own store invalidates locally on the 200.
    try:
        from app.core.sync_engine import sync_engine
        await sync_engine.broadcast_user_session_cleared(
            user_id=existing.get("api_user_id") or "admin",
            session_id=session_id,
            source_device_id=None,
        )
    except Exception:  # noqa: BLE001 — sync fan-out is best-effort
        logger.debug("session clear broadcast failed", exc_info=True)

    background_tasks.add_task(
        _cleanup_session_artifacts,
        session_id,
        old_sdk_sid,
        existing.get("project_id"),
    )

    return {"status": "ok", "session_id": session_id}


@router.post("/{session_id}/resume", status_code=status.HTTP_200_OK)
async def resume_session(
    request: Request,
    session_id: str,
    if_busy: str = Query(
        "queue",
        pattern="^(queue|conflict)$",
        description=(
            "What to do when the session already has an unsettled turn. "
            "'queue' (default) enqueues the resume behind it; 'conflict' "
            "restores the old 409 refusal for a caller that must not have a "
            "resume run later."
        ),
    ),
    token: str = Depends(require_auth),
):
    """Re-run the last turn from the session head without adding a message.

    This backs the Retry button on an exhausted provider-retry card. The failed
    turn left the head on a user node the model never answered — the prompt, or
    the tool results it was about to read — so the engine picks the loop back up
    from exactly that node. Nothing is appended: no ``message.user`` row, and
    the request the provider sees is the one that failed, so the prefix cache
    survives.

    A busy session queues the resume durably (``if_busy=queue``, the default);
    ``if_busy=conflict`` refuses with 409 instead. A resume already queued for
    this session is returned as-is rather than enqueued twice.

    Only fluid sessions can do this. The Claude CLI owns its own transcript and
    has no head node to hand back, so a non-fluid session is refused outright.
    """
    existing = await require_session(request, session_id)
    _refuse_archived(existing, "resume")

    from app.core import turn_manager, turn_claim
    from app.db import turns as turns_db

    # ONE atomic read, not separate live/queued checks: a turn flipping
    # queued→running between two reads is invisible to both, which here
    # would leave the caller believing the resume starts now when it is
    # really queued.
    busy = await run_sync(turns_db.session_has_unsettled_turns, session_id)
    if busy and if_busy == "conflict":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a turn is already running",
        )

    sdk_session_id = existing.get("sdk_session_id") or ""
    if not sdk_session_id.startswith("ses_"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="resume needs a fluid session",
        )

    if busy:
        # Already one resume waiting? Hand back that turn instead of stacking a
        # second. A resume carries no prompt, so two of them are the same
        # request — and re-running the head twice is exactly the duplicate the
        # old 409 existed to prevent.
        queued = await run_sync(turns_db.get_queued_turns, session_id)
        for row in queued or []:
            if (row or {}).get("source") == "resume":
                return {
                    "turn_id": row.get("id"),
                    "session_id": session_id,
                    "queued": True,
                    "duplicate": True,
                }

    payload: dict = {}
    api_user = get_api_user_from_request(request)
    if api_user:
        payload["api_user_id"] = api_user["id"]

    profile_id = existing.get("profile_id")
    # Off-loop twin of ``submit``: the durable queue writes ride one thread hop
    # (``no_kick`` is unchanged — the NOTIFY below is the whole hand-off).
    turn_id = await turn_manager.submit_async(
        session_id,
        "resume",
        prompt="",
        # The 'claude-code' no-profile sentinel is install-local and must never
        # ride a turn another install executes.
        profile_id=None if profile_id == "claude-code" else profile_id,
        project_id=existing.get("project_id"),
        payload=payload or None,
        no_kick=True,
    )
    # The session may be placed on the worker; the durable row plus this NOTIFY
    # is the whole hand-off, exactly as the worktree submit seam does it.
    await run_sync(turn_claim.notify_turns_changed, session_id)

    return {"turn_id": turn_id, "session_id": session_id, "queued": bool(busy)}


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
            from app.core import browser_runtime
            await browser_runtime.close_session(session_id)
            # Same fan-out as the single delete: other devices drop the row
            # and leave the session if they have it open. Best-effort.
            try:
                from app.core.sync_engine import sync_engine
                await sync_engine.broadcast_user_session_deleted(
                    user_id=existing.get("api_user_id") or "admin",
                    session_id=session_id,
                    source_device_id=None,
                )
            except Exception:  # noqa: BLE001 — sync fan-out is best-effort
                logger.debug("session delete broadcast failed", exc_info=True)
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
    """Archive a session. API users can only archive their accessible sessions.

    Kept for older clients; the sidebar uses ``PATCH /{id}/active`` with
    ``archived`` so a session owned by another worker archives on its owner.
    """
    await set_session_active(request, session_id, SessionActiveUpdate(is_active=False, archived=True), token)
    return {"status": "ok", "message": "Session archived"}


@router.patch("/{session_id}/active", response_model=Session)
async def set_session_active(
    request: Request,
    session_id: str,
    payload: SessionActiveUpdate,
    token: str = Depends(require_auth),
):
    """Set the active status of a session. Returns the updated session."""
    existing = await require_session(request, session_id)

    from app.network import control, session_active
    try:
        return await session_active.update_membership(existing, payload.is_active, payload.archived)
    except session_active.MembershipUpdateFailed as exc:
        raise HTTPException(500, str(exc)) from exc
    except control.RunnerUnreachable as exc:
        raise HTTPException(503, str(exc)) from exc
    except (control.ControlRefused, PermissionError) as exc:
        raise HTTPException(409, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


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
    await require_session(request, session_id)

    changes = await run_sync(database.get_sync_logs, session_id, since_id=since_id)
    latest_id = await run_sync(database.get_latest_sync_id, session_id)

    # Check if streaming — durable runs-table busy read (import here to avoid
    # circular import)
    from app.core.sync_engine import sync_engine
    from app.core import streaming_status
    is_streaming = streaming_status.has_active_run(session_id)

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
    including whether work is in progress.

    Returns:
        - session_id: The session ID
        - title: Session title
        - status: Session status (active, archived, etc.)
        - is_streaming: Whether work is currently in progress
        - connected_devices: Number of devices watching this session
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
    }
