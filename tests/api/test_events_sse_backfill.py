"""Tests for the SSE tail's lazy legacy backfill (app/api/events.py hook).

Consumes the module-level ``event_stream`` generator DIRECTLY — never via
ASGITransport/TestClient, which buffer the infinite SSE body and deadlock. The
gateway listener thread is stubbed (no real PostgreSQL).
"""

import asyncio


import app.api.events as events_mod
import app.core.history_backfill as hb
from app.api.events import event_stream
from app.db import session_events as se
from tests.api.sse_helpers import collect_frames, parse_frame
from tests.shared_fixtures import reset_backfill_memo  # noqa: F401  (autouse fixture)


LEGACY = [
    {"role": "user", "type": "text", "content": "old prompt", "uuid": "u-1"},
    {"role": "assistant", "type": "text", "content": "old reply", "uuid": "a-1"},
]


async def test_stream_backfills_legacy_history_first(mock_db, gw, monkeypatch):
    """An empty log with legacy history serves that history as v2 events."""
    monkeypatch.setattr(hb, "_load_legacy_history", lambda sid: list(LEGACY))

    agen = event_stream("sess-bf-a", 0)
    try:
        frames = await collect_frames(agen, 2)
    finally:
        await agen.aclose()

    assert [f["event"] for f in frames] == ["message.user", "message.assistant"]
    assert [f["id"] for f in frames] == [1, 2]
    assert frames[0]["data"]["content"] == "old prompt"
    assert frames[0]["data"]["uuid"] == "u-1"
    assert frames[0]["data"]["turn_id"] == "backfill"
    assert frames[0]["data"]["run_id"] is None
    assert frames[1]["data"]["content"] == "old reply"


async def test_caught_up_sentinel_follows_legacy_backfill(mock_db, gw, monkeypatch):
    """The catch-up sentinel is emitted after the legacy-history backfill frames,
    so a session hydrated purely from legacy history still paints in one batch."""
    monkeypatch.setattr(hb, "_load_legacy_history", lambda sid: list(LEGACY))
    monkeypatch.setattr(events_mod, "KEEPALIVE_SECONDS", 30.0)

    agen = event_stream("sess-bf-cu", 0)
    raw = []

    async def _pump():
        async for chunk in agen:
            if chunk.startswith(":"):
                continue
            raw.append(parse_frame(chunk))
            if len(raw) >= 3:
                return

    try:
        await asyncio.wait_for(_pump(), timeout=5.0)
    finally:
        await agen.aclose()

    assert [f.get("event") for f in raw] == [
        "message.user", "message.assistant", "caught_up",
    ]
    assert raw[2]["data"] == {"seq": 2}


async def test_backfilled_then_live_events_share_one_seq_space(mock_db, gw, monkeypatch):
    monkeypatch.setattr(hb, "_load_legacy_history", lambda sid: list(LEGACY))

    agen = event_stream("sess-bf-b", 0)
    try:
        first = await collect_frames(agen, 2)
        _, last_seq = se.append_events("sess-bf-b", [{"type": "turn.started"}])
        gw._dispatch("sess-bf-b", last_seq)
        more = await collect_frames(agen, 1, timeout=3.0)
    finally:
        await agen.aclose()

    assert [f["id"] for f in first] == [1, 2]
    assert more[0]["id"] == 3
    assert more[0]["event"] == "turn.started"


async def test_populated_log_skips_legacy_load(mock_db, gw, monkeypatch):
    se.append_events("sess-bf-c", [{"type": "turn.started"}])
    calls = []
    monkeypatch.setattr(
        hb, "_load_legacy_history", lambda sid: calls.append(sid) or list(LEGACY)
    )

    agen = event_stream("sess-bf-c", 0)
    try:
        frames = await collect_frames(agen, 1)
    finally:
        await agen.aclose()

    assert calls == []  # loader never consulted
    assert frames[0]["event"] == "turn.started"
    assert len(se.get_events("sess-bf-c")) == 1


async def test_second_stream_does_not_duplicate_backfill(mock_db, gw, monkeypatch):
    monkeypatch.setattr(hb, "_load_legacy_history", lambda sid: list(LEGACY))

    agen1 = event_stream("sess-bf-d", 0)
    try:
        await collect_frames(agen1, 2)
    finally:
        await agen1.aclose()

    agen2 = event_stream("sess-bf-d", 0)
    try:
        frames = await collect_frames(agen2, 2)
    finally:
        await agen2.aclose()

    assert [f["id"] for f in frames] == [1, 2]
    assert len(se.get_events("sess-bf-d")) == 2  # appended exactly once


async def test_backfill_failure_does_not_break_the_stream(mock_db, gw, monkeypatch):
    def _boom(sid):
        raise RuntimeError("legacy store down")
    monkeypatch.setattr(hb, "_load_legacy_history", _boom)
    monkeypatch.setattr(events_mod, "KEEPALIVE_SECONDS", 0.05)

    agen = event_stream("sess-bf-e", 0)
    try:
        # Stream must stay alive and deliver a later live event.
        se.append_events("sess-bf-e", [{"type": "turn.started"}])
        frames = await collect_frames(agen, 1, timeout=3.0)
    finally:
        await agen.aclose()

    assert frames[0]["event"] == "turn.started"
