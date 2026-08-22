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
from contextlib import suppress
from typing import Optional

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
from app.api.ws_utils import safe_ws_send
from app.core import browser_panel_registry as registry
from app.core import browser_stream
from app.db import database
from app.db.async_database import run_sync

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
    """Live CDP liveness probe (reuses the one in app/api/browser.py)."""
    from app.api.browser import _cdp_get

    return await _cdp_get("/json/version") is not None


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
    registry.register(body.session_id, body.target_id, body.tab_id, body.url)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/api/v1/sessions/{session_id}/browser/panel")
async def get_panel_status(
    request: Request,
    session_id: str,
    token: str = Depends(require_auth),
) -> dict:
    """Report whether a live panel target exists for this session."""
    async with api_error_handler("get browser panel status"):
        await require_session(request, session_id)  # 404 / 403 access check
        target = registry.get_target(session_id)
        return {
            "has_target": bool(target is not None and target.target_id),
            "url": target.url if target is not None else None,
            "browser_running": await _browser_running(),
        }


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

    # Handshake: hello, then the initial status (sent directly — no viewer
    # sender is running yet, so there is no concurrent writer to race).
    await safe_ws_send(websocket, {"type": "hello", "protocol": 1})
    await safe_ws_send(websocket, await _initial_status(session_id))

    hub, viewer = await browser_stream.attach_viewer(session_id, websocket)
    # Toolbar state for a hub that already existed (mid-navigation reconnect):
    # a brand-new hub broadcasts its first page frame soon after anyway.
    if isinstance(hub, browser_stream.BrowserStreamHub) and (
        hub.page_url is not None or hub.page_loading
    ):
        await safe_ws_send(
            websocket, {"type": "page", "url": hub.page_url, "loading": hub.page_loading}
        )
    ping_task = asyncio.create_task(_panel_ping(hub, viewer))

    try:
        while True:
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                break

            try:
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError("payload not an object")
            except Exception:
                await websocket.close(code=4400, reason="Protocol violation")
                break

            mtype = data.get("type")
            if mtype == "pong":
                continue
            if mtype in ("input", "navigate", "command"):
                try:
                    cdp_cmd = browser_stream.translate_input(
                        data, bounds=hub.last_bounds()
                    )
                except Exception:
                    await websocket.close(code=4400, reason="Protocol violation")
                    break
                if cdp_cmd is not None:
                    await hub.dispatch_input(cdp_cmd)
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
        await browser_stream.detach_viewer(session_id, viewer)
