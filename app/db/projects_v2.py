"""The ``projects_v2`` table — the origin-keyed project registry (v2 core,
Phase 3).

A *project_v2* is a logical project identified by its ``origin`` — the stable
grouping token that is identical on every install (a ``git:…`` remote hash, a
``manual:…`` admin-link token, or a synth ``local:<legacy id>`` for a
remote-less project). ``origin`` is the UNIQUE key; the ``id`` is derived
deterministically from it (``'pv2-' + sha256(origin)[:24]``) so the same origin
resolves to the same row on every install and across re-runs — the same
convention the v60 collapse migration uses when it seeds this table from the
legacy ``projects`` rows.

This module is a synchronous database layer used by project resolution and
checkout management. Legacy project IDs remain supported at the API boundary;
these rows identify logical projects and their physical runner checkouts.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, List, Optional

from app.db.base import get_db, parse_jsonb as _parse_json
from app.db.compat import phs

logger = logging.getLogger(__name__)


def project_v2_id_for_origin(origin: str) -> str:
    """Deterministic ``projects_v2.id`` for an ``origin`` — identical on every
    install and stable across re-runs (matches the v60 collapse migration)."""
    return "pv2-" + hashlib.sha256(origin.encode()).hexdigest()[:24]


def _row(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Normalize a fetched projects_v2 row: decode the ``settings`` JSONB."""
    if row is None:
        return None
    d = dict(row)
    d["settings"] = _parse_json(d.get("settings")) or {}
    return d


def _rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [d for r in rows if (d := _row(r)) is not None]


def upsert_project(
    origin: str,
    name: Optional[str] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Insert (or update) the project_v2 for ``origin``.

    On an existing origin the ``name`` and ``updated_at`` are refreshed
    (``settings`` are set on insert only — a later settings API owns updates).
    The ``id`` is deterministic in ``origin`` so two installs upserting the same
    new origin converge to one row. Returns the resulting row.
    """
    project_id = project_v2_id_for_origin(origin)
    resolved_name = name or origin
    settings_json = json.dumps(settings or {})
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO projects_v2 (id, origin, name, settings) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (origin) DO UPDATE SET name = EXCLUDED.name, "
            "updated_at = NOW() "
            "RETURNING *",
            (project_id, origin, resolved_name, settings_json),
        )
        row = _row(cursor.fetchone())
    if row is None:  # INSERT/UPSERT ... RETURNING always yields the row
        raise RuntimeError(f"upsert_project: no row returned for origin {origin!r}")
    return row


#: ``projects_v2.settings`` keys holding the project's Git remote (never with
#: credentials) and default branch. ``settings`` replicates with the row, so a
#: project whose only copy is on an offline worker can still be cloned
#: elsewhere (cross-worker setup, base-checkout self-seed).
REMOTE_URL_KEY = "remote_url"
DEFAULT_BRANCH_KEY = "default_branch"


def stored_remote(row: Optional[Dict[str, Any]]) -> tuple:
    """``(remote_url, default_branch)`` recorded on a projects_v2 row (each may be None)."""
    project_settings = (row or {}).get("settings")
    if not isinstance(project_settings, dict):
        return None, None
    url = project_settings.get(REMOTE_URL_KEY)
    branch = project_settings.get(DEFAULT_BRANCH_KEY)
    return (url if isinstance(url, str) and url else None,
            branch if isinstance(branch, str) and branch else None)


def record_remote(origin: str, remote_url: str, default_branch: Optional[str] = None) -> bool:
    """Record ``origin``'s Git remote and default branch on its (existing) row.

    ``remote_url`` must already be credential-free (``strip_url_userinfo``).
    Writes only what is missing or wrong, so workers holding different
    spellings of one remote (HTTPS and SSH) never take turns overwriting it:
    the URL is replaced only when absent or, for a ``git:`` origin, when the
    stored one no longer names this origin; the branch only when absent or
    when the URL is being replaced. Returns whether the row changed.
    """
    from app.core.project_links import auto_link_group_id

    if not origin or not remote_url:
        return False
    if origin.startswith("git:") and auto_link_group_id(remote_url) != origin:
        return False  # never store another repository's remote on this identity
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT settings FROM projects_v2 WHERE origin = %s", (origin,))
        found = cursor.fetchone()
        if found is None:
            return False
        current = _parse_json(found["settings"])
        current = dict(current) if isinstance(current, dict) else {}
        stored_url, stored_branch = stored_remote({"settings": current})
        patch: Dict[str, Any] = {}
        if stored_url is None or (origin.startswith("git:") and auto_link_group_id(stored_url) != origin):
            patch[REMOTE_URL_KEY] = remote_url
            if default_branch and default_branch != stored_branch:
                patch[DEFAULT_BRANCH_KEY] = default_branch
        elif stored_branch is None and default_branch:
            patch[DEFAULT_BRANCH_KEY] = default_branch
        if not patch:
            return False
        current.update(patch)
        cursor.execute(
            "UPDATE projects_v2 SET settings = %s, updated_at = NOW() WHERE origin = %s",
            (json.dumps(current), origin),
        )
        return bool(cursor.rowcount)


def get_project_v2(project_id: str) -> Optional[Dict[str, Any]]:
    """Return a project_v2 by its ``id``, or ``None``."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM projects_v2 WHERE id = %s", (project_id,))
        row = cursor.fetchone()
    return _row(row)


def get_project_v2_by_origin(origin: str) -> Optional[Dict[str, Any]]:
    """Return the project_v2 for an ``origin`` (the UNIQUE key), or ``None``."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM projects_v2 WHERE origin = %s", (origin,))
        row = cursor.fetchone()
    return _row(row)


def get_projects_v2_by_ids(ids) -> Dict[str, Dict[str, Any]]:
    """``{id: row}`` for the requested project_v2 ids, in ONE query.

    The batched sibling of :func:`get_project_v2`, for callers that resolve a
    whole page at once. Duplicates and falsy ids are dropped; missing ids are
    simply absent from the result. An empty (or entirely falsy) input returns
    ``{}`` WITHOUT touching the database — ``IN ()`` is a syntax error in
    Postgres.
    """
    unique = [i for i in dict.fromkeys(ids) if i]
    if not unique:
        return {}
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT * FROM projects_v2 WHERE id IN ({phs(len(unique))})",
            unique,
        )
        rows = cursor.fetchall()
    return {row["id"]: row for row in _rows(rows) if row.get("id")}


def get_projects_v2_by_origins(origins) -> Dict[str, Dict[str, Any]]:
    """``{origin: row}`` for the requested origins, in ONE query.

    The batched sibling of :func:`get_project_v2_by_origin` (``origin`` is the
    UNIQUE key, so the mapping is 1:1). Same empty-input and missing-key
    semantics as :func:`get_projects_v2_by_ids`.
    """
    unique = [o for o in dict.fromkeys(origins) if o]
    if not unique:
        return {}
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT * FROM projects_v2 WHERE origin IN ({phs(len(unique))})",
            unique,
        )
        rows = cursor.fetchall()
    return {row["origin"]: row for row in _rows(rows) if row.get("origin")}


def list_projects_v2() -> List[Dict[str, Any]]:
    """Return every project_v2, newest first."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM projects_v2 ORDER BY created_at DESC, id ASC")
        rows = cursor.fetchall()
    return _rows(rows)


from app.core.resource_sync import install_database_mutations as _install_resource_sync

_install_resource_sync(
    globals(), 'projects',
    'upsert_project record_remote',
    scope='shared',
)
