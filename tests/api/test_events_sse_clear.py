"""An in-place clear must not strand a client on the chat's shared SSE reader.

Issue #797: the clear restarts the event log at seq 1, but the session's shared
reader (``_SessionTail``) kept the old log's cursor. A page that reconnected
after the clear while another connection still held that reader saw every new
event as already sent, so the reply never arrived. Only the PostgreSQL LISTEN
transport is stubbed: the clear's own committed ``pg_notify`` payload is what
the gateway receives.
"""
import asyncio
import json

import pytest

import app.api.events as events_mod
import app.db.sessions as sessions_db
from app.api.events import event_stream
from app.db import database, session_events as se
from tests.api.sse_helpers import collect_frames, parse_frame


@pytest.fixture(autouse=True)
def clean_tails():
    yield
    for tail in list(events_mod._tails.values()):
        tail.close()
    events_mod._tails.clear()


class _NotifyCursor:
    """Records ``pg_notify`` calls (absent on the SQLite shim) instead of running them."""

    def __init__(self, cursor, notifications):
        self.cursor, self.notifications = cursor, notifications

    def __getattr__(self, name):
        return getattr(self.cursor, name)

    def execute(self, sql, params=()):
        if "pg_notify" in sql:
            self.notifications.append(params[1])
            return None
        return self.cursor.execute(sql, params)


def _clear(monkeypatch, session_id):
    notifications = []
    real = sessions_db.get_cursor
    monkeypatch.setattr(sessions_db, "get_cursor", lambda conn: _NotifyCursor(real(conn), notifications))
    database.clear_session_conversation(session_id)
    monkeypatch.setattr(sessions_db, "get_cursor", real)
    return notifications


async def _connect(session_id):
    stream = event_stream(session_id, 0)
    while True:
        frame = parse_frame(await anext(stream))
        if frame.get("event") == "caught_up":
            return stream, frame["data"]


@pytest.mark.parametrize("deliver_clear_before_reconnect", [False, True])
async def test_reply_after_clear_reaches_a_client_on_the_shared_reader(
    mock_db, gw, monkeypatch, deliver_clear_before_reconnect
):
    monkeypatch.setattr(events_mod, "KEEPALIVE_SECONDS", 0.05)
    # Push delivery counts as healthy, so only the pushed path (not the
    # keepalive poll) can deliver within the timeout below.
    monkeypatch.setattr(events_mod, "_push_delivery_healthy", lambda tail: True)
    sid = "cleared-while-open"
    database.create_session(sid, "default", title="Disposable clear")
    se.append_events(sid, [{"type": "message.user", "payload": {"content": f"old-{n}"}} for n in range(3)])

    # Another device (or the page's own connection that is still closing)
    # holds the shared reader, seeded at the old log's seq 3.
    other, caught = await _connect(sid)
    assert caught["seq"] == 3
    await asyncio.sleep(0.05)
    streams = [other]
    try:
        notifications = _clear(monkeypatch, sid)
        assert notifications, "the clear wakes the shared readers"
        assert json.loads(notifications[-1]) == {"session_id": sid, "max_seq": 0, "recheck_log": True}
        if deliver_clear_before_reconnect:
            for payload in notifications:
                gw._handle_payload(payload)
            reset = (await collect_frames(other, 1, timeout=1.0))[0]
            assert reset["event"] == "reset" and reset["data"]["reason"] == "epoch"

        # The page reconnects on ``user_session_cleared`` to the empty log.
        page, caught = await _connect(sid)
        streams.append(page)
        assert caught == {"seq": 0, "epoch": se.EMPTY_LOG_EPOCH}
        if not deliver_clear_before_reconnect:
            # The race: the page attached to the old reader before it heard.
            for payload in notifications:
                gw._handle_payload(payload)
            reset = (await collect_frames(other, 1, timeout=1.0))[0]
            assert reset["event"] == "reset" and reset["data"]["reason"] == "epoch"
        await asyncio.sleep(0.1)

        # The next turn restarts at seq 1.
        _, last = se.append_events(sid, [
            {"type": "message.user", "payload": {"content": "after clearing"}},
            {"type": "message.user", "payload": {"content": "the reply"}},
        ])
        gw._handle_payload(json.dumps({"session_id": sid, "max_seq": last}))
        rows = await collect_frames(page, 2, timeout=1.0)
        assert [(row["id"], row["data"]["content"]) for row in rows] == [
            (1, "after clearing"), (2, "the reply"),
        ]

        # Later events keep arriving pushed, from the live reader.
        _, last = se.append_events(sid, [{"type": "message.user", "payload": {"content": "next"}}])
        gw._handle_payload(json.dumps({"session_id": sid, "max_seq": last}))
        row = (await collect_frames(page, 1, timeout=1.0))[0]
        assert (row["id"], row["data"]["content"]) == (3, "next")
        assert not events_mod._tails[sid].closed
    finally:
        for stream in streams:
            await stream.aclose()
