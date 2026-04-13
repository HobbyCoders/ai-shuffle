"""WebSocket utility functions."""

import asyncio
import logging
from typing import Optional, Callable, Awaitable

from fastapi import WebSocket
from fastapi.websockets import WebSocketState

logger = logging.getLogger(__name__)


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
