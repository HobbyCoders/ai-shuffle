"""Live browser panel API — registry publish, status, and screencast WS proxy.

Three surfaces:

  POST /api/v1/browser/panel/register   (loopback, token-authed, hidden)
      The spawned ``browser`` MCP server publishes the CDP page it is driving.

  GET  /api/v1/sessions/{id}/browser/panel
      User-facing status: does a target exist, its url, is the browser running.

  WS   /api/v1/ws/browser/{id}
      The live screencast + input proxy. Speaks the panel WS protocol and
      attaches the socket to that session's :class:`BrowserStreamHub`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import suppress
from typing import Awaitable, Callable, Optional

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from pydantic import BaseModel

from app.api.auth import require_auth
from app.api.common import api_error_handler
from app.api.sessions_helpers import (
    check_session_access_for_api_user,
    require_session,
)
from app.api.ws_auth import authenticate_websocket
from app.api.ws_key_watch import watch_api_key
from app.api.ws_utils import safe_ws_send
from app.core import browser_control, browser_stream
from app.core import browser_panel_registry as registry
from app.db import database
from app.db.async_database import run_sync
from app.network import browser_panel as remote_panel
from app.network import control, tokens

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Browser"])


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class PanelRegisterRequest(BaseModel):
    session_id: str
    token: str
    target_id: Optional[str] = None
    tab_id: Optional[str] = None
    url: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _browser_running() -> bool:
    """Live CDP liveness probe (shared one in app/core/native_chrome)."""
    from app.core.native_chrome import cdp_get

    return await cdp_get("/json/version") is not None


async def _initial_status(session_id: str) -> dict:
    """Compute the status frame sent right after ``hello`` on a fresh WS."""
    if not await _browser_running():
        return {"type": "status", "state": "browser_off", "url": None}
    target = registry.get_target(session_id)
    if target is not None and target.target_id:
        return {"type": "status", "state": "connecting", "url": target.url}
    return {"type": "status", "state": "no_page", "url": None}


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------
@router.post(
    "/api/v1/browser/panel/register",
    include_in_schema=False,
    status_code=status.HTTP_204_NO_CONTENT,
)
async def register_panel(body: PanelRegisterRequest) -> Response:
    """Publish the session's current CDP target. Authed by the panel token."""
    if not registry.verify_token(body.session_id, body.token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Invalid panel token"
        )
    if browser_control.get(body.session_id).runtime_id is not None:
        raise HTTPException(409, "Workspace runtime owns this session")
    registry.register(body.session_id, body.target_id, body.tab_id, body.url)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/api/v1/browser/panel/control/{operation}", include_in_schema=False)
async def workspace_control(operation: str, request: Request):
    if operation not in ("register", "poll", "report"):
        raise HTTPException(404)
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > browser_control.MAX_BODY:
            raise HTTPException(413, "Workspace payload too large")
    try:
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError()
        session_id = browser_control.identifier(body.get("session_id"))
        runtime_id = browser_control.identifier(body.get("runtime_id"))
        token = body.get("token")
        if not isinstance(token, str) or len(token) > 256:
            raise ValueError()
        browser_control.finite(body)
    except (ValueError, TypeError, RecursionError):
        raise HTTPException(422, "Invalid workspace payload")
    if not registry.verify_token(session_id, token):
        raise HTTPException(403, "Invalid panel token")
    broker = browser_control.get(session_id)
    try:
        if operation == "register":
            await broker.register(runtime_id)
            return {"ok": True, "viewer_count": len(broker.viewers)}
        broker.check(runtime_id)
        if operation == "poll":
            return {"commands": await broker.poll(runtime_id), "viewer_count": len(broker.viewers)}
        await broker.report(runtime_id, body.get("state"), body.get("results", []))
        return Response(status_code=204)
    except browser_control.ControlError as exc:
        code = 409 if operation in ("register", "poll") else 422
        try:
            broker.check(runtime_id)
        except browser_control.ControlError:
            code = 409
        raise HTTPException(code, str(exc))
    except (ValueError, TypeError, KeyError):
        raise HTTPException(422, "Invalid workspace payload")


@router.post("/api/v1/sessions/{session_id}/browser/panel/start")
async def start_panel(
    request: Request, session_id: str, token: str = Depends(require_auth),
) -> dict:
    """Ready the shared workspace without taking control or navigating it."""
    session = await require_session(request, session_id)
    owner = control.owner_of(session)
    if owner:
        return await remote_panel.forward_http(request, owner, session_id, "start")
    from app.core import browser_runtime
    try:
        return await browser_runtime.start(session_id)
    except browser_runtime.RuntimeUnavailable as exc:
        # These messages are app-authored constants (never server-echoed text),
        # and the panel shows the detail verbatim: a startup failure the user
        # can act on beats a generic 503 they can only re-try.
        raise HTTPException(
            503, str(exc) or "Browser runtime could not start"
        ) from None


@router.post("/api/v1/sessions/{session_id}/browser/panel/upload")
async def upload_panel_files(
    request: Request, session_id: str, viewer_id: str, runtime_id: str,
    tab_id: str, field: str, token: str = Depends(require_auth),
) -> dict:
    session = await require_session(request, session_id)
    owner = control.owner_of(session)
    if owner:
        return await remote_panel.forward_http(request, owner, session_id, "upload")
    from app.core import browser_uploads
    return await browser_uploads.upload(request, session_id, viewer_id, runtime_id, tab_id, field)


@router.post("/api/v1/browser/panel/rpc", include_in_schema=False)
async def browser_rpc(request: Request):
    from app.core import browser_runtime
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > browser_control.MAX_BODY:
            raise HTTPException(413, "Browser payload too large")
    try:
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError()
        session_id = browser_control.identifier(body.get("session_id"))
        token = body.get("token")
        if not isinstance(token, str) or len(token) > 256:
            raise ValueError()
    except (ValueError, TypeError, RecursionError):
        raise HTTPException(422, "Invalid browser payload") from None
    if not registry.verify_token(session_id, token):
        raise HTTPException(403, "Invalid panel token")
    # Tokens from deleted sessions, even during cleanup, cannot start a process.
    if not await run_sync(database.get_session, session_id):
        raise HTTPException(404, "Session unavailable")
    try:
        return await browser_runtime.call(session_id, body.get("tool"), body.get("arguments", {}))
    except (ValueError, TypeError, RecursionError):
        raise HTTPException(422, "Invalid browser tool call") from None
    except browser_runtime.RuntimeUnavailable as exc:
        raise HTTPException(
            503, str(exc) or "Browser runtime unavailable; outcome unknown"
        ) from None


@router.get("/api/v1/sessions/{session_id}/browser/panel")
async def get_panel_status(
    request: Request,
    session_id: str,
    token: str = Depends(require_auth),
) -> dict:
    """Report whether a live panel target exists for this session."""
    async with api_error_handler("get browser panel status"):
        session = await require_session(request, session_id)  # 404 / 403 access check
        owner = control.owner_of(session)
        if owner:
            return await remote_panel.forward_http(request, owner, session_id, "")
        target = registry.get_target(session_id)
        return {
            "has_target": bool(target is not None and target.target_id),
            "url": target.url if target is not None else None,
            "browser_running": await _browser_running(),
        }


@router.get("/api/v1/network/peer/browser/{session_id}")
async def peer_panel_status(session_id: str, device_id: str = Depends(tokens.require_device)) -> dict:
    await remote_panel.require_owned(session_id)
    target = registry.get_target(session_id)
    return {"has_target": bool(target is not None and target.target_id),
            "url": target.url if target is not None else None,
            "browser_running": await _browser_running()}


@router.post("/api/v1/network/peer/browser/{session_id}/start")
async def peer_panel_start(session_id: str, device_id: str = Depends(tokens.require_device)) -> dict:
    await remote_panel.require_owned(session_id)
    from app.core import browser_runtime
    try:
        return await browser_runtime.start(session_id)
    except browser_runtime.RuntimeUnavailable as exc:
        raise HTTPException(503, str(exc) or "Browser runtime could not start") from None


@router.post("/api/v1/network/peer/browser/{session_id}/upload")
async def peer_panel_upload(request: Request, session_id: str, viewer_id: str, runtime_id: str,
                            tab_id: str, field: str, device_id: str = Depends(tokens.require_device)) -> dict:
    await remote_panel.require_owned(session_id)
    from app.core import browser_uploads
    return await browser_uploads.upload(request, session_id, viewer_id, runtime_id, tab_id, field)


@router.websocket("/api/v1/network/peer/browser/{session_id}")
async def peer_panel_websocket(websocket: WebSocket, session_id: str) -> None:
    await remote_panel.serve_peer(websocket, session_id)


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------
async def _panel_ping(hub: browser_stream.BrowserStreamHub, viewer) -> None:
    try:
        while True:
            await asyncio.sleep(30)
            if not await hub.send_to_viewer(viewer, {"type": "ping"}):
                break
    except asyncio.CancelledError:
        pass


@router.websocket("/api/v1/ws/browser/{session_id}")
async def browser_panel_websocket(
    websocket: WebSocket,
    session_id: str,
    token: Optional[str] = Query(None),
) -> None:
    """Live screencast + input proxy for one session's supervised browser."""
    await websocket.accept()

    auth_ok, api_user = await authenticate_websocket(websocket, token)
    if not auth_ok:
        await websocket.close(code=4001, reason="Authentication failed")
        return

    session = await run_sync(database.get_session, session_id)
    if not session:
        await websocket.close(code=4004, reason="Session not found")
        return

    try:
        check_session_access_for_api_user(api_user, session)
    except HTTPException:
        await websocket.close(code=4003, reason="Access denied")
        return

    # A revoked API key loses the panel (app.api.ws_key_watch).
    async with watch_api_key(websocket):
        owner = control.owner_of(session)
        if owner:
            await remote_panel.forward_websocket(websocket, owner, session_id)
            return
        await serve_browser_panel(websocket, session_id)


async def serve_browser_panel(
    websocket: WebSocket,
    session_id: str,
    *,
    login_only: bool = False,
    login_input: Optional[Callable[[dict], Awaitable[None]]] = None,
) -> None:
    """Login controls belong to the provider flow, not the read-only stream hub."""
    # Handshake: hello, then the initial status (sent directly — no viewer
    # sender is running yet, so there is no concurrent writer to race).
    viewer_id = None if login_only else str(uuid.uuid4())
    if login_only:
        await safe_ws_send(websocket, {"type": "hello", "protocol": 1})
    else:
        await safe_ws_send(
            websocket,
            {
                "type": "hello",
                "protocol": 2,
                "viewer_id": viewer_id,
                "capabilities": ["workspace-control"],
            },
        )
    initial = await _initial_status(session_id)
    if login_only:
        initial["url"] = None
    await safe_ws_send(websocket, initial)

    if login_only:
        hub, viewer = await browser_stream.attach_viewer(
            session_id, websocket, redact_urls=True
        )
    else:
        hub, viewer = await browser_stream.attach_viewer(session_id, websocket)
    # Toolbar state for a hub that already existed (mid-navigation reconnect):
    # a brand-new hub broadcasts its first page frame soon after anyway.
    if isinstance(hub, browser_stream.BrowserStreamHub) and (
        hub.page_url is not None or hub.page_loading
    ):
        await hub.send_to_viewer(
            viewer, {"type": "page", "url": hub.page_url, "loading": hub.page_loading}
        )
    broker = None if login_only else browser_control.get(session_id)
    if broker is not None:
        broker.attach(viewer_id, lambda message: hub.send_to_viewer(viewer, message))
        await broker.send(viewer_id, {"type": "workspace", "state": broker.state})
    ping_task = asyncio.create_task(_panel_ping(hub, viewer))

    try:
        while True:
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                break
            except RuntimeError:
                break  # this side already closed it (a revoked key, app.api.ws_key_watch)

            try:
                if len(raw.encode("utf-8")) > 32768:
                    raise ValueError("payload too large")
                data = json.loads(raw)
                browser_control.finite(data)
                if not isinstance(data, dict):
                    raise ValueError("payload not an object")
            except Exception:
                await websocket.close(code=4400, reason="Protocol violation")
                break

            mtype = data.get("type")
            if mtype == "pong":
                continue
            if login_only:
                if mtype not in ("input", "command", "viewport"):
                    await websocket.close(code=4400, reason="Protocol violation")
                    break
                try:
                    cdp_cmd = browser_stream.translate_input(
                        data, bounds=hub.last_bounds()
                    )
                except Exception:
                    await websocket.close(code=4400, reason="Protocol violation")
                    break
                if cdp_cmd is not None:
                    try:
                        if login_input is None:
                            raise ValueError("Missing login input owner")
                        await login_input(cdp_cmd)
                    except Exception:
                        await hub.send_to_viewer(viewer, {
                            "type": "error",
                            "message": "The sign-in browser is unavailable. Close this dialog and try again.",
                        })
                        await websocket.close(code=1011, reason="Sign-in browser unavailable")
                        break
                continue
            if mtype in (
                "workspace-command",
                "input",
                "navigate",
                "command",
                "viewport",
                "inspect",
            ):
                try:
                    broker.identity(data)
                    if mtype == "inspect":
                        target = registry.get_target(session_id)
                        if (
                            target is None
                            or target.target_id != broker.state["target_id"]
                            or getattr(hub, "_attached_target_id", None)
                            != target.target_id
                        ):
                            raise browser_control.ControlError(
                                "Stale inspection target"
                            )
                        inspect = browser_stream.parse_inspect(data)
                        await hub.dispatch_inspect(viewer, inspect)
                    else:
                        command = dict(data)
                        internal = mtype == "input"
                        if internal:
                            normalized = browser_stream.translate_input(
                                data, bounds=hub.last_bounds()
                            )
                            if normalized is None:
                                raise browser_control.ControlError("Invalid input")
                            command.update(op="input", args=normalized)
                        elif mtype != "workspace-command":
                            keys = {
                                "navigate": ("url",),
                                "command": ("command",),
                                "viewport": ("mode", "width", "height"),
                            }[mtype]
                            command.update(
                                op=mtype, args={key: data.get(key) for key in keys}
                            )
                        await broker.submit(viewer_id, command, internal=internal)
                except (ValueError, TypeError, KeyError) as exc:
                    message = str(exc)[:2048]
                    command_id = data.get("id")
                    if isinstance(command_id, str) and len(command_id) <= 128:
                        await hub.send_to_viewer(
                            viewer,
                            {
                                "type": "command-result",
                                "id": command_id,
                                "ok": False,
                                "error": message,
                            },
                        )
                    else:
                        await hub.send_to_viewer(
                            viewer,
                            {
                                "type": "error",
                                "message": message,
                                "code": "workspace-command-rejected",
                            },
                        )
                continue

            # Anything else is a protocol violation.
            await websocket.close(code=4400, reason="Protocol violation")
            break

    except WebSocketDisconnect:
        pass
    except Exception:
        logger.debug("browser panel WS error for %s", session_id, exc_info=True)
    finally:
        ping_task.cancel()
        with suppress(asyncio.CancelledError):
            await ping_task
        if broker is not None:
            await broker.detach(viewer_id)
        await browser_stream.detach_viewer(session_id, viewer)
