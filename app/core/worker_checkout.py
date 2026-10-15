"""Host-owned Git isolation for Task and Swarm workers.

Use the caller's committed HEAD or an explicit validated review commit, never
an inferred origin tip or the routing root.
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
from typing import Any, Dict, Mapping, Optional

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
    runtime: dict | None = None

    def instructions(self) -> str:
        return (
            "# Host-provisioned worker checkout\n"
            + json.dumps({"path": self.path, "branch": self.branch,
                          "base_commit": self.base_commit,
                          "integration_target": self.parent_branch,
                          "runtime": self.runtime})
            + "\nThe app created and bound this dedicated checkout before launch. "
            "Use it instead of any checkout path in the assignment. Your default "
            "working directory is this checkout. Do not create another worktree "
            "or switch the parent's branch. Uncommitted parent changes and "
            "dependencies are not copied. When runtime information is supplied, "
            "use the recorded Python interpreter for "
            "tests (do not install into the parent environment); prefer a checkout-local "
            "venv once setup completes. Runtime setup status and ready-to-run Monitor "
            "inputs are in the receipt. Do not race a running setup task. "
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
    mismatch = project_resolver.describe_checkout_mismatch(row) if candidate else None
    if mismatch:
        # Name the real cause: the folder is there, on another branch.
        raise WorkerCheckoutError(
            f"The calling session's checkout is on the wrong branch: {mismatch}. "
            "Switch that folder back to its branch (keep the other work on its "
            "own branch or checkout), then retry. No workers were launched."
        )
    if candidate or expected:
        branch = f" for branch '{expected}'" if expected else ""
        raise WorkerCheckoutError(
            f"The calling session's checkout{branch} is unavailable on this "
            "device; refusing to launch workers in another checkout."
        )
    return None


def _tool_surface(profile_id, tool_allowlist):
    """The explicit tool surface: the allowlist, else the agent's allow-mode list."""
    from app.db import database

    if tool_allowlist:
        return tool_allowlist
    profile = database.get_profile(profile_id) or {}
    config = profile.get("config") or {}
    if config.get("tool_selection_mode", "allow") == "allow":
        return config.get("allowed_tools")
    return None


def prepare_worker_checkout(parent_row, worker_session_id, profile_id, tool_allowlist=None, *, base_commit=None):
    """Provision write-capable project workers; explicit research may share.

    Shares only explicitly declared surfaces of known, non-mutating builtins
    (no Write/Edit, no shell). Unknown/unrestricted surfaces are isolated.
    A mismatched/unavailable bound Git checkout is a refusal, never a fallback.
    Non-project and non-Git tasks retain their normal working-directory behavior.
    """
    from app.core import checkout_manager, project_resolver
    from app.core.git_service import git_service

    if base_commit is not None and (not isinstance(base_commit, str) or not _SHA.fullmatch(base_commit)):
        raise WorkerCheckoutError("base_commit must be a full commit SHA already available locally.")
    if base_commit is None and _is_shared_surface(_tool_surface(profile_id, tool_allowlist)):
        return None
    if parent_row is None:
        raise WorkerCheckoutError("The calling session is unavailable; refusing worker checkout inheritance.")
    if not (parent_row.get("project_v2_id") or parent_row.get("project_id")):
        if base_commit is not None:
            raise WorkerCheckoutError("base_commit requires a Git project checkout.")
        return None
    project = project_resolver.resolve_project_v2(dict(parent_row))
    if not project:
        raise WorkerCheckoutError("The caller's project is unavailable; refusing shared coding checkout.")
    expected = parent_row.get("branch") or parent_row.get("worktree_branch")
    parent_path = _caller_checkout(parent_row, expected)
    if not parent_path:
        if base_commit is not None:
            raise WorkerCheckoutError("base_commit requires a Git project checkout.")
        return None
    # An unbound caller (the project's base checkout) still integrates into a
    # real branch: report it, and hold it steady through provisioning.
    target = expected or _current_branch(parent_path)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", worker_session_id):
        raise WorkerCheckoutError("Invalid worker identity for a dedicated branch.")
    branch = f"{WORKER_BRANCH_PREFIX}{worker_session_id}"

    with _project_lock(project["id"]):
        ref = base_commit or "HEAD"
        tip = git_service._run_git(parent_path, ["rev-parse", "--verify", f"{ref}^{{commit}}"], timeout=5)
        sha = tip.stdout.strip()
        if tip.returncode or not _SHA.fullmatch(sha) or (base_commit and sha != base_commit):
            raise WorkerCheckoutError("The requested base commit is unavailable locally; fetch it before dispatch.")
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
    from app.core.worker_runtime import runtime_receipt

    return WorkerCheckout(path, branch, sha, project["id"], target,
                          runtime_receipt(parent_path, path))


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


def reclaim_untouched_worker_checkout(
    worker_session_id: str, checkout: Optional[WorkerCheckout] = None,
) -> bool:
    """Reclaim a finished/unstarted worker's checkout if it holds no work.

    Uses ``checkout`` when the caller provisioned it, else the worker's own
    session binding. Only a ``task/<worker id>`` branch is eligible; anything
    with commits past its recorded base or uncommitted changes is retained for
    review. Returns True when it was removed.
    """
    from app.core.git_service import git_service
    from app.db import database

    if checkout is not None:
        branch, path, project_id = checkout.branch, checkout.path, checkout.project_v2_id
    else:
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


# --- Workers that run on another worker -------------------------------------
#
# Git moves between workers only through origin (``checkout_transfer``). The
# parent publishes ``task/<worker id>`` at its committed HEAD; the target
# fetches it and provisions the same isolated checkout a local worker gets.
# Only identifiers travel: branch, base SHA, project id and an origin identity
# hash. Paths, URLs and credentials never do.

#: The worker runs in its own ``task/<id>`` checkout fetched from origin.
REMOTE_BRANCH = "branch"
#: A research-only surface: no push, the target's base checkout, read-only.
REMOTE_SHARED = "shared"
#: No project or not Git: nothing to transfer; run in the target's workspace.
REMOTE_NONE = "none"

_INVALID_STATE = "The worker checkout description is invalid; nothing was started."


@dataclass(frozen=True)
class RemoteWorkerBranch:
    """What the parent sends the target about the worker's checkout."""

    mode: str
    project_v2_id: Optional[str] = None
    branch: Optional[str] = None
    base_commit: Optional[str] = None
    parent_branch: Optional[str] = None
    remote: Optional[str] = None
    pushed: bool = False
    reason: Optional[str] = None

    def to_wire(self) -> Dict[str, Any]:
        return {key: getattr(self, key) for key in self.__dataclass_fields__}

    @classmethod
    def from_wire(cls, body: Any) -> "RemoteWorkerBranch":
        if not isinstance(body, dict) or body.get("mode") not in (REMOTE_BRANCH, REMOTE_SHARED, REMOTE_NONE):
            raise WorkerCheckoutError(_INVALID_STATE)
        text = {key: body.get(key) for key in cls.__dataclass_fields__ if key not in ("mode", "pushed")}
        if any(value is not None and not isinstance(value, str) for value in text.values()):
            raise WorkerCheckoutError(_INVALID_STATE)
        return cls(str(body["mode"]), pushed=body.get("pushed") is True, **text)


@dataclass(frozen=True)
class ReceivedWorkerCheckout:
    """Where a remote worker runs on the target.

    ``checkout`` is set only for an isolated ``task/<id>`` checkout. ``cwd`` is
    that checkout, the base checkout for a shared surface, or None when the
    worker should run in this worker's workspace directory.
    """

    mode: str
    cwd: Optional[str]
    checkout: Optional[WorkerCheckout] = None


def _state(state) -> RemoteWorkerBranch:
    return state if isinstance(state, RemoteWorkerBranch) else RemoteWorkerBranch.from_wire(state)


def publish_worker_branch(parent_row, worker_session_id, profile_id, tool_allowlist=None) -> RemoteWorkerBranch:
    """Parent side: push ``task/<worker id>`` at the caller's committed HEAD.

    Same decisions as :func:`prepare_worker_checkout`: research-only surfaces
    share (no push), no project or no Git checkout means nothing to transfer,
    and a mismatched or missing bound checkout raises. Uncommitted parent
    changes are not copied, as locally. The push is never forced; a failed push
    returns ``pushed=False`` with a plain ``reason`` and the caller refuses.
    No local branch is created on the parent.
    """
    from app.core import checkout_transfer, project_resolver
    from app.core.git_service import git_service

    row = dict(parent_row or {})
    has_project = bool(row.get("project_v2_id") or row.get("project_id"))
    if _is_shared_surface(_tool_surface(profile_id, tool_allowlist)):
        project = project_resolver.resolve_project_v2(row) if has_project else None
        if not project:
            return RemoteWorkerBranch(REMOTE_NONE)
        cwd = project_resolver.resolve_existing_cwd(row)
        remote = checkout_transfer.origin_identity(cwd) if cwd and git_service.is_git_repo(cwd) else None
        return RemoteWorkerBranch(REMOTE_SHARED, project_v2_id=project["id"], remote=remote)
    if parent_row is None:
        raise WorkerCheckoutError("The calling session is unavailable; refusing worker checkout inheritance.")
    if not has_project:
        return RemoteWorkerBranch(REMOTE_NONE)
    project = project_resolver.resolve_project_v2(row)
    if not project:
        raise WorkerCheckoutError("The caller's project is unavailable; refusing shared coding checkout.")
    expected = row.get("branch") or row.get("worktree_branch")
    parent_path = _caller_checkout(parent_row, expected)
    if not parent_path:
        return RemoteWorkerBranch(REMOTE_NONE)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", worker_session_id):
        raise WorkerCheckoutError("Invalid worker identity for a dedicated branch.")
    branch = f"{WORKER_BRANCH_PREFIX}{worker_session_id}"
    target = expected or _current_branch(parent_path)
    tip = git_service._run_git(parent_path, ["rev-parse", "--verify", "HEAD^{commit}"], timeout=5)
    sha = tip.stdout.strip()
    if tip.returncode or not _SHA.fullmatch(sha):
        raise WorkerCheckoutError("The caller has no valid committed HEAD to fork a worker from.")
    published = checkout_transfer.publish_task_branch(parent_path, branch, sha)
    return RemoteWorkerBranch(
        REMOTE_BRANCH, project_v2_id=project["id"], branch=branch, base_commit=sha,
        parent_branch=target, remote=published["remote"], pushed=published["pushed"],
        reason=published["reason"],
    )


def _base_checkout_path(project_id: str) -> Optional[str]:
    """This worker's base checkout of the project, when it exists on disk."""
    from app.core import checkout_manager, project_resolver
    from app.core.config import settings
    from app.db import checkouts

    runner = settings.effective_instance_id
    if not checkouts.has_active_base_checkout(project_id, runner):
        return None
    row = project_resolver._local_checkout(project_id, runner)
    path = checkout_manager._resolve_checkout_path(row["path"]) if row and row.get("path") else None
    return path if path and os.path.isdir(path) else None


def receive_worker_branch(state, worker_session_id) -> ReceivedWorkerCheckout:
    """Target side: fetch the parent's ``task/<id>`` and provision its checkout.

    Refuses by name when the project is not checked out here or this origin is
    a different repository. The new checkout must sit at exactly the SHA the
    parent sent; any failure reclaims what this call created.
    """
    from app.core import checkout_manager, checkout_transfer
    from app.core.git_service import git_service
    from app.db import projects_v2

    sent = _state(state)
    if sent.mode == REMOTE_NONE:
        return ReceivedWorkerCheckout(REMOTE_NONE, None)
    project = projects_v2.get_project_v2(sent.project_v2_id) if sent.project_v2_id else None
    base = _base_checkout_path(project["id"]) if project else None
    if not project or not base:
        raise WorkerCheckoutError("This project is not checked out on this worker; open or clone it here "
                                  "first. Nothing was started.")
    if not git_service.is_git_repo(base):
        raise WorkerCheckoutError("This worker's checkout of the project is not a Git repository; nothing was started.")
    if sent.remote is not None and checkout_transfer.origin_identity(base) != sent.remote:
        raise WorkerCheckoutError("This worker's origin is a different repository from the calling worker's "
                                  "origin; clone the project from the same origin to run tasks here. "
                                  "Nothing was started.")
    if sent.mode == REMOTE_SHARED:
        return ReceivedWorkerCheckout(REMOTE_SHARED, base)

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", worker_session_id or ""):
        raise WorkerCheckoutError("Invalid worker identity for a dedicated branch.")
    branch = f"{WORKER_BRANCH_PREFIX}{worker_session_id}"
    sha = sent.base_commit or ""
    if sent.branch != branch or not _SHA.fullmatch(sha):
        raise WorkerCheckoutError("The worker checkout description does not match this worker; nothing was started.")
    if not sent.pushed or sent.remote is None:
        raise WorkerCheckoutError(f"The calling worker did not publish {branch} to origin; nothing was started.")
    with _project_lock(project["id"]):
        try:
            checkout_transfer.fetch_task_branch(base, branch, sha, sent.remote)
        except ValueError as exc:
            raise WorkerCheckoutError(f"{exc} Nothing was started.") from None
        # The local ref first, at the fetched SHA, so ensure_checkout adopts it
        # instead of forking from an origin/<base> tip.
        if not git_service.create_branch(base, branch, start_point=sha):
            raise WorkerCheckoutError(f"Could not create dedicated worker branch {branch}; refusing to reuse it.")
        path = None
        try:
            path = checkout_manager.ensure_checkout(project, branch=branch, provision_runtime=False)
            if not path:
                raise WorkerCheckoutError(f"Could not provision dedicated checkout for {branch}; nothing was started.")
            checkout_manager._require_branch_binding(path, branch)
            actual = git_service._run_git(path, ["rev-parse", "HEAD"], timeout=5)
            if actual.returncode or actual.stdout.strip() != sha:
                raise WorkerCheckoutError(f"Dedicated checkout {path} does not match the commit the calling "
                                          "worker sent; refusing launch.")
        except BaseException:
            # Nothing has run in it yet: reclaim what this call created.
            _reclaim(base, project["id"], path, branch, sha)
            raise
    checkout = WorkerCheckout(path, branch, sha, project["id"], sent.parent_branch)
    return ReceivedWorkerCheckout(REMOTE_BRANCH, path, checkout)


def release_worker_branch(state) -> Dict[str, Any]:
    """Parent side: delete the published ``task/<id>`` on origin if untouched.

    Compare-and-delete against the base SHA, so a branch carrying anyone's new
    commits stays. Returns ``deleted`` and a plain ``reason``; never raises for
    Git failures.
    """
    from app.core import checkout_transfer

    sent = _state(state)
    if sent.mode != REMOTE_BRANCH or not sent.pushed or not sent.branch or not sent.base_commit:
        return {"deleted": False, "reason": "Nothing was published to origin for this worker."}
    base = _base_checkout_path(sent.project_v2_id) if sent.project_v2_id else None
    if not base:
        return {"deleted": False,
                "reason": "This project is not checked out on this worker; the branch was left in place."}
    try:
        return checkout_transfer.delete_untouched_task_branch(base, sent.branch, sent.base_commit)
    except ValueError as exc:
        return {"deleted": False, "reason": str(exc)}


# --- Commits back -------------------------------------------------------------
#
# When a remote worker finishes, the worker that ran it pushes its
# ``task/<id>`` to origin if it holds commits, and reports what it did as a
# git block of identifiers and fixed-text reasons. The calling worker fetches
# the branch into a local ref of the same name. Uncommitted changes are
# reported, never committed for the worker.

#: Longest reason kept in a git block.
MAX_GIT_REASON_CHARS = 300


def empty_git_block(mode: str, reason: Optional[str] = None) -> Dict[str, Any]:
    """A git block with nothing in it: ``mode`` and an optional plain reason."""
    return {"mode": mode, "branch": None, "base": None, "head": None, "commits": 0,
            "pushed": False, "uncommitted_files": 0, "remote": None, "reason": reason}


def bound_git_block(raw: Any, worker_session_id: str) -> Optional[Dict[str, Any]]:
    """A git block reduced to known, bounded fields, or None when there is none.

    Used on both sides: before the report is stored and when it arrives. The
    branch must be this worker's own ``task/<id>``; SHAs, counts and the
    origin hash must have their shapes; the reason is cut, stripped of URL
    credentials and of harness markers.
    """
    from fluid.tools.hardening import defang_harness_markers

    from app.core.scrub import redact_url_credentials

    if not isinstance(raw, Mapping) or raw.get("mode") not in (REMOTE_BRANCH, REMOTE_SHARED, REMOTE_NONE):
        return None

    def sha(value: Any) -> Optional[str]:
        return value if isinstance(value, str) and _SHA.fullmatch(value) else None

    def count(value: Any) -> int:
        return min(value, 1_000_000) if type(value) is int and value > 0 else 0

    expected = f"{WORKER_BRANCH_PREFIX}{worker_session_id}"
    branch = expected if raw.get("mode") == REMOTE_BRANCH and raw.get("branch") == expected else None
    head = sha(raw.get("head")) if branch else None
    reason = raw.get("reason")
    if isinstance(reason, str) and reason.strip():
        reason = defang_harness_markers(redact_url_credentials(reason.strip()))[:MAX_GIT_REASON_CHARS]
    else:
        reason = None
    remote = raw.get("remote")
    return {
        "mode": raw["mode"],
        "branch": branch,
        "base": sha(raw.get("base")) if branch else None,
        "head": head,
        "commits": count(raw.get("commits")) if branch else 0,
        "pushed": bool(branch and head and raw.get("pushed") is True),
        "uncommitted_files": count(raw.get("uncommitted_files")) if branch else 0,
        "remote": remote if isinstance(remote, str) and re.fullmatch(r"[0-9a-f]{64}", remote) else None,
        "reason": reason,
    }


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


def git_note(block: Mapping[str, Any], outcome: Mapping[str, Any], worker_name: str) -> str:
    """One plain paragraph for the parent: where the remote worker's work is."""
    worker = worker_name or "the other worker"
    reason = block.get("reason")
    if block.get("mode") == REMOTE_SHARED:
        return (f"Git: it ran read-only in {worker}'s checkout of this project, so there are no "
                "commits to bring back.")
    if block.get("mode") != REMOTE_BRANCH:
        return ("Git: it did not run in a Git checkout (no project, or the project is not a Git "
                "repository), so there are no commits to bring back.")
    branch = block.get("branch") or "its task branch"
    commits = int(block.get("commits") or 0)
    head = str(block.get("head") or "")[:12]
    if not commits:
        text = f"Git: it made no commits on {branch}."
        if reason:
            text += f" {reason}"
    elif block.get("pushed"):
        text = f"Git: {_plural(commits, 'commit')} on {branch} (head {head}), pushed to origin from {worker}."
        if outcome.get("fetched"):
            text += (f" Fetched into this project, so `git log {branch}` shows them; this checkout's "
                     "files and branch were not changed.")
        else:
            text += (f" It was not fetched here: {outcome.get('reason') or 'unknown reason.'} Run "
                     f"`git fetch origin {branch}` in this project to get it.")
    else:
        text = (f"Git: {_plural(commits, 'commit')} on {branch} (head {head}) were not pushed and stay in "
                f"its checkout on {worker}: {reason or 'unknown reason.'}")
    files = int(block.get("uncommitted_files") or 0)
    if files:
        text += (f" It also left uncommitted changes in {_plural(files, 'file')} in its checkout on "
                 f"{worker}; they were not committed or copied.")
    return text


def _branch_base(repo: str, branch: str) -> Optional[str]:
    """The commit ``branch`` was created at: its oldest reflog entry."""
    from app.core.git_service import git_service

    log = git_service._run_git(repo, ["reflog", "show", "--format=%H", f"refs/heads/{branch}"], timeout=5)
    entries = [e for e in (log.stdout or "").split() if e]
    return entries[-1] if not log.returncode and entries and _SHA.fullmatch(entries[-1]) else None


def push_worker_commits(worker_session_id: str, *, mode: Optional[str],
                        base_commit: Optional[str] = None) -> Dict[str, Any]:
    """Target side: push a finished remote worker's commits; return its git block.

    Only a ``task/<worker id>`` checkout (mode ``branch``) has anything to
    push, and only commits past ``base_commit`` (the parent's HEAD it started
    from) are pushed, without force, to that same branch name on origin.
    Uncommitted changes are counted, not committed. A checkout with no
    commits and no changes is reclaimed, as a local worker's is. Never raises
    for Git failures; the reason says what happened.
    """
    from app.core import checkout_transfer
    from app.db import database

    if mode == REMOTE_SHARED:
        return empty_git_block(REMOTE_SHARED)
    if mode != REMOTE_BRANCH:
        return empty_git_block(REMOTE_NONE)
    branch = f"{WORKER_BRANCH_PREFIX}{worker_session_id}"
    block = empty_git_block(REMOTE_BRANCH)
    block["branch"] = branch
    row: Dict[str, Any] = dict(database.get_session(worker_session_id) or {})
    project_id = row.get("project_v2_id")
    repo = _base_checkout_path(project_id) if project_id else None
    if not repo:
        block["reason"] = "The project's checkout is gone from this worker, so nothing was pushed."
        return block
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", worker_session_id):
        block["reason"] = "Invalid worker identity for a dedicated branch; nothing was pushed."
        return block
    base = base_commit if isinstance(base_commit, str) and _SHA.fullmatch(base_commit) else _branch_base(repo, branch)
    if not base:
        block["reason"] = f"Could not tell which commit {branch} started from, so nothing was pushed."
        return block
    pushed = checkout_transfer.push_task_result(repo, branch, base)
    block.update({key: pushed[key] for key in ("base", "head", "commits", "pushed", "remote", "reason")})
    path = row.get("worktree_path")
    if path and os.path.isdir(path):
        block["uncommitted_files"] = checkout_transfer.uncommitted_files(path) or 0
    if pushed["head"] == base and not block["uncommitted_files"]:
        reclaim_untouched_worker_checkout(worker_session_id)
    return block


def fetch_worker_commits(parent_row, worker_session_id: str, block: Mapping[str, Any]) -> Dict[str, Any]:
    """Parent side: bring a remote worker's pushed branch into this project.

    ``block`` is the validated git block from its report. With commits on
    origin, the branch is fetched into a local ``task/<id>`` so ``git log``
    works here; only refs change. With no commits, the branch the parent
    published at dispatch is removed from origin while it still points at its
    base. Returns ``fetched``, ``released`` and a plain ``reason`` when it was
    not fetched. Never raises for Git failures.
    """
    from app.core import checkout_transfer, project_resolver

    outcome: Dict[str, Any] = {"fetched": False, "released": False, "reason": None}
    branch = f"{WORKER_BRANCH_PREFIX}{worker_session_id}"
    if block.get("mode") != REMOTE_BRANCH or block.get("branch") != branch:
        return outcome
    row = dict(parent_row or {})
    project = project_resolver.resolve_project_v2(row) if (row.get("project_v2_id") or row.get("project_id")) else None
    repo = _base_checkout_path(project["id"]) if project else None
    if not repo:
        outcome["reason"] = "This project is not checked out on this worker, so the branch was not fetched here."
        return outcome
    base, head = block.get("base"), block.get("head")
    try:
        if not block.get("commits") and base and head == base:
            released = checkout_transfer.delete_untouched_task_branch(repo, branch, base)
            outcome["released"] = bool(released.get("deleted"))
            return outcome
        if not block.get("pushed") or not head:
            return outcome
        checkout_transfer.fetch_task_result(repo, branch, head, block.get("remote"))
    except ValueError as exc:
        outcome["reason"] = str(exc)
        return outcome
    outcome["fetched"] = True
    return outcome
