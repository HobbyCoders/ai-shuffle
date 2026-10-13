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
  2. ``sync_engine`` device fan-out no longer awaits sockets at ALL: every
     connection owns a bounded queue + writer task, the producer only enqueues,
     and each writer times its own send out (2.5 s) and reaps its connection
     out of the registry. One zombie device can neither stall the producer nor
     delay delivery to the healthy ones. See
     ``tests/core/test_sync_engine_writer.py`` for the full W2 coverage.
  3. ``ChatWebSocketHandler.send_json`` — the ORIGIN socket, the last unqueued
     send path — now uses that same writer: the call site only enqueues, the
     writer owns the one bounded send, and a dead peer costs exactly one
     stalled task (its own) instead of blocking whoever produced the frame.
     Overflow is two-tier: whole-state snapshots coalesce in place, everything
     correctness-bearing drops the connection rather than the frame.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

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


async def _until(predicate, timeout: float = 2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()


@pytest.mark.asyncio
async def test_send_json_does_not_await_a_hung_socket(monkeypatch):
    """The whole point of the origin queue: the producer returns immediately
    even though the socket never completes a send."""
    monkeypatch.setattr("app.core.sync_engine._SEND_TIMEOUT_SECONDS", 5.0)
    handler = _make_handler(_HungWebSocket())

    started = asyncio.get_running_loop().time()
    ok = await handler.send_json({"type": "start", "session_id": "s"})
    elapsed = asyncio.get_running_loop().time() - started

    assert ok is True  # accepted onto the queue
    assert elapsed < 0.5, f"send_json awaited the socket ({elapsed:.2f}s)"
    await handler._writer.aclose()


@pytest.mark.asyncio
async def test_send_json_timeout_poisons_connection_and_force_closes(monkeypatch):
    """A hung send kills the connection from the WRITER side: its own bounded
    send times out, it closes the zombie socket (so the receive loop unblocks
    and cleanup deregisters the device) and poisons the handler so every later
    send fast-fails."""
    monkeypatch.setattr("app.core.sync_engine._SEND_TIMEOUT_SECONDS", 0.05)
    ws = _HungWebSocket()
    handler = _make_handler(ws)

    ok = await handler.send_json({"type": "start", "session_id": "s"})
    assert ok is True  # the frame was accepted; the wire is the writer's job

    assert await _until(lambda: handler.connection_healthy is False)
    assert await _until(lambda: ws.close.await_count > 0)
    assert handler._writer.death_reason == "send_failed"

    # Fast-path: subsequent sends return False immediately (no stall each).
    ok2 = await handler.send_json({"type": "anything"})
    assert ok2 is False


@pytest.mark.asyncio
async def test_origin_writer_coalesces_whole_state_frames_in_place():
    """Two queue_updated snapshots for one session collapse to the newest, in
    the position the first one held, so ordering with other frames survives."""
    ws = _HungWebSocket()  # never drains, so the queue is observable
    handler = _make_handler(ws)
    writer = handler._ensure_writer()

    assert await handler.send_json(
        {"type": "sync", "event_type": "queue_updated", "session_id": "s1",
         "data": {"queued_messages": ["first"]}}
    )
    assert await handler.send_json({"type": "done", "session_id": "s1"})
    assert await handler.send_json(
        {"type": "sync", "event_type": "queue_updated", "session_id": "s1",
         "data": {"queued_messages": ["second"]}}
    )

    # The first frame is in flight on the hung socket; the two still queued are
    # the (superseded-in-place) snapshot and the done frame — in that order.
    queued = [f.text for f in writer._pending]
    assert len(queued) == 2
    assert '"second"' in queued[0] and '"first"' not in queued[0]
    assert '"done"' in queued[1]
    assert writer.coalesced == 1
    await writer.aclose()


@pytest.mark.asyncio
async def test_origin_writer_drops_connection_rather_than_a_done_frame():
    """Correctness-bearing frames are never silently dropped: an overflowing
    queue takes the connection down instead, and the client resyncs from
    durable state on reconnect."""
    ws = _HungWebSocket()
    handler = _make_handler(ws)
    writer = handler._ensure_writer()

    # Enough correctness-bearing frames to overrun the production queue depth
    # (nothing drains: the socket never completes a send).
    accepted = 0
    attempts = 2000
    for i in range(attempts):
        if not await handler.send_json({"type": "tool_use", "i": i}):
            break
        accepted += 1

    assert accepted < attempts, (
        "the queue never overflowed — test is not exercising the policy"
    )
    assert writer.death_reason == "queue_overflow"
    assert handler.connection_healthy is False
    # Nothing was silently dropped while the connection was still declared live.
    assert await handler.send_json({"type": "done"}) is False
    await writer.aclose()


@pytest.mark.asyncio
async def test_run_awaits_the_writer_task_on_disconnect(monkeypatch):
    """Lifecycle: a writer task must never outlive its connection. When the
    receive loop ends (client disconnected), ``run`` cancels AND awaits it."""
    from starlette.websockets import WebSocketDisconnect

    import app.api.chat_ws_handler as cwh

    ws = _HealthyWebSocket()
    ws.receive_json = AsyncMock(side_effect=WebSocketDisconnect(1001))
    handler = _make_handler(ws)

    sync = MagicMock()
    for name in ("register_user_device", "unregister_user_device", "unregister_device"):
        setattr(sync, name, AsyncMock())
    monkeypatch.setattr(cwh, "sync_engine", sync)
    monkeypatch.setattr(
        "app.core.streaming_status.get_user_streaming_sessions",
        lambda _user_id: [],
    )

    await handler.run()

    writer = handler._writer
    assert writer is not None, "the connect-time frame should have made a writer"
    assert writer._task is None, "writer task outlived the connection"
    assert handler.connection_healthy is False
    # And the connect-time snapshot did reach the socket before teardown.
    assert any(json.loads(t)["type"] == "user_streaming_sessions" for t in ws.sent)


@pytest.mark.asyncio
async def test_origin_writer_delivers_in_order_and_closes_with_the_connection():
    """Happy path: frames arrive in call order, and the writer task does not
    outlive the connection."""
    ws = _HealthyWebSocket()
    handler = _make_handler(ws)

    for i in range(5):
        assert await handler.send_json({"type": "frame", "i": i})
    assert await _until(lambda: len(ws.sent) == 5)
    assert [json.loads(t)["i"] for t in ws.sent] == [0, 1, 2, 3, 4]

    writer = handler._writer
    task = writer._task
    await writer.aclose()
    assert task.done()
    assert writer._task is None


# ─── sync_engine fan-out ─────────────────────────────────────────────────────


async def _until(predicate, timeout: float = 2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()


@pytest.mark.asyncio
async def test_broadcast_event_prunes_hung_device_and_reaches_healthy_one(monkeypatch):
    """One half-open device must not stall the fan-out. Post-W2 the producer
    never waits for it at all — the hung device's OWN writer times its send
    out, closes the zombie socket and reaps the device from the registry, while
    the healthy device receives the frame from its own writer."""
    monkeypatch.setattr("app.core.sync_engine._SEND_TIMEOUT_SECONDS", 0.05)
    engine = SyncEngine()
    hung = _HungWebSocket()
    healthy = _HealthyWebSocket()
    await engine.register_device("dev-hung", "sess-1", hung)  # type: ignore
    await engine.register_device("dev-ok", "sess-1", healthy)  # type: ignore
    try:
        await engine.broadcast_event(
            SyncEvent(event_type="stream_frame", session_id="sess-1", data={"type": "start"})
        )

        # Healthy device got the frame despite the zombie sibling.
        assert await _until(lambda: len(healthy.sent) == 1)
        # The hung device is pruned so future broadcasts skip it entirely.
        assert await _until(lambda: "dev-hung" not in engine._connections.get("sess-1", {}))
        assert "dev-ok" in engine._connections.get("sess-1", {})
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_device_connection_send_event_evicts_a_hung_socket(monkeypatch):
    """send_event is now a non-blocking enqueue (it returns True: the frame was
    accepted), and the connection's writer independently declares the socket
    dead once its bounded send times out."""
    monkeypatch.setattr("app.core.sync_engine._SEND_TIMEOUT_SECONDS", 0.05)
    conn = DeviceConnection(
        device_id="dev-1", session_id="sess-1", websocket=_HungWebSocket()  # type: ignore
    )
    try:
        queued = await conn.send_event(
            SyncEvent(event_type="stream_frame", session_id="sess-1", data={})
        )
        assert queued is True
        assert await _until(lambda: not conn.writer.alive)
        assert conn.writer.death_reason == "send_failed"
        # Subsequent frames fast-fail so the caller can prune the connection.
        assert await conn.send_event(
            SyncEvent(event_type="stream_frame", session_id="sess-1", data={})
        ) is False
    finally:
        await conn.writer.aclose()


@pytest.mark.asyncio
async def test_user_device_connection_send_json_evicts_a_hung_socket(monkeypatch):
    monkeypatch.setattr("app.core.sync_engine._SEND_TIMEOUT_SECONDS", 0.05)
    conn = UserDeviceConnection(
        device_id="dev-1", user_id="u1", websocket=_HungWebSocket()  # type: ignore
    )
    try:
        assert await conn.send_json({"type": "ping"}) is True
        assert await _until(lambda: not conn.writer.alive)
        assert await conn.send_json({"type": "ping"}) is False
    finally:
        await conn.writer.aclose()
