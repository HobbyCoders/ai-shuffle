"""Owner-only UI for Chat resource access. Never exposed as an agent tool."""
from __future__ import annotations

import logging

import asyncssh
from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.auth import require_admin
from app.api.sessions_helpers import require_session
from app.core.chat_access import AccessDenied, GrantSubmission, broker, session_owner

router = APIRouter(prefix="/api/v1/chat-access", tags=["Chat access"])
logger = logging.getLogger(__name__)


async def checked(request: Request, session_id: str):
    from app.core.owner_auth import loopback_request
    if (request.url.scheme != "https"
            and request.scope.get("shuffle.forwarded_https") is not True
            and not loopback_request(request)):
        raise HTTPException(403, "Private access requires the desktop loopback connection or HTTPS to this worker, directly or through its protected remote connection.")
    if getattr(request.state, "preview_grant", None) is not None:
        raise HTTPException(403, "Preview access cannot manage private connections.")
    await require_session(request, session_id)


@router.get("/{session_id}")
async def snapshot(session_id: str, request: Request, token: str = Depends(require_admin)):
    await checked(request, session_id)
    try:
        return await broker.snapshot(session_id)
    except AccessDenied as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/{session_id}/requests/{request_id}")
async def grant(session_id: str, request_id: str, request: Request, body: dict, token: str = Depends(require_admin)):
    await checked(request, session_id)
    # Validate manually so a Pydantic error never echoes supplied SSH secrets.
    try:
        spec = GrantSubmission.model_validate(body)
    except ValueError:
        raise HTTPException(422, "Invalid access request. Check the fields and duration.") from None
    try:
        return await broker.fulfill(session_id, request_id, spec)
    except AccessDenied as exc:
        raise HTTPException(400, str(exc)) from None
    except asyncssh.PermissionDenied:
        raise HTTPException(400, "SSH sign-in was rejected. Check the username and password or private key.") from None
    except asyncssh.HostKeyNotVerifiable:
        raise HTTPException(400, "The SSH host key did not match. Verify the server fingerprint using a trusted source.") from None
    except TimeoutError:
        raise HTTPException(400, "The SSH connection timed out. Check the server address, port and reachability.") from None
    except (asyncssh.Error, ValueError, OSError) as exc:
        logger.warning("Chat access connection failed: %s", type(exc).__name__)
        raise HTTPException(400, "Could not connect. Check the path or SSH address, host-key fingerprint and credentials.") from None
    except Exception as exc:  # noqa: BLE001 — never echo private inputs from unexpected failures
        logger.warning("Chat access grant failed: %s", type(exc).__name__)
        raise HTTPException(500, "Private access is unavailable on this worker. Check the worker log before retrying.") from None


@router.delete("/{session_id}/saved/{connection_id}")
async def forget_connection(session_id: str, connection_id: str, request: Request, token: str = Depends(require_admin)):
    await checked(request, session_id)
    from app.core import chat_vault
    from app.db.async_database import run_sync
    try:
        owner = await session_owner(session_id)
        await run_sync(chat_vault.delete, owner, connection_id)
        for grant in list(broker.grants.values()):
            if grant.owner[2:] == owner[2:] and grant.saved_id == connection_id:
                broker.revoke(grant.session_id, grant.id)
        # Clients no longer poll empty snapshots, so tell every panel of this
        # owner (on any session) that the saved list changed.
        broker._notify(session_id, owner, saved=True)
        return {"deleted": True}
    except AccessDenied as exc:
        raise HTTPException(409, str(exc)) from None


@router.delete("/{session_id}/requests/{request_id}")
async def decline(session_id: str, request_id: str, request: Request, token: str = Depends(require_admin)):
    await checked(request, session_id)
    try:
        await broker.decline(session_id, request_id)
        return {"declined": True}
    except AccessDenied as exc:
        raise HTTPException(409, str(exc)) from None


@router.delete("/{session_id}/grants/{grant_id}")
async def revoke(session_id: str, grant_id: str, request: Request, token: str = Depends(require_admin)):
    await checked(request, session_id)
    try:
        # The owner check also refuses management through a mirrored session.
        await broker.get(session_id, grant_id)
        broker.revoke(session_id, grant_id)
        return {"revoked": True}
    except AccessDenied as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/{session_id}/actions/{action_id}")
async def decide(session_id: str, action_id: str, request: Request, body: dict, token: str = Depends(require_admin)):
    await checked(request, session_id)
    if set(body) != {"allow"} or type(body["allow"]) is not bool:
        raise HTTPException(422, "Supply a single boolean approval decision.")
    try:
        await broker.decide(session_id, action_id, body["allow"])
        return {"accepted": True}
    except AccessDenied as exc:
        raise HTTPException(409, str(exc)) from None
