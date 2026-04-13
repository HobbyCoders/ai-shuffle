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
    if kwargs:
        fn = functools.partial(fn, *args, **kwargs)
        return await loop.run_in_executor(_git_executor, fn)
    return await loop.run_in_executor(_git_executor, fn, *args)


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

def get_project_path(project_id: str, allow_missing: bool = False) -> Optional[str]:
    """Get the filesystem path for a project.

    .. deprecated:: Use :func:`app.api.common.resolve_project_path` instead.
    """
    result = resolve_project_path(
        project_id, check_access=False, allow_missing=allow_missing
    )
    return str(result) if result is not None else None


def get_working_dir(project_id: str, worktree_id: Optional[str] = None) -> str:
    """
    Get the working directory for git operations.

    When worktree_id is provided, returns the worktree's path.
    Otherwise returns the main project path.
    """
    if worktree_id:
        worktree = database.get_worktree(worktree_id)
        if not worktree:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Worktree not found: {worktree_id}"
            )
        return get_worktree_working_dir(worktree)

    return get_project_path(project_id)


# In-memory cache for git repository records to avoid DB+git calls on every poll.
# Maps project_id -> (record, timestamp). TTL = 60s.
_repo_record_cache: Dict[str, Tuple[Optional[dict], float]] = {}
_REPO_RECORD_TTL = 60.0  # seconds


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
    _repo_record_cache[project_id] = (repo, time.monotonic())
    return repo
