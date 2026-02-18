"""
Internal API endpoints

Endpoints used internally by the application (e.g., git hook callbacks).
These are not intended for external consumption.
"""

import asyncio
import logging
import os
from typing import Optional

from fastapi import APIRouter, Request, Response, HTTPException, status
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/internal", tags=["Internal"])


class GitHookCallbackRequest(BaseModel):
    """Request body from a git hook callback."""
    hook: str
    repo: Optional[str] = None


@router.post("/git-hook", status_code=202)
async def git_hook_callback(request: Request, body: GitHookCallbackRequest):
    """Receive callbacks from git hooks installed in monitored repos.

    This endpoint is called by POSIX shell hook scripts installed by
    GitTracker. It validates the token, maps the repo path to a
    project, and dispatches immediate state updates.

    Returns 202 Accepted immediately to never block git operations.
    """
    from app.core.git_tracker import git_tracker

    # Verify token
    token = request.headers.get("X-Hook-Token", "")
    if not git_tracker.verify_hook_token(token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid hook token",
        )

    if not body.repo:
        return Response(status_code=202)

    repo_path = os.path.normpath(body.repo)

    # Dispatch processing asynchronously (don't block the response)
    asyncio.ensure_future(git_tracker.on_hook(body.hook, repo_path))

    return Response(status_code=202)
