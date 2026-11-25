"""Per-runner lazy materialization of project_v2 checkouts.

A *checkout* is the physical working tree of a ``projects_v2`` project on ONE
runner (``runner_id`` = this install's ``effective_instance_id``). The origin-keyed
``projects_v2`` row is install-independent identity; the ``checkouts`` row is its
per-runner materialization — ``branch`` NULL/'' is the *base* checkout, a non-empty
``branch`` is a logical worktree materialized at its own deterministic path.

This module owns the ONE lazy-materialization entrypoint, ``ensure_checkout``,
which ``project_resolver.resolve_cwd`` and ``git_ops_runner`` call. It is
idempotent and safe to call repeatedly. Design commitments:

- **Deterministic per-runner path** — ``{base_checkout_dir}/.fluid/worktrees/
  {sanitized_branch}`` — nested inside the project's own clone on this runner,
  replacing both the legacy ``md5(project_path)[:4]`` scheme and the earlier
  workspace-root ``checkouts/{project_slug}/{branch}`` layout. A materialization,
  never identity, so the same branch resolves per-machine without cross-host
  orphaning.
- **Clones only through the gated self-seed.** A project with no local base
  checkout on this runner is cloned from its remote by ``_seed_base_checkout``
  when ``settings.self_seed_checkouts`` and the per-project opt-out allow it and a
  cloneable URL resolves. Otherwise ``ensure_checkout`` returns ``None`` —
  honestly "not on this device" — rather than fabricating a checkout.
- **Never destructive.** If a directory exists but is not a valid worktree, we log
  and return ``None`` rather than tearing anything down.
- **Verified branch binding.** Reused and newly materialized branch checkouts
  must have their own Git root and the requested HEAD. A stale row or colliding
  sanitized path is refused, never repaired by switching HEAD or relabeling it.

Reuses ``git_service`` and ``worktree_manager._sanitize_branch_name`` by import.
"""

from __future__ import annotations

from contextlib import nullcontext
import json
import logging
import ntpath
import os
import posixpath
import re
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fluid.tools.child_env import scrub_child_env

from app.core.config import settings
from app.core.fs_utils import force_rmtree
from app.core.git_service import git_service
from app.core.git_worktree_ops import (
    BaseBranchUnavailableError,
    WorktreeAddError,
    repo_worktree_lock,
    worktree_error_detail,
)
from app.core.worktree_manager import (
    WorktreeError,
    resolve_rebound_branch_plan,
    worktree_manager,
)
from app.db import checkouts as checkouts_db
from app.db.base import get_db

logger = logging.getLogger(__name__)

# The single literal for the per-install checkouts subdirectory under the
# configured workspace root. Used everywhere a checkout path is derived.
_CHECKOUTS_SUBDIR = "checkouts"

# The reserved directory name for a project's base (branch-NULL) checkout under
# its per-project slug dir. A logical branch that sanitizes to
# this literal is renamed by ``_branch_checkout_path``'s collision guard so it can
# never land on — and clobber — the base dir.
_BASE_DIR_NAME = "__base__"


def _checkout_root() -> Path:
    """The runner's checkout root: the configured workspace dir + ``checkouts``.

    Read from ``settings.effective_workspace_dir`` (the same source
    ``load_workspace_from_database`` populates) at call time so a runtime
    workspace override is honored. No new setting is introduced.
    """
    return Path(settings.effective_workspace_dir) / _CHECKOUTS_SUBDIR


def _resolve_checkout_path(path: str) -> str:
    """Resolve a ``checkouts.path`` read from the DB against this runner's workspace.

    A ``checkouts.path`` is authoritative and, for rows written by V2, absolute.
    But the v60 collapse migration seeded some rows verbatim from legacy
    ``projects.path``, which could be workspace-RELATIVE (e.g. ``ai-shuffle``);
    pre-V2 ``get_project_working_dir`` resolved those against ``workspace_dir`` at
    runtime. Mirror that here: an ABSOLUTE path is returned unchanged; a RELATIVE
    one is joined under ``settings.effective_workspace_dir`` so it resolves the
    same way. Read-only normalization — the DB row is NEVER rewritten (a relative
    row is repaired out-of-band; new relative writes are rejected by
    ``checkouts.upsert_checkout``).

    A path absolute on EITHER POSIX or Windows counts as absolute, so a path
    authored on one runner's OS is never mangled when read on another (the primary
    is Windows, the worker is Linux, and they share one Postgres).
    """
    if os.path.isabs(path) or ntpath.isabs(path) or posixpath.isabs(path):
        return path
    return str(Path(settings.effective_workspace_dir) / path)


def _slugify(text: str) -> str:
    """Filesystem-safe, lowercase slug of ``text`` (empty string if nothing left)."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-.")
    return s.lower()


def _project_slug(project_v2_row: Dict[str, Any]) -> str:
    """A filesystem-safe slug for a project_v2, suffixed with the first 8 chars of
    its id so two projects whose names slugify identically never collide.

    Falls back to the id when the name is empty / slugifies to nothing.
    """
    pid = str(project_v2_row.get("id") or "")
    base = _slugify(str(project_v2_row.get("name") or "")) or _slugify(pid) or "project"
    id_suffix = re.sub(r"[^A-Za-z0-9]", "", pid)[:8] or "0"
    return f"{base}-{id_suffix}"


_WORKTREES_DIRNAME = ".fluid"


def _branch_checkout_path(base_dir: str, branch: str) -> Path:
    """Deterministic materialization path for a (project, branch) checkout.

    ``{base_checkout_dir}/.fluid/worktrees/{sanitized_branch}`` — nested inside
    the project's own clone so per-branch checkouts travel with the project dir.
    Uses ``pathlib`` joins (never string concat with separators) so it is correct
    on Windows and POSIX alike.

    Raises ``WorktreeError`` when the branch sanitizes to ``""``, ``"."`` or
    ``".."``, or when the path resolves outside the base checkout (e.g. a
    committed ``.fluid`` symlink) — the same rule ``worktree_manager`` applies.
    """
    sanitized = worktree_manager._sanitize_branch_name(branch)
    if sanitized in ("", ".", ".."):
        raise WorktreeError(f"Invalid branch name for a checkout: '{branch}'")
    if sanitized == _BASE_DIR_NAME:
        # Collision guard: the base checkout reserves
        # ``__base__``. A logical branch whose name sanitizes to that exact literal
        # is renamed so it can never resolve onto — and overwrite — the base dir.
        sanitized = f"{sanitized}-branch"
    path = Path(base_dir) / _WORKTREES_DIRNAME / "worktrees" / sanitized
    worktree_manager._require_inside_checkout(base_dir, path)
    return path


def _is_empty_cleanup_remnant(path: str, row: Optional[Dict[str, Any]]) -> bool:
    """Allow Git to reuse an empty recorded checkout, never remove its contents."""
    if not row or not row.get("path"):
        return False
    if os.path.normcase(os.path.abspath(path)) != os.path.normcase(
        os.path.abspath(_resolve_checkout_path(row["path"]))
    ):
        return False
    candidate = Path(path)
    if _is_directory_link(candidate):
        return False
    try:
        with os.scandir(candidate) as entries:
            return next(entries, None) is None
    except OSError:
        return False


def _dir_is_worktree(path: str) -> bool:
    """A directory is a usable checkout iff it exists AND carries a ``.git`` entry
    (a FILE for a linked worktree, or a DIRECTORY for a plain clone) — the same
    two-rule liveness test as ``worktree_manager.is_worktree_valid``."""
    p = Path(path)
    return p.is_dir() and (p / ".git").exists()


def _require_checkout_root(path: str) -> None:
    """Validate the physical root independently of its logical branch binding.

    Metadata/status reads may inspect a deliberately switched checkout, but must
    never discover an enclosing repository through a broken or missing marker.
    """
    root = git_service._run_git(path, ["rev-parse", "--show-toplevel"], timeout=10)
    if (
        not _dir_is_worktree(path)
        or root.returncode != 0
        or not (root.stdout or "").strip()
        or os.path.normcase(os.path.realpath(root.stdout.strip()))
        != os.path.normcase(os.path.realpath(path))
    ):
        raise WorktreeError(f"Checkout {path!r} is not its own Git working tree")


def _reattach_detached_checkout(path: str, branch: str) -> bool:
    """Put a registered branch checkout with no branch back on its branch, losslessly.

    An agent can leave its own checkout detached, typically with
    ``git checkout origin/main`` after its PR merged, often followed by
    deleting the merged branch. Every later send then failed the binding
    check, and the owner had no way to re-point the session. Recover only
    when no commit or file can be lost:

    - the bound branch no longer exists locally: create it at HEAD;
    - it exists and its tip is HEAD or an ancestor of HEAD: move it to HEAD.

    Both attach HEAD at the commit it already has, so the working tree is
    untouched, and git itself refuses a branch that another worktree has
    checked out. Anything else (a branch with commits HEAD lacks, an
    unborn HEAD, a folder that is not its own working tree, a checkout on
    some other branch) is left for :func:`_require_branch_binding` to
    refuse. Returns True when it reattached.
    """
    if not branch or branch.startswith("-"):
        return False
    try:
        _require_checkout_root(path)
    except WorktreeError:
        return False
    if git_service._run_git(path, ["symbolic-ref", "-q", "HEAD"], timeout=10).returncode == 0:
        return False  # on a branch: a real mismatch, never ours to rewrite
    head = git_service._run_git(path, ["rev-parse", "--verify", "-q", "HEAD^{commit}"], timeout=10)
    head_sha = (head.stdout or "").strip()
    if head.returncode != 0 or not head_sha:
        return False
    if git_service._run_git(path, ["check-ref-format", "--branch", branch], timeout=10).returncode != 0:
        return False
    tip = git_service._run_git(
        path, ["rev-parse", "--verify", "-q", f"refs/heads/{branch}^{{commit}}"], timeout=10
    )
    tip_sha = (tip.stdout or "").strip() if tip.returncode == 0 else ""
    if tip_sha and tip_sha != head_sha:
        ancestor = git_service._run_git(
            path, ["merge-base", "--is-ancestor", tip_sha, head_sha], timeout=10
        )
        if ancestor.returncode != 0:
            return False
    switched = git_service._run_git(path, ["switch", "-C", branch, head_sha], timeout=30)
    if switched.returncode != 0 or git_service.get_current_branch(path) != branch:
        logger.warning(
            "checkout_manager: could not reattach detached %s to %r: %s",
            path, branch, (switched.stderr or "").strip()[:300],
        )
        return False
    logger.info(
        "checkout_manager: reattached detached checkout %s to its branch %r at %s (%s)",
        path, branch, head_sha[:12], "moved forward" if tip_sha else "recreated",
    )
    return True


def switch_detached_checkout_to_branch(path: str, branch: str) -> Optional[str]:
    """Switch a clean, detached session checkout back to its bound branch (#1437).

    :func:`_reattach_detached_checkout` runs first and keeps the working
    tree. When it declines because the branch has commits HEAD lacks (an
    agent ran ``git checkout origin/main`` for a baseline run), this runs
    ``git switch <branch>``, which moves the working tree to the branch tip.
    It switches only when nothing can be lost:

    - ``path`` is a linked worktree (never a project's main checkout), on a
      detached HEAD, and the bound branch exists locally;
    - no staged or unstaged change to a tracked file;
    - HEAD's commit is on a local branch, a remote-tracking ref or a tag,
      so no commit made while detached becomes unreachable.

    Git refuses the rest itself: a branch another worktree has checked out,
    an untracked or ignored file the switch would overwrite
    (``--no-overwrite-ignore``), a rebase, merge or bisect in progress. Never
    forces, resets or discards. Callers decide when moving the working tree
    is the session's own business (the turn-start path,
    ``project_resolver.reattach_own_detached_checkout``). Returns the
    commit HEAD was on when it switched, else ``None``.
    """
    if not branch or branch.startswith("-"):
        return None
    try:
        _require_checkout_root(path)
    except WorktreeError:
        return None
    if not os.path.isfile(os.path.join(path, ".git")):
        return None  # the main working tree is never a session's own
    with repo_worktree_lock(path):
        if git_service._run_git(path, ["symbolic-ref", "-q", "HEAD"], timeout=10).returncode == 0:
            return None
        if _reattach_detached_checkout(path, branch):
            return None  # attached in place; the working tree did not move
        head = git_service._run_git(path, ["rev-parse", "--verify", "-q", "HEAD^{commit}"], timeout=10)
        head_sha = (head.stdout or "").strip()
        if head.returncode != 0 or not head_sha:
            return None
        if git_service._run_git(
            path, ["rev-parse", "--verify", "-q", f"refs/heads/{branch}^{{commit}}"], timeout=10
        ).returncode != 0:
            return None
        status = git_service._run_git(
            path, ["status", "--porcelain", "--untracked-files=no"], timeout=30
        )
        if status.returncode != 0 or (status.stdout or "").strip():
            return None
        refs = git_service._run_git(path, [
            "for-each-ref", "--count=1", "--format=%(refname)", f"--contains={head_sha}",
            "refs/heads", "refs/remotes", "refs/tags",
        ], timeout=30)
        if refs.returncode != 0 or not (refs.stdout or "").strip():
            return None
        switched = git_service._run_git(
            path, ["switch", "--no-overwrite-ignore", branch], timeout=60
        )
        if switched.returncode != 0 or git_service.get_current_branch(path) != branch:
            logger.info(
                "checkout_manager: did not switch detached %s back to %r: %s",
                path, branch, (switched.stderr or "").strip()[:300],
            )
            return None
    logger.info(
        "checkout_manager: switched detached checkout %s from %s back to its branch %r",
        path, head_sha[:12], branch,
    )
    return head_sha


def _require_branch_binding(path: str, branch: str) -> None:
    """Refuse stale/adopted branch paths without switching HEAD or rewriting rows."""
    _require_checkout_root(path)
    actual = git_service.get_current_branch(path)
    if actual != branch:
        raise WorktreeError(
            f"Checkout {path!r} is on {actual!r}, not the bound branch {branch!r}; "
            "refusing to use or reassign it"
        )


def _require_complete_checkout(path: str, *, row: Optional[Dict[str, Any]] = None) -> None:
    """Inspect fresh adoptions and known failed adds, not ordinary user edits.

    Existing active rows remain trusted across upgrades and successful historical
    recreations unless a later failed add flagged this physical checkout. A failed
    add from before failure markers existed with a stale active row is unknowable.
    """
    _require_checkout_root(path)
    active = bool(row and (row.get("status") or "active") == "active")
    git_service.require_complete_worktree(path, force=not active)


def _accept_branch_checkout(
    path: str, project_id: str, runner_id: str, branch: str, *, reattach: bool = False,
) -> str:
    """Validate and publish together; never activate an in-flight checkout."""
    with repo_worktree_lock(path):
        row = checkouts_db.get_checkout(project_id, runner_id, branch=branch)
        stored = row.get("path") if row else None
        same = bool(stored and os.path.normcase(os.path.realpath(_resolve_checkout_path(stored)))
                    == os.path.normcase(os.path.realpath(path)))
        try:
            _require_complete_checkout(path, row=row if same else None)
        except WorktreeAddError:
            if same and row.get("status") == "active":
                checkouts_db.set_checkout_status(row["id"], "incomplete")
            raise
        if reattach and git_service.get_current_branch(path) != branch:
            _reattach_detached_checkout(path, branch)
        _require_branch_binding(path, branch)
        # Preserve legacy relative-row semantics; never create a second row.
        registered = stored if same else path
        if os.path.isabs(registered) and (not same or row.get("status") != "active"):
            checkouts_db.upsert_checkout(
                project_id, runner_id, registered, branch=branch, status="active",
            )
        return path


def _ref_exists(base_dir: str, branch: str) -> bool:
    """Whether ``branch`` exists here as a cached origin ref or a local branch."""
    return any(
        git_service._run_git(base_dir, ["rev-parse", "--verify", "--quiet", ref], timeout=5).returncode == 0
        for ref in (f"refs/remotes/origin/{branch}", f"refs/heads/{branch}")
    )


def _fork_default(base_dir: str) -> Tuple[Optional[str], bool]:
    """The repository's default branch for a new chat, asking origin if unknown.

    Origin is asked first which branch its HEAD names; that branch is fetched
    and recorded as ``origin/HEAD``, as a fresh clone would. A recorded
    ``origin/HEAD`` can belong to a previous URL (``git remote set-url``
    leaves it behind), so it is only the answer when origin can't be asked,
    and a local main/master only after that: a stale ``master`` must not
    beat a remote ``trunk``. Returns ``(branch, named_by_origin)``; branch is
    ``None`` when nothing is known. ``named_by_origin`` means origin just
    named it, so a missing branch is an error, never a reason to fork from
    something else.
    """
    confirmed = git_service.known_default_branch(base_dir, confirmed=True)
    if git_service.has_origin(base_dir):
        # With a recorded fallback, don't let an unreachable origin hold the
        # chat for the full fetch deadline before the fetch itself times out.
        learned = _learn_origin_default(
            base_dir, ls_remote_timeout=ORIGIN_HEAD_REVALIDATE_TIMEOUT if confirmed else None,
        )
        if learned:
            return learned, True
    if confirmed:
        return confirmed, False
    return git_service.known_default_branch(base_dir), False


def _origin_head_branch(base_dir: str, timeout: float) -> Optional[str]:
    """The branch origin's HEAD names (``ls-remote --symref``), or ``None``."""
    from app.core import checkout_transfer

    try:
        listed = checkout_transfer._run(
            base_dir, "ls-remote", "--symref", "origin", "HEAD", timeout=timeout,
        )
        if listed.returncode != 0:
            return None
        output = (listed.stdout or b"").decode("utf-8", "replace")
        branch = next((line[len("ref: refs/heads/"):-len("\tHEAD")]
                       for line in output.splitlines()
                       if line.startswith("ref: refs/heads/") and line.endswith("\tHEAD")), "")
        valid = branch and not branch.startswith("-") and subprocess.run(
            ["git", "check-ref-format", "--branch", branch], cwd=base_dir,
            capture_output=True, timeout=5, env=scrub_child_env(),
        ).returncode == 0
    except (subprocess.SubprocessError, OSError, ValueError):  # ValueError: _run timed out
        return None
    return branch if valid else None


ORIGIN_HEAD_REVALIDATE_TIMEOUT = 15


def revalidate_origin_head(base_dir: str) -> None:
    """Re-check ``origin/HEAD`` after origin's URL changed.

    ``git remote set-url`` leaves ``origin/HEAD`` naming the previous
    repository's default, which would otherwise be trusted as confirmed. Point
    it at the new origin's default when that branch is already fetched;
    otherwise drop it (a cache Git rebuilds with ``remote set-head``), so the
    next new chat asks origin instead of reusing the old repository's branch.
    """
    from app.core import checkout_transfer
    from app.core.git_cache import _git_cache

    branch = _origin_head_branch(base_dir, ORIGIN_HEAD_REVALIDATE_TIMEOUT)
    tracking = f"refs/remotes/origin/{branch}" if branch else ""
    try:
        if tracking and git_service._run_git(
                base_dir, ["rev-parse", "--verify", "--quiet", tracking], timeout=5).returncode == 0:
            checkout_transfer._run(base_dir, "symbolic-ref", "refs/remotes/origin/HEAD", tracking, timeout=5)
        else:
            git_service._run_git(base_dir, ["symbolic-ref", "--delete", "--quiet", "refs/remotes/origin/HEAD"],
                                 timeout=5)
    except (subprocess.SubprocessError, OSError, ValueError):
        logger.debug("checkout_manager: revalidating origin/HEAD failed for %s", base_dir, exc_info=True)
    _git_cache.invalidate(f"default_branch:{base_dir}")


def _learn_origin_default(base_dir: str, *, ls_remote_timeout: Optional[float] = None) -> Optional[str]:
    """Ask origin for its HEAD branch, fetch it and record ``origin/HEAD``."""
    from app.core import checkout_transfer
    from app.core.git_cache import _git_cache
    from app.core.git_fork_base import CHAT_FETCH_TIMEOUT, fetch_base

    branch = _origin_head_branch(base_dir, ls_remote_timeout or CHAT_FETCH_TIMEOUT)
    if not branch:
        return None
    # Once origin has named its default, that is the base even if fetching it
    # fails; the caller reports an unavailable base rather than substitute a
    # local master. fetch_base keeps FETCH_HEAD and coalesces the next fetch.
    _, fetched = fetch_base(base_dir, branch, timeout=CHAT_FETCH_TIMEOUT)
    tracking = f"refs/remotes/origin/{branch}"
    if fetched and git_service._run_git(
            base_dir, ["rev-parse", "--verify", "--quiet", tracking], timeout=5).returncode == 0:
        try:
            checkout_transfer._run(base_dir, "symbolic-ref", "refs/remotes/origin/HEAD", tracking, timeout=5)
        except (subprocess.SubprocessError, OSError, ValueError):
            pass  # Only a cache of origin's answer; the branch is still right.
        _git_cache.invalidate(f"default_branch:{base_dir}")
    return branch


def _branch_plan(
    base_dir: str,
    branch: str,
    base_branch: Optional[str] = None,
    create_new_branch: bool = False,
    *,
    project_id: Optional[str] = None,
    runner_id: Optional[str] = None,
) -> Optional[Tuple[bool, Optional[str]]]:
    """Decide new-branch vs existing-branch for ``git worktree add``.

    * ``create_new_branch=True`` — the caller is intentionally minting a fresh
      branch (new-session compose): create it from ``base_branch`` (its own tip;
      ``add_worktree`` prefers ``origin/{base}`` internally). Fetch the base
      with a short, best-effort deadline; never recover an unrelated branch.
    * otherwise reuse ``worktree_manager.resolve_rebound_branch_plan`` so lazy
      materialization stays in lockstep with the cross-device rebind path:
        - branch exists locally → check it out → ``(False, None)``
        - else present on origin (after a fetch) → create from the pushed tip →
          ``(True, branch)`` — ``add_worktree`` prefers ``origin/{branch}``
        - else the branch is unrecoverable on this runner → return ``None`` so
          the caller honestly reports not-materializable, rather than silently
          fabricating an empty branch off the base HEAD (the pre-fix bug).
    """
    if create_new_branch:
        from app.core.git_fork_base import CHAT_FETCH_TIMEOUT, fetch_base

        named_by_origin = False
        if base_branch:
            target = base_branch
        else:
            target, named_by_origin = _fork_default(base_dir)
        fetched_branch, fetched = fetch_base(base_dir, target, timeout=CHAT_FETCH_TIMEOUT)
        if base_branch == "main" and not fetched and not _ref_exists(base_dir, "main"):
            # Older composers (and saved drafts) send ``main`` when nothing was
            # picked. If the repository has no main, use its real default
            # branch. Any other missing base still fails, so a deleted branch
            # the user picked is reported instead of silently replaced.
            logger.info("checkout_manager: legacy base 'main' not found; using the default branch")
            base_branch = None
            target, named_by_origin = _fork_default(base_dir)
            fetched_branch, fetched = fetch_base(base_dir, target, timeout=CHAT_FETCH_TIMEOUT)
        if base_branch or fetched:
            return True, base_branch or fetched_branch
        # Offline, prefer the same default branch if either cached ref exists;
        # retain HEAD only for repositories with no default ref at all.
        if _ref_exists(base_dir, fetched_branch):
            return True, fetched_branch
        if named_by_origin:
            logger.warning("checkout_manager: origin's default branch %r could not be fetched", fetched_branch)
            raise BaseBranchUnavailableError(fetched_branch)
        return True, None
    try:
        return resolve_rebound_branch_plan(
            git_service, base_dir, branch, project_id=project_id, runner_id=runner_id,
        )
    except WorktreeError as e:
        logger.warning(
            "checkout_manager: branch %r not materializable on this runner: %s",
            branch,
            e,
        )
        return None
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(
            "checkout_manager: branch-plan resolution failed for %s: %s", base_dir, e
        )
        return None


def _resolve_clone_url(origin: str, project_v2_row: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """The first URL :func:`_clone_candidates` would try, else ``None``."""
    candidates = _clone_candidates(origin, project_v2_row)
    return candidates[0] if candidates else None


def _clone_candidates(origin: str, project_v2_row: Optional[Dict[str, Any]] = None) -> List[str]:
    """The remote URLs to clone a project_v2 ``origin`` from, in order.

    Consumed by ``_seed_base_checkout`` to clone a base checkout on demand when a
    pinned runner has no local materialization: it tries them in order and moves
    on only when a clone is refused (``project_setup.clone_failure_retries``).

    The list is ``project_setup.clone_candidates``, as Workers setup builds
    it: a pinned project clones its pin only; otherwise the URLs that name
    ``origin`` (the replicated ``projects_v2`` row's stored remote first, which
    survives the last local record of the project going away, then legacy
    records, as the seed picked before), the origin's own spelling before the
    other one (git@ vs https), then the other spelling of each on a public
    forge. A URL names ``origin`` when ``origin`` is
    one of its ``remote_link_ids``: the same deterministic, install-independent
    hash the v60 collapse used to assign origins, so a match is a true
    identity, not a heuristic, and a fork linked into the group is never cloned
    for it. The first URL is as stored, as before; the others carry no
    credentials.

    Only a ``git:…`` origin can ever name a remote, so ``local:`` / ``manual:``
    origins are hard-fail excluded up front with no DB lookup. Every no-match
    path returns an empty list explicitly. Best-effort: a DB error logs and
    leaves only the stored remote, so a caller degrades to today's "not on this
    device" error.
    """
    if not origin:
        return []
    if origin.startswith("local:") or origin.startswith("manual:"):
        return []
    from app.core import project_setup

    stored = None
    if (project_v2_row or {}).get("origin") == origin:
        stored = project_setup.stored_candidate(project_v2_row)
    try:
        records = project_setup.clone_records()
    except Exception as e:  # pragma: no cover - defensive; resolution is best-effort
        logger.warning(
            "checkout_manager: clone-url lookup failed for origin %r: %s", origin, e
        )
        records = []
    return [c["url"] for c in project_setup.clone_candidates(
        origin, records, stored, clean_first=False, stored_first=True)]


def _remote_kind(url: str) -> str:
    from app.core.project_setup import remote_kind

    return remote_kind(url)


def _retry_with_next_url(err: Optional[str]) -> bool:
    """Whether a refused clone may try the project's next URL."""
    from app.core import project_setup

    return project_setup.clone_failure_retries(err or "")


def _project_self_seed_enabled(project_v2_row: Dict[str, Any]) -> bool:
    """Whether this project permits lazy self-seed (the per-project opt-out).

    Default ON: only an EXPLICIT ``projects_v2.settings.self_seed = false`` disables
    on-demand cloning for THIS project (a missing key, ``None``, or any truthy value
    keeps the default-ON behavior). ``projects_v2`` rows carry ``settings`` already
    decoded to a dict by ``db.projects_v2._row``; a non-dict is treated as absent
    (default ON) rather than raising. Explicit ``is False`` only — never an
    ``or``-fallback chain (standing repo rule).
    """
    proj_settings = project_v2_row.get("settings")
    if not isinstance(proj_settings, dict):
        return True
    return proj_settings.get("self_seed") is not False


def _seed_base_checkout(
    project_v2_row: Optional[Dict[str, Any]],
    project_id: str,
    runner_id: str,
) -> Optional[str]:
    """Lazily self-seed a MISSING base checkout by cloning the project's remote.

    Called by ``_ensure_base_checkout`` only when no base checkout is materialized on
    this runner (the pinned-runner-never-cloned case). Gated behind
    ``AI_SHUFFLE_SELF_SEED`` (``settings.self_seed_checkouts``, default ON) AND a
    per-project opt-out (``projects_v2.settings.self_seed`` — an explicit ``false``
    disables it for THIS project only). When either gate is off,
    NOTHING is cloned and the caller falls through to today's honest "not
    materialized here" (``None``).

    The cloneable URL is recovered from legacy data via ``_resolve_clone_url``; when
    that returns ``None`` (``local:``/``manual:`` origins, no matching legacy repo, or
    a DB error) a clear warning is logged and NOTHING is cloned — again ``None`` and
    the existing missing-checkout behavior. Never an ``or``-fallback chain: every path
    handles ``None`` explicitly.

    On a resolvable URL the seed does a FULL clone (``git_service.clone_repo``, which
    disables git's interactive credential prompt) into a temp sibling dir under this
    runner's own ``_checkout_root()``, then an atomic rename into
    ``{project_slug}/__base__`` and an ``upsert_checkout`` of the base row. Any failure
    removes the temp dir and leaves no partial checkout, returning ``None``.
    """
    if not settings.self_seed_checkouts:
        return None
    if not project_v2_row:
        return None
    if not _project_self_seed_enabled(project_v2_row):
        logger.info(
            "checkout_manager: self-seed disabled for project %s via "
            "projects_v2.settings.self_seed=false; leaving unmaterialized",
            project_id,
        )
        return None

    origin = project_v2_row.get("origin")
    clone_urls = _clone_candidates(origin, project_v2_row) if origin else []
    if not clone_urls:
        logger.warning(
            "checkout_manager: cannot self-seed base checkout for project %s "
            "(origin %r): no cloneable remote URL resolved; leaving unmaterialized",
            project_id,
            origin,
        )
        return None

    slug_dir = _checkout_root() / _project_slug(project_v2_row)
    base_path = slug_dir / _BASE_DIR_NAME
    base_str = str(base_path)

    # Idempotency / never-destroy: if something already occupies the base path,
    # adopt it when it is a valid checkout, else refuse to touch it (the
    # never-destructive rule) rather than clobbering it with a fresh clone.
    if base_path.exists():
        if _dir_is_worktree(base_str):
            checkouts_db.upsert_checkout(project_id, runner_id, base_str, status="active")
            return base_str
        logger.warning(
            "checkout_manager: %s exists but is not a git checkout; refusing to seed over it",
            base_str,
        )
        return None

    try:
        slug_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:  # pragma: no cover - defensive
        logger.warning("checkout_manager: cannot create slug dir %s: %s", slug_dir, e)
        return None

    # Each URL is one spelling of the project's repository (git@ vs https):
    # this runner may hold the credentials for, or reach, only one of them, so
    # a refused clone tries the next. A pin is the only URL. A failed try's
    # temp dir is removed before the next.
    for index, clone_url in enumerate(clone_urls):
        try:
            temp_dir = tempfile.mkdtemp(prefix=f"{_BASE_DIR_NAME}.seed-", dir=str(slug_dir))
        except OSError as e:  # pragma: no cover - defensive
            logger.warning(
                "checkout_manager: cannot create temp seed dir under %s: %s", slug_dir, e
            )
            return None

        success, err = git_service.clone_repo(temp_dir, clone_url)
        if success:
            if index:
                logger.info(
                    "checkout_manager: self-seed for project %s cloned over %s after "
                    "another spelling of its remote was refused",
                    project_id,
                    _remote_kind(clone_url),
                )
            break
        _rmtree_quiet(temp_dir)
        retry = index + 1 < len(clone_urls) and _retry_with_next_url(err)
        logger.warning(
            "checkout_manager: self-seed clone failed for project %s over %s: %s%s",
            project_id,
            _remote_kind(clone_url),
            err,
            "; trying another spelling of its remote" if retry else "",
        )
        if not retry:
            return None

    try:
        os.rename(temp_dir, base_str)
    except OSError as e:
        logger.warning(
            "checkout_manager: atomic rename of seeded base %s -> %s failed: %s",
            temp_dir,
            base_str,
            e,
        )
        _rmtree_quiet(temp_dir)
        return None

    checkouts_db.upsert_checkout(project_id, runner_id, base_str, status="active")
    logger.info(
        "checkout_manager: self-seeded base checkout for project %s at %s",
        project_id,
        base_str,
    )
    return base_str


def _rmtree_quiet(path: str) -> None:
    """Best-effort recursive delete of a seed temp dir; never raises.

    The seed is a Git clone: force_rmtree also removes Git's read-only
    objects, which a plain rmtree leaves behind on Windows.
    """
    try:
        force_rmtree(path, ignore_errors=True)
    except Exception as e:  # pragma: no cover - ignore_errors already swallows
        logger.warning("checkout_manager: temp-dir cleanup failed for %s: %s", path, e)


def _ensure_base_checkout(
    project_id: str,
    runner_id: str,
    project_v2_row: Optional[Dict[str, Any]] = None,
    *,
    discover_worktrees: bool = False,
) -> Optional[str]:
    """Resolve an available base; only seed when no registration or row exists."""
    rows, registered = checkouts_db.base_checkout_candidates(project_id, runner_id)
    if not rows and not registered:
        return _seed_base_checkout(project_v2_row, project_id, runner_id)
    for row in rows:
        path = row.get("path")
        if not path:
            continue
        resolved = _resolve_checkout_path(path)
        if not os.path.isdir(resolved):
            continue
        # Retained folders serve bound chats, not catalog/worktree discovery.
        if discover_worktrees and checkouts_db.is_active(row):
            register_unregistered_worktrees(project_id, runner_id, resolved)
        return resolved
    return None


def _adopt_legacy_worktree(
    project_v2_row: Dict[str, Any],
    project_id: str,
    runner_id: str,
    branch: str,
) -> Optional[str]:
    """Lazily adopt an EXISTING legacy ``worktrees`` dir for ``branch`` — the lazy
    counterpart of the v60 collapse migration Step E, symmetric with
    ``project_resolver._effective_branch`` (the lazy Step F).

    The one-time v60 Step E folded every legacy ``worktrees`` row into a branch
    ``checkouts`` row. Legacy worktrees minted AFTER that one-time seed were never
    seeded, so they carry no ``checkouts`` row and nothing at the deterministic
    path. Two live sources still produce them post-collapse:

    * an un-upgraded install sharing the DB, still writing ``worktrees`` rows; and
    * the legacy ExecutionModeBar/WS ``worktree_id`` intake, which materializes
      into the old md5-hash worktree dir and sets ``sessions.worktree_branch``.

    Such a branch is ALREADY checked out on disk in its legacy dir. Falling through
    to deterministic materialization would run ``git worktree add`` at a fresh path
    and fail with "branch already checked out at <legacy md5 path>" (and, absent
    that guard, double-materialize a branch already present). So — before
    ``_branch_plan``/materialization — resolve that existing dir and adopt it.

    Mirrors Step E's join (``worktrees`` -> ``git_repositories`` -> ``projects``)
    and is status-blind like Step E (the dir is stat'd at use time, the real
    check). The legacy project's origin is derived exactly as
    ``project_resolver.origin_for_legacy_project`` (``link_group_id`` else
    ``'local:' + id``) and matched against this project_v2's ``origin``. For the
    first candidate whose ``worktree_path`` exists on this runner's disk AND is a
    valid worktree (disk presence = this-runner locality, the same proxy the
    module already uses), upsert the branch ``checkouts`` row and return the path.
    No candidate → ``None`` → the caller falls through to ordinary deterministic
    materialization unchanged.
    """
    origin = project_v2_row.get("origin")
    if not origin:
        return None
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT w.worktree_path AS worktree_path, "
                "p.id AS legacy_id, p.link_group_id AS link_group_id "
                "FROM worktrees w "
                "JOIN git_repositories gr ON gr.id = w.repository_id "
                "JOIN projects p ON p.id = gr.project_id "
                "WHERE w.branch_name = %s AND w.worktree_path IS NOT NULL",
                (branch,),
            )
            candidates = cursor.fetchall()
    except Exception as e:  # pragma: no cover - defensive; adopt is best-effort
        logger.warning(
            "checkout_manager: legacy-worktree lookup failed for branch %r: %s",
            branch,
            e,
        )
        return None

    for cand in candidates:
        # origin match, exactly like project_resolver.origin_for_legacy_project.
        legacy_origin = cand["link_group_id"] or ("local:" + str(cand["legacy_id"]))
        if legacy_origin != origin:
            continue
        path = cand["worktree_path"]
        if path and os.path.isdir(path) and _dir_is_worktree(path):
            return _accept_branch_checkout(path, project_id, runner_id, branch)
    return None


def _reuse_branch_checkout_locked(
    project_v2_row: Dict[str, Any], project_id: str, runner_id: str, branch: str, dest: Path,
) -> Tuple[bool, Optional[str]]:
    """Under the common lock, resolve reuse/refusal before considering an add.

    A negative discovery snapshot is never permission to invalidate a row:
    another resolver may publish while branch planning runs outside this lock.
    """
    dest_str = str(dest)
    # Fast path: an existing branch checkout row whose dir is a live worktree.
    row = checkouts_db.get_checkout(project_id, runner_id, branch=branch)
    if row:
        path = row.get("path")
        # Normalize a workspace-relative legacy path for the on-disk checks
        # and return; the raw DB value is left untouched (read-only normalization).
        resolved = _resolve_checkout_path(path) if path else path
        if resolved and os.path.isdir(resolved):
            if _dir_is_worktree(resolved):
                return True, _accept_branch_checkout(
                    resolved, project_id, runner_id, branch, reattach=True,
                )
            # A Windows cwd lock can leave an empty directory after Git removes
            # its contents. The recorded path identifies this checkout even if
            # older cleanup failed to save its SHA. Git can reuse it without
            # deletion; the branch plan below still requires a recoverable tip.
            if not _is_empty_cleanup_remnant(resolved, row):
                logger.warning(
                    "checkout_manager: %s exists but is not a worktree; refusing to touch it",
                    resolved,
                )
                return True, None
        # Row exists but its dir is gone → fall through and re-materialize below
        # (at the same deterministic path, so the upsert reuses the same row).

    # Adopt an already-materialized dir at the deterministic path (idempotent).
    if dest.exists():
        if _dir_is_worktree(dest_str):
            return True, _accept_branch_checkout(dest_str, project_id, runner_id, branch)
        if not _is_empty_cleanup_remnant(dest_str, row):
            logger.warning(
                "checkout_manager: %s exists but is not a worktree; refusing to touch it",
                dest_str,
            )
            return True, None

    # Lazy Step-E mirror: a branch already checked out in a post-collapse legacy
    # worktree dir (un-upgraded install or the legacy WS worktree_id intake) must
    # resolve to THAT dir — materializing at the deterministic path would fail on
    # "branch already checked out" and double-materialize.
    legacy_path = _adopt_legacy_worktree(project_v2_row, project_id, runner_id, branch)
    if legacy_path is not None:
        return True, legacy_path
    return False, None


def _ensure_branch_checkout(
    project_v2_row: Dict[str, Any],
    project_id: str,
    runner_id: str,
    branch: str,
    *,
    base_branch: Optional[str] = None,
    create_new_branch: bool = False,
    provision_runtime: bool = True,
) -> Optional[str]:
    """Resolve a branch checkout; workers can opt out of shared runtime links."""
    # A branch checkout requires a materialized base checkout to `git worktree add`
    # from. No base here → not materializable (return None; caller may 409/fallback).
    # Pass the row so a missing base can lazily self-seed before the worktree.
    base_path = _ensure_base_checkout(project_id, runner_id, project_v2_row)
    if not base_path:
        return None
    try:
        dest = _branch_checkout_path(base_path, branch)
    except WorktreeError as e:
        logger.warning("checkout_manager: refusing checkout for branch %r: %s", branch, e)
        return None
    dest_str = str(dest)
    with repo_worktree_lock(base_path):
        # Plain folders/enclosing repositories must still be refused before
        # branch planning or any row/directory mutation.
        _require_checkout_root(base_path)
        decided, path = _reuse_branch_checkout_locked(
            project_v2_row, project_id, runner_id, branch, dest,
        )
        if decided:
            return path

    from app.core.worker_checkout import _project_lock

    # Only new-chat creation joins dispatch's project lock. Existing checkout
    # lookup, recovery/clone, and runtime setup never hold this lock.
    with _project_lock(project_id) if create_new_branch else nullcontext():
        plan = _branch_plan(
            base_path, branch, base_branch=base_branch, create_new_branch=create_new_branch,
            project_id=project_id, runner_id=runner_id,
        )
        # Lock order is project admission (new branches only), then repository.
        # Never hold the repository lock while waiting for project admission.
        with repo_worktree_lock(base_path):
            _require_checkout_root(base_path)
            decided, path = _reuse_branch_checkout_locked(
                project_v2_row, project_id, runner_id, branch, dest,
            )
            if decided:
                return path
            if plan is None:
                # Never fabricate a branch that cannot be recovered here.
                return None
            new_branch, plan_base = plan
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                logger.warning("checkout_manager: could not create worktree parent %s: %s", dest.parent, e)
                return None
            row = checkouts_db.get_checkout(project_id, runner_id, branch=branch)
            if row:
                checkouts_db.set_checkout_status(row["id"], "incomplete")
            success, err = git_service.add_worktree(
                main_dir=base_path,
                path=dest_str,
                branch=branch,
                new_branch=new_branch,
                base_branch=plan_base,
                fetch_base=not create_new_branch,
            )
            if not success:
                # Invalidate even a row published by an older/foreign writer.
                latest = checkouts_db.get_checkout(project_id, runner_id, branch=branch)
                if latest and latest.get("status") == "active":
                    checkouts_db.set_checkout_status(latest["id"], "incomplete")
            else:
                # Finish post-add validation before another resolver can adopt
                # this path and intentionally edit its now-published checkout.
                _require_branch_binding(dest_str, branch)
                _require_complete_checkout(dest_str)
    if not success:
        # Keep write-ahead evidence, including the recorded row's incomplete
        # status. New failures have no row and also require validation on reuse.
        logger.warning(
            "checkout_manager: git worktree add failed for branch %r at %s: %s",
            branch,
            dest_str,
            err,
        )
        raise WorktreeAddError(
            f"Git worktree add failed for branch '{branch}': {worktree_error_detail(err)}"
        )

    # Everything a run needs is gitignored, so the new worktree has none of it.
    # Link it from the checkout it branched off before anyone tries to build or
    # run in it. Best-effort by contract: a failure here degrades the worktree,
    # it does not invalidate it.
    try:
        provisioning = (provision_worktree_runtime(base_path, dest_str)
                        if provision_runtime else {"skipped": ["isolated worker runtime"]})
        # Said out loud on one line, because the alternative is somebody
        # spending ten minutes deciding whether a build failure is their code.
        logger.info(
            "checkout_manager: provisioned %s — linked=%s installed=%s "
            "synced=%s skipped=%s errors=%s",
            dest_str,
            provisioning.get("linked"),
            provisioning.get("installed"),
            provisioning.get("synced"),
            provisioning.get("skipped"),
            provisioning.get("errors"),
        )
    except Exception as e:  # noqa: BLE001 - defence in depth
        logger.warning(
            "checkout_manager: runtime provisioning raised for %s: %s", dest_str, e
        )

    # Provisioning may take long enough for another process to change HEAD.
    _accept_branch_checkout(dest_str, project_id, runner_id, branch)
    logger.info(
        "checkout_manager: materialized branch checkout for project %s "
        "(branch %r) at %s",
        project_id,
        branch,
        dest_str,
    )
    return dest_str


# ---------------------------------------------------------------------------
# Node provisioning for a fresh worktree
# ---------------------------------------------------------------------------

# A worktree starts with no node_modules — it is gitignored, so `git worktree
# add` never brings it across. Every frontend command in the new checkout then
# fails until someone reinstalls a tree that already exists one directory over,
# for the same project. Linking it is instant — but only correct while the
# branch wants the same packages the source tree has installed.
#
# When it doesn't, the link is worse than nothing. A branch that upgraded
# Tailwind (3 -> 4, adding @tailwindcss/vite) linked against a source tree still
# on the old install, and every frontend command in that worktree failed in a
# way that reads like a code bug rather than a provisioning one: "Cannot
# optimize dependency", "Can't resolve 'tailwindcss'", 152 phantom svelte-check
# errors.
#
# So provisioning checks freshness before it links: npm's own record of the
# installed tree (node_modules/.package-lock.json) against the WORKTREE's
# package-lock.json. Satisfied -> link, instantly, as before. Not satisfied ->
# install into the worktree's own package dir instead, with `npm ci`, which is
# how CI and the Dockerfile install this repo. Unknowable (no readable
# lockfile) -> link, because provisioning never had this check and an
# unreadable lockfile is not worth minutes of install time.

_SVELTE_CONFIG_NAMES = (
    "svelte.config.js",
    "svelte.config.ts",
    "svelte.config.mjs",
    "svelte.config.cjs",
)

# `svelte-kit sync` generates .svelte-kit/ (tsconfig, $types, route manifests).
# Without it svelte-check and vite fail on a fresh checkout.
_SVELTE_SYNC_TIMEOUT_SECONDS = 300


def _is_sveltekit_package(package: Path) -> bool:
    """Whether ``package`` is a SvelteKit app that needs `svelte-kit sync`.

    SvelteKit 2 projects have a svelte.config file. SvelteKit 3 configures the
    kit in vite.config instead, so the dependency itself is the other signal.
    """
    try:
        if any((package / name).is_file() for name in _SVELTE_CONFIG_NAMES):
            return True
        manifest = json.loads((package / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(manifest, dict):
        return False
    return any(
        isinstance(manifest.get(section), dict) and "@sveltejs/kit" in manifest[section]
        for section in ("dependencies", "devDependencies")
    )


def _keeps_generated_types_in_node_modules(modules: Path) -> bool:
    """Whether the SvelteKit installed in ``modules`` generates into it.

    SvelteKit 3's sync writes ``node_modules/$app/tsconfig.json`` and the app
    types under ``node_modules/$app/types``, where SvelteKit 2 wrote them to
    the package's own ``.svelte-kit``. The generated tsconfig points at the
    sources with paths relative to itself, so a tree shared with another
    checkout resolves ``$lib`` in THAT checkout, and each checkout's sync
    overwrites the other's types.
    """
    manifest = _read_json_file(modules / "@sveltejs" / "kit" / "package.json")
    version = (manifest or {}).get("version")
    try:
        return int(str(version).split(".", 1)[0]) >= 3
    except ValueError:
        return False


def _sibling_modules_owner(source: Path, source_modules: Path) -> Optional[Path]:
    """The sibling package whose node_modules ``source_modules`` links to.

    A package may share another package's tree inside one checkout (a second
    app compiled from the same sources). Returns that package's path relative
    to the checkout, or ``None`` when ``source_modules`` is the package's own.
    """
    try:
        # Resolved rather than is_symlink(): a Windows junction is not one.
        target = source_modules.resolve()
        if target.name != "node_modules" or target.parent == source_modules.parent.resolve():
            return None
        return target.parent.relative_to(source.resolve())
    except (OSError, ValueError):
        return None


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def shares_generated_types_with_another_checkout(modules: Path, checkout: Path) -> bool:
    """Whether ``modules`` is a SvelteKit 3 tree that lives outside ``checkout``.

    True for a link provisioned before SvelteKit 3 arrived: every sync, check
    or build through it reads and overwrites the other checkout's types.
    """
    return _keeps_generated_types_in_node_modules(modules) and not _is_inside(modules, checkout)


# The project's lockfile, and npm's own record of what a node_modules actually
# holds (npm writes the latter on every install).
_LOCKFILE_NAME = "package-lock.json"
_INSTALLED_TREE_RECORD = ".package-lock.json"

# A cold `npm ci` on this repo's frontend takes a couple of minutes; a slow
# registry can take several. Bounded so a hung install cannot pin a checkout
# forever, generous enough that a normal one is never cut short.
_NPM_INSTALL_TIMEOUT_SECONDS = 1200


def _node_package_dirs(root: Path) -> List[Path]:
    """Directories under ``root`` that look like a node package.

    The root itself plus its immediate subdirectories. One level is enough for
    the common "the app lives in frontend/" layout without walking a whole
    repository, and it keeps the cost at a single listdir.
    """
    found: List[Path] = []
    try:
        if (root / "package.json").is_file():
            found.append(root)
        children = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError as e:
        logger.debug("checkout_manager: cannot scan %s for packages: %s", root, e)
        return found

    for child in children:
        if child.name.startswith(".") or child.name == "node_modules":
            continue
        try:
            if (child / "package.json").is_file():
                found.append(child)
        except OSError:  # pragma: no cover - defensive
            continue
    return found


def _link_directory(source: Path, dest: Path) -> None:
    """Point ``dest`` at ``source``: symlink on POSIX, junction on Windows.

    The worker is Linux, where a plain directory symlink is enough. On a
    Windows dev host an unprivileged symlink is refused, so a junction — which
    needs no privilege — is used instead. Raises on failure; the caller logs.
    """
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(source), str(dest))
    else:
        os.symlink(str(source), str(dest), target_is_directory=True)


def _read_json_file(path: Path) -> Optional[Dict[str, Any]]:
    """Parse a JSON object from ``path``, or ``None`` if it cannot be read."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _required_packages(lockfile: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """The ``node_modules/...`` -> version map a lockfile says must be present.

    Optional and platform-gated entries are dropped: npm legitimately leaves
    them uninstalled on a machine they do not apply to, so demanding them would
    make every tree look stale. Returns ``None`` for a lockfile shape this does
    not understand (npm 6 and older, where there is no ``packages`` map).
    """
    packages = lockfile.get("packages")
    if not isinstance(packages, dict):
        return None
    required: Dict[str, str] = {}
    for key, entry in packages.items():
        if not isinstance(key, str) or not key.startswith("node_modules/"):
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("optional") or entry.get("os") or entry.get("cpu"):
            continue
        version = entry.get("version")
        if isinstance(version, str) and version:
            required[key] = version
    return required


def _installed_tree_verdict(source_modules: Path, dest_package: Path) -> Tuple[str, str]:
    """Whether ``source_modules`` satisfies ``dest_package``'s lockfile.

    Returns ``(verdict, reason)``. ``"match"`` means the link is safe,
    ``"stale"`` means it would hand the branch the wrong packages, and
    ``"unknown"`` means there is nothing to compare — which links, keeping the
    behaviour provisioning has always had.
    """
    if not source_modules.is_dir() or next(source_modules.iterdir(), None) is None:
        return "stale", "source node_modules is missing or empty"
    dest_lock = _read_json_file(dest_package / _LOCKFILE_NAME)
    if dest_lock is None:
        return "unknown", "worktree has no readable lockfile"
    required = _required_packages(dest_lock)
    if not required:
        return "unknown", "worktree lockfile names no packages"
    missing = [key for key in required
               if not (source_modules / key[len("node_modules/"):]).is_dir()]
    if missing:
        return "stale", f"{len(missing)} required package directory(s) missing ({', '.join(missing[:3])})"

    installed_record = _read_json_file(source_modules / _INSTALLED_TREE_RECORD)
    if installed_record is None:
        # No npm record of the source install (npm 6, or a tree assembled by
        # hand). Comparing the two LOCKFILES still catches the case that hurts:
        # a branch that moved its dependencies away from the source checkout's.
        source_lock = _read_json_file(source_modules.parent / _LOCKFILE_NAME)
        if source_lock is None:
            return "unknown", "source tree has no npm install record"
        if _required_packages(source_lock) == required:
            return "match", "source lockfile matches the worktree's"
        return "stale", "source lockfile differs from the worktree's"

    installed = _required_packages(installed_record)
    if installed is None:
        return "unknown", "unreadable npm install record"

    differing = sorted(
        key for key, version in required.items() if installed.get(key) != version
    )
    if not differing:
        return "match", "installed tree satisfies the worktree's lockfile"
    names = [key[len("node_modules/"):] for key in differing[:3]]
    suffix = ", ..." if len(differing) > 3 else ""
    return (
        "stale",
        f"{len(differing)} package(s) missing or at another version "
        f"({', '.join(names)}{suffix})",
    )


def _run_npm_install(package_dir: Path) -> Tuple[bool, str]:
    """Install ``package_dir``'s own dependencies. Returns ``(ok, detail)``.

    ``npm ci`` when there is a lockfile — the reproducible install CI and the
    Dockerfile use — and ``npm install`` when there is not, since ``npm ci``
    refuses to run without one.
    """
    if (package_dir / _LOCKFILE_NAME).is_file():
        command = ["npm", "ci", "--no-audit", "--no-fund"]
    else:
        command = ["npm", "install", "--no-audit", "--no-fund"]
    printable = " ".join(command)
    try:
        result = subprocess.run(
            command,
            cwd=str(package_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_NPM_INSTALL_TIMEOUT_SECONDS,
            shell=(os.name == "nt"),
            env=scrub_child_env(),
        )
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"{printable} could not run: {e}"

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:400]
        return False, f"{printable} failed (rc={result.returncode}): {detail}"
    return True, printable


def _run_svelte_kit_sync(package_dir: Path) -> bool:
    """Run only the installed local CLI; never let npx download a missing one."""
    cli = package_dir / "node_modules" / "@sveltejs" / "kit" / "svelte-kit.js"
    if not cli.is_file():
        return False
    try:
        result = subprocess.run(
            ["node", str(cli), "sync"],
            cwd=str(package_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_SVELTE_SYNC_TIMEOUT_SECONDS,
            shell=(os.name == "nt"),
            env=scrub_child_env(),
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug(
            "checkout_manager: svelte-kit sync could not run in %s: %s",
            package_dir,
            e,
        )
        return False

    if result.returncode != 0:
        logger.debug(
            "checkout_manager: svelte-kit sync failed in %s (rc=%s): %s",
            package_dir,
            result.returncode,
            (result.stderr or "").strip()[:400],
        )
        return False
    return True


def provision_node_modules(
    source_root: str, dest_root: str, *, allow_install: bool = False
) -> Dict[str, List[str]]:
    """Give a fresh worktree the source checkout's installed node packages.

    For each package directory the two checkouts share, either links the
    source's ``node_modules`` into the worktree (when the source's installed
    tree satisfies the WORKTREE's lockfile). Stale or empty dependencies are
    reported as setup-required; checkout creation never silently installs them.
    A SvelteKit 3 app is never linked: its generated types live in node_modules
    and belong to one checkout, so it always needs its own install. A package
    that shares a sibling's SvelteKit 3 tree gets the same link inside the
    worktree; every other tree is shared across checkouts as before.
    A caller that obtained explicit setup approval may pass allow_install=True.
    Then runs the installed local ``svelte-kit sync`` where configured; no npx
    fallback may download missing tools.

    Entirely best-effort and never raises: a worktree without node_modules is
    degraded, not broken, and it must not cost anyone a checkout. Returns a
    summary of what happened — including which path each package took, so the
    provisioning log answers "why does this branch not build" on its own —
    which is what the tests assert on.
    """
    summary: Dict[str, List[str]] = {
        "linked": [],
        "installed": [],
        "synced": [],
        "skipped": [],
        "errors": [],
    }
    try:
        source = Path(source_root)
        dest = Path(dest_root)
        packages = _node_package_dirs(dest)
    except Exception as e:  # noqa: BLE001 - never fail a checkout over this
        logger.warning("checkout_manager: node provisioning aborted: %s", e)
        summary["errors"].append(str(e))
        return summary

    for package in packages:
        rel = package.relative_to(dest)
        source_modules = source / rel / "node_modules"
        dest_modules = package / "node_modules"
        label = str(rel) if str(rel) != "." else "<root>"

        try:
            if dest_modules.exists() or dest_modules.is_symlink():
                summary["skipped"].append(f"{label}: already present")
            elif not source_modules.is_dir():
                summary["skipped"].append(f"{label}: source has no node_modules")
                continue
            elif (
                owner := _sibling_modules_owner(source, source_modules)
            ) is not None and (
                _keeps_generated_types_in_node_modules(source_modules)
                or _keeps_generated_types_in_node_modules(dest / owner / "node_modules")
            ):
                # The source package uses a sibling's tree, and that tree (or
                # the one this worktree has in its place) is SvelteKit 3's.
                # Mirror the link inside the worktree; never reach through to
                # the source checkout's. Any other shared tree links below, as
                # it always has.
                own = dest / owner / "node_modules"
                if not own.is_dir() or not _is_inside(own, dest):
                    summary["skipped"].append(
                        f"{label}: setup required (shares {owner.as_posix()}/node_modules, "
                        "which this worktree does not have yet)"
                    )
                    continue
                _link_directory(own, dest_modules)
                summary["linked"].append(label)
            else:
                verdict, reason = _installed_tree_verdict(source_modules, package)
                if verdict != "stale" and _keeps_generated_types_in_node_modules(source_modules):
                    verdict = "stale"
                    reason = (
                        "SvelteKit 3 keeps this checkout's generated types in "
                        "node_modules, so the tree cannot be shared"
                    )
                if verdict == "stale":
                    if not allow_install:
                        summary["skipped"].append(
                            f"{label}: setup required ({reason}); install not authorized"
                        )
                        continue
                    logger.info(
                        "checkout_manager: not linking node_modules for %s into %s "
                        "(%s) — installing the worktree's own dependencies",
                        label,
                        package,
                        reason,
                    )
                    ok, detail = _run_npm_install(package)
                    if not ok:
                        summary["errors"].append(f"{label}: install failed: {detail}")
                        logger.warning(
                            "checkout_manager: could not install node_modules for "
                            "%s in %s: %s",
                            label,
                            package,
                            detail,
                        )
                        # No modules means sync cannot work either.
                        continue
                    summary["installed"].append(f"{label}: {detail} ({reason})")
                    logger.info(
                        "checkout_manager: installed node_modules for %s in %s "
                        "via %s",
                        label,
                        package,
                        detail,
                    )
                else:
                    _link_directory(source_modules, dest_modules)
                    summary["linked"].append(label)
                    logger.info(
                        "checkout_manager: linked node_modules for %s into %s (%s)",
                        label,
                        package,
                        reason,
                    )
        except Exception as e:  # noqa: BLE001
            summary["errors"].append(f"{label}: link failed: {e}")
            logger.warning(
                "checkout_manager: could not provision node_modules for %s in %s: %s",
                label,
                package,
                e,
            )
            # No modules means sync cannot work either.
            continue

        if not _is_sveltekit_package(package):
            continue
        if shares_generated_types_with_another_checkout(dest_modules, dest):
            # A tree linked from another checkout (an older worktree): sync
            # would write this checkout's types over that checkout's.
            summary["errors"].append(
                f"{label}: svelte-kit sync skipped: node_modules belongs to another "
                "checkout; install this worktree's own dependencies"
            )
            continue
        if _run_svelte_kit_sync(package):
            summary["synced"].append(label)
        else:
            summary["errors"].append(f"{label}: svelte-kit sync failed")

    return summary


# ---------------------------------------------------------------------------
# The rest of what a runnable worktree needs
# ---------------------------------------------------------------------------

# node_modules is not the only gitignored thing a run depends on. The Python
# virtualenv and the built frontend assets are gitignored too, so a fresh
# worktree has neither, and every attempt to actually run the app from one used
# to end with somebody wiring them up by hand. These are the directories worth
# linking; each is skipped when the source does not have it.
_RUNTIME_LINK_DIRS = (
    "venv",
    ".venv",
    "app/static",
)

# Files that provisioning must NEVER place in a worktree, however tempting.
#
# `.env` holds the database password in plaintext. It is gitignored, so a
# worktree never gets one, and the observed workaround was to copy the real one
# in — which put the production password on disk once per worktree, silently
# pointed the branch's app at the production database, and left the copies
# behind when the worktree was reaped. Configuration reaches a worktree process
# by reference instead: the spawner exports AI_SHUFFLE_ENV_FILE with the
# absolute path of the canonical file (app.core.environment), and
# app.core.config reads it from there. Nothing is copied.
_NEVER_PROVISION = (".env",)


def _is_never_provisioned(name: str) -> bool:
    """Whether ``name`` is a config/secret file provisioning must not carry."""
    base = os.path.basename(name.replace("\\", "/").rstrip("/"))
    return base in _NEVER_PROVISION or base.endswith(".env")


def provision_worktree_runtime(
    source_root: str, dest_root: str
) -> Dict[str, List[str]]:
    """Give a fresh worktree everything a run needs, and nothing it must not have.

    Links the source checkout's node packages (see ``provision_node_modules``),
    its virtualenv, and its built frontend assets. Deliberately provisions no
    configuration file: secrets travel by env-var reference at spawn time, never
    as a copy on disk. See ``_NEVER_PROVISION``.

    Best-effort throughout and never raises, for the same reason
    ``provision_node_modules`` doesn't: a worktree missing a link is degraded,
    not broken, and must not cost anyone a checkout.
    """
    summary = provision_node_modules(source_root, dest_root)

    try:
        source = Path(source_root)
        dest = Path(dest_root)
    except Exception as e:  # noqa: BLE001 - never fail a checkout over this
        summary["errors"].append(str(e))
        return summary

    # Surface a config file somebody already put here by hand. Not deleted —
    # removing a file the app did not create is not provisioning's call — but
    # said out loud, because a stray `.env` in a worktree is a plaintext
    # credential nobody is tracking and is very likely pointed at production.
    for name in _NEVER_PROVISION:
        try:
            if (dest / name).exists():
                summary["errors"].append(f"{name}: hand-copied config present")
                logger.warning(
                    "checkout_manager: %s holds a hand-copied %s — configuration "
                    "reaches worktree processes by env-var reference; this copy "
                    "is an untracked credential and should be removed",
                    dest_root,
                    name,
                )
        except OSError:  # pragma: no cover - defensive
            continue

    for rel in _RUNTIME_LINK_DIRS:
        if _is_never_provisioned(rel):  # pragma: no cover - guards the constant
            continue
        src_dir = source / rel
        dst_dir = dest / rel
        try:
            if dst_dir.exists() or dst_dir.is_symlink():
                summary["skipped"].append(f"{rel}: already present")
                continue
            if not src_dir.is_dir():
                summary["skipped"].append(f"{rel}: source has none")
                continue
            dst_dir.parent.mkdir(parents=True, exist_ok=True)
            _link_directory(src_dir, dst_dir)
            summary["linked"].append(rel)
            logger.info("checkout_manager: linked %s into %s", rel, dest_root)
        except Exception as e:  # noqa: BLE001
            summary["errors"].append(f"{rel}: link failed: {e}")
            logger.warning(
                "checkout_manager: could not link %s in %s: %s", rel, dest_root, e
            )

    # Keep a bounded, credential-free receipt where subsequent session turns
    # can find it. Current filesystem presence is probed separately.
    from fluid.context.runtime_facts import record_provisioning

    record_provisioning(dest_root, summary)
    return summary


# ---------------------------------------------------------------------------
# Severing the links before a worktree is deleted
# ---------------------------------------------------------------------------

# The links provisioning plants are junctions on Windows, and `git worktree
# remove` there recurses THROUGH a junction into its target (verified on git
# 2.50.1.windows.1, with and without --force): removing a provisioned worktree
# gutted the main checkout's entire frontend/node_modules. shutil.rmtree has
# been junction-safe since Python 3.8, but git must never see the links.
# Unknown nested links are just as dangerous as provisioned ones. Mounts must
# also be refused: neither git nor rmtree is safe across a filesystem boundary.


def _is_directory_link(path: Path, info: Optional[os.stat_result] = None) -> bool:
    """Detect symlinks, junctions and other Windows reparse points without following."""
    # Recovery callers use the Path probe; the removal walk supplies lstat
    # metadata already, avoiding another filesystem lookup for every entry.
    if info is None and path.is_symlink():
        return True
    try:
        info = info if info is not None else os.lstat(path)
    except FileNotFoundError:
        return False
    isjunction = getattr(os.path, "isjunction", None)
    return bool(
        getattr(info, "st_reparse_tag", 0)
        == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", None)
        or stat.S_ISLNK(info.st_mode)
        or (isjunction is not None and isjunction(path))
        or getattr(info, "st_file_attributes", 0)
        & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _remove_directory_link(path: Path) -> None:
    """Remove the link entry itself, never its target's contents."""
    try:
        os.unlink(path)
    except OSError:
        # A junction (and, on some platforms, a directory symlink) unlinks as
        # a directory entry.
        os.rmdir(path)


def _worktree_mount_points() -> set[Path]:
    """Include same-device Linux bind mounts, which ismount/st_dev can miss."""
    if os.sys.platform != "linux":
        return set()
    mounts = set()
    for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
        field = line.split()[4]
        for escaped, plain in ((r"\040", " "), (r"\011", "\t"), (r"\012", "\n"), (r"\134", "\\")):
            field = field.replace(escaped, plain)
        mounts.add(Path(field))
    return mounts


def sever_runtime_links(dest_root: str) -> Dict[str, List[str]]:
    """Preflight recursive removal, detaching ALL links, not just provisioned ones.

    Walk without following symlinks/junctions/reparse points or crossing devices.
    Mount points cannot safely be detached here: refuse them instead. Skip Git's
    administrative internals, but inspect the .git entry itself. Real directories
    and files are left to Git. Any inspection/detachment failure MUST block the
    caller's recursive removal; errors are returned in the summary.
    """
    summary: Dict[str, List[str]] = {"severed": [], "errors": []}
    try:
        dest = Path(os.path.abspath(dest_root))
        try:
            root_info = dest.lstat()
        except FileNotFoundError:
            return summary
        if _is_directory_link(dest, root_info):
            raise ValueError(f"refusing removal: worktree root is a link: {dest}")
        # Check the table before walking, including mounts under .git internals.
        # Resolve only for mount-table comparison, never to traverse a link.
        resolved = dest.resolve()
        for mount in _worktree_mount_points():
            if mount == resolved or resolved in mount.parents:
                raise ValueError(f"refusing removal: mount point inside worktree: {mount}")

        pending = [dest]
        while pending:
            directory = pending.pop()
            info = directory.lstat()
            if _is_directory_link(directory, info):
                raise ValueError(f"refusing removal: directory became a link: {directory}")
            if info.st_dev != root_info.st_dev or os.path.ismount(directory):
                raise ValueError(f"refusing removal: mount point or device boundary: {directory}")
            with os.scandir(directory) as entries:
                for entry in entries:
                    candidate = Path(entry.path)
                    # Windows DirEntry.stat() reports st_dev=0; compare lstat
                    # metadata on both sides without following junctions.
                    info = candidate.lstat()
                    if _is_directory_link(candidate, info):
                        _remove_directory_link(candidate)
                        summary["severed"].append(str(candidate.relative_to(dest)))
                        continue  # Never inspect a link's children.
                    if info.st_dev != root_info.st_dev or os.path.ismount(candidate):
                        raise ValueError(
                            f"refusing removal: mount point or device boundary: {candidate}"
                        )
                    if stat.S_ISDIR(info.st_mode) and entry.name != ".git":
                        pending.append(candidate)
    except Exception as e:  # noqa: BLE001 - fail closed, including inspection errors
        summary["errors"].append(str(e))
        logger.warning("checkout_manager: removal preflight failed for %s: %s", dest_root, e)
    return summary


# ---------------------------------------------------------------------------
# Adopting worktrees this app did not create
# ---------------------------------------------------------------------------


def register_unregistered_worktrees(
    project_id: str, runner_id: str, any_checkout_path: str
) -> List[str]:
    """Give every worktree of this repo a ``checkouts`` row, whoever made it.

    ``git worktree add`` is not the app's private tool. A session's own agent
    runs it directly, and the CLI creates isolated worktrees of its own; both
    land under the same ``.fluid/worktrees/`` tree that the app manages, and
    neither writes a row. The result observed in the field was worktrees that
    no table knew about: nothing reaped them, nothing cleaned them up, and the
    session that made one reported a null worktree id and path while its branch
    was plainly set.

    Registering them is a pure catch-up write. Rows are keyed on
    ``(project, runner, path)`` and ``upsert_checkout`` is idempotent, so an
    already-registered worktree costs one no-op and a worktree the app created
    a moment ago is simply confirmed. Returns the paths newly adopted, which is
    what the tests assert on.

    Best-effort and never raises: failing to adopt someone else's worktree must
    not fail the checkout that triggered the sweep.
    """
    adopted: List[str] = []
    try:
        entries = git_service.list_worktrees(any_checkout_path)
        known = {
            os.path.normcase(os.path.normpath(os.path.abspath(str(r.get("path")))))
            for r in checkouts_db.list_checkouts(project_id)
            if r.get("path")
        }
    except Exception as e:  # noqa: BLE001 - never fail a checkout over this
        logger.debug(
            "checkout_manager: worktree sweep failed for %s: %s",
            any_checkout_path,
            e,
        )
        return adopted

    for entry in entries or []:
        path = entry.get("path")
        if not path or entry.get("is_bare") or not os.path.isabs(path):
            continue
        # A detached worktree has no branch to key a checkout row on; the app's
        # own resolution is branch-keyed, so there is nothing useful to record.
        wt_branch = (entry.get("branch") or "").strip()
        if not wt_branch or entry.get("is_detached"):
            continue
        # `git worktree list --porcelain` reports forward slashes even on
        # Windows. Store the OS form the rest of the app writes, or the row is
        # invisible to every path-keyed lookup that follows.
        path = os.path.normpath(os.path.abspath(path))
        if os.path.normcase(path) in known:
            continue
        if not os.path.isdir(path):
            continue
        try:
            _accept_branch_checkout(path, project_id, runner_id, wt_branch)
        except Exception as e:  # noqa: BLE001 - one failed inspection must not fail the sweep
            logger.warning("checkout_manager: refusing worktree adoption at %s: %s", path, e)
            continue
        adopted.append(path)
        logger.info(
            "checkout_manager: adopted unregistered worktree %s (branch %r) "
            "for project %s",
            path,
            wt_branch,
            project_id,
        )
    return adopted


def new_branch_exists_detail(branch: str) -> str:
    """Refusal detail for a "create new branch" request naming an existing branch.

    Shared by local intake (HTTP 409) and the remote ``ensure_worktree`` op (its
    error). The composer keys its auto-name retry on the leading
    ``Branch '<name>' already exists`` phrase; keep it stable.
    """
    return (
        f"Branch '{branch}' already exists, so it can't be created as a new "
        f"branch for this session. Select it as an existing branch, or choose "
        f"a different name."
    )


def require_worktree_project(project_v2_row: dict[str, Any], runner_id: str | None = None) -> None:
    """Reject an enclosing project before branch lookup or ownership config writes.

    Run on the selected worker, not the caller's potentially different checkout.
    Plain-folder/missing-root handling remains with the normal materializer.
    """
    # Read-only intake: do not self-seed a clone just to validate the scope.
    rows = checkouts_db.list_checkouts_with_legacy_fallback(
        project_v2_row["id"], runner_id or settings.effective_instance_id
    )
    path = next((_resolve_checkout_path(row["path"]) for row in rows
                 if row.get("path") and os.path.isdir(_resolve_checkout_path(row["path"]))), None)
    if not path:
        return
    result = git_service._run_git(path, ["rev-parse", "--show-toplevel"], timeout=5)
    if result.returncode == 0 and result.stdout.strip():
        root, folder = os.path.realpath(result.stdout.strip()), os.path.realpath(path)
        if os.path.normcase(root) != os.path.normcase(folder) and os.path.commonpath([root, folder]) == root:
            raise WorktreeError(
                f"Inside repository {root}. Open that repository root as a project to use worktrees."
            )


def local_branch_exists(
    project_v2_row: dict[str, Any] | None,
    branch: str,
    runner_id: str | None = None,
) -> bool:
    """Whether ``branch`` already exists as a local ref in this runner's clone.

    New-session intake calls this before minting a "new" branch. When the branch
    already exists, :func:`ensure_checkout` would otherwise hand back whatever
    checkout that branch already has — typically another session's working tree
    (#1528). ``False`` when there is no base checkout here; materialization then
    reports that itself.
    """
    branch = (branch or "").strip()
    if not project_v2_row or not branch:
        return False
    project_id = project_v2_row.get("id")
    if not project_id:
        return False
    if runner_id is None:
        runner_id = settings.effective_instance_id
    base_path = _ensure_base_checkout(project_id, runner_id, project_v2_row)
    if not base_path:
        return False
    result = git_service._run_git(
        base_path,
        ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        timeout=10,
    )
    return result.returncode == 0


# ``branch.<name>.<key>`` in the base clone's config: who minted a "new" branch.
# Git drops the whole ``branch.<name>`` section when the branch is deleted, so a
# later, unrelated branch of the same name never inherits an old owner.
_NEW_BRANCH_OWNER_KEY = "aiShuffleNewBranchFor"


def _base_path_for(
    project_v2_row: dict[str, Any] | None, runner_id: str | None
) -> str | None:
    project_id = (project_v2_row or {}).get("id")
    if not project_id:
        return None
    return _ensure_base_checkout(
        project_id, runner_id or settings.effective_instance_id, project_v2_row
    )


def new_branch_owner(
    project_v2_row: dict[str, Any] | None,
    branch: str,
    runner_id: str | None = None,
) -> str | None:
    """The request identity that minted ``branch`` here as a "new" branch.

    Recorded by :func:`record_new_branch_owner` from the remote ``ensure_worktree``
    op. Lets a retried op for the SAME session recognise a branch it already
    minted, while any other session asking to "create" that branch is refused
    (#1528). ``None`` when unrecorded or there is no base checkout here.
    """
    branch = (branch or "").strip()
    base_path = _base_path_for(project_v2_row, runner_id) if branch else None
    if not base_path:
        return None
    result = git_service._run_git(
        base_path,
        ["config", "--local", "--get", f"branch.{branch}.{_NEW_BRANCH_OWNER_KEY}"],
        timeout=10,
    )
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip() or None


def record_new_branch_owner(
    project_v2_row: dict[str, Any] | None,
    branch: str,
    owner: str,
    runner_id: str | None = None,
) -> bool:
    """Record ``owner`` as the minter of new branch ``branch`` (best effort).

    Written BEFORE the branch is created, so a crash between creation and the
    op's settlement still lets that same session's retry succeed. Returns False
    when it could not be written; minting proceeds, only retry recognition is lost.
    """
    branch = (branch or "").strip()
    owner = (owner or "").strip()
    base_path = _base_path_for(project_v2_row, runner_id) if branch and owner else None
    if not base_path:
        return False
    result = git_service._run_git(
        base_path,
        ["config", "--local", f"branch.{branch}.{_NEW_BRANCH_OWNER_KEY}", owner],
        timeout=10,
    )
    if result.returncode != 0:
        logger.warning(
            "checkout_manager: could not record new-branch owner for %r: %s",
            branch, (result.stderr or "").strip(),
        )
        return False
    return True


def ensure_checkout(
    project_v2_row: Optional[Dict[str, Any]],
    branch: Optional[str] = None,
    runner_id: Optional[str] = None,
    *,
    base_branch: Optional[str] = None,
    create_new_branch: bool = False,
    provision_runtime: bool = True,
    raise_add_errors: bool = False,
) -> Optional[str]:
    """Lazily materialize (or resolve) a checkout for a project_v2 on this runner.

    Args:
        project_v2_row: a ``projects_v2`` row (dict with at least ``id``/``name``).
        branch: the logical branch; ``None`` or ``''`` = the base checkout.
        runner_id: defaults to ``settings.effective_instance_id``.
        base_branch: for a branch that must be created, the branch to fork from
            (``add_worktree`` prefers ``origin/{base}``). Ignored for the base
            checkout and for a branch that already exists locally / on origin.
        create_new_branch: intentionally mint ``branch`` as a fresh branch from
            ``base_branch`` (new-session compose). When ``False`` (default) an
            unknown branch is recovered from origin or, failing that, reported as
            not-materializable rather than fabricated.
        provision_runtime: when False, do not share dependencies or built assets
            from the base checkout. Coding workers need independent runtimes.
        raise_add_errors: opt session setup into WorktreeAddError with Git's
            diagnostic when an attempted checkout fails. Defaults to False.

    Returns the absolute checkout path, or ``None`` when it is not materializable
    on this device or Git refuses the checkout. Detailed add errors are raised
    only when explicitly requested. Idempotent and safe to call on every send.
    """
    if not project_v2_row:
        return None
    project_id = project_v2_row.get("id")
    if not project_id:
        return None
    if runner_id is None:
        runner_id = settings.effective_instance_id

    branch = (branch or "").strip()
    if not branch:
        return _ensure_base_checkout(project_id, runner_id, project_v2_row, discover_worktrees=True)
    try:
        path = _ensure_branch_checkout(
            project_v2_row,
            project_id,
            runner_id,
            branch,
            base_branch=base_branch,
            create_new_branch=create_new_branch,
            provision_runtime=provision_runtime,
        )
    except WorktreeAddError as e:
        if raise_add_errors:
            raise
        logger.warning("checkout_manager: refusing checkout for branch %r: %s", branch, e)
        return None
    except WorktreeError as e:
        logger.warning("checkout_manager: refusing checkout for branch %r: %s", branch, e)
        return None
    if path:
        register_unregistered_worktrees(project_id, runner_id, path)
    return path
