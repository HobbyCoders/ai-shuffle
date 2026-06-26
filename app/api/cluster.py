"""
Cluster API — cross-install session control on a shared database.

Two audiences share the ``/api/v1/cluster`` prefix:

- **Peer endpoints** (``require_cluster_auth`` — the shared cluster secret):
  install-to-install control for a session running on *this* install. The
  caller is another install relaying live send/interrupt/stream because the user
  is driving the session from a different device. Keyed by the shared session id.
- **Admin endpoints** (``require_auth`` — the normal admin/API user): power the
  Devices UI — list installs, rename/delete a device, inspect/rotate the cluster
  secret.

History never comes through here (it lives in the shared DB); only the live
control plane does. ``/sessions/{id}/live`` deliberately serves **local** state
only — it is invoked on the owning install, so it must not re-proxy and create a
relay chain.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from pydantic import BaseModel

from app.db import database
from app.db.async_database import run_sync
from app.api.auth import require_auth
from app.api.cloud import CloudIntakeRequest, CloudIntakeResponse, run_cloud_intake
from app.core import cluster

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/cluster", tags=["Cluster"])


# ============================================================================
# Peer control endpoints (shared cluster secret)
# ============================================================================

class ClusterMessageRequest(BaseModel):
    """A peer install relaying a follow-up prompt for a session we own."""
    prompt: str
    profile_id: Optional[str] = None
    overrides: Optional[Dict[str, Any]] = None


@router.post("/sessions/{session_id}/message")
async def cluster_message(
    session_id: str,
    body: ClusterMessageRequest,
    install_id: str = Depends(cluster.require_cluster_auth),
):
    """Run a follow-up prompt on a session this install owns (peer relay).

    Mirrors ``cloud_intake_message`` but is keyed by the *shared* session id and
    runs as admin (the originating install already authenticated its own user).
    """
    from app.api.sessions_worktree import _detect_command_meta
    from app.core.headless_query_runner import launch_headless_query
    from app.core.sync_engine import sync_engine

    prompt = (body.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Empty prompt")

    session = await run_sync(database.get_session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    profile_id = body.profile_id or session.get("profile_id") or "claude-code"
    msg_role, msg_meta = _detect_command_meta(prompt)

    await run_sync(
        database.add_session_message,
        session_id=session_id,
        role=msg_role,
        content=prompt,
        metadata=msg_meta,
        profile_id=profile_id,
    )
    try:
        await sync_engine.broadcast_message_added(
            session_id=session_id,
            message={"role": "user", "content": prompt, "profile_id": profile_id},
            source_device_id=None,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Cluster message broadcast failed: %s", e)

    try:
        await launch_headless_query(
            prompt=prompt,
            session_id=session_id,
            profile_id=profile_id,
            project_id=session.get("project_id"),
            user_id="admin",
            api_user_id=None,
            worktree_id=session.get("worktree_id"),
            overrides=body.overrides,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("Cluster follow-up query failed: %s", e)
        raise HTTPException(status_code=500, detail="Failed to start query")

    logger.info("Cluster message from install %s ran on session %s", install_id or "?", session_id)
    return {"ok": True, "session_id": session_id}


@router.post("/sessions/{session_id}/interrupt")
async def cluster_interrupt(
    session_id: str,
    install_id: str = Depends(cluster.require_cluster_auth),
):
    """Interrupt a running query on a session this install owns (peer relay)."""
    from app.core.query_engine import interrupt_session
    from app.api.websocket import _active_chat_sessions, _active_sessions_lock, _stopped_sessions

    session = await run_sync(database.get_session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    _stopped_sessions.add(session_id)
    interrupted = await interrupt_session(session_id)

    # Force-cancel the task if the SDK doesn't stop quickly (mirrors cloud intake).
    async with _active_sessions_lock:
        task = _active_chat_sessions.get(session_id)
    if task and not task.done():
        import asyncio

        async def _safety_cancel(t, timeout=10.0):
            await asyncio.sleep(timeout)
            if not t.done():
                t.cancel()

        asyncio.ensure_future(_safety_cancel(task))

    return {"ok": True, "interrupted": interrupted}


@router.get("/sessions/{session_id}/live")
async def cluster_live(
    request: Request,
    session_id: str,
    since_update_id: int = Query(0),
    install_id: str = Depends(cluster.require_cluster_auth),
):
    """Serve LOCAL live state for a session this install owns (peer relay).

    Delegates to the normal live endpoint, which on the owning install naturally
    skips every proxy branch (the session is local here, instance_id == us), so
    we serve in-memory/DB state directly and never chain to another install.
    """
    from app.api.sessions import get_live_session

    return await get_live_session(
        request=request,
        session_id=session_id,
        since_update_id=since_update_id,
        token="cluster",
    )


class ClusterMobileNotifyRequest(BaseModel):
    """A peer install relaying a mobile notification for one of its events."""
    title: str = ""
    body: str = ""
    data: Optional[Dict[str, Any]] = None


@router.post("/mobile-notify")
async def cluster_mobile_notify(
    body: ClusterMobileNotifyRequest,
    install_id: str = Depends(cluster.require_cluster_auth),
):
    """Receive a mobile notification fanned out from a peer install.

    A session event (question / permission / "done") fired on the *owning*
    install, which fanned it out to every online peer. This install delivers it
    to its OWN connected phones only — local SSE streams + registered push
    transports — and deliberately never re-fans-out, so notifications cannot
    loop between installs.
    """
    from app.services.push_notification import deliver_local_notification

    await deliver_local_notification(
        title=body.title or "AI Shuffle",
        body=body.body or "",
        data=body.data or {},
    )
    logger.debug("Delivered relayed mobile-notify from install %s", install_id or "?")
    return {"ok": True}


@router.post("/intake", response_model=CloudIntakeResponse)
async def cluster_intake_endpoint(
    body: CloudIntakeRequest,
    install_id: str = Depends(cluster.require_cluster_auth),
):
    """Offload a new session onto this install (registry-targeted offload).

    Same worker logic as the legacy ``/api/v1/cloud/intake`` but authenticated by
    the shared cluster secret instead of an API key. Runs as admin; the resulting
    session is owned by this install and visible/controllable cluster-wide.
    """
    return await run_cloud_intake(body, api_user_id=None)


@router.post("/upload")
async def cluster_upload(
    file: UploadFile = File(...),
    path: str = Form(default=""),
    install_id: str = Depends(cluster.require_cluster_auth),
):
    """Receive an attachment for a cluster offload (cluster-secret authed).

    Reuses the workspace upload save path so the file lands where intake's
    ``pending_upload_paths`` expects it.
    """
    from app.api.workspace_files import upload_file as _ws_upload

    return await _ws_upload(file=file, path=path, token="cluster")


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
