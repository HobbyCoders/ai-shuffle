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
from app.core.worktree_manager import worktree_manager
from app.db import checkouts as checkouts_db

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


def _branch_plan(base_dir: str, branch: str) -> Tuple[bool, Optional[str]]:
    """Decide new-branch vs existing-branch for ``git worktree add``, matching
    ``worktree_manager.create_worktree`` semantics: if the branch already exists
    locally, check it out (``new_branch=False``); otherwise create it from the base
    checkout's current HEAD (``new_branch=True``, no explicit base)."""
    try:
        local = git_service.list_branches(base_dir, include_remote=False)
        local_names = [b["name"] for b in local if not b.get("is_remote")]
        if branch in local_names:
            return False, None
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("checkout_manager: list_branches failed for %s: %s", base_dir, e)
    return True, None


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


def _ensure_branch_checkout(
    project_v2_row: Dict[str, Any],
    project_id: str,
    runner_id: str,
    branch: str,
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

    new_branch, base_branch = _branch_plan(base_path, branch)
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
        base_branch=base_branch,
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
) -> Optional[str]:
    """Lazily materialize (or resolve) a checkout for a project_v2 on this runner.

    Args:
        project_v2_row: a ``projects_v2`` row (dict with at least ``id``/``name``).
        branch: the logical branch; ``None`` or ``''`` = the base checkout.
        runner_id: defaults to ``settings.effective_instance_id``.

    Returns the absolute checkout path, or ``None`` when it is not materializable
    on this device (no base checkout exists here — Phase 3 never clones). Idempotent
    and safe to call on every send.
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
    return _ensure_branch_checkout(project_v2_row, project_id, runner_id, branch)
