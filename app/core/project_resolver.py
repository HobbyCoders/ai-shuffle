"""The single origin / checkout / cwd authority.

Replaces the legacy ``project_links`` heuristic chains, the ``message_resolver``
path-existence cwd walk, and the ``project_utils`` worktree-cwd fallbacks with one
DB-keyed resolver. Everything hangs off two stable facts recorded by the v60
collapse migration:

- ``sessions.project_v2_id`` — the origin-keyed project the session belongs to
  (install-independent identity).
- the per-runner ``checkouts`` row — where that project is materialized on THIS
  runner (``runner_id`` = ``settings.effective_instance_id``).

Pure DB reads plus ``checkout_manager`` calls for lazy materialization. It imports
NO legacy resolution module (``project_links`` / ``message_resolver`` /
``project_utils``) and NO in-memory store — old and new paths meet only at the DB.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, Optional, Tuple

from app.core import checkout_manager
from app.core.config import settings
from app.db import checkouts as checkouts_db
from app.db import projects as projects_db
from app.db import projects_v2
from app.db.base import get_db

logger = logging.getLogger(__name__)

# Listings repeatedly resolve every session bound to an unusable checkout.
# Remember refusals for the process lifetime, not for a time window. Never
# evict entries, even on recovery: at capacity, new paths log at DEBUG rather
# than resetting the warning storm. Validation still detects restored checkouts.
_REFUSAL_MEMO_MAX = 2048
_refusals: Dict[Tuple[str, str], str] = {}
_refusals_lock = threading.Lock()


def _refusal_key(path: str, branch: str) -> tuple[str, str]:
    # A junction being removed/repointed must not mint a fresh warning key.
    return os.path.normcase(os.path.abspath(path)), branch


def _log_refusal(path: str, branch: str, reason: str) -> None:
    """Warn once per refused checkout per process, including after recovery."""
    key = _refusal_key(path, branch)
    with _refusals_lock:
        repeat = key in _refusals or len(_refusals) >= _REFUSAL_MEMO_MAX
        if not repeat:
            _refusals[key] = reason
    if repeat:
        logger.debug("project_resolver: still refusing checkout for branch %r: %s", branch, reason)
    else:
        logger.warning("project_resolver: refusing checkout for branch %r: %s", branch, reason)


def origin_for_legacy_project(project_row: Dict[str, Any]) -> str:
    """The collapse key for a legacy ``projects`` row.

    Returns the existing ``link_group_id`` grouping token (``git:…`` / ``manual:…``)
    when present, else a deterministic ``'local:' + id`` synth for remote-less rows
    (project ids are PKs → collision-free). Identical to the origin the v60 collapse
    migration seeds ``projects_v2`` with, so a self-heal derives the SAME row.
    """
    return project_row.get("link_group_id") or ("local:" + str(project_row.get("id")))


def resolve_catalog_project(project_v2_id: str) -> Optional[Dict[str, Any]]:
    """Follow a late-linked placeholder for project-level reads, never sessions.

    The local first-link record proves the exact legacy generation and target;
    names, paths and a reused legacy primary key cannot. Do not seed anything
    here: creation/linking commits the proof with the replacement atomically.
    """
    from app.db import project_catalog_aliases

    project = projects_v2.get_project_v2(project_v2_id)
    if project and (project.get("origin") or "").startswith("local:"):
        replacement = project_catalog_aliases.replacement_for(project_v2_id)
        if replacement:
            return projects_v2.get_project_v2(replacement) or project
    return project


def catalog_project_read_ids(project: Dict[str, Any]) -> list[str]:
    """Current identity plus proven, non-revoked first-link branch identities.

    Only project-scoped reads use this union: sessions stay bound to their
    recorded identity, and callers must not use an alias's retired base.
    Missing historical provenance fails closed, never inferring a generation
    from mutable legacy metadata or exposing branches after deletion/ID reuse.
    """
    from app.db import project_catalog_aliases

    return [project["id"], *project_catalog_aliases.originals_for(project["id"])]


def _persist_session_project_v2(session_id: str, project_v2_id: str) -> None:
    """One-shot self-heal write: bind a session to its project_v2, only if still
    NULL (never clobbers an existing binding, never touches legacy ``project_id``)."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE sessions SET project_v2_id = %s "
                "WHERE id = %s AND project_v2_id IS NULL",
                (project_v2_id, session_id),
            )
    except Exception as e:  # pragma: no cover - self-heal is best-effort
        logger.warning(
            "project_resolver: failed to persist project_v2_id for session %s: %s",
            session_id,
            e,
        )


def resolve_project_v2(session_row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Resolve the project_v2 a session belongs to, or ``None``.

    Authoritative path: ``session.project_v2_id`` → ``get_project_v2``. Self-heal
    fallback (sessions created before the seed, or by an old install): derive the
    origin from the legacy ``project_id`` → ``get_project_v2_by_origin``; when found,
    persist ``sessions.project_v2_id`` (only if it was NULL) so the next resolve is
    a direct hit.
    """
    if not session_row:
        return None

    project_v2_id = session_row.get("project_v2_id")
    if project_v2_id:
        row = projects_v2.get_project_v2(project_v2_id)
        if row is not None:
            return row
        # Bound id no longer resolves (stale) — fall through to self-heal.

    legacy_project_id = session_row.get("project_id")
    if not legacy_project_id:
        return None

    legacy = projects_db.get_project(legacy_project_id)
    if not legacy:
        return None

    origin = origin_for_legacy_project(legacy)
    pv2 = projects_v2.get_project_v2_by_origin(origin)
    if pv2 is None:
        # Lazy self-seed for a legacy project that post-dates the v60 collapse
        # (created on an install running old code AFTER this DB was migrated, so
        # the one-time seed never saw it). Create the SAME rows the migration
        # would have — same deterministic ids, same ON CONFLICT DO NOTHING
        # semantics — then re-resolve. Concurrent-boot / double-resolve safe:
        # the second run inserts nothing. Never touches a legacy column.
        pv2 = _seed_pv2_from_legacy(legacy, origin)
        if pv2 is None:
            return None

    session_id = session_row.get("id")
    if session_id and not session_row.get("project_v2_id"):
        _persist_session_project_v2(session_id, pv2["id"])
    return pv2


def _seed_pv2_from_legacy(
    legacy: Dict[str, Any], origin: str
) -> Optional[Dict[str, Any]]:
    """Create the ``projects_v2`` row and the legacy project's base ``checkouts``
    row for a post-migration legacy project, mirroring the v60 collapse EXACTLY:
    ``id = 'pv2-'+sha256(origin)[:24]``, checkout ``id = 'ck-'+legacy_id``
    (see :func:`checkouts_db.seed_legacy_base_checkout` for a relinked project),
    ``runner_id = instance_id or ''``, ``branch`` NULL, ``ON CONFLICT DO NOTHING``.

    Idempotent (a concurrent seed loses the race harmlessly). Returns the
    resulting projects_v2 row, or ``None`` if it somehow could not be resolved.
    """
    try:
        return projects_db.seed_project_catalog(legacy)
    except Exception as e:  # pragma: no cover - best-effort self-seed
        logger.warning(
            "project_resolver: failed to self-seed project_v2 for origin %s: %s",
            origin,
            e,
        )
    return projects_v2.get_project_v2_by_origin(origin)


def _effective_branch(session_row: Dict[str, Any]) -> Optional[str]:
    """The session's logical branch, lazily mirroring the v60 Step-F backfill.

    Returns ``sessions.branch`` when it is set. When ``branch`` is NULL/empty but
    the session carries a legacy ``worktree_branch`` — a worktree session minted
    by an un-upgraded install AFTER the one-time v60 collapse, which migration
    Step F never saw — backfill ``sessions.branch`` from it (one-shot,
    NULL-guarded, never overwriting a non-NULL branch, exactly like Step F) and
    use it for THIS resolution. Reads the SESSION's OWN ``worktree_branch``
    column — it does NOT require the ``worktrees`` row. Without this, such a
    session would resolve to the BASE checkout under the resolver (a cwd
    regression), because only Step F otherwise sets ``sessions.branch``.
    """
    branch = session_row.get("branch")
    if branch and str(branch).strip():
        return branch
    wt_branch = session_row.get("worktree_branch")
    if not wt_branch or not str(wt_branch).strip():
        return None
    session_id = session_row.get("id")
    if session_id:
        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE sessions SET branch = %s "
                    "WHERE id = %s AND (branch IS NULL OR branch = '')",
                    (wt_branch, session_id),
                )
        except Exception as e:  # pragma: no cover - best-effort self-heal
            logger.warning(
                "project_resolver: failed to backfill sessions.branch for %s: %s",
                session_id,
                e,
            )
    # Use it for THIS resolution even if the persist raced/failed.
    session_row["branch"] = wt_branch
    return wt_branch


def resolve_local_checkout(
    session_row: Dict[str, Any], runner_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """The ``checkouts`` row materializing this session's project on THIS runner,
    honoring ``session.branch`` (base checkout when no branch), or ``None``.

    Prefer a row whose directory exists when an older checkout has moved.
    ``None`` means no matching row on this runner. If all directories are gone,
    retain the oldest row for metadata callers; ``resolve_existing_cwd`` rejects
    its missing path. This lookup never materializes a checkout.
    """
    project = resolve_project_v2(session_row)
    if not project:
        return None
    if runner_id is None:
        runner_id = settings.effective_instance_id
    branch = _effective_branch(session_row)
    return _local_checkout(project["id"], runner_id, branch=branch)


def _local_checkout(
    project_id: str, runner_id: str, branch: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    # A moved checkout leaves the old row behind. Prefer an existing directory
    # among this runner's candidates, without falling back to another branch.
    # Legacy relative paths use the same runner-local normalization as writes;
    # never rewrite the stored row or test it against the process cwd.
    rows = checkouts_db.list_checkouts_with_legacy_fallback(
        project_id, runner_id, branch=branch
    )
    for row in rows:
        path = checkout_manager._resolve_checkout_path(row["path"]) if row.get("path") else None
        if path and os.path.isdir(path):
            return row
    # Metadata callers still need the recorded checkout when its folder is gone.
    return rows[0] if rows else None


def has_local_base_checkout(
    session_row: Dict[str, Any], runner_id: Optional[str] = None
) -> bool:
    """Whether this session's project has its BASE checkout materialized on THIS
    runner (branch-independent).

    The first adopt preflight gate: the project's base must be present on disk.
    Adopt then calls ``resolve_cwd`` to prepare the canonical branch checkout
    before transferring ownership. Requiring an already-existing BRANCH checkout
    here would reject sessions whose branches can be recovered from the remote.

    True iff ``resolve_project_v2`` resolves and any matching base checkout
    (branch NULL, legacy-fallback honored) has a directory on this runner's disk.
    Pure read — never materializes.
    """
    project = resolve_project_v2(session_row)
    if not project:
        return False
    if runner_id is None:
        runner_id = settings.effective_instance_id
    row = _local_checkout(project["id"], runner_id)
    if not row:
        return False
    path = checkout_manager._resolve_checkout_path(row["path"]) if row.get("path") else None
    return bool(path and os.path.isdir(path))


def resolve_cwd(session_row: Dict[str, Any], *, raise_add_errors: bool = False) -> Optional[str]:
    """The ONE working-directory resolver.

    Session bound to a branch → the branch checkout path; else the project's base
    checkout path; both materialized lazily by ``checkout_manager.ensure_checkout``.
    Returns ``None`` when there is no project_v2 or no materializable checkout on
    this runner — the caller falls back to the default workspace (that fallback is
    deliberately NOT baked in here so the resolver stays a pure origin/checkout
    authority). Session option building can opt into detailed checkout errors
    with ``raise_add_errors=True``; all other callers keep the None contract.
    """
    project = resolve_project_v2(session_row)
    if not project:
        return None
    branch = _effective_branch(session_row)
    if branch and str(branch).strip():
        kwargs = {"raise_add_errors": True} if raise_add_errors else {}
        return checkout_manager.ensure_checkout(project, branch=branch, **kwargs)
    return checkout_manager.ensure_checkout(project, None)


def existing_checkout_path(session_row: Dict[str, Any]) -> Optional[str]:
    """The registered checkout folder this session uses, if it exists, unvalidated.

    :func:`resolve_existing_cwd` returns this path only after Git confirms a
    branch checkout is still on its branch, which costs a subprocess. A caller
    matching many sessions against one folder compares this path first and
    validates only a match. Pure read; never materializes.
    """
    row = resolve_local_checkout(session_row)
    if not row:
        return None
    path = checkout_manager._resolve_checkout_path(row["path"]) if row.get("path") else None
    return path if path and os.path.isdir(path) else None


def resolve_session_cwd(session_row: Dict[str, Any], *, caller: Optional[str] = None) -> Optional[str]:
    """Read a session's runtime cwd, including both no-project chat lanes.

    Match query_options' lane priority before looking at checkout bindings.
    A supplied current caller decides owner versus restricted chat; otherwise
    use the latest stamped caller as on resume. This grants no tools and creates
    no folders/checkouts.
    Project sessions still use the branch-validating pure-read resolver.
    """
    from app.core.session_modes import (
        chat_uploads_dir,
        owner_chat_dir,
        runs_in_owner_chat_folder,
        runtime_session_mode,
    )

    if runtime_session_mode(session_row, caller=caller) == "chat":
        return str(chat_uploads_dir())
    if runs_in_owner_chat_folder(session_row, caller=caller):
        return str(owner_chat_dir())
    return resolve_existing_cwd(session_row)


def resolve_existing_cwd(session_row: Dict[str, Any]) -> Optional[str]:
    """Pure-read cwd resolution — NEVER materializes.

    The read-path counterpart of :func:`resolve_cwd`. Looks up the session's
    checkout row (honoring ``session.branch``) via ``resolve_local_checkout`` and
    returns its ``path`` ONLY when the row exists AND that dir is present on this
    runner's disk; branch-bound paths must also have their own matching Git HEAD.
    Returns ``None`` otherwise — an unavailable checkout surfaces honestly and
    the caller falls back to the default
    workspace. No ``git worktree add`` / no clone, so reads never mutate disk.
    """
    path = existing_checkout_path(session_row)
    if path:
        branch = _effective_branch(session_row)
        if branch:
            try:
                checkout_manager._require_branch_binding(path, branch)
            except checkout_manager.WorktreeError as e:
                _log_refusal(path, branch, str(e))
                return None
        return path
    return None


def follow_own_branch_switch(session_row: Dict[str, Any]) -> bool:
    """Rebind a running session to the branch its agent switched to (#2369).

    The resolvers refuse a branch checkout whose HEAD is not the session's
    bound branch, and turn end corrects the binding
    (``turn_finalize.resync_session_branch_from_head``). In between, an agent
    that ran ``git switch`` or ``git branch -m`` in its own worktree lost every
    tool that resolves the checkout mid-turn: the browser, the Task launcher.

    Call this from those agent-facing paths before (or after a refused)
    resolution. It applies the turn-end resync early, with the same checks
    (own linked worktree, never the main checkout, never a checkout another
    session is bound to, never a detached HEAD or a name another checkout
    claims), and only while the session has a live turn: a switch made
    between turns was not made by this session's agent, and the spawn path
    keeps refusing it. Updates ``session_row`` in place and returns True when
    the binding moved.
    """
    session_id = session_row.get("id") if session_row else None
    if not session_id or not _effective_branch(session_row):
        return False
    from app.db import turns as turns_db

    if turns_db.get_live_turn(session_id) is None:
        return False
    from app.core.turn_finalize import resync_session_branch_from_head

    moved = resync_session_branch_from_head(session_id)
    if not moved:
        return False
    old, new = moved
    session_row["branch"] = new
    if str(session_row.get("worktree_branch") or "").strip():
        session_row["worktree_branch"] = new
    logger.info(
        "project_resolver: session %s followed its own checkout from branch %r to %r",
        session_id, old, new,
    )
    return True


def reattach_own_detached_checkout(session_row: Dict[str, Any]) -> bool:
    """At this session's turn start, put its detached checkout back on its branch (#1437).

    An agent that ran ``git checkout origin/main`` in its own checkout for a
    baseline run, then parked, could never start another turn: every wake
    was refused before the model ran, and the checkout guard stops other
    sessions from switching the folder. Only the turn-start path and an idle
    Task worker's follow-up admission call this, never a mid-turn tool
    resolve, so a deliberate detach is never undone under a running agent.
    It also leaves the folder alone while this
    session still has background work running (it may be running in the
    detached tree) or another live session is bound to the same checkout.
    The git-level rules (clean, nothing unreachable, nothing forced) are
    :func:`checkout_manager.switch_detached_checkout_to_branch`.

    Never raises; returns True when it switched the working tree.
    """
    session_id = session_row.get("id") if session_row else None
    branch = _effective_branch(session_row) if session_row else None
    if not session_id or not branch:
        return False
    try:
        path = existing_checkout_path(session_row)
        if not path or checkout_manager.git_service.get_current_branch(path) is not None:
            return False  # on a branch: bound, or a real mismatch that stays refused
        project = resolve_project_v2(session_row)
        if not project:
            return False
        from app.core.session_reservations import holds_resources
        from app.core.task_registry import task_registry
        from app.db import database

        # Serving Monitors (a dev server, a watcher) never park a turn, so the
        # parking view skips them; they still run in this tree.
        if (task_registry.get_parking_background_work(session_id)
                or task_registry.has_running_monitor_shell(session_id)
                or task_registry.has_unresolved_background_launches(session_id)):
            logger.info(
                "project_resolver: not switching detached checkout %s of %s back to %r — "
                "the session still has background work running", path, session_id, branch,
            )
            return False
        # Unlike the end-of-turn resync, the session's own workers count too:
        # one sharing this folder may be working in the detached tree.
        if any(
            other.get("id") != session_id and holds_resources(other["id"])
            for other in database.list_sessions_for_v2_branch(project["id"], branch)
        ):
            logger.info(
                "project_resolver: not switching detached checkout %s of %s back to %r — "
                "another live session is bound to it", path, session_id, branch,
            )
            return False
        old_head = checkout_manager.switch_detached_checkout_to_branch(path, branch)
    except Exception:  # noqa: BLE001 — the binding check still refuses on its own
        logger.warning("project_resolver: detached-checkout reattach failed for %s",
                       session_id, exc_info=True)
        return False
    if not old_head:
        return False
    logger.info(
        "project_resolver: session %s switched its checkout %s from detached "
        "%s back to its branch %r", session_id, path, old_head[:12], branch,
    )
    return True


def describe_checkout_mismatch(session_row: Dict[str, Any]) -> Optional[str]:
    """Why a bound checkout that IS on disk was refused, else ``None``.

    The cwd resolvers return ``None`` both when the branch checkout is missing
    and when its folder exists on another branch. Refusals use this to name the
    second case instead of reporting a present folder as gone. Pure read: it
    never switches HEAD or rewrites the checkout row.
    """
    branch = _effective_branch(session_row)
    row = resolve_local_checkout(session_row) if branch else None
    if not row or not row.get("path"):
        return None
    path = checkout_manager._resolve_checkout_path(row["path"])
    if not os.path.isdir(path):
        return None
    try:
        checkout_manager._require_branch_binding(path, branch)
    except checkout_manager.WorktreeError:
        if not checkout_manager._dir_is_worktree(path):
            # A leftover folder with no .git of its own (e.g. a pruned
            # worktree's remains) would report the ENCLOSING repo's branch,
            # and "switch that folder back" would switch that other checkout.
            # Keep the caller's missing-checkout wording for it.
            return None
        actual = checkout_manager.git_service.get_current_branch(path)
        if actual and actual != branch:
            return (f"its folder {path} exists but is on branch '{actual}', "
                    f"not this session's branch '{branch}'")
        return (f"its folder {path} exists but is not a Git checkout of "
                f"branch '{branch}' (detached HEAD or not its own working tree)")
    return None
