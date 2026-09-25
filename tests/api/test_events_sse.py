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

import pytest
from fastapi import HTTPException

from starlette.requests import ClientDisconnect

import app.api.events as events_mod
from app.api.events import event_stream, stream_session_events
from app.api.sse_utils import is_client_disconnect as _is_client_disconnect
from app.db import session_events as se
from tests.api.helpers import fake_request
from tests.api.sse_helpers import collect_frames, parse_frame


async def _collect_raw(agen, n, timeout=5.0):
    """Like ``collect_frames`` but keeps the ``caught_up`` sentinel (still skips
    keepalive comments) so sentinel placement can be asserted."""
    frames = []

    async def _pump():
        async for chunk in agen:
            if chunk.startswith(":"):
                continue
            frames.append(parse_frame(chunk))
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
        frames = await collect_frames(agen, 3)
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
        frames = await collect_frames(agen, 1)
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
        first = await collect_frames(agen, 1)
        assert first[0]["id"] == 1
        se.append_events("sess-c", [{"type": "turn.completed"}])
        more = await collect_frames(agen, 1, timeout=3.0)
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
        first = await collect_frames(agen, 1)
        assert first[0]["id"] == 1
        _, last_seq = se.append_events("sess-d", [{"type": "turn.completed"}])
        gw._dispatch("sess-d", last_seq)
        more = await collect_frames(agen, 1, timeout=3.0)
    finally:
        await agen.aclose()

    assert more[0]["id"] == 2


async def test_caught_up_sentinel_after_backfill(mock_db, gw, monkeypatch):
    """The catch-up sentinel is emitted once, right after the last backfill frame,
    carries the backfill cursor as ``seq``, and has no ``id:`` line (so it does not
    advance the reconnect cursor)."""
    # Keep the live poll far away so the sentinel is the 3rd frame, not a live one.
    monkeypatch.setattr(events_mod, "KEEPALIVE_SECONDS", 30.0)
    se.append_events("sess-cu", [
        {"type": "turn.started"},
        {"type": "turn.completed"},
    ])

    agen = event_stream("sess-cu", 0)
    try:
        raw = await _collect_raw(agen, 3)
    finally:
        await agen.aclose()

    assert [f.get("event") for f in raw] == [
        "turn.started", "turn.completed", "caught_up",
    ]
    # Sentinel carries the backfill cursor and no id line.
    assert raw[2]["data"] == {"seq": 2}
    assert "id" not in raw[2]


async def test_caught_up_precedes_live_frames(mock_db, gw, monkeypatch):
    """The sentinel is emitted BEFORE any live-tail frame, so the client can fold
    the whole history in one batch before switching to incremental live paint."""
    monkeypatch.setattr(events_mod, "KEEPALIVE_SECONDS", 0.05)
    se.append_events("sess-cu2", [{"type": "turn.started"}])

    agen = event_stream("sess-cu2", 0)
    try:
        # Backfill frame + sentinel.
        head = await _collect_raw(agen, 2, timeout=3.0)
        # A new live event lands after catch-up.
        se.append_events("sess-cu2", [{"type": "turn.completed"}])
        live = await _collect_raw(agen, 1, timeout=3.0)
    finally:
        await agen.aclose()

    assert head[0]["event"] == "turn.started"
    assert head[1]["event"] == "caught_up"
    assert live[0]["event"] == "turn.completed"
    assert live[0]["id"] == 2


async def test_caught_up_emitted_on_empty_backfill(mock_db, gw, monkeypatch):
    """Even when ``from_seq`` is past the log tail (empty backfill — e.g. a
    reconnect), the sentinel is still emitted so the client's batch flag re-arms
    and folds once."""
    monkeypatch.setattr(events_mod, "KEEPALIVE_SECONDS", 30.0)
    se.append_events("sess-cu3", [{"type": "turn.started"}])

    agen = event_stream("sess-cu3", 1)  # from_seq at the tail → no backfill rows
    try:
        raw = await _collect_raw(agen, 1)
    finally:
        await agen.aclose()

    assert raw[0]["event"] == "caught_up"
    assert raw[0]["data"] == {"seq": 1}


async def test_replay_after_clear_is_empty(mock_db, gw, monkeypatch):
    """A fresh hydration AFTER an in-place clear must serve an EMPTY transcript.

    ``clear_session_conversation`` used to wipe every other store but leave
    ``session_events`` (and its seq counter) behind — and since this feed
    replays the event log from seq 0 as the transcript authority, every hard
    reload / new tab / transcript-cache miss resurrected the "cleared"
    conversation. The wipe must reach the log itself.
    """
    monkeypatch.setattr(events_mod, "KEEPALIVE_SECONDS", 30.0)
    from app.db import database

    database.create_session("sess-clear", "default")
    se.append_events("sess-clear", [
        {"type": "turn.started"},
        {"type": "assistant.text_delta", "payload": {"text": "old convo"}},
        {"type": "turn.completed"},
    ])

    database.clear_session_conversation("sess-clear")

    agen = event_stream("sess-clear", 0)
    try:
        raw = await _collect_raw(agen, 1)
    finally:
        await agen.aclose()

    # The one and only frame is the catch-up sentinel at seq 0: no transcript
    # rows replayed, and the legacy backfill (which a truly empty log triggers)
    # finds nothing either because the clear also dropped session_messages.
    assert raw[0]["event"] == "caught_up"
    assert raw[0]["data"] == {"seq": 0}


async def test_close_unsubscribes(mock_db, gw):
    se.append_events("sess-e", [{"type": "turn.started"}])

    agen = event_stream("sess-e", 0)
    await collect_frames(agen, 1)
    assert "sess-e" in gw._subscribers
    await agen.aclose()
    assert gw._subscribers == {}


# ---------------------------------------------------------------------------
# Client-disconnect handling (issue #627)
#
# On disconnect Starlette cancels the streaming task, throwing the exception
# into this generator at its current ``await``. If it escapes, the app's
# BaseHTTPMiddleware stack (CSRF/access-log) collapses it into an
# anyio ExceptionGroup that surfaces as an unhandled 500 + error_log row instead
# of a clean connection close. ``athrow`` injects the exact exception shapes at
# the live suspension point without a buffering ASGI test client.
# ---------------------------------------------------------------------------


async def test_client_disconnect_cancellederror_ends_cleanly(mock_db, gw):
    """A plain CancelledError (client disconnect) ends the stream, not raises."""
    se.append_events("sess-cd", [{"type": "turn.started"}])
    agen = event_stream("sess-cd", 0)
    await collect_frames(agen, 1)  # drain backfill -> generator parked mid-stream
    assert "sess-cd" in gw._subscribers

    # Generator swallows the cancellation and returns -> athrow raises StopAsyncIteration.
    with pytest.raises(StopAsyncIteration):
        await agen.athrow(asyncio.CancelledError())

    assert gw._subscribers == {}  # finally still unsubscribed; nothing escaped


async def test_client_disconnect_exceptiongroup_ends_cleanly(mock_db, gw):
    """The anyio ExceptionGroup-wrapped cancellation is handled the same way."""
    se.append_events("sess-eg", [{"type": "turn.started"}])
    agen = event_stream("sess-eg", 0)
    await collect_frames(agen, 1)
    assert "sess-eg" in gw._subscribers

    grp = BaseExceptionGroup("disconnect", [asyncio.CancelledError()])
    with pytest.raises(StopAsyncIteration):
        await agen.athrow(grp)

    assert gw._subscribers == {}


async def test_client_disconnect_clientdisconnect_ends_cleanly(mock_db, gw):
    """A starlette ClientDisconnect thrown into the stream also ends cleanly."""
    se.append_events("sess-cld", [{"type": "turn.started"}])
    agen = event_stream("sess-cld", 0)
    await collect_frames(agen, 1)

    with pytest.raises(StopAsyncIteration):
        await agen.athrow(ClientDisconnect())

    assert gw._subscribers == {}


async def test_real_error_still_propagates(mock_db, gw):
    """A genuine mid-stream error is NOT swallowed — it must still surface."""
    se.append_events("sess-err", [{"type": "turn.started"}])
    agen = event_stream("sess-err", 0)
    await collect_frames(agen, 1)
    assert "sess-err" in gw._subscribers

    with pytest.raises(RuntimeError, match="boom"):
        await agen.athrow(RuntimeError("boom"))

    assert gw._subscribers == {}  # unsubscribed even on the error path


async def test_exceptiongroup_with_real_error_propagates(mock_db, gw):
    """An ExceptionGroup carrying a real error is re-raised, not swallowed."""
    se.append_events("sess-egr", [{"type": "turn.started"}])
    agen = event_stream("sess-egr", 0)
    await collect_frames(agen, 1)

    grp = BaseExceptionGroup("mixed", [asyncio.CancelledError(), RuntimeError("boom")])
    with pytest.raises(BaseExceptionGroup):
        await agen.athrow(grp)

    assert gw._subscribers == {}


def test_is_client_disconnect_classifier():
    """Classifier recognizes disconnect/cancellation shapes and rejects errors."""
    assert _is_client_disconnect(asyncio.CancelledError())
    assert _is_client_disconnect(ClientDisconnect())
    assert _is_client_disconnect(BaseExceptionGroup("g", [asyncio.CancelledError()]))
    assert _is_client_disconnect(
        BaseExceptionGroup("g", [asyncio.CancelledError(), ClientDisconnect()])
    )
    # Nested groups (anyio can wrap more than one level deep).
    assert _is_client_disconnect(
        BaseExceptionGroup("outer", [BaseExceptionGroup("inner", [asyncio.CancelledError()])])
    )
    # Any real error present -> not a pure disconnect.
    assert not _is_client_disconnect(RuntimeError("boom"))
    assert not _is_client_disconnect(
        BaseExceptionGroup("g", [asyncio.CancelledError(), ValueError()])
    )


async def test_endpoint_returns_sse_response(mock_db, gw):
    from app.db import database

    database.create_session("sess-f", "default")
    response = await stream_session_events(fake_request(), "sess-f", 0, token="test-token")
    try:
        assert response.media_type == "text/event-stream"
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"
    finally:
        await response.body_iterator.aclose()


async def test_endpoint_unknown_session_is_404(mock_db, gw):
    with pytest.raises(HTTPException) as exc:
        await stream_session_events(fake_request(), "sess-missing", 0, token="test-token")
    assert exc.value.status_code == 404


async def test_endpoint_denies_api_user_outside_session_profile(mock_db, gw):
    from app.db import database

    database.create_session("sess-g", "default")
    api_user = {"id": "u1", "project_id": None, "profile_ids": ["other-profile"]}
    with pytest.raises(HTTPException) as exc:
        await stream_session_events(fake_request(api_user), "sess-g", 0, token="test-token")
    assert exc.value.status_code == 403
