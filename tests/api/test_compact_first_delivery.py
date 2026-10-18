"""Delivery edge cases from PR #171 review, using the real intake and Stop path."""
import time
import pytest

from tests.api.test_compact_first import compact_chat, send
from app.core import query_session_lifecycle, session_handoff, turn_manager
from app.core.task_registry import task_registry
from app.db import database, session_events, session_moves, turns


def query(**overrides):
    return {"prompt": "Keep these exact words", "client_message_id": "client-" + "d" * 32, **overrides}


def test_pair_admission_itself_deduplicates_under_its_lock(compact_chat):
    kwargs = {"prompt": query()["prompt"], "command_id": query()["client_message_id"],
              "payload": {"compact_first": True}}
    first = turns.enqueue_turn("compact-chat", "user", **kwargs)
    retry = turns.enqueue_turn("compact-chat", "user", **kwargs)
    assert first["id"] == retry["id"]
    assert len(turns.get_queued_turns("compact-chat")) == 2


@pytest.mark.parametrize("terminal", [False, True])
def test_lost_response_retry_admits_only_one_pair(compact_chat, terminal):
    payload = {"then_send": query()}
    first = send(compact_chat, payload).json()
    if terminal:
        turns.start_turn(first["turn_id"])
        turns.settle_turn(first["turn_id"], "completed")
        turns.start_turn(first["queued_turn_id"])
        turns.settle_turn(first["queued_turn_id"], "completed")
    retry = send(compact_chat, payload)
    assert retry.status_code == 200, retry.text
    assert retry.json() == first
    with turns.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM turns WHERE session_id=%s", ("compact-chat",))
        assert len(cur.fetchall()) == 2


@pytest.mark.parametrize("compact_started", [False, True])
async def test_stop_keeps_followup_as_durable_unsent_message(compact_chat, compact_started):
    payload = {"then_send": query(upload_paths=["/uploads/proof.png"])}
    pair = send(compact_chat, payload).json()
    if compact_started:
        assert turns.start_turn(pair["turn_id"])
    stopped = await query_session_lifecycle.enter_user_stop("compact-chat")
    assert stopped["queued_aborted"] == (1 if compact_started else 2)
    row = turns.get_turn(pair["queued_turn_id"])
    assert row["state"] == "aborted"
    assert turns.pop_ready("compact-chat") is None
    messages = database.get_session_messages("compact-chat")
    assert [m["content"] for m in messages] == ["Keep these exact words"]
    assert messages[0]["metadata"]["upload_paths"] == ["/uploads/proof.png"]
    assert row["payload"]["upload_paths"] == ["/uploads/proof.png"]
    events = session_events.get_events("compact-chat")
    unsent = [e for e in events if e["event_type"] == "message.user"]
    assert len(unsent) == 1
    assert unsent[0]["payload"]["content"] == messages[0]["content"]
    assert unsent[0]["payload"]["not_sent"] is True
    assert unsent[0]["payload"]["upload_paths"] == ["/uploads/proof.png"]
    # A second Stop/reload/lost-ack retry cannot duplicate the transcript or
    # resume the stopped message. Every client replays this same durable event.
    await query_session_lifecycle.enter_user_stop("compact-chat")
    retry = send(compact_chat, payload)
    assert retry.status_code == 200, retry.text
    assert retry.json() == pair
    assert task_registry.is_user_stopped("compact-chat")
    assert len(database.get_session_messages("compact-chat")) == 1
    assert len([e for e in session_events.get_events("compact-chat") if e["event_type"] == "message.user"]) == 1


async def test_stop_preserves_remote_upload_blobs(compact_chat):
    row = turns.enqueue_turn("compact-chat", "user", prompt="See /uploads/photo.png",
                             payload={"compact_first": True, "upload_blob_ids": ["blob-proof"]})
    await query_session_lifecycle.enter_user_stop("compact-chat")
    assert turns.get_turn(row["id"])["state"] == "aborted"
    assert database.get_session_messages("compact-chat")[0]["metadata"]["upload_blob_ids"] == ["blob-proof"]


def test_stop_locks_counter_before_session_and_notifies_only_after_commit(compact_chat, monkeypatch):
    from contextlib import contextmanager
    pair = send(compact_chat, {"then_send": query()}).json()
    real_get_db = turns.get_db
    trace = []

    class Cursor:
        def __init__(self, cursor): self.cursor = cursor
        def __getattr__(self, name): return getattr(self.cursor, name)
        def execute(self, sql, *args, **kwargs):
            trace.append(sql)
            return self.cursor.execute(sql, *args, **kwargs)

    class Connection:
        def __init__(self, conn):
            self.conn = conn
            self._connection = getattr(conn, "_connection", conn)
        def __getattr__(self, name): return getattr(self.conn, name)
        def cursor(self, *args, **kwargs): return Cursor(self.conn.cursor(*args, **kwargs))

    @contextmanager
    def traced_db():
        with real_get_db() as conn:
            yield Connection(conn)
        trace.append("committed")

    def notified(sid, scope):
        assert sid == "compact-chat" and scope == "session_events"
        assert trace[-1] == "committed"
        trace.append("notified")

    monkeypatch.setattr(turns, "get_db", traced_db)
    monkeypatch.setattr(session_events, "notify_session_write", notified)
    turns.remove_queued_turn(pair["queued_turn_id"], preserve_compact_first=True)
    counter = next(i for i, sql in enumerate(trace) if "FROM session_event_counters" in sql and "FOR UPDATE" in sql)
    session = next(i for i, sql in enumerate(trace) if "FROM sessions" in sql and "FOR UPDATE" in sql)
    abort = next(i for i, sql in enumerate(trace) if "UPDATE turns SET state='aborted'" in sql)
    assert counter < session < abort
    assert trace[-2:] == ["committed", "notified"]


async def test_stop_record_failure_rolls_back_withdrawal(compact_chat, monkeypatch):
    pair = send(compact_chat, {"then_send": query()}).json()
    turns.start_turn(pair["turn_id"])
    def unavailable(*args, **kwargs):
        raise RuntimeError("event log unavailable")
    monkeypatch.setattr(session_events, "append_events", unavailable)
    await query_session_lifecycle.enter_user_stop("compact-chat")
    assert turns.get_turn(pair["queued_turn_id"])["state"] == "queued"
    assert not database.get_session_messages("compact-chat")


def begin_move(monkeypatch):
    pending = session_handoff.PendingHandoff("other-worker", "Other", "", time.time())
    session_moves.save("compact-chat", target_id=pending.target_id, label=pending.label,
                       note="", requested_by="owner", requested_at=pending.requested_at,
                       expires_at=pending.expires_at)
    monkeypatch.setitem(session_handoff._pending, "compact-chat", pending)
    return pending


def test_move_racing_admission_is_accepted_and_retries_do_not_hold_twice(compact_chat, monkeypatch):
    original = turn_manager._submit_blocking
    def move_then_submit(*args, **kwargs):
        if not session_handoff.holds("compact-chat"):
            begin_move(monkeypatch)
        return original(*args, **kwargs)
    monkeypatch.setattr(turn_manager, "_submit_blocking", move_then_submit)
    payload = {"then_send": query()}
    first = send(compact_chat, payload)
    assert first.status_code == 200, first.text
    assert first.json()["turn_id"] is None
    assert first.json()["held"] is True
    assert first.json()["queued_turn_id"].startswith("held-")
    assert send(compact_chat, payload).json() == first.json()
    held = session_handoff.held_view("compact-chat")
    assert len(held) == 1
    assert held[0]["payload"]["compact_first"]
    # Cancel the move and exercise the real replay path, not a fabricated row.
    session_handoff._pending["compact-chat"].holding = False
    monkeypatch.setattr(turn_manager, "_submit_blocking", original)
    assert session_handoff._submit_here("compact-chat", held) == (1, [])
    assert [r["prompt"] for r in turns.get_queued_turns("compact-chat")] == ["/compact", query()["prompt"]]


async def test_forwarded_pair_held_during_move_is_accepted(compact_chat, monkeypatch):
    from app.network import control
    begin_move(monkeypatch)
    monkeypatch.setattr(control, "_require_owned", lambda sid: database.get_session(sid))
    body = {"session_id": "compact-chat", "prompt": query()["prompt"], "profile_id": "default",
            "source": "user", "owner_send": True, "payload": {"compact_first": True}}
    first = await control._submit_forwarded_turn(body, "sender", query()["client_message_id"])
    retry = await control._submit_forwarded_turn(body, "sender", query()["client_message_id"])
    assert first == retry
    assert first["held"] and first["compact_turn_id"] is None
    assert first["turn_id"].startswith("held-")
    assert len(session_handoff.held_view("compact-chat")) == 1
