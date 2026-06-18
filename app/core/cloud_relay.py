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
    payload = {
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
