"""The single origin / checkout / cwd authority (v2 core, Phase 3).

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

No callers are wired in this commit (P4 does that).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from app.core import checkout_manager
from app.core.config import settings
from app.db import checkouts as checkouts_db
from app.db import projects as projects_db
from app.db import projects_v2
from app.db.base import get_db

logger = logging.getLogger(__name__)


def origin_for_legacy_project(project_row: Dict[str, Any]) -> str:
    """The collapse key for a legacy ``projects`` row.

    Returns the existing ``link_group_id`` grouping token (``git:…`` / ``manual:…``)
    when present, else a deterministic ``'local:' + id`` synth for remote-less rows
    (project ids are PKs → collision-free). Identical to the origin the v60 collapse
    migration seeds ``projects_v2`` with, so a self-heal derives the SAME row.
    """
    return project_row.get("link_group_id") or ("local:" + str(project_row.get("id")))


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
    ``id = 'pv2-'+sha256(origin)[:24]``, checkout ``id = 'ck-'+legacy_id``,
    ``runner_id = instance_id or ''``, ``branch`` NULL, ``ON CONFLICT DO NOTHING``.

    Idempotent (a concurrent seed loses the race harmlessly). Returns the
    resulting projects_v2 row, or ``None`` if it somehow could not be resolved.
    """
    pv2_id = projects_v2.project_v2_id_for_origin(origin)
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO projects_v2 (id, origin, name, settings) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (origin) DO NOTHING",
                (pv2_id, origin, legacy.get("name") or origin, "{}"),
            )
            cursor.execute(
                "INSERT INTO checkouts "
                "(id, project_id, runner_id, path, branch, status) "
                "VALUES (%s, %s, %s, %s, NULL, 'active') "
                "ON CONFLICT DO NOTHING",
                (
                    "ck-" + str(legacy.get("id")),
                    pv2_id,
                    legacy.get("instance_id") or "",
                    legacy.get("path"),
                ),
            )
    except Exception as e:  # pragma: no cover - best-effort self-seed
        logger.warning(
            "project_resolver: failed to self-seed project_v2 for origin %s: %s",
            origin,
            e,
        )
    return projects_v2.get_project_v2_by_origin(origin)


def resolve_local_checkout(
    session_row: Dict[str, Any], runner_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """The ``checkouts`` row materializing this session's project on THIS runner,
    honoring ``session.branch`` (base checkout when no branch), or ``None``.

    ``None`` is the honest "not on this device" state — the project exists only on
    other runners (adopt surfaces the 409). Pure lookup: it does not materialize.
    """
    project = resolve_project_v2(session_row)
    if not project:
        return None
    if runner_id is None:
        runner_id = settings.effective_instance_id
    branch = session_row.get("branch")
    return checkouts_db.get_checkout_with_legacy_fallback(
        project["id"], runner_id, branch=branch
    )


def resolve_cwd(session_row: Dict[str, Any]) -> Optional[str]:
    """The ONE working-directory resolver.

    Session bound to a branch → the branch checkout path; else the project's base
    checkout path; both materialized lazily by ``checkout_manager.ensure_checkout``.
    Returns ``None`` when there is no project_v2 or no materializable checkout on
    this runner — the caller falls back to the default workspace (that fallback is
    deliberately NOT baked in here so the resolver stays a pure origin/checkout
    authority).
    """
    project = resolve_project_v2(session_row)
    if not project:
        return None
    branch = session_row.get("branch")
    if branch and str(branch).strip():
        return checkout_manager.ensure_checkout(project, branch=branch)
    return checkout_manager.ensure_checkout(project, None)


def resolve_existing_cwd(session_row: Dict[str, Any]) -> Optional[str]:
    """Pure-read cwd resolution — NEVER materializes.

    The read-path counterpart of :func:`resolve_cwd`. Looks up the session's
    checkout row (honoring ``session.branch``) via ``resolve_local_checkout`` and
    returns its ``path`` ONLY when the row exists AND that dir is present on this
    runner's disk. Returns ``None`` otherwise — an unmaterialized (or vanished)
    checkout surfaces honestly and the caller falls back to the default
    workspace. No ``git worktree add`` / no clone, so reads never mutate disk.
    """
    row = resolve_local_checkout(session_row)
    if not row:
        return None
    path = row.get("path")
    if path and os.path.isdir(path):
        return path
    return None
