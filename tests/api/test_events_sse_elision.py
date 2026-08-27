"""Compaction-aware SSE hydration for long sessions."""

import asyncio
import json

import pytest
from psycopg2.extras import Json

import app.api.events as events_mod
import app.core.history_backfill as history_backfill
from app.api.events import (
    _boundary_frame,
    _compacted_replay_start,
    event_stream,
)
from app.core.event_gateway import EventGateway
from app.core.provider.fluid_transcript import latest_compaction_boundary
from app.db import session_events as session_events_db
from app.db import sessions as sessions_db
from app.db.base import get_cursor, get_db


@pytest.fixture(autouse=True)
def _reset_backfill_memo():
    history_backfill._reset_memo()
    yield
    history_backfill._reset_memo()


@pytest.fixture
def gateway(monkeypatch):
    value = EventGateway()
    monkeypatch.setattr(value, "_ensure_listener", lambda: None)
    monkeypatch.setattr(events_mod, "get_gateway", lambda: value)
    return value


def _message(role: str, text: str) -> dict:
    return {
        "role": role,
        "content": [{"kind": "text", "text": text}],
        "provider_meta": {},
    }


def _seed_fluid(sdk_session_id: str, nodes: list[tuple[str, str | None, dict]]) -> None:
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            "INSERT INTO fluid_sessions (session_id, head_node_id, created_at) "
            "VALUES (%s, %s, %s)",
            (sdk_session_id, nodes[-1][0], 1000.0),
        )
        turn = 0
        for offset, (node_id, parent_id, message) in enumerate(nodes):
            cursor.execute(
                "INSERT INTO fluid_session_nodes "
                "(session_id, node_id, parent_id, ts, message) "
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    sdk_session_id,
                    node_id,
                    parent_id,
                    1000.0 + offset,
                    Json(message),
                ),
            )
            if message.get("role") == "user":
                cursor.execute(
                    "INSERT INTO fluid_session_events (session_id, seq, event) "
                    "VALUES (%s, %s, %s)",
                    (
                        sdk_session_id,
                        turn + 1,
                        Json(
                            {
                                "_cls": "TurnStarted",
                                "session_id": sdk_session_id,
                                "node_id": node_id,
                                "turn_id": f"turn-{turn}",
                                "seq": turn + 1,
                                "model": "test-model",
                                "provider": "test-provider",
                            }
                        ),
                    ),
                )
                turn += 1


def _append_compaction(
    sdk_session_id: str,
    seq: int,
    spans: list[list[str]],
    *,
    tier: int = 2,
) -> None:
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            "INSERT INTO fluid_session_events (session_id, seq, event) "
            "VALUES (%s, %s, %s)",
            (
                sdk_session_id,
                100 + seq,
                Json(
                    {
                        "_cls": "ContextCompaction",
                        "session_id": sdk_session_id,
                        "turn_id": "turn-compact",
                        "node_id": None,
                        "seq": 100 + seq,
                        "tier": tier,
                        "reason": "trigger",
                        "trimmed": [],
                        "spans": spans,
                        "reclaimed_chars": 500,
                    }
                ),
            ),
        )


def _link_app_session(app_session_id: str, sdk_session_id: str) -> None:
    sessions_db.create_session(app_session_id, "default")
    sessions_db.update_session(app_session_id, sdk_session_id=sdk_session_id)


def _seed_app_turns(
    session_id: str, count: int, *, steer_during: int | None = None
) -> None:
    for turn in range(count):
        events = [
            {
                "type": "message.user",
                "payload": {"content": f"prompt {turn}"},
                "turn_id": f"turn-{turn}",
            },
            {
                "type": "tool.use",
                "payload": {"tool_use_id": f"tool-{turn}", "name": "Read"},
                "turn_id": f"turn-{turn}",
            },
        ]
        if steer_during == turn:
            events.append(
                {
                    "type": "message.user",
                    "payload": {"content": "mid-turn steer"},
                    "turn_id": f"turn-{turn}",
                }
            )
        events.append(
            {
                "type": "tool.result",
                "payload": {"tool_use_id": f"tool-{turn}", "content": "ok"},
                "turn_id": f"turn-{turn}",
            }
        )
        session_events_db.append_events(session_id, events)


def _parse(frame: str) -> dict:
    parsed: dict = {}
    for line in frame.splitlines():
        if line.startswith("id: "):
            parsed["id"] = int(line[4:])
        elif line.startswith("event: "):
            parsed["event"] = line[7:]
        elif line.startswith("data: "):
            parsed["data"] = json.loads(line[6:])
    return parsed


def _four_turn_path() -> list[tuple[str, str | None, dict]]:
    return [
        ("n1", None, _message("user", "one")),
        ("n2", "n1", _message("assistant", "reply one")),
        ("n3", "n2", _message("user", "two")),
        ("n4", "n3", _message("assistant", "reply two")),
        ("n5", "n4", _message("user", "three")),
        ("n6", "n5", _message("assistant", "reply three")),
        ("n7", "n6", _message("user", "four")),
        ("n8", "n7", _message("assistant", "reply four")),
    ]


def test_boundary_uses_latest_contiguous_promoted_prefix(mock_db):
    sdk = "ses_compactpref1"
    _seed_fluid(sdk, _four_turn_path())
    _append_compaction(sdk, 1, [["n1", "n2", "digest"]])
    _append_compaction(sdk, 2, [["n3", "n4", "digest"]])
    # A later epoch promotion overlaps the already-covered prefix.
    _append_compaction(sdk, 3, [["n1", "n4", "epoch"]], tier=3)

    boundary = latest_compaction_boundary(sdk)

    assert boundary is not None
    assert boundary["node_id"] == "n4"
    assert boundary["turns"] == 2
    assert boundary["tier"] == 3


def test_uncompacted_session_reads_neither_log_nor_node_tree(mock_db, monkeypatch):
    """The session-open cost of a session that never compacted must be one
    existence probe.

    Every session open calls this, and a ``ContextCompaction`` promotion is rare
    — so the unfiltered ``SELECT ... ORDER BY seq`` over the whole event log,
    plus the full node-tree walk, were being paid on every open to discover
    there was nothing to elide (half a second and a disk spill on a long
    session). Both are booby-trapped here: reaching either one is the bug.
    """
    import app.core.provider.fluid_transcript as ft

    sdk = "ses_nocompact01"
    _seed_fluid(sdk, _four_turn_path())

    def _boom(*_a, **_kw):
        raise AssertionError("read the whole session before probing for compaction")

    monkeypatch.setattr(ft, "_event_rows", _boom)
    monkeypatch.setattr(ft, "load_fluid_path", _boom)

    assert latest_compaction_boundary(sdk) is None


def test_boundary_lookup_ignores_unrelated_event_classes(mock_db):
    """The filtered read must still see everything the analysis needs.

    Only ContextCompaction (the promoted spans) and TurnStarted (the node ->
    durable turn-id bridge) are read; the ToolUse/ToolResult/Usage traffic that
    makes up the bulk of a real log is filtered out in the database, and its
    absence must not change the answer.
    """
    sdk = "ses_noisylog001"
    _seed_fluid(sdk, _four_turn_path())
    with get_db() as conn:
        cursor = get_cursor(conn)
        for offset, cls in enumerate(("ToolUse", "ToolResult", "Usage", "TextDelta")):
            cursor.execute(
                "INSERT INTO fluid_session_events (session_id, seq, event) "
                "VALUES (%s, %s, %s)",
                (
                    sdk,
                    500 + offset,
                    Json({"_cls": cls, "session_id": sdk, "payload": "x" * 256}),
                ),
            )
    _append_compaction(sdk, 1, [["n1", "n2", "digest"]])

    boundary = latest_compaction_boundary(sdk)

    assert boundary is not None
    assert boundary["node_id"] == "n2"
    assert boundary["turns"] == 1
    # The bridge still resolves — TurnStarted survived the filter.
    assert boundary["first_retained_turn_id"] == "turn-1"


def test_tier_one_only_has_no_replay_cutoff(mock_db):
    sdk = "ses_trimonly001"
    _seed_fluid(sdk, _four_turn_path())
    _append_compaction(sdk, 1, [], tier=1)

    assert latest_compaction_boundary(sdk) is None


def test_cross_path_span_fails_closed(mock_db):
    sdk = "ses_crosspath01"
    _seed_fluid(sdk, _four_turn_path())
    _append_compaction(sdk, 1, [["n1", "abandoned-node", "digest"]])

    assert latest_compaction_boundary(sdk) is None


@pytest.mark.parametrize(
    "span",
    [
        ["n1", "n3", "epoch"],  # ends at the next turn start
        ["n2", "n4", "epoch"],  # starts in the middle of a turn
        ["n1", "n4", "digest"],  # digest may cover exactly one turn only
        ["n1", "n2", "unknown"],
    ],
)
def test_malformed_or_partial_span_fails_closed(mock_db, span):
    sdk = f"ses_badspan{span[0]}{span[1]}{span[2]}"
    _seed_fluid(sdk, _four_turn_path())
    _append_compaction(sdk, 1, [span])

    assert latest_compaction_boundary(sdk) is None


def test_gap_does_not_hide_uncompacted_turns(mock_db):
    sdk = "ses_gapcompact1"
    _seed_fluid(sdk, _four_turn_path())
    _append_compaction(sdk, 1, [["n1", "n2", "digest"]])
    _append_compaction(sdk, 2, [["n5", "n6", "digest"]])

    boundary = latest_compaction_boundary(sdk)

    assert boundary is not None
    assert boundary["node_id"] == "n2"
    assert boundary["turns"] == 1


def test_compaction_maps_to_first_complete_retained_turn(mock_db):
    app_session = "app-compacted-map"
    sdk = "ses_compactmap01"
    _seed_fluid(sdk, _four_turn_path())
    _append_compaction(sdk, 1, [["n1", "n4", "epoch"]], tier=3)
    _link_app_session(app_session, sdk)
    _seed_app_turns(app_session, 4)

    first_kept, elided, tier = _compacted_replay_start(app_session)

    assert (first_kept, elided, tier) == (7, 6, 3)
    first_row = session_events_db.get_events(app_session, first_kept - 1, 1)[0]
    assert first_row["event_type"] == "message.user"
    assert first_row["payload"]["content"] == "prompt 2"


def test_mid_turn_steer_is_not_mistaken_for_a_turn_start(mock_db):
    app_session = "app-compacted-steer"
    sdk = "ses_compactsteer"
    _seed_fluid(sdk, _four_turn_path())
    _append_compaction(sdk, 1, [["n1", "n4", "epoch"]], tier=3)
    _link_app_session(app_session, sdk)
    _seed_app_turns(app_session, 4, steer_during=0)

    first_kept, elided, tier = _compacted_replay_start(app_session)

    assert (first_kept, elided, tier) == (8, 7, 3)
    first_row = session_events_db.get_events(app_session, first_kept - 1, 1)[0]
    assert first_row["payload"]["content"] == "prompt 2"


def test_rewound_event_log_fails_closed(mock_db):
    app_session = "app-compacted-rewound"
    sdk = "ses_compactrewnd"
    _seed_fluid(sdk, _four_turn_path())
    _append_compaction(sdk, 1, [["n1", "n4", "epoch"]], tier=3)
    _link_app_session(app_session, sdk)
    _seed_app_turns(app_session, 4)
    session_events_db.append_events(
        app_session,
        [{"type": "transcript.rewound", "payload": {"from_seq": 4}}],
    )

    assert _compacted_replay_start(app_session) == (0, 0, 0)


def test_no_compaction_keeps_full_history(mock_db):
    app_session = "app-no-compaction"
    sdk = "ses_nocompact01"
    _seed_fluid(sdk, _four_turn_path())
    _link_app_session(app_session, sdk)
    _seed_app_turns(app_session, 4)

    assert _compacted_replay_start(app_session) == (0, 0, 0)


def test_turn_count_mismatch_fails_closed(mock_db):
    app_session = "app-turn-mismatch"
    sdk = "ses_turnmismatch"
    _seed_fluid(sdk, _four_turn_path())
    _append_compaction(sdk, 1, [["n1", "n4", "epoch"]], tier=3)
    _link_app_session(app_session, sdk)
    _seed_app_turns(app_session, 2)

    assert _compacted_replay_start(app_session) == (0, 0, 0)


def test_boundary_frame_has_no_sse_id():
    frame = _boundary_frame("app-session", 7, 6, 3)
    parsed = _parse(frame)

    assert "id" not in parsed
    assert parsed["event"] == "system.compaction_boundary"
    assert parsed["data"]["seq"] == 6
    assert parsed["data"]["data"]["elidedEvents"] == 6


@pytest.mark.asyncio
async def test_fresh_stream_starts_at_compacted_boundary(mock_db, gateway):
    app_session = "app-compacted-stream"
    sdk = "ses_compstream01"
    _seed_fluid(sdk, _four_turn_path())
    _append_compaction(sdk, 1, [["n1", "n4", "epoch"]], tier=3)
    _link_app_session(app_session, sdk)
    _seed_app_turns(app_session, 4)

    frames: list[dict] = []
    stream = event_stream(app_session, 0)
    try:
        async def consume() -> None:
            async for chunk in stream:
                parsed = _parse(chunk)
                frames.append(parsed)
                if parsed.get("event") == "caught_up":
                    return

        await asyncio.wait_for(consume(), timeout=3.0)
    finally:
        await stream.aclose()

    assert frames[0]["event"] == "system.compaction_boundary"
    assert frames[1]["event"] == "message.user"
    assert frames[1]["id"] == 7
    assert frames[-1]["event"] == "caught_up"


@pytest.mark.asyncio
async def test_reconnect_never_reapplies_elision(mock_db, gateway):
    app_session = "app-compacted-resume"
    sdk = "ses_compresume1"
    _seed_fluid(sdk, _four_turn_path())
    _append_compaction(sdk, 1, [["n1", "n4", "epoch"]], tier=3)
    _link_app_session(app_session, sdk)
    _seed_app_turns(app_session, 4)

    frames: list[dict] = []
    stream = event_stream(app_session, 5)
    try:
        async for chunk in stream:
            parsed = _parse(chunk)
            if parsed.get("event") == "caught_up":
                break
            frames.append(parsed)
    finally:
        await stream.aclose()

    assert all(frame.get("event") != "system.compaction_boundary" for frame in frames)
    assert frames[0]["id"] == 6
