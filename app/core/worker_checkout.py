"""Host-owned Git isolation for Task and Swarm workers.

Use the direct caller's committed HEAD, never an origin tip or the routing root.
No model-chosen paths, credential copies, shared runtime links or automatic
merges. A worker checkout that holds work (commits or file changes) is retained
for review; one that provably holds nothing — a provisioning failure, a launch
that never started, a worker that finished without touching it — is reclaimed
with compare-and-delete Git operations that cannot discard work.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
import re
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: Builtins that can run arbitrary commands. A shell can mutate the checkout
#: (``git checkout``, ``sed -i``, ``npm install``) as surely as Edit can, so a
#: surface holding one is never shared — even though Task's background-mode
#: heuristic (``is_read_only_tool_surface``) counts it as research.
SHELL_TOOLS = frozenset({"bash", "monitor"})

#: Worker branches live under this prefix; reclaim refuses anything else.
WORKER_BRANCH_PREFIX = "task/"

_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")

# Serialize branch + worktree creation per project: parallel Swarm launches
# otherwise race on the same repository's ref and worktree locks, and a lost
# race surfaces as a permanent "could not create" start failure.
_project_locks: Dict[str, threading.Lock] = {}
_project_locks_guard = threading.Lock()


def _project_lock(project_id: str) -> threading.Lock:
    with _project_locks_guard:
        return _project_locks.setdefault(project_id, threading.Lock())


class WorkerCheckoutError(RuntimeError):
    """Fail closed rather than launch a coding worker in the caller's tree."""


@dataclass(frozen=True)
class WorkerCheckout:
    path: str
    branch: str
    base_commit: str
    project_v2_id: str
    parent_branch: str | None

    def instructions(self) -> str:
        return (
            "# Host-provisioned worker checkout\n"
            + json.dumps({"path": self.path, "branch": self.branch,
                          "base_commit": self.base_commit,
                          "integration_target": self.parent_branch})
            + "\nThe app created and bound this dedicated checkout before launch. "
            "Use it instead of any checkout path in the assignment. Your default "
            "working directory is this checkout. Do not create another worktree "
            "or switch the parent's branch. Uncommitted parent changes and "
            "dependencies are not copied; install required dependencies here. "
            "Commit scoped changes and return their SHAs and this checkout path "
            "for the parent's review/integration. Do not merge into the parent "
            "checkout, remove this worktree, or push/open a PR unless assigned. "
            "A checkout left with no commits and no changes is reclaimed when "
            "you finish."
        )


def _is_shared_surface(tools) -> bool:
    """True only for an explicit, fully known surface that cannot mutate files."""
    from app.core.builtin_subagents import WRITE_CAPABLE_TOOLS, is_read_only_tool_surface
    from fluid.cli.prompt import BUILTIN_TOOL_NAMES

    if not tools:
        return False
    known = {name.lower() for name in BUILTIN_TOOL_NAMES}
    # Normalize qualified builtin names without treating external MCP writers
    # as a known read-only surface. Wildcards and unknown tools isolate safely.
    normalized = [str(name).removeprefix("mcp__builtins__").strip().lower() for name in tools]
    if not all(name in known or name in WRITE_CAPABLE_TOOLS for name in normalized):
        return False
    if any(name in SHELL_TOOLS for name in normalized):
        return False
    return is_read_only_tool_surface(normalized)


def _current_branch(path: str) -> Optional[str]:
    from app.core.git_service import git_service

    head = git_service._run_git(path, ["symbolic-ref", "--quiet", "--short", "HEAD"], timeout=5)
    name = (head.stdout or "").strip()
    return name if head.returncode == 0 and name else None


def _caller_checkout(parent_row, expected: Optional[str]) -> Optional[str]:
    """The caller's materialized checkout, ``None`` to keep legacy behavior.

    Raises when the caller is (or must be) a Git checkout that cannot be used:
    a bound branch with no checkout here, or a checkout on the wrong branch.
    A non-Git folder, or an unbound project with nothing materialized on this
    runner, keeps the worker's normal cwd resolution — exactly as before.
    """
    from app.core import checkout_manager, project_resolver
    from app.core.git_service import git_service

    row = dict(parent_row)
    path = project_resolver.resolve_existing_cwd(row)
    if path:
        return path if git_service.is_git_repo(path) else None
    local = project_resolver.resolve_local_checkout(row)
    candidate = None
    if local and local.get("path"):
        candidate = checkout_manager._resolve_checkout_path(local["path"])
        if not os.path.isdir(candidate):
            candidate = None
    if candidate and not git_service.is_git_repo(candidate):
        return None
    if candidate or expected:
        raise WorkerCheckoutError(
            "The caller's checkout is unavailable or its branch binding "
            "mismatches; refusing to launch in another checkout."
        )
    return None


def prepare_worker_checkout(parent_row, worker_session_id, profile_id, tool_allowlist=None):
    """Provision write-capable project workers; explicit research may share.

    Shares only explicitly declared surfaces of known, non-mutating builtins
    (no Write/Edit, no shell). Unknown/unrestricted surfaces are isolated.
    A mismatched/unavailable bound Git checkout is a refusal, never a fallback.
    Non-project and non-Git tasks retain their normal working-directory behavior.
    """
    from app.core import checkout_manager, project_resolver
    from app.core.git_service import git_service
    from app.db import database

    tools = tool_allowlist
    if not tools:
        profile = database.get_profile(profile_id) or {}
        config = profile.get("config") or {}
        if config.get("tool_selection_mode", "allow") == "allow":
            tools = config.get("allowed_tools")
    if _is_shared_surface(tools):
        return None
    if parent_row is None:
        raise WorkerCheckoutError("The calling session is unavailable; refusing worker checkout inheritance.")
    if not (parent_row.get("project_v2_id") or parent_row.get("project_id")):
        return None
    project = project_resolver.resolve_project_v2(dict(parent_row))
    if not project:
        raise WorkerCheckoutError("The caller's project is unavailable; refusing shared coding checkout.")
    expected = parent_row.get("branch") or parent_row.get("worktree_branch")
    parent_path = _caller_checkout(parent_row, expected)
    if not parent_path:
        return None
    # An unbound caller (the project's base checkout) still integrates into a
    # real branch: report it, and hold it steady through provisioning.
    target = expected or _current_branch(parent_path)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", worker_session_id):
        raise WorkerCheckoutError("Invalid worker identity for a dedicated branch.")
    branch = f"{WORKER_BRANCH_PREFIX}{worker_session_id}"

    with _project_lock(project["id"]):
        tip = git_service._run_git(parent_path, ["rev-parse", "--verify", "HEAD^{commit}"], timeout=5)
        sha = tip.stdout.strip()
        if tip.returncode or not _SHA.fullmatch(sha):
            raise WorkerCheckoutError("The caller has no valid committed HEAD to fork a worker from.")
        # Creating the ref first prevents add_worktree's new-branch path fetching
        # origin and preferring an origin/<base> tip over the actual caller HEAD.
        if not git_service.create_branch(parent_path, branch, start_point=sha):
            raise WorkerCheckoutError(f"Could not create dedicated worker branch {branch}; refusing to reuse it.")
        path = None
        try:
            path = checkout_manager.ensure_checkout(project, branch=branch, provision_runtime=False)
            if not path:
                raise WorkerCheckoutError(
                    f"Could not provision dedicated checkout for {branch}; "
                    "refusing shared-checkout fallback."
                )
            checkout_manager._require_branch_binding(path, branch)
            actual = git_service._run_git(path, ["rev-parse", "HEAD"], timeout=5)
            if actual.returncode or actual.stdout.strip() != sha:
                raise WorkerCheckoutError(f"Dedicated checkout {path} does not match its recorded base commit; refusing launch.")
            if expected:
                checkout_manager._require_branch_binding(parent_path, expected)
            elif _current_branch(parent_path) != target:
                raise WorkerCheckoutError("The caller's branch changed during worker provisioning; refusing launch.")
        except BaseException:
            # Nothing has run in it yet: reclaim what this call created.
            _reclaim(parent_path, project["id"], path, branch, sha)
            raise
    return WorkerCheckout(path, branch, sha, project["id"], target)


def _reclaim(main_dir: str, project_id: str, path: Optional[str], branch: str, base_commit: str) -> bool:
    """Remove a worker checkout and branch only while they hold no work.

    Every step is conservative: ``git worktree remove`` without ``--force``
    refuses a dirty or untracked tree, and the ref delete is a compare-and-
    delete against ``base_commit``, so a commit made meanwhile survives.
    """
    from app.core.git_service import git_service

    try:
        if path and os.path.isdir(path):
            head = git_service._run_git(path, ["rev-parse", "HEAD"], timeout=5)
            status = git_service._run_git(path, ["status", "--porcelain"], timeout=10)
            if (head.returncode or head.stdout.strip() != base_commit
                    or status.returncode or status.stdout.strip()):
                return False
            if not git_service.remove_worktree(main_dir, path, force=False):
                return False
        removed = git_service._run_git(
            main_dir, ["update-ref", "-d", f"refs/heads/{branch}", base_commit], timeout=10,
        )
        if path:
            try:
                from app.db.checkouts import archive_checkout_by_path, set_head_sha_for_path

                # Same handoff the Git panel's cleanup leaves: the recorded tip
                # lets a resumed worker re-materialize this branch and path.
                set_head_sha_for_path(path, base_commit, project_id=project_id)
                archive_checkout_by_path(project_id, path)
            except Exception:  # noqa: BLE001 — the row is metadata; Git is the truth
                logger.debug("worker_checkout: could not archive %s", path, exc_info=True)
        return removed.returncode == 0
    except Exception:  # noqa: BLE001 — reclaim is best-effort; never mask the caller
        logger.warning("worker_checkout: could not reclaim %s", branch, exc_info=True)
        return False


def reclaim_untouched_worker_checkout(worker_session_id: str) -> bool:
    """Reclaim a finished/unstarted worker's checkout if it holds no work.

    Reads the worker's own session binding. Only a ``task/<worker id>`` branch
    is eligible; anything with commits past its recorded base or uncommitted
    changes is retained for review. Returns True when it was removed.
    """
    from app.core.git_service import git_service
    from app.db import database

    try:
        row: Optional[Dict[str, Any]] = database.get_session(worker_session_id)
    except Exception:  # noqa: BLE001
        return False
    branch = (row or {}).get("branch") or ""
    path = (row or {}).get("worktree_path")
    project_id = (row or {}).get("project_v2_id")
    if branch != f"{WORKER_BRANCH_PREFIX}{worker_session_id}" or not path or not project_id:
        return False
    if not os.path.isdir(path):
        return False
    # The branch's oldest reflog entry is the caller HEAD it was forked from.
    # Any later entry (a commit, reset or rebase) means the worker touched it;
    # no reflog at all means we cannot prove that, so keep it.
    log = git_service._run_git(
        path, ["reflog", "show", "--format=%H", f"refs/heads/{branch}"], timeout=5,
    )
    entries = [e for e in (log.stdout or "").split() if e]
    if log.returncode or len(entries) != 1 or not _SHA.fullmatch(entries[0]):
        return False
    base_commit = entries[0]
    main_dir = _main_dir_for(path)
    if not main_dir:
        return False
    return _reclaim(main_dir, project_id, path, branch, base_commit)


def discard_unused_worker_checkout(checkout: WorkerCheckout) -> bool:
    """Reclaim a checkout provisioned for a worker that never launched."""
    main_dir = _main_dir_for(checkout.path)
    if not main_dir:
        return False
    return _reclaim(main_dir, checkout.project_v2_id, checkout.path,
                    checkout.branch, checkout.base_commit)


def _main_dir_for(path: str) -> Optional[str]:
    """The repository's main working tree, to run worktree admin from."""
    from app.core.git_service import git_service

    common = git_service._run_git(
        path, ["rev-parse", "--path-format=absolute", "--git-common-dir"], timeout=5,
    )
    main_dir = os.path.dirname((common.stdout or "").strip()) if common.returncode == 0 else None
    return main_dir if main_dir and os.path.isdir(main_dir) else None


def restore_reclaimed_worker_checkout(worker_session_id: str) -> bool:
    """Re-materialize a reclaimed worker checkout before the worker resumes.

    A resumed turn reuses its saved cwd, so the directory must exist again.
    Restores the ``task/<worker id>`` branch from the tip stamped at reclaim,
    at the same deterministic path, without shared runtime links. Returns True
    when nothing needed restoring or the checkout is back at its saved path.
    """
    from app.core import checkout_manager, project_resolver
    from app.db import database

    try:
        row = database.get_session(worker_session_id) or {}
        branch = row.get("branch") or ""
        path = row.get("worktree_path")
        if branch != f"{WORKER_BRANCH_PREFIX}{worker_session_id}" or not path:
            return True
        if os.path.isdir(path):
            return True
        project = project_resolver.resolve_project_v2(dict(row))
        if not project:
            return False
        with _project_lock(project["id"]):
            _recreate_branch_from_recorded_tip(project["id"], branch)
            restored = checkout_manager.ensure_checkout(
                project, branch=branch, provision_runtime=False,
            )
        return bool(restored) and os.path.realpath(restored) == os.path.realpath(path)
    except Exception:  # noqa: BLE001 — a refusal, reported by the caller
        logger.warning("worker_checkout: could not restore %s", worker_session_id, exc_info=True)
        return False


def _recreate_branch_from_recorded_tip(project_id: str, branch: str) -> None:
    """Recreate a reclaimed worker branch locally, so materializing it needs
    no origin fetch (an untouched worker branch was never pushed)."""
    from app.core import checkout_manager
    from app.core.config import settings
    from app.core.git_service import git_service
    from app.db import checkouts

    runner = settings.effective_instance_id
    row = checkouts.get_checkout(project_id, runner, branch=branch) or {}
    base = checkouts.get_checkout(project_id, runner) or {}
    sha = str(row.get("head_sha") or "").strip()
    base_path = checkout_manager._resolve_checkout_path(base["path"]) if base.get("path") else None
    if not _SHA.fullmatch(sha) or not base_path or not os.path.isdir(base_path):
        return
    exists = git_service._run_git(base_path, ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], timeout=5)
    if exists.returncode != 0:
        git_service.create_branch(base_path, branch, start_point=sha)
