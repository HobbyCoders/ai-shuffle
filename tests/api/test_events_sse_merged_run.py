"""A live reader whose rows a mirrored merged run replaced is reset (#1278).

A mirror holding the first fragments of a run receives the owner's merged row
for it: the fragments are replaced by one row at the run's last seq, and the
log's epoch (its first row's id) does not change. A client already sent those
fragments must not then be sent the merged row as if it were new text: the
shared reader's log check after the mirror write finds its cursor row gone and
resets every client, which replays the run once.
"""
import asyncio
import json

import pytest

import app.api.events as events_mod
from app.api.events import event_stream
from app.db import database
from app.db import session_events as se
from app.db.base import get_db
from tests.api.sse_helpers import collect_frames, parse_frame


@pytest.fixture(autouse=True)
def clean_tails():
    yield
    for tail in list(events_mod._tails.values()):
        tail.close()
    events_mod._tails.clear()


async def _connect(session_id, from_seq=0):
    stream = event_stream(session_id, from_seq)
    frames = []
    while True:
        frame = parse_frame(await anext(stream))
        if frame.get("event") == "caught_up":
            return stream, frame["data"], frames
        frames.append(frame)


def _mirror_merged_run(session_id, first_seq, last_seq, text):
    """Write the owner's merged row the way a mirror apply does."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM session_events WHERE session_id = %s AND seq = %s", (session_id, first_seq))
        head = dict(cur.fetchone())
        record = {**head, "seq": last_seq,
                  "payload": {"text": text, se.MERGED_FROM_KEY: first_seq}}
        fitted = se.fit_mirrored_delta(cur, session_id, last_seq, record)
        assert fitted is not None
        # materialize upserts on (session_id, seq).
        cur.execute("DELETE FROM session_events WHERE session_id = %s AND seq = %s", (session_id, last_seq))
        cur.execute(
            "INSERT INTO session_events (id, session_id, seq, run_id, turn_id, event_type, payload, command_id, "
            "created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (fitted["id"], session_id, last_seq, fitted.get("run_id"), fitted.get("turn_id"),
             fitted["event_type"], json.dumps(fitted["payload"]), fitted.get("command_id"),
             fitted.get("created_at")))


async def test_reader_on_a_replaced_fragment_resets_instead_of_repeating_text(mock_db, gw, monkeypatch):
    monkeypatch.setattr(events_mod, "KEEPALIVE_SECONDS", 0.05)
    monkeypatch.setattr(events_mod, "_push_delivery_healthy", lambda tail: True)
    sid = "merged-under-reader"
    database.create_session(sid, "default", title="Disposable merged run")
    se.append_events(sid, [{"type": "message.user", "payload": {"content": "hi"}}])
    se.append_events(sid, [{"type": "assistant.text_delta", "payload": {"text": t}} for t in ("A", "B", "C")])
    epoch = se.get_log_epoch(sid)

    stream, caught, _ = await _connect(sid)
    assert caught["seq"] == 4
    await asyncio.sleep(0.05)
    try:
        _mirror_merged_run(sid, 2, 7, "ABCDEF")
        assert se.get_log_epoch(sid) == epoch  # same log: the epoch alone cannot tell
        gw._handle_payload(json.dumps({"session_id": sid, "max_seq": 7, "recheck_log": True}))
        frame = (await collect_frames(stream, 1, timeout=1.0))[0]
        assert frame["event"] == "reset" and frame["data"] == {"reason": "cursor", "epoch": epoch}
    finally:
        await stream.aclose()

    # The client's reload replays the run once.
    replay, caught, frames = await _connect(sid)
    try:
        assert caught["seq"] == 7
        assert [(f["event"], f["data"].get("text") or f["data"].get("content")) for f in frames] == [
            ("message.user", "hi"), ("assistant.text_delta", "ABCDEF")]
        assert se.MERGED_FROM_KEY not in frames[1]["data"]
    finally:
        await replay.aclose()


async def test_reader_past_the_merged_run_is_not_reset(mock_db, gw, monkeypatch):
    monkeypatch.setattr(events_mod, "KEEPALIVE_SECONDS", 0.05)
    monkeypatch.setattr(events_mod, "_push_delivery_healthy", lambda tail: True)
    sid = "merged-below-reader"
    database.create_session(sid, "default", title="Disposable merged run")
    se.append_events(sid, [{"type": "message.user", "payload": {"content": "hi"}}])
    se.append_events(sid, [{"type": "assistant.text_delta", "payload": {"text": t}} for t in ("A", "B", "C")])

    stream, caught, _ = await _connect(sid)
    assert caught["seq"] == 4
    await asyncio.sleep(0.05)
    try:
        # The whole run was held: the merged row lands on the reader's own row.
        _mirror_merged_run(sid, 2, 4, "ABC")
        gw._handle_payload(json.dumps({"session_id": sid, "max_seq": 4, "recheck_log": True}))
        _, last = se.append_events(sid, [{"type": "message.assistant", "payload": {"content": "done"}}])
        gw._handle_payload(json.dumps({"session_id": sid, "max_seq": last}))
        frame = (await collect_frames(stream, 1, timeout=1.0))[0]
        assert (frame["event"], frame["data"]["content"]) == ("message.assistant", "done")
    finally:
        await stream.aclose()


async def test_merge_committed_between_the_check_and_the_read_resets_the_reader(mock_db, gw, monkeypatch):
    # S5: the probe sees the cursor row, the merge commits, the read finds ABCDEF.
    monkeypatch.setattr(events_mod, "KEEPALIVE_SECONDS", 0.05)
    monkeypatch.setattr(events_mod, "_push_delivery_healthy", lambda tail: True)
    sid = "merged-mid-check"
    database.create_session(sid, "default", title="Disposable merged run")
    se.append_events(sid, [{"type": "message.user", "payload": {"content": "hi"}}])
    se.append_events(sid, [{"type": "assistant.text_delta", "payload": {"text": t}} for t in ("A", "B", "C")])
    epoch = se.get_log_epoch(sid)
    stream, caught, _ = await _connect(sid)
    assert caught["seq"] == 4
    await asyncio.sleep(0.05)
    real = events_mod.get_log_check

    def racing_check(session_id, seq):
        result = real(session_id, seq)
        _mirror_merged_run(sid, 2, 7, "ABCDEF")
        return result

    monkeypatch.setattr(events_mod, "get_log_check", racing_check)
    try:
        gw._handle_payload(json.dumps({"session_id": sid, "max_seq": 7, "recheck_log": True}))
        frame = (await collect_frames(stream, 1, timeout=1.0))[0]
        assert frame["event"] == "reset" and frame["data"] == {"reason": "cursor", "epoch": epoch}
    finally:
        await stream.aclose()


async def test_resume_racing_a_merge_resets_instead_of_replaying_it(mock_db, gw, monkeypatch):
    sid = "merged-mid-resume"
    database.create_session(sid, "default", title="Disposable merged run")
    se.append_events(sid, [{"type": "message.user", "payload": {"content": "hi"}}])
    se.append_events(sid, [{"type": "assistant.text_delta", "payload": {"text": t}} for t in ("A", "B", "C")])
    epoch = se.get_log_epoch(sid)
    real = events_mod._resume_check

    def racing_check(session_id, from_seq, sent_epoch):
        result = real(session_id, from_seq, sent_epoch)
        _mirror_merged_run(sid, 2, 7, "ABCDEF")
        return result

    monkeypatch.setattr(events_mod, "_resume_check", racing_check)
    stream = event_stream(sid, 4, epoch)
    try:
        frame = parse_frame(await anext(stream))
        assert frame["event"] == "reset" and frame["data"] == {"reason": "cursor", "epoch": epoch}
    finally:
        await stream.aclose()


async def test_reconciling_client_does_not_get_a_merged_row_over_its_own_text(mock_db):
    sid = "merged-mid-reconcile"
    database.create_session(sid, "default", title="Disposable merged run")
    se.append_events(sid, [{"type": "message.user", "payload": {"content": "hi"}}])
    se.append_events(sid, [{"type": "assistant.text_delta", "payload": {"text": t}} for t in ("A", "B", "C")])
    _mirror_merged_run(sid, 2, 7, "ABCDEF")
    with pytest.raises(events_mod._CursorCrossed):
        async for _ in events_mod._tail_from_db(sid, 4):
            pass
    # From the top nothing was sent yet, so the merged row is the text.
    assert [seq async for seq, _, _ in events_mod._tail_from_db(sid, 0)] == [1, 7]


async def test_reinstalled_log_resets_connected_readers(mock_db, gw, monkeypatch):
    # A reinstall can rewrite rows behind a cursor whose own row survives.
    monkeypatch.setattr(events_mod, "KEEPALIVE_SECONDS", 0.05)
    monkeypatch.setattr(events_mod, "_push_delivery_healthy", lambda tail: True)
    sid = "reinstalled-under-reader"
    database.create_session(sid, "default", title="Disposable reinstall")
    se.append_events(sid, [{"type": "message.user", "payload": {"content": "hi"}}])
    epoch = se.get_log_epoch(sid)
    stream, caught, _ = await _connect(sid)
    assert caught["seq"] == 1 and caught["epoch"] == epoch
    await asyncio.sleep(0.05)
    try:
        with get_db() as conn:
            se.note_log_rewritten(conn.cursor(), sid)
        rewritten = se.get_reader_epoch(sid)
        gw._handle_payload(json.dumps({"session_id": sid, "max_seq": 0, "recheck_log": True}))
        frame = (await collect_frames(stream, 1, timeout=1.0))[0]
        assert frame["event"] == "reset" and frame["data"] == {"reason": "epoch", "epoch": rewritten}
    finally:
        await stream.aclose()
    # Mirrors compare the first row's id with each other: the token stays local.
    assert se.get_log_epoch(sid) == epoch != rewritten


async def test_resume_at_a_surviving_cursor_is_reset_after_a_rewrite(mock_db):
    sid = "rewritten-under-cursor"
    database.create_session(sid, "default", title="Disposable resume")
    se.append_events(sid, [{"type": "message.user", "payload": {"content": "hi"}}])
    epoch = se.get_log_epoch(sid)
    assert events_mod._resume_check(sid, 1, epoch) == (epoch, None)
    with get_db() as conn:
        se.note_log_rewritten(conn.cursor(), sid)
    current, reason = events_mod._resume_check(sid, 1, epoch)
    assert reason == "epoch" and current == se.get_reader_epoch(sid) != epoch
    # The client now holds the new log: it resumes normally, however often it reconnects.
    assert events_mod._resume_check(sid, 1, current) == (current, None)
    assert se.get_reader_watermark(sid)["epoch"] == current
    assert se.get_watermark(sid)["epoch"] == epoch


async def test_a_first_load_names_its_log_for_an_interrupted_resume(mock_db, monkeypatch):
    sid = "named-on-first-load"
    database.create_session(sid, "default", title="Disposable first load")
    se.append_events(sid, [{"type": "message.user", "payload": {"content": "hi"}}])

    async def allowed(request, session_id):
        return None

    monkeypatch.setattr(events_mod, "require_transcript_session", allowed)
    first = await events_mod.stream_session_events(request=None, session_id=sid, from_seq=0, epoch=None, token="t")
    assert first.headers["x-log-epoch"] == se.get_reader_epoch(sid)
    resumed = await events_mod.stream_session_events(request=None, session_id=sid, from_seq=1, epoch=None, token="t")
    assert "x-log-epoch" not in resumed.headers
