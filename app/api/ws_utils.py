"""WebSocket utility functions."""

import asyncio
import json
import logging
from typing import Optional, Callable, Awaitable

from fastapi import WebSocket
from fastapi.websockets import WebSocketState

logger = logging.getLogger(__name__)

# Upper bound on any single outbound WebSocket send. A half-open connection
# (client vanished uncleanly — wifi switch, phone lock, NAT timeout) never
# raises: frames just buffer in the kernel until the socket send buffer fills,
# then the await blocks INDEFINITELY (uvicorn's protocol-level pings are
# disabled in main.py, so the dead peer is never detected). An unbounded send
# in the turn pump's broadcast leg freezes the pump — and with it the
# TurnRecorder, so the session event log stalls and the transcript goes stale
# for EVERY device (even a hard refresh backfills only the pre-stall events)
# until TCP finally gives up on the zombie socket. Bounding the send converts
# that hang into the normal dead-connection error path within seconds.
#
# W2: lowered 10.0 -> 2.5s. The device fan-out no longer awaits sockets at all
# (``app.core.sync_engine`` gives every connection a bounded queue + writer
# task), so this timeout governs only the ORIGIN socket passthrough in
# ``SessionRuntime._default_broadcast``. There it is still a per-frame await in
# the pump, which is exactly why it must be short: it is an eviction deadline
# for a genuinely dead socket, not a budget for a slow one. Kept numerically in
# sync with ``app.core.sync_engine._SEND_TIMEOUT_SECONDS`` (core must not import
# the api layer, hence the duplication).
#
# Remaining gap (needs a file outside this change's ownership): the origin
# socket has no writer queue of its own — ``ChatWebSocketHandler.send_json``
# still awaits inline. Once a send times out the handler poisons the connection
# and every later send fast-fails, so the worst case is ONE 2.5s hiccup per dead
# origin socket rather than one per frame.
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
    through the shared lock/health check. Otherwise falls back to direct
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
