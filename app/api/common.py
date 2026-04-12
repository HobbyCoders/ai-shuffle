"""
Shared API helper functions.

Consolidates duplicated utility functions that were previously copy-pasted
across multiple API modules (generated_images, generated_videos, shared_files,
projects, github, git, project_agents, mcp, project_plugins, hooks).
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional

from fastapi import HTTPException, Request, status

from app.core.config import settings
from app.core.project_utils import (
    get_project_working_dir_as_path,
    get_worktree_working_dir,
)
from app.db import database
from app.api.auth import get_api_user_from_request

logger = logging.getLogger(__name__)


# =============================================================================
# Path Safety
# =============================================================================

def is_path_safe(file_path: Path, base_path: Path) -> bool:
    """
    Check if a file path is safely within the base path.
    Prevents path traversal attacks.
    """
    try:
        resolved_file = file_path.resolve()
        resolved_base = base_path.resolve()
        return resolved_file.is_relative_to(resolved_base)
    except (ValueError, RuntimeError):
        return False


# =============================================================================
# File Utilities
# =============================================================================

def format_file_size(size_bytes: int) -> str:
    """Format file size in human-readable format."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"


# =============================================================================
# Project Access & Path Resolution
# =============================================================================

def check_project_access(request: Request, project_id: str) -> None:
    """
    Check if the requester has access to the project.
    Raises HTTPException(403) if an API user is restricted to a different project.
    """
    api_user = get_api_user_from_request(request)
    if api_user and api_user.get("project_id"):
        if api_user["project_id"] != project_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this project"
            )


def validate_project_access(request: Request, project_id: str) -> dict:
    """
    Validate that the requester has access to the project AND that it exists.

    Returns the project dict if valid. Raises HTTPException(403) for access
    violations, HTTPException(404) if project doesn't exist.
    """
    check_project_access(request, project_id)

    project = database.get_project(project_id)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project not found: {project_id}"
        )

    return project


def get_project_path_from_dict(project: dict) -> Path:
    """Get the resolved filesystem path for a project dict.

    .. deprecated:: Use :func:`resolve_project_path` instead.
    """
    return get_project_working_dir_as_path(project)


def resolve_project_path(
    project_id: Optional[str] = None,
    *,
    request: Optional[Request] = None,
    check_access: bool = True,
    allow_missing: bool = False,
    check_exists: bool = False,
    worktree_id: Optional[str] = None,
    fallback_to_workspace: bool = False,
) -> Optional[Path]:
    """Resolve a project (or worktree) identifier to a filesystem path.

    This is the single entry-point for "project ID -> filesystem path"
    resolution.  It replaces the four per-module helpers that previously
    duplicated this logic.

    Args:
        project_id: The project identifier.  May be *None* when
            *fallback_to_workspace* is True.
        request: The incoming HTTP request.  When provided together with
            *check_access=True*, verifies that the caller is allowed to
            access the project (raises 403 otherwise).
        check_access: Whether to perform the access check.  Ignored when
            *request* is None.
        allow_missing: If True, return None instead of raising 404 when
            the project doesn't exist in the database.
        check_exists: If True, verify that the resolved directory exists
            on disk and raise 404 if it doesn't.
        worktree_id: When provided, resolve the worktree path instead of
            the main project path.
        fallback_to_workspace: When True **and** neither *worktree_id*
            nor *project_id* resolve to a path, fall back to
            ``settings.workspace_dir``.

    Returns:
        A :class:`~pathlib.Path` pointing at the resolved directory, or
        None when *allow_missing* is True and the project does not exist.

    Raises:
        HTTPException(403): Access denied (only when *check_access* and
            *request* are both provided).
        HTTPException(404): Project/worktree/directory not found.
    """
    # --- worktree shortcut ---------------------------------------------------
    if worktree_id:
        worktree = database.get_worktree(worktree_id)
        if worktree:
            return Path(get_worktree_working_dir(worktree))
        # If worktree not found, fall through to project resolution.

    # --- access check ---------------------------------------------------------
    if request is not None and check_access and project_id:
        check_project_access(request, project_id)

    # --- project lookup -------------------------------------------------------
    if project_id:
        project = database.get_project(project_id)
        if not project:
            if allow_missing:
                return None
            if fallback_to_workspace:
                return Path(str(settings.workspace_dir))
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Project not found: {project_id}",
            )

        resolved = get_project_working_dir_as_path(project)

        if check_exists and not resolved.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Project directory not found: {project_id}",
            )

        return resolved

    # --- fallback -------------------------------------------------------------
    if fallback_to_workspace:
        return Path(str(settings.workspace_dir))

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="No project_id provided",
    )


# =============================================================================
# Error Handling
# =============================================================================

@asynccontextmanager
async def api_error_handler(
    action: str,
    status_code: int = 500
) -> AsyncIterator[None]:
    """
    Context manager for consistent API error handling.

    Catches non-HTTP exceptions, logs them, and raises a standardized
    HTTPException. Existing HTTPExceptions pass through unchanged.

    Usage:
        async with api_error_handler("tag session"):
            sdk_tag_session(session_id=sdk_sid, tag=tag)
            return {"status": "ok"}
    """
    try:
        yield
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to {action}: {e}")
        raise HTTPException(
            status_code=status_code,
            detail=f"Failed to {action}"
        ) from e
