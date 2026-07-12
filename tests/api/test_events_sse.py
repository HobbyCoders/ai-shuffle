"""Tests for the SSE event-log tail (app/api/events.py).

The endpoint's SSE body never ends, and ASGI test transports (httpx
ASGITransport / TestClient) buffer the full response before returning it, so
streaming through them deadlocks. Instead these tests consume the module-level
``event_stream`` generator directly — the endpoint is a thin StreamingResponse
wrapper around it, covered by ``test_endpoint_returns_sse_response``.

The gateway's listener thread is stubbed (no real PostgreSQL); the notify path
is exercised by calling ``_dispatch`` by hand.
"""

import asyncio
import json

import pytest

import app.api.events as events_mod
from app.api.events import event_stream, stream_session_events
from app.core.event_gateway import EventGateway
from app.db import session_events as se


@pytest.fixture
def gw(monkeypatch):
    """Fresh gateway with the PG listener stubbed, wired into the events module."""
    g = EventGateway()
    monkeypatch.setattr(g, "_ensure_listener", lambda: None)
    monkeypatch.setattr(events_mod, "get_gateway", lambda: g)
    return g


def _parse(frame: str) -> dict:
    parsed = {}
    for line in frame.splitlines():
        if line.startswith("id: "):
            parsed["id"] = int(line[4:])
        elif line.startswith("event: "):
            parsed["event"] = line[len("event: "):]
        elif line.startswith("data: "):
            parsed["data"] = json.loads(line[len("data: "):])
    return parsed


async def _collect(agen, n, timeout=5.0):
    """Read ``n`` event frames (skipping keepalive comments) from the stream."""
    frames = []

    async def _pump():
        async for chunk in agen:
            if chunk.startswith(":"):
                continue
            frames.append(_parse(chunk))
            if len(frames) >= n:
                return

    await asyncio.wait_for(_pump(), timeout=timeout)
    return frames


async def test_backfill_frames_arrive_in_order(mock_db, gw):
    se.append_events("sess-a", [
        {"type": "turn.started"},
        {"type": "assistant.text_delta", "payload": {"text": "hello"}},
        {"type": "turn.completed", "payload": {"stop_reason": "end_turn"}},
    ])

    agen = event_stream("sess-a", 0)
    try:
        frames = await _collect(agen, 3)
    finally:
        await agen.aclose()

    assert [f["id"] for f in frames] == [1, 2, 3]
    assert [f["event"] for f in frames] == [
        "turn.started", "assistant.text_delta", "turn.completed",
    ]
    # Merged wire shape: payload fields at top level, envelope keys present.
    assert frames[1]["data"]["text"] == "hello"
    assert frames[1]["data"]["type"] == "assistant.text_delta"
    assert frames[1]["data"]["seq"] == 2
    assert frames[1]["data"]["session_id"] == "sess-a"
    assert frames[2]["data"]["stop_reason"] == "end_turn"


async def test_from_seq_skips_already_seen(mock_db, gw):
    se.append_events("sess-b", [
        {"type": "turn.started"},
        {"type": "turn.completed"},
        {"type": "turn.started"},
    ])

    agen = event_stream("sess-b", 2)
    try:
        frames = await _collect(agen, 1)
    finally:
        await agen.aclose()

    assert len(frames) == 1
    assert frames[0]["id"] == 3


async def test_poll_fallback_delivers_without_dispatch(mock_db, gw, monkeypatch):
    """A new event must arrive via the keepalive poll even with NO notify."""
    monkeypatch.setattr(events_mod, "KEEPALIVE_SECONDS", 0.05)
    se.append_events("sess-c", [{"type": "turn.started"}])

    agen = event_stream("sess-c", 0)
    try:
        first = await _collect(agen, 1)
        assert first[0]["id"] == 1
        se.append_events("sess-c", [{"type": "turn.completed"}])
        more = await _collect(agen, 1, timeout=3.0)
    finally:
        await agen.aclose()

    assert more[0]["id"] == 2
    assert more[0]["event"] == "turn.completed"


async def test_notify_wakes_stream_before_poll(mock_db, gw, monkeypatch):
    """A gateway dispatch must deliver promptly even with the poll far away."""
    monkeypatch.setattr(events_mod, "KEEPALIVE_SECONDS", 30.0)
    se.append_events("sess-d", [{"type": "turn.started"}])

    agen = event_stream("sess-d", 0)
    try:
        first = await _collect(agen, 1)
        assert first[0]["id"] == 1
        _, last_seq = se.append_events("sess-d", [{"type": "turn.completed"}])
        gw._dispatch("sess-d", last_seq)
        more = await _collect(agen, 1, timeout=3.0)
    finally:
        await agen.aclose()

    assert more[0]["id"] == 2


async def test_close_unsubscribes(mock_db, gw):
    se.append_events("sess-e", [{"type": "turn.started"}])

    agen = event_stream("sess-e", 0)
    await _collect(agen, 1)
    assert "sess-e" in gw._subscribers
    await agen.aclose()
    assert gw._subscribers == {}


async def test_endpoint_returns_sse_response(mock_db, gw):
    response = await stream_session_events("sess-f", 0, token="test-token")
    try:
        assert response.media_type == "text/event-stream"
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"
    finally:
        await response.body_iterator.aclose()
