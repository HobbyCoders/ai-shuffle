"""SSE backfill coalesces streaming deltas — and never drops history.

Opening a long session used to replay every persisted frame one at a time,
which on a thinking-heavy transcript is thousands of frames the client only
concatenates anyway. The backfill now merges a run of consecutive
assistant.thinking_delta / assistant.text_delta rows (same type, same turn,
same run) into ONE frame, while every other event still ships verbatim. The
generator is consumed DIRECTLY — ASGI transports buffer the infinite SSE body.
"""

import asyncio
import json

import pytest

import app.api.events as events_mod
import app.core.history_backfill as hb
from app.api.events import event_stream
from app.core.event_gateway import EventGateway
from app.db import session_events as se


@pytest.fixture(autouse=True)
def _reset_memo():
    hb._reset_memo()
    yield
    hb._reset_memo()


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


async def _backfill(session_id: str, from_seq: int = 0) -> list:
    """Every frame up to and including the caught_up sentinel."""
    frames = []

    async def _pump(agen):
        async for chunk in agen:
            if chunk.startswith(":"):
                continue
            parsed = _parse(chunk)
            frames.append(parsed)
            if parsed.get("event") == "caught_up":
                return

    agen = event_stream(session_id, from_seq)
    try:
        await asyncio.wait_for(_pump(agen), timeout=5.0)
    finally:
        await agen.aclose()
    return frames


def _delta(kind: str, text: str, turn_id: str) -> dict:
    return {"type": kind, "payload": {"text": text}, "turn_id": turn_id}


THINKING = "assistant.thinking_delta"
TEXT = "assistant.text_delta"


async def test_delta_runs_collapse_to_one_frame_per_turn(mock_db, gw):
    """3 turns of 50 thinking + 10 text deltas replay as 3 + 3 + 3 frames."""
    sid = "sess-coalesce-turns"
    ranges = []
    for t in range(3):
        turn = f"turn-{t}"
        ranges.append(
            se.append_events(
                sid,
                [{"type": "message.user", "payload": {"content": f"ask {t}"}, "turn_id": turn}]
                + [_delta(THINKING, f"th{t}-{i}|", turn) for i in range(50)]
                + [_delta(TEXT, f"tx{t}-{i}|", turn) for i in range(10)],
            )
        )

    frames = await _backfill(sid)

    assert [f["event"] for f in frames] == [
        "message.user", THINKING, TEXT,
        "message.user", THINKING, TEXT,
        "message.user", THINKING, TEXT,
        "caught_up",
    ]
    # No history was elided, and no synthetic boundary was invented.
    assert all(f["event"] != "system.compaction_boundary" for f in frames)

    for t, (first_seq, last_seq) in enumerate(ranges):
        user, think, text = frames[t * 3: t * 3 + 3]
        assert user["id"] == first_seq
        # Merged text is the whole run, in order.
        assert think["data"]["text"] == "".join(f"th{t}-{i}|" for i in range(50))
        assert text["data"]["text"] == "".join(f"tx{t}-{i}|" for i in range(10))
        # The SSE id is the LAST seq of the run, so a reconnect never rewinds
        # into the middle of a merged frame.
        assert think["id"] == first_seq + 50
        assert think["data"]["seq"] == first_seq + 50
        assert text["id"] == last_seq


async def test_a_run_split_across_a_page_boundary_still_merges(mock_db, gw, monkeypatch):
    sid = "sess-coalesce-paged"
    monkeypatch.setattr(events_mod, "_PAGE", 7)
    first_seq, last_seq = se.append_events(
        sid,
        [{"type": "message.user", "payload": {"content": "ask"}, "turn_id": "t1"}]
        + [_delta(THINKING, f"{i}|", "t1") for i in range(20)],
    )

    frames = await _backfill(sid)

    assert [f["event"] for f in frames] == ["message.user", THINKING, "caught_up"]
    assert frames[1]["data"]["text"] == "".join(f"{i}|" for i in range(20))
    assert frames[1]["id"] == last_seq
    assert frames[-1]["data"]["seq"] == last_seq


async def test_deltas_are_not_merged_across_a_turn_boundary(mock_db, gw):
    sid = "sess-coalesce-turn-split"
    se.append_events(
        sid,
        [_delta(THINKING, f"a{i}|", "turn-a") for i in range(5)]
        + [_delta(THINKING, f"b{i}|", "turn-b") for i in range(5)],
    )

    frames = await _backfill(sid)

    assert [f["event"] for f in frames] == [THINKING, THINKING, "caught_up"]
    assert frames[0]["data"]["text"] == "".join(f"a{i}|" for i in range(5))
    assert frames[1]["data"]["text"] == "".join(f"b{i}|" for i in range(5))
    assert frames[0]["data"]["turn_id"] == "turn-a"
    assert frames[1]["data"]["turn_id"] == "turn-b"
    assert frames[0]["id"] == 5
    assert frames[1]["id"] == 10


async def test_a_tool_use_splits_two_delta_runs(mock_db, gw):
    sid = "sess-coalesce-tool"
    se.append_events(
        sid,
        [_delta(THINKING, f"a{i}|", "t1") for i in range(3)]
        + [{
            "type": "tool.use",
            "payload": {"tool_use_id": "tu1", "name": "Read", "input": {}},
            "turn_id": "t1",
        }]
        + [_delta(THINKING, f"b{i}|", "t1") for i in range(3)],
    )

    frames = await _backfill(sid)

    assert [f["event"] for f in frames] == [
        THINKING, "tool.use", THINKING, "caught_up",
    ]
    assert frames[0]["data"]["text"] == "a0|a1|a2|"
    assert frames[2]["data"]["text"] == "b0|b1|b2|"
    assert [frames[0]["id"], frames[1]["id"], frames[2]["id"]] == [3, 4, 7]


async def test_reconnect_from_cursor_skips_everything_at_or_below_it(mock_db, gw):
    sid = "sess-coalesce-resume"
    first_seq, last_seq = se.append_events(
        sid,
        [{"type": "message.user", "payload": {"content": "one"}, "turn_id": "t1"}]
        + [_delta(THINKING, f"a{i}|", "t1") for i in range(4)],
    )
    se.append_events(
        sid,
        [{"type": "message.user", "payload": {"content": "two"}, "turn_id": "t2"}]
        + [_delta(THINKING, f"b{i}|", "t2") for i in range(4)],
    )

    frames = await _backfill(sid, from_seq=last_seq)

    assert [f["event"] for f in frames] == ["message.user", THINKING, "caught_up"]
    ids = [f["id"] for f in frames if "id" in f]
    assert ids and min(ids) > last_seq
    assert frames[1]["data"]["text"] == "".join(f"b{i}|" for i in range(4))
