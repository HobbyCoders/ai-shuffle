"""Which workers have which project, and setting a project up on another worker.

The Projects page's cross-worker view:

- ``GET /projects/overview``: every logical project known to any worker, with
  each approved worker's state (ready, missing, setting up, offline, ...).
- ``GET /projects/workers``: one project's per-worker detail (folder, branch,
  changes, where a setup would put it).
- ``POST /projects/setup``: set a project up on a worker (this one included).
- ``POST /projects/setup/{op_id}/cancel`` and
  ``DELETE /projects/workers/{install_id}``.

The setup itself runs on the target worker (``app.core.project_setup``). This
module asks each online peer once per read (bounded, like
``GET /cluster/projects``) and fails open: a peer whose answer is missing is
``unknown``, never ``missing``.

Registered BEFORE the projects router so ``/projects/overview`` and
``/projects/workers`` are not read as a project id.
"""
from __future__ import annotations

import asyncio
import logging
import ntpath
import posixpath
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.api.auth import get_api_user_from_request, require_admin, require_auth
from app.api.cluster import _catalog_scoped_origin, _self_install_id
from app.api.common import require_origin_access
from app.core import project_setup as setup
from app.core.project_links import remote_key
from app.db import database
from app.db.async_database import run_sync

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/projects", tags=["Projects"])

PEER_CONCURRENCY = 4


class SetupRequest(BaseModel):
    project_v2_id: Optional[str] = None
    project_id: Optional[str] = None
    install_id: str = Field(..., min_length=1, max_length=200)
    folder: Optional[str] = Field(None, max_length=1024)
    mode: Literal["auto", "adopt"] = "auto"


# ---------------------------------------------------------------------------
# Reading every worker
# ---------------------------------------------------------------------------

def _approved_workers() -> List[Dict[str, Any]]:
    return [row for row in database.get_installs() if row.get("id") and row.get("approved", True)]


def _shared_db_status(runner_id: str, project_v2_id: Optional[str]) -> Dict[str, Any]:
    """A peer's status read straight from a shared database (no worker network)."""
    from app.db import checkouts as checkouts_db

    out: Dict[str, Any] = {
        "project_ids": checkouts_db.eligible_base_project_ids(runner_id),
        "workspace": None,
        "ops": list(setup.latest_ops(runner_id).values()),
        "projects": {},
    }
    if project_v2_id:
        base = next((ck for ck in checkouts_db.list_checkouts(project_v2_id)
                     if ck.get("runner_id") == runner_id and not ck.get("branch")), None)
        out["detail"] = {"folder": (base or {}).get("path"), "branch": None,
                         "dirty": (base or {}).get("dirty"), "changed_files": None}
    return out


async def _collect(installs: List[Dict[str, Any]], me: str,
                   project_v2_id: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """``{install_id: view}``: each worker's status payload, or why there is none."""
    from app.db import project_setup as setup_db
    from app.network import control, engine, git as network_git

    networked = engine.context() is not None
    semaphore = asyncio.Semaphore(PEER_CONCURRENCY)

    async def one(install: Dict[str, Any]) -> Dict[str, Any]:
        install_id = install["id"]
        is_self = install_id == me
        view = {"install": install, "is_self": is_self, "online": bool(is_self or install.get("online")),
                "status": None, "unsupported": False}
        if is_self:
            view["status"] = await run_sync(setup.local_status, project_v2_id)
            return view
        if not networked:
            view["status"] = await run_sync(_shared_db_status, install_id, project_v2_id)
            return view
        if not view["online"]:
            return view
        async with semaphore:
            try:
                view["status"] = await control.call(
                    install_id, setup.ACTION, {"action": "status", "project_v2_id": project_v2_id},
                    timeout=setup.PEER_TIMEOUT_SECONDS)
            except control.ControlRefused as exc:
                if setup._UNKNOWN_ACTION not in str(exc):
                    logger.warning("Cannot read project setup state from worker %s: %s", install_id, exc)
                else:
                    view["unsupported"] = True
                    try:
                        ids = await network_git.catalog(install_id)
                        view["status"] = {"project_ids": ids, "workspace": None, "ops": [], "projects": {}}
                    except Exception:  # noqa: BLE001 - fail open: unknown, never missing
                        logger.warning("Cannot read project catalog from worker %s", install_id, exc_info=True)
            except Exception:  # noqa: BLE001 - fail open: unknown, never missing
                logger.warning("Cannot read project setup state from worker %s", install_id, exc_info=True)
        if view["status"] is not None:
            has = set(view["status"].get("project_ids") or []) | set((view["status"].get("projects") or {}))
            try:
                await run_sync(setup_db.save_peer_catalog, install_id, sorted(has))
            except Exception:  # noqa: BLE001 - the cache is only for offline display
                logger.debug("Peer catalog cache write failed for %s", install_id, exc_info=True)
        return view

    views = await asyncio.gather(*(one(install) for install in installs))
    return {view["install"]["id"]: view for view in views}


def _offline_has(installs: List[Dict[str, Any]], networked: bool) -> Dict[str, set]:
    """Projects each worker had when last seen: the cached catalog in a network,
    the shared legacy ``projects`` rows on a shared database."""
    from app.db import project_setup as setup_db
    from app.db.base import get_db
    from app.db.projects_v2 import project_v2_id_for_origin

    if networked:
        return {device: set(ids) for device, ids in setup_db.peer_catalogs().items()}
    ids = {row["id"] for row in installs}
    out: Dict[str, set] = {}
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, link_group_id, instance_id FROM projects WHERE instance_id IS NOT NULL")
        for row in cursor.fetchall():
            if row["instance_id"] in ids:
                origin = row["link_group_id"] or ("local:" + str(row["id"]))
                out.setdefault(row["instance_id"], set()).add(project_v2_id_for_origin(origin))
    return out


def _session_counts() -> Dict[str, int]:
    """Sessions per logical project: by the legacy row's CURRENT origin when the
    session names a project row here (a project linked to its remote after its
    first chats keeps those chats' older ``project_v2_id``, #802), else by
    ``project_v2_id``."""
    from app.core.project_resolver import origin_for_legacy_project
    from app.db.base import get_db
    from app.db.projects_v2 import project_v2_id_for_origin

    counts: Dict[str, int] = {}
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT s.project_v2_id AS project_v2_id, p.id AS id, p.link_group_id AS link_group_id, "
                       "COUNT(*) AS n FROM sessions s LEFT JOIN projects p ON p.id = s.project_id "
                       "WHERE s.project_v2_id IS NOT NULL OR p.id IS NOT NULL "
                       "GROUP BY s.project_v2_id, p.id, p.link_group_id")
        for row in cursor.fetchall():
            pv2 = (project_v2_id_for_origin(origin_for_legacy_project(row)) if row["id"] is not None
                   else row["project_v2_id"])
            counts[pv2] = counts.get(pv2, 0) + int(row["n"])
    return counts


def _local_snapshot() -> Dict[str, Any]:
    """Every local read the merge needs, in one thread hop."""
    from app.core.project_resolver import resolve_project_v2
    from app.db import projects_v2
    from app.db import project_setup as setup_db

    setup.ensure_repo_records()
    own_rows = setup._own_project_rows()
    pv2s = {row["id"]: row for row in projects_v2.list_projects_v2()}
    for row in own_rows:
        # A project added here gets its logical identity on first use; seed it
        # now (the same self-seed git-state does) so it is listed and peers see it.
        if projects_v2.project_v2_id_for_origin(setup._origin_of(row)) not in pv2s:
            seeded = resolve_project_v2({"project_id": row["id"]})
            if seeded:
                pv2s[seeded["id"]] = seeded
    return {
        "pv2s": pv2s,
        "own_by_origin": setup._own_rows_by_origin(),
        "superseded": setup._superseded_local_origins(own_rows),
        "repos": setup._repo_info_by_origin(),
        "requests": setup_db.list_requests(),
        "session_counts": _session_counts(),
    }


def derive_state(*, pv2_id: str, view: Dict[str, Any], has_offline: bool,
                 request: Optional[Dict[str, Any]], superseded_here: bool) -> tuple:
    """``(state, setup)`` for one worker and one project (pure)."""
    payload = view.get("status")
    op = None
    if payload is not None:
        op = next((item for item in payload.get("ops") or [] if item.get("project_v2_id") == pv2_id), None)
    setup_info = None
    if request is not None:
        if request["status"] == "queued_offline":
            setup_info = {"op_id": request["id"], "status": "queued", "progress": None,
                          "error_code": None, "error": None}
        elif request["status"] == "error":
            setup_info = {"op_id": request["id"], "status": "error", "progress": None,
                          "error_code": request.get("error_code") or setup.INTERNAL,
                          "error": request.get("error")}
    if setup_info is None and op is not None:
        setup_info = {key: op.get(key) for key in ("op_id", "status", "progress", "error_code", "error")}
        if setup_info["error_code"] == setup.UNSUPPORTED and not setup_info["error"]:
            setup_info["error"] = setup.unsupported_message(view["install"].get("device_name"))
    if request is not None and request["status"] == "queued_offline":
        return "queued_offline", setup_info
    if setup_info is not None and setup_info["status"] in ("queued", "running"):
        if not view["online"] and setup_info["status"] == "queued":
            return "queued_offline", setup_info  # shared database: it runs when that worker is back
        return "setting_up", setup_info
    has_now = False
    if payload is not None:
        has = set(payload.get("project_ids") or []) | set(payload.get("projects") or {})
        has_now = pv2_id in has and not (view["is_self"] and superseded_here)
    if view["online"] and has_now:
        return "ready", setup_info
    if view["online"] and payload is None:
        return "unknown", setup_info
    if setup_info is not None and setup_info["status"] == "error":
        return "failed", setup_info
    if not view["online"]:
        return ("offline_has" if has_offline or has_now else "offline_missing"), setup_info
    return "missing", setup_info


def _latest_requests(requests: List[Dict[str, Any]]) -> Dict[tuple, Dict[str, Any]]:
    """Newest waiting or failed request per (project, worker)."""
    out: Dict[tuple, Dict[str, Any]] = {}
    for row in requests:  # newest first
        key = (row["project_v2_id"], row["target_id"])
        if key in out:
            continue
        if row["status"] in ("queued_offline", "error"):
            out[key] = row
        else:
            out[key] = {}  # a later dispatch/cancel supersedes older failures
    return {key: row for key, row in out.items() if row}


def _project_info(pv2: Dict[str, Any], snapshot: Dict[str, Any],
                  views: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    origin = pv2.get("origin") or ""
    repo = snapshot["repos"].get(origin) or {}
    own = snapshot["own_by_origin"].get(origin)
    peer_info: Dict[str, Any] = {}
    for view in views.values():
        info = ((view.get("status") or {}).get("projects") or {}).get(pv2["id"])
        if info and (not peer_info or (info.get("clone_url") and not peer_info.get("clone_url"))):
            peer_info = info
    clone_url = repo.get("clone_url") or (peer_info.get("clone_url") if origin.startswith(("git:", "manual:")) else None)
    folder_name = (setup._folder_name(own.get("path")) if own else None) or peer_info.get("folder_name") \
        or setup._slug(pv2.get("name") or "") or "project"
    return {
        "kind": "git" if clone_url or origin.startswith("git:") else "folder",
        "clone_url": clone_url,
        "remote": setup.display_remote(clone_url),
        # What the New project dialog compares a pasted URL with: never a credential.
        "remote_key": remote_key(clone_url) or None,
        # Other spellings of the remote (git@ vs https) a setup tries if this one is refused.
        "alternate_urls": list(repo.get("alternate_urls") or []) if repo.get("clone_url") else [],
        "default_branch": repo.get("default_branch") or peer_info.get("default_branch"),
        "default_folder": folder_name,
        "local_project_id": own.get("id") if own and origin not in snapshot["superseded"] else None,
    }


def _worker_entry(pv2_id: str, view: Dict[str, Any], snapshot: Dict[str, Any],
                  offline_has: Dict[str, set], latest: Dict[tuple, Dict[str, Any]],
                  origin: str) -> Dict[str, Any]:
    install = view["install"]
    state, setup_info = derive_state(
        pv2_id=pv2_id, view=view,
        has_offline=pv2_id in offline_has.get(install["id"], set()),
        request=latest.get((pv2_id, install["id"])),
        superseded_here=origin in snapshot["superseded"],
    )
    return {
        "install_id": install["id"],
        "device_name": install.get("device_name"),
        "is_self": view["is_self"],
        "online": view["online"],
        "approved": bool(install.get("approved", True)),
        "state": state,
        "setup": setup_info,
    }


_LISTED_STATES = {"ready", "setting_up", "queued_offline", "failed", "offline_has"}
#: A worker in one of these holds (or is getting) a copy: the project is live.
_HELD_STATES = {"ready", "setting_up", "queued_offline", "offline_has"}
#: Online but unreadable: it may hold a copy, so the project is never treated as unheld.
_MAYBE_HELD_STATES = _HELD_STATES | {"unknown"}


def _folded(pv2: Dict[str, Any], pv2s: Dict[str, Dict[str, Any]]) -> bool:
    """A duplicate folded into another project that is still known here."""
    from app.core.project_remote import merged_into

    target = merged_into(pv2)
    return bool(target and any(row.get("origin") == target for row in pv2s.values()))


def _retired(pv2: Dict[str, Any], pv2s: Dict[str, Dict[str, Any]]) -> bool:
    """Forgotten by the owner, or a duplicate folded into another project."""
    project_settings = pv2.get("settings") if isinstance(pv2.get("settings"), dict) else {}
    return project_settings.get("forgotten") is True or _folded(pv2, pv2s)


def _shown(pv2: Dict[str, Any], pv2s: Dict[str, Dict[str, Any]], workers: List[Dict[str, Any]],
           offline_has: Dict[str, set]) -> bool:
    """Whether the Projects overview gives ``pv2`` a card, from each worker's entry."""
    listed = any(w["state"] in _LISTED_STATES for w in workers) or any(
        w["state"] == "unknown" and pv2["id"] in offline_has.get(w["install_id"], set()) for w in workers)
    if not listed:
        return False
    # Nothing left of a retired one anywhere: a failed old setup must not keep it stuck here.
    return not (_retired(pv2, pv2s) and not any(w["state"] in _MAYBE_HELD_STATES for w in workers))


def _listed_here(origin: str) -> bool:
    """Whether the Projects overview lists ``origin`` from what this worker's
    own database holds (:func:`_shown`; sync, never waits on a peer).

    Each worker's state comes from this database only: this worker's own
    status and, on a shared database, every runner's rows and setup ops. In
    a network a paired worker keeps its own database, so its live state is
    not read here and it counts as holding nothing: a duplicate of a project
    only such a worker holds is not refused (a known limit).

    A first clone in progress (the identity's provisional mark) is no copy:
    only a registered one, or a setup on this database, lists it.
    """
    from app.db import projects_v2
    from app.network import engine

    pv2 = projects_v2.get_project_v2_by_origin(origin)
    if not pv2:
        return False
    provisional = projects_v2.provisional_token(pv2) is not None
    installs = _approved_workers()
    me = _self_install_id()
    networked = engine.context() is not None
    offline_has = {} if networked else _offline_has(installs, networked)
    snapshot = {"superseded": setup._superseded_local_origins(setup._own_project_rows())}
    workers = []
    for install in installs:
        install_id = install["id"]
        is_self = install_id == me
        if networked and not is_self:
            continue
        view = {"install": install, "is_self": is_self, "online": bool(is_self or install.get("online")),
                "status": setup.local_status() if is_self else _shared_db_status(install_id, None),
                "unsupported": False}
        if provisional:
            view["status"] = {**view["status"], "ops": []}
        workers.append(_worker_entry(pv2["id"], view, snapshot, offline_has, {}, origin))
    if provisional:
        # Only a copy counts: judged as hidden, a worker must hold it.
        marks = pv2.get("settings") if isinstance(pv2.get("settings"), dict) else {}
        pv2 = {**pv2, "settings": {**marks, "forgotten": True}}
    pv2s = {row["id"]: row for row in projects_v2.list_projects_v2()}
    return _shown(pv2, {**pv2s, pv2["id"]: pv2}, workers, offline_has)


async def _gather(project_v2_id: Optional[str] = None) -> tuple:
    from app.network import engine

    installs = await run_sync(_approved_workers)
    me = _self_install_id()
    snapshot, views = await asyncio.gather(run_sync(_local_snapshot), _collect(installs, me, project_v2_id))
    offline_has = await run_sync(_offline_has, installs, engine.context() is not None)
    latest = _latest_requests(snapshot["requests"])
    return installs, me, snapshot, views, offline_has, latest


def _ordered_views(views: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(views.values(), key=lambda v: (not v["is_self"], str(v["install"].get("device_name") or "").lower(),
                                                 v["install"]["id"]))


@router.get("/overview")
async def projects_overview(request: Request, token: str = Depends(require_auth)) -> Dict[str, Any]:
    """Every logical project and every approved worker's state for it."""
    user = get_api_user_from_request(request)
    scoped_origin = await run_sync(_catalog_scoped_origin, user)
    if scoped_origin == "":
        return {"projects": []}
    _, _, snapshot, views, offline_has, latest = await _gather()
    ordered = _ordered_views(views)
    projects = []
    for pv2 in snapshot["pv2s"].values():
        origin = pv2.get("origin") or ""
        if scoped_origin and origin != scoped_origin:
            continue
        workers = [_worker_entry(pv2["id"], view, snapshot, offline_has, latest, origin) for view in ordered]
        if not scoped_origin and _folded(pv2, snapshot["pv2s"]):
            continue  # its copies are listed under the project it was folded into
        if not _shown(pv2, snapshot["pv2s"], workers, offline_has):
            continue
        info = _project_info(pv2, snapshot, views)
        projects.append({
            "project_v2_id": pv2["id"],
            "local_project_id": info["local_project_id"],
            "name": pv2.get("name") or origin,
            "kind": info["kind"],
            "remote": info["remote"],
            "remote_key": info["remote_key"],
            "default_branch": info["default_branch"],
            "session_count": snapshot["session_counts"].get(pv2["id"], 0),
            "workers": workers,
        })
    projects.sort(key=lambda p: (str(p["name"]).casefold(), p["project_v2_id"]))
    return {"projects": projects}


def _join_folder(workspace: Optional[str], folder: str) -> Optional[str]:
    if not workspace:
        return None
    if "\\" in workspace or ntpath.splitdrive(workspace)[0]:
        return ntpath.join(workspace, folder)
    return posixpath.join(workspace, folder)


@router.get("/workers")
async def project_workers(
    request: Request,
    project_id: Optional[str] = Query(None, description="Legacy project id on this worker"),
    project_v2_id: Optional[str] = Query(None, description="Logical project id"),
    token: str = Depends(require_auth),
) -> Dict[str, Any]:
    """One project's per-worker detail for the Workers tab."""
    access = await run_sync(require_origin_access, request=request, project_id=project_id,
                            project_v2_id=project_v2_id)
    pv2 = access["project_v2"]
    if not pv2:
        raise HTTPException(status_code=404, detail="Project not found")
    sees_paths = getattr(request.state, "is_admin", False) is True and get_api_user_from_request(request) is None
    _, _, snapshot, views, offline_has, latest = await _gather(pv2["id"])
    snapshot["pv2s"].setdefault(pv2["id"], pv2)
    origin = pv2.get("origin") or ""
    info = _project_info(pv2, snapshot, views)
    workers = []
    for view in _ordered_views(views):
        entry = _worker_entry(pv2["id"], view, snapshot, offline_has, latest, origin)
        payload = view.get("status") or {}
        detail = payload.get("detail") or {}
        has_copy = entry["state"] in ("ready", "offline_has")
        # Worktree folders that stay on disk when the copy is removed. None from
        # a worker too old to report them: its confirmation names no number.
        kept = detail.get("removal_worktrees") if has_copy else None
        kept = kept if isinstance(kept, list) else None
        entry.update({
            "folder": detail.get("folder") if sees_paths and has_copy else None,
            "folder_revision": detail.get("folder_revision") if sees_paths and entry["state"] == "ready" else None,
            "suggested_folder": (_join_folder(payload.get("workspace"), info["default_folder"])
                                 if sees_paths and not has_copy else None),
            "branch": detail.get("branch") if has_copy else None,
            "dirty": detail.get("dirty") if has_copy else None,
            "changed_files": detail.get("changed_files") if has_copy else None,
            "removal_chats": detail.get("removal_chats") if has_copy else None,
            "removal_worktree_count": len(kept) if kept is not None else None,
            "removal_worktrees": kept if sees_paths else None,
            "last_seen": setup._iso(view["install"].get("last_seen")),
        })
        workers.append(entry)
    return {
        "project_v2_id": pv2["id"],
        "local_project_id": info["local_project_id"],
        "name": pv2.get("name") or origin,
        "kind": info["kind"],
        "remote": info["remote"],
        "default_branch": info["default_branch"],
        "default_folder": info["default_folder"],
        "workers": workers,
    }


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------

def _find_install(installs: List[Dict[str, Any]], install_id: str) -> Dict[str, Any]:
    from app.network import runners

    row = next((row for row in installs if row["id"] == install_id), None)
    if row is None:
        # A legacy instance id for a networked worker.
        wanted = runners.resolve_runner_id(install_id)
        row = next((row for row in installs if row["id"] == wanted), None)
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown worker")
    return row


async def _spec_for(pv2: Dict[str, Any], body: SetupRequest, installs: List[Dict[str, Any]],
                    me: str) -> Dict[str, Any]:
    """What the target needs: identity, kind, remote and folder."""
    snapshot = await run_sync(_local_snapshot)
    if (pv2.get("origin") or "").startswith("local:"):
        # The snapshot may just have linked a remote-less placeholder to its
        # remote (#802): set up what it PROVABLY became, per this worker's
        # first-link record, never by matching names or legacy ids.
        from app.core.project_resolver import resolve_catalog_project

        pv2 = await run_sync(resolve_catalog_project, pv2["id"]) or pv2
    views: Dict[str, Dict[str, Any]] = {}
    info = _project_info(pv2, snapshot, views)
    origin = pv2.get("origin") or ""
    if info["clone_url"] is None and origin.startswith(("git:", "manual:")):
        # Not on this worker: ask a worker that has it for its remote.
        views = await _collect([row for row in installs if row["id"] != me and row.get("online")], me)
        info = _project_info(pv2, snapshot, views)
    folder = (body.folder or "").strip() or info["default_folder"]
    return {
        "project_v2_id": pv2["id"], "origin": origin, "name": pv2.get("name") or info["default_folder"],
        "kind": info["kind"], "clone_url": info["clone_url"], "alternate_urls": info["alternate_urls"],
        "folder": folder, "mode": body.mode,
    }


def _shared_db_enqueue(target_id: str, spec: Dict[str, Any], op_id: str) -> Dict[str, Any]:
    """Shared database, no network: queue the op for the target's own claim pass."""
    from app.db import git_ops as git_ops_db

    for row in git_ops_db.list_ops_for_runner(target_id, setup.SETUP_OP, limit=50):
        if row.get("project_v2_id") == spec["project_v2_id"] and row["status"] in ("queued", "running"):
            return row
    params = {key: spec.get(key) for key in ("origin", "name", "kind", "mode", "clone_url", "alternate_urls",
                                             "folder")}
    params.update(path=None, requested_by=None)
    return git_ops_db.insert_op(target_id, setup.SETUP_OP, project_v2_id=spec["project_v2_id"],
                                params=params, op_id=op_id)


@router.post("/setup", status_code=status.HTTP_202_ACCEPTED)
async def setup_project(request: Request, body: SetupRequest,
                        token: str = Depends(require_admin)) -> Dict[str, Any]:
    """Set a project up on a worker: clone it there, adopt a same-repo folder, or
    use (else create) its folder. Idempotent while one is in flight."""
    from app.db import project_setup as setup_db
    from app.network import control, engine

    access = await run_sync(require_origin_access, request=request, project_id=body.project_id,
                            project_v2_id=body.project_v2_id)
    pv2 = access["project_v2"]
    if not pv2:
        raise HTTPException(status_code=404, detail="Project not found")
    installs = await run_sync(_approved_workers)
    me = _self_install_id()
    target = _find_install(installs, body.install_id)
    target_id = target["id"]
    spec = await _spec_for(pv2, body, installs, me)
    pv2 = {**pv2, "id": spec["project_v2_id"]}  # the identity actually set up
    if spec["kind"] == "git" and not spec["clone_url"]:
        raise HTTPException(status_code=409, detail=(
            "Can't find this project's Git remote. Bring a worker that has the project online and try again."))
    from app.db import projects_v2

    await run_sync(projects_v2.set_forgotten, access["project_v2"]["origin"], False)  # wanted again
    op_id = setup.new_op_id()

    if target_id == me:
        try:
            result = await setup.start_local_now(spec, op_id=op_id,
                                                 requested_by=setup._own_device_id())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        setup.emit_changed()
        return {"op_id": result["id"], "install_id": target_id,
                "status": "done" if result["status"] == "done" else "queued"}

    if engine.context() is None:
        row = await run_sync(_shared_db_enqueue, target_id, spec, op_id)
        setup.emit_changed()
        return {"op_id": row["id"], "install_id": target_id,
                "status": "queued" if target.get("online") else "queued_offline"}

    params = {"spec": spec, "worker_name": target.get("device_name")}

    async def wait_offline() -> Dict[str, Any]:
        await run_sync(setup_db.insert_request, op_id, pv2["id"], target_id, params,
                       setup_db.REQUEST_QUEUED_OFFLINE)
        setup.note_pending(target_id)
        setup.emit_changed()
        return {"op_id": op_id, "install_id": target_id, "status": "queued_offline"}

    existing = next((row for row in await run_sync(setup_db.list_requests, project_v2_id=pv2["id"],
                                                   status=setup_db.REQUEST_QUEUED_OFFLINE)
                     if row["target_id"] == target_id), None)
    if existing is not None:
        return {"op_id": existing["id"], "install_id": target_id, "status": "queued_offline"}
    if not target.get("online"):
        return await wait_offline()
    try:
        result = await control.call(target_id, setup.ACTION, {"action": "start", "op_id": op_id, "spec": spec},
                                    timeout=setup.PEER_TIMEOUT_SECONDS)
    except control.MaybeApplied:
        result = {"id": op_id, "status": "queued"}
    except control.RunnerUnreachable:
        return await wait_offline()
    except control.ControlRefused as exc:
        if setup._UNKNOWN_ACTION in str(exc):
            message = setup.unsupported_message(target.get("device_name"))
            await run_sync(setup_db.insert_request, op_id, pv2["id"], target_id, params,
                           setup_db.REQUEST_ERROR, error_code=setup.UNSUPPORTED, error=message)
            setup.emit_changed()
            raise HTTPException(status_code=409, detail={"error_code": setup.UNSUPPORTED, "error": message}) from exc
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    remote_id = str(result.get("id") or op_id)
    await run_sync(setup_db.insert_request, remote_id, pv2["id"], target_id, params,
                   setup_db.REQUEST_DISPATCHED)
    setup.emit_changed()
    return {"op_id": remote_id, "install_id": target_id,
            "status": "done" if result.get("status") == "done" else "queued"}


ALREADY_ADDED = "already_added"
BEING_SET_UP = "being_set_up"


class CloneNewRequest(BaseModel):
    """A new project cloned from the New project dialog (same id/name rules as ``ProjectCreate``)."""
    id: str = Field(..., pattern=r"^[a-z0-9-]+$", min_length=1, max_length=50)
    name: str = Field(..., min_length=1, max_length=100)
    clone_url: str = Field(..., min_length=1, max_length=2048)
    #: A folder name or path on this worker; omitted = ``<workspace>/<id>``.
    folder: Optional[str] = Field(None, max_length=1024)


def _first_clone_alive(pv2: Dict[str, Any], owner: Dict[str, Any]) -> bool:
    """Whether the first clone that marked ``pv2`` provisional may still be
    running, judged from this worker's own database only.

    Settled (hidden after it failed), finished or stale ops are not. An op
    this database doesn't hold (a paired worker's, which keeps its own
    database) is not either: whether it runs isn't decided here, so a new
    clone here may proceed (duplicates between such workers aren't refused).
    """
    from app.db import git_ops as git_ops_db

    marks = pv2.get("settings") if isinstance(pv2.get("settings"), dict) else {}
    if marks.get("forgotten") is True:
        return False
    row = git_ops_db.get_op(owner["op_id"])
    if row is None:
        return False
    return not (row.get("status") in git_ops_db.TERMINAL_STATUSES or git_ops_db._is_stale_running(row))


def _being_set_up(owner: Optional[Dict[str, Any]], doing: str = "cloning") -> HTTPException:
    """409 for a repository a runner on this database is cloning as a new
    project (its provisional mark) or setting up (:func:`setup.setup_holder`)."""
    runner_id = (owner or {}).get("runner_id") or ""
    if runner_id == setup._me():
        worker = "This worker"
    else:
        rows = {row["id"]: row for row in _approved_workers()}
        worker = (rows.get(runner_id) or {}).get("device_name") or "Another worker"
    return HTTPException(status_code=409, detail={
        "error_code": BEING_SET_UP, "worker": worker,
        "error": f"{worker} is {doing} this repository; wait for it or cancel it there."})


def _clone_new_spec(body: CloneNewRequest, op_id: str) -> Dict[str, Any]:
    """Check a new clone before anything runs; the setup spec for it (sync).

    Refuses (409) a repository that is already a project as far as this
    worker's own database shows (:func:`_listed_elsewhere`), a taken id, name
    or folder, and a folder that has other files in it, so the owner hears it
    at once instead of after a clone; and one that a runner on this database
    is cloning as a new project or setting up, under either joined spelling
    of its remote (``being_set_up``). A paired worker with its own database is not asked:
    a duplicate between such workers is not refused here.
    """
    from pathlib import Path

    from app.api.projects import check_new_project
    from app.core.git_service import classify_clone_url
    from app.core.models import ProjectCreate
    from app.core.project_links import same_remote
    from app.core.project_remote import identity_for_remote, pinned_remote
    from app.core.scrub import strip_url_userinfo
    from app.db import git_ops as git_ops_db
    from app.db import projects_v2

    # Each worker clones with its own sign-in: credentials in a pasted URL are
    # never used or stored.
    clean = strip_url_userinfo(body.clone_url.strip()) or ""
    kind, detail = classify_clone_url(clean)
    if kind == "invalid":
        raise HTTPException(status_code=400, detail=detail or "Enter the repository's clone URL")
    if kind != "remote":
        # Network remotes only (https://, ssh://, git@host:path). A path or
        # file:// URL is a folder on this worker: the Folder tab adds it, and
        # Git's own discovery could open a repository outside what was checked.
        raise HTTPException(status_code=400, detail="To add a folder on this worker, use Folder.")
    found = identity_for_remote(clean)
    if not found:
        raise HTTPException(status_code=400, detail="Enter the repository's clone URL")
    origin = found[0]
    existing = (setup._own_rows_by_origin().get(origin) or _listed_elsewhere(origin)
                or _project_with_remote(clean))
    if existing is not None:
        raise HTTPException(status_code=409, detail={
            "error_code": ALREADY_ADDED, "project_id": existing.get("id"),
            "error": existing.get("message")
            or f"This repository is already the project {existing.get('name') or existing.get('id')}."})
    pv2 = projects_v2.get_project_v2_by_origin(origin)
    pinned = pinned_remote(pv2)
    if pinned and not same_remote(pinned, clean):
        # The project this repository was first seen as now uses another one.
        raise HTTPException(status_code=409, detail={
            "error_code": setup.MOVED, "error": setup.moved_message(pinned)})
    # This repository (either joined spelling) being set up on this database already:
    # another spelling's first clone (its provisional mark), or an ordinary
    # setup (the Workers tab), which no mark records.
    spellings = _same_repository_origins(clean) | {origin}
    for other in sorted(spellings - {origin}):
        other_row = projects_v2.get_project_v2_by_origin(other)
        other_owner = projects_v2.provisional_token(other_row)
        if other_owner is not None and _first_clone_alive(other_row, other_owner):
            raise _being_set_up(other_owner)
    holder = setup.setup_holder([projects_v2.project_v2_id_for_origin(o) for o in sorted(spellings)])
    if holder is not None:
        raise _being_set_up(holder, "setting up")
    # The id asked for: on a cross-install collision the locked creation path
    # adds the suffix itself (``payload.id`` only names the folder it would get).
    payload, _db_path, path = check_new_project(
        ProjectCreate(id=body.id, name=body.name.strip(), path=(body.folder or "").strip() or None))
    if path.exists() and not setup._is_empty_dir(Path(path)) and not setup._same_repo(path, clean, origin):
        raise HTTPException(status_code=409, detail={
            "error_code": setup.FOLDER_EXISTS,
            "error": f"{path} already exists and has other files in it. Choose another project ID."})
    # Not listed here (a listed one was refused above): this first clone marks
    # the identity provisional with its own op, and only that op hides it
    # again if it fails (``hide_failed_first_clone``).
    mark = {"runner_id": setup._me(), "op_id": op_id}
    owner = projects_v2.provisional_token(pv2)
    if pv2 is None:
        projects_v2.upsert_project(origin, name=payload.name)
        claimed = projects_v2.claim_provisional(origin, mark, expect=None)
    elif owner is None:
        # A remembered identity: it keeps its name.
        claimed = projects_v2.claim_provisional(origin, mark, expect=None)
    else:
        # An earlier attempt marked it. One that ended here (an earlier try,
        # say) is replaced, and the project is named after this attempt; a
        # mark from a paired worker's own database is replaced but keeps its name.
        if _first_clone_alive(pv2, owner):
            raise _being_set_up(owner)
        ours = git_ops_db.get_op(owner["op_id"]) is not None
        claimed = projects_v2.claim_provisional(origin, mark, expect=owner, name=payload.name if ours else None)
    if not claimed:
        raise _being_set_up(projects_v2.provisional_token(projects_v2.get_project_v2_by_origin(origin)) or owner)
    # Its remote on the identity, so another spelling finds it while it clones.
    projects_v2.record_remote(origin, clean)
    return {
        "project_v2_id": projects_v2.project_v2_id_for_origin(origin), "origin": origin,
        "name": payload.name, "kind": "git", "clone_url": clean,
        "folder": (body.folder or "").strip() or payload.id, "mode": "auto",
        "project_id": body.id, "expected_id": payload.id,
    }


def _same_repository_origins(clean: str) -> set:
    """Every identity whose remote is ``clean`` by identity: a remote naming
    one of the ids ``clean`` names (``remote_link_ids``), so
    ``git@github.com:owner/repo`` and ``https://github.com/owner/repo`` (on a
    public forge, :func:`~app.core.project_links.joined_pair`) match and two SSH
    logins on one host don't."""
    from app.core.project_links import remote_link_ids

    wanted = remote_link_ids(clean)
    if not wanted:
        return set()
    return {origin for origin, info in setup._repo_info_by_origin().items()
            if remote_link_ids(info.get("clone_url")) & wanted}


def _project_with_remote(clean: str) -> Optional[Dict[str, Any]]:
    """The project (this worker's row, else one listed from this database) whose
    remote is ``clean`` (:func:`_same_repository_origins`), as the repository
    picker marks "Already added".

    On a public forge ``git@host:owner/repo`` and ``https://host/owner/repo`` are one identity,
    but a project an older version added over SSH stays keyed by that spelling. Listed means
    :func:`_listed_elsewhere`; an identity only remembered from a removed
    project does not block a new clone.
    """
    matches = sorted(_same_repository_origins(clean))
    if not matches:
        return None
    own = setup._own_rows_by_origin()
    for origin in matches:
        if origin in own:
            return own[origin]
    for origin in matches:
        found = _listed_elsewhere(origin)
        if found:
            return found
    return None


def _listed_elsewhere(origin: str) -> Optional[Dict[str, Any]]:
    """The already-added answer for an identity listed from this database
    (:func:`_listed_here`), else None."""
    from app.db import projects_v2

    if not _listed_here(origin):
        return None
    name = (projects_v2.get_project_v2_by_origin(origin) or {}).get("name") or "another project"
    return {"id": None, "name": name,
            "message": f"This repository is already the project {name} on another worker. "
                       "Set it up here from its page in Projects."}


@router.post("/clone", status_code=status.HTTP_202_ACCEPTED)
async def clone_new_project(body: CloneNewRequest, token: str = Depends(require_admin)) -> Dict[str, Any]:
    """Create a project by cloning a repository on THIS worker.

    Runs as a setup op (``app.core.project_setup``): the clone goes to a
    temporary folder with progress and cancel, and the project is registered
    only once it succeeded. A failed or cancelled clone leaves no project
    behind, so retrying with the same id works. Poll ``GET /projects/setup/{op_id}``.
    """
    op_id = setup.new_op_id()
    spec = await run_sync(_clone_new_spec, body, op_id)
    try:
        result = await setup.start_local_now(spec, op_id=op_id, requested_by=setup._own_device_id())
    except ValueError as exc:
        await run_sync(setup.hide_failed_first_clone, {"id": op_id, "params": spec})
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    setup.emit_changed()
    return {"op_id": result["id"], "project_v2_id": spec["project_v2_id"],
            "project_id": spec["expected_id"], "status": result["status"]}


@router.get("/setup/{op_id}")
async def setup_status(op_id: str, token: str = Depends(require_admin)) -> Dict[str, Any]:
    """One setup op on THIS worker: status, progress, and the project once done."""
    from app.db import git_ops as git_ops_db

    row = await run_sync(git_ops_db.get_op, op_id)
    if row is None or row.get("op") != setup.SETUP_OP or row.get("runner_id") != setup._me():
        raise HTTPException(status_code=404, detail="Unknown setup")
    summary = setup._summarize_op(row)
    result = row.get("result") or {}
    summary["project_id"] = result.get("project_id") if row.get("status") == "done" else None
    return summary


@router.post("/setup/{op_id}/cancel")
async def cancel_setup(op_id: str, token: str = Depends(require_admin)) -> Dict[str, Any]:
    """Cancel a waiting or running setup (best effort; never touches a folder it did not create)."""
    from app.db import git_ops as git_ops_db
    from app.db import project_setup as setup_db
    from app.network import control

    me_runtime = setup._me()
    row = await run_sync(git_ops_db.get_op, op_id)
    if row is not None and row.get("op") == setup.SETUP_OP:
        if row.get("runner_id") == me_runtime:
            try:
                result = await run_sync(setup.cancel_local, op_id)
            except LookupError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except setup.SetupConflict as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            setup.emit_changed()
            return result
        # Shared database: a queued op can be withdrawn; a running clone is the target's.
        if await run_sync(git_ops_db.cancel_queued, op_id, row["runner_id"], "Setup was cancelled."):
            await run_sync(setup.hide_failed_first_clone, row)
            setup.emit_changed()
            return {"status": "cancelled"}
        raise HTTPException(status_code=409, detail="This setup is already running or finished on its worker")
    req = await run_sync(setup_db.get_request, op_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Unknown setup")
    if req["status"] == setup_db.REQUEST_QUEUED_OFFLINE:
        if await run_sync(setup_db.set_request_status, op_id, setup_db.REQUEST_CANCELLED,
                          expect=setup_db.REQUEST_QUEUED_OFFLINE):
            setup.emit_changed()
            return {"status": "cancelled"}
        req = await run_sync(setup_db.get_request, op_id) or req
    if req["status"] != setup_db.REQUEST_DISPATCHED:
        raise HTTPException(status_code=409, detail="This setup already finished")
    try:
        await control.call(req["target_id"], setup.ACTION, {"action": "cancel", "op_id": op_id},
                           timeout=setup.PEER_TIMEOUT_SECONDS)
    except control.ControlRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except control.RunnerUnreachable as exc:
        raise HTTPException(status_code=409, detail=f"Can't reach that worker to cancel: {exc}") from exc
    setup.emit_changed()
    return {"status": "cancelled"}


class ChangeFolderRequest(BaseModel):
    folder: str = Field(min_length=1, max_length=4096)
    expected_revision: str = Field(min_length=64, max_length=64)


async def _worker_folder_action(request: Request, install_id: str, project_id: Optional[str],
                                project_v2_id: Optional[str], action: str, **fields: Any) -> Dict[str, Any]:
    from app.network import control, engine

    access = await run_sync(require_origin_access, request=request, project_id=project_id,
                            project_v2_id=project_v2_id)
    pv2 = access["project_v2"]
    if not pv2:
        raise HTTPException(404, "Project not found")
    target = _find_install(await run_sync(_approved_workers), install_id)
    target_id = target["id"]
    payload = {"project_v2_id": pv2["id"], **fields}
    local = target_id == _self_install_id()
    if local:
        result = await run_sync(setup.folder_action, action, payload)
    elif not target.get("online"):
        raise HTTPException(409, "That worker is offline. Try again when it is online.")
    elif engine.context() is None:
        raise HTTPException(409, "Change the folder directly on that worker.")
    else:
        try:
            result = await control.call(target_id, setup.ACTION, {"action": action, **payload},
                                        timeout=setup.PEER_TIMEOUT_SECONDS)
        except control.ControlRefused as exc:
            if "Unknown setup action" in str(exc) or setup._UNKNOWN_ACTION in str(exc):
                raise HTTPException(409, setup.unsupported_message(target.get("device_name"))) from exc
            raise HTTPException(409, "That worker refused the folder request. Reload and try again.") from exc
        except control.RunnerUnreachable as exc:
            raise HTTPException(409, "That worker did not answer. Check its current folder before trying again.") from exc
    if "error" in result:
        code = result.get("status", 409)
        raise HTTPException(code if code in (400, 403, 404, 409) else 409, result["error"])
    if action == "change-folder":
        if local:
            from app.core.project_remote import reconcile_soon

            setup._after_local_registration_change()  # announce the updated local catalog
            reconcile_soon()  # a copy of the project's previous repository switches now
        else:
            setup.emit_changed()
    return result


@router.get("/workers/{install_id}/folder/browse")
async def browse_worker_folder(request: Request, install_id: str,
                               project_id: Optional[str] = Query(None),
                               project_v2_id: Optional[str] = Query(None),
                               path: Optional[str] = Query(None, max_length=4096),
                               token: str = Depends(require_admin)) -> Dict[str, Any]:
    """Browse directories on the worker that owns this project registration."""
    return await _worker_folder_action(request, install_id, project_id, project_v2_id,
                                       "browse-folder", folder=path)


@router.put("/workers/{install_id}/folder")
async def change_worker_folder(request: Request, install_id: str, body: ChangeFolderRequest,
                               project_id: Optional[str] = Query(None),
                               project_v2_id: Optional[str] = Query(None),
                               token: str = Depends(require_admin)) -> Dict[str, Any]:
    """Retarget one worker's project registration; do not move any files."""
    return await _worker_folder_action(request, install_id, project_id, project_v2_id,
                                       "change-folder", **body.model_dump())


class ChangeRepositoryRequest(BaseModel):
    remote_url: str = Field(min_length=1, max_length=2048)
    keep_upstream: bool = False


@router.put("/catalog/repository")
async def change_repository(request: Request, body: ChangeRepositoryRequest,
                            project_id: Optional[str] = Query(None),
                            project_v2_id: Optional[str] = Query(None),
                            token: str = Depends(require_admin)) -> Dict[str, Any]:
    """Point the project at another Git repository on every worker; it stays one project."""
    from app.core.project_remote import RepositoryChangeError, change_repository as change

    access = await run_sync(require_origin_access, request=request, project_id=project_id,
                            project_v2_id=project_v2_id)
    pv2 = access["project_v2"]
    if not pv2:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        return await change(pv2["id"], body.remote_url, keep_upstream=body.keep_upstream)
    except RepositoryChangeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/catalog/forget")
async def forget_project(request: Request,
                         project_id: Optional[str] = Query(None),
                         project_v2_id: Optional[str] = Query(None),
                         token: str = Depends(require_admin)) -> Dict[str, Any]:
    """Take a project no worker holds off the Projects page, on every worker.

    Nothing is deleted: chats that used it keep it, and it comes back as soon
    as any worker has a copy again. Refused while a worker still holds one.
    """
    from app.db import project_setup as setup_db, projects_v2

    access = await run_sync(require_origin_access, request=request, project_id=project_id,
                            project_v2_id=project_v2_id)
    pv2 = access["project_v2"]
    if not pv2:
        raise HTTPException(status_code=404, detail="Project not found")
    installs, _me, snapshot, views, offline_has, latest = await _gather(pv2["id"])
    origin = pv2.get("origin") or ""
    entries = [_worker_entry(pv2["id"], view, snapshot, offline_has, latest, origin)
               for view in _ordered_views(views)]
    holders = [w["device_name"] or "a worker" for w in entries if w["state"] in _HELD_STATES]
    if holders:
        raise HTTPException(status_code=409, detail=f"{', '.join(holders)} still has this project. "
                                                    "Remove it from Workers first.")
    unread = [w["device_name"] or "a worker" for w in entries if w["state"] in _MAYBE_HELD_STATES]
    if unread:
        raise HTTPException(status_code=409, detail=f"Couldn't check whether {', '.join(unread)} still has "
                                                    "this project. Try again in a moment.")
    await run_sync(projects_v2.set_forgotten, origin, True)
    for install in installs:
        await run_sync(setup_db.delete_requests_for, pv2["id"], install["id"])
    setup._after_local_registration_change()
    return {"ok": True, "project_v2_id": pv2["id"]}


@router.delete("/workers/{install_id}")
async def remove_from_worker(
    request: Request,
    install_id: str,
    project_id: Optional[str] = Query(None),
    project_v2_id: Optional[str] = Query(None),
    token: str = Depends(require_admin),
) -> Dict[str, Any]:
    """Remove one worker's record of the project (its files stay)."""
    from app.db import project_setup as setup_db
    from app.network import control, engine

    access = await run_sync(require_origin_access, request=request, project_id=project_id,
                            project_v2_id=project_v2_id)
    pv2 = access["project_v2"]
    if not pv2:
        raise HTTPException(status_code=404, detail="Project not found")
    installs = await run_sync(_approved_workers)
    target = _find_install(installs, install_id)
    target_id = target["id"]
    if target_id == _self_install_id():
        try:
            result = await setup.remove_local(pv2["id"])
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        setup._after_local_registration_change()
    elif engine.context() is None:
        raise HTTPException(status_code=409, detail=f"Remove it on {target.get('device_name') or 'that worker'}")
    elif not target.get("online"):
        raise HTTPException(status_code=409, detail={"error_code": setup.OFFLINE,
                                                     "error": f"{target.get('device_name') or 'That worker'} is offline"})
    else:
        try:
            result = await control.call(target_id, setup.ACTION, {"action": "remove", "project_v2_id": pv2["id"]},
                                        timeout=setup.PEER_TIMEOUT_SECONDS)
        except control.ControlRefused as exc:
            if setup._UNKNOWN_ACTION in str(exc):
                raise HTTPException(status_code=409, detail={
                    "error_code": setup.UNSUPPORTED,
                    "error": setup.unsupported_message(target.get("device_name"))}) from exc
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except control.RunnerUnreachable as exc:
            raise HTTPException(status_code=409, detail={"error_code": setup.OFFLINE, "error": str(exc)}) from exc
        await run_sync(setup_db.forget_peer_project, target_id, pv2["id"])
        setup.emit_changed()
    await run_sync(setup_db.delete_requests_for, pv2["id"], target_id)
    # ``kept``: rows another worker claimed during the removal (left as they are).
    return {"ok": True, "install_id": target_id, "removed": result.get("removed", []),
            "kept": result.get("kept", [])}
