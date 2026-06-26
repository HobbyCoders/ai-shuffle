"""
Cloud relay — client for cross-install (cluster) session control.

When a session runs in "cloud" execution mode it is owned by another install in
the cluster. Every install shares ONE database, so the session's id is identical
everywhere; we resolve the *owning* install from the ``installs`` registry by the
session's ``instance_id`` and relay live control (stream, send, interrupt) to its
``/api/v1/cluster`` endpoints, authenticating with the shared cluster secret.
History needs no relay — it already lives in the shared DB.

This module is the caller→owner client. The owner side lives in
``app.api.cluster`` (the ``/api/v1/cluster`` endpoints) and ``app.api.cloud``
(the intake/worktree machinery the owner runs).
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
    """Raised when a peer install returns an error or is unreachable."""


def _raise_for_status(resp: httpx.Response, action: str) -> None:
    if resp.status_code >= 400:
        detail = ""
        try:
            body = resp.json()
            detail = body.get("detail") or body.get("message") or ""
        except Exception:  # noqa: BLE001
            detail = (resp.text or "")[:300]
        raise CloudRelayError(
            f"Peer install {action} failed ({resp.status_code}): {detail}"
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
    """Build the JSON body for the cluster intake call."""
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
# Every install shares ONE database, so a session's id is identical on every
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


# ---------------------------------------------------------------------------
# Mobile-notification fan-out
# ---------------------------------------------------------------------------
#
# Session events (a question, a permission prompt, "session done") are raised on
# the install that *owns* the running session and are otherwise in-memory there.
# A phone is connected to a single install (the hub), so without a bridge it
# would never learn about activity on a *different* cluster install. The owning
# install therefore fans each notification out to every other online, reachable
# peer, which delivers it to its own connected phones. The receiving endpoint
# (`/api/v1/cluster/mobile-notify`) delivers locally only and never re-fans, so
# there is no relay loop.

# Notifications are latency-sensitive but optional: keep the per-peer timeout
# short so a slow/unreachable peer never stalls the others or the hot path.
MOBILE_NOTIFY_TIMEOUT = 8.0


def online_peers_with_url() -> list[Dict[str, Any]]:
    """Other installs that are online AND advertise a peer-reachable base URL.

    Excludes this install (local delivery already covers it) and any view-only
    peer (no ``base_url`` to POST to). A single-install deployment returns an
    empty list, so fan-out is a no-op after one cheap registry read.
    """
    from app.core.config import settings

    me = settings.effective_instance_id
    peers: list[Dict[str, Any]] = []
    for row in database.get_installs():
        if row.get("id") == me:
            continue
        if not row.get("online"):
            continue
        if not (row.get("base_url") or "").strip():
            continue
        peers.append(row)
    return peers


async def _post_mobile_notify(base_url: str, payload: Dict[str, Any]) -> None:
    """POST a single mobile notification to one peer; swallow all errors.

    Best-effort by design: a notification that fails to reach one peer must not
    affect the others or surface to the caller.
    """
    async with httpx.AsyncClient(timeout=MOBILE_NOTIFY_TIMEOUT) as client:
        try:
            resp = await client.post(
                f"{base_url.rstrip('/')}/api/v1/cluster/mobile-notify",
                json=payload,
                headers=_cluster_headers(),
            )
            resp.raise_for_status()
        except Exception as e:  # noqa: BLE001 — best-effort fan-out
            logger.debug("mobile-notify to %s failed: %s", base_url, e)


async def fanout_mobile_notify(
    title: str,
    body: str,
    data: Optional[Dict[str, Any]] = None,
) -> int:
    """Relay a mobile notification to every other online, reachable install.

    Returns the number of peers contacted (0 on a single-install deployment or
    when the registry is unavailable). Never raises — the caller is a
    fire-and-forget hot path that must not be disrupted by notification routing.
    """
    import asyncio

    from app.db.async_database import run_sync

    try:
        peers = await run_sync(online_peers_with_url)
    except Exception as e:  # noqa: BLE001 — registry unavailable → no fan-out
        logger.debug("mobile-notify fan-out skipped (registry error): %s", e)
        return 0
    if not peers:
        return 0
    payload = {"title": title, "body": body, "data": data or {}}
    await asyncio.gather(
        *(_post_mobile_notify(p["base_url"], payload) for p in peers),
        return_exceptions=True,
    )
    return len(peers)


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
