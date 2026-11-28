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
        # The browser's id and account are its enrollment's, never what the
        # extension claims: one extension cannot take over another's browser
        # entry or pass as another account's browser.
        hello["browser_id"] = str(device["device_id"])
        hello["account_id"] = await run_sync(_device_account, device)
        hello["paired_client_id"] = device.get("paired_client_id")
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
    watchdog = asyncio.create_task(_watch_device(key, device.get("paired_client_id"), browser, websocket))
    try:
        while True:
            message = json.loads(await websocket.receive_text())
            if isinstance(message, dict) and not browser.closed:
                await browser.dispatch(message)
    except (WebSocketDisconnect, ValueError, RuntimeError):
        pass
    except Exception:
        logger.exception("Browser extension socket failed")
    finally:
        watchdog.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await watchdog
        ext.unregister(browser)
        logger.info("Browser extension disconnected: %s", browser.name)


def _device_account(device: dict) -> Optional[str]:
    from app.db import mobile_hub as hub_store
    owner = hub_store.ownership(device["device_id"])
    return owner["account_id"] if owner else None


async def _watch_device(key: str, client_id: Optional[str], browser, websocket: WebSocket) -> None:
    """Re-check the extension's device key on a timer, and at once on unpair.

    Independent of incoming frames: a removed extension stops being
    selectable and its socket closes without waiting for it to speak.
    """
    from app.api.ws_key_watch import paired_client_wake

    with paired_client_wake(client_id) as wake:
        while True:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(wake.wait(), REVALIDATE_SECONDS)
            wake.clear()
            try:
                await run_sync(_device_for_key, key)
            except HTTPException as exc:
                if exc.status_code >= 500:
                    continue
                # Unlist it before anything else, so no chat can pick it meanwhile.
                ext.unregister(browser)
                with contextlib.suppress(Exception):
                    await browser.send({"event": "refused", "status": exc.status_code,
                                        "detail": str(exc.detail)})
                with contextlib.suppress(Exception):
                    await websocket.close(code=4001)
                return
            except Exception:  # noqa: BLE001 - a DB hiccup must not end a live socket
                logger.warning("Browser extension re-check failed; retrying", exc_info=True)


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
    # The extension's side panel lets a chat it shows use its browser only
    # while that chat is new (no finished turn, no built-in browser open) and
    # nobody picked a browser for it: viewing an older chat never moves it.
    only_if_new: bool = False


def _browser_access(request: Request) -> ext.MayUse:
    """Which connected browsers this caller may hand to a chat (blocking).

    A browser carries its owner's site sign-ins, so access to a chat is not
    enough. An API key or a shared preview never gets one. A paired device
    gets the browsers signed in with its own Shuffle account, and the
    extension's side panel its own browser. The worker's own login gets every
    browser connected to its worker (another account's extension connects only
    after the worker owner approved that device).
    """
    from app.api.auth import get_api_user_from_request
    from app.core import mobile_tool_identity

    if get_api_user_from_request(request) or mobile_tool_identity.request_is_preview(request):
        return _NO_BROWSER
    return _access(mobile_tool_identity.request_principal(request))


def _NO_BROWSER(_browser) -> bool:
    return False


def _access(paired: Optional[dict]) -> ext.MayUse:
    """A paired controller's browsers, or every browser for the worker login."""
    if not paired:
        return lambda _browser: True
    account, client_id = paired.get("account_id"), paired.get("principal_id")
    return lambda browser: browser is not None and (
        browser.paired_client_id == client_id or (bool(account) and browser.account_id == account))


def socket_browser_access(websocket) -> ext.MayUse:
    """:func:`_browser_access` for a socket :func:`authenticate_websocket` admitted."""
    from app.core import mobile_tool_identity as identity

    value = getattr(websocket.state, "mobile_tool_principal", None)
    if (not isinstance(value, dict) or value.get("kind") == "api_user"
            or value.get(identity.PREVIEW_LIMITED_KEY)
            or (value.get("kind") == "admin" and value.get("principal_id") == identity.PREVIEW_PRINCIPAL_ID)):
        return _NO_BROWSER
    if value.get("kind") == "paired":
        return _access(value)
    return _access(None) if value.get("kind") == "admin" else _NO_BROWSER


CHAT_BROWSER_REFUSED = "This chat uses someone's own browser; only that browser's owner can open it"


def require_chat_browser(request: Request, session_id: str) -> None:
    """403 unless this caller may use the browser the chat drives (blocking)."""
    if not ext.may_use_chat_browser(session_id, _browser_access(request)):
        raise HTTPException(403, CHAT_BROWSER_REFUSED)


def _target_response(session_id: str, may_use: ext.MayUse, session: dict) -> dict:
    from app.network import control

    # A chat hosted on another worker picks its browser there.
    remote = bool(control.owner_of(session))
    return {"browser_id": ext.session_target(session_id), "assigned": ext.session_assigned(session_id),
            "browsers": [] if remote else ext.browsers(may_use), "remote": remote}


@router.get("/api/v1/sessions/{session_id}/browser/target")
async def get_target(request: Request, session_id: str, _token: str = Depends(require_auth)) -> dict:
    session = await require_session(request, session_id)
    may_use = await run_sync(_browser_access, request)
    return _target_response(session_id, may_use, session)


def _is_new_chat(session: dict) -> bool:
    from app.core import browser_runtime
    return int(session.get("turn_count") or 0) == 0 and session["id"] not in browser_runtime._runtimes


@router.put("/api/v1/sessions/{session_id}/browser/target")
async def set_target(request: Request, session_id: str, body: TargetBody,
                     _token: str = Depends(require_auth)) -> dict:
    from app.network import control

    session = await require_session(request, session_id)
    if control.owner_of(session):
        raise HTTPException(409, "This chat runs on another worker; open it there to use your browser")
    may_use = await run_sync(_browser_access, request)
    if body.only_if_new and (ext.session_assigned(session_id) or not _is_new_chat(session)):
        return _target_response(session_id, may_use, session)
    if body.browser_id == ext.session_target(session_id) and ext.session_assigned(session_id):
        return _target_response(session_id, may_use, session)
    if not ext.may_use_chat_browser(session_id, may_use):
        raise HTTPException(403, CHAT_BROWSER_REFUSED)
    try:
        ext.set_session_target(session_id, body.browser_id, may_use)
    except ext.ExtensionError as exc:
        raise HTTPException(409, str(exc)) from None
    # Stop only the runtime (not the session's panel registration, whose token
    # the agent's browser tools hold): the next tool call or panel start opens
    # against the new browser.
    from app.core import browser_runtime
    runtime = browser_runtime._runtimes.get(session_id)
    if runtime:
        await runtime.close()
    return _target_response(session_id, may_use, session)
