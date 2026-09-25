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
from app.db.async_database import run_sync
from app.api.auth import require_auth, require_admin, get_api_user_from_request
from app.api.common import check_project_access, format_file_size, get_file_extension, api_error_handler, is_path_safe
from app.api.sessions_helpers import check_session_access_for_api_user
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


def _needs_link_backfill(projects: List[dict]) -> List[dict]:
    """The rows a link-group backfill could still fix — a pure, in-memory check.

    Deliberately cheap and query-free: it is what keeps the listing endpoint
    from touching the database at all once every project carries a group (the
    steady state), so the backfill's read + writes never run on the hot path.
    """
    return [
        p for p in projects
        if not p.get("link_group_id") and not is_link_locked(p)
    ]


def _backfill_link_groups(projects: List[dict]) -> None:
    """Best-effort: fill NULL link_group_id from each project's git remote.

    The workspace picker collapses same-repo clones by ``link_group_id``, but
    the auto path only runs when a git-repository record is touched — a fresh
    clone that was never git-polled stays NULL and shows as a duplicate entry.
    Deriving it here keeps the picker list identical to the sessions board.
    Locked rows keep their manual group; failures never break the listing.

    Self-limiting: a derived group is WRITTEN to the row, so that project is
    never pending again — after the one-time derivation the listing issues no
    query and no write at all (see ``_needs_link_backfill``). Synchronous; the
    listing runs it inside its single ``run_sync`` hop, never on the event loop.
    """
    pending = _needs_link_backfill(projects)
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


def _load_projects_for_listing(api_user_project_id: Optional[str]) -> tuple:
    """Every DB read (and the one-time link-group derivation) for the listing.

    Runs in ONE worker thread: these are synchronous psycopg2 calls that used
    to execute directly on the event loop, stalling every concurrent request
    for the duration of a full ``projects`` scan plus a session-count GROUP BY.
    """
    session_counts = database.get_all_project_session_counts()

    if api_user_project_id:
        # API user with assigned project - only return that project
        project = database.get_project(api_user_project_id)
        projects = [project] if project else []
    else:
        # Admin or unrestricted API user - return all projects
        projects = database.get_all_projects()

    # No-op (not one query, not one write) once every row carries a group.
    _backfill_link_groups(projects)
    return projects, session_counts


@router.get("", response_model=List[Project])
async def list_projects(request: Request, token: str = Depends(require_auth)):
    """List all projects. API users only see their assigned project."""
    api_user = get_api_user_from_request(request)
    api_user_project_id = api_user.get("project_id") if api_user else None

    projects, session_counts = await run_sync(
        _load_projects_for_listing, api_user_project_id
    )

    for project in projects:
        project["session_count"] = session_counts.get(project["id"], 0)
        # Resolve relative paths to absolute for display
        if not is_absolute_project_path(project["path"]):
            project["path"] = get_project_working_dir(project)
    return projects


def _load_project_detail(project_id: str) -> tuple:
    """Project row + its session count — one thread hop for the detail read."""
    project = database.get_project(project_id)
    if not project:
        return None, 0
    return project, database.get_project_session_count(project_id)


@router.get("/{project_id}", response_model=Project)
async def get_project(request: Request, project_id: str, token: str = Depends(require_auth)):
    """Get a specific project. API users can only access their assigned project."""
    check_project_access(request, project_id)

    # Both reads in ONE thread hop (they were sync calls on the event loop).
    project, session_count = await run_sync(_load_project_detail, project_id)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project not found: {project_id}"
        )
    project["session_count"] = session_count
    # Resolve relative paths to absolute for display
    if not is_absolute_project_path(project["path"]):
        project["path"] = get_project_working_dir(project)
    return project


@router.post("", response_model=Project, status_code=status.HTTP_201_CREATED)
async def create_project(request: ProjectCreate, token: str = Depends(require_admin)):
    """Create a new project - Admin only"""
    def _create():
        # Read-decide-write in ONE hop. The duplicate-id probe (twice, once for
        # the instance-suffixed id) decides whether the INSERT happens at all;
        # splitting it from the write would let a concurrent create claim the id
        # in between. The directory mkdir is blocking filesystem I/O and rides
        # the same hop rather than stalling the loop on its own.
        payload = request

        # Check if ID already exists
        existing = database.get_project(payload.id)
        if existing:
            # If the existing project belongs to a different instance, auto-suffix
            # the ID so the same project name can exist on multiple machines.
            if existing.get("instance_id") and existing["instance_id"] != settings.effective_instance_id:
                import hashlib
                suffix = hashlib.sha256(settings.effective_instance_id.encode()).hexdigest()[:6]
                payload = payload.model_copy(update={"id": f"{payload.id}-{suffix}"})
                # Re-check with the new ID
                if database.get_project(payload.id):
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"Project already exists: {payload.id}"
                    )
            else:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Project already exists: {payload.id}"
                )

        # Determine project path and create directory if needed
        # Normalize Docker paths first (e.g., /workspace/foo → foo)
        effective_path = normalize_docker_path(payload.path) if payload.path else None

        if effective_path and is_absolute_project_path(effective_path):
            # Absolute path (local mode only) - create directory if it doesn't exist
            project_path = validate_project_path(effective_path, allow_create=True)
            project_path.mkdir(parents=True, exist_ok=True)
            db_path = effective_path
        else:
            # Relative path (or normalized Docker path) - create directory inside workspace
            rel_path = effective_path if effective_path else payload.id
            project_path = validate_project_path(rel_path)
            project_path.mkdir(parents=True, exist_ok=True)
            db_path = rel_path

        # Create project in database
        settings_dict = None
        if payload.settings:
            settings_dict = payload.settings.model_dump(exclude_none=True)

        return database.create_project(
            project_id=payload.id,
            name=payload.name,
            description=payload.description,
            path=db_path,
            settings_dict=settings_dict
        )

    return await run_sync(_create)


@router.put("/{project_id}", response_model=Project)
async def update_project(
    project_id: str,
    request: ProjectUpdate,
    token: str = Depends(require_admin)
):
    """Update a project - Admin only"""
    def _update():
        # Read-decide-write kept atomic: the existence probe gates the UPDATE.
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

        return database.update_project(
            project_id=project_id,
            name=request.name,
            description=request.description,
            path=db_path,
            settings_dict=settings_dict
        )

    return await run_sync(_update)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(project_id: str, token: str = Depends(require_admin)):
    """Delete a project (database record only, not files) - Admin only"""
    def _delete():
        # Read-decide-write kept atomic: existence probe + DELETE in one hop.
        existing = database.get_project(project_id)
        if not existing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Project not found: {project_id}"
            )

        database.delete_project(project_id)

    await run_sync(_delete)


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

    def _link():
        # Read-decide-write kept atomic in ONE hop: both rows are read, their
        # existing settings/link groups decide the group id, and four writes
        # follow. Splitting the reads from the writes would let a concurrent
        # link (or an unlink) land in between and clobber the merged settings.
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

    return await run_sync(_link)


@router.delete("/{project_id}/link", response_model=Project)
async def unlink_project(project_id: str, token: str = Depends(require_admin)):
    """Remove a project's manual link, then re-derive its auto group - Admin only.

    Clears the lock and link group, then recomputes the deterministic
    auto-group from the project's git remote (so a still-shared repo regroups
    with its peers).
    """
    from app.core.project_links import LINK_LOCKED_KEY, maybe_auto_link_project

    def _unlink():
        # Read-decide-write kept atomic: the row read supplies the settings dict
        # that is written back, so the clear and the re-derivation must not be
        # split across hops.
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

    return await run_sync(_unlink)


def _get_project_session(session_id: str, project_id: str, api_user: Optional[dict]) -> Optional[dict]:
    """Load a session for a project-scoped route; 404 if it is another project's, 403 if denied."""
    session = database.get_session(session_id)
    if session:
        if session.get("project_id") != project_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Session not found: {session_id}"
            )
        check_session_access_for_api_user(api_user, session)
    return session


def _get_project_worktree(worktree_id: str, project_id: str) -> Optional[dict]:
    """Load a worktree for a project-scoped route; 404 if it belongs to another project's repository."""
    worktree = database.get_worktree(worktree_id)
    if worktree:
        repo = database.get_git_repository_by_project(project_id)
        if not repo or worktree.get("repository_id") != repo["id"]:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Worktree not found in this project: {worktree_id}"
            )
    return worktree


def _resolve_in_project(project: dict, rel: str) -> tuple[Path, Path]:
    """Resolve ``rel`` inside the project's working dir; 400 if it escapes it.

    Returns ``(full_path, base_path)``, both resolved. The workspace boundary
    alone is not enough: ``../other-project`` stays inside the workspace. A
    leading slash is project-relative, as it was when paths were string-joined.
    """
    base_path = get_project_working_dir_as_path(project).resolve()
    rel = rel.lstrip("/\\")
    full_path = (base_path / rel).resolve() if rel else base_path
    if not is_path_safe(full_path, base_path):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Path escapes project directory"
        )
    return full_path, base_path


def _resolve_leaf_in_project(project: dict, rel: str) -> tuple[Path, Path]:
    """``_resolve_in_project`` for delete and rename: a symlink is never followed.

    The path must still resolve inside the project, but when its last component
    is a symlink the returned path is the link itself (in its resolved parent
    directory), so deleting or renaming ``alias -> real`` acts on ``alias``.
    """
    full_path, base_path = _resolve_in_project(project, rel)
    rel = rel.lstrip("/\\")
    lexical = base_path / rel
    if not rel or lexical.name in ("", ".", "..") or not lexical.is_symlink():
        return full_path, base_path
    parent = lexical.parent.resolve()
    if not is_path_safe(parent, base_path):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Path escapes project directory"
        )
    return parent / lexical.name, base_path


def _refuse_git_metadata(*paths: str) -> None:
    """400 when any path names the repository metadata directory.

    Every component is checked, in every spelling git itself refuses (case,
    trailing dots and spaces, ``.git::$INDEX_ALLOCATION``, the 8.3 name
    ``GIT~1``): ``.git`` controls what the app's own git runs execute (hooks,
    config), so file routes must never write into it or move it.
    """
    from app.core.git_commit_ops import _is_ntfs_dotgit

    for path in paths:
        for name in str(path).replace("\\", "/").split("/"):
            if name and _is_ntfs_dotgit(name):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Path is inside the repository metadata directory (.git)",
                )


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
    api_user = get_api_user_from_request(request)

    def _list():
        # ONE hop for the whole listing. Everything here is blocking: the
        # session/project reads, the checkout resolution (an isdir probe) and,
        # dominating them all, an ``iterdir`` with a ``stat`` per entry. The
        # directory walk used to run straight on the event loop.
        # A v2 branch session's own checkout takes priority over the base project dir
        # (pure-read resolution — an unmaterialized checkout falls back to the project).
        session_cwd: Optional[Path] = None
        if session_id:
            from app.core.project_resolver import resolve_existing_cwd

            session = _get_project_session(session_id, project_id, api_user)
            if session:
                cwd = resolve_existing_cwd(session)
                if cwd:
                    session_cwd = Path(cwd)

        if session_cwd is not None:
            base_path = session_cwd
            full_path = base_path / path if path else base_path
            if not is_path_safe(full_path, base_path):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Path escapes session checkout"
                )

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

        full_path, base_path = _resolve_in_project(project, path)

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

    return await run_sync(_list)


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

    # Uploads are confined without following links, so the spelling is the target.
    _refuse_git_metadata(path, filename)

    # Build target directory path using Path joining (avoid mixed slashes on Windows)
    base = Path(base_project_path)
    if path:
        validate_project_path(str(base / path), allow_create=True)
    else:
        # Default to uploads subdirectory to keep project root clean
        validate_project_path(str(base / "uploads"), allow_create=True)

    from app.core.confined_files import upload
    safe_filename = Path(filename).name
    if not safe_filename or safe_filename.startswith('.'):
        raise HTTPException(status_code=400, detail="Invalid filename")
    file_path = upload(get_project_working_dir_as_path(base_project_path),
                       path or "uploads", safe_filename, content)

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
    api_user = get_api_user_from_request(request)
    _refuse_git_metadata(path, file.filename or "")

    def _load_project():
        project = database.get_project(project_id)
        if not project:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Project not found: {project_id}"
            )
        # The destination may come from session_id / worktree_id; both must
        # belong to this project, or the file lands in another project's checkout.
        if session_id:
            _get_project_session(session_id, project_id, api_user)
        if worktree_id:
            _get_project_worktree(worktree_id, project_id)
        return project

    # TWO hops here, deliberately: the 404 is decided before the body is read
    # (unchanged ordering) and the body read is a real await that cannot happen
    # inside a worker thread. The second hop is the write, and it must stay
    # inside ``api_error_handler`` so a filesystem failure keeps mapping to the
    # same 500 — the project read must NOT be inside that handler.
    project = await run_sync(_load_project)

    # Read the bytes (enforcing the size limit) before deciding local vs relay so
    # oversized files are rejected here and never carried over the peer channel.
    content = await _read_upload_within_limit(file)

    async with api_error_handler("save uploaded file"):
        return await run_sync(
            _perform_project_upload,
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

    def _resolve():
        # ONE hop: the project read plus the exists/is_file stats. Only the
        # FileResponse object is built back on the loop (Starlette streams the
        # file off-loop itself).
        project = database.get_project(project_id)
        if not project:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Project not found: {project_id}"
            )

        full_path, _ = _resolve_in_project(project, path)

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

        return full_path

    full_path = await run_sync(_resolve)

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

    # Validate folder name
    folder_name = body.name.strip()
    if not folder_name or "/" in folder_name or "\\" in folder_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid folder name"
        )
    _refuse_git_metadata(path, folder_name)

    def _resolve():
        project = database.get_project(project_id)
        if not project:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Project not found: {project_id}"
            )

        folder_path, base_path = _resolve_in_project(
            project, f"{path}/{folder_name}" if path else folder_name
        )
        # The resolved identity too: a link to .git is followed in the parent.
        _refuse_git_metadata(folder_path.relative_to(base_path).as_posix())

        if folder_path.exists():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Folder already exists"
            )

        return folder_path, base_path

    # TWO hops on purpose: ``api_error_handler`` today wraps ONLY the mkdir, so
    # a DB failure keeps surfacing as itself rather than as "Failed to create
    # folder". Folding the read into that block would change the status a
    # database error produces.
    folder_path, base_path = await run_sync(_resolve)

    async with api_error_handler("create folder"):
        await run_sync(folder_path.mkdir, parents=True, exist_ok=False)

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

    # Validate new name
    new_name = body.new_name.strip()
    if not new_name or "/" in new_name or "\\" in new_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid name"
        )
    _refuse_git_metadata(path, new_name)

    def _resolve():
        project = database.get_project(project_id)
        if not project:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Project not found: {project_id}"
            )

        current_path, base_path = _resolve_leaf_in_project(project, path)
        if not current_path.exists() and not current_path.is_symlink():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="File or folder not found"
            )
        if current_path == base_path:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot rename project root directory"
            )

        # Build new path
        new_path = current_path.parent / new_name
        if not is_path_safe(new_path, base_path):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid name"
            )
        # The resolved identities too: a link to .git is followed in the parent.
        _refuse_git_metadata(
            current_path.relative_to(base_path).as_posix(),
            new_path.relative_to(base_path).as_posix(),
        )
        if new_path.exists():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A file or folder with that name already exists"
            )

        return current_path, new_path, base_path

    # TWO hops: ``api_error_handler`` must keep wrapping only the rename, as it
    # does today, so a DB error is not relabelled as a rename failure.
    current_path, new_path, base_path = await run_sync(_resolve)

    def _rename():
        current_path.rename(new_path)
        # The is_dir() stat is part of the same filesystem step, not a second one.
        return new_path.is_dir()

    async with api_error_handler("rename file or folder"):
        is_dir = await run_sync(_rename)

    return {
        "name": new_name,
        "path": str(new_path.relative_to(base_path)),
        "type": "directory" if is_dir else "file"
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
    _refuse_git_metadata(path)

    def _resolve():
        project = database.get_project(project_id)
        if not project:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Project not found: {project_id}"
            )

        target_path, base_path = _resolve_leaf_in_project(project, path)
        if not target_path.exists() and not target_path.is_symlink():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="File or folder not found"
            )

        # Don't allow deleting the project root
        if target_path == base_path:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot delete project root directory"
            )
        # The resolved identity too: a link to .git is followed in the parent
        # (a link that IS the leaf is removed itself, which is harmless).
        _refuse_git_metadata(target_path.relative_to(base_path).as_posix())

        return target_path

    # TWO hops: ``api_error_handler`` must keep wrapping only the removal, so a
    # DB error is not relabelled as a delete failure.
    target_path = await run_sync(_resolve)

    def _remove():
        if target_path.is_symlink():  # the link itself, never its target
            target_path.unlink()
        elif target_path.is_dir():
            shutil.rmtree(target_path)
        else:
            target_path.unlink()

    async with api_error_handler("delete file or folder"):
        await run_sync(_remove)

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
    api_user = get_api_user_from_request(request)

    def _load():
        # ONE hop for the working-dir resolution AND the history scan. The scan
        # walks the SDK's JSONL history directory and parses every file it
        # finds — by far the most expensive thing this endpoint does, and it
        # used to run on the event loop together with up to two DB reads.
        # Resolve working directory: session checkout > worktree > project.
        # A v2 branch session's JSONL history lives under ITS checkout dir.
        if not worktree_id and session_id:
            from app.core.project_resolver import resolve_existing_cwd

            session = _get_project_session(session_id, project_id, api_user)
            if session:
                cwd = resolve_existing_cwd(session)
                if cwd:
                    return list_chat_history_sessions(
                        working_dir=cwd, search=search, limit=limit
                    )

        # Resolve working directory: worktree takes priority over project
        working_dir: str
        if worktree_id:
            worktree = _get_project_worktree(worktree_id, project_id)
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
        return list_chat_history_sessions(
            working_dir=working_dir,
            search=search,
            limit=limit
        )

    sessions = await run_sync(_load)

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

    def _sync():
        # ONE hop: the existence probe and the sync itself, which shells out to
        # git and writes worktree rows — blocking work that dwarfs the read.
        # During startup, the frontend may poll before the project record
        # exists. Return a safe default instead of 404 to avoid noisy logs.
        project = database.get_project(project_id)
        if not project:
            return None

        # Run sync
        return worktree_manager.sync_worktrees_for_project(project_id)

    result = await run_sync(_sync)
    if result is None:
        return WorktreeSyncResponse(
            is_git_repo=False,
            active_worktrees=0,
            cleaned_up=[],
            imported=0
        )

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


def _resolve_sync_profile_id(requested: Optional[str], project: dict) -> str:
    """The agent imported sessions are filed under.

    The app seeds no agents, so there is no "default" to fall back on.
    """
    if requested:
        if not database.get_profile(requested):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Agent not found: {requested}")
        return requested
    default_id = (project.get("settings") or {}).get("default_profile_id")
    if default_id and database.get_profile(default_id):
        return default_id
    profiles = database.get_all_profiles()
    if not profiles:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No agents are configured; create an agent before importing sessions.",
        )
    return profiles[0]["id"]


@router.post("/{project_id}/sync-sessions", response_model=SessionSyncResponse)
async def sync_project_sessions(
    request: Request,
    project_id: str,
    profile_id: Optional[str] = Query(None, description="Agent for imported sessions; defaults to the project's default agent, else the first agent"),
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
        profile_id: The agent for imported sessions. Defaults to the project's
            default agent, else the first agent; 409 when there are no agents.

    Returns:
        imported_count: Number of sessions successfully imported
        skipped_count: Number of sessions skipped (already imported or errors)
        errors: List of error messages for failed imports
        sessions: List of newly imported session objects
    """
    from app.core.session_sync import session_sync_service

    # Check project access
    check_project_access(request, project_id)

    def _sync():
        # ONE hop: the existence probe plus the import, which scans and parses
        # every JSONL session file under the Claude projects dir and inserts
        # rows for the new ones — all of it blocking.
        # Verify project exists
        project = database.get_project(project_id)
        if not project:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Project not found: {project_id}"
            )

        # Run session sync
        return session_sync_service.sync_project_sessions(
            project_id=project_id,
            profile_id=_resolve_sync_profile_id(profile_id, project),
        )

    result = await run_sync(_sync)

    return SessionSyncResponse(
        imported_count=result["imported_count"],
        skipped_count=result["skipped_count"],
        errors=result["errors"],
        sessions=result["sessions"]
    )
