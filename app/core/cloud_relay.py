"""
Cluster relay — registry-based cross-install session control.

On a cluster, every install shares ONE database, so a session's id is identical
on every install and its ``instance_id`` marks the owning device. When a user
drives a session from a device connected to a *different* install, that install
resolves the owning install from the ``installs`` registry and relays live
control (send / interrupt / live-stream) to its ``/api/v1/cluster`` endpoints,
authenticating with the shared cluster secret. History needs no relay — it lives
in the shared DB; only the live control plane does.

This module is the relaying (primary→owner) client. The owner side lives in
``app.api.cluster`` (the ``/api/v1/cluster`` endpoints). Offload of a *new*
session onto a chosen install goes through ``cluster_intake`` here +
``run_cloud_intake`` on the target.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import httpx

from app.db import database

logger = logging.getLogger(__name__)

# Network timeouts (seconds). Intake clones a repo and may take a while; the
# rest are quick control-plane calls.
INTAKE_TIMEOUT = 180.0
CONTROL_TIMEOUT = 30.0
UPLOAD_TIMEOUT = 120.0


class CloudRelayError(Exception):
    """Raised when the owning install returns an error or is unreachable."""


def _raise_for_status(resp: httpx.Response, action: str) -> None:
    if resp.status_code >= 400:
        detail = ""
        try:
            body = resp.json()
            detail = body.get("detail") or body.get("message") or ""
        except Exception:  # noqa: BLE001
            detail = (resp.text or "")[:300]
        raise CloudRelayError(
            f"Cluster {action} failed ({resp.status_code}): {detail}"
        )


def _intake_payload(
    *,
    project_id: str,
    repo_url: str,
    github_repo_name: Optional[str],
    default_branch: str,
    prompt: str,
    profile_id: Optional[str],
    branch_name: Optional[str],
    base_branch: Optional[str],
    pending_upload_paths: Optional[list[str]],
    overrides: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build the JSON body for a cluster intake call."""
    return {
        "project_id": project_id,
        "repo_url": repo_url,
        "github_repo_name": github_repo_name,
        "default_branch": default_branch,
        "prompt": prompt,
        "profile_id": profile_id,
        "branch_name": branch_name,
        "base_branch": base_branch,
        "pending_upload_paths": pending_upload_paths or [],
        "overrides": overrides,
    }


# ============================================================================
# Cluster relay — registry-based cross-install control
# ============================================================================
#
# Every install here shares ONE database, so a session's id is identical on every
# install. We resolve the *owning* install from the ``installs`` registry by the
# session's ``instance_id`` and relay live control to its ``/api/v1/cluster``
# endpoints, authenticating with the shared cluster secret. History needs no
# relay (it is in the shared DB) — only live stream + send + interrupt do.


class InstallOffline(CloudRelayError):
    """The owning install is unreachable: offline, unregistered, or view-only."""


def is_remote_session(session: Dict[str, Any]) -> bool:
    """True if the session is owned by a *different* install on the shared DB.

    NULL ``instance_id`` (legacy) and our own id both count as local.
    """
    from app.core.config import settings

    inst = session.get("instance_id")
    return bool(inst) and inst != settings.effective_instance_id


def resolve_target(session: Dict[str, Any]) -> Tuple[str, str]:
    """Resolve ``(base_url, session_id)`` for the install that owns ``session``.

    The session id is shared cluster-wide, so it is returned unchanged. Raises
    ``CloudRelayError`` if the session is actually local (caller should run it
    here), or ``InstallOffline`` when the owner is unregistered, offline, or has
    no peer-reachable ``base_url`` (view-only) — in which case the caller should
    surface a "device offline / not controllable" message and keep history
    read-only.
    """
    from app.core.config import settings

    instance_id = session.get("instance_id")
    if not instance_id or instance_id == settings.effective_instance_id:
        raise CloudRelayError("Session is owned by this install (no relay needed)")

    install = database.get_install(instance_id)
    if not install:
        raise InstallOffline(f"Owning install {instance_id} is not registered")
    name = install.get("device_name") or instance_id
    if not install.get("online"):
        raise InstallOffline(f"Device '{name}' is offline")
    base_url = (install.get("base_url") or "").rstrip("/")
    if not base_url:
        raise InstallOffline(f"Device '{name}' is view-only (no reachable address)")
    return base_url, session["id"]


def _cluster_headers() -> Dict[str, str]:
    from app.core import cluster

    return cluster.cluster_headers()


async def relay_message(
    session: Dict[str, Any],
    prompt: str,
    profile_id: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
) -> None:
    """Relay a follow-up prompt to the install that owns the session."""
    base_url, sid = resolve_target(session)
    payload = {"prompt": prompt, "profile_id": profile_id, "overrides": overrides}
    async with httpx.AsyncClient(timeout=CONTROL_TIMEOUT) as client:
        try:
            resp = await client.post(
                f"{base_url}/api/v1/cluster/sessions/{sid}/message",
                json=payload,
                headers=_cluster_headers(),
            )
        except httpx.HTTPError as e:
            raise InstallOffline(f"Could not reach owning install: {e}") from e
    _raise_for_status(resp, "cluster message")


async def relay_interrupt(session: Dict[str, Any]) -> None:
    """Relay an interrupt/stop to the install that owns the session."""
    base_url, sid = resolve_target(session)
    async with httpx.AsyncClient(timeout=CONTROL_TIMEOUT) as client:
        try:
            resp = await client.post(
                f"{base_url}/api/v1/cluster/sessions/{sid}/interrupt",
                headers=_cluster_headers(),
            )
        except httpx.HTTPError as e:
            raise InstallOffline(f"Could not reach owning install: {e}") from e
    _raise_for_status(resp, "cluster interrupt")


async def relay_live(
    session: Dict[str, Any],
    since_update_id: int = 0,
) -> Tuple[int, Optional[Dict[str, Any]]]:
    """Fetch the owning install's live session state.

    Returns ``(status_code, json_or_none)``; a 304 (no change) yields
    ``(304, None)`` so the caller can pass it straight through to the poller.
    """
    base_url, sid = resolve_target(session)
    async with httpx.AsyncClient(timeout=CONTROL_TIMEOUT) as client:
        try:
            resp = await client.get(
                f"{base_url}/api/v1/cluster/sessions/{sid}/live",
                params={"since_update_id": since_update_id},
                headers=_cluster_headers(),
            )
        except httpx.HTTPError as e:
            raise InstallOffline(f"Could not reach owning install: {e}") from e
    if resp.status_code == 304:
        return 304, None
    _raise_for_status(resp, "cluster live")
    return resp.status_code, resp.json()


def resolve_install_base_url(target_instance_id: str) -> str:
    """Resolve an online, controllable install's base URL for offload.

    Raises ``InstallOffline`` when the chosen device is unregistered, offline, or
    view-only (no advertised URL).
    """
    install = database.get_install(target_instance_id)
    if not install:
        raise InstallOffline(f"Device {target_instance_id} is not registered")
    name = install.get("device_name") or target_instance_id
    if not install.get("online"):
        raise InstallOffline(f"Device '{name}' is offline")
    base_url = (install.get("base_url") or "").rstrip("/")
    if not base_url:
        raise InstallOffline(f"Device '{name}' is view-only (no reachable address)")
    return base_url


async def cluster_intake(
    base_url: str,
    *,
    project_id: str,
    repo_url: str,
    github_repo_name: Optional[str],
    default_branch: str,
    prompt: str,
    profile_id: Optional[str] = None,
    branch_name: Optional[str] = None,
    base_branch: Optional[str] = None,
    pending_upload_paths: Optional[list[str]] = None,
    overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Offload to a registered install via the cluster-secret intake endpoint.

    The worker (``run_cloud_intake``) clones/links the project, spins up a
    worktree session and starts the query. Targeted by registry-resolved
    ``base_url`` and authenticated with the shared cluster secret. The returned
    ``session_id`` is shared cluster-wide, so the primary needs no proxy row.
    """
    payload = _intake_payload(
        project_id=project_id,
        repo_url=repo_url,
        github_repo_name=github_repo_name,
        default_branch=default_branch,
        prompt=prompt,
        profile_id=profile_id,
        branch_name=branch_name,
        base_branch=base_branch,
        pending_upload_paths=pending_upload_paths,
        overrides=overrides,
    )
    async with httpx.AsyncClient(timeout=INTAKE_TIMEOUT) as client:
        try:
            resp = await client.post(
                f"{base_url.rstrip('/')}/api/v1/cluster/intake",
                json=payload,
                headers=_cluster_headers(),
            )
        except httpx.HTTPError as e:
            raise InstallOffline(f"Could not reach target device: {e}") from e
    _raise_for_status(resp, "cluster intake")
    return resp.json()


async def cluster_upload_file(base_url: str, local_path: str) -> Optional[str]:
    """Upload a local attachment to a target install via the cluster endpoint.

    Returns the workspace-relative path the target stored it at (for
    ``pending_upload_paths``), or ``None`` on a soft failure.
    """
    p = Path(local_path)
    if not p.exists():
        logger.warning("Cluster upload skipped, file missing: %s", local_path)
        return None
    async with httpx.AsyncClient(timeout=UPLOAD_TIMEOUT) as client:
        try:
            with p.open("rb") as fh:
                resp = await client.post(
                    f"{base_url.rstrip('/')}/api/v1/cluster/upload",
                    files={"file": (p.name, fh)},
                    headers=_cluster_headers(),
                )
        except httpx.HTTPError as e:
            logger.warning("Cluster upload failed for %s: %s", local_path, e)
            return None
    if resp.status_code >= 400:
        logger.warning("Cluster upload rejected for %s (%s)", local_path, resp.status_code)
        return None
    try:
        return resp.json().get("path")
    except Exception:  # noqa: BLE001
        return None
