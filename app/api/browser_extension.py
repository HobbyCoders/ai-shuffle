"""Routes for the AI Shuffle browser extension (the owner's own Chrome or Edge).

* ``WS /api/v1/browser-extension/connect``: the extension's single socket. The
  first frame is ``{"event": "hello", "key": "aim_…", browser_id, name,
  product, version}``; the key is the device key the extension got when it
  enrolled with this worker through the owner's account. It travels in a frame,
  not the URL, so it never reaches an access log.
* ``WS /api/v1/browser-extension/cdp/{token}``: loopback only. A chat's browser
  runtime connects here with the one-time token the worker gave it; see
  ``app.core.browser_extension.Relay``.
* ``WS /api/v1/browser-extension/page/{token}``: loopback only. The Browser
  panel's live view of one of the chat's tabs (``PageTap``).
* ``GET/PUT /api/v1/sessions/{id}/browser/target``: which browser a chat uses
  (``null`` is the built-in browser) and the browsers that are connected.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import ipaddress
import json
import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from app.api.auth import require_auth
from app.api.sessions_helpers import require_session
from app.core import browser_extension as ext
from app.db.async_database import run_sync

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Browser"])

EXTENSION_SCOPE = "browser:extension"
HELLO_SECONDS = 10
REVALIDATE_SECONDS = 60


def _device_for_key(key: str) -> dict:
    """The enrolled extension behind ``key``, live-checked like a phone's key."""
    from app.api.mobile import MOBILE_API_KEY_PREFIX, validate_mobile_device
    from app.db import mobile as db

    if not isinstance(key, str) or not key.startswith(MOBILE_API_KEY_PREFIX) or len(key) > 128:
        raise HTTPException(401, "Sign in to AI Shuffle in the extension")
    device = db.get_mobile_device_by_api_key_hash(hashlib.sha256(key.encode()).hexdigest())
    if not device:
        raise HTTPException(401, "This browser is no longer authorized; sign in again")
    device = validate_mobile_device(device)
    if EXTENSION_SCOPE not in (device.get("scopes") or []):
        raise HTTPException(403, "Only the AI Shuffle browser extension can connect here")
    return device


@router.websocket("/api/v1/browser-extension/connect")
async def extension_socket(websocket: WebSocket):
    await websocket.accept()
    try:
        hello = json.loads(await asyncio.wait_for(websocket.receive_text(), HELLO_SECONDS))
        if not isinstance(hello, dict) or hello.get("event") != "hello":
            raise ValueError("hello expected")
        key = hello.pop("key", None)
        device = await run_sync(_device_for_key, key)
        # The browser's id is its enrolled device, never what the extension
        # claims: one extension cannot take over another's browser entry.
        hello["browser_id"] = str(device["device_id"])
        browser = ext.ExtensionBrowser(websocket, hello)
    except HTTPException as exc:
        with contextlib.suppress(Exception):
            await websocket.send_text(json.dumps({"event": "refused", "status": exc.status_code,
                                                  "detail": str(exc.detail)}))
            await websocket.close(code=4001 if exc.status_code == 401 else 4003)
        return
    except (asyncio.TimeoutError, ValueError, ext.ExtensionError, WebSocketDisconnect):
        with contextlib.suppress(Exception):
            await websocket.close(code=4400)
        return
    ext.register(browser)
    await browser.send({"event": "welcome", "browser_id": browser.id})
    logger.info("Browser extension connected: %s", browser.name)
    checked = time.monotonic()
    try:
        while True:
            message = json.loads(await websocket.receive_text())
            if isinstance(message, dict):
                await browser.dispatch(message)
            if time.monotonic() - checked > REVALIDATE_SECONDS:
                checked = time.monotonic()
                try:
                    await run_sync(_device_for_key, key)
                except HTTPException as exc:
                    if exc.status_code < 500:
                        await browser.send({"event": "refused", "status": exc.status_code,
                                            "detail": str(exc.detail)})
                        await websocket.close(code=4001)
                        return
    except (WebSocketDisconnect, ValueError):
        pass
    except Exception:
        logger.exception("Browser extension socket failed")
    finally:
        ext.unregister(browser)
        logger.info("Browser extension disconnected: %s", browser.name)


def _loopback(websocket: WebSocket) -> bool:
    if websocket.scope.get("shuffle.ingress") not in (None, "local"):
        return False
    host = (websocket.client.host if websocket.client else "") or ""
    with contextlib.suppress(ValueError):
        return ipaddress.ip_address(host).is_loopback
    return False


@router.websocket("/api/v1/browser-extension/cdp/{token}")
async def relay_socket(websocket: WebSocket, token: str):
    if not _loopback(websocket):
        await websocket.close(code=4003)
        return
    await ext.serve_relay(websocket, token)


@router.websocket("/api/v1/browser-extension/page/{token}")
async def page_socket(websocket: WebSocket, token: str):
    if not _loopback(websocket):
        await websocket.close(code=4003)
        return
    await ext.serve_page(websocket, token)


class TargetBody(BaseModel):
    browser_id: Optional[str] = Field(default=None, max_length=64)
    # The extension's side panel claims a chat it shows only if nobody picked a
    # browser for that chat yet; an explicit choice is never overridden.
    only_if_unassigned: bool = False


async def _target_response(session_id: str) -> dict:
    return {"browser_id": ext.session_target(session_id), "assigned": ext.session_assigned(session_id),
            "browsers": ext.browsers()}


@router.get("/api/v1/sessions/{session_id}/browser/target")
async def get_target(request: Request, session_id: str, _token: str = Depends(require_auth)) -> dict:
    await require_session(request, session_id)
    return await _target_response(session_id)


@router.put("/api/v1/sessions/{session_id}/browser/target")
async def set_target(request: Request, session_id: str, body: TargetBody,
                     _token: str = Depends(require_auth)) -> dict:
    from app.network import control

    session = await require_session(request, session_id)
    if control.owner_of(session):
        raise HTTPException(409, "This chat runs on another worker; open it there to use your browser")
    if body.only_if_unassigned and ext.session_assigned(session_id):
        return await _target_response(session_id)
    if body.browser_id == ext.session_target(session_id) and ext.session_assigned(session_id):
        return await _target_response(session_id)
    try:
        ext.set_session_target(session_id, body.browser_id)
    except ext.ExtensionError as exc:
        raise HTTPException(409, str(exc)) from None
    # Stop only the runtime (not the session's panel registration, whose token
    # the agent's browser tools hold): the next tool call or panel start opens
    # against the new browser.
    from app.core import browser_runtime
    runtime = browser_runtime._runtimes.get(session_id)
    if runtime:
        await runtime.close()
    return await _target_response(session_id)
