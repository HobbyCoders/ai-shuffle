"""
Cluster API — device registry + cluster-secret management on a shared database.

These endpoints power the **Devices UI** (``require_auth`` — the normal admin/API
user): list installs, rename/delete a device, inspect/rotate the cluster secret,
and manage the address this install advertises to peers.

History and live session state live in the shared DB; the legacy peer relay
endpoints (send/interrupt/permission/question/intake/upload) were removed with
the V2 placement cutover — cross-runner execution is now the ``runs`` lease
claim loop and durable ``pending_interactions``, not HTTP relay.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel

from app.db import database
from app.db.async_database import run_sync
from app.api.auth import require_auth
from app.api.common import require_origin_access
from app.core import cluster

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/cluster", tags=["Cluster"])


# ============================================================================
# Admin endpoints (normal auth) — power the Devices UI
# ============================================================================

class InstallRenameRequest(BaseModel):
    device_name: str


class InstallBaseUrlRequest(BaseModel):
    base_url: Optional[str] = None


@router.get("/installs")
async def list_installs(token: str = Depends(require_auth)) -> Dict[str, Any]:
    """List every registered install, annotating which row is this one."""
    from app.core.config import settings

    installs = await run_sync(database.get_installs)
    me = settings.effective_instance_id
    for row in installs:
        row["is_self"] = row.get("id") == me
    return {"installs": installs, "self_instance_id": me}


def _iso(value: Any) -> Optional[str]:
    """Timestamps arrive as ``str`` from the SQLite shim but ``datetime`` from
    real Postgres — coerce to an ISO string either way."""
    if value is None or isinstance(value, str):
        return value
    return value.isoformat()


@router.get("/git-state")
async def git_state(
    request: Request,
    project_id: Optional[str] = Query(None, description="Legacy project id"),
    project_v2_id: Optional[str] = Query(None, description="Origin-keyed project_v2 id"),
    token: str = Depends(require_auth),
) -> Dict[str, Any]:
    """Per-device git state for one project — the read side of D6 (W3-1).

    Answers, for every registered install, "can this device run a worktree
    session for this project, and what does it already have checked out?".
    ``eligible`` is ``checkouts.has_active_base_checkout`` — the SAME predicate
    the with-worktree remote preflight enforces, so a device the composer shows
    as eligible is exactly a device whose send will not 409 (W3-2).

    Pass exactly one of ``project_id`` or ``project_v2_id`` (400 if both or
    neither). ``project_v2_id`` does not 404 just because this install has no
    legacy ``projects`` row — remote-only compose reads git-state by v2 id.

    A legacy project with no ``projects_v2`` row (a non-git project) is not an
    error: it answers 200 with ``project_v2_id: null`` and every device ineligible.
    """
    from app.core.config import settings
    from app.db import checkouts as checkouts_db
    from app.db import git_ops as git_ops_db

    access = await run_sync(
        require_origin_access,
        request=request,
        project_id=project_id,
        project_v2_id=project_v2_id,
    )
    pv2 = access["project_v2"]
    pv2_id = pv2["id"] if pv2 else None

    installs = await run_sync(database.get_installs)
    me = settings.effective_instance_id

    if pv2_id:
        rows = await run_sync(checkouts_db.list_checkouts, pv2_id)
        recent = await run_sync(git_ops_db.list_recent_for_project, pv2_id, 20)
    else:
        rows, recent = [], []

    # ONE list_checkouts read, grouped in Python — no per-install query fan-out.
    by_runner: Dict[str, list] = {}
    for row in rows:
        by_runner.setdefault(row.get("runner_id") or "", []).append({
            "branch": row.get("branch"),
            "path": row.get("path"),
            "head_sha": row.get("head_sha"),
            "dirty": row.get("dirty"),
            "status": row.get("status"),
            "updated_at": _iso(row.get("updated_at")),
        })

    out = []
    for install in installs:
        install_id = install.get("id")
        entry = dict(install)
        entry["is_self"] = install_id == me
        entry["eligible"] = bool(
            pv2_id
            and await run_sync(
                checkouts_db.has_active_base_checkout, pv2_id, install_id
            )
        )
        entry["checkouts"] = by_runner.get(install_id or "", [])
        out.append(entry)

    return {
        "project_v2_id": pv2_id,
        "installs": out,
        "recent_ops": [
            {
                "id": op.get("id"),
                "runner_id": op.get("runner_id"),
                "op": op.get("op"),
                "status": op.get("status"),
                "params": op.get("params"),
                "result": op.get("result"),
                "created_at": _iso(op.get("created_at")),
                "updated_at": _iso(op.get("updated_at")),
            }
            for op in recent
        ],
    }


@router.patch("/installs/{install_id}")
async def update_install(
    install_id: str,
    rename: Optional[InstallRenameRequest] = None,
    token: str = Depends(require_auth),
) -> Dict[str, Any]:
    """Rename a device (the user-friendly name shown on its sessions)."""
    if not rename or not (rename.device_name or "").strip():
        raise HTTPException(status_code=400, detail="device_name is required")
    updated = await run_sync(database.set_install_name, install_id, rename.device_name.strip())
    if not updated:
        raise HTTPException(status_code=404, detail="Install not found")
    return updated


@router.delete("/installs/{install_id}")
async def delete_install(install_id: str, token: str = Depends(require_auth)) -> Dict[str, Any]:
    """Remove a stale install from the registry.

    Deleting this install's own row is allowed but pointless — it re-registers
    on its next heartbeat. Mainly for pruning devices that are gone for good.
    """
    ok = await run_sync(database.delete_install, install_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Install not found")
    # Prune the departed install's usage-snapshot row too — otherwise it lingers
    # in the shared table forever (read-time filtered only). Best-effort.
    try:
        await run_sync(database.delete_usage_snapshot, install_id)
    except Exception as e:  # noqa: BLE001 — cleanup must not fail the delete
        logger.debug("Usage-snapshot prune skipped for %s: %s", install_id, e)
    return {"ok": True, "deleted": install_id}


@router.get("/advertise")
async def get_advertise_url(token: str = Depends(require_auth)) -> Dict[str, Any]:
    """Report this install's advertised peer address (and override status)."""
    return await run_sync(cluster.advertise_status)


@router.put("/advertise")
async def set_advertise_url(
    body: InstallBaseUrlRequest,
    token: str = Depends(require_auth),
) -> Dict[str, Any]:
    """Override the URL this install advertises to peers (blank = auto-detect).

    Useful when the auto-detected host name isn't reachable from other devices
    (reverse proxy, custom domain). Takes effect immediately (re-registers).
    """
    url = (body.base_url or "").strip()
    if url and not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail="URL must start with http:// or https://")
    return await run_sync(cluster.set_advertise_url_override, url)


@router.post("/advertise/test")
async def test_advertise_reachability(token: str = Depends(require_auth)) -> Dict[str, Any]:
    """Self-test the currently advertised address by fetching its health route.

    Catches the most common misconfigurations — an unresolvable host name
    (``[Errno -2]``), a wrong port, or a down listener — before peers hit them as
    relay failures. This runs from *this* server's network namespace, so it can't
    prove cross-host reachability through NAT/firewalls, only that the URL itself
    resolves and answers. The response says as much.
    """
    import httpx

    url, source = await run_sync(cluster.advertise_url)
    if not url:
        return {
            "ok": False,
            "url": None,
            "source": source,
            "detail": "No address is advertised — this install is view-only to peers.",
        }

    health = f"{url.rstrip('/')}/api/v1/health"
    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
            resp = await client.get(health)
        if resp.status_code < 400:
            return {
                "ok": True,
                "url": url,
                "source": source,
                "detail": "Address resolves and responds. Peers on the same network should be able to reach it.",
            }
        return {
            "ok": False,
            "url": url,
            "source": source,
            "detail": f"Address responded with HTTP {resp.status_code} — check the URL points at this install.",
        }
    except httpx.HTTPError as e:
        return {
            "ok": False,
            "url": url,
            "source": source,
            "detail": f"Could not reach the advertised address: {e}",
        }


@router.get("/secret/status")
async def cluster_secret_status(token: str = Depends(require_auth)) -> Dict[str, Any]:
    """Report how the cluster secret is sourced — never returns the secret itself."""
    from app.core.config import settings

    env_pinned = bool(settings.cluster_secret and settings.cluster_secret.strip())
    # Touch the secret so a fresh install reports "configured" (auto-seeds on read).
    configured = bool(await run_sync(cluster.get_cluster_secret))
    return {"configured": configured, "env_pinned": env_pinned}


@router.post("/secret/regenerate")
async def regenerate_cluster_secret(token: str = Depends(require_auth)) -> Dict[str, Any]:
    """Rotate the auto-generated cluster secret (no-op when env-pinned).

    Rotating invalidates the old secret cluster-wide: every other install must be
    restarted (or re-read the setting) before it can relay again.
    """
    from app.core.config import settings

    env_pinned = bool(settings.cluster_secret and settings.cluster_secret.strip())
    await run_sync(cluster.regenerate_cluster_secret)
    return {"ok": True, "env_pinned": env_pinned}
