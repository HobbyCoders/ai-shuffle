"""Best-effort freshness for new checkouts; never move a caller's branch."""
from __future__ import annotations

from functools import lru_cache
import logging
from pathlib import Path
import re
import subprocess
import time

from app.core import checkout_transfer
from app.core.git_service import git_service
from app.core.git_worktree_ops import repo_worktree_lock

logger = logging.getLogger(__name__)
FETCH_TIMEOUT = 5
CHAT_FETCH_TIMEOUT = 60
FETCH_COALESCE_SECONDS = 10
# Access for a key is serialized by that repository's worktree lock.
_recent_fetches: dict[tuple[str, str], float] = {}


@lru_cache(maxsize=32)
def _git_version(root: str) -> tuple[int, ...]:
    result = git_service._run_git(root, ["--version"], timeout=5)
    match = re.search(r"(\d+)\.(\d+)", result.stdout or "")
    return tuple(map(int, match.groups())) if match else (0, 0)


@lru_cache(maxsize=1)
def _log_legacy_fetch() -> None:
    logger.warning("Git older than 2.29: checkout fetch uses compatible flags and may update FETCH_HEAD")


def fetch_base(path: str, branch: str | None = None, *,
               timeout: float | None = None) -> tuple[str, bool]:
    """Fetch only the requested/default origin branch, with bounded tree cleanup.

    Successful fetches coalesce for ten seconds under the repository lock. The
    old relative git-common-dir form also works before Git 2.31. Never expose
    credential-helper output in logs or fallback receipts.
    """
    branch = branch or git_service.get_default_branch(path)
    try:
        common = git_service._run_git(path, ["rev-parse", "--git-common-dir"], timeout=5)
        if common.returncode:
            raise ValueError("Git common directory unavailable")
        root = str((Path(path) / common.stdout.strip()).resolve().parent)
        with repo_worktree_lock(root):
            key = (root, branch)
            if time.monotonic() - _recent_fetches.get(key, float("-inf")) < FETCH_COALESCE_SECONDS:
                return branch, True
            flags = ["--no-tags"]
            if _git_version(root) >= (2, 29):
                flags.append("--no-write-fetch-head")
            else:
                _log_legacy_fetch()
            # Unlike subprocess.run(capture_output=True), this runner kills
            # helper children and never drains inherited pipes after a timeout.
            result = checkout_transfer._run(
                path, "fetch", *flags, "origin",
                f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
                timeout=FETCH_TIMEOUT if timeout is None else timeout,
            )
            if result.returncode == 0:
                now = time.monotonic()
                for expired, stamp in list(_recent_fetches.items()):
                    if now - stamp >= FETCH_COALESCE_SECONDS:
                        _recent_fetches.pop(expired, None)
                _recent_fetches[key] = now
                logger.debug("Fetched checkout base origin/%s", branch)
                return branch, True
    except (subprocess.SubprocessError, OSError, ValueError):
        pass
    logger.warning("Checkout base fetch failed or timed out; using existing refs")
    return branch, False


def caller_uses_default(path: str, default: str | None = None) -> bool:
    """Only the default branch or an explicit origin/default tracking branch."""
    default = default or git_service.get_default_branch(path)
    branch = git_service._run_git(path, ["symbolic-ref", "--quiet", "--short", "HEAD"], timeout=5)
    if branch.returncode:
        return False  # Detached/unknown callers must keep their own committed tip.
    if branch.stdout.strip() == default:
        return True
    upstream = git_service._run_git(
        path, ["rev-parse", "--symbolic-full-name", "@{upstream}"], timeout=5,
    )
    return upstream.returncode == 0 and upstream.stdout.strip() == f"refs/remotes/origin/{default}"


def choose_worker_base(path: str, caller: str = "HEAD", *,
                       base_commit: str | None = None,
                       default_branch_caller: bool | None = None) -> tuple[str, str]:
    """Caller holds the project lock; peers supply eligibility checked at source."""
    if base_commit is not None:
        return base_commit, "Explicit base_commit."
    if default_branch_caller is None:
        default_branch_caller = caller_uses_default(path)
    if not default_branch_caller:
        return caller, "Caller HEAD; non-default caller branch."
    branch, fetched = fetch_base(path)
    if not fetched:
        return caller, "Caller HEAD; fetch-failed fallback."
    remote = f"origin/{branch}"
    ancestor = git_service._run_git(
        path, ["merge-base", "--is-ancestor", caller, remote], timeout=5,
    )
    if ancestor.returncode == 0:
        return remote, f"Fresh {remote}."
    behind = git_service._run_git(
        path, ["rev-list", "--count", f"{caller}..{remote}"], timeout=5,
    )
    count = behind.stdout.strip()
    if behind.returncode == 0 and count.isdigit():
        noun = "commit" if count == "1" else "commits"
        return caller, f"Caller HEAD; {count} {noun} behind {remote}."
    return caller, f"Caller HEAD; distance from {remote} unavailable."
