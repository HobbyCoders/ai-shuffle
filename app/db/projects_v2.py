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
#: Set when an owner pointed the project at a repository (``pin_remote``):
#: the stored remote is then authoritative and ``record_remote`` leaves it be.
PINNED_KEY = "remote_pinned"


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
        if current.get(PINNED_KEY) is True:
            # The owner pointed this project at a repository: a worker that has
            # not applied it yet still reports the old remote, and must never
            # put it back.
            return False
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


def _patch_settings(origin: str, patch: Dict[str, Any], drop: tuple = ()) -> bool:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT settings FROM projects_v2 WHERE origin = %s", (origin,))
        found = cursor.fetchone()
        if found is None:
            return False
        current = _parse_json(found["settings"])
        current = dict(current) if isinstance(current, dict) else {}
        current.update(patch)
        for key in drop:
            current.pop(key, None)
        cursor.execute(
            "UPDATE projects_v2 SET settings = %s, updated_at = NOW() WHERE origin = %s",
            (json.dumps(current), origin),
        )
        return bool(cursor.rowcount)


def pin_remote(origin: str, remote_url: str, *, default_branch: Optional[str], generation: int,
               keep_upstream: bool, previous: Optional[str]) -> bool:
    """Point ``origin`` at ``remote_url`` for every worker (credential-free URL).

    The origin itself never changes. ``generation`` tells each worker whether
    it already applied this change (``app.core.project_remote``).
    """
    patch: Dict[str, Any] = {
        REMOTE_URL_KEY: remote_url, PINNED_KEY: True,
        "remote_generation": generation, "remote_keep_upstream": bool(keep_upstream),
    }
    if default_branch:
        patch[DEFAULT_BRANCH_KEY] = default_branch
    if previous:
        patch["previous_remote_url"] = previous
    return _patch_settings(origin, patch, drop=() if default_branch else (DEFAULT_BRANCH_KEY,))


def mark_merged(origin: str, into_origin: str) -> bool:
    """Record that ``origin`` (a duplicate) now belongs to ``into_origin``."""
    if not origin or not into_origin or origin == into_origin:
        return False
    return _patch_settings(origin, {"merged_into": into_origin})


def set_forgotten(origin: str, forgotten: bool) -> bool:
    """Hide (or show again) a project no worker holds on the Projects page."""
    if forgotten:
        return _patch_settings(origin, {"forgotten": True})
    row = get_project_v2_by_origin(origin)
    if not row or (row.get("settings") or {}).get("forgotten") is not True:
        return False
    return _patch_settings(origin, {}, drop=("forgotten",))


#: Set while a project exists only as a new project's first clone (``POST
#: /projects/clone``) that has not succeeded yet: ``{runner_id, op_id}`` of
#: the latest attempt. Only that attempt hides it again when it fails
#: (:func:`settle_provisional`); a registered copy clears it
#: (:func:`mark_registered`).
PROVISIONAL_KEY = "provisional_clone"


def provisional_token(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The first clone's provisional mark on a projects_v2 row, else None."""
    settings = (row or {}).get("settings")
    token = settings.get(PROVISIONAL_KEY) if isinstance(settings, dict) else None
    return token if isinstance(token, dict) and token.get("runner_id") and token.get("op_id") else None


def _same_token(a: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]]) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return a.get("runner_id") == b.get("runner_id") and a.get("op_id") == b.get("op_id")


def _swap_settings(origin: str, change, *, name: Optional[str] = None) -> bool:
    """Compare-and-set on one row: ``change(settings)`` returns the new settings
    or None to leave the row as it is, decided under the row lock."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT settings FROM projects_v2 WHERE origin = %s FOR UPDATE", (origin,))
        found = cursor.fetchone()
        if found is None:
            return False
        current = _parse_json(found["settings"])
        updated = change(dict(current) if isinstance(current, dict) else {})
        if updated is None:
            return False
        if name:
            cursor.execute("UPDATE projects_v2 SET settings = %s, name = %s, updated_at = NOW() WHERE origin = %s",
                           (json.dumps(updated), name, origin))
        else:
            cursor.execute("UPDATE projects_v2 SET settings = %s, updated_at = NOW() WHERE origin = %s",
                           (json.dumps(updated), origin))
        return bool(cursor.rowcount)


def claim_provisional(origin: str, token: Dict[str, Any], *, expect: Optional[Dict[str, Any]],
                      name: Optional[str] = None) -> bool:
    """Mark ``origin`` as ``token``'s first clone (and show it again), only
    while the current mark is still ``expect`` (None: no mark). With ``name``
    the project is renamed in the same write."""
    def change(settings: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not _same_token(provisional_token({"settings": settings}), expect):
            return None
        settings[PROVISIONAL_KEY] = dict(token)
        settings.pop("forgotten", None)
        return settings

    return _swap_settings(origin, change, name=name)


def settle_provisional(origin: str, runner_id: str, op_id: str) -> bool:
    """Hide ``origin`` after its first clone failed, only while that clone
    (``runner_id``/``op_id``) is still its mark: not if a later attempt
    replaced it or a copy was registered since. The mark stays, so a retry
    replaces it."""
    def change(settings: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        owner = provisional_token({"settings": settings})
        if not _same_token(owner, {"runner_id": runner_id, "op_id": op_id}) or settings.get("forgotten") is True:
            return None
        settings["forgotten"] = True
        return settings

    return _swap_settings(origin, change)


def mark_registered(origin: str) -> bool:
    """A worker registered a copy of ``origin``: it is a listed project again
    (neither hidden nor a first clone in progress). No write when already so."""
    row = get_project_v2_by_origin(origin)
    settings = (row or {}).get("settings") or {}
    if not row or (settings.get("forgotten") is not True and PROVISIONAL_KEY not in settings):
        return False
    return _patch_settings(origin, {}, drop=("forgotten", PROVISIONAL_KEY))


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
    'upsert_project record_remote pin_remote mark_merged set_forgotten claim_provisional settle_provisional '
    'mark_registered',
    scope='shared',
)
