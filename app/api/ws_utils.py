"""WebSocket utility functions."""

import asyncio
import json
import logging
from typing import Optional, Callable, Awaitable

from fastapi import WebSocket
from fastapi.websockets import WebSocketState

logger = logging.getLogger(__name__)

# Bound on a single direct send (browser_panel/browser_stream via safe_ws_send).
# A half-open socket never raises (uvicorn pings are disabled in app/serve.py),
# so an unbounded send would block forever. Kept equal to
# ``app.core.sync_engine._SEND_TIMEOUT_SECONDS`` (core must not import the api layer).
WS_SEND_TIMEOUT_SECONDS = 2.5


class WSPayloadEncodeError(Exception):
    """Raised when a WebSocket payload cannot be JSON-encoded.

    Distinguished from transport failures so callers can drop the single bad
    message WITHOUT marking the whole connection dead — a non-serializable
    payload is a bug in that one message, not a sign the socket has gone away.
    """


async def safe_ws_send(websocket: WebSocket, data: dict) -> None:
    """Encode ``data`` to JSON and send it as a text frame.

    Mirrors Starlette's ``WebSocket.send_json`` (which encodes in text mode)
    but uses ``default=str`` so values that the stock encoder rejects — most
    importantly ``datetime`` objects that slip through from DB rows — are
    coerced to strings instead of raising mid-send.

    Raises:
        WSPayloadEncodeError: if the payload still can't be encoded (a real
            bug in the message). The caller should drop the message but treat
            the connection as healthy.
        asyncio.TimeoutError: the send did not complete within
            ``WS_SEND_TIMEOUT_SECONDS`` — a half-open socket whose send buffer
            is full. Treat exactly like any transport failure: the connection
            is dead.
        Exception: any transport-level send failure is propagated unchanged so
            the caller can mark the connection dead.
    """
    try:
        text = json.dumps(data, separators=(",", ":"), default=str)
    except (TypeError, ValueError) as e:
        raise WSPayloadEncodeError(str(e)) from e
    await asyncio.wait_for(websocket.send_text(text), WS_SEND_TIMEOUT_SECONDS)


async def ping_loop(
    websocket: WebSocket,
    device_id: str,
    send_fn: Optional[Callable[[dict], Awaitable[bool]]] = None,
):
    """Send periodic pings to keep connection alive.

    When send_fn is provided (e.g. the chat endpoint's send_json), pings go
    through it (a False return ends the loop). Otherwise falls back to direct
    websocket.send_json.
    """
    try:
        while True:
            await asyncio.sleep(30)  # Ping every 30 seconds

            ping_msg = {"type": "ping", "timestamp": None}

            if send_fn:
                if not await send_fn(ping_msg):
                    break
            else:
                if websocket.client_state != WebSocketState.CONNECTED:
                    break
                try:
                    await websocket.send_json(ping_msg)
                except Exception:
                    break

    except asyncio.CancelledError:
        pass
