"""
Worktree session creation endpoint.

Extracted from sessions.py for maintainability. The sub-router is
included in the main sessions router for backward compatibility.
"""

from fastapi import APIRouter, HTTPException, Depends, status, Request

from app.db import database
from app.db.async_database import run_sync
from app.api.auth import require_auth, get_api_user_from_request
from app.api.sessions_helpers import (
    CreateWorktreeSessionRequest,
    WorktreeSessionResponse,
)

import logging
logger = logging.getLogger(__name__)

# Sub-router — included by the main sessions router (no prefix needed)
worktree_router = APIRouter(tags=["Sessions"])


@worktree_router.post("/with-worktree", response_model=WorktreeSessionResponse)
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

    # Broadcast to all connected devices so sidebars update immediately.
    # Without this, sessions created via REST (not WS) never appear in
    # the sidebar until the user manually refreshes.
    try:
        from app.core.sync_engine import sync_engine
        import asyncio
        asyncio.ensure_future(sync_engine.broadcast_user_session_created(
            user_id="admin",
            session_id=session["id"],
            source_device_id=None
        ))
    except Exception as e:
        logger.warning(f"Failed to broadcast session creation: {e}")

    return WorktreeSessionResponse(
        session_id=session["id"],
        worktree_id=worktree["id"],
        branch_name=worktree["branch_name"],
        base_branch=worktree.get("base_branch") or body.base_branch,
        worktree_path=worktree["worktree_path"],
        status="active"
    )
