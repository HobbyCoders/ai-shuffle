"""
Internal API endpoints

Endpoints used internally by the application (e.g., git hook callbacks).
These are not intended for external consumption.
"""

import asyncio
import logging
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
    GitHookManager. It validates the token, maps the repo path to a
    project, and dispatches immediate state updates.

    Returns 202 Accepted immediately to never block git operations.
    """
    from app.core.git_hook_manager import git_hook_manager

    # Verify token
    token = request.headers.get("X-Hook-Token", "")
    if not git_hook_manager.verify_hook_token(token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid hook token",
        )

    hook_name = body.hook
    repo_path = body.repo

    if not repo_path:
        return Response(status_code=202)

    # Normalize the repo path for consistent lookup
    import os
    repo_path = os.path.normpath(repo_path)

    # Dispatch processing asynchronously (don't block the response)
    asyncio.ensure_future(_process_hook(hook_name, repo_path))

    return Response(status_code=202)


async def _process_hook(hook_name: str, repo_path: str):
    """Process a git hook event asynchronously."""
    try:
        from app.core.git_watcher import git_watcher

        await git_watcher.process_hook_event(hook_name, repo_path)
    except Exception as e:
        logger.debug(f"Error processing git hook {hook_name} for {repo_path}: {e}")
