"""Per-runner lazy materialization of project_v2 checkouts (v2 core, Phase 3).

A *checkout* is the physical working tree of a ``projects_v2`` project on ONE
runner (``runner_id`` = this install's ``effective_instance_id``). The origin-keyed
``projects_v2`` row is install-independent identity; the ``checkouts`` row is its
per-runner materialization — ``branch`` NULL/'' is the *base* checkout, a non-empty
``branch`` is a logical worktree materialized at its own deterministic path.

This module owns the ONE lazy-materialization entrypoint, ``ensure_checkout``,
which the send/resume cwd seam (P4) will call on every send. It is idempotent and
safe to call repeatedly. Design commitments (Phase 3):

- **Deterministic per-runner path** — ``{base_checkout_dir}/.fluid/worktrees/
  {sanitized_branch}`` — nested inside the project's own clone on this runner,
  replacing both the legacy ``md5(project_path)[:4]`` scheme and the earlier
  workspace-root ``checkouts/{project_slug}/{branch}`` layout. A materialization,
  never identity, so the same branch resolves per-machine without cross-host
  orphaning.
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
import ntpath
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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

# The reserved directory name for a project's base (branch-NULL) checkout under
# its per-project slug dir (#607, ruling 4). A logical branch that sanitizes to
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
    But the v60 collapse migration (#654) seeded some rows verbatim from legacy
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
    """
    sanitized = worktree_manager._sanitize_branch_name(branch)
    if sanitized == _BASE_DIR_NAME:
        # Collision guard (#607, ruling 4): the base checkout reserves
        # ``__base__``. A logical branch whose name sanitizes to that exact literal
        # is renamed so it can never resolve onto — and overwrite — the base dir.
        sanitized = f"{sanitized}-branch"
    return Path(base_dir) / _WORKTREES_DIRNAME / "worktrees" / sanitized


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


def _project_self_seed_enabled(project_v2_row: Dict[str, Any]) -> bool:
    """Whether this project permits lazy self-seed (#607, ruling 2 per-project opt-out).

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
    """Lazily self-seed a MISSING base checkout by cloning the project's remote (#607).

    Called by ``_ensure_base_checkout`` only when no base checkout is materialized on
    this runner (the pinned-runner-never-cloned case). Gated behind
    ``AI_SHUFFLE_SELF_SEED`` (``settings.self_seed_checkouts``, default ON) AND a
    per-project opt-out (``projects_v2.settings.self_seed`` — an explicit ``false``
    disables it for THIS project only, #607 ruling 2). When either gate is off,
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
    clone_url = _resolve_clone_url(origin) if origin else None
    if clone_url is None:
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
    # adopt it when it is a valid checkout, else refuse to touch it (standing #581
    # / never-destructive rule) rather than clobbering it with a fresh clone.
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

    try:
        temp_dir = tempfile.mkdtemp(prefix=f"{_BASE_DIR_NAME}.seed-", dir=str(slug_dir))
    except OSError as e:  # pragma: no cover - defensive
        logger.warning(
            "checkout_manager: cannot create temp seed dir under %s: %s", slug_dir, e
        )
        return None

    success, err = git_service.clone_repo(temp_dir, clone_url)
    if not success:
        logger.warning(
            "checkout_manager: self-seed clone failed for project %s from %s: %s",
            project_id,
            clone_url,
            err,
        )
        _rmtree_quiet(temp_dir)
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
    """Best-effort recursive delete of a seed temp dir; never raises."""
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception as e:  # pragma: no cover - ignore_errors already swallows
        logger.warning("checkout_manager: temp-dir cleanup failed for %s: %s", path, e)


def _ensure_base_checkout(
    project_id: str,
    runner_id: str,
    project_v2_row: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Resolve the base (branch-NULL) checkout path for a project on this runner.

    Returns the path when a base checkout row exists AND its dir exists (refreshing
    ``status`` to ``'active'`` if it had drifted). When there is NO base checkout row
    on this runner, attempts a lazy self-seed (``_seed_base_checkout``, behind
    ``AI_SHUFFLE_SELF_SEED``); if the seed cannot run (flag off, or no cloneable URL
    resolved) it falls through to ``None`` — honestly "not materialized here". When a
    row exists but its dir is gone we NEVER recreate it in place — honest ``None``.
    """
    # branch=None → base checkout; legacy fallback resolves the runner_id=''
    # rows the v60 collapse seeded for pre-instance_id legacy projects.
    row = checkouts_db.get_checkout_with_legacy_fallback(project_id, runner_id)
    if not row:
        return _seed_base_checkout(project_v2_row, project_id, runner_id)
    path = row.get("path")
    if not path:
        return None
    # Normalize a workspace-relative legacy path (#654) before the on-disk check
    # and the return; the raw DB value is left untouched (read-only normalization).
    resolved = _resolve_checkout_path(path)
    if not os.path.isdir(resolved):
        # Row survives but the dir was removed on this device — do NOT recreate.
        return None
    # Status-refresh reuses the row's ORIGINAL path (its uniqueness key) so it
    # updates in place — but only when that path is absolute, so a legacy relative
    # row (repaired out-of-band) never trips upsert_checkout's relative-path guard.
    if row.get("status") != "active" and os.path.isabs(path):
        checkouts_db.upsert_checkout(project_id, runner_id, path, status="active")
    return resolved


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
    # Pass the row so a missing base can lazily self-seed (#607) before the worktree.
    base_path = _ensure_base_checkout(project_id, runner_id, project_v2_row)
    if not base_path:
        return None


    # Fast path: an existing branch checkout row whose dir is a live worktree.
    row = checkouts_db.get_checkout(project_id, runner_id, branch=branch)
    if row:
        path = row.get("path")
        # Normalize a workspace-relative legacy path (#654) for the on-disk checks
        # and return; the raw DB value is left untouched (read-only normalization).
        resolved = _resolve_checkout_path(path) if path else path
        if resolved and os.path.isdir(resolved):
            if _dir_is_worktree(resolved):
                # Status-refresh reuses the ORIGINAL path (its uniqueness key) so it
                # updates in place — only when absolute, so a legacy relative row
                # never trips upsert_checkout's relative-path guard.
                if row.get("status") != "active" and os.path.isabs(path):
                    checkouts_db.upsert_checkout(
                        project_id, runner_id, path, branch=branch, status="active"
                    )
                return resolved
            # Dir present but not a worktree — never destroy; surface not-materialized.
            logger.warning(
                "checkout_manager: %s exists but is not a worktree; refusing to touch it",
                resolved,
            )
            return None
        # Row exists but its dir is gone → fall through and re-materialize below
        # (at the same deterministic path, so the upsert reuses the same row).

    dest = _branch_checkout_path(base_path, branch)
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

    # Everything a run needs is gitignored, so the new worktree has none of it.
    # Link it from the checkout it branched off before anyone tries to build or
    # run in it. Best-effort by contract: a failure here degrades the worktree,
    # it does not invalidate it.
    try:
        provision_worktree_runtime(base_path, dest_str)
    except Exception as e:  # noqa: BLE001 - defence in depth
        logger.warning(
            "checkout_manager: runtime provisioning raised for %s: %s", dest_str, e
        )

    checkouts_db.upsert_checkout(
        project_id, runner_id, dest_str, branch=branch, status="active"
    )
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
# on the same lockfile, for the same project. Linking it is instant.

_SVELTE_CONFIG_NAMES = (
    "svelte.config.js",
    "svelte.config.ts",
    "svelte.config.mjs",
    "svelte.config.cjs",
)

# `svelte-kit sync` generates .svelte-kit/ (tsconfig, $types, route manifests).
# Without it svelte-check and vite fail on a fresh checkout.
_SVELTE_SYNC_TIMEOUT_SECONDS = 300


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


def _run_svelte_kit_sync(package_dir: Path) -> bool:
    """Best-effort ``npx svelte-kit sync``. Returns whether it succeeded."""
    try:
        result = subprocess.run(
            ["npx", "svelte-kit", "sync"],
            cwd=str(package_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_SVELTE_SYNC_TIMEOUT_SECONDS,
            shell=(os.name == "nt"),
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


def provision_node_modules(source_root: str, dest_root: str) -> Dict[str, List[str]]:
    """Give a fresh worktree the source checkout's installed node packages.

    For each package directory the two checkouts share, links the source's
    ``node_modules`` into the worktree and then runs ``svelte-kit sync`` where a
    svelte config says it is a SvelteKit app.

    Entirely best-effort and never raises: a worktree without node_modules is
    degraded, not broken, and it must not cost anyone a checkout. Returns a
    summary of what happened, which is what the tests assert on.
    """
    summary: Dict[str, List[str]] = {
        "linked": [],
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
            else:
                _link_directory(source_modules, dest_modules)
                summary["linked"].append(label)
                logger.info(
                    "checkout_manager: linked node_modules for %s into %s",
                    label,
                    package,
                )
        except Exception as e:  # noqa: BLE001
            summary["errors"].append(f"{label}: link failed: {e}")
            logger.warning(
                "checkout_manager: could not link node_modules for %s in %s: %s",
                label,
                package,
                e,
            )
            # No modules means sync cannot work either.
            continue

        try:
            has_svelte_config = any(
                (package / name).is_file() for name in _SVELTE_CONFIG_NAMES
            )
        except OSError:  # pragma: no cover - defensive
            has_svelte_config = False

        if not has_svelte_config:
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
            checkouts_db.upsert_checkout(
                project_id, runner_id, path, branch=wt_branch, status="active"
            )
        except Exception as e:  # noqa: BLE001
            logger.debug(
                "checkout_manager: could not adopt worktree %s: %s", path, e
            )
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
        base = _ensure_base_checkout(project_id, runner_id, project_v2_row)
        if base:
            register_unregistered_worktrees(project_id, runner_id, base)
        return base
    path = _ensure_branch_checkout(
        project_v2_row,
        project_id,
        runner_id,
        branch,
        base_branch=base_branch,
        create_new_branch=create_new_branch,
    )
    if path:
        register_unregistered_worktrees(project_id, runner_id, path)
    return path
