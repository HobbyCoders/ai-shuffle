"""The ``checkouts`` table — per-runner working-tree materializations of a
project_v2 (v2 core).

A *checkout* is the physical materialization of a project_v2 on ONE runner
(``runner_id`` = the install's ``effective_instance_id``). Each row records
where that project's ``branch`` is checked out on that runner (``path``), plus
optional working-tree state (``head_sha``, ``dirty``). ``branch`` NULL (or the
empty string) is the *base* checkout; a non-empty ``branch`` is a logical
worktree materialized at its own ``path``. Uniqueness is
``(project_id, runner_id, path)`` — the same key the collapse migration and this
module upsert against.

This module is a synchronous database layer used by project resolution and
checkout management. Legacy project IDs remain supported at the API boundary;
these rows identify logical projects and their physical runner checkouts.
"""

from __future__ import annotations

import hashlib
import logging
import ntpath
import os
import posixpath
import re
from typing import Any, Dict, List, Optional

from app.db.base import get_db

logger = logging.getLogger(__name__)


def checkout_id_for(project_id: str, runner_id: str, path: str) -> str:
    """Deterministic ``checkouts.id`` for a (project, runner, path) triple —
    stable across re-runs so a re-upsert never mints a duplicate id."""
    key = f"{project_id}\x00{runner_id}\x00{path}"
    return "ck-" + hashlib.sha256(key.encode()).hexdigest()[:24]


def _row(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Normalize a fetched checkout row (no JSONB columns; ``dirty`` is a
    boolean that the SQLite shim returns as 0/1)."""
    if row is None:
        return None
    d = dict(row)
    if "dirty" in d and d["dirty"] is not None:
        d["dirty"] = bool(d["dirty"])
    return d


def _rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [d for r in rows if (d := _row(r)) is not None]


def upsert_checkout(
    project_id: str,
    runner_id: str,
    path: str,
    branch: Optional[str] = None,
    head_sha: Optional[str] = None,
    dirty: Optional[bool] = None,
    status: str = "active",
) -> Dict[str, Any]:
    """Insert (or update) the checkout for ``(project_id, runner_id, path)``.

    On an existing row the working-tree fields are refreshed, but a ``None``
    argument leaves the stored value intact (``COALESCE``) so a partial upsert
    never clobbers a previously-recorded ``branch``/``head_sha``/``dirty``.
    ``status`` (which always has a value) and ``updated_at`` are always set.
    Returns the resulting row.

    ``path`` MUST be absolute: the V2 resolver treats ``checkouts.path`` as
    absolute and cannot resolve a relative one. Reject a relative path here so
    no new relative row can ever be written.
    """
    if not os.path.isabs(path):
        raise ValueError(
            f"upsert_checkout: refusing workspace-relative checkout path {path!r}; "
            "checkouts.path must be absolute (see #654)"
        )
    checkout_id = checkout_id_for(project_id, runner_id, path)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO checkouts "
            "(id, project_id, runner_id, path, branch, head_sha, dirty, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (project_id, runner_id, path) DO UPDATE SET "
            "branch = COALESCE(EXCLUDED.branch, checkouts.branch), "
            "head_sha = COALESCE(EXCLUDED.head_sha, checkouts.head_sha), "
            "dirty = COALESCE(EXCLUDED.dirty, checkouts.dirty), "
            "status = EXCLUDED.status, "
            "updated_at = NOW() "
            "RETURNING *",
            (checkout_id, project_id, runner_id, path, branch, head_sha, dirty, status),
        )
        row = _row(cursor.fetchone())
    if row is None:  # INSERT/UPSERT ... RETURNING always yields the row
        raise RuntimeError(
            f"upsert_checkout: no row returned for ({project_id!r}, {runner_id!r}, {path!r})"
        )
    return row


def get_checkout(
    project_id: str, runner_id: str, branch: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Return the checkout for a project on a runner, or ``None``.

    ``branch=None`` matches the *base* checkout (``branch`` NULL or ''); a
    non-empty ``branch`` matches that logical worktree exactly.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        if branch:
            cursor.execute(
                "SELECT * FROM checkouts "
                "WHERE project_id = %s AND runner_id = %s AND branch = %s "
                "ORDER BY created_at ASC, id ASC LIMIT 1",
                (project_id, runner_id, branch),
            )
        else:
            cursor.execute(
                "SELECT * FROM checkouts "
                "WHERE project_id = %s AND runner_id = %s "
                "AND (branch IS NULL OR branch = '') "
                "ORDER BY created_at ASC, id ASC LIMIT 1",
                (project_id, runner_id),
            )
        row = cursor.fetchone()
    return _row(row)


def get_checkout_by_path(path: str, *, project_id: Optional[str] = None,
                         runner_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Resolve a physical path only on its runner; reject ambiguous projects."""
    if not path:
        return None
    if runner_id is None:
        from app.core.config import settings
        runner_id = settings.effective_instance_id
    target = os.path.normcase(os.path.normpath(path))
    with get_db() as conn:
        cursor = conn.cursor()
        if project_id:
            cursor.execute("SELECT * FROM checkouts WHERE runner_id = %s AND project_id = %s",
                           (runner_id, project_id))
        else:
            cursor.execute("SELECT * FROM checkouts WHERE runner_id = %s", (runner_id,))
        rows = cursor.fetchall()
    matches = [row for row in _rows(rows)
               if os.path.normcase(os.path.normpath(row.get("path") or "")) == target]
    return matches[0] if len(matches) == 1 else None


def set_head_sha_for_path(path: str, head_sha: str, *, project_id: Optional[str] = None,
                          runner_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Record ``head_sha`` on the checkout at ``path`` without changing status."""
    if not path or not head_sha:
        return None
    row = get_checkout_by_path(path, project_id=project_id, runner_id=runner_id)
    if not row or not row.get("id"):
        return None
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE checkouts SET head_sha = %s, updated_at = NOW() "
            "WHERE id = %s RETURNING *",
            (head_sha, row["id"]),
        )
        updated = cursor.fetchone()
    return _row(updated)


def get_checkout_with_legacy_fallback(
    project_id: str, runner_id: str, branch: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Return the checkout for a project on a runner, falling back to the
    legacy runner_id='' row when no exact-runner row exists.

    The v60 collapse seeds ``runner_id = legacy instance_id or ''``: legacy
    ``projects`` rows whose ``instance_id`` was NULL land under the ``''``
    sentinel (unreachable via ``effective_instance_id``, which is always
    non-empty). A real-runner row therefore WINS; the ``''`` row is only the
    FALLBACK so those pre-instance_id checkouts remain resolvable. The same
    fallback applies to base (``branch=None``) and branch lookups.
    """
    row = get_checkout(project_id, runner_id, branch=branch)
    if row is not None:
        return row
    if runner_id == "":
        return None
    return get_checkout(project_id, "", branch=branch)


def _base_checkout_statuses(cursor: Any, project_id: str, runner_id: str) -> List[str]:
    """Statuses of EVERY base row (``branch`` NULL or '') for a (project, runner).

    A missing/NULL ``status`` normalizes to ``'active'`` — same coercion the
    eligibility predicate has always applied to a single row.
    """
    cursor.execute(
        "SELECT status FROM checkouts "
        "WHERE project_id = %s AND runner_id = %s "
        "AND (branch IS NULL OR branch = '')",
        (project_id, runner_id),
    )
    return [(dict(r).get("status") or "active") for r in cursor.fetchall()]


def has_active_base_checkout(project_id: str, runner_id: str) -> bool:
    """True iff ``runner_id`` has ANY ACTIVE **BASE** checkout of this project.

    THE eligibility predicate. Callers: cluster git-state
    (``GET /api/v1/cluster/git-state``, which feeds composer badges), the
    with-worktree remote preflight
    (``app.api.sessions_worktree._preflight_remote_target``), WS create-pick
    (``ChatWebSocketHandler._validate_target_install``), and the
    ``POST /sessions/{id}/move`` checkout gate. They must never diverge: the
    composer badge that says a device is eligible and the preflight that
    decides whether the send 409s are the same question, so they ask it with
    the same function.

    "Base" is ``branch`` NULL or ''. **ANY** matters: uniqueness is
    ``(project_id, runner_id, path)``, so one runner can hold SEVERAL base rows
    once a project's path moves — an archived row at the old path plus a live
    one at the new path. :func:`get_checkout` returns only the OLDEST base row,
    so asking it would read the stale archived row and call an eligible runner
    ineligible. This scans every base row instead and answers on the best one.

    The legacy fallback of :func:`get_checkout_with_legacy_fallback` is
    preserved exactly: the ``runner_id=''`` sentinel rows are consulted ONLY
    when the runner owns no base row at all (a real-runner row always wins,
    even an archived one), and never when ``runner_id`` is itself ''.

    Deliberately NOT ``capable_runners``: that matches ANY active checkout row
    including a branch-only worktree, and a new branch needs a base checkout to
    fork from.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        statuses = _base_checkout_statuses(cursor, project_id, runner_id)
        if not statuses and runner_id != "":
            statuses = _base_checkout_statuses(cursor, project_id, "")
        return any(s == "active" for s in statuses)


def _checkouts_for_branch(
    cursor: Any, project_id: str, runner_id: str, branch: Optional[str]
) -> List[Dict[str, Any]]:
    """Every checkout of ``(project, runner, branch)``, any status.

    Uniqueness is ``(project_id, runner_id, path)``, so a moved path leaves
    several rows. Callers that care about existence must scan this full set —
    :func:`get_checkout` is LIMIT 1 oldest and would hide a newer active row
    behind an archived one. ``branch`` None/'' is the base (NULL or ''); a
    non-empty branch matches that logical worktree exactly.
    """
    if branch:
        cursor.execute(
            "SELECT * FROM checkouts "
            "WHERE project_id = %s AND runner_id = %s AND branch = %s "
            "ORDER BY created_at ASC, id ASC",
            (project_id, runner_id, branch),
        )
    else:
        cursor.execute(
            "SELECT * FROM checkouts "
            "WHERE project_id = %s AND runner_id = %s "
            "AND (branch IS NULL OR branch = '') "
            "ORDER BY created_at ASC, id ASC",
            (project_id, runner_id),
        )
    return _rows(cursor.fetchall())


def list_checkouts_with_legacy_fallback(
    project_id: str, runner_id: str, branch: Optional[str] = None
) -> List[Dict[str, Any]]:
    """All candidates, oldest first, preserving exact-runner precedence.

    A missing directory must not hide another checkout on the same runner.
    Legacy unowned rows are considered only when the runner has no rows for
    this branch, matching ``get_checkout_with_legacy_fallback``.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        rows = _checkouts_for_branch(cursor, project_id, runner_id, branch)
        if not rows and runner_id != "":
            rows = _checkouts_for_branch(cursor, project_id, "", branch)
    return rows


def list_active_checkouts(
    project_id: str, runner_id: str, branch: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Every ACTIVE checkout of ``(project, runner, branch)``.

    A missing/NULL ``status`` normalizes to ``'active'`` — same coercion as
    :func:`has_active_base_checkout`. No legacy ``runner_id=''`` fallback: the
    owner of a session is a real runner.
    """
    with get_db() as conn:
        rows = _checkouts_for_branch(conn.cursor(), project_id, runner_id, branch)
    return [r for r in rows if (r.get("status") or "active") == "active"]


def has_active_checkout(
    project_id: str, runner_id: str, branch: Optional[str] = None
) -> bool:
    """True iff ``runner_id`` has ANY ACTIVE checkout of this project at this branch.

    Used by ``enrich_session_worktree_meta`` for remote ``worktree_deleted``
    honesty. Scans every matching row — :func:`get_checkout` is LIMIT 1 oldest
    and would treat an archived-at-old-path row as the whole story.

    ``branch`` None/'' is the base (NULL or ''); a non-empty branch is exact.
    Deliberately NOT the eligibility predicate — that stays
    :func:`has_active_base_checkout` (base only, plus the legacy ``runner_id=''``
    fallback).
    """
    return bool(list_active_checkouts(project_id, runner_id, branch))


def eligible_base_project_ids(runner_id: str) -> List[str]:
    """Batch the base-checkout predicate, including its legacy fallback."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT DISTINCT c.project_id FROM checkouts c "
            "WHERE (c.branch IS NULL OR c.branch = '') "
            "AND (c.status IS NULL OR c.status = '' OR c.status = 'active') "
            "AND (c.runner_id = %s OR (c.runner_id = '' AND NOT EXISTS ("
            "SELECT 1 FROM checkouts own WHERE own.project_id = c.project_id "
            "AND own.runner_id = %s AND (own.branch IS NULL OR own.branch = '')))) "
            "ORDER BY c.project_id",
            (runner_id, runner_id),
        )
        return [row["project_id"] for row in cursor.fetchall()]


def list_active_base_checkouts() -> List[Dict[str, Any]]:
    """One SELECT of every ACTIVE base checkout across every project.

    Catalog (``GET /cluster/projects``) joins this in Python to
    ``list_projects_v2`` + ``get_installs``. Do not call
    :func:`list_checkouts` per ``projects_v2`` — that is N queries.

    "Base" is ``branch`` NULL or ''. A missing/NULL ``status`` counts as
    ``'active'`` — same coercion as :func:`has_active_base_checkout`.
    Branch-only and archived rows are omitted.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM checkouts "
            "WHERE (branch IS NULL OR branch = '') "
            "AND (status IS NULL OR status = '' OR status = 'active') "
            "ORDER BY project_id ASC, runner_id ASC, created_at ASC, id ASC"
        )
        rows = cursor.fetchall()
    return _rows(rows)


def list_checkouts(project_id: str) -> List[Dict[str, Any]]:
    """Return every checkout for a project across all runners."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM checkouts WHERE project_id = %s "
            "ORDER BY runner_id ASC, created_at ASC, id ASC",
            (project_id,),
        )
        rows = cursor.fetchall()
    return _rows(rows)


def list_all_active_checkout_paths() -> List[str]:
    """Return the ``path`` of every ACTIVE checkout across all projects/runners.

    A flat list of the raw path strings (no row normalization) — used by the
    legacy worktree sync to recognize (and skip) V2 placement checkout dirs so
    they never get imported as legacy ``worktrees`` rows.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT path FROM checkouts WHERE status = 'active'")
        rows = cursor.fetchall()
    return [dict(r)["path"] for r in rows]


def set_checkout_status(checkout_id: str, status: str) -> Optional[Dict[str, Any]]:
    """Set a checkout's ``status`` (e.g. ``'active'`` → ``'archived'``).

    Returns the updated row, or ``None`` if no checkout has that id.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE checkouts SET status = %s, updated_at = NOW() "
            "WHERE id = %s "
            "RETURNING *",
            (status, checkout_id),
        )
        row = cursor.fetchone()
    return _row(row)


def archive_checkout_by_path(project_id: str, path: str, *,
                             runner_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Mark the checkout whose path matches ``path`` as archived.

    Comparison is case- and slash-insensitive so Windows and POSIX spellings
    of the same directory hit the same row. Returns the updated row, the
    already-archived row, or ``None`` when nothing matches.
    """
    if not project_id or not path:
        return None
    row = get_checkout_by_path(path, project_id=project_id, runner_id=runner_id)
    if not row or row.get("status") == "archived":
        return row
    return set_checkout_status(row["id"], "archived")


# ---------------------------------------------------------------------------
# Legacy workspace-relative rows (#654 follow-up)
# ---------------------------------------------------------------------------
#
# The v60 collapse (and the resolver's lazy legacy self-seed) copy a legacy
# ``projects.path`` into ``checkouts`` verbatim, and an old one can be
# workspace-relative (``ai-shuffle``). V2 records the same folder again under its
# absolute path, so one folder ends up with two rows. Readers that pick "the"
# checkout then saw two candidates and refused ("missing or ambiguous").


def is_absolute_checkout_path(path: Optional[str]) -> bool:
    """Absolute on POSIX OR Windows, so a path written on one OS is never
    mistaken for a relative one when read on another."""
    return bool(path) and (posixpath.isabs(path) or ntpath.isabs(path))


def _is_base(row: Dict[str, Any]) -> bool:
    return not (row.get("branch") or "")


def _path_parts(path: str) -> List[str]:
    return [p for p in re.split(r"[\\/]+", path) if p and p != "."]


def _names_same_folder(absolute: str, relative: str) -> bool:
    """Whether ``absolute`` ends with the components of ``relative``
    (``/workspace/ai-shuffle`` and ``ai-shuffle``). Used where the workspace
    folder is not known yet (the boot migration runs before it is loaded)."""
    rel = _path_parts(relative)
    tail = _path_parts(absolute)[-len(rel):] if rel else []
    if not rel or len(tail) != len(rel):
        return False
    if ntpath.splitdrive(absolute)[0]:  # Windows paths compare case-insensitively
        return [p.casefold() for p in tail] == [p.casefold() for p in rel]
    return tail == rel


def legacy_seed_is_redundant(cursor: Any, project_id: str, runner_id: str,
                             path: Optional[str]) -> bool:
    """True when seeding a legacy relative ``path`` as a base checkout would only
    duplicate a base row this project already has for the same folder.

    The legacy seeders re-run (the collapse on every migration pass, the
    resolver on demand), so without this check a duplicate removed by
    :func:`repair_relative_checkout_paths` would come straight back.
    """
    if not path or is_absolute_checkout_path(path):
        return False
    cursor.execute(
        "SELECT path FROM checkouts WHERE project_id = %s AND runner_id = %s "
        "AND (branch IS NULL OR branch = '')",
        (project_id, runner_id),
    )
    return any(
        is_absolute_checkout_path(r["path"]) and _names_same_folder(r["path"], path)
        for r in cursor.fetchall()
    )


def _folder_key(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def repair_relative_checkout_paths(runner_id: str, workspace_dir: Any) -> Dict[str, int]:
    """Repair this runner's legacy workspace-relative checkout rows.

    Runs at boot once the workspace folder is known. For each relative row:

    * another row of the same project already records that folder in the same
      role (base, or the same branch): the relative row is a duplicate and is
      deleted;
    * no row records that folder and it exists on disk: the path is rewritten
      to its absolute form, in place (same id);
    * otherwise it is kept. A relative BASE row whose only twin is tagged with a
      branch is still the project's base checkout; dropping it would leave the
      project without one and make the app clone a fresh copy.

    Only rows of ``runner_id`` are touched. Returns counts per outcome.
    """
    result = {"deleted": 0, "rewritten": 0, "kept": 0}
    workspace = str(workspace_dir)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM checkouts WHERE runner_id = %s", (runner_id,))
        rows = _rows(cursor.fetchall())
        absolute = [r for r in rows if is_absolute_checkout_path(r.get("path"))]
        for row in rows:
            path = row.get("path")
            if not path or is_absolute_checkout_path(path):
                continue
            target = os.path.normpath(os.path.join(workspace, path))
            key = _folder_key(target)
            twins = [a for a in absolute
                     if a["project_id"] == row["project_id"] and _folder_key(a["path"]) == key]
            same_role = [a for a in twins
                         if _is_base(a) == _is_base(row)
                         and (_is_base(row) or a.get("branch") == row.get("branch"))]
            if same_role:
                cursor.execute("DELETE FROM checkouts WHERE id = %s AND path = %s",
                               (row["id"], path))
                result["deleted"] += 1
                logger.info("checkouts: removed duplicate relative row %s (%r = %s)",
                            row["id"], path, same_role[0]["path"])
            elif not twins and os.path.isdir(target):
                cursor.execute(
                    "UPDATE checkouts SET path = %s, updated_at = NOW() "
                    "WHERE id = %s AND path = %s",
                    (target, row["id"], path),
                )
                result["rewritten"] += 1
                absolute.append({**row, "path": target})
                logger.info("checkouts: made relative row %s absolute (%r -> %s)",
                            row["id"], path, target)
            else:
                result["kept"] += 1
    return result
