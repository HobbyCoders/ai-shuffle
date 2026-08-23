"""Replay elision — long sessions load their latest compacted state.

Opening a session replays the event log from seq 0; on a long transcript that
streams (and repaints) everything the engine already compacted away, and the
auto-scroll chases the replay to the bottom. The SSE backfill now elides the
compacted-away prefix behind a synthetic system.compaction_boundary marker,
with a hard count cap for logs the fluid ladder never touched. The generator
is consumed DIRECTLY — ASGI transports buffer the infinite SSE body.
"""

import json

import pytest

import app.api.events as events_mod
import app.core.history_backfill as hb
from app.api.events import (
    MAX_REPLAY_EVENTS,
    _boundary_frame,
    _replay_elision,
    _turn_start_nodes,
    event_stream,
)
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


def _seed_log(session_id: str, turns: int, per_turn: int = 3) -> None:
    """``turns`` complete turns: message.user + (per_turn-1) filler events."""
    for t in range(turns):
        se.append_events(
            session_id,
            [{"type": "message.user", "content": f"turn {t}"}]
            + [{"type": "message.assistant", "content": f"reply {t}"}]
            * (per_turn - 1),
        )


# ─── unit: elision decision ──────────────────────────────────────────────────


async def test_short_log_is_not_elided(mock_db):
    _seed_log("sess-el-short", 10)
    assert _replay_elision("sess-el-short") == (0, 0, "")


async def test_long_log_elides_to_the_next_turn_start(mock_db):
    sid = "sess-el-cap"
    turns = MAX_REPLAY_EVENTS // 3 + 50
    _seed_log(sid, turns)
    cutoff, elided, reason = _replay_elision(sid)

    assert reason == "capped"
    # Cutoff IS a message.user row (pairs can never straddle it).
    rows = se.get_events(sid, cutoff - 1, 1)
    assert rows[0]["seq"] == cutoff
    assert rows[0]["event_type"] == "message.user"
    # Everything before it is reported as elided and nothing else.
    assert elided == cutoff - 1
    total = se.get_max_seq(sid)
    assert total - cutoff < MAX_REPLAY_EVENTS + 3  # kept tail is bounded


async def test_elision_never_fires_when_no_turn_start_remains(mock_db):
    sid = "sess-el-mid"
    # A cap-sized log whose tail is one giant turn: no message.user row at or
    # after the bound → replay must fall back to full rather than split a turn.
    se.append_events(
        sid,
        [{"type": "message.user", "content": "only turn"}]
        + [
            {"type": "message.assistant", "content": f"chunk {i}"}
            for i in range(MAX_REPLAY_EVENTS + 20)
        ],
    )
    assert _replay_elision(sid) == (0, 0, "")


# ─── unit: fluid compaction boundary mapping ─────────────────────────────────


FLUID_SDK = "ses_elision0001"


def _msg(role, blocks):
    return {"role": role, "content": blocks, "provider_meta": {}}


def _user(text):
    return _msg("user", [{"kind": "text", "text": text}])


def _assistant(text):
    return _msg("assistant", [{"kind": "text", "text": text}])


def _seed_fluid_session(session_id: str, nodes) -> None:
    from psycopg2.extras import Json

    from app.db.base import get_cursor, get_db

    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            "INSERT INTO fluid_sessions (session_id, head_node_id, created_at) "
            "VALUES (%s, %s, %s)",
            (FLUID_SDK, nodes[-1][0], 1000.0),
        )
        for i, (node_id, parent_id, message) in enumerate(nodes):
            cursor.execute(
                "INSERT INTO fluid_session_nodes "
                "(session_id, node_id, parent_id, ts, message) "
                "VALUES (%s, %s, %s, %s, %s)",
                (FLUID_SDK, node_id, parent_id, 1000.0 + i, Json(message)),
            )


def _link_app_session(session_id: str) -> None:
    from app.db import sessions as sessions_db

    sessions_db.create_session(session_id, "default")
    sessions_db.update_session(session_id, sdk_session_id=FLUID_SDK)


def test_turn_start_nodes_mirror_fluid_is_turn_start(mock_db):
    nodes = [
        ("n1", None, _user("first")),
        ("n2", "n1", _assistant("reply")),
        ("n3", "n2", _user("second")),
    ]
    _seed_fluid_session("unused-app", nodes)
    starts = _turn_start_nodes(FLUID_SDK)
    assert [n["node_id"] for n in starts] == ["n1", "n3"]


def test_latest_compaction_boundary_reads_persisted_promotions(mock_db):
    nodes = [
        ("n1", None, _user("first")),
        ("n2", "n1", _assistant("reply")),
        ("n3", "n2", _user("second")),
        ("n4", "n3", _assistant("final")),
    ]
    _seed_fluid_session("unused-app-2", nodes)
    from psycopg2.extras import Json

    from app.db.base import get_cursor, get_db

    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            "INSERT INTO fluid_session_events (session_id, seq, event) "
            "VALUES (%s, %s, %s)",
            (
                FLUID_SDK,
                1,
                Json({
                    "_cls": "ContextCompaction",
                    "tier": 2,
                    "reason": "trigger",
                    "trimmed": [],
                    "spans": [["n1", "n2", "digest"]],
                    "reclaimed_chars": 500,
                    "session_id": FLUID_SDK,
                    "node_id": None,
                    "turn_id": "t1",
                    "seq": -1,
                }),
            ),
        )

    from app.core.provider.fluid_transcript import latest_compaction_boundary

    boundary = latest_compaction_boundary(FLUID_SDK)
    assert boundary == {
        "node_id": "n2",
        "ts": 1000.0 + 1,
        "spans": 1,
        "tier": 2,
    }


def test_boundary_frame_carries_no_id_line():
    frame = _boundary_frame(42, 41, "compacted")
    assert not frame.startswith("id:")
    parsed = _parse("id: 999\n" + frame)  # an id line from another frame must win
    assert parsed["id"] == 999
    data = _parse(frame)["data"]
    assert data["type"] == "system.compaction_boundary"
    assert data["subtype"] == "compact_boundary"
    assert data["data"]["elidedEvents"] == 41


# ─── stream behavior ─────────────────────────────────────────────────────────


async def test_full_replay_of_a_long_log_starts_with_the_boundary_marker(
    mock_db, gw
):
    sid = "sess-el-stream"
    _seed_log(sid, MAX_REPLAY_EVENTS // 3 + 50)

    raw = []

    async def _pump(agen):
        async for chunk in agen:
            if chunk.startswith(":"):
                continue
            parsed = _parse(chunk)
            if parsed.get("event") == "caught_up":
                raw.append(parsed)
                return
            raw.append(parsed)

    agen = event_stream(sid, 0)
    try:
        import asyncio

        await asyncio.wait_for(_pump(agen), timeout=5.0)
    finally:
        await agen.aclose()

    assert raw[0]["event"] == "system.compaction_boundary"
    assert "id" not in raw[0] or isinstance(raw[0]["id"], int) is False
    assert raw[1]["event"] == "message.user"  # first KEPT event is a turn start
    assert raw[-1]["event"] == "caught_up"


async def test_reconnect_from_cursor_gets_no_elision(mock_db, gw):
    sid = "sess-el-resume"
    _seed_log(sid, 300)

    agen = event_stream(sid, 5)
    try:
        frames = []
        async for chunk in agen:
            if chunk.startswith(":"):
                continue
            parsed = _parse(chunk)
            if parsed.get("event") == "caught_up":
                break
            frames.append(parsed)
            if len(frames) >= 2:
                break
    finally:
        await agen.aclose()

    assert all(f["event"] != "system.compaction_boundary" for f in frames)
    assert [f["id"] for f in frames] == [6, 7]
