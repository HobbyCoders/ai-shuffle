"""Regression tests for WebSocket send serialization.

Root cause of the "Send later" UI bug (#fix/chat-messages-not-displaying):
a ``queue_updated`` event carried a raw ``datetime`` (the ``created_at`` column
of a queued-message row). Starlette's ``websocket.send_json`` runs the payload
through ``json.dumps``, which raised ``Object of type datetime is not JSON
serializable``. The handler's ``send_json`` caught that in a broad
``except Exception`` and flipped ``connection_healthy = False`` — permanently
poisoning the connection. The very next thing that happens after a query
finishes is the queue drain, whose ``send_json({"type": "start"})`` then
returned False, so the drained "Send later" message was dropped before it ever
streamed or persisted.

These tests lock in the two-part fix:
  1. ``safe_ws_send`` coerces datetimes (default=str) so they never break a send.
  2. A genuinely non-serializable payload raises ``WSPayloadEncodeError`` and is
     dropped WITHOUT marking the connection dead — only real transport errors do.
"""

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi.websockets import WebSocketState

from app.api.ws_utils import safe_ws_send, WSPayloadEncodeError
from app.api.chat_ws_handler import ChatWebSocketHandler


class _FakeWebSocket:
    """Minimal stand-in for a Starlette WebSocket used by send paths."""

    def __init__(self, send_text_side_effect=None):
        self.client_state = WebSocketState.CONNECTED
        self.sent_text: list[str] = []
        self.send_text = AsyncMock(side_effect=self._send_text)
        self._send_text_side_effect = send_text_side_effect

    async def _send_text(self, text: str):
        if self._send_text_side_effect is not None:
            raise self._send_text_side_effect
        self.sent_text.append(text)


def _queue_updated_payload():
    """A ``queue_updated`` event shaped exactly like the one that broke prod —
    a queued-message row whose ``created_at`` is a raw datetime."""
    return {
        "type": "sync",
        "event_type": "queue_updated",
        "session_id": "sess-1",
        "data": {
            "queued_messages": [
                {
                    "id": "queued-abc",
                    "session_id": "sess-1",
                    "content": "Send later message",
                    "profile_id": None,
                    "project_id": None,
                    "position": 0,
                    "created_at": datetime(2026, 6, 24, 22, 17, 0, tzinfo=timezone.utc),
                }
            ]
        },
    }


# ─── safe_ws_send ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_safe_ws_send_serializes_datetime_payload():
    """A datetime in the payload must NOT raise — it is coerced to a string."""
    ws = _FakeWebSocket()
    await safe_ws_send(ws, _queue_updated_payload())  # type: ignore

    assert len(ws.sent_text) == 1
    # Round-trips as valid JSON and the datetime survived as a string.
    decoded = json.loads(ws.sent_text[0])
    created = decoded["data"]["queued_messages"][0]["created_at"]
    assert isinstance(created, str)
    assert "2026" in created


@pytest.mark.asyncio
async def test_safe_ws_send_raises_typed_error_for_unserializable():
    """A truly non-serializable payload raises WSPayloadEncodeError (not a bare
    exception) so callers can distinguish 'bad message' from 'dead socket'."""
    ws = _FakeWebSocket()

    # default=str stringifies almost any scalar, so the reliable way to make
    # json.dumps raise is a recursive structure (ValueError: circular reference).
    recursive: dict = {}
    recursive["self"] = recursive

    with pytest.raises(WSPayloadEncodeError):
        await safe_ws_send(ws, {"type": "x", "data": recursive})  # type: ignore

    # Nothing was sent.
    assert ws.sent_text == []


@pytest.mark.asyncio
async def test_safe_ws_send_propagates_transport_error():
    """A transport-level send failure propagates so the caller can mark the
    connection dead (this path SHOULD poison the connection)."""
    ws = _FakeWebSocket(send_text_side_effect=RuntimeError("socket closed"))

    with pytest.raises(RuntimeError):
        await safe_ws_send(ws, {"type": "ping"})  # type: ignore


# ─── ChatWebSocketHandler.send_json (the real poisoning contract) ────────────
#
# W2: ``send_json`` is now a non-blocking enqueue onto this socket's writer, so
# "accepted" (True) and "on the wire" are two different moments. Encoding still
# happens synchronously at enqueue time, which is what keeps the "one bad
# message is not a dead socket" contract below intact; transport failures now
# surface on the writer task instead of at the call site.


def _make_handler(ws) -> ChatWebSocketHandler:
    return ChatWebSocketHandler(
        websocket=ws, device_id="dev-1", user_id="admin", api_user_id=None
    )


async def _until(predicate, timeout: float = 2.0):
    """Wait for a writer-task side effect (delivery, death) to land."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()


async def _drain(handler):
    """Stop the handler's writer task so no task outlives the test."""
    if handler._writer is not None:
        await handler._writer.aclose()


@pytest.mark.asyncio
async def test_send_json_datetime_payload_keeps_connection_healthy():
    """The exact regression: a queue_updated event with a datetime used to flip
    connection_healthy=False. It must now succeed and leave the connection alive
    so the subsequent queue drain can still send its 'start' frame."""
    ws = _FakeWebSocket()
    handler = _make_handler(ws)

    ok = await handler.send_json(_queue_updated_payload())

    assert ok is True
    assert handler.connection_healthy is True
    # Delivery happens on the writer task now, not at the call site.
    assert await _until(lambda: len(ws.sent_text) == 1)
    await _drain(handler)


@pytest.mark.asyncio
async def test_send_json_unserializable_drops_message_without_poisoning():
    """A non-serializable payload is dropped (returns False) but the connection
    stays healthy — one bad message must not kill the whole connection."""
    ws = _FakeWebSocket()
    handler = _make_handler(ws)

    recursive: dict = {}
    recursive["self"] = recursive

    ok = await handler.send_json({"type": "weird", "data": recursive})

    assert ok is False
    assert handler.connection_healthy is True  # NOT poisoned
    # Nothing reached the writer — the bad frame never got queued.
    assert ws.sent_text == []
    # A subsequent well-formed send still goes through.
    ok2 = await handler.send_json({"type": "start", "session_id": "s"})
    assert ok2 is True
    assert await _until(lambda: len(ws.sent_text) == 1)
    await _drain(handler)


@pytest.mark.asyncio
async def test_send_json_transport_error_poisons_connection():
    """A real transport failure DOES mark the connection dead.

    W2: the failure surfaces on the writer task (the frame was accepted onto the
    queue first), and the writer's reap hook poisons the connection — after
    which the fast path short-circuits every later send without touching the
    socket, exactly as before.
    """
    ws = _FakeWebSocket(send_text_side_effect=RuntimeError("socket closed"))
    handler = _make_handler(ws)

    ok = await handler.send_json({"type": "start", "session_id": "s"})
    assert ok is True  # accepted onto the queue; the wire is the writer's job

    assert await _until(lambda: handler.connection_healthy is False)
    # Fast-path: once dead, further sends return False without touching the socket.
    ws.send_text.reset_mock()
    ok2 = await handler.send_json({"type": "anything"})
    assert ok2 is False
    ws.send_text.assert_not_called()
    await _drain(handler)
