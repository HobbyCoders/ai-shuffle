"""Owner-only UI for Chat resource access. Never exposed as an agent tool.

A session mirrored from another device keeps its grants, requests and saved
connections on that device. These endpoints forward such a session's access
operations to it over the authenticated worker network (see
``app.network.chat_access_routing``) and keep nothing here.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.auth import require_admin
from app.api.sessions_helpers import require_session
from app.core.chat_access import AccessDenied, GrantSubmission, broker, grant_failure
from app.db.async_database import run_sync
from app.network import chat_access_routing as routing
from app.network import control

router = APIRouter(prefix="/api/v1/chat-access", tags=["Chat access"])
logger = logging.getLogger(__name__)

UNSUPPORTED_OWNER = "Unknown control action"

INSECURE_TRANSPORT = ("Private access is not available on this connection. Open this chat in the "
                      "AI Shuffle desktop app window, or through this worker's HTTPS remote access address.")


async def checked(request: Request, session_id: str) -> dict:
    """Private inputs need a transport the worker itself can vouch for.

    Accepted: the local listener from loopback (with the desktop's per-launch
    token when the desktop started the worker), a TLS socket, or the managed
    remote listener, whose ``shuffle.forwarded_https`` flag is derived from the
    accepting socket in app.serve and cannot be set by a request header on any
    other listener. A browser tab on localhost beside the desktop app, plain
    HTTP from the LAN, and forwarded-proto headers on the local listener are
    refused.
    """
    from app.core.owner_auth import loopback_request
    if (request.url.scheme != "https"
            and request.scope.get("shuffle.forwarded_https") is not True
            and not loopback_request(request)):
        raise HTTPException(403, INSECURE_TRANSPORT)
    if getattr(request.state, "preview_grant", None) is not None:
        raise HTTPException(403, "Preview access cannot manage private connections.")
    return await require_session(request, session_id)


async def owning_device(row) -> str | None:
    """The device that runs this session when it is only a mirror here, else None."""
    return await run_sync(control.owner_of, row) if isinstance(row, dict) else None


async def forward(owner: str, session_id: str, operation: str, payload: dict | None = None, *,
                  secure: bool = False, timeout: float | None = None) -> dict:
    try:
        return await routing.forward(owner, session_id, operation, payload, secure=secure, timeout=timeout)
    except control.InsecureRoute:
        raise HTTPException(409, routing.INSECURE_INPUTS) from None
    except control.RunnerUnreachable:
        raise HTTPException(503, "The device that runs this conversation is unreachable. Its private access "
                            "stays on that device; try again when it is online.") from None
    except control.ControlRefused as exc:
        if str(exc) == UNSUPPORTED_OWNER:
            raise HTTPException(409, "Update the device that runs this conversation to manage its private "
                                "access from here.") from None
        raise HTTPException(409, str(exc)) from None


@router.get("/{session_id}")
async def snapshot(session_id: str, request: Request, token: str = Depends(require_admin)):
    row = await checked(request, session_id)
    owner = await owning_device(row)
    if owner is not None:
        try:
            return await routing.forward(owner, session_id, "snapshot")
        except control.RunnerUnreachable:
            # Nothing can be approved from here while the owner is away.
            return dict(routing.EMPTY_SNAPSHOT)
        except control.ControlRefused as exc:
            if str(exc) == UNSUPPORTED_OWNER:
                return dict(routing.EMPTY_SNAPSHOT)
            raise HTTPException(409, str(exc)) from None
    try:
        return await broker.snapshot(session_id, row)
    except AccessDenied as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/{session_id}/requests/{request_id}")
async def grant(session_id: str, request_id: str, request: Request, body: dict, token: str = Depends(require_admin)):
    row = await checked(request, session_id)
    # Validate manually so a Pydantic error never echoes supplied SSH secrets.
    try:
        spec = GrantSubmission.model_validate(body)
    except ValueError:
        raise HTTPException(422, "Invalid access request. Check the fields and duration.") from None
    owner = await owning_device(row)
    if owner is not None:
        # Relayed in memory only; private inputs go to the owner over HTTPS or not at all.
        result = await forward(owner, session_id, "grant", {"request_id": request_id, "submission": body},
                               secure=routing.carries_secret(spec), timeout=routing.GRANT_TIMEOUT)
        if "grant_error" in result:
            error = result["grant_error"]
            raise HTTPException(error["status"], error["detail"])
        return result
    try:
        return await broker.fulfill(session_id, request_id, spec)
    except Exception as exc:  # noqa: BLE001 — never echo private inputs from any failure
        status, message = grant_failure(exc)
        raise HTTPException(status, message) from None


@router.delete("/{session_id}/saved/{connection_id}")
async def forget_connection(session_id: str, connection_id: str, request: Request, token: str = Depends(require_admin)):
    row = await checked(request, session_id)
    owner = await owning_device(row)
    if owner is not None:
        return await forward(owner, session_id, "forget", {"connection_id": connection_id})
    try:
        await broker.forget(session_id, connection_id)
        return {"deleted": True}
    except AccessDenied as exc:
        raise HTTPException(409, str(exc)) from None


@router.delete("/{session_id}/requests/{request_id}")
async def decline(session_id: str, request_id: str, request: Request, token: str = Depends(require_admin)):
    row = await checked(request, session_id)
    owner = await owning_device(row)
    if owner is not None:
        return await forward(owner, session_id, "decline", {"request_id": request_id})
    try:
        await broker.decline(session_id, request_id)
        return {"declined": True}
    except AccessDenied as exc:
        raise HTTPException(409, str(exc)) from None


@router.delete("/{session_id}/grants/{grant_id}")
async def revoke(session_id: str, grant_id: str, request: Request, token: str = Depends(require_admin)):
    row = await checked(request, session_id)
    owner = await owning_device(row)
    if owner is not None:
        return await forward(owner, session_id, "revoke", {"grant_id": grant_id})
    try:
        # The owner check refuses a session this worker no longer runs.
        await broker.get(session_id, grant_id)
        broker.revoke(session_id, grant_id)
        return {"revoked": True}
    except AccessDenied as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/{session_id}/actions/{action_id}")
async def decide(session_id: str, action_id: str, request: Request, body: dict, token: str = Depends(require_admin)):
    row = await checked(request, session_id)
    if set(body) != {"allow"} or type(body["allow"]) is not bool:
        raise HTTPException(422, "Supply a single boolean approval decision.")
    owner = await owning_device(row)
    if owner is not None:
        return await forward(owner, session_id, "decide", {"action_id": action_id, "allow": body["allow"]})
    try:
        await broker.decide(session_id, action_id, body["allow"])
        return {"accepted": True}
    except AccessDenied as exc:
        raise HTTPException(409, str(exc)) from None
