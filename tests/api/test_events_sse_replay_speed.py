"""History replay speed: chunked backfill, tool-input delta dedup, log epoch.

* The endpoint joins backfill frames into large chunks (one pass through the
  middleware stack per chunk, not per frame) and passes the live tail through
  frame by frame.
* A ``tool.input_delta`` whose input a later ``tool.use`` repeats is not
  replayed; an orphan run (no ``tool.use``) replays as one merged frame.
* ``caught_up`` names the log epoch; a resume with a stale epoch, or with a
  cursor whose row was compacted away, opens with ``event: reset`` and gets a
  fresh load.

The generator is consumed DIRECTLY — ASGI transports buffer the infinite SSE
body.
"""

import asyncio
import logging

import app.api.events as events_mod
from app.api.events import _batch_backfill, event_stream
from app.db import session_events as se
from tests.api.sse_helpers import parse_frame
from tests.shared_fixtures import reset_backfill_memo  # noqa: F401  (autouse fixture)


async def _until_caught_up(session_id, from_seq=0, epoch=None):
    """Every frame up to and including caught_up (reset included)."""
    frames = []

    async def _pump(agen):
        async for chunk in agen:
            if chunk.startswith(":"):
                continue
            parsed = parse_frame(chunk)
            frames.append(parsed)
            if parsed.get("event") == "caught_up":
                return

    agen = event_stream(session_id, from_seq, epoch)
    try:
        await asyncio.wait_for(_pump(agen), timeout=5.0)
    finally:
        await agen.aclose()
    return frames


def _assert_strictly_increasing(frames):
    ids = [f["id"] for f in frames if "id" in f]
    assert ids == sorted(set(ids)), ids


# ---------------------------------------------------------------------------
# A. Chunked backfill
# ---------------------------------------------------------------------------


async def test_backfill_frames_are_joined_and_live_frames_pass_through():
    backfill = [f"id: {i}\nevent: x\ndata: {{}}\n\n" for i in range(1, 4)]
    caught_up = 'event: caught_up\ndata: {"seq": 3}\n\n'
    live = ["id: 4\nevent: x\ndata: {}\n\n", ": keepalive\n\n", "id: 5\nevent: x\ndata: {}\n\n"]

    async def source():
        for chunk in backfill + [caught_up] + live:
            yield chunk

    out = [chunk async for chunk in _batch_backfill(source())]

    assert out == ["".join(backfill) + caught_up] + live


async def test_a_large_backfill_is_sent_in_bounded_chunks(monkeypatch):
    monkeypatch.setattr(events_mod, "_BACKFILL_CHUNK_BYTES", 100)
    frame = "id: 1\nevent: x\ndata: " + "a" * 40 + "\n\n"  # ~60 chars
    caught_up = 'event: caught_up\ndata: {"seq": 1}\n\n'

    async def source():
        for _ in range(5):
            yield frame
        yield caught_up

    out = [chunk async for chunk in _batch_backfill(source())]

    # Flushed each time the buffer passes the bound, then at caught_up.
    assert out == [frame * 2, frame * 2, frame + caught_up]


async def test_a_reset_is_sent_on_its_own_before_the_fresh_load():
    reset = 'event: reset\ndata: {"reason": "epoch", "epoch": "ep-2"}\n\n'
    backfill = [f"id: {i}\nevent: x\ndata: {{}}\n\n" for i in range(1, 4)]
    caught_up = 'event: caught_up\ndata: {"seq": 3, "epoch": "ep-2"}\n\n'

    async def source():
        for chunk in [reset] + backfill + [caught_up]:
            yield chunk

    out = [chunk async for chunk in _batch_backfill(source())]

    # The client restarts as soon as it sees the reset; holding it behind the
    # replay it is about to discard would only delay that.
    assert out == [reset, "".join(backfill) + caught_up]


async def test_closing_the_batcher_closes_the_inner_stream():
    closed = []

    async def source():
        try:
            yield 'event: caught_up\ndata: {"seq": 0}\n\n'
            while True:
                await asyncio.sleep(0)
                yield ": keepalive\n\n"
        finally:
            closed.append(True)

    agen = _batch_backfill(source())
    assert (await agen.__anext__()).startswith("event: caught_up")
    await agen.__anext__()
    await agen.aclose()

    assert closed == [True]


# ---------------------------------------------------------------------------
# B. tool.input_delta dedup
# ---------------------------------------------------------------------------


def _input_delta(tool_use_id, partial, turn="t1", name=None):
    payload = {"tool_use_id": tool_use_id, "partial_json": partial}
    if name:
        payload["name"] = name
    return {"type": "tool.input_delta", "payload": payload, "turn_id": turn}


def _tool_use(tool_use_id, turn="t1"):
    return {
        "type": "tool.use",
        "payload": {"tool_use_id": tool_use_id, "name": "Write", "input": {"a": 1}},
        "turn_id": turn,
    }


async def test_input_deltas_are_dropped_when_their_tool_use_follows(mock_db, gw):
    sid = "sess-tid-drop"
    se.append_events(
        sid,
        [{"type": "message.user", "payload": {"content": "go"}, "turn_id": "t1"}]
        + [_input_delta("tu1", f'{{"a":{i}', name="Write") for i in range(5)]
        + [_tool_use("tu1")]
        + [{"type": "tool.result", "payload": {"tool_use_id": "tu1"}, "turn_id": "t1"}],
    )

    frames = await _until_caught_up(sid)

    assert [f["event"] for f in frames] == [
        "message.user", "tool.use", "tool.result", "caught_up",
    ]
    assert [f["id"] for f in frames[:-1]] == [1, 7, 8]
    assert frames[-1]["data"]["seq"] == 8


async def test_orphan_input_deltas_replay_as_one_frame_per_run(mock_db, gw):
    """A background subagent launch emits input deltas and never a tool.use."""
    sid = "sess-tid-orphan"
    se.append_events(
        sid,
        [{"type": "message.user", "payload": {"content": "go"}, "turn_id": "t1"}]
        + [_input_delta("bg1", f"p{i}|", name="Task") for i in range(4)]
        + [{"type": "subagent.started", "payload": {"task_id": "bg1"}, "turn_id": "t1"}]
        + [_input_delta("tu2", f"q{i}|") for i in range(3)]
        + [_tool_use("tu2")],
    )

    frames = await _until_caught_up(sid)

    assert [f["event"] for f in frames] == [
        "message.user", "tool.input_delta", "subagent.started", "tool.use",
        "caught_up",
    ]
    orphan = frames[1]
    assert orphan["data"]["partial_json"] == "p0|p1|p2|p3|"
    assert orphan["data"]["tool_use_id"] == "bg1"
    assert orphan["data"]["name"] == "Task"
    # The merged frame carries the run's LAST seq, so a reconnect cursor
    # never lands inside it.
    assert orphan["id"] == 5
    assert orphan["data"]["seq"] == 5
    assert [f["id"] for f in frames[:-1]] == [1, 5, 6, 10]
    _assert_strictly_increasing(frames)


async def test_orphan_runs_for_different_tools_stay_separate(mock_db, gw):
    sid = "sess-tid-two-orphans"
    se.append_events(
        sid,
        [_input_delta("a", "a0|"), _input_delta("a", "a1|")]
        + [_input_delta("b", "b0|"), _input_delta("b", "b1|")],
    )

    frames = await _until_caught_up(sid)

    assert [f["event"] for f in frames] == [
        "tool.input_delta", "tool.input_delta", "caught_up",
    ]
    assert [f["data"]["partial_json"] for f in frames[:2]] == ["a0|a1|", "b0|b1|"]
    assert [f["id"] for f in frames[:2]] == [2, 4]


async def test_a_dropped_delta_still_separates_text_runs(mock_db, gw):
    """Dropping a delta must not merge the text on either side of it."""
    sid = "sess-tid-split"
    se.append_events(
        sid,
        [{"type": "assistant.text_delta", "payload": {"text": "a"}, "turn_id": "t1"}]
        + [_input_delta("tu1", "{")]
        + [{"type": "assistant.text_delta", "payload": {"text": "b"}, "turn_id": "t1"}]
        + [_tool_use("tu1")],
    )

    frames = await _until_caught_up(sid)

    assert [f["event"] for f in frames] == [
        "assistant.text_delta", "assistant.text_delta", "tool.use", "caught_up",
    ]
    assert [f["data"]["text"] for f in frames[:2]] == ["a", "b"]
    assert [f["id"] for f in frames[:-1]] == [1, 3, 4]


async def test_tool_use_on_a_later_page_still_drops_the_deltas(mock_db, gw, monkeypatch):
    monkeypatch.setattr(events_mod, "_PAGE", 3)
    sid = "sess-tid-paged"
    se.append_events(
        sid,
        [_input_delta("tu1", f"{i}") for i in range(7)] + [_tool_use("tu1")],
    )

    frames = await _until_caught_up(sid)

    assert [f["event"] for f in frames] == ["tool.use", "caught_up"]
    assert frames[0]["id"] == 8
    assert frames[1]["data"]["seq"] == 8


async def test_resume_past_the_tool_use_does_not_lose_orphan_state(mock_db, gw):
    """Deltas after the resume cursor whose tool.use is BEFORE it replay."""
    sid = "sess-tid-resume"
    se.append_events(sid, [_tool_use("tu1")])
    se.append_events(sid, [_input_delta("tu1", "late")])

    frames = await _until_caught_up(sid, from_seq=1)

    assert [f["event"] for f in frames] == ["tool.input_delta", "caught_up"]
    assert frames[0]["id"] == 2


async def test_live_tail_input_deltas_are_not_dropped(mock_db, gw, monkeypatch):
    monkeypatch.setattr(events_mod, "KEEPALIVE_SECONDS", 0.05)
    sid = "sess-tid-live"
    se.append_events(sid, [{"type": "message.user", "payload": {}, "turn_id": "t1"}])

    agen = event_stream(sid, 0)
    frames = []
    try:
        async def _pump():
            async for chunk in agen:
                if chunk.startswith(":"):
                    continue
                parsed = parse_frame(chunk)
                frames.append(parsed)
                if parsed.get("event") == "caught_up":
                    se.append_events(
                        sid, [_input_delta("tu1", "x"), _input_delta("tu1", "y"), _tool_use("tu1")]
                    )
                if parsed.get("event") == "tool.use":
                    return

        await asyncio.wait_for(_pump(), timeout=5.0)
    finally:
        await agen.aclose()

    assert [f["event"] for f in frames] == [
        "message.user", "caught_up",
        "tool.input_delta", "tool.input_delta", "tool.use",
    ]


# ---------------------------------------------------------------------------
# C. Log epoch
# ---------------------------------------------------------------------------


def _mk_session(session_id):
    from app.db import database

    database.create_session(session_id, "default")


async def test_caught_up_carries_the_log_epoch(mock_db, gw):
    sid = "sess-ep-cu"
    se.append_events(sid, [{"type": "turn.started"}, {"type": "turn.completed"}])

    frames = await _until_caught_up(sid)

    assert frames[-1]["event"] == "caught_up"
    assert frames[-1]["data"] == {"seq": 2, "epoch": se.get_log_epoch(sid)}
    assert frames[-1]["data"]["epoch"] != se.EMPTY_LOG_EPOCH


async def test_an_empty_log_still_has_an_epoch(mock_db, gw):
    frames = await _until_caught_up("sess-ep-empty")

    assert frames == [
        {"event": "caught_up", "data": {"seq": 0, "epoch": se.EMPTY_LOG_EPOCH}}
    ]


async def test_resume_with_matching_epoch_is_not_reset(mock_db, gw):
    sid = "sess-ep-match"
    se.append_events(sid, [{"type": "turn.started"}, {"type": "turn.completed"}])
    epoch = (await _until_caught_up(sid))[-1]["data"]["epoch"]
    se.append_events(sid, [{"type": "turn.started"}])

    frames = await _until_caught_up(sid, from_seq=2, epoch=epoch)

    assert [f["event"] for f in frames] == ["turn.started", "caught_up"]
    assert frames[0]["id"] == 3
    assert frames[-1]["data"] == {"seq": 3, "epoch": epoch}


async def test_resume_after_clear_is_reset_then_fully_reloaded(mock_db, gw):
    from app.db import database

    sid = "sess-ep-clear"
    _mk_session(sid)
    se.append_events(sid, [{"type": "turn.started"}, {"type": "turn.completed"}])
    old_epoch = (await _until_caught_up(sid))[-1]["data"]["epoch"]

    database.clear_session_conversation(sid)
    se.append_events(sid, [{"type": "message.user", "payload": {"content": "new"}}])
    new_epoch = se.get_log_epoch(sid)
    assert new_epoch != old_epoch

    # The client's cursor (2) is past the new log's tail (1); without the
    # epoch it would silently get nothing.
    frames = await _until_caught_up(sid, from_seq=2, epoch=old_epoch)

    assert frames[0] == {
        "event": "reset", "data": {"reason": "epoch", "epoch": new_epoch},
    }
    assert "id" not in frames[0]
    assert [f["event"] for f in frames[1:]] == ["message.user", "caught_up"]
    assert frames[1]["id"] == 1
    assert frames[-1]["data"] == {"seq": 1, "epoch": new_epoch}


async def test_resume_without_an_epoch_behaves_as_before(mock_db, gw):
    from app.db import database

    sid = "sess-ep-none"
    _mk_session(sid)
    se.append_events(sid, [{"type": "turn.started"}, {"type": "turn.completed"}])
    database.clear_session_conversation(sid)
    se.append_events(sid, [{"type": "message.user", "payload": {}}])

    frames = await _until_caught_up(sid, from_seq=2)

    assert [f["event"] for f in frames] == ["caught_up"]
    assert frames[0]["data"] == {"seq": 2, "epoch": se.get_log_epoch(sid)}


async def test_resume_inside_a_compacted_run_is_reset(mock_db, gw):
    """A cursor that sat inside a delta run merged by compaction would
    otherwise receive the whole merged text again."""
    sid = "sess-ep-compacted"
    se.append_events(
        sid,
        [{"type": "turn.started", "turn_id": "t1"}]
        + [{"type": "assistant.text_delta", "payload": {"text": f"{i}"}, "turn_id": "t1"}
           for i in range(5)]
        + [{"type": "turn.completed", "turn_id": "t1"}],
    )
    epoch = se.get_log_epoch(sid)
    se.compact_session_deltas(sid, keep_tail_events=0)
    # Compaction never changes the epoch...
    assert se.get_log_epoch(sid) == epoch

    # ...but a live client that stopped at seq 3 (mid-run) must reload.
    frames = await _until_caught_up(sid, from_seq=3, epoch=epoch)

    assert frames[0] == {"event": "reset", "data": {"reason": "cursor", "epoch": epoch}}
    assert [f["event"] for f in frames[1:]] == [
        "turn.started", "assistant.text_delta", "turn.completed", "caught_up",
    ]
    assert frames[2]["data"]["text"] == "01234"

    # A cursor on a surviving row resumes normally.
    frames = await _until_caught_up(sid, from_seq=6, epoch=epoch)
    assert [f["event"] for f in frames] == ["turn.completed", "caught_up"]


# ---------------------------------------------------------------------------
# E. Measurement
# ---------------------------------------------------------------------------


async def test_fresh_replay_logs_one_summary_line(mock_db, gw, caplog):
    sid = "sess-measure"
    se.append_events(sid, [{"type": "turn.started"}, {"type": "turn.completed"}])

    with caplog.at_level(logging.INFO, logger="app.api.events"):
        await _until_caught_up(sid)
        await _until_caught_up(sid, from_seq=1)

    lines = [r.getMessage() for r in caplog.records if "SSE feed replay" in r.getMessage()]
    assert len(lines) == 1  # the resume does not log one
    assert f"session={sid}" in lines[0]
    assert "frames=2" in lines[0]
    assert "elided=0" in lines[0]
    assert "bytes=" in lines[0] and "caught_up_ms=" in lines[0]
