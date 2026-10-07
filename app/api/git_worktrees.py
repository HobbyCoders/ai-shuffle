"""
Git worktree API endpoints.

Provides endpoints for worktree management including:
- List, get, delete, reopen worktrees
- Sync worktree records with git
- Browse files within worktrees
"""

import asyncio
import concurrent.futures
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple, Union

from fastapi import APIRouter, HTTPException, Depends, Request, status

from app.core import git_native
from app.core.git_service import git_service
from app.core.worktree_manager import worktree_manager, WorktreeError
from app.core.project_utils import get_worktree_working_dir
from app.db import database
from app.db.async_database import run_sync
from app.api.auth import require_admin, require_auth
from app.api.common import check_project_access, format_file_size, get_file_extension
from app.api.git_helpers import run_git, get_project_path, invalidate_working_dir_cache

from app.api.schemas.git import (
    StagedFile,
    SessionInfo,
    WorktreeGitStatus,
    WorktreeInfo,
    WorktreeListResponse,
    WorktreeCompactListResponse,
    WorktreeStatusSummary,
    WorktreeSummary,
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
        checkout_id=wt.get("checkout_id"),
        resource_kind=wt.get("resource_kind", "worktree"),
        legacy_lifecycle_supported=wt.get("legacy_lifecycle_supported", True),
        project_id=wt.get("project_id"),
        runner_id=wt.get("runner_id"),
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
        status_checked_at=wt.get("status_checked_at"),
        status_error=wt.get("status_error"),
        # Legacy field
        session=legacy_session_info
    )


def _convert_worktree_to_summary(wt: dict) -> WorktreeSummary:
    """Compact row: identity, branch and dirty counts (see WorktreeSummary)."""
    gs = wt.get("git_status")
    return WorktreeSummary(
        id=wt["id"],
        resource_kind=wt.get("resource_kind", "worktree"),
        branch_name=wt["branch_name"],
        base_branch=wt.get("base_branch"),
        exists=wt.get("exists"),
        git_status=WorktreeStatusSummary(
            is_clean=gs.get("is_clean", True),
            modified_count=len(gs.get("modified", [])),
            staged_count=len(gs.get("staged", [])),
            untracked_count=len(gs.get("untracked", [])),
        ) if gs else None,
        status_error=wt.get("status_error"),
    )


def _session_may_be_hot(streaming: set, active_since: float):
    """Session filter for the compact list: keep only sessions that can make
    their worktree hot (see _is_hot). Mapping every historical session to its
    checkout costs a DB lookup and a HEAD read each, and only hotness uses it."""
    def keep(session: dict) -> bool:
        if session.get("id") in streaming:
            return True
        updated = _to_epoch(session.get("updated_at"))
        return updated is not None and updated >= active_since
    return keep


# Dedicated, bounded thread pool for the per-worktree status fan-out.
#
# 2b98408f collapsed a per-worktree run_git fan-out into ONE main-GitPool job so
# a big project could not flood the 4-worker pool; a later change ran the reads
# concurrently on this pool, but still from inside that one GitPool job. With
# ~60 worktrees the job held a main git worker for ~9s per poll, and git/status,
# diff-stats and branches queued behind it. The fan-out now runs entirely off the
# main pool: a refresh coordinator (_status_refresh_executor) submits the reads
# here, so the endpoint holds ZERO GitPool jobs across the fan-out. Each
# worktree is a distinct working directory with its own index.lock, so
# concurrent reads across worktrees are safe.
_STATUS_FANOUT_MAX_WORKERS = 6
_status_fanout_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=_STATUS_FANOUT_MAX_WORKERS,
    thread_name_prefix="git-wt-status",
)

# Coordinators block on the fan-out pool, so they get threads of their own (a
# coordinator running ON the fan-out pool could starve its own reads). Two
# covers two projects refreshing at once; a third queues.
_status_refresh_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="git-wt-refresh",
)

# Stale-while-revalidate for the worktree status map.
#
# The endpoint always answers from the last known statuses. Once they are older
# than _WORKTREE_STATUS_FRESH_SECONDS it starts ONE background refresh for the
# project; concurrent polls share it rather than starting their own. The UI
# polls every ~2s, so a hot worktree's status lags by at most one window plus
# one refresh.
_WORKTREE_STATUS_FRESH_SECONDS = 5.0
# A cold project (no refresh has finished since boot or a full invalidation)
# waits up to this long for its first refresh so the first response is not all
# blank. Focused and hot worktrees are read first, so they are normally in by
# then; the rest fill in on later polls.
_WORKTREE_STATUS_COLD_WAIT_SECONDS = 3.0
# A refresh re-reads only "hot" worktrees: one the client names in
# ?focus_worktree_id=, one with a streaming session, or one whose sessions saw
# activity within this window. Idle worktrees keep their last known status; a
# worktree with no status yet is read once.
_WORKTREE_HOT_ACTIVITY_SECONDS = 3 * 60 * 60
# Per-worktree failure back-off: after the Nth consecutive failed read the
# worktree is skipped for min(BASE * 2**(N-1), MAX) seconds. A corrupt worktree
# (e.g. a zeroed index) otherwise fails the same way on every poll.
_WORKTREE_STATUS_BACKOFF_BASE_SECONDS = 30.0
_WORKTREE_STATUS_BACKOFF_MAX_SECONDS = 30 * 60.0


@dataclass
class _WorktreeStatusEntry:
    """Last known status of one worktree, plus its failure back-off."""
    status: Optional[dict] = None
    checked_at: Optional[str] = None  # ISO wall-clock time of the last read
    error: Optional[str] = None
    failures: int = 0
    retry_at: float = 0.0  # monotonic; no reads before this


@dataclass
class _ProjectStatusState:
    entries: Dict[str, _WorktreeStatusEntry] = field(default_factory=dict)
    refreshed_at: Optional[float] = None  # monotonic; None until a refresh ends
    inflight: Optional[concurrent.futures.Future] = None
    # A per-worktree invalidation bumps the generation; a refresh that started
    # before it must not write its (now stale) read for that worktree back.
    generation: int = 0
    invalidated_gen: Dict[str, int] = field(default_factory=dict)


# project_id -> status state. Refresh coordinators write from their own threads
# while request handlers read on the loop, so every access holds _status_lock.
_worktree_status_cache: Dict[str, _ProjectStatusState] = {}
_status_lock = threading.Lock()


def _worktree_status_one(wt: dict) -> Tuple[Optional[dict], Optional[str]]:
    """Read one worktree's git status. Never raises: returns (status, error).

    Exactly one of the two is set. Isolating each worktree's failure here keeps
    one bad worktree (invalid path, deleted on disk, corrupt repo) from
    poisoning the batch; the caller logs it and backs off.
    """
    try:
        worktree_full_path = get_worktree_working_dir(wt)
    except (ValueError, TypeError) as e:
        return None, f"invalid worktree path: {e}"
    try:
        from app.core.checkout_manager import _require_branch_binding

        if wt.get("branch_name"):
            _require_branch_binding(worktree_full_path, wt["branch_name"])
        status_data = git_service.get_status(worktree_full_path)
    except Exception as e:  # noqa: BLE001 — one worktree must not fail the batch
        return None, str(e) or type(e).__name__
    # The pygit2 reader swallows a failed read and returns a partial "clean"
    # dict; it records the error on the side so we don't show that as real.
    native_error = git_native.get_status_error(worktree_full_path)
    if native_error:
        return None, native_error
    return status_data, None


def _record_status(
    entry: _WorktreeStatusEntry, wt_id: str,
    status_data: Optional[dict], error: Optional[str],
) -> None:
    """Store one read result and advance or clear the back-off (lock held).

    Logs on transitions only: the first failure, and recovery.
    """
    entry.checked_at = datetime.now(timezone.utc).isoformat()
    if error is None:
        if entry.failures:
            logger.info(
                f"Worktree {wt_id} git status recovered after {entry.failures} failed read(s)"
            )
        entry.status, entry.error, entry.failures, entry.retry_at = status_data, None, 0, 0.0
        return
    entry.failures += 1
    delay = min(
        _WORKTREE_STATUS_BACKOFF_BASE_SECONDS * 2 ** min(entry.failures - 1, 16),
        _WORKTREE_STATUS_BACKOFF_MAX_SECONDS,
    )
    entry.status, entry.error, entry.retry_at = None, error, time.monotonic() + delay
    if entry.failures == 1:
        logger.warning(
            f"Worktree {wt_id} git status failed ({error}); skipping it for {delay:.0f}s, "
            "backing off further while it keeps failing"
        )
    else:
        logger.debug(f"Worktree {wt_id} git status failed again ({error}); next try in {delay:.0f}s")


def _refresh_project_statuses(state: _ProjectStatusState, jobs: list, start_gen: int) -> None:
    """Read *jobs* on the fan-out pool and fold the results into *state*.

    Runs on _status_refresh_executor, never on the main GitPool. Each result is
    stored as soon as its read finishes, so a cold request that stops waiting
    early still serves whatever is in.
    """
    try:
        futures = {
            _status_fanout_executor.submit(_worktree_status_one, wt): wt["id"] for wt in jobs
        }
        for fut in concurrent.futures.as_completed(futures):
            wt_id = futures[fut]
            # _worktree_status_one never raises, so fut.result() is safe.
            status_data, error = fut.result()
            with _status_lock:
                if state.invalidated_gen.get(wt_id, -1) > start_gen:
                    continue  # invalidated mid-read; the next refresh re-reads it
                entry = state.entries.setdefault(wt_id, _WorktreeStatusEntry())
                _record_status(entry, wt_id, status_data, error)
    except Exception:  # noqa: BLE001 — a failed refresh must still release inflight
        logger.exception("Worktree status refresh failed")
    finally:
        with _status_lock:
            state.refreshed_at = time.monotonic()
            state.inflight = None


def _streaming_session_ids() -> set:
    """Session ids streaming in this process's pool; empty if there is none.

    Reads the singleton without creating it: a status poll must not be what
    spins up the session pool.
    """
    try:
        from app.core import session_pool as _session_pool_mod
        pool = _session_pool_mod._session_pool
        return set(pool.streaming_session_ids()) if pool is not None else set()
    except Exception:  # noqa: BLE001 — hotness is advisory
        return set()


def _to_epoch(value) -> Optional[float]:
    """Parse a DB timestamp (datetime or ISO string; naive means UTC)."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    return None


def _is_hot(wt: dict, streaming: set, active_since: float) -> bool:
    """True if a session on *wt* is streaming or was active since *active_since*."""
    for session in wt.get("sessions") or []:
        if session.get("id") in streaming:
            return True
        updated = _to_epoch(session.get("updated_at"))
        if updated is not None and updated >= active_since:
            return True
    return False


def _plan_refresh(
    state: _ProjectStatusState, existing: list, focus_worktree_id: Optional[str], now: float
) -> list:
    """Pick the worktrees the next refresh reads, focused/hot first (lock held).

    A worktree never read yet is always picked. Once the data is stale, so are
    focused and hot worktrees, and broken ones whose back-off has run out.
    Anything still backing off is skipped, even when hot.
    """
    stale = state.refreshed_at is None or now - state.refreshed_at >= _WORKTREE_STATUS_FRESH_SECONDS
    streaming = _streaming_session_ids() if stale else set()
    active_since = time.time() - _WORKTREE_HOT_ACTIVITY_SECONDS
    focused, hot, other = [], [], []
    for wt in existing:
        entry = state.entries.get(wt["id"])
        is_focus = wt["id"] == focus_worktree_id
        if entry is None:
            (focused if is_focus else other).append(wt)
        elif not stale or entry.retry_at > now:
            continue
        elif entry.failures:
            other.append(wt)
        elif is_focus:
            focused.append(wt)
        elif _is_hot(wt, streaming, active_since):
            hot.append(wt)
    return focused + hot + other


async def _load_worktree_statuses(
    project_id: str, worktrees: list, focus_worktree_id: Optional[str] = None
) -> None:
    """Fill ``git_status`` (and the status fields) on each existing worktree.

    Serves the last known statuses and starts at most one background refresh
    per project when they are stale (see _WORKTREE_STATUS_FRESH_SECONDS). Only a
    cold project waits for that refresh, and for at most
    _WORKTREE_STATUS_COLD_WAIT_SECONDS.
    """
    for wt in worktrees:
        if wt.get("exists") is False:
            wt["git_status"] = None
            wt["status_error"] = "directory missing or not its own Git working tree; needs cleanup"
    existing = [wt for wt in worktrees if wt.get("exists", True) and wt.get("id")]
    now = time.monotonic()
    with _status_lock:
        state = _worktree_status_cache.get(project_id)
        if state is None:
            state = _worktree_status_cache[project_id] = _ProjectStatusState()
        # Worktrees gone from the list (deleted, or missing on disk) drop out.
        live_ids = {wt["id"] for wt in existing}
        for gone in [wt_id for wt_id in state.entries if wt_id not in live_ids]:
            del state.entries[gone]
        inflight = state.inflight
        if inflight is None:
            jobs = _plan_refresh(state, existing, focus_worktree_id, now)
            if jobs:
                inflight = state.inflight = _status_refresh_executor.submit(
                    _refresh_project_statuses, state, jobs, state.generation
                )
            elif state.refreshed_at is None or now - state.refreshed_at >= _WORKTREE_STATUS_FRESH_SECONDS:
                state.refreshed_at = now  # nothing hot to read: the data is current
        cold = state.refreshed_at is None

    if cold and inflight is not None:
        try:
            await asyncio.wait_for(
                asyncio.shield(asyncio.wrap_future(inflight)),
                timeout=_WORKTREE_STATUS_COLD_WAIT_SECONDS,
            )
        except asyncio.TimeoutError:
            pass  # serve what has landed; later polls pick up the rest

    with _status_lock:
        for wt in existing:
            entry = state.entries.get(wt["id"])
            wt["git_status"] = entry.status if entry else None
            wt["status_checked_at"] = entry.checked_at if entry else None
            wt["status_error"] = entry.error if entry else None


def _invalidate_worktree_status_cache(project_id: str, worktree_id: Optional[str] = None) -> None:
    """Forget cached status after a mutation.

    With *worktree_id*, only that worktree is dropped: the next poll reads it
    afresh and its back-off starts over, while the rest of the project keeps
    serving. Without one, the whole project goes cold.
    """
    with _status_lock:
        if worktree_id is None:
            _worktree_status_cache.pop(project_id, None)
            return
        state = _worktree_status_cache.get(project_id)
        if state is None:
            return
        state.entries.pop(worktree_id, None)
        state.generation += 1
        state.invalidated_gen[worktree_id] = state.generation


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


# =============================================================================
# Worktree Endpoints
# =============================================================================

@router.get("/worktree-report")
async def worktree_report(project_id: str, _=Depends(require_admin)):
    """Report-only: which of this project's worktrees can be removed safely, and why not."""
    from app.core.worktree_report import report

    return await run_sync(report, project_id)


@router.get("/worktrees", response_model=Union[WorktreeListResponse, WorktreeCompactListResponse])
async def list_worktrees(
    request: Request,
    project_id: str,
    include_status: bool = True,
    focus_worktree_id: Optional[str] = None,
    compact: bool = False,
    token: str = Depends(require_auth)
):
    """
    List all worktrees for a project.

    Returns worktrees with their associated session information.

    Query params:
        include_status: If True (default), includes git status (dirty state) for each worktree
        focus_worktree_id: The worktree the client has open; its status is kept
            as fresh as a busy worktree's
        compact: Return only worktrees that exist on disk, as WorktreeSummary
            rows (no session history, paths or file lists), and skip mapping
            sessions that cannot affect status freshness. The branch picker
            uses this; the full shape stays the default.
    """
    from app.api.common import require_origin_access
    from app.db.projects_v2 import get_project_v2

    if await run_sync(get_project_v2, project_id):
        # A canonical project need not have a legacy project or even a base
        # checkout here. Listing is read-only and never materializes a checkout.
        await run_sync(require_origin_access, request=request, project_v2_id=project_id)
    else:
        check_project_access(request, project_id)
        working_dir = get_project_path(project_id)
        if not await run_git(git_service.is_git_repo, working_dir):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Project is not a git repository"
            )

    if compact:
        session_filter = _session_may_be_hot(
            _streaming_session_ids(), time.time() - _WORKTREE_HOT_ACTIVITY_SECONDS
        )
        worktrees = await run_sync(
            worktree_manager.get_worktrees_for_project, project_id, session_filter=session_filter
        )
        worktrees = [wt for wt in worktrees if wt.get("exists") is not False]
    else:
        worktrees = await run_sync(worktree_manager.get_worktrees_for_project, project_id)

    # Optionally attach git status for each existing worktree. This serves the
    # last known statuses and refreshes hot worktrees in the background, off the
    # main GitPool (see _load_worktree_statuses).
    if include_status:
        await _load_worktree_statuses(project_id, worktrees, focus_worktree_id)

    if compact:
        summaries = [_convert_worktree_to_summary(wt) for wt in worktrees]
        return WorktreeCompactListResponse(worktrees=summaries, total=len(summaries))

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

    # The removed worktree's cached status and back-off go with it.
    _invalidate_worktree_status_cache(project_id, worktree_id)
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

    _require_worktree_for_project(worktree_id, project_id)

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
    # Reopen relocates the whole worktree: its cached status (and back-off) and
    # any memoized working dir for this project describe the old checkout.
    # (Both are in-memory caches, so invalidating them before the DB block
    # below rather than in the middle of it changes nothing.)
    _invalidate_worktree_status_cache(project_id, worktree_id)
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
                sizeFormatted=format_file_size(stat_info.st_size) if not is_dir else None,
                path=str(item.relative_to(worktree_base)),
                extension=get_file_extension(item.name) if not is_dir else None,
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
