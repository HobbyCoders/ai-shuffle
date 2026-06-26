"""
Cloud relay — client for offloading sessions to a second AI Shuffle install.

When a session runs in "cloud" execution mode, the install the user is talking
to (the *primary*) does not run the agent itself. Instead it forwards the work
to a configured second install (the *worker*) over the worker's normal REST API,
authenticating with an API key. The worker creates/links a project, spins up a
git worktree session and runs the query with its own Claude credentials. The
primary then mirrors the worker's live session state back to the user so the
session looks and behaves like any other local session.

This module is the primary→worker client. The worker side lives in
``app.api.cloud`` (the ``/api/v1/cloud/intake`` endpoints).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import httpx

from app.db import database

logger = logging.getLogger(__name__)

CLOUD_URL_SETTING = "cloud_install_url"
CLOUD_API_KEY_SETTING = "cloud_install_api_key"

# Network timeouts (seconds). Intake clones a repo and may take a while; the
# rest are quick control-plane calls.
INTAKE_TIMEOUT = 180.0
CONTROL_TIMEOUT = 30.0
UPLOAD_TIMEOUT = 120.0


class CloudNotConfigured(Exception):
    """Raised when cloud offload is used but no second install is configured."""


class CloudRelayError(Exception):
    """Raised when the second install returns an error or is unreachable."""


def get_cloud_config() -> Tuple[str, str]:
    """Return ``(base_url, api_key)`` for the configured second install.

    Raises ``CloudNotConfigured`` if either piece is missing.
    """
    from app.api.settings_crypto import get_decrypted_api_key

    base_url = (database.get_system_setting(CLOUD_URL_SETTING) or "").strip().rstrip("/")
    api_key = get_decrypted_api_key(CLOUD_API_KEY_SETTING) or ""
    if not base_url or not api_key:
        raise CloudNotConfigured(
            "Cloud offload is not configured. Set the second install URL and "
            "API key in Settings → System → Cloud Offload."
        )
    return base_url, api_key


def is_configured() -> bool:
    """True if a second install URL and API key are both set."""
    try:
        get_cloud_config()
        return True
    except CloudNotConfigured:
        return False


def _headers(api_key: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _raise_for_status(resp: httpx.Response, action: str) -> None:
    if resp.status_code >= 400:
        detail = ""
        try:
            body = resp.json()
            detail = body.get("detail") or body.get("message") or ""
        except Exception:  # noqa: BLE001
            detail = (resp.text or "")[:300]
        raise CloudRelayError(
            f"Second install {action} failed ({resp.status_code}): {detail}"
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
    """Build the JSON body shared by the legacy and cluster intake calls."""
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


async def intake(
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
    """Create (or link) a project + worktree session on the worker and start work.

    Returns the worker's intake response, including ``session_id`` (the remote
    session id) and worktree metadata.
    """
    base_url, api_key = get_cloud_config()
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
                f"{base_url}/api/v1/cloud/intake",
                json=payload,
                headers=_headers(api_key),
            )
        except httpx.HTTPError as e:
            raise CloudRelayError(f"Could not reach second install: {e}") from e
    _raise_for_status(resp, "intake")
    return resp.json()


async def forward_message(
    session: Dict[str, Any],
    prompt: str,
    profile_id: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
) -> None:
    """Forward a follow-up prompt to an existing cloud-worker session."""
    base_url, remote_id = _session_remote(session)
    _, api_key = get_cloud_config()
    payload = {"prompt": prompt, "profile_id": profile_id, "overrides": overrides}
    async with httpx.AsyncClient(timeout=CONTROL_TIMEOUT) as client:
        try:
            resp = await client.post(
                f"{base_url}/api/v1/cloud/intake/{remote_id}/message",
                json=payload,
                headers=_headers(api_key),
            )
        except httpx.HTTPError as e:
            raise CloudRelayError(f"Could not reach second install: {e}") from e
    _raise_for_status(resp, "message")


async def forward_interrupt(session: Dict[str, Any]) -> None:
    """Forward an interrupt/stop to an existing cloud-worker session."""
    base_url, remote_id = _session_remote(session)
    _, api_key = get_cloud_config()
    async with httpx.AsyncClient(timeout=CONTROL_TIMEOUT) as client:
        try:
            resp = await client.post(
                f"{base_url}/api/v1/cloud/intake/{remote_id}/interrupt",
                headers=_headers(api_key),
            )
        except httpx.HTTPError as e:
            raise CloudRelayError(f"Could not reach second install: {e}") from e
    _raise_for_status(resp, "interrupt")


async def get_live(
    session: Dict[str, Any],
    since_update_id: int = 0,
) -> Tuple[int, Optional[Dict[str, Any]]]:
    """Fetch the worker's live session state.

    Returns ``(status_code, json_or_none)``. A 304 (no change) yields
    ``(304, None)`` so the caller can pass it straight through to the poller.
    """
    base_url, remote_id = _session_remote(session)
    _, api_key = get_cloud_config()
    async with httpx.AsyncClient(timeout=CONTROL_TIMEOUT) as client:
        try:
            resp = await client.get(
                f"{base_url}/api/v1/sessions/{remote_id}/live",
                params={"since_update_id": since_update_id},
                headers=_headers(api_key),
            )
        except httpx.HTTPError as e:
            raise CloudRelayError(f"Could not reach second install: {e}") from e
    if resp.status_code == 304:
        return 304, None
    _raise_for_status(resp, "live")
    return resp.status_code, resp.json()


async def upload_file(local_path: str) -> Optional[str]:
    """Upload a local file to the worker's workspace.

    Returns the workspace-relative path the worker stored it at, which can be
    passed back to intake as a ``pending_upload_paths`` entry. Returns ``None``
    on a soft failure (intake still proceeds without the attachment).
    """
    base_url, api_key = get_cloud_config()
    p = Path(local_path)
    if not p.exists():
        logger.warning("Cloud upload skipped, file missing: %s", local_path)
        return None
    async with httpx.AsyncClient(timeout=UPLOAD_TIMEOUT) as client:
        try:
            with p.open("rb") as fh:
                resp = await client.post(
                    f"{base_url}/api/v1/workspace/files/upload",
                    files={"file": (p.name, fh)},
                    headers=_headers(api_key),
                )
        except httpx.HTTPError as e:
            logger.warning("Cloud upload failed for %s: %s", local_path, e)
            return None
    if resp.status_code >= 400:
        logger.warning(
            "Cloud upload rejected for %s (%s)", local_path, resp.status_code
        )
        return None
    try:
        return resp.json().get("path")
    except Exception:  # noqa: BLE001
        return None


def _session_remote(session: Dict[str, Any]) -> Tuple[str, str]:
    """Pull the worker base URL + remote session id off a local cloud session."""
    base_url = (session.get("cloud_remote_url") or "").rstrip("/")
    remote_id = session.get("cloud_remote_session_id")
    if not base_url or not remote_id:
        raise CloudRelayError("Session is not a cloud session")
    return base_url, remote_id


# ============================================================================
# Cluster relay — registry-based cross-install control
# ============================================================================
#
# The newer path. Unlike the legacy offload above (per-session ``cloud_remote_url``
# + a manually-configured API key), every install here shares ONE database, so a
# session's id is identical on every install. We resolve the *owning* install
# from the ``installs`` registry by the session's ``instance_id`` and relay live
# control to its ``/api/v1/cluster`` endpoints, authenticating with the shared
# cluster secret. History needs no relay (it is in the shared DB) — only live
# stream + send + interrupt do.


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

    Same worker behavior as legacy ``intake`` (clone/link project, worktree,
    start query) but targeted by registry-resolved ``base_url`` and authenticated
    with the shared cluster secret instead of a per-install API key. The returned
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
