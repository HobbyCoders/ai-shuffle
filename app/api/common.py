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
    get_worktree_working_dir_as_path,
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


# ---------------------------------------------------------------------------
# Served-file roots
#
# Chat download links (/api/files/by-path and the media by-path routes) must
# reach files anywhere the app legitimately works: the shared workspace AND
# every registered project / worktree checkout, which in local mode can live
# anywhere on disk. Everything else stays rejected.
# ---------------------------------------------------------------------------

# Registered roots change rarely (project added, worktree created), but a
# media-heavy chat page can fire dozens of by-path requests at once - cache
# the root set briefly so each request does not re-scan the DB.
_SERVED_ROOTS_TTL_SECONDS = 5.0
_served_roots_cache = None


def reset_served_roots_cache() -> None:
    """Drop the cached served-roots set (used by tests)."""
    global _served_roots_cache
    _served_roots_cache = None


def _registered_serve_roots() -> tuple:
    """All registered project/worktree directories, cached briefly.

    Never raises: a DB hiccup must fail closed (workspace-only serving), not
    take the file endpoints down.
    """
    global _served_roots_cache
    import time

    now = time.monotonic()
    if _served_roots_cache is not None and now - _served_roots_cache[0] < _SERVED_ROOTS_TTL_SECONDS:
        return _served_roots_cache[1]

    roots = []
    try:
        for project in database.get_all_projects():
            try:
                roots.append(get_project_working_dir_as_path(project))
            except Exception:  # noqa: BLE001 - one bad row must not drop the rest
                logger.debug("Skipping unresolvable project path", exc_info=True)
        for repository in database.get_all_git_repositories():
            for worktree in database.get_worktrees_for_repository(repository["id"]):
                try:
                    roots.append(get_worktree_working_dir_as_path(worktree))
                except Exception:  # noqa: BLE001
                    logger.debug("Skipping unresolvable worktree path", exc_info=True)
    except Exception:  # noqa: BLE001 - fail closed: serve from workspace only
        logger.warning("Could not enumerate served roots; serving workspace files only", exc_info=True)
        return ()

    _served_roots_cache = (now, tuple(roots))
    return _served_roots_cache[1]


def is_path_served(file_path: Path, workspace: Path) -> bool:
    """
    Check if a file may be served over the by-path endpoints.

    Safe when the file resolves inside the workspace OR inside any registered
    project/worktree directory.
    """
    if is_path_safe(file_path, workspace):
        return True
    return any(is_path_safe(file_path, root) for root in _registered_serve_roots())


def require_file_access(file_path: Path, workspace: Path, request: Optional[Request]) -> None:
    """Authorize a served path against the caller's project, including checkouts.

    Unscoped authenticated callers keep the existing served-root behavior.
    Scoped API users never inherit the global workspace or another project's roots.
    The caller offloads this synchronous DB/path work with ``run_sync``.
    """
    api_user = get_api_user_from_request(request) if request is not None else None
    project_id = (api_user or {}).get("project_id")
    if not project_id:
        allowed = is_path_served(file_path, workspace)
    else:
        allowed = False
        try:
            project = database.get_project(project_id)
            if project:
                roots = [get_project_working_dir_as_path(project)]
                repo = database.get_git_repository_by_project(project_id)
                if repo:
                    roots.extend(
                        get_worktree_working_dir_as_path(row)
                        for row in database.get_worktrees_for_repository(repo["id"])
                    )
                from app.core import project_resolver
                from app.db import checkouts, projects_v2
                origin = project_resolver.origin_for_legacy_project(project)
                logical = projects_v2.get_project_v2_by_origin(origin)
                if logical:
                    roots.extend(
                        Path(row["path"])
                        for row in checkouts.list_checkouts(logical["id"])
                        if row.get("runner_id") in (settings.effective_instance_id, "")
                        and row.get("path")
                    )
                allowed = any(is_path_safe(file_path, root) for root in roots)
        except Exception:
            logger.warning("Could not resolve scoped file roots; denying access", exc_info=True)
    if not allowed:
        raise HTTPException(status_code=403, detail="Access denied: file is outside workspace")


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

XOR_PROJECT_IDS_DETAIL = "pass exactly one of project_id or project_v2_id"
ATTACHMENTS_NEED_LOCAL_CHECKOUT = (
    "Attachments need a local checkout of this project. Clone it here, or send without files."
)


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


def require_origin_access(
    *,
    request: Optional[Request] = None,
    api_user: Optional[dict] = None,
    project_id: Optional[str] = None,
    project_v2_id: Optional[str] = None,
) -> dict:
    """XOR project identity + origin-based access for scoped API users.

    Takes a FastAPI ``request`` **or** an ``api_user`` dict (the WS handler has
    no Request). XOR 400 is opt-in to this helper — callers that allow
    projectless create (WS ``handle_query``) must not call it when both ids
    are absent.

    Returns ``{"origin", "project_v2", "legacy"}``. ``legacy`` is the local
    ``projects`` row when ``project_id`` was given, else None (remote-only
    compose by ``project_v2_id``).
    """
    from app.core import project_resolver
    from app.db import projects_v2 as projects_v2_db

    project_id = (project_id or "").strip() or None
    project_v2_id = (project_v2_id or "").strip() or None
    if bool(project_id) == bool(project_v2_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=XOR_PROJECT_IDS_DETAIL,
        )

    legacy = None
    pv2 = None
    origin: Optional[str] = None

    if project_id:
        legacy = database.get_project(project_id)
        if not legacy:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Project not found: {project_id}",
            )
        origin = project_resolver.origin_for_legacy_project(legacy)
        # Self-heal / self-seed the same way git-state and with-worktree did
        # before this helper — a missing projects_v2 row is not a 404 here.
        pv2 = project_resolver.resolve_project_v2({"project_id": project_id})
    else:
        pv2 = projects_v2_db.get_project_v2(project_v2_id)
        if not pv2:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Project not found: {project_v2_id}",
            )
        origin = pv2.get("origin")

    user = api_user
    if user is None and request is not None:
        user = get_api_user_from_request(request)
    if user and user.get("project_id"):
        assigned = database.get_project(user["project_id"])
        if not assigned or project_resolver.origin_for_legacy_project(assigned) != origin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this project",
            )
    return {"origin": origin, "project_v2": pv2, "legacy": legacy}


def assigned_origin(api_user: Optional[dict]) -> Optional[str]:
    """Origin of a scoped API user's project, or None if unscoped/admin.

    ``None`` means no restriction (unscoped / admin). A scoped user whose
    assigned ``projects`` row is gone is **not** unscoped — this raises 403,
    same as :func:`require_origin_access`. Catalog (PR 8) filters
    ``GET /cluster/projects`` with this and must not treat a deleted assigned
    project as “list every origin”. Do not call
    :func:`require_origin_access` there (both ids absent would 400).
    """
    if not api_user or not api_user.get("project_id"):
        return None
    assigned = database.get_project(api_user["project_id"])
    if not assigned:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied to this project",
        )
    from app.core.project_resolver import origin_for_legacy_project
    return origin_for_legacy_project(assigned)


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
