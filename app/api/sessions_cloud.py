"""
Cloud session creation — the *primary* side of cloud offload.

``POST /api/v1/sessions/cloud`` creates a local "proxy" session whose real work
runs on the configured second install (see ``app.core.cloud_relay`` and
``app.api.cloud``). The local session records the remote install URL + remote
session id; the rest of the app (live polling, follow-up messages, interrupt)
transparently forwards to the worker, so the user sees and drives it like any
normal session.

Cloud mode only works for projects backed by a GitHub repository — the worker
needs a clone URL to reproduce the project in its own ``/workspace``.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from app.db import database
from app.db.async_database import run_sync
from app.api.auth import require_auth, get_api_user_from_request

logger = logging.getLogger(__name__)

# Sub-router — included by the main sessions router (no prefix needed).
cloud_router = APIRouter(tags=["Sessions"])


class CreateCloudSessionRequest(BaseModel):
    project_id: str
    prompt: str
    profile_id: Optional[str] = None
    branch_name: Optional[str] = None
    base_branch: Optional[str] = None
    pending_upload_paths: Optional[List[str]] = None
    overrides: Optional[Dict[str, Any]] = None
    # Registry-targeted offload: run on this registered install instead of the
    # single manually-configured second install. When set, the result is an
    # ordinary cross-install session (no local proxy row, no cloud_remote_*).
    target_instance_id: Optional[str] = None


class CloudSessionResponse(BaseModel):
    session_id: str
    remote_session_id: str
    remote_url: str
    project_id: str
    status: str = "active"
    query_started: bool = False


def _resolve_github_repo(project_id: str) -> tuple[str, Optional[str], str]:
    """Return ``(clone_url, github_repo_name, default_branch)`` for a project.

    Raises ``HTTPException`` if the project has no GitHub remote configured.
    """
    repo = database.get_git_repository_by_project(project_id)
    remote_url = (repo or {}).get("remote_url") if repo else None
    github_repo_name = (repo or {}).get("github_repo_name") if repo else None
    default_branch = (repo or {}).get("default_branch") if repo else None

    is_github = bool(github_repo_name) or (remote_url and "github.com" in remote_url)
    if not remote_url or not is_github:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Cloud mode requires a GitHub repository. Connect this project "
                "to a GitHub remote first."
            ),
        )
    return remote_url, github_repo_name, default_branch or "main"


@cloud_router.post("/cloud", response_model=CloudSessionResponse)
async def create_cloud_session(
    request: Request,
    body: CreateCloudSessionRequest,
    token: str = Depends(require_auth),
):
    """Offload a new session to a registered install (or the legacy second install)."""
    from app.core import cloud_relay
    from app.core.sync_engine import sync_engine

    prompt = (body.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Empty prompt")

    # Preferred path: offload onto a registered install chosen from the device
    # picker. The session is owned by that install and shared cluster-wide.
    if body.target_instance_id:
        return await _create_offload_via_registry(request, body, prompt)

    if not cloud_relay.is_configured():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Cloud offload is not configured. Set the second install URL and "
                "API key in Settings → System → Cloud Offload."
            ),
        )

    # Verify the project exists locally and is a GitHub repo.
    project = await run_sync(database.get_project, body.project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project not found: {body.project_id}")

    api_user = get_api_user_from_request(request)
    api_user_id: Optional[str] = api_user["id"] if api_user else None
    if api_user and api_user.get("project_id") and api_user["project_id"] != body.project_id:
        raise HTTPException(status_code=403, detail="Access denied to this project")

    clone_url, github_repo_name, default_branch = await run_sync(
        _resolve_github_repo, body.project_id
    )

    # Forward any locally-uploaded attachments to the worker's workspace.
    remote_upload_paths: List[str] = []
    for path in body.pending_upload_paths or []:
        try:
            remote = await cloud_relay.upload_file(path)
            if remote:
                remote_upload_paths.append(remote)
        except Exception as e:  # noqa: BLE001
            logger.warning("Cloud attachment upload failed for %s: %s", path, e)

    # Hand off to the worker (creates/links project, worktree, starts query).
    try:
        intake = await cloud_relay.intake(
            project_id=body.project_id,
            repo_url=clone_url,
            github_repo_name=github_repo_name,
            default_branch=default_branch,
            prompt=prompt,
            profile_id=body.profile_id,
            branch_name=body.branch_name,
            base_branch=body.base_branch,
            pending_upload_paths=remote_upload_paths,
            overrides=body.overrides,
        )
    except cloud_relay.CloudNotConfigured as e:
        raise HTTPException(status_code=400, detail=str(e))
    except cloud_relay.CloudRelayError as e:
        raise HTTPException(status_code=502, detail=str(e))

    remote_session_id = intake.get("session_id")
    if not remote_session_id:
        raise HTTPException(status_code=502, detail="Second install did not return a session id")

    base_url, _ = cloud_relay.get_cloud_config()

    # Create the local proxy session.
    session_id = str(uuid.uuid4())
    title = prompt[:100].strip()
    if len(prompt) > 100:
        title = title.rsplit(" ", 1)[0] + "..."
    profile_id = body.profile_id or "claude-code"

    await run_sync(
        database.create_session,
        session_id=session_id,
        profile_id=profile_id,
        project_id=body.project_id,
        api_user_id=api_user_id,
        title=title,
        cloud_remote_url=base_url,
        cloud_remote_session_id=remote_session_id,
    )
    # Keep a local copy of the prompt so search / sidebar previews work even
    # though the canonical transcript lives on (and is mirrored from) the worker.
    await run_sync(
        database.add_session_message,
        session_id=session_id,
        role="user",
        content=prompt,
        profile_id=profile_id,
    )

    try:
        await sync_engine.broadcast_user_session_created(
            user_id="admin", session_id=session_id, source_device_id=None
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Cloud session broadcast failed: %s", e)

    return CloudSessionResponse(
        session_id=session_id,
        remote_session_id=remote_session_id,
        remote_url=base_url,
        project_id=body.project_id,
        status="active",
        query_started=bool(intake.get("query_started")),
    )


async def _create_offload_via_registry(
    request: Request,
    body: CreateCloudSessionRequest,
    prompt: str,
) -> CloudSessionResponse:
    """Offload onto a registered install resolved from the registry.

    Unlike the legacy path this creates NO local proxy row: on a shared database
    the worker's session is already visible everywhere, and its ``instance_id``
    marks the owning device so the cross-install relay (live/send/interrupt) can
    reach it. We just kick off the work on the target and return its session id.
    """
    from app.core import cloud_relay

    # Verify the project exists locally and the caller may access it.
    project = await run_sync(database.get_project, body.project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project not found: {body.project_id}")

    api_user = get_api_user_from_request(request)
    if api_user and api_user.get("project_id") and api_user["project_id"] != body.project_id:
        raise HTTPException(status_code=403, detail="Access denied to this project")

    # Resolve the chosen device's reachable URL (online + advertises a base_url).
    try:
        base_url = await run_sync(
            cloud_relay.resolve_install_base_url, body.target_instance_id
        )
    except cloud_relay.InstallOffline as e:
        raise HTTPException(status_code=409, detail=str(e))

    clone_url, github_repo_name, default_branch = await run_sync(
        _resolve_github_repo, body.project_id
    )

    # Forward any locally-uploaded attachments to the target's workspace.
    remote_upload_paths: List[str] = []
    for path in body.pending_upload_paths or []:
        try:
            remote = await cloud_relay.cluster_upload_file(base_url, path)
            if remote:
                remote_upload_paths.append(remote)
        except Exception as e:  # noqa: BLE001
            logger.warning("Offload attachment upload failed for %s: %s", path, e)

    # Hand off to the target (creates/links project, worktree, starts query).
    try:
        intake = await cloud_relay.cluster_intake(
            base_url,
            project_id=body.project_id,
            repo_url=clone_url,
            github_repo_name=github_repo_name,
            default_branch=default_branch,
            prompt=prompt,
            profile_id=body.profile_id,
            branch_name=body.branch_name,
            base_branch=body.base_branch,
            pending_upload_paths=remote_upload_paths,
            overrides=body.overrides,
        )
    except cloud_relay.InstallOffline as e:
        raise HTTPException(status_code=409, detail=str(e))
    except cloud_relay.CloudRelayError as e:
        raise HTTPException(status_code=502, detail=str(e))

    remote_session_id = intake.get("session_id")
    if not remote_session_id:
        raise HTTPException(status_code=502, detail="Target device did not return a session id")

    # The session id is shared across the cluster — the frontend opens it like
    # any other (remote) session. No proxy row, no cloud_remote_* written.
    return CloudSessionResponse(
        session_id=remote_session_id,
        remote_session_id=remote_session_id,
        remote_url=base_url,
        project_id=body.project_id,
        status="active",
        query_started=bool(intake.get("query_started")),
    )
