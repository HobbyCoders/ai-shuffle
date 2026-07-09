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
from typing import Any, Dict, List, Literal, Optional

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

    Keyed by the *shared* session id and runs as admin (the originating install
    already authenticated its own user).
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

    # 'claude-code' is the controller's "no explicit profile" sentinel, not a
    # real profile row — treat it as absent so the session's own profile wins
    # (stream_to_websocket would error-yield "Profile not found" on it, which
    # the headless path has no surface for).
    body_profile = body.profile_id if body.profile_id != "claude-code" else None
    profile_id = body_profile or session.get("profile_id") or "claude-code"
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


class ClusterQueueMessageRequest(BaseModel):
    """A peer install relaying a "send later" queued message for a session we own."""
    content: str
    profile_id: Optional[str] = None
    project_id: Optional[str] = None


@router.post("/sessions/{session_id}/queue_message")
async def cluster_queue_message(
    session_id: str,
    body: ClusterQueueMessageRequest,
    install_id: str = Depends(cluster.require_cluster_auth),
):
    """Queue a "send later" message on a session this install owns (peer relay).

    Reuses the same server-side queue handler the local ``/ws/chat`` path uses:
    the message is stored in the shared queue, broadcast to devices, and — if
    the session is idle here — drained immediately by running the query in this
    install's worktree. If a query is already streaming, the post-query drain
    picks it up. Keyed by the *shared* session id and runs as admin (the
    originating install already authenticated its own user).
    """
    from app.core.headless_query_runner import HeadlessChatHandler

    content = (body.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="Empty queue content")

    session = await run_sync(database.get_session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    handler = HeadlessChatHandler(user_id="admin", api_user_id=None)
    handler.current_session_id = session_id
    try:
        await handler.handle_queue_message({
            "session_id": session_id,
            "content": content,
            # Same sentinel handling as cluster_message: the queued row's
            # profile_id drives the eventual drain, so it must be real.
            "profile": (body.profile_id if body.profile_id != "claude-code" else None)
                       or session.get("profile_id"),
            "project": body.project_id or session.get("project_id"),
        })
    except Exception as e:  # noqa: BLE001
        logger.exception("Cluster queue_message failed: %s", e)
        raise HTTPException(status_code=500, detail="Failed to queue message")

    logger.info("Cluster queue_message from install %s on session %s", install_id or "?", session_id)
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


@router.get("/sessions/{session_id}/pending_questions")
async def cluster_pending_questions(
    session_id: str,
    install_id: str = Depends(cluster.require_cluster_auth),
):
    """Return the pending AskUserQuestion requests for a session this install owns.

    A pending question lives ONLY in the owning install's in-memory
    ``user_question_handler`` — a viewer install's local handler is always empty
    for a remote session. A controlling install relays ``get_pending_questions``
    here so a user driving the session from another install can still see (and
    then answer) the prompt. Keyed by the *shared* session id.
    """
    from app.core.user_question_handler import user_question_handler

    session = await run_sync(database.get_session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    return {
        "session_id": session_id,
        "questions": user_question_handler.get_pending_questions(session_id),
    }


class ClusterQuestionResponseRequest(BaseModel):
    """A peer install relaying a user's AskUserQuestion answer for a session we own."""
    request_id: str
    answers: Dict[str, Any]


@router.post("/sessions/{session_id}/question_response")
async def cluster_question_response(
    session_id: str,
    body: ClusterQuestionResponseRequest,
    install_id: str = Depends(cluster.require_cluster_auth),
):
    """Deliver a user's AskUserQuestion answer to a session this install owns.

    The pending question and the coroutine awaiting it live here (the owner); the
    answer arrived on a device connected to a *different* install, which relays it
    to us. Resolving it unblocks the owner's ``request_answers`` waiter. Keyed by
    the *shared* session id.
    """
    from app.core.user_question_handler import user_question_handler

    session = await run_sync(database.get_session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    ok = await user_question_handler.respond(
        request_id=body.request_id,
        session_id=session_id,
        answers=body.answers,
    )
    logger.info(
        "Cluster question_response from install %s on session %s -> %s",
        install_id or "?", session_id, ok,
    )
    return {"ok": ok, "session_id": session_id, "request_id": body.request_id}


@router.get("/sessions/{session_id}/pending_permissions")
async def cluster_pending_permissions(
    session_id: str,
    install_id: str = Depends(cluster.require_cluster_auth),
):
    """Return the pending + deferred tool-permission requests for a session we own.

    A pending permission request lives ONLY in the owning install's in-memory
    ``permission_handler`` — a viewer install's local handler is always empty for
    a remote session. A controlling install relays ``get_pending_permissions``
    here so a user driving the session from another install can still see (and
    then answer) the approval prompt. Keyed by the *shared* session id.
    """
    from app.core.permission_handler import permission_handler

    session = await run_sync(database.get_session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    return {
        "session_id": session_id,
        "pending": permission_handler.get_pending_requests(session_id),
        "deferred": permission_handler.get_deferred_requests(session_id),
    }


class ClusterPermissionResponseRequest(BaseModel):
    """A peer install relaying a user's tool-permission decision for a session we own."""
    request_id: str
    decision: Literal["allow", "deny", "defer"]
    remember: Optional[Literal["none", "session", "profile"]] = None  # ignored for defer
    pattern: Optional[str] = None  # optional rule pattern (ignored for defer)


@router.post("/sessions/{session_id}/permission_response")
async def cluster_permission_response(
    session_id: str,
    body: ClusterPermissionResponseRequest,
    install_id: str = Depends(cluster.require_cluster_auth),
):
    """Deliver a user's tool-permission decision to a session this install owns.

    The pending permission request and the coroutine awaiting it (inside
    ``permission_handler.request_permission``) live here (the owner); the decision
    arrived on a device connected to a *different* install, which relays it to us.
    Resolving it unblocks the owner's waiter (else it sits until the 300s
    auto-deny). Keyed by the *shared* session id.
    """
    from app.core.permission_handler import permission_handler

    session = await run_sync(database.get_session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Mirror the WS handler: remember/pattern are meaningless for a defer.
    remember = body.remember if body.decision != "defer" else None
    pattern = body.pattern if body.decision != "defer" else None

    result = await permission_handler.respond(
        request_id=body.request_id,
        session_id=session_id,
        decision=body.decision,
        remember=remember,
        pattern=pattern,
        broadcast_func=None,
    )
    # respond() returns {"error": ...} when the request is not pending (already
    # resolved / timed out), else a {"resolved": True, ...} dict. Collapse to a
    # single ok verdict so the caller can tell a landed decision from a stale one.
    ok = isinstance(result, dict) and not result.get("error")
    logger.info(
        "Cluster permission_response from install %s on session %s -> %s",
        install_id or "?", session_id, ok,
    )
    return {"ok": ok, "session_id": session_id, "request_id": body.request_id}


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


class ClusterStreamingRequest(BaseModel):
    """A peer install announcing a streaming-state transition for a session it owns.

    The owning install fans these out on every local stream start/end so peer
    installs can update their own connected devices' streaming indicators.
    """
    user_id: str
    session_id: str
    is_streaming: bool
    instance_id: Optional[str] = None  # owning install id (for offline cleanup)
    project_id: Optional[str] = None
    worktree_id: Optional[str] = None


@router.post("/streaming")
async def cluster_streaming(
    body: ClusterStreamingRequest,
    install_id: str = Depends(cluster.require_cluster_auth),
) -> Dict[str, Any]:
    """Apply a peer install's streaming transition to this install's devices.

    Records the remote session's streaming state locally and re-broadcasts
    ``user_streaming_changed`` to this install's connected devices. Never
    re-fans-out, so a transition cannot loop between installs.
    """
    from app.core.sync_engine import sync_engine

    await sync_engine.apply_remote_streaming_change(
        user_id=body.user_id,
        session_id=body.session_id,
        is_streaming=body.is_streaming,
        owner_instance_id=body.instance_id or install_id or None,
        project_id=body.project_id,
        worktree_id=body.worktree_id,
    )
    return {"ok": True}


@router.post("/intake", response_model=CloudIntakeResponse)
async def cluster_intake_endpoint(
    body: CloudIntakeRequest,
    install_id: str = Depends(cluster.require_cluster_auth),
):
    """Offload a new session onto this install (registry-targeted offload).

    Runs the shared worker logic (``run_cloud_intake``), authenticated by the
    shared cluster secret. Runs as admin; the resulting session is owned by this
    install and visible/controllable cluster-wide.
    """
    return await run_cloud_intake(body, api_user_id=None)


class ClusterGitStateRequest(BaseModel):
    """A peer asking for this install's git state for a repo (branch picker)."""
    project_id: str
    repo_url: str
    github_repo_name: Optional[str] = None
    default_branch: str = "main"


@router.post("/git-state")
async def cluster_git_state(
    body: ClusterGitStateRequest,
    install_id: str = Depends(cluster.require_cluster_auth),
) -> Dict[str, Any]:
    """Report this install's git state for a repo (cluster-secret authed).

    Powers the cloud branch picker: a primary install previews the *worker's*
    branches and currently checked-out branch so the user can pick from what the
    remote actually has (no-worktree cloud = local on this device). Read-only —
    resolves the project without cloning. If the repo isn't present here yet the
    worker will clone it on the first intake, so we report ``present: false`` and
    the primary offers just the default branch.
    """
    from app.api.cloud import _find_local_project_for_repo
    from app.core.project_utils import get_project_working_dir
    from app.core.git_service import git_service

    def _collect() -> Dict[str, Any]:
        project = _find_local_project_for_repo(body.project_id, body.repo_url)
        if not project:
            return {"present": False, "default_branch": body.default_branch, "branches": [], "current_branch": None}
        working_dir = get_project_working_dir(project)
        return {
            "present": True,
            "default_branch": body.default_branch,
            # include_remote=True mirrors the local branch selector (the default
            # of the /branches endpoint) so the picker shows the same branches the
            # remote device itself would offer.
            "branches": git_service.list_branches(working_dir, include_remote=True),
            "current_branch": git_service.get_current_branch(working_dir),
        }

    return await run_sync(_collect)


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
