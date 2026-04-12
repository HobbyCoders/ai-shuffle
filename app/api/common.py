"""
Shared API helper functions.

Consolidates duplicated utility functions that were previously copy-pasted
across multiple API modules (generated_images, generated_videos, shared_files,
projects, github, git, project_agents, mcp, project_plugins, hooks).
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import HTTPException, Request, status

from app.core.project_utils import get_project_working_dir_as_path
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
    """Get the resolved filesystem path for a project dict."""
    return get_project_working_dir_as_path(project)


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
