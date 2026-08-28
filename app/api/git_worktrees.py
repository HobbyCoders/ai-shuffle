"""
Git worktree API endpoints.

Provides endpoints for worktree management including:
- List, create, get, delete, reopen worktrees
- Sync worktree records with git
- Browse files within worktrees
"""

import concurrent.futures
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple

from fastapi import APIRouter, HTTPException, Depends, Request, status

from app.core.git_service import git_service
from app.core.worktree_manager import worktree_manager, WorktreeError
from app.core.project_utils import get_worktree_working_dir
from app.db import database
from app.db.async_database import run_sync
from app.api.auth import require_auth
from app.api.common import check_project_access
from app.api.git_helpers import run_git, get_project_path, invalidate_working_dir_cache

from app.api.schemas.git import (
    StagedFile,
    SessionInfo,
    WorktreeGitStatus,
    WorktreeInfo,
    WorktreeListResponse,
    WorktreeCreateStandalone,
    WorktreeSyncResponse,
    WorktreeFileInfo,
    WorktreeFilesResponse,
    OperationResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# =============================================================================
# Worktree Helper Functions
# =============================================================================

def _convert_worktree_to_info(wt: dict) -> WorktreeInfo:
    """Convert worktree dict to WorktreeInfo model.

    Supports both new session relationship model (sessions list) and
    legacy single session model for backward compatibility.
    """
    # Convert sessions list (new model)
    sessions_list = []
    if wt.get("sessions"):
        for s in wt["sessions"]:
            sessions_list.append(SessionInfo(
                id=s["id"],
                title=s.get("title"),
                status=s["status"],
                updated_at=s.get("updated_at")
            ))

    # Active session (new model)
    active_session_info = None
    if wt.get("active_session"):
        as_data = wt["active_session"]
        active_session_info = SessionInfo(
            id=as_data["id"],
            title=as_data.get("title"),
            status=as_data["status"],
            updated_at=as_data.get("updated_at")
        )

    # Legacy single session (deprecated, for backward compat)
    legacy_session_info = None
    if wt.get("session"):
        legacy_session_info = SessionInfo(
            id=wt["session"]["id"],
            title=wt["session"].get("title"),
            status=wt["session"]["status"],
            updated_at=wt["session"].get("updated_at")
        )

    git_status_info = None
    if wt.get("git_status"):
        gs = wt["git_status"]
        staged_files = [
            StagedFile(file=s["file"], status=s["status"])
            for s in gs.get("staged", [])
        ]
        modified_list = gs.get("modified", [])
        untracked_list = gs.get("untracked", [])
        git_status_info = WorktreeGitStatus(
            is_clean=gs.get("is_clean", True),
            current_branch=gs.get("current_branch"),
            modified_count=len(modified_list),
            staged_count=len(staged_files),
            untracked_count=len(untracked_list),
            modified=modified_list,
            staged=staged_files,
            untracked=untracked_list
        )

    return WorktreeInfo(
        id=wt["id"],
        repository_id=wt["repository_id"],
        session_id=wt.get("session_id"),  # Deprecated
        branch_name=wt["branch_name"],
        worktree_path=wt["worktree_path"],
        base_branch=wt.get("base_branch"),
        status=wt["status"],
        created_at=wt["created_at"],
        # New model fields
        sessions=sessions_list,
        session_count=wt.get("session_count", len(sessions_list)),
        active_session=active_session_info,
        exists=wt.get("exists"),
        git_status=git_status_info,
        # Legacy field
        session=legacy_session_info
    )


# Dedicated, bounded thread pool for the per-worktree status fan-out.
#
# The fan-out runs INSIDE a single main-GitPool job (see _collect_worktree_statuses),
# so submitting the endpoint still costs exactly ONE GitPool job regardless of
# worktree count — preserving the queue-depth fix from 2b98408f. But that fix
# made the fan-out fully SEQUENTIAL: N `git status` subprocesses run back-to-back
# in one worker, so a project with 7+ worktrees took 6-9s (the observed
# git/worktrees SLOW entries). Each worktree is a DISTINCT working directory with
# its own index.lock, so concurrent `git status` across different worktrees is
# safe (this is exactly what the code did before 2b98408f, via asyncio.gather).
# We restore that parallelism but on a dedicated, bounded pool so it can never
# saturate the main 4-worker GitPool or block unrelated queued git ops.
_STATUS_FANOUT_MAX_WORKERS = 6
_status_fanout_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=_STATUS_FANOUT_MAX_WORKERS,
    thread_name_prefix="git-wt-status",
)


def _worktree_status_one(wt: dict) -> Tuple[str, Optional[dict]]:
    """Read one worktree's git status. Never raises — returns (id, status|None).

    Isolating each worktree's failure here guarantees one bad worktree (invalid
    path, deleted on disk, corrupt repo) cannot poison the whole fan-out.
    """
    wt_id = wt.get("id", "?")
    try:
        worktree_full_path = get_worktree_working_dir(wt)
    except (ValueError, TypeError) as e:
        logger.warning(f"Worktree {wt_id} has invalid path, skipping status: {e}")
        return wt_id, None
    try:
        return wt_id, git_service.get_status(worktree_full_path)
    except Exception as e:  # noqa: BLE001 — one worktree must not fail the batch
        logger.warning(f"Failed to get git status for worktree {wt_id}: {e}")
        return wt_id, None


def _collect_worktree_statuses(worktrees: list) -> dict:
    """Compute git status for every existing worktree in ONE GitPool job.

    Submitting the /git/worktrees endpoint still costs a single main-GitPool job
    regardless of worktree count (the 2b98408f invariant, guarded by tests). The
    per-worktree ``git status`` calls now run CONCURRENTLY on a dedicated bounded
    pool (``_status_fanout_executor``) instead of sequentially, cutting the
    endpoint's wall time from ``N * per_status`` down to roughly
    ``ceil(N / _STATUS_FANOUT_MAX_WORKERS) * per_status``. ``git_service.get_status``
    keeps its own short-TTL per-dir cache, so most calls here are cheap hits.

    Returns a map of ``worktree_id -> status dict`` (or ``None`` when the status
    could not be read); worktrees that don't exist on disk are skipped entirely,
    preserving the previous behaviour of never setting ``git_status`` for them.
    """
    existing = [wt for wt in worktrees if wt.get("exists", True)]
    if not existing:
        return {}

    statuses: dict = {}
    futures = [_status_fanout_executor.submit(_worktree_status_one, wt) for wt in existing]
    for fut in concurrent.futures.as_completed(futures):
        # _worktree_status_one never raises, so fut.result() is safe.
        wt_id, status_data = fut.result()
        statuses[wt_id] = status_data
    return statuses


# Project-level TTL cache for the whole worktree-status fan-out.
#
# Multiple UI surfaces (sessions board + worktree panel, and every open tab)
# poll /git/worktrees on a ~2s cadence, so bursts of concurrent/near-simultaneous
# requests each re-ran the fan-out and re-submitted a GitPool job. Mirroring the
# "check the TTL cache BEFORE submitting pool work" pattern (PG-pool #531/#532),
# a short project-level cache lets a burst share one fan-out result and skip the
# GitPool submit entirely on a hit. TTL matches the status poll cadence so the UI
# stays fresh. Invalidated on any worktree create/delete/sync mutation.
_WORKTREE_STATUS_CACHE_TTL = 2.0  # seconds
_worktree_status_cache: Dict[str, Tuple[dict, float]] = {}


def _peek_worktree_status_cache(project_id: str) -> Optional[dict]:
    """Return the cached fan-out result for *project_id* if still fresh, else None."""
    entry = _worktree_status_cache.get(project_id)
    if entry and (time.monotonic() - entry[1]) < _WORKTREE_STATUS_CACHE_TTL:
        return entry[0]
    return None


def _invalidate_worktree_status_cache(project_id: str) -> None:
    """Drop any cached fan-out result for *project_id* (call after mutations)."""
    _worktree_status_cache.pop(project_id, None)


def _copy_uncommitted_files(main_dir: str, worktree_path: str) -> dict:
    """
    Copy uncommitted files (modified and untracked) from main repo to worktree.

    This allows users to continue working on uncommitted changes in a worktree
    without having to commit or stash first.

    Args:
        main_dir: Path to the main repository
        worktree_path: Path to the worktree

    Returns:
        Dict with copied file counts and any errors
    """
    import shutil

    result = {
        "modified_copied": 0,
        "untracked_copied": 0,
        "errors": []
    }

    try:
        # Get git status to find uncommitted files
        status = git_service.get_status(main_dir)

        main_path = Path(main_dir)
        worktree_abs_path = Path(worktree_path)

        # Copy modified files (staged and unstaged)
        modified_files = status.get("modified", [])
        # Also include staged files that might be renamed or added
        staged_files = [s.get("file") for s in status.get("staged", []) if s.get("file")]
        all_modified = set(modified_files + staged_files)

        for file_path in all_modified:
            try:
                src = main_path / file_path
                dest = worktree_abs_path / file_path

                if src.exists():
                    # Ensure parent directory exists
                    dest.parent.mkdir(parents=True, exist_ok=True)

                    if src.is_file():
                        shutil.copy2(src, dest)
                        result["modified_copied"] += 1
                    elif src.is_dir():
                        if dest.exists():
                            shutil.rmtree(dest)
                        shutil.copytree(src, dest)
                        result["modified_copied"] += 1
            except Exception as e:
                result["errors"].append(f"Failed to copy modified file {file_path}: {str(e)}")

        # Copy untracked files
        untracked_files = status.get("untracked", [])
        for file_path in untracked_files:
            try:
                src = main_path / file_path
                dest = worktree_abs_path / file_path

                if src.exists():
                    # Ensure parent directory exists
                    dest.parent.mkdir(parents=True, exist_ok=True)

                    if src.is_file():
                        shutil.copy2(src, dest)
                        result["untracked_copied"] += 1
                    elif src.is_dir():
                        if dest.exists():
                            shutil.rmtree(dest)
                        shutil.copytree(src, dest)
                        result["untracked_copied"] += 1
            except Exception as e:
                result["errors"].append(f"Failed to copy untracked file {file_path}: {str(e)}")

        logger.debug(
            f"Copied uncommitted files to worktree: "
            f"{result['modified_copied']} modified, {result['untracked_copied']} untracked"
        )

    except Exception as e:
        result["errors"].append(f"Failed to get git status: {str(e)}")

    return result


def _require_worktree_for_project(
    worktree_id: str, project_id: str, *, use_details: bool = False
) -> dict:
    """Fetch a worktree and verify it belongs to the given project.

    Args:
        worktree_id: The worktree to look up.
        project_id: The project that must own the worktree.
        use_details: If True, fetch via ``worktree_manager.get_worktree_details``
                     (includes sessions/git info). Otherwise use the lighter
                     ``database.get_worktree``.

    Returns:
        The worktree dict.

    Raises:
        HTTPException 404 if the worktree does not exist or does not belong
        to *project_id*.
    """
    if use_details:
        worktree = worktree_manager.get_worktree_details(worktree_id)
    else:
        worktree = database.get_worktree(worktree_id)

    if not worktree:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Worktree not found: {worktree_id}",
        )

    repo = database.get_git_repository_by_project(project_id)
    if not repo or worktree["repository_id"] != repo["id"]:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Worktree not found in this project: {worktree_id}",
        )

    return worktree


def _get_file_extension(name: str) -> str:
    """Get lowercase file extension without the dot"""
    ext = Path(name).suffix.lower()
    return ext[1:] if ext else ""


def _format_file_size(size_bytes: int) -> str:
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
# Worktree Endpoints
# =============================================================================

@router.get("/worktrees", response_model=WorktreeListResponse)
async def list_worktrees(
    request: Request,
    project_id: str,
    include_status: bool = True,
    token: str = Depends(require_auth)
):
    """
    List all worktrees for a project.

    Returns worktrees with their associated session information.

    Query params:
        include_status: If True (default), includes git status (dirty state) for each worktree
    """
    check_project_access(request, project_id)
    working_dir = get_project_path(project_id)

    if not await run_git(git_service.is_git_repo, working_dir):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project is not a git repository"
        )

    worktrees = worktree_manager.get_worktrees_for_project(project_id)

    # Optionally fetch git status for each existing worktree.
    # Per-worktree `git status` calls run concurrently on a bounded dedicated
    # pool inside ONE main-GitPool job (see _collect_worktree_statuses). A short
    # project-level TTL cache is checked BEFORE submitting that job so a burst of
    # UI polls shares one fan-out result and skips the pool entirely on a hit.
    if include_status:
        statuses = _peek_worktree_status_cache(project_id)
        if statuses is None:
            statuses = await run_git(_collect_worktree_statuses, worktrees)
            _worktree_status_cache[project_id] = (statuses, time.monotonic())
        for wt in worktrees:
            if wt.get("exists", True):
                wt["git_status"] = statuses.get(wt.get("id"))

    worktree_infos = [_convert_worktree_to_info(wt) for wt in worktrees]

    return WorktreeListResponse(
        worktrees=worktree_infos,
        total=len(worktree_infos)
    )


@router.get("/worktrees/{worktree_id}", response_model=WorktreeInfo)
async def get_worktree(
    request: Request,
    project_id: str,
    worktree_id: str,
    token: str = Depends(require_auth)
):
    """
    Get detailed information about a specific worktree.

    Includes session info and current git status of the worktree.
    """
    check_project_access(request, project_id)

    worktree = _require_worktree_for_project(worktree_id, project_id, use_details=True)

    return _convert_worktree_to_info(worktree)


@router.delete("/worktrees/{worktree_id}", response_model=OperationResponse)
async def delete_worktree(
    request: Request,
    project_id: str,
    worktree_id: str,
    keep_branch: bool = True,
    token: str = Depends(require_auth)
):
    """
    Remove a worktree and optionally delete its branch.

    Args:
        project_id: Project ID
        worktree_id: Worktree ID to remove
        keep_branch: If true (default), keep the branch after removing worktree
    """
    check_project_access(request, project_id)

    worktree = _require_worktree_for_project(worktree_id, project_id)

    success = await worktree_manager.cleanup_worktree(worktree_id, keep_branch=keep_branch)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to remove worktree"
        )

    # The removed worktree changes the fan-out result — drop the cached status.
    _invalidate_worktree_status_cache(project_id)
    # The checkout no longer exists, so any memoized path for this project's
    # sessions/worktrees is now wrong — drop it rather than serving a dead path.
    invalidate_working_dir_cache(project_id=project_id)

    branch_msg = " (branch kept)" if keep_branch else " (branch deleted)"
    return OperationResponse(
        success=True,
        message=f"Removed worktree for branch '{worktree['branch_name']}'{branch_msg}"
    )


@router.post("/worktrees/{worktree_id}/reopen", response_model=WorktreeInfo)
async def reopen_worktree(
    request: Request,
    project_id: str,
    worktree_id: str,
    token: str = Depends(require_auth)
):
    """
    Recreate a completed worktree so the user can resume the session.

    Creates a new git worktree with the same branch name (new branch from
    default branch if the old branch was deleted). Updates the DB record
    back to ACTIVE.
    """
    check_project_access(request, project_id)

    worktree = _require_worktree_for_project(worktree_id, project_id)

    try:
        updated = worktree_manager.recreate_worktree(worktree_id)
    except WorktreeError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )

    # A cross-device reopen rebinds the worktree onto THIS install's clone of the
    # repo (updated["repository_id"] now points at the local repo). recreate_worktree
    # only re-points the single resumed session when called from the send path with
    # a rebind_session_id — a MANUAL reopen has none, and afterwards the send-path
    # self-heal can NEVER fire (the cwd now exists locally, so every rebind gate is
    # skipped). Left alone, the sessions' project_id stays foreign permanently and
    # MCP servers resolve the wrong project forever. Broad repointing is correct
    # here: reopen is an explicit manual relocation of the whole worktree, unlike
    # the send path which stays scoped to one session (PR #493 follow-up #2).
    # Reopen relocates the whole worktree: the fan-out result and any memoized
    # working dir for this project both describe the old (deleted) checkout.
    # (Both are in-memory caches, so invalidating them before the DB block
    # below rather than in the middle of it changes nothing.)
    _invalidate_worktree_status_cache(project_id)
    invalidate_working_dir_cache(project_id=project_id)

    def _repoint_sessions() -> list:
        """Resolve the local project and repoint this worktree's sessions.

        READ-DECIDE-WRITE, kept atomic in ONE hop: the local repo lookup, the
        session fan-out and the per-session ``set_session_project`` decision all
        depend on each other. Split across separate thread hops, a session
        created or re-parented between the read and the write would be missed or
        repointed on stale data — a race that does not exist today.

        Returns the session ids (in fan-out order) so the caller can evict their
        pooled subprocesses, which must happen ON the loop.
        """
        local_pid: Optional[str] = None
        local_repo = database.get_git_repository(updated["repository_id"])
        if local_repo:
            local_pid = local_repo.get("project_id")

        sessions = database.get_sessions_for_worktree(worktree_id)
        for sess in sessions:
            sid = sess["id"]
            if local_pid and sess.get("project_id") != local_pid:
                database.set_session_project(sid, local_pid)
                logger.info(
                    f"Repointed session {sid} to local project {local_pid} after worktree reopen"
                )
        return [sess["id"] for sess in sessions]

    session_ids = await run_sync(_repoint_sessions)

    # Evict stale pooled subprocesses whose CWD pointed to the old (deleted) worktree.
    # Must be done here (async context) since recreate_worktree is sync.
    from app.core.session_pool import get_session_pool
    pool = get_session_pool()
    for sid in session_ids:
        if pool.get(sid):
            await pool.remove(sid)
            logger.debug(f"Evicted pooled session {sid} after worktree reopen")

    return _convert_worktree_to_info(updated)


@router.post("/worktrees/sync", response_model=WorktreeSyncResponse)
async def sync_worktrees(
    request: Request,
    project_id: str,
    token: str = Depends(require_auth)
):
    """
    Synchronize database worktree records with actual git worktrees.

    This cleans up orphaned records and marks worktrees that no longer exist.
    """
    check_project_access(request, project_id)
    working_dir = get_project_path(project_id)

    if not await run_git(git_service.is_git_repo, working_dir):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project is not a git repository"
        )

    result = await run_git(worktree_manager.sync_worktrees, project_id)

    # Sync can add/remove/import worktrees — drop the cached fan-out status.
    _invalidate_worktree_status_cache(project_id)
    # Sync moves checkouts around, so memoized working dirs may be stale.
    invalidate_working_dir_cache(project_id=project_id)

    return WorktreeSyncResponse(
        synced=result.get("synced", 0),
        orphaned=result.get("orphaned", 0),
        cleaned_up=result.get("cleaned_up", 0),
        imported=result.get("imported", 0),
        errors=result.get("errors", [])
    )


# =============================================================================
# Worktree File Browsing
# =============================================================================

@router.get("/worktrees/{worktree_id}/files", response_model=WorktreeFilesResponse)
async def list_worktree_files(
    request: Request,
    project_id: str,
    worktree_id: str,
    path: str = "",
    show_hidden: bool = False,
    token: str = Depends(require_auth)
):
    """
    List files within a worktree directory.

    This allows browsing files in a worktree's working directory,
    which is separate from the main project directory.

    Args:
        project_id: Project ID
        worktree_id: Worktree ID to browse files in
        path: Relative path within the worktree (empty for root)
        show_hidden: Whether to show hidden files (starting with .)

    Returns:
        List of files in the specified directory within the worktree
    """
    check_project_access(request, project_id)

    worktree = _require_worktree_for_project(worktree_id, project_id)

    # Build full path to worktree directory
    worktree_base = Path(get_worktree_working_dir(worktree))

    if not worktree_base.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Worktree directory not found on disk"
        )

    # Build target path within worktree
    if path:
        # Validate path doesn't escape worktree boundary
        target_path = (worktree_base / path).resolve()
        try:
            target_path.relative_to(worktree_base.resolve())
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Path escapes worktree boundary"
            )
    else:
        target_path = worktree_base

    if not target_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Directory not found"
        )

    if not target_path.is_dir():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Path is not a directory"
        )

    # List directory contents
    files = []
    for item in target_path.iterdir():
        # Skip hidden files unless requested
        if item.name.startswith('.') and not show_hidden:
            continue

        # Never surface nested git worktree checkouts (.fluid/worktrees/... or
        # legacy .claude/worktrees/...), which are app internals rather than real
        # project content, even when show_hidden is requested.
        try:
            rel_to_base = item.relative_to(worktree_base)
            if os.path.normpath(str(rel_to_base)) in (
                os.path.join(".fluid", "worktrees"),
                os.path.join(".claude", "worktrees"),
            ):
                continue
        except ValueError:
            pass

        try:
            stat_info = item.stat()
            is_dir = item.is_dir()

            file_info = WorktreeFileInfo(
                name=item.name,
                type="directory" if is_dir else "file",
                size=stat_info.st_size if not is_dir else None,
                sizeFormatted=_format_file_size(stat_info.st_size) if not is_dir else None,
                path=str(item.relative_to(worktree_base)),
                extension=_get_file_extension(item.name) if not is_dir else None,
                modifiedAt=datetime.fromtimestamp(stat_info.st_mtime).isoformat(),
                createdAt=datetime.fromtimestamp(stat_info.st_ctime).isoformat(),
            )
            files.append(file_info)
        except (OSError, PermissionError):
            # Skip files we can't access
            continue

    # Sort: directories first, then files alphabetically
    files.sort(key=lambda x: (x.type != "directory", x.name.lower()))

    return WorktreeFilesResponse(
        files=files,
        path=path,
        worktreeId=worktree_id,
        worktreePath=worktree["worktree_path"]
    )
