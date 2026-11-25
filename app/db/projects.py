"""Project database operations."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, TypedDict

from app.db.base import get_db, row_to_dict, rows_to_list, logger
from app.db.compat import phs
from app.core.config import settings


# ============================================================================
# TypedDict definitions
# ============================================================================

class ProjectRow(TypedDict):
    """Full project row from SELECT * FROM projects."""
    id: str
    name: str
    description: Optional[str]
    path: str
    settings: Dict[str, Any]
    instance_id: Optional[str]
    link_group_id: Optional[str]
    created_at: str
    updated_at: str


def _parse_project_row(row: Optional[Dict[str, Any]]) -> Optional[ProjectRow]:
    """Normalize a raw projects row: decode the settings JSON column."""
    if row:
        row["settings"] = json.loads(row["settings"]) if isinstance(row["settings"], str) else (row["settings"] if row["settings"] is not None else {})
    return row


def get_project(project_id: str) -> Optional[ProjectRow]:
    """Get a project by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM projects WHERE id = %s", (project_id,))
        return _parse_project_row(row_to_dict(cursor.fetchone()))


def get_projects_by_ids(project_ids) -> Dict[str, Dict[str, Any]]:
    """``{project_id: row}`` for every requested project, in ONE query.

    The batched sibling of :func:`get_project`, for callers that resolve a whole
    page of sessions at once (the session-list enrichment pipeline runs on every
    sidebar poll, so a ``get_project`` per distinct project is an N+1 on the
    hottest endpoint in the app).

    Deliberately NOT instance-filtered — mirrors :func:`get_project`, not
    :func:`get_all_projects` — so a session owned by another install still
    resolves its project name. Reads only the columns the listing needs
    (``id``, ``name``, ``path``, ``link_group_id``); use :func:`get_project`
    when the full row (``settings``, ``description``, …) is wanted.

    Missing ids are omitted. An empty/blank-only input returns ``{}`` without
    touching the database (``IN ()`` is a syntax error in Postgres). If the
    batched read fails, degrades to one :func:`get_project` per DISTINCT id
    rather than to no data at all.
    """
    ids = [pid for pid in dict.fromkeys(project_ids) if pid]
    if not ids:
        return {}

    placeholders = phs(len(ids))
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, name, path, link_group_id FROM projects "
                f"WHERE id IN ({placeholders})",
                ids,
            )
            rows = rows_to_list(cursor.fetchall())
        return {row["id"]: dict(row) for row in rows if row.get("id")}
    except Exception:  # noqa: BLE001 — callers must never break on a batch read
        logger.debug("Batched project lookup failed; falling back", exc_info=True)

    # Fallback: one lookup per DISTINCT id (never per caller row).
    resolved: Dict[str, Dict[str, Any]] = {}
    for pid in ids:
        try:
            proj = get_project(pid)
        except Exception:  # noqa: BLE001
            proj = None
        if proj:
            resolved[pid] = proj
    return resolved


def get_all_projects() -> List[ProjectRow]:
    """Get all projects, filtered to the current instance if instance_id is set."""
    instance_id = settings.effective_instance_id
    with get_db() as conn:
        cursor = conn.cursor()
        # Show projects belonging to this instance OR projects with no instance_id (legacy)
        cursor.execute(
            "SELECT * FROM projects WHERE instance_id = %s OR instance_id IS NULL ORDER BY name ASC",
            (instance_id,)
        )
        return [_parse_project_row(row) for row in rows_to_list(cursor.fetchall())]


def _catalog_origin(project: Dict[str, Any]) -> str:
    """The origin a legacy row is catalogued under (``project_resolver``'s key)."""
    return project.get("link_group_id") or ("local:" + str(project.get("id")))


def _catalog_runners(project: Dict[str, Any]) -> List[str]:
    """The runner ids this worker's base checkouts of ``project`` are keyed by."""
    return sorted({settings.effective_instance_id, project.get("instance_id") or ""})


def _catalog_project_v2_id(cursor, origin: str) -> Optional[str]:
    cursor.execute("SELECT id FROM projects_v2 WHERE origin = %s", (origin,))
    row = cursor.fetchone()
    return row["id"] if row else None


def _retire_from_catalog(cursor, project: Dict[str, Any]) -> None:
    """Take a deleted project's base checkout out of the project catalog.

    ``GET /cluster/projects`` lists every ``projects_v2`` with an active base
    checkout, so without this a deleted project stayed in every selector as one
    available on this worker (#434). The identity and the checkout row are kept,
    only marked removed, so a chat still bound to the project keeps opening in
    its folder. Another local clone of the same origin keeps it listed.
    """
    from app.db.checkouts import retire_base_checkouts

    origin = _catalog_origin(project)
    cursor.execute(
        "SELECT 1 FROM projects WHERE id <> %s AND link_group_id = %s "
        "AND (instance_id = %s OR instance_id IS NULL) LIMIT 1",
        (project["id"], origin, settings.effective_instance_id),
    )
    if cursor.fetchone() is not None:
        return
    project_v2_id = _catalog_project_v2_id(cursor, origin)
    if project_v2_id:
        retire_base_checkouts(cursor, project_v2_id, _catalog_runners(project))


def _restore_to_catalog(cursor, project: Dict[str, Any]) -> None:
    """Re-adding a deleted project brings its retired base checkout back."""
    from app.db.checkouts import restore_removed_base_checkouts

    project_v2_id = _catalog_project_v2_id(cursor, _catalog_origin(project))
    if project_v2_id:
        restore_removed_base_checkouts(cursor, project_v2_id, _catalog_runners(project), project["path"])


def _seed_catalog(cursor, project: Dict[str, Any]) -> str:
    """Seed the collapse identity and base checkout in the caller's transaction.

    Reuse the migration's deterministic keys and conflict handling, including
    the alternate checkout id for a relinked folder. Never refresh an existing
    identity or reactivate a removed checkout merely by resolving it.
    """
    from app.db import checkouts, project_catalog_aliases, projects_v2

    origin = _catalog_origin(project)
    project_v2_id = projects_v2.project_v2_id_for_origin(origin)
    cursor.execute(
        "INSERT INTO projects_v2 (id, origin, name, settings) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT (origin) DO NOTHING RETURNING id",
        (project_v2_id, origin, project.get("name") or origin, "{}"),
    )
    inserted = cursor.fetchone()
    project_v2_id = _catalog_project_v2_id(cursor, origin)
    if origin == "local:" + str(project["id"]):
        if inserted:
            project_catalog_aliases.register_placeholder(cursor, project_v2_id, project["id"])
    else:
        project_catalog_aliases.record_first_link(cursor, project["id"], project_v2_id)
    checkouts.seed_legacy_base_checkout(
        cursor, str(project["id"]), project_v2_id,
        project.get("instance_id") or "", project.get("path"),
    )
    return project_v2_id


def seed_project_catalog(project: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Post-commit hooked fallback for projects written by pre-upgrade workers."""
    from app.db import projects_v2

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM projects WHERE id = %s FOR UPDATE", (project["id"],))
        live = row_to_dict(cursor.fetchone())
        if not live:
            return None
        _seed_catalog(cursor, live)
    return projects_v2.get_project_v2_by_origin(_catalog_origin(live))


def create_project(
    project_id: str,
    name: str,
    description: Optional[str],
    path: str,
    settings_dict: Optional[Dict[str, Any]] = None,
    link_group_id: Optional[str] = None,
) -> ProjectRow:
    """Create a new project.

    ``link_group_id``: the logical identity the project already has (its Git
    remote's ``git:`` id, or the origin a setup registers it under). Seeding
    that identity from the start leaves no ``local:<id>`` placeholder behind,
    which a later link would only supersede (a second, stale project).
    """
    from app.db import project_catalog_aliases

    now = datetime.now(timezone.utc).isoformat()
    instance_id = settings.effective_instance_id
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO projects (id, name, description, path, settings, instance_id, link_group_id,
                                     created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (project_id, name, description, path, json.dumps(settings_dict or {}), instance_id,
             link_group_id or None, now, now)
        )
        # If this ID existed in an earlier generation (including pre-upgrade
        # deletion), its retained checkouts can never prove this new project's link.
        project_catalog_aliases.revoke(cursor, project_id)
        project = {"id": project_id, "name": name, "path": path, "instance_id": instance_id,
                   "link_group_id": link_group_id or None}
        _seed_catalog(cursor, project)
        _restore_to_catalog(cursor, project)
    return get_project(project_id)


def update_project(
    project_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    path: Optional[str] = None,
    settings_dict: Optional[Dict[str, Any]] = None
) -> Optional[ProjectRow]:
    """Update a project"""
    updates = []
    values = []
    if name is not None:
        updates.append("name = %s")
        values.append(name)
    if description is not None:
        updates.append("description = %s")
        values.append(description)
    if path is not None:
        updates.append("path = %s")
        values.append(path)
    if settings_dict is not None:
        updates.append("settings = %s")
        values.append(json.dumps(settings_dict))

    if updates:
        updates.append("updated_at = %s")
        values.append(datetime.now(timezone.utc).isoformat())
        values.append(project_id)

        with get_db() as conn:
            cursor = conn.cursor()
            # Serialize with other moves, relinking and deletion. An earlier
            # snapshot can describe neither the current path nor its identity.
            cursor.execute("SELECT * FROM projects WHERE id = %s FOR UPDATE", (project_id,))
            existing = row_to_dict(cursor.fetchone())
            if not existing:
                return None
            cursor.execute(
                f"UPDATE projects SET {', '.join(updates)} WHERE id = %s",
                values
            )
            if path is not None and path != existing.get("path"):
                # Move only this legacy row's live base registration. Retired
                # identities keep their old folder for retained sessions; branch
                # checkouts and other runners are independent materializations.
                current_id = _catalog_project_v2_id(cursor, _catalog_origin(existing))
                runners = _catalog_runners(existing)
                placeholders = ", ".join(["%s"] * len(runners))
                cursor.execute(
                    "UPDATE checkouts SET path = %s, updated_at = NOW() "
                    f"WHERE project_id = %s AND runner_id IN ({placeholders}) "
                    "AND path = %s AND (branch IS NULL OR branch = '') "
                    "AND (status IS NULL OR status = '' OR status = 'active')",
                    (path, current_id, *runners, existing.get("path")),
                )

    return get_project(project_id)


class FolderConflict(Exception):
    """An unsafe or stale edit to a worker's base registration."""


def _retarget_base(cursor, legacy_id, identity, runner, old_path, new_path, stored_path, *, original=False):
    from app.db import checkouts

    # The move is scoped to one legacy registration. A same-origin clone is
    # not proof that its runner-local base can be edited or retired.
    registrations = checkouts.live_base_registrations(cursor, identity, runner)
    if [row['id'] for row in registrations] != [legacy_id]:
        if original:
            return False
        raise FolderConflict('This worker does not have a single matching base folder to change.')
    cursor.execute(
        'SELECT id, path, status FROM checkouts WHERE project_id = %s AND runner_id = %s '
        "AND (branch IS NULL OR branch = '') ORDER BY id FOR UPDATE", (identity, runner),
    )
    bases = cursor.fetchall()
    old_key = checkouts.base_folder_key(old_path)
    matching = [r for r in bases if (original or checkouts.is_active(r))
                and r['path'] and checkouts.base_folder_key(r['path']) == old_key]
    if not matching:
        # A relink can make the first-link alias current again without moving
        # its historical base along with the current registration.
        if original:
            return False
        raise FolderConflict('This worker does not have a single matching base folder to change.')
    # Old setup sometimes activated retained folders. Only the verified live
    # path is writable; take stale active bases out of selection.
    for row in bases:
        if not original and row not in matching and checkouts.is_active(row):
            cursor.execute('UPDATE checkouts SET status = %s, updated_at = NOW() WHERE id = %s',
                           (checkouts.REMOVED_STATUS, row['id']))
    # Setup can leave a legacy relative row beside its canonical absolute twin.
    base = next((r for r in matching if checkouts.is_absolute_checkout_path(r['path'])), matching[0])
    for duplicate in matching:
        if duplicate['id'] != base['id']:
            cursor.execute('DELETE FROM checkouts WHERE id = %s', (duplicate['id'],))
    cursor.execute(
        'SELECT 1 FROM checkouts WHERE project_id = %s AND runner_id = %s AND path IN (%s, %s) '
        'AND id <> %s', (identity, runner, new_path, stored_path, base['id']),
    )
    if cursor.fetchone():
        raise FolderConflict('That folder is already registered as a checkout.')
    cursor.execute(
        'UPDATE checkouts SET path = %s, status = %s, head_sha = NULL, dirty = NULL, updated_at = NOW() '
        'WHERE id = %s', (new_path, checkouts.REMOVED_STATUS if original else base['status'], base['id']),
    )
    return True


def retarget_folder(
    snapshot: dict, *, old_path: str, new_path: str, stored_path: str, same_folder: bool,
    name: Optional[str] = None, description: Optional[str] = None,
    settings_dict: Optional[Dict[str, Any]] = None,
) -> ProjectRow:
    """Move the worker's live base and any proven first-link base in one transaction."""
    project_id = snapshot['id']
    runner = settings.effective_instance_id
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM projects WHERE id = %s FOR UPDATE', (project_id,))
        current = _parse_project_row(row_to_dict(cursor.fetchone()))
        if not current or current != snapshot or current.get('instance_id') != runner:
            raise FolderConflict('The project folder changed. Close this editor and reopen it.')
        if not same_folder:
            from app.core.worktree_manager import preserve_worktree_owners

            from app.db import project_catalog_aliases

            pv2_id = _catalog_project_v2_id(cursor, _catalog_origin(current))
            original_id = project_catalog_aliases.original_for_legacy(cursor, project_id, pv2_id)
            _retarget_base(cursor, project_id, pv2_id, runner, old_path, new_path, stored_path)
            if original_id and not _retarget_base(
                cursor, project_id, original_id, runner, old_path, new_path, stored_path, original=True
            ):
                original_id = None
            # Turn admission locks the same rows. Include shared-only chats on
            # either identity, not explicit bindings to past, unrelated links.
            scope = (
                "(instance_id = %s OR instance_id IS NULL) AND "
                "((project_v2_id IS NULL AND project_id = %s) OR project_v2_id IN (%s, %s))"
            )
            params = (runner, project_id, pv2_id, original_id)
            cursor.execute(f'SELECT id FROM sessions WHERE {scope} ORDER BY id FOR UPDATE', params)
            if cursor.fetchall():
                cursor.execute(
                    'SELECT 1 FROM turns WHERE session_id IN '
                    f'(SELECT id FROM sessions WHERE {scope}) '
                    "AND state IN ('queued','running','interrupting','draining') LIMIT 1",
                    params,
                )
                if cursor.fetchone():
                    raise FolderConflict('Wait for queued and running chats on this worker to finish first.')
            preserve_worktree_owners(cursor, current, old_path)
        if same_folder and name is None and description is None and settings_dict is None:
            return current
        cursor.execute(
            'UPDATE projects SET path = %s, name = COALESCE(%s, name), '
            'description = COALESCE(%s, description), settings = COALESCE(%s::jsonb, settings), '
            'updated_at = %s WHERE id = %s RETURNING *',
            (current['path'] if same_folder else stored_path, name, description,
             json.dumps(settings_dict) if settings_dict is not None else None,
             datetime.now(timezone.utc).isoformat(), project_id),
        )
        return _parse_project_row(row_to_dict(cursor.fetchone()))


def set_project_link_group(project_id: str, link_group_id: Optional[str]) -> Optional[ProjectRow]:
    """Set (or clear, with ``None``) a project's link group id.

    Unlike ``update_project`` this writes the value directly so it can be
    cleared back to NULL. Touches ``updated_at`` so peers notice the change.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM projects WHERE id = %s FOR UPDATE", (project_id,))
        previous = row_to_dict(cursor.fetchone())
        cursor.execute(
            "UPDATE projects SET link_group_id = %s, updated_at = %s WHERE id = %s",
            (link_group_id, datetime.now(timezone.utc).isoformat(), project_id),
        )
        if previous and cursor.rowcount:
            project = {**previous, "link_group_id": link_group_id}
            if _catalog_origin(previous) != _catalog_origin(project):
                # Keep old identities/checkouts for bound chats, but do not
                # advertise an abandoned origin as another runnable project.
                _retire_from_catalog(cursor, previous)
            _seed_catalog(cursor, project)
            # Explicit re-add/relink is allowed to restore a removed base.
            _restore_to_catalog(cursor, project)
    return get_project(project_id)


def get_projects_in_link_group(link_group_id: str) -> List[ProjectRow]:
    """All project rows sharing a link group — one row per install for the same repo.

    Deliberately NOT instance-filtered: callers use this to find which installs
    have a clone of a logical project (see app/core/project_links.py).
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM projects WHERE link_group_id = %s ORDER BY id ASC",
            (link_group_id,),
        )
        return [_parse_project_row(row) for row in rows_to_list(cursor.fetchall())]


def get_project_session_count(project_id: str) -> int:
    """Get the number of sessions associated with a project"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as cnt FROM sessions WHERE project_id = %s", (project_id,))
        return cursor.fetchone()["cnt"]


def get_all_project_session_counts() -> Dict[str, int]:
    """Get session counts for all projects in a single query"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT project_id, COUNT(*) as count
            FROM sessions
            WHERE project_id IS NOT NULL
            GROUP BY project_id
        """)
        return {row["project_id"]: row["count"] for row in cursor.fetchall()}


# Which chats removing a project deletes: only chats whose binding names a
# project row being removed, owned by this worker. Never another worker's,
# never another project's.
#
#   * deleted: ``sessions.project_id`` names a removed row (whatever its
#     ``project_v2_id``: a relinked project's older chats keep their first
#     identity), and ``instance_id`` is this worker or NULL (a pre-instance
#     row, shared like the project rows ``get_all_projects`` lists), and no
#     other device executes it (``owner_device_id`` unset or this device, so
#     never a mirror).
#   * kept: every chat bound only by a shared project identity
#     (``project_id`` NULL, ``project_v2_id`` set; e.g. a chat a peer placed
#     here). An identity does not prove which project row a chat came from:
#     a relinked clone's older chats keep the old identity, which a later
#     clone of that origin (or a reused ``local:<id>``) takes over. So removal
#     never deletes by identity; those chats stay, uncounted.

def _removal_binding(cursor, project_ids) -> Optional[Dict[str, Any]]:
    from app.db.sessions import _deleting_worker

    removing = sorted(set(project_ids))
    cursor.execute(f"SELECT id FROM projects WHERE id IN ({phs(len(removing))}) ORDER BY id", removing)
    rows = cursor.fetchall()
    if not rows:
        return None
    return {
        "project_ids": [row["id"] for row in rows],
        "runner": settings.effective_instance_id,
        "device_id": _deleting_worker()[0],
    }


def chat_in_removal(row: Dict[str, Any], binding: Dict[str, Any]) -> bool:
    """Whether removing ``binding``'s projects deletes this chat (see above)."""
    owner = row.get("owner_device_id")
    if owner and owner != binding["device_id"]:
        return False
    return (row.get("project_id") in binding["project_ids"]
            and row.get("instance_id") in (None, binding["runner"]))


def _removal_chats(cursor, binding: Dict[str, Any]) -> List[Dict[str, Any]]:
    ids = binding["project_ids"]
    cursor.execute(
        "SELECT * FROM sessions WHERE (owner_device_id IS NULL OR owner_device_id = %s) "
        f"AND project_id IN ({phs(len(ids))}) AND (instance_id IS NULL OR instance_id = %s) ORDER BY id",
        (binding["device_id"], *ids, binding["runner"]),
    )
    return [row for row in rows_to_list(cursor.fetchall()) if chat_in_removal(row, binding)]


def _removal_worktrees(cursor, project_ids) -> List[Dict[str, Any]]:
    """Active legacy worktree rows (folder on disk) that cascade away with these projects.

    Removal never runs ``git worktree remove``: the rows go with the project
    (``git_repositories`` -> ``worktrees`` ON DELETE CASCADE) and the folders
    stay on disk, so the confirmation lists them.
    """
    cursor.execute(
        "SELECT w.id, w.worktree_path FROM worktrees w JOIN git_repositories r ON r.id = w.repository_id "
        f"WHERE r.project_id IN ({phs(len(project_ids))}) AND w.status = 'active' ORDER BY w.worktree_path, w.id",
        list(project_ids),
    )
    return rows_to_list(cursor.fetchall())


def project_removal_plan(project_ids) -> Optional[Dict[str, Any]]:
    """``{"binding", "chats", "worktrees"}`` for removing these project rows together, or None.

    ``chats`` are full session rows, for the ordinary chat delete; pass
    ``binding`` to :func:`chat_in_removal` to re-check each under its lock.
    ``worktrees`` are the folders that stay on disk (:func:`_removal_worktrees`).
    """
    if not project_ids:
        return None
    with get_db() as conn:
        cursor = conn.cursor()
        binding = _removal_binding(cursor, project_ids)
        if binding is None:
            return None
        return {"binding": binding, "chats": _removal_chats(cursor, binding),
                "worktrees": _removal_worktrees(cursor, binding["project_ids"])}


def delete_project(project_id: str) -> bool:
    """Delete a project and its chats on this worker (see ``chat_in_removal``); files stay.

    The API removes the chats first through the ordinary chat delete
    (``app/core/project_removal.py``); this sweep covers the rest.
    """
    from app.db import project_catalog_aliases
    from app.db.sessions import delete_session

    # Each chat goes through the ordered delete (app/db/session_locks.py): its
    # FK cascade removes staged hand-off rows, so a raw DELETE here locked the
    # session before their headers and deadlocked hand-off cleanup (#1215).
    # It also clears the runtime rows (runs, turns, events) a raw DELETE left.
    # A chat created in the project after this read survives as a no-project
    # chat (project_id is ON DELETE SET NULL), never as a dangling reference.
    plan = project_removal_plan([project_id])
    for row in plan["chats"] if plan else ():
        # Conditional: a chat moved to another project or worker after this
        # read (a cross-device resume or worktree reopen) is not this
        # project's to delete (review S10).
        delete_session(row["id"], matches=lambda locked, b=plan["binding"]: chat_in_removal(locked, b))
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, instance_id, link_group_id FROM projects WHERE id = %s FOR UPDATE", (project_id,))
        project = row_to_dict(cursor.fetchone())
        if project:
            project_catalog_aliases.revoke(cursor, project_id)
            _retire_from_catalog(cursor, project)
        cursor.execute("DELETE FROM projects WHERE id = %s", (project_id,))
        return cursor.rowcount > 0


from app.core.resource_sync import install_database_mutations as _install_resource_sync

_install_resource_sync(
    globals(), 'projects',
    'create_project update_project delete_project set_project_link_group seed_project_catalog',
    scope='shared',
)
