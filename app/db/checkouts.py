"""The ``checkouts`` table — per-runner working-tree materializations of a
project_v2 (v2 core, Phase 3).

A *checkout* is the physical materialization of a project_v2 on ONE runner
(``runner_id`` = the install's ``effective_instance_id``). Each row records
where that project's ``branch`` is checked out on that runner (``path``), plus
optional working-tree state (``head_sha``, ``dirty``). ``branch`` NULL (or the
empty string) is the *base* checkout (D7); a non-empty ``branch`` is a logical
worktree materialized at its own ``path``. Uniqueness is
``(project_id, runner_id, path)`` — the same key the collapse migration and this
module upsert against.

This module is a PURE DB layer: plain functions over ``get_db()``, PostgreSQL
SQL that the test pg_shim translates to SQLite. It has no callers yet (Phase 3
P1 lands the schema + DB layer; later commits wire materialization/resolution).
"""

from __future__ import annotations

import hashlib
import logging
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
    """
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
