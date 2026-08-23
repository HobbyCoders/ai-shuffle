"""
Project management API routes
"""

import logging
import shutil
import uuid
from datetime import datetime
from typing import List, Optional
from pathlib import Path

from fastapi import APIRouter, HTTPException, Depends, status, UploadFile, File, Request, Query, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.core.models import Project, ProjectCreate, ProjectUpdate
from app.core.config import settings
from app.core.project_utils import get_project_working_dir, get_project_working_dir_as_path, get_worktree_working_dir, is_absolute_project_path
from app.core.platform import is_local_mode
from app.db import database
from app.api.auth import require_auth, require_admin, get_api_user_from_request
from app.api.common import check_project_access, format_file_size, api_error_handler
from app.core.project_links import is_link_locked

logger = logging.getLogger(__name__)


# File operation request models
class CreateFolderRequest(BaseModel):
    name: str


class RenameRequest(BaseModel):
    new_name: str


class ProjectLinkRequest(BaseModel):
    target_project_id: str

router = APIRouter(prefix="/api/v1/projects", tags=["Projects"])


def _backfill_link_groups(projects: List[dict]) -> None:
    """Best-effort: fill NULL link_group_id from each project's git remote.

    The workspace picker collapses same-repo clones by ``link_group_id``, but
    the auto path only runs when a git-repository record is touched — a fresh
    clone that was never git-polled stays NULL and shows as a duplicate entry.
    Deriving it here keeps the picker list identical to the sessions board.
    Locked rows keep their manual group; failures never break the listing.
    """
    pending = [
        p for p in projects
        if not p.get("link_group_id") and not is_link_locked(p)
    ]
    if not pending:
        return
    try:
        from app.core.project_links import auto_link_group_id

        remotes = database.get_remote_urls_for_projects([p["id"] for p in pending])
        for project in pending:
            group_id = auto_link_group_id(remotes.get(project["id"]))
            if group_id:
                database.set_project_link_group(project["id"], group_id)
                project["link_group_id"] = group_id
    except Exception:  # noqa: BLE001 — listing must never fail on the backfill
        logger.debug("link_group_id backfill failed", exc_info=True)


def normalize_docker_path(path: str) -> str:
    """
    In Docker mode, convert absolute workspace paths to relative paths.

    The DirectoryBrowser returns absolute paths (e.g., /workspace/my-project)
    even in Docker mode. This helper strips the workspace prefix so downstream
    validation treats them as workspace-relative paths.

    Returns the original path unchanged in local mode or if the path is not
    within the workspace directory.
    """
    if not is_absolute_project_path(path) or is_local_mode():
        return path

    workspace = settings.workspace_dir.resolve()
    try:
        resolved = Path(path).resolve()
        rel = resolved.relative_to(workspace)
        # POSIX separators: relative workspace paths are stored in the shared DB
        # and must resolve identically on every install, including non-Windows.
        return rel.as_posix() if str(rel) != "." else ""
    except ValueError:
        # Path is outside workspace — return as-is and let validation reject it
        return path


def validate_project_path(path: str, allow_create: bool = False) -> Path:
    """
    Validate a project path.

    In Docker mode: path must be within the workspace boundary. Absolute paths
                    that resolve inside the workspace are accepted and converted
                    to relative paths automatically.
    In local mode: absolute paths are allowed (pointing anywhere on disk),
                   relative paths are resolved within workspace.

    If allow_create=True, non-existent directories are permitted (caller will create them).
    """
    # In Docker mode, normalize absolute workspace paths to relative
    path = normalize_docker_path(path)

    if is_absolute_project_path(path):
        # Absolute paths only allowed in local mode
        if not is_local_mode():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Absolute paths are not allowed in Docker mode"
            )
        full_path = Path(path).resolve()
        if full_path.exists() and not full_path.is_dir():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Path is not a directory: {path}"
            )
        if not allow_create and not full_path.exists():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Directory does not exist: {path}"
            )
        return full_path

    # Relative path - resolve within workspace and enforce boundary
    full_path = (settings.workspace_dir / path).resolve()

    try:
        full_path.relative_to(settings.workspace_dir.resolve())
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Path escapes workspace boundary"
        )

    return full_path


@router.get("", response_model=List[Project])
async def list_projects(request: Request, token: str = Depends(require_auth)):
    """List all projects. API users only see their assigned project."""
    api_user = get_api_user_from_request(request)

    # Get session counts for all projects
    session_counts = database.get_all_project_session_counts()

    if api_user and api_user.get("project_id"):
        # API user with assigned project - only return that project
        project = database.get_project(api_user["project_id"])
        if project:
            _backfill_link_groups([project])
            project["session_count"] = session_counts.get(project["id"], 0)
            if not is_absolute_project_path(project["path"]):
                project["path"] = get_project_working_dir(project)
            return [project]
        return []

    # Admin or unrestricted API user - return all projects
    projects = database.get_all_projects()
    _backfill_link_groups(projects)
    for project in projects:
        project["session_count"] = session_counts.get(project["id"], 0)
        # Resolve relative paths to absolute for display
        if not is_absolute_project_path(project["path"]):
            project["path"] = get_project_working_dir(project)
    return projects


@router.get("/{project_id}", response_model=Project)
async def get_project(request: Request, project_id: str, token: str = Depends(require_auth)):
    """Get a specific project. API users can only access their assigned project."""
    check_project_access(request, project_id)

    project = database.get_project(project_id)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project not found: {project_id}"
        )
    project["session_count"] = database.get_project_session_count(project_id)
    # Resolve relative paths to absolute for display
    if not is_absolute_project_path(project["path"]):
        project["path"] = get_project_working_dir(project)
    return project


@router.post("", response_model=Project, status_code=status.HTTP_201_CREATED)
async def create_project(request: ProjectCreate, token: str = Depends(require_admin)):
    """Create a new project - Admin only"""
    # Check if ID already exists
    existing = database.get_project(request.id)
    if existing:
        # If the existing project belongs to a different instance, auto-suffix
        # the ID so the same project name can exist on multiple machines.
        if existing.get("instance_id") and existing["instance_id"] != settings.effective_instance_id:
            import hashlib
            suffix = hashlib.sha256(settings.effective_instance_id.encode()).hexdigest()[:6]
            request = request.model_copy(update={"id": f"{request.id}-{suffix}"})
            # Re-check with the new ID
            if database.get_project(request.id):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Project already exists: {request.id}"
                )
        else:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Project already exists: {request.id}"
            )

    # Determine project path and create directory if needed
    # Normalize Docker paths first (e.g., /workspace/foo → foo)
    effective_path = normalize_docker_path(request.path) if request.path else None

    if effective_path and is_absolute_project_path(effective_path):
        # Absolute path (local mode only) - create directory if it doesn't exist
        project_path = validate_project_path(effective_path, allow_create=True)
        project_path.mkdir(parents=True, exist_ok=True)
        db_path = effective_path
    else:
        # Relative path (or normalized Docker path) - create directory inside workspace
        rel_path = effective_path if effective_path else request.id
        project_path = validate_project_path(rel_path)
        project_path.mkdir(parents=True, exist_ok=True)
        db_path = rel_path

    # Create project in database
    settings_dict = None
    if request.settings:
        settings_dict = request.settings.model_dump(exclude_none=True)

    project = database.create_project(
        project_id=request.id,
        name=request.name,
        description=request.description,
        path=db_path,
        settings_dict=settings_dict
    )

    return project


@router.put("/{project_id}", response_model=Project)
async def update_project(
    project_id: str,
    request: ProjectUpdate,
    token: str = Depends(require_admin)
):
    """Update a project - Admin only"""
    existing = database.get_project(project_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project not found: {project_id}"
        )

    # Validate new path if provided
    db_path = None
    if request.path is not None:
        effective_path = normalize_docker_path(request.path) if request.path else None
        if effective_path and is_absolute_project_path(effective_path):
            validate_project_path(effective_path, allow_create=False)
            db_path = effective_path
        elif effective_path:
            validate_project_path(effective_path)
            db_path = effective_path

    settings_dict = None
    if request.settings:
        settings_dict = request.settings.model_dump(exclude_none=True)

    project = database.update_project(
        project_id=project_id,
        name=request.name,
        description=request.description,
        path=db_path,
        settings_dict=settings_dict
    )

    return project


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(project_id: str, token: str = Depends(require_admin)):
    """Delete a project (database record only, not files) - Admin only"""
    existing = database.get_project(project_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project not found: {project_id}"
        )

    database.delete_project(project_id)


def _project_with_meta(project_id: str) -> dict:
    """Re-read a project and decorate it for a Project response."""
    project = database.get_project(project_id)
    project["session_count"] = database.get_project_session_count(project_id)
    if not is_absolute_project_path(project["path"]):
        project["path"] = get_project_working_dir(project)
    return project


@router.post("/{project_id}/link", response_model=Project)
async def link_project(
    project_id: str,
    request: ProjectLinkRequest,
    token: str = Depends(require_admin),
):
    """Manually link two project rows into one logical project - Admin only.

    Both rows join the same ``link_group_id`` and are marked locked so the
    auto-by-git-remote path stops overwriting the manual choice. The sessions
    board then folds their sessions into a single group. Useful for clones whose
    remotes differ (forks, mirrors) that the user nonetheless treats as one
    project.
    """
    from app.core.project_links import LINK_LOCKED_KEY

    source = database.get_project(project_id)
    if not source:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project not found: {project_id}",
        )
    target = database.get_project(request.target_project_id)
    if not target:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project not found: {request.target_project_id}",
        )
    if source["id"] == target["id"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot link a project to itself",
        )

    # Prefer an existing group so linking into a group of 3+ keeps everyone
    # together; otherwise mint a fresh manual group id.
    group_id = target.get("link_group_id") or source.get("link_group_id") or f"manual:{uuid.uuid4().hex[:16]}"

    for proj in (source, target):
        merged = dict(proj.get("settings") or {})
        merged[LINK_LOCKED_KEY] = True
        database.update_project(proj["id"], settings_dict=merged)
        database.set_project_link_group(proj["id"], group_id)

    return _project_with_meta(project_id)


@router.delete("/{project_id}/link", response_model=Project)
async def unlink_project(project_id: str, token: str = Depends(require_admin)):
    """Remove a project's manual link, then re-derive its auto group - Admin only.

    Clears the lock and link group, then recomputes the deterministic
    auto-group from the project's git remote (so a still-shared repo regroups
    with its peers).
    """
    from app.core.project_links import LINK_LOCKED_KEY, maybe_auto_link_project

    project = database.get_project(project_id)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project not found: {project_id}",
        )

    merged = dict(project.get("settings") or {})
    merged.pop(LINK_LOCKED_KEY, None)
    database.update_project(project_id, settings_dict=merged)
    database.set_project_link_group(project_id, None)

    # Re-derive the deterministic auto group from the git remote, if any.
    try:
        repo = database.get_git_repository_by_project(project_id)
        maybe_auto_link_project(project_id, (repo or {}).get("remote_url"))
    except Exception:  # noqa: BLE001
        logger.debug("re-auto-link after unlink failed for %s", project_id, exc_info=True)

    return _project_with_meta(project_id)


def get_file_extension(name: str) -> str:
    """Get lowercase file extension without the dot"""
    ext = Path(name).suffix.lower()
    return ext[1:] if ext else ""


@router.get("/{project_id}/files")
async def list_project_files(
    request: Request,
    project_id: str,
    path: str = "",
    show_hidden: bool = False,
    session_id: Optional[str] = Query(None, description="Session ID — resolves a v2 branch session's own checkout"),
    token: str = Depends(require_auth)
):
    """List files in a project directory. API users can only access their assigned project."""
    check_project_access(request, project_id)

    # A v2 branch session's own checkout takes priority over the base project dir
    # (pure-read resolution — an unmaterialized checkout falls back to the project).
    session_cwd: Optional[Path] = None
    if session_id:
        from app.core.project_resolver import resolve_existing_cwd

        session = database.get_session(session_id)
        if session:
            cwd = resolve_existing_cwd(session)
            if cwd:
                session_cwd = Path(cwd)

    if session_cwd is not None:
        base_path = session_cwd
        full_path = base_path / path if path else base_path

        if not full_path.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Directory not found"
            )
        if not full_path.is_dir():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Path is not a directory"
            )

        files = []
        for item in full_path.iterdir():
            if item.name.startswith('.') and not show_hidden:
                continue
            try:
                stat_info = item.stat()
                is_dir = item.is_dir()
                files.append({
                    "name": item.name,
                    "type": "directory" if is_dir else "file",
                    "size": stat_info.st_size if not is_dir else None,
                    "sizeFormatted": format_file_size(stat_info.st_size) if not is_dir else None,
                    "path": str(item.relative_to(base_path)),
                    "extension": get_file_extension(item.name) if not is_dir else None,
                    "modifiedAt": datetime.fromtimestamp(stat_info.st_mtime).isoformat(),
                    "createdAt": datetime.fromtimestamp(stat_info.st_ctime).isoformat(),
                })
            except (OSError, PermissionError):
                continue
        files.sort(key=lambda x: (x["type"] != "directory", x["name"].lower()))
        return {"files": files, "path": path, "projectId": project_id}

    project = database.get_project(project_id)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project not found: {project_id}"
        )

    # Build the full path
    base_path = get_project_working_dir_as_path(project)
    if path:
        full_path = validate_project_path(f"{project['path']}/{path}")
    else:
        full_path = base_path

    if not full_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Directory not found"
        )

    if not full_path.is_dir():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Path is not a directory"
        )

    # List directory contents
    files = []
    for item in full_path.iterdir():
        # Skip hidden files unless requested
        if item.name.startswith('.') and not show_hidden:
            continue

        try:
            stat_info = item.stat()
            is_dir = item.is_dir()

            file_info = {
                "name": item.name,
                "type": "directory" if is_dir else "file",
                "size": stat_info.st_size if not is_dir else None,
                "sizeFormatted": format_file_size(stat_info.st_size) if not is_dir else None,
                "path": str(item.relative_to(base_path)),
                "extension": get_file_extension(item.name) if not is_dir else None,
                "modifiedAt": datetime.fromtimestamp(stat_info.st_mtime).isoformat(),
                "createdAt": datetime.fromtimestamp(stat_info.st_ctime).isoformat(),
            }
            files.append(file_info)
        except (OSError, PermissionError):
            # Skip files we can't access
            continue

    # Sort: directories first, then files
    files.sort(key=lambda x: (x["type"] != "directory", x["name"].lower()))

    return {"files": files, "path": path, "projectId": project_id}


MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50 MB


async def _read_upload_within_limit(file: UploadFile) -> bytes:
    """Read an UploadFile fully, rejecting anything over MAX_UPLOAD_SIZE.

    Reads in 64KB chunks so the limit is enforced without trusting the
    (spoofable) Content-Length header.
    """
    chunks = []
    total_size = 0
    while True:
        chunk = await file.read(64 * 1024)
        if not chunk:
            break
        total_size += len(chunk)
        if total_size > MAX_UPLOAD_SIZE:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"File too large. Maximum upload size is {MAX_UPLOAD_SIZE // (1024 * 1024)}MB"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _perform_project_upload(
    project: dict,
    *,
    content: bytes,
    filename: str,
    path: str = "",
    session_id: str = "",
    worktree_id: str = "",
) -> dict:
    """Write already-read upload bytes to the project/worktree uploads dir.

    Resolves the destination the same way whether the upload arrives directly or
    is relayed from another install: worktree working dir when the session (or
    ``worktree_id``) resolves one and it exists on disk, else the project path.
    Runs on the install that OWNS the session, so the returned ``full_path`` is
    always rooted on a disk the session's agent can read. Synchronous (filesystem
    + DB) — call via ``run_sync`` from async contexts.
    """
    # Determine base path: use worktree path if session or worktree_id resolves one
    base_project_path = project["path"]
    worktree = None
    if session_id:
        worktree = database.get_worktree_by_session(session_id)
    if not worktree and worktree_id:
        worktree = database.get_worktree(worktree_id)
    if worktree and worktree.get("worktree_path"):
        # Verify the worktree directory actually exists on disk
        worktree_full = Path(get_worktree_working_dir(worktree))
        if worktree_full.exists():
            base_project_path = worktree["worktree_path"]

    # Build target directory path using Path joining (avoid mixed slashes on Windows)
    base = Path(base_project_path)
    if path:
        target_dir = validate_project_path(str(base / path), allow_create=True)
    else:
        # Default to uploads subdirectory to keep project root clean
        target_dir = validate_project_path(str(base / "uploads"), allow_create=True)

    # Ensure directory exists
    target_dir.mkdir(parents=True, exist_ok=True)

    # Sanitize filename
    safe_filename = Path(filename).name
    if not safe_filename or safe_filename.startswith('.'):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid filename"
        )

    # Full file path
    file_path = target_dir / safe_filename

    # Handle duplicate filenames by adding counter
    if file_path.exists():
        stem = file_path.stem
        suffix = file_path.suffix
        counter = 1
        while file_path.exists():
            file_path = target_dir / f"{stem}_{counter}{suffix}"
            counter += 1

    file_path.write_bytes(content)

    # Return relative path for reference (relative to the effective base path)
    base_path = get_project_working_dir_as_path(base_project_path)
    relative_path = file_path.relative_to(base_path)

    # Use forward slashes for consistent cross-platform path references
    relative_path_str = relative_path.as_posix()

    logger.debug(f"Upload complete: original='{filename}' -> saved='{file_path.name}', path='{relative_path_str}'")

    return {
        "filename": file_path.name,
        "path": relative_path_str,
        "full_path": str(file_path),
        "size": len(content),
        "sizeFormatted": format_file_size(len(content))
    }


@router.post("/{project_id}/upload")
async def upload_file(
    request: Request,
    project_id: str,
    file: UploadFile = File(...),
    path: str = Form(default=""),
    session_id: str = Form(default=""),
    worktree_id: str = Form(default=""),
    token: str = Depends(require_auth)
):
    """Upload a file to a specific path within the project. API users can only access their assigned project.

    If session_id is provided and the session is linked to a worktree, the file
    is uploaded to the worktree's working directory instead of the main project path.
    If worktree_id is provided directly (e.g. before a session exists), it is used
    as a fallback to resolve the worktree.
    """
    # Check Content-Length header for early rejection
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_UPLOAD_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large. Maximum upload size is {MAX_UPLOAD_SIZE // (1024 * 1024)}MB"
        )

    check_project_access(request, project_id)
    project = database.get_project(project_id)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project not found: {project_id}"
        )

    # Read the bytes (enforcing the size limit) before deciding local vs relay so
    # oversized files are rejected here and never carried over the peer channel.
    content = await _read_upload_within_limit(file)

    async with api_error_handler("save uploaded file"):
        return _perform_project_upload(
            project,
            content=content,
            filename=file.filename or "upload",
            path=path,
            session_id=session_id,
            worktree_id=worktree_id,
        )


@router.get("/{project_id}/files/download")
async def download_file(
    request: Request,
    project_id: str,
    path: str = Query(..., description="Path to file relative to project root"),
    token: str = Depends(require_auth)
):
    """Download a file from the project."""
    check_project_access(request, project_id)
    project = database.get_project(project_id)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project not found: {project_id}"
        )

    # Validate and build full path
    full_path = validate_project_path(f"{project['path']}/{path}")

    if not full_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File not found"
        )

    if not full_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Path is not a file"
        )

    return FileResponse(
        path=full_path,
        filename=full_path.name,
        media_type="application/octet-stream"
    )


@router.post("/{project_id}/files/folder")
async def create_folder(
    request: Request,
    project_id: str,
    body: CreateFolderRequest,
    path: str = Query(default="", description="Parent directory path"),
    token: str = Depends(require_auth)
):
    """Create a new folder within the project."""
    check_project_access(request, project_id)
    project = database.get_project(project_id)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project not found: {project_id}"
        )

    # Validate folder name
    folder_name = body.name.strip()
    if not folder_name or "/" in folder_name or "\\" in folder_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid folder name"
        )

    # Build the folder path
    if path:
        folder_path = validate_project_path(f"{project['path']}/{path}/{folder_name}")
    else:
        folder_path = validate_project_path(f"{project['path']}/{folder_name}")

    if folder_path.exists():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Folder already exists"
        )

    async with api_error_handler("create folder"):
        folder_path.mkdir(parents=True, exist_ok=False)

    base_path = get_project_working_dir_as_path(project)
    return {
        "name": folder_name,
        "path": str(folder_path.relative_to(base_path)),
        "type": "directory"
    }


@router.put("/{project_id}/files/rename")
async def rename_file_or_folder(
    request: Request,
    project_id: str,
    body: RenameRequest,
    path: str = Query(..., description="Path to file or folder to rename"),
    token: str = Depends(require_auth)
):
    """Rename a file or folder within the project."""
    check_project_access(request, project_id)
    project = database.get_project(project_id)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project not found: {project_id}"
        )

    # Validate new name
    new_name = body.new_name.strip()
    if not new_name or "/" in new_name or "\\" in new_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid name"
        )

    # Get current path
    current_path = validate_project_path(f"{project['path']}/{path}")
    if not current_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File or folder not found"
        )

    # Build new path
    new_path = current_path.parent / new_name
    if new_path.exists():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A file or folder with that name already exists"
        )

    async with api_error_handler("rename file or folder"):
        current_path.rename(new_path)

    base_path = get_project_working_dir_as_path(project)
    return {
        "name": new_name,
        "path": str(new_path.relative_to(base_path)),
        "type": "directory" if new_path.is_dir() else "file"
    }


@router.delete("/{project_id}/files")
async def delete_file_or_folder(
    request: Request,
    project_id: str,
    path: str = Query(..., description="Path to file or folder to delete"),
    token: str = Depends(require_auth)
):
    """Delete a file or folder within the project."""
    check_project_access(request, project_id)
    project = database.get_project(project_id)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project not found: {project_id}"
        )

    # Get the path to delete
    target_path = validate_project_path(f"{project['path']}/{path}")
    if not target_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File or folder not found"
        )

    # Don't allow deleting the project root
    base_path = get_project_working_dir_as_path(project)
    if target_path.resolve() == base_path.resolve():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete project root directory"
        )

    async with api_error_handler("delete file or folder"):
        if target_path.is_dir():
            shutil.rmtree(target_path)
        else:
            target_path.unlink()

    return {"deleted": path}


# ============================================================================
# Chat History References
# ============================================================================

class ChatHistoryItem(BaseModel):
    """A chat history session available for reference"""
    name: str
    type: str  # Always "chat_history"
    path: str
    sdk_session_id: str
    title: str | None
    preview: str | None
    message_count: int
    modified_at: str
    size_bytes: int


class ChatHistoryResponse(BaseModel):
    """Response containing list of chat history sessions"""
    sessions: List[ChatHistoryItem]


@router.get("/{project_id}/chat-history", response_model=ChatHistoryResponse)
async def list_chat_history(
    request: Request,
    project_id: str,
    search: str = Query("", description="Search query for filtering sessions"),
    limit: int = Query(50, ge=1, le=100, description="Maximum sessions to return"),
    worktree_id: Optional[str] = Query(None, description="Worktree ID (overrides project path when in worktree mode)"),
    session_id: Optional[str] = Query(None, description="Session ID — resolves a v2 branch session's own checkout"),
    token: str = Depends(require_auth)
):
    """
    List chat history sessions available for referencing in the @ mention UI.

    Returns sessions from the Claude SDK's JSONL history files for this project
    or worktree. When worktree_id is provided, shows history from the worktree directory.
    """
    from app.core.jsonl_parser import list_chat_history_sessions

    check_project_access(request, project_id)

    # Resolve working directory: session checkout > worktree > project.
    # A v2 branch session's JSONL history lives under ITS checkout dir.
    if not worktree_id and session_id:
        from app.core.project_resolver import resolve_existing_cwd

        session = database.get_session(session_id)
        if session:
            cwd = resolve_existing_cwd(session)
            if cwd:
                return ChatHistoryResponse(
                    sessions=[
                        ChatHistoryItem(**s)
                        for s in list_chat_history_sessions(
                            working_dir=cwd, search=search, limit=limit
                        )
                    ]
                )

    # Resolve working directory: worktree takes priority over project
    working_dir: str
    if worktree_id:
        worktree = database.get_worktree(worktree_id)
        if worktree:
            working_dir = get_worktree_working_dir(worktree)
        else:
            project = database.get_project(project_id)
            if not project:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Project not found: {project_id}")
            working_dir = get_project_working_dir(project)
    else:
        project = database.get_project(project_id)
        if not project:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Project not found: {project_id}")
        working_dir = get_project_working_dir(project)

    # List chat history sessions
    sessions = list_chat_history_sessions(
        working_dir=working_dir,
        search=search,
        limit=limit
    )

    return ChatHistoryResponse(sessions=[ChatHistoryItem(**s) for s in sessions])


# ============================================================================
# Git/Worktree Sync
# ============================================================================

class WorktreeSyncResponse(BaseModel):
    """Response from worktree sync operation"""
    is_git_repo: bool
    active_worktrees: int
    cleaned_up: List[dict]
    imported: int = 0


@router.post("/{project_id}/sync-worktrees", response_model=WorktreeSyncResponse)
async def sync_project_worktrees(
    request: Request,
    project_id: str,
    token: str = Depends(require_auth)
):
    """
    Sync worktree database records with actual state on disk.

    Should be called when loading a chat card for a project to ensure
    the database reflects reality (worktrees deleted outside the app).

    Returns:
        is_git_repo: Whether this project is a git repository
        active_worktrees: Count of valid active worktrees
        cleaned_up: List of stale worktree records that were cleaned up
    """
    from app.core.worktree_manager import worktree_manager

    # Check project access
    check_project_access(request, project_id)

    # During startup, the frontend may poll before the project record exists.
    # Return a safe default instead of 404 to avoid noisy logs.
    project = database.get_project(project_id)
    if not project:
        return WorktreeSyncResponse(
            is_git_repo=False,
            active_worktrees=0,
            cleaned_up=[],
            imported=0
        )

    # Run sync
    result = worktree_manager.sync_worktrees_for_project(project_id)

    return WorktreeSyncResponse(
        is_git_repo=result.get("is_git_repo", False),
        active_worktrees=result.get("active", 0),
        cleaned_up=result.get("cleaned_up", []),
        imported=result.get("imported", 0)
    )


# ============================================================================
# Session Sync - Import Claude Code sessions
# ============================================================================

class SessionSyncResponse(BaseModel):
    """Response from session sync operation"""
    imported_count: int
    skipped_count: int
    errors: List[str]
    sessions: List[dict]


@router.post("/{project_id}/sync-sessions", response_model=SessionSyncResponse)
async def sync_project_sessions(
    request: Request,
    project_id: str,
    profile_id: str = Query("default", description="Profile to use for imported sessions"),
    token: str = Depends(require_auth)
):
    """
    Sync Claude Code sessions for a project.

    Discovers and imports sessions from ~/.claude/projects/ that were created
    directly in Claude Code but are not yet tracked in the AI Shuffle database.

    This endpoint:
    1. Finds the Claude projects directory matching this project's working dir
    2. Discovers all JSONL session files not already in the database
    3. Imports each session with metadata (title, message count, token usage)

    Should be called when viewing a project's session list to ensure all
    sessions are visible, regardless of whether they were created in
    Claude Code or AI Shuffle.

    Args:
        project_id: The project to sync sessions for
        profile_id: The profile to associate with imported sessions (default: 'default')

    Returns:
        imported_count: Number of sessions successfully imported
        skipped_count: Number of sessions skipped (already imported or errors)
        errors: List of error messages for failed imports
        sessions: List of newly imported session objects
    """
    from app.core.session_sync import session_sync_service

    # Check project access
    check_project_access(request, project_id)

    # Verify project exists
    project = database.get_project(project_id)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project not found: {project_id}"
        )

    # Run session sync
    result = session_sync_service.sync_project_sessions(
        project_id=project_id,
        profile_id=profile_id
    )

    return SessionSyncResponse(
        imported_count=result["imported_count"],
        skipped_count=result["skipped_count"],
        errors=result["errors"],
        sessions=result["sessions"]
    )
