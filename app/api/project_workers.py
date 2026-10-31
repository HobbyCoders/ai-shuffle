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
        listed = any(w["state"] in _LISTED_STATES for w in workers) or any(
            w["state"] == "unknown" and pv2["id"] in offline_has.get(w["install_id"], set()) for w in workers)
        if not listed:
            continue
        info = _project_info(pv2, snapshot, views)
        projects.append({
            "project_v2_id": pv2["id"],
            "local_project_id": info["local_project_id"],
            "name": pv2.get("name") or origin,
            "kind": info["kind"],
            "remote": info["remote"],
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
        entry.update({
            "folder": detail.get("folder") if sees_paths and has_copy else None,
            "suggested_folder": (_join_folder(payload.get("workspace"), info["default_folder"])
                                 if sees_paths and not has_copy else None),
            "branch": detail.get("branch") if has_copy else None,
            "dirty": detail.get("dirty") if has_copy else None,
            "changed_files": detail.get("changed_files") if has_copy else None,
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
        "kind": info["kind"], "clone_url": info["clone_url"], "folder": folder, "mode": body.mode,
    }


def _shared_db_enqueue(target_id: str, spec: Dict[str, Any], op_id: str) -> Dict[str, Any]:
    """Shared database, no network: queue the op for the target's own claim pass."""
    from app.db import git_ops as git_ops_db

    for row in git_ops_db.list_ops_for_runner(target_id, setup.SETUP_OP, limit=50):
        if row.get("project_v2_id") == spec["project_v2_id"] and row["status"] in ("queued", "running"):
            return row
    params = {key: spec.get(key) for key in ("origin", "name", "kind", "mode", "clone_url", "folder")}
    params.update(path=None, requested_by=None)
    return git_ops_db.insert_op(target_id, setup.SETUP_OP, project_v2_id=spec["project_v2_id"],
                                params=params, op_id=op_id)


@router.post("/setup", status_code=status.HTTP_202_ACCEPTED)
async def setup_project(request: Request, body: SetupRequest,
                        token: str = Depends(require_admin)) -> Dict[str, Any]:
    """Set a project up on a worker: clone it there, adopt a same-repo folder, or
    create its (empty) folder. Idempotent while one is in flight."""
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
    if spec["kind"] == "git" and not spec["clone_url"]:
        raise HTTPException(status_code=409, detail=(
            "Can't find this project's Git remote. Bring a worker that has the project online and try again."))
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
            result = await run_sync(setup.remove_local, pv2["id"])
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        setup._after_local_remove()
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
    return {"ok": True, "install_id": target_id, "removed": result.get("removed", [])}
