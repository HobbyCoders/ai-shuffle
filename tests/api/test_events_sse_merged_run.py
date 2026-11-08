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
