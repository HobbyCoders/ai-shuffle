"""
Git helper utilities shared across git sub-modules.

Provides the dedicated git thread pool, request coalescing, path resolution,
and repository record management used by git.py, git_worktrees.py, and git_ai.py.
"""

import asyncio
import concurrent.futures
import time
import uuid
import logging
from typing import Any, Callable, Dict, Optional, Tuple

from fastapi import HTTPException, status

from app.core.git_service import git_service
from app.core.github_service import CACHE_MISS, TTLCache
from app.core.project_utils import get_worktree_working_dir
from app.db import database
from app.api.common import resolve_project_path

logger = logging.getLogger(__name__)


# =============================================================================
# Dedicated Git Thread Pool & Request Coalescing
# =============================================================================
# Git subprocess calls (git status, git diff, etc.) are I/O-heavy and can take
# 5-10+ seconds on large repos.  Running them in the default asyncio executor
# starves other async work.  A dedicated pool with a small worker count also
# prevents concurrent git calls from competing for the same index.lock.

_git_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="git-worker",
)


async def run_git(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a blocking git function in the dedicated git thread pool.

    Supports keyword arguments via functools.partial (run_in_executor only
    accepts positional args natively).
    """
    import functools

    loop = asyncio.get_running_loop()

    # Track queue depth and timing for slowdown diagnostics
    pending = _git_executor._work_queue.qsize()
    fn_name = getattr(fn, '__name__', str(fn))
    if pending > 2:
        logger.warning(f"[GitPool] queue depth={pending} before running {fn_name}")

    start = time.monotonic()
    try:
        if kwargs:
            fn = functools.partial(fn, *args, **kwargs)
            result = await loop.run_in_executor(_git_executor, fn)
        else:
            result = await loop.run_in_executor(_git_executor, fn, *args)
        elapsed = time.monotonic() - start
        if elapsed > 5.0:
            logger.warning(f"[GitPool] {fn_name} took {elapsed:.1f}s (queue_depth_at_submit={pending})")
        return result
    except Exception:
        elapsed = time.monotonic() - start
        logger.warning(f"[GitPool] {fn_name} FAILED after {elapsed:.1f}s")
        raise


class _RequestCoalescer:
    """Coalesce concurrent requests for the same key into a single execution.

    When multiple polls arrive while a git operation is already in-flight,
    they all await the SAME future instead of queuing more thread-pool work.
    This is the primary mechanism that prevents thread-pool saturation.
    """

    def __init__(self) -> None:
        self._in_flight: Dict[str, asyncio.Future] = {}

    async def get_or_run(
        self, key: str, fn: Callable[..., Any], *args: Any
    ) -> Any:
        """Return an in-flight result if one exists, otherwise execute *fn*."""
        if key in self._in_flight:
            return await self._in_flight[key]

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._in_flight[key] = future

        try:
            result = await run_git(fn, *args)
            future.set_result(result)
            return result
        except Exception as exc:
            future.set_exception(exc)
            raise
        finally:
            self._in_flight.pop(key, None)


_coalescer = _RequestCoalescer()


# =============================================================================
# Helper Functions
# =============================================================================

async def stage_and_commit(
    working_dir: str, message: str, include_unstaged: bool
) -> Tuple[bool, Optional[str], Optional[str]]:
    """Stage (optionally everything) and commit — shared by the ``/commit``
    endpoint and the Ship-It flow so both enforce the same rules.

    When *include_unstaged* is True, stages all changes first. When False,
    requires pre-staged changes (mirrors ``create_commit``'s "no staged changes"
    validation) instead of silently producing an empty commit.

    Returns ``(ok, sha_or_error, reason)``:
    * success -> ``(True, <commit sha>, None)``
    * failure -> ``(False, <error or None>, reason)`` where reason is one of
      ``"stage_failed"``, ``"no_staged"``, ``"commit_failed"`` so each caller can
      map it to its own error surface (HTTP 400 vs. an op step error).
    """
    if include_unstaged:
        if not await run_git(git_service.stage_files, working_dir, ["."]):
            return False, None, "stage_failed"
    else:
        status_data = await run_git(git_service.get_status, working_dir)
        if not status_data.get("staged"):
            return False, None, "no_staged"

    ok, sha_or_error = await run_git(git_service.commit, working_dir, message)
    if not ok:
        return False, sha_or_error, "commit_failed"
    return True, sha_or_error, None


def get_project_path(project_id: str, allow_missing: bool = False) -> Optional[str]:
    """Get the filesystem path for a project.

    .. deprecated:: Use :func:`app.api.common.resolve_project_path` instead.
    """
    result = resolve_project_path(
        project_id, check_access=False, allow_missing=allow_missing
    )
    return str(result) if result is not None else None


# ---------------------------------------------------------------------------
# Working-directory resolution (cached)
# ---------------------------------------------------------------------------
# Resolving (project_id, worktree_id, session_id) -> checkout path costs 1-2
# synchronous DB round trips plus a filesystem stat, and every hot git endpoint
# does it on every poll (~2s cadence per open tab, finding G9). The mapping is
# stable for the lifetime of a session, so it is cached with a short TTL and an
# explicit invalidation hook for the flows that change it.
_WORKING_DIR_TTL = 30.0
# A resolution that FELL BACK to the project path because the session's checkout
# does not exist yet is provisional — the checkout may be materialized moments
# later — so it is held only briefly.
_WORKING_DIR_FALLBACK_TTL = 5.0

_working_dir_cache = TTLCache(
    ttl=_WORKING_DIR_TTL, max_entries=512, name="working-dir"
)


def _resolve_working_dir(
    project_id: str,
    worktree_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Tuple[str, bool]:
    """Uncached resolution. Returns ``(path, resolved_from_checkout)``.

    Blocking: reads the database and stats the filesystem.
    """
    if worktree_id:
        worktree = database.get_worktree(worktree_id)
        if not worktree:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Worktree not found: {worktree_id}"
            )
        return get_worktree_working_dir(worktree), True

    if session_id:
        from app.core.project_resolver import resolve_existing_cwd

        session = database.get_session(session_id)
        if session:
            cwd = resolve_existing_cwd(session)
            if cwd:
                return cwd, True
            if session.get("project_v2_id"):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "This session's checkout is not available on this device. "
                        "Resume it here to prepare its local checkout, or restore "
                        "the missing checkout before retrying."
                    ),
                )

    return get_project_path(project_id), not session_id


def _working_dir_cache_key(
    project_id: str, worktree_id: Optional[str], session_id: Optional[str]
) -> Tuple[str, Optional[str], Optional[str]]:
    return (project_id, worktree_id, session_id)


def _store_working_dir(key, value: str, resolved_from_checkout: bool) -> None:
    _working_dir_cache.set(
        key,
        value,
        ttl=None if resolved_from_checkout else _WORKING_DIR_FALLBACK_TTL,
    )


def get_working_dir(
    project_id: str,
    worktree_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """
    Get the working directory for git operations.

    Resolution order:
    1. ``worktree_id`` — a legacy worktrees row's directory.
    2. ``session_id`` — a v2 branch session's own checkout (sessions.branch +
       project_v2_id), so chat-header git actions target the branch checkout
       rather than silently falling back to the base checkout.
    Otherwise returns the main project path.

    Results are memoized for a few seconds (see :data:`_WORKING_DIR_TTL`).

    This is the **synchronous** variant, kept for sync callers. Async handlers
    should await :func:`get_working_dir_async`, which does the same thing but
    hands the uncached resolution to a worker thread instead of blocking the
    event loop.
    """
    key = _working_dir_cache_key(project_id, worktree_id, session_id)
    cached = _working_dir_cache.get_or_miss(key)
    if cached is not CACHE_MISS:
        return cached

    value, from_checkout = _resolve_working_dir(project_id, worktree_id, session_id)
    _store_working_dir(key, value, from_checkout)
    return value


async def get_working_dir_async(
    project_id: str,
    worktree_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Non-blocking :func:`get_working_dir` for async handlers.

    A cache hit never touches a thread; a miss resolves in a worker so the DB
    round trip and the filesystem stat stay off the event loop.
    """
    key = _working_dir_cache_key(project_id, worktree_id, session_id)
    cached = _working_dir_cache.get_or_miss(key)
    if cached is not CACHE_MISS:
        return cached

    value, from_checkout = await asyncio.to_thread(
        _resolve_working_dir, project_id, worktree_id, session_id
    )
    _store_working_dir(key, value, from_checkout)
    return value


def invalidate_working_dir_cache(
    project_id: Optional[str] = None,
    worktree_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> None:
    """Drop memoized checkout paths.

    Call after anything that moves a session's checkout (worktree create/delete,
    session rebind). With no arguments the whole cache is dropped; otherwise
    only entries matching every supplied component are.
    """
    if project_id is None and worktree_id is None and session_id is None:
        _working_dir_cache.invalidate()
        return

    def matches(key) -> bool:
        p_id, wt_id, s_id = key
        if project_id is not None and p_id != project_id:
            return False
        if worktree_id is not None and wt_id != worktree_id:
            return False
        if session_id is not None and s_id != session_id:
            return False
        return True

    _working_dir_cache.invalidate_where(matches)


# In-memory cache for git repository records to avoid DB+git calls on every poll.
# Maps project_id -> (record, timestamp). TTL = 60s.
_repo_record_cache: Dict[str, Tuple[Optional[dict], float]] = {}
_REPO_RECORD_TTL = 60.0  # seconds

# Sentinel returned by peek_repo_record_cache on a miss. A fresh cache entry may
# legitimately be None (project is not a git repo), so None cannot signal "miss".
_CACHE_MISS = object()


def peek_repo_record_cache(project_id: str) -> Any:
    """Return the cached git_repositories record for *project_id* if still fresh.

    Returns the cached value (which may be ``None`` for a known non-repo) on a
    hit, or the module-level ``_CACHE_MISS`` sentinel when there is no fresh
    entry. This lets hot pollers short-circuit BEFORE submitting a ``run_git``
    job: ``ensure_git_repository_record`` performs the same TTL check, but only
    AFTER the executor has already accepted the job, so a cache hit still burned
    a GitPool queue/worker slot on every /git/status poll (2s cadence).
    """
    cached = _repo_record_cache.get(project_id)
    if cached and (time.monotonic() - cached[1]) < _REPO_RECORD_TTL:
        return cached[0]
    return _CACHE_MISS


def _auto_link_from_repo(project_id: str, repo: Optional[dict]) -> None:
    """Best-effort: keep the project's link_group_id derived from its git remote.

    Called whenever the git_repositories record is touched so same-repo clones on
    other installs group together on the board. Never raises into the caller.
    """
    try:
        from app.core.project_links import maybe_auto_link_project
        maybe_auto_link_project(project_id, (repo or {}).get("remote_url"))
    except Exception:  # noqa: BLE001
        logger.debug("auto-link from repo failed for %s", project_id, exc_info=True)


def ensure_git_repository_record(project_id: str, working_dir: str) -> Optional[dict]:
    """
    Ensure a git_repositories record exists for this project if it's a git repo.
    Creates one if needed, returns the record or None if not a git repo.
    Uses an in-memory cache to avoid repeated DB/git calls during polling.
    """
    now = time.monotonic()
    cached = _repo_record_cache.get(project_id)
    if cached and (now - cached[1]) < _REPO_RECORD_TTL:
        return cached[0]

    if not git_service.is_git_repo(working_dir):
        _repo_record_cache[project_id] = (None, now)
        return None

    repo = database.get_git_repository_by_project(project_id)
    if repo:
        _auto_link_from_repo(project_id, repo)
        _repo_record_cache[project_id] = (repo, now)
        return repo

    # Create a new record
    repo_id = f"repo-{uuid.uuid4().hex[:12]}"
    remote_url = git_service.get_remote_url(working_dir)
    default_branch = git_service.get_default_branch(working_dir)

    # Try to extract GitHub repo name from URL
    github_repo_name = None
    if remote_url:
        # Handle both HTTPS and SSH URLs
        # https://github.com/owner/repo.git or git@github.com:owner/repo.git
        if "github.com" in remote_url:
            try:
                if remote_url.startswith("git@"):
                    # git@github.com:owner/repo.git
                    github_repo_name = remote_url.split(":")[1].replace(".git", "")
                else:
                    # https://github.com/owner/repo.git
                    parts = remote_url.replace(".git", "").split("github.com/")
                    if len(parts) > 1:
                        github_repo_name = parts[1]
            except (IndexError, ValueError) as e:
                logger.warning(f"Failed to parse GitHub repo name from URL '{remote_url}': {e}")

    repo = database.create_git_repository(
        repository_id=repo_id,
        project_id=project_id,
        remote_url=remote_url,
        default_branch=default_branch,
        github_repo_name=github_repo_name
    )
    logger.debug(f"Created git repository record for project {project_id}: {repo_id}")
    _auto_link_from_repo(project_id, repo)
    _repo_record_cache[project_id] = (repo, time.monotonic())
    return repo
