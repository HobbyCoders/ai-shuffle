"""
Cloud session creation — offload a new session onto a registered install.

``POST /api/v1/sessions/cloud`` offloads a new session onto a registered install
chosen from the device picker (``target_instance_id``). On a shared database the
worker's session is visible everywhere and its ``instance_id`` marks the owning
device, so there is no local proxy row — the cross-install relay
(``app.core.cloud_relay``) reaches it for live polling, follow-up messages, and
interrupt. The user sees and drives it like any normal session.

Cloud mode only works for projects backed by a GitHub repository — the worker
needs a clone URL to reproduce the project in its own ``/workspace``.
"""

from __future__ import annotations

import logging
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
    # The registered install to run on, resolved from the device picker. The
    # result is an ordinary cross-install session (no local proxy row). Required.
    target_instance_id: Optional[str] = None
    # "Cloud = Local on another device": when ``use_worktree`` is False the
    # session runs directly in the target's main checkout on ``branch_name``
    # (the worker checks it out / creates it there), mirroring local no-worktree
    # mode. When True the target forks a worktree (auto-named from the prompt if
    # ``branch_name`` is empty). ``create_new_branch`` / ``auto_stash`` are
    # forwarded verbatim to the worker. Defaults preserve the legacy worktree path.
    use_worktree: bool = True
    create_new_branch: bool = False
    auto_stash: bool = False


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


@cloud_router.get("/cloud/git-state")
async def get_cloud_git_state(
    request: Request,
    project_id: str,
    target_instance_id: str,
    token: str = Depends(require_auth),
) -> Dict[str, Any]:
    """Preview a target device's git state for the cloud branch picker.

    Resolves the project's GitHub remote, then asks the chosen install (via the
    cluster relay) which branches it has and what it currently has checked out —
    so the picker can mirror the *remote's* branch selector and default to its
    current branch. Returns ``{present, branches, current_branch, default_branch}``.
    """
    from app.core import cloud_relay

    project = await run_sync(database.get_project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project not found: {project_id}")

    api_user = get_api_user_from_request(request)
    if api_user and api_user.get("project_id") and api_user["project_id"] != project_id:
        raise HTTPException(status_code=403, detail="Access denied to this project")

    clone_url, github_repo_name, default_branch = await run_sync(
        _resolve_github_repo, project_id
    )

    try:
        state = await cloud_relay.relay_git_state(
            target_instance_id,
            {
                "project_id": project_id,
                "repo_url": clone_url,
                "github_repo_name": github_repo_name,
                "default_branch": default_branch,
            },
        )
    except cloud_relay.InstallOffline as e:
        raise HTTPException(status_code=409, detail=str(e))
    except cloud_relay.CloudRelayError as e:
        raise HTTPException(status_code=502, detail=str(e))

    return state


@cloud_router.post("/cloud", response_model=CloudSessionResponse)
async def create_cloud_session(
    request: Request,
    body: CreateCloudSessionRequest,
    token: str = Depends(require_auth),
):
    """Offload a new session to a registered install chosen from the device picker.

    On a shared database the worker's session is visible everywhere, so there is
    no local proxy row — the chosen install owns the session and the cross-install
    relay reaches it. ``target_instance_id`` is required.
    """
    prompt = (body.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Empty prompt")

    if not body.target_instance_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cloud mode requires a target device. Pick one from the device list.",
        )

    return await _create_offload_via_registry(request, body, prompt)


async def _create_offload_via_registry(
    request: Request,
    body: CreateCloudSessionRequest,
    prompt: str,
) -> CloudSessionResponse:
    """Offload onto a registered install resolved from the registry.

    Creates NO local proxy row: on a shared database the worker's session is
    already visible everywhere, and its ``instance_id`` marks the owning device
    so the cross-install relay (live/send/interrupt) can reach it. We just kick
    off the work on the target and return its session id.
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

    # Hand off to the target (creates/links project, then runs in a worktree or
    # directly in the main checkout depending on use_worktree, and starts query).
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
            use_worktree=body.use_worktree,
            create_new_branch=body.create_new_branch,
            auto_stash=body.auto_stash,
        )
    except cloud_relay.InstallOffline as e:
        raise HTTPException(status_code=409, detail=str(e))
    except cloud_relay.CloudRelayError as e:
        # Preserve a worker 409 (dirty checkout on the remote) with its detail so
        # the frontend can offer "Stash & switch" (retry with auto_stash=true).
        # Other peer errors collapse to a generic 502.
        if e.status_code == 409:
            raise HTTPException(status_code=409, detail=e.detail or str(e))
        raise HTTPException(status_code=502, detail=str(e))

    remote_session_id = intake.get("session_id")
    if not remote_session_id:
        raise HTTPException(status_code=502, detail="Target device did not return a session id")

    # The session id is shared across the cluster — the frontend opens it like
    # any other (remote) session. No proxy row written.
    return CloudSessionResponse(
        session_id=remote_session_id,
        remote_session_id=remote_session_id,
        remote_url=base_url,
        project_id=body.project_id,
        status="active",
        query_started=bool(intake.get("query_started")),
    )
