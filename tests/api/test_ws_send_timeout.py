"""Regression tests for bounded WebSocket sends (stale-transcript-after-drop bug).

Root cause: a client that vanishes UNCLEANLY (wifi switch, phone lock, NAT
timeout) leaves a half-open socket. Nothing raises server-side — frames just
buffer in the kernel until the socket send buffer fills, then ``await send``
blocks INDEFINITELY (uvicorn's protocol pings are disabled in main.py, and the
app-level ping only sends, never enforces a pong deadline, so the dead peer is
never detected). The turn pump awaits its broadcast leg between events, so one
blocked send froze the pump — and with it the TurnRecorder, so the session
event log stalled and the transcript went stale for EVERY device. Even a hard
refresh backfilled only pre-stall events, while the SDK subprocess kept
working.

The fix bounds every send in the pump's broadcast path:
  1. ``safe_ws_send`` (origin-socket path) times out after
     WS_SEND_TIMEOUT_SECONDS and the caller treats it as a dead connection.
  2. ``ChatWebSocketHandler.send_json`` marks the connection dead on timeout
     and force-closes the zombie socket so the receive loop's finally-cleanup
     actually runs.
  3. ``sync_engine`` device fan-out (broadcast_event / send_event / send_json)
     times out per device and prunes the dead one, so one zombie device can't
     stall delivery to the healthy ones.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi.websockets import WebSocketState

from app.api.ws_utils import safe_ws_send
from app.api.chat_ws_handler import ChatWebSocketHandler
from app.core.sync_engine import (
    DeviceConnection,
    SyncEngine,
    SyncEvent,
    UserDeviceConnection,
)


class _HungWebSocket:
    """A half-open socket: sends never complete and never raise."""

    def __init__(self):
        self.client_state = WebSocketState.CONNECTED
        self.close = AsyncMock()

    async def send_text(self, text: str):
        await asyncio.Event().wait()  # parks forever

    async def send_json(self, data):
        await asyncio.Event().wait()


class _HealthyWebSocket:
    def __init__(self):
        self.client_state = WebSocketState.CONNECTED
        self.sent: list = []
        self.close = AsyncMock()

    async def send_text(self, text: str):
        self.sent.append(text)

    async def send_json(self, data):
        self.sent.append(data)


# ─── safe_ws_send ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_safe_ws_send_times_out_on_hung_socket(monkeypatch):
    """A send that never completes raises TimeoutError instead of blocking the
    caller (the turn pump) forever."""
    monkeypatch.setattr("app.api.ws_utils.WS_SEND_TIMEOUT_SECONDS", 0.05)

    with pytest.raises(asyncio.TimeoutError):
        await safe_ws_send(_HungWebSocket(), {"type": "start"})  # type: ignore


# ─── ChatWebSocketHandler.send_json ──────────────────────────────────────────


def _make_handler(ws) -> ChatWebSocketHandler:
    return ChatWebSocketHandler(
        websocket=ws, device_id="dev-1", user_id="admin", api_user_id=None
    )


@pytest.mark.asyncio
async def test_send_json_timeout_poisons_connection_and_force_closes(monkeypatch):
    """A hung send marks the connection dead (so the pump's origin_send
    fast-fails from then on) and schedules a background close of the zombie
    socket so the receive loop unblocks and cleanup deregisters the device."""
    monkeypatch.setattr("app.api.ws_utils.WS_SEND_TIMEOUT_SECONDS", 0.05)
    ws = _HungWebSocket()
    handler = _make_handler(ws)

    ok = await handler.send_json({"type": "start", "session_id": "s"})

    assert ok is False
    assert handler.connection_healthy is False

    # The force-close runs as a fire-and-forget task — give it a beat.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    ws.close.assert_awaited()

    # Fast-path: subsequent sends return False immediately (no 10s stall each).
    ok2 = await handler.send_json({"type": "anything"})
    assert ok2 is False


# ─── sync_engine fan-out ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_broadcast_event_prunes_hung_device_and_reaches_healthy_one(monkeypatch):
    """One half-open device must not stall the fan-out: the hung device times
    out, is pruned from the registry, and the healthy device still receives the
    frame."""
    monkeypatch.setattr("app.core.sync_engine._SEND_TIMEOUT_SECONDS", 0.05)
    engine = SyncEngine()
    hung = _HungWebSocket()
    healthy = _HealthyWebSocket()
    await engine.register_device("dev-hung", "sess-1", hung)  # type: ignore
    await engine.register_device("dev-ok", "sess-1", healthy)  # type: ignore

    await engine.broadcast_event(
        SyncEvent(event_type="stream_frame", session_id="sess-1", data={"type": "start"})
    )

    # Healthy device got the frame despite the zombie sibling.
    assert len(healthy.sent) == 1
    # The hung device was pruned so future broadcasts skip it entirely.
    assert "dev-hung" not in engine._connections.get("sess-1", {})
    assert "dev-ok" in engine._connections.get("sess-1", {})


@pytest.mark.asyncio
async def test_device_connection_send_event_times_out(monkeypatch):
    monkeypatch.setattr("app.core.sync_engine._SEND_TIMEOUT_SECONDS", 0.05)
    conn = DeviceConnection(
        device_id="dev-1", session_id="sess-1", websocket=_HungWebSocket()  # type: ignore
    )
    ok = await conn.send_event(
        SyncEvent(event_type="stream_frame", session_id="sess-1", data={})
    )
    assert ok is False


@pytest.mark.asyncio
async def test_user_device_connection_send_json_times_out(monkeypatch):
    monkeypatch.setattr("app.core.sync_engine._SEND_TIMEOUT_SECONDS", 0.05)
    conn = UserDeviceConnection(
        device_id="dev-1", user_id="u1", websocket=_HungWebSocket()  # type: ignore
    )
    ok = await conn.send_json({"type": "ping"})
    assert ok is False
