"""Per-runner lazy materialization of project_v2 checkouts (v2 core, Phase 3).

A *checkout* is the physical working tree of a ``projects_v2`` project on ONE
runner (``runner_id`` = this install's ``effective_instance_id``). The origin-keyed
``projects_v2`` row is install-independent identity; the ``checkouts`` row is its
per-runner materialization — ``branch`` NULL/'' is the *base* checkout, a non-empty
``branch`` is a logical worktree materialized at its own deterministic path.

This module owns the ONE lazy-materialization entrypoint, ``ensure_checkout``,
which the send/resume cwd seam (P4) will call on every send. It is idempotent and
safe to call repeatedly. Design commitments (Phase 3):

- **Deterministic per-runner path** — ``{workspace_root}/checkouts/{project_slug}/
  {sanitized_branch}`` — replacing ``worktree_manager._get_worktree_path``'s
  ``md5(project_path)[:4]`` hashing. A materialization, never identity, so the same
  branch resolves per-machine without cross-host orphaning.
- **Phase 3 NEVER clones from a remote.** A project with no local base checkout on
  this runner is simply not materializable here (adopt surfaces a 409). Remote
  placement is Phase 4. So ``ensure_checkout`` returns ``None`` — honestly
  "not on this device" — rather than fabricating a clone.
- **Never destructive.** If a directory exists but is not a valid worktree, we log
  and return ``None`` rather than tearing anything down.

No callers are wired in this commit (P4 does that). Reuses ``git_service`` and
``worktree_manager._sanitize_branch_name`` by import (both survive PR-B).
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from app.core.config import settings
from app.core.git_service import git_service
from app.core.project_links import auto_link_group_id
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


def _checkout_root() -> Path:
    """The runner's checkout root: the configured workspace dir + ``checkouts``.

    Read from ``settings.effective_workspace_dir`` (the same source
    ``load_workspace_from_database`` populates) at call time so a runtime
    workspace override is honored. No new setting is introduced.
    """
    return Path(settings.effective_workspace_dir) / _CHECKOUTS_SUBDIR


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


def _branch_checkout_path(project_v2_row: Dict[str, Any], branch: str) -> Path:
    """Deterministic materialization path for a (project, branch) checkout.

    ``{workspace_root}/checkouts/{project_slug}/{sanitized_branch}``. Uses
    ``pathlib`` joins (never string concat with separators) so it is correct on
    Windows and POSIX alike.
    """
    sanitized = worktree_manager._sanitize_branch_name(branch)
    return _checkout_root() / _project_slug(project_v2_row) / sanitized


def _dir_is_worktree(path: str) -> bool:
    """A directory is a usable checkout iff it exists AND carries a ``.git`` entry
    (a FILE for a linked worktree, or a DIRECTORY for a plain clone) — the same
    two-rule liveness test as ``worktree_manager.is_worktree_valid``."""
    p = Path(path)
    return p.is_dir() and (p / ".git").exists()


def _branch_plan(
    base_dir: str,
    branch: str,
    base_branch: Optional[str] = None,
    create_new_branch: bool = False,
) -> Optional[Tuple[bool, Optional[str]]]:
    """Decide new-branch vs existing-branch for ``git worktree add``.

    * ``create_new_branch=True`` — the caller is intentionally minting a fresh
      branch (new-session compose): create it from ``base_branch`` (its own tip;
      ``add_worktree`` prefers ``origin/{base}`` internally). No fetch, no
      recovery — the branch is new by construction.
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
        return True, base_branch
    try:
        return resolve_rebound_branch_plan(git_service, base_dir, branch)
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


def _resolve_clone_url(origin: str) -> Optional[str]:
    """Recover a cloneable remote URL for a project_v2 ``origin`` from legacy data.

    Consumed by the PR-2 self-seed slice (#607) to clone a base checkout on demand
    when a pinned runner has no local materialization. Has NO callers yet — this is
    the inert PR-1 plumbing; PR-2 wires it into ``_ensure_base_checkout``.

    A v2 ``origin`` is the collapse key ``project_resolver.origin_for_legacy_project``
    seeds: the legacy ``link_group_id`` (a ``git:…`` / ``manual:…`` token) or a synth
    ``'local:' + id`` for remote-less rows. Only a ``git:…`` origin can ever name a
    remote, so ``local:`` / ``manual:`` origins are hard-fail excluded up front
    (ruling 5) with no DB lookup. For the rest we scan legacy ``projects`` ->
    ``git_repositories.remote_url`` and return the first URL whose
    ``auto_link_group_id(remote_url)`` reproduces this exact ``origin`` — the same
    deterministic, install-independent hash the v60 collapse used to assign origins,
    so a match is a true identity, not a heuristic.

    Every no-match path returns ``None`` explicitly (never an ``or``-fallback chain —
    a standing repo rule from a past production bug). Best-effort: a DB error logs
    and returns ``None`` so a caller degrades to today's "not on this device" error.
    """
    if not origin:
        return None
    if origin.startswith("local:") or origin.startswith("manual:"):
        return None
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT gr.remote_url AS remote_url "
                "FROM git_repositories gr "
                "JOIN projects p ON p.id = gr.project_id "
                "WHERE gr.remote_url IS NOT NULL AND gr.remote_url <> ''",
            )
            candidates = cursor.fetchall()
    except Exception as e:  # pragma: no cover - defensive; resolution is best-effort
        logger.warning(
            "checkout_manager: clone-url lookup failed for origin %r: %s", origin, e
        )
        return None

    for cand in candidates:
        remote_url = cand["remote_url"]
        if auto_link_group_id(remote_url) == origin:
            return remote_url
    return None


def _ensure_base_checkout(project_id: str, runner_id: str) -> Optional[str]:
    """Resolve the base (branch-NULL) checkout path for a project on this runner.

    Returns the path when a base checkout row exists AND its dir exists (refreshing
    ``status`` to ``'active'`` if it had drifted). Returns ``None`` — honestly "not
    materialized here" — when there is no row, or the row exists but its dir is gone.
    Phase 3 NEVER recreates/clones a missing base.
    """
    # branch=None → base checkout; legacy fallback resolves the runner_id=''
    # rows the v60 collapse seeded for pre-instance_id legacy projects.
    row = checkouts_db.get_checkout_with_legacy_fallback(project_id, runner_id)
    if not row:
        return None
    path = row.get("path")
    if not path or not os.path.isdir(path):
        # Row survives but the dir was removed on this device — do NOT recreate.
        return None
    if row.get("status") != "active":
        checkouts_db.upsert_checkout(project_id, runner_id, path, status="active")
    return path


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
    * the not-yet-cut-over ExecutionModeBar/WS ``worktree_id`` intake (legacy until
      the Phase 4 worktree-UI redesign), which materializes into the old md5-hash
      worktree dir and sets ``sessions.worktree_branch``.

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
            checkouts_db.upsert_checkout(
                project_id, runner_id, path, branch=branch, status="active"
            )
            return path
    return None


def _ensure_branch_checkout(
    project_v2_row: Dict[str, Any],
    project_id: str,
    runner_id: str,
    branch: str,
    *,
    base_branch: Optional[str] = None,
    create_new_branch: bool = False,
) -> Optional[str]:
    """Resolve (materializing if needed) the (project, runner, branch) checkout."""
    # A branch checkout requires a materialized base checkout to `git worktree add`
    # from. No base here → not materializable (return None; caller may 409/fallback).
    base_path = _ensure_base_checkout(project_id, runner_id)
    if not base_path:
        return None

    # Fast path: an existing branch checkout row whose dir is a live worktree.
    row = checkouts_db.get_checkout(project_id, runner_id, branch=branch)
    if row:
        path = row.get("path")
        if path and os.path.isdir(path):
            if _dir_is_worktree(path):
                if row.get("status") != "active":
                    checkouts_db.upsert_checkout(
                        project_id, runner_id, path, branch=branch, status="active"
                    )
                return path
            # Dir present but not a worktree — never destroy; surface not-materialized.
            logger.warning(
                "checkout_manager: %s exists but is not a worktree; refusing to touch it",
                path,
            )
            return None
        # Row exists but its dir is gone → fall through and re-materialize below
        # (at the same deterministic path, so the upsert reuses the same row).

    dest = _branch_checkout_path(project_v2_row, branch)
    dest_str = str(dest)

    # Adopt an already-materialized dir at the deterministic path (idempotent).
    if dest.exists():
        if _dir_is_worktree(dest_str):
            checkouts_db.upsert_checkout(
                project_id, runner_id, dest_str, branch=branch, status="active"
            )
            return dest_str
        logger.warning(
            "checkout_manager: %s exists but is not a worktree; refusing to touch it",
            dest_str,
        )
        return None

    # Lazy Step-E mirror: a branch already checked out in a post-collapse legacy
    # worktree dir (un-upgraded install or the legacy WS worktree_id intake) must
    # resolve to THAT dir — materializing at the deterministic path would fail on
    # "branch already checked out" and double-materialize.
    legacy_path = _adopt_legacy_worktree(project_v2_row, project_id, runner_id, branch)
    if legacy_path is not None:
        return legacy_path

    plan = _branch_plan(
        base_path, branch, base_branch=base_branch, create_new_branch=create_new_branch
    )
    if plan is None:
        # Branch not local, not on origin, and not an intentional new branch —
        # honestly not materializable here (never fabricate an empty branch).
        return None
    new_branch, plan_base = plan
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:  # pragma: no cover - defensive
        logger.warning("checkout_manager: cannot create %s: %s", dest.parent, e)
        return None

    success, err = git_service.add_worktree(
        main_dir=base_path,
        path=dest_str,
        branch=branch,
        new_branch=new_branch,
        base_branch=plan_base,
    )
    if not success:
        logger.warning(
            "checkout_manager: git worktree add failed for branch %r at %s: %s",
            branch,
            dest_str,
            err,
        )
        return None

    checkouts_db.upsert_checkout(
        project_id, runner_id, dest_str, branch=branch, status="active"
    )
    return dest_str


def ensure_checkout(
    project_v2_row: Optional[Dict[str, Any]],
    branch: Optional[str] = None,
    runner_id: Optional[str] = None,
    *,
    base_branch: Optional[str] = None,
    create_new_branch: bool = False,
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

    Returns the absolute checkout path, or ``None`` when it is not materializable
    on this device (no base checkout here — Phase 3 never clones — or a branch that
    exists nowhere and was not requested as new). Idempotent and safe to call on
    every send.
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
        return _ensure_base_checkout(project_id, runner_id)
    return _ensure_branch_checkout(
        project_v2_row,
        project_id,
        runner_id,
        branch,
        base_branch=base_branch,
        create_new_branch=create_new_branch,
    )
