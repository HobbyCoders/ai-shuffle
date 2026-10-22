"""Best-effort freshness for new checkouts; never move a caller's branch."""
from __future__ import annotations

import logging
from pathlib import Path
import subprocess

from app.core.git_service import git_service
from app.core.git_worktree_ops import repo_worktree_lock

logger = logging.getLogger(__name__)
FETCH_TIMEOUT = 5


def fetch_base(path: str, branch: str | None = None) -> tuple[str, bool]:
    """Fetch only the requested/default origin branch, under the repository lock.

    Explicit refspecs also work with narrow clones. Never include stderr in the
    fallback receipt/log: credential helpers can put secrets there.
    """
    branch = branch or git_service.get_default_branch(path)
    try:
        common = git_service._run_git(
            path, ["rev-parse", "--path-format=absolute", "--git-common-dir"], timeout=5,
        )
        root = str(Path(common.stdout.strip()).parent) if common.returncode == 0 else path
        with repo_worktree_lock(root):
            result = git_service._run_git(
                path, ["fetch", "--no-tags", "--no-write-fetch-head", "origin",
                       f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
                timeout=FETCH_TIMEOUT,
            )
        if result.returncode == 0:
            logger.debug("Fetched checkout base origin/%s", branch)
            return branch, True
    except (subprocess.SubprocessError, OSError):
        pass
    logger.warning("Checkout base fetch failed or timed out; using existing refs")
    return branch, False


def choose_worker_base(path: str, caller: str = "HEAD", *,
                       base_commit: str | None = None) -> tuple[str, str]:
    """Caller must hold the per-project lock; an explicit local commit wins."""
    branch, fetched = fetch_base(path)
    if base_commit is not None:
        return base_commit, ("Explicit base_commit." if fetched else
                             "Explicit base_commit; fetch-failed fallback.")
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
