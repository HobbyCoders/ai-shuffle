"""
Browser WebSocket — Dedicated endpoint for live browser streaming and input.

Endpoint: /ws/browser/{session_id}

Separates high-bandwidth frame data from the chat WebSocket to avoid
interfering with chat streaming latency.

Frame delivery uses binary WebSocket messages (raw JPEG bytes) for ~33%
less overhead than base64-encoded JSON.  Input events are JSON messages
dispatched directly to Chrome DevTools Protocol for minimal latency.

Message types FROM server:
- Binary: Raw JPEG bytes (screencast frames, ~10-30 FPS)
- JSON: {"type": "state", "url": "...", "title": "...", "isActive": true}
- JSON: {"type": "error", "message": "..."}
- JSON: {"type": "ping"}

Message types FROM client:
- {"type": "start", "width": N, "height": N, "quality"?: N, "fps"?: N}
- {"type": "stop"}
- {"type": "resize", "width": N, "height": N}
- {"type": "mouse", "action": "click"|"move"|"down"|"up", "x": N, "y": N, "button"?: str}
- {"type": "key", "action": "down"|"up"|"press", "key": str, "code": str, "text"?: str, "modifiers"?: N}
- {"type": "scroll", "x": N, "y": N, "deltaX": N, "deltaY": N}
- {"type": "pong"}
"""

import asyncio
import json
import logging
import secrets
import time
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from fastapi.websockets import WebSocketState

from app.api.websocket import authenticate_websocket
from app.core.config import INTERNAL_BROWSER_TOKEN
from app.db import database
from app.mcp.chrome_session import chrome_session_manager as browser_session_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["BrowserWebSocket"])


@router.websocket("/ws/browser/{session_id}")
async def browser_websocket(
    websocket: WebSocket,
    session_id: str,
    token: Optional[str] = Query(None),
):
    """
    Dedicated WebSocket for live browser screencast and input forwarding.

    Lifecycle:
    1. Client connects with session_id and auth token
    2. Server authenticates and gets/creates browser session
    3. Client sends {"type": "start", "width": N, "height": N} to begin screencast
    4. Server streams binary JPEG frames + JSON state updates
    5. Client sends mouse/key/scroll events for real-time interaction
    6. On disconnect, screencast stops automatically
    """
    await websocket.accept()

    # Authenticate — reuse existing WebSocket auth
    is_authenticated, user_id = await authenticate_websocket(websocket, token)

    # Also accept internal browser token (for future internal use)
    if not is_authenticated:
        internal_token = websocket.query_params.get("internal_token")
        if internal_token and INTERNAL_BROWSER_TOKEN and secrets.compare_digest(internal_token, INTERNAL_BROWSER_TOKEN):
            is_authenticated = True
            user_id = "internal"

    if not is_authenticated:
        logger.warning(f"Browser WebSocket auth failed for session {session_id}")
        await websocket.close(code=4001, reason="Authentication failed")
        return

    # Verify the session belongs to the authenticated user (skip for internal)
    if user_id != "internal":
        db_session = database.get_session(session_id)
        if db_session and db_session.get("user_id") and db_session["user_id"] != user_id:
            logger.warning(f"Browser WebSocket: user {user_id} denied access to session {session_id}")
            await websocket.close(code=4003, reason="Access denied")
            return

    # Get or create browser session
    try:
        session = await browser_session_manager.get_or_create_session(session_id)
    except Exception as e:
        logger.error(f"Failed to get/create browser session {session_id}: {e}")
        await websocket.close(code=4002, reason="Browser session error")
        return

    screencast_active = False
    send_lock = asyncio.Lock()
    connection_healthy = True

    async def send_frame(frame_data: bytes) -> None:
        """Send raw JPEG frame as binary WebSocket message."""
        nonlocal connection_healthy
        if not connection_healthy:
            return

        async with send_lock:
            if not connection_healthy:
                return
            if websocket.client_state != WebSocketState.CONNECTED:
                connection_healthy = False
                return
            try:
                await websocket.send_bytes(frame_data)
            except Exception:
                connection_healthy = False

    async def send_json_safe(data: dict) -> None:
        """Send a JSON message, ignoring errors if disconnected."""
        nonlocal connection_healthy
        if not connection_healthy:
            return

        async with send_lock:
            if not connection_healthy:
                return
            if websocket.client_state != WebSocketState.CONNECTED:
                connection_healthy = False
                return
            try:
                await websocket.send_json(data)
            except Exception:
                connection_healthy = False

    # Send initial browser state
    title = await session.get_title()
    url = await session.get_url()
    await send_json_safe({
        "type": "state",
        "url": url,
        "title": title,
        "isActive": True,
    })

    # Ping/keepalive task
    async def ping_loop():
        while connection_healthy:
            await asyncio.sleep(30)
            if connection_healthy:
                await send_json_safe({"type": "ping"})

    ping_task = asyncio.create_task(ping_loop())

    # Navigation listener — send state updates when the page navigates
    nav_listener_active = True

    def on_page_load(_=None) -> None:
        """Handler for page load events — sends URL/title updates."""
        if not nav_listener_active or not connection_healthy:
            return

        async def _send_state():
            try:
                t = await session.get_title()
                u = await session.get_url()
                await send_json_safe({
                    "type": "state",
                    "url": u,
                    "title": t,
                    "isActive": True,
                })
            except Exception:
                pass

        asyncio.create_task(_send_state())

    # Register the load listener via CDP event
    if session.cdp_client:
        session.cdp_client.on("Page.loadEventFired", on_page_load)

    # Simple rate limiter for input events (mouse/key/scroll)
    _last_input_time = 0.0
    _INPUT_MIN_INTERVAL = 0.005  # 5ms minimum between input events (~200/sec max)

    try:
        while True:
            raw = await websocket.receive()

            # Handle text (JSON) messages
            if "text" in raw:
                try:
                    data = json.loads(raw["text"])
                except json.JSONDecodeError:
                    continue

                msg_type = data.get("type", "")

                if msg_type == "start":
                    width = max(320, min(3840, data.get("width", 1280)))
                    height = max(240, min(2160, data.get("height", 720)))
                    quality = max(10, min(100, data.get("quality", 60)))
                    fps = max(1, min(30, data.get("fps", 10)))
                    # Chromium screencast base is ~60fps; everyNthFrame controls rate
                    every_nth = max(1, 60 // fps)

                    try:
                        await session.start_screencast(
                            width=width,
                            height=height,
                            quality=quality,
                            every_nth_frame=every_nth,
                            callback=send_frame,
                        )
                        screencast_active = True
                        logger.debug(
                            f"Browser WS: screencast started for {session_id} "
                            f"at {width}x{height} q={quality} fps={fps}"
                        )
                    except Exception as e:
                        logger.error(f"Screencast start failed: {e}")
                        await send_json_safe({"type": "error", "message": f"Screencast failed: {e}"})

                elif msg_type == "stop":
                    if screencast_active:
                        await session.stop_screencast()
                        screencast_active = False

                elif msg_type == "resize":
                    width = max(320, min(3840, data.get("width", 1280)))
                    height = max(240, min(2160, data.get("height", 720)))
                    quality = max(10, min(100, data.get("quality", 60)))

                    if screencast_active:
                        # Restart screencast with new dimensions
                        await session.stop_screencast()
                        try:
                            await session.start_screencast(
                                width=width,
                                height=height,
                                quality=quality,
                                callback=send_frame,
                            )
                        except Exception as e:
                            logger.error(f"Screencast resize failed: {e}")
                            screencast_active = False
                    else:
                        # Just resize viewport
                        await session.set_viewport(width, height)

                elif msg_type == "mouse":
                    # Rate-limit input events
                    now = time.monotonic()
                    if now - _last_input_time < _INPUT_MIN_INTERVAL:
                        continue
                    _last_input_time = now

                    action = data.get("action", "click")
                    x = float(data.get("x", 0))
                    y = float(data.get("y", 0))
                    button = data.get("button", "left")

                    try:
                        if action == "click":
                            await session.cdp_mouse_event("mousePressed", x, y, button, 1)
                            await session.cdp_mouse_event("mouseReleased", x, y, button, 1)
                        elif action == "dblclick":
                            await session.cdp_mouse_event("mousePressed", x, y, button, 1)
                            await session.cdp_mouse_event("mouseReleased", x, y, button, 1)
                            await session.cdp_mouse_event("mousePressed", x, y, button, 2)
                            await session.cdp_mouse_event("mouseReleased", x, y, button, 2)
                        elif action == "down":
                            await session.cdp_mouse_event("mousePressed", x, y, button, 1)
                        elif action == "up":
                            await session.cdp_mouse_event("mouseReleased", x, y, button, 1)
                        elif action == "move":
                            await session.cdp_mouse_event("mouseMoved", x, y)
                    except Exception as e:
                        logger.debug(f"Mouse event failed: {e}")

                elif msg_type == "key":
                    now = time.monotonic()
                    if now - _last_input_time < _INPUT_MIN_INTERVAL:
                        continue
                    _last_input_time = now

                    action = data.get("action", "press")
                    key = data.get("key", "")
                    code = data.get("code", "")
                    text = data.get("text", "")
                    modifiers = data.get("modifiers", 0)

                    try:
                        if action == "press":
                            # For printable characters: use keyDown (no text) + insertText + keyUp.
                            # keyDown with text AND char events both cause double input in raw CDP.
                            # insertText is the reliable way to inject characters.
                            await session.cdp_key_event("keyDown", key, code, "", modifiers)
                            if text and len(text) == 1 and not modifiers:
                                if session.cdp_client and session.cdp_client.connected:
                                    await session.cdp_client.insert_text(text)
                            await session.cdp_key_event("keyUp", key, code, "", modifiers)
                        elif action == "down":
                            await session.cdp_key_event("keyDown", key, code, "", modifiers)
                        elif action == "up":
                            await session.cdp_key_event("keyUp", key, code, "", modifiers)
                    except Exception as e:
                        logger.debug(f"Key event failed: {e}")

                elif msg_type == "scroll":
                    now = time.monotonic()
                    if now - _last_input_time < _INPUT_MIN_INTERVAL:
                        continue
                    _last_input_time = now

                    x = float(data.get("x", 0))
                    y = float(data.get("y", 0))
                    delta_x = float(data.get("deltaX", 0))
                    delta_y = float(data.get("deltaY", 0))

                    try:
                        await session.cdp_scroll(x, y, delta_x, delta_y)
                    except Exception as e:
                        logger.debug(f"Scroll event failed: {e}")

                elif msg_type == "pong":
                    pass  # Keepalive response, no action needed

            # Handle binary messages (currently unused from client)
            elif "bytes" in raw:
                pass

    except WebSocketDisconnect:
        logger.debug(f"Browser WebSocket disconnected: {session_id}")
    except Exception as e:
        logger.error(f"Browser WebSocket error for {session_id}: {e}", exc_info=True)
    finally:
        # Cleanup
        connection_healthy = False
        nav_listener_active = False
        ping_task.cancel()

        # Remove the CDP page load listener
        try:
            if session.cdp_client:
                session.cdp_client.off("Page.loadEventFired", on_page_load)
        except Exception:
            pass

        if screencast_active:
            try:
                await session.stop_screencast()
            except Exception:
                pass

        logger.debug(f"Browser WebSocket cleanup complete: {session_id}")
