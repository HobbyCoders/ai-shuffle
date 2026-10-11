"""User-message revisions are persisted and delivered without opening the chat."""
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.db import base, database, sessions
from app.core.global_events import global_events
from app.core.models import Session, SessionWithMessages
from tests.core.test_global_events import Socket, flush
from tests.db import schema_lifecycle_helpers as lifecycle


def stamp(sid):
    value = database.get_session(sid)["last_user_message_at"]
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def test_prompt_then_steer_and_non_user_messages():
    database.create_session("revision", "default")
    assert stamp("revision") is None
    prompt = database.add_session_message("revision", "user", "Start")
    first = stamp("revision")
    assert first == datetime.fromisoformat(prompt["created_at"])
    assert first.utcoffset().total_seconds() == 0
    database.add_session_message("revision", "assistant", "Which one?")
    assert stamp("revision") == first
    steer = database.add_session_message("revision", "user", "The first one")
    assert stamp("revision") == datetime.fromisoformat(steer["created_at"])
    assert stamp("revision") > first


def test_event_batch_stamps_latest_inserted_user_not_batch_end():
    from app.db import session_events

    sid = "batch-seq"
    database.create_session(sid, "default")
    events = [
        {"id": "u1", "type": "message.user", "payload": {}},
        {"id": "u2", "type": "message.user", "payload": {}},
        {"type": "message.user", "payload": {"source": "agent_message"}},
        {"type": "message.assistant", "payload": {"content": "Which one?"}},
    ]
    with patch.object(global_events, "lifecycle") as publish:
        assert session_events.append_events(sid, events) == (1, 4)
        assert database.get_session(sid)["last_user_message_seq"] == 2
        assert publish.call_count == 1
        assert publish.call_args.args[1]["updates"]["last_user_message_seq"] == 2
        # A duplicate after a new user in the same allocation must not win.
        session_events.append_events(sid, [
            {"type": "message.user", "payload": {}}, events[1],
        ])
        assert database.get_session(sid)["last_user_message_seq"] == 5
        assert publish.call_args.args[1]["updates"]["last_user_message_seq"] == 5
        # Legacy-only delivery has no event position, so must preserve it.
        database.add_session_message(sid, "user", "Legacy")
        assert database.get_session(sid)["last_user_message_seq"] == 5
        assert publish.call_args.args[1]["updates"]["last_user_message_seq"] == 5


def test_clear_resets_event_revision_for_new_log_epoch():
    from app.db import session_events

    sid = "clear-seq"
    database.create_session(sid, "default")
    event = {"type": "message.user", "payload": {}}
    with patch.object(global_events, "lifecycle") as publish:
        session_events.append_events(sid, [event] * 20)
        old_row = database.get_session(sid)
        old_update = publish.call_args.args[1]["updates"]
        assert old_row["last_user_message_epoch"] == session_events.get_log_epoch(sid)
        sessions.clear_session_conversation(sid)
        cleared = database.get_session(sid)
        assert cleared["last_user_message_seq"] is None
        assert cleared["last_user_message_epoch"] is None
        session_events.append_events(sid, [event, {
            "type": "message.assistant", "payload": {"content": "New question?"},
        }])
        epoch = session_events.get_log_epoch(sid)
        current = database.get_session(sid)
        assert current["last_user_message_seq"] == 1
        assert current["last_user_message_epoch"] == epoch
        # A delayed snapshot AND a delayed WS update have larger numbers but
        # belong to a different log, so neither can answer this question.
        for stale in (old_row, old_update):
            assert stale["last_user_message_seq"] == 20
            assert stale["last_user_message_epoch"] != epoch
        session_events.append_events(sid, [event])
        answer = publish.call_args.args[1]["updates"]
        assert answer["last_user_message_epoch"] == epoch
        assert answer["last_user_message_seq"] == 3


def test_event_sequence_wins_over_wall_clock():
    from app.db import session_events

    sid = "clock-seq"
    database.create_session(sid, "default")
    with base.get_db() as conn:
        conn.cursor().execute(
            "UPDATE sessions SET last_user_message_at = %s WHERE id = %s",
            ("2099-01-01T00:00:00+00:00", sid),
        )
    session_events.append_events(sid, [{"type": "message.user", "payload": {}}])
    assert database.get_session(sid)["last_user_message_seq"] == 1
    assert stamp(sid).year != 2099


def test_batch_and_command():
    for sid in ("one", "two"):
        database.create_session(sid, "default")
    rows = sessions.add_session_messages_batch([
        {"session_id": "one", "role": "user", "content": "Prompt"},
        {"session_id": "one", "role": "assistant", "content": "Reply"},
        {"session_id": "two", "role": "command", "content": "/help"},
    ])
    assert stamp("one") == stamp("two") == datetime.fromisoformat(rows[0]["created_at"])


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("rollback", [False, True])
def test_notification_waits_for_outer_commit(batch, rollback):
    database.create_session("transaction", "default")
    with patch.object(global_events, "lifecycle") as publish:
        try:
            with base.transaction() as conn:
                if batch:
                    sessions.add_session_messages_batch([
                        {"session_id": "transaction", "role": "user", "content": "hello"}
                    ], _conn=conn)
                else:
                    sessions.add_session_message("transaction", "user", "hello", _conn=conn)
                publish.assert_not_called()
                if rollback:
                    raise ValueError("rollback")
        except ValueError:
            assert rollback
        assert publish.call_count == (0 if rollback else 1)
    assert (stamp("transaction") is None) == rollback
    assert bool(database.get_session_messages("transaction")) != rollback


@pytest.mark.asyncio
@pytest.mark.parametrize("seq", [None, 17])
async def test_message_added_frame_preserves_optional_event_sequence(seq):
    from app.core.sync_engine import sync_engine

    message = {"id": "message", "content": "Hello"}
    if seq is not None:
        message["seq"] = seq
    with patch.object(sync_engine, "broadcast_event", new_callable=AsyncMock) as broadcast:
        await sync_engine.broadcast_message_added("session", message)
    assert broadcast.call_args.args[0].data["message"]["seq"] == seq
    assert ("seq" in message) == (seq is not None)  # no caller mutation


@pytest.mark.asyncio
async def test_delivered_steer_broadcasts_to_unopened_tabs():
    from app.core.session_runtime import SessionRuntime

    database.create_session("steer", "default")
    database.add_session_message("steer", "user", "Initial prompt")
    before = stamp("steer")
    sockets = [Socket(), Socket(), Socket()]
    ids = [global_events.register(ws, kind, owner) for ws, kind, owner in zip(
        sockets, ["admin", "admin", "api_user"], ["tab-a", "tab-b", "other"]
    )]
    try:
        runtime = SessionRuntime({"id": "turn", "session_id": "steer", "profile_id": "default"})
        with patch("app.core.sync_engine.sync_engine.broadcast_message_added", new=AsyncMock()):
            await runtime._persist_user_message("Answer from another device")
        await flush()
        assert stamp("steer") > before
        for ws in sockets[:2]:
            updates = [m for m in ws.sent if m.get("type") == "user_session_updated"]
            assert len(updates) == 1
            assert updates[0]["session_id"] == "steer"
            assert datetime.fromisoformat(updates[0]["updates"]["last_user_message_at"]) == stamp("steer")
        assert not any(m.get("session_id") == "steer" for m in sockets[2].sent)
    finally:
        for socket_id in ids:
            await global_events.unregister(socket_id)


def test_list_and_detail_payload_models():
    database.create_session("api-revision", "default")
    database.add_session_message("api-revision", "user", "hello")
    row = database.get_session("api-revision")
    for model in (Session, SessionWithMessages):
        payload = model(**row).model_dump(mode="json")
        assert datetime.fromisoformat(payload["last_user_message_at"].replace("Z", "+00:00")) == stamp("api-revision")
    from app.network import policy
    assert "last_user_message_at" in policy.COLLECTIONS["sessions"].fields
    assert "last_user_message_seq" in policy.COLLECTIONS["sessions"].fields
    assert "last_user_message_epoch" in policy.COLLECTIONS["sessions"].fields


def test_schema_upgrade_preserves_old_sessions_as_null(tmp_path):
    raw = lifecycle.connect(tmp_path / "upgrade.db")
    try:
        lifecycle.boot(raw)
        raw.execute("INSERT INTO sessions (id, title) VALUES ('old', 'preserved')")
        raw.execute("ALTER TABLE sessions DROP COLUMN last_user_message_at")
        raw.execute("ALTER TABLE sessions DROP COLUMN last_user_message_seq")
        raw.execute("ALTER TABLE sessions DROP COLUMN last_user_message_epoch")
        raw.execute("DELETE FROM system_settings WHERE key = ?", (database.MIGRATIONS_FINGERPRINT_KEY,))
        raw.commit()
        lifecycle.boot(raw)
        lifecycle.boot(raw)
        row = raw.execute("SELECT title, last_user_message_at, last_user_message_seq, last_user_message_epoch FROM sessions WHERE id='old'").fetchone()
        assert tuple(row) == ("preserved", None, None, None)
    finally:
        raw.close()


@pytest.mark.parametrize("path", [
    "/api/v1/sessions",
    "/api/v1/sessions/paginated",
    "/api/v1/sessions/cursor",
    "/api/v1/sessions/api-revision",
])
def test_http_payload_includes_persisted_revision(path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.sessions import router
    from app.api.auth import require_auth

    database.create_session("api-revision", "default")
    database.add_session_message("api-revision", "user", "hello")
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: "test-token"
    with TestClient(app) as client:
        response = client.get(path)
    assert response.status_code == 200, response.text
    payload = response.json()
    row = payload[0] if isinstance(payload, list) else payload.get("items", [payload])[0]
    assert datetime.fromisoformat(row["last_user_message_at"].replace("Z", "+00:00")) == stamp("api-revision")


def test_commit_failure_does_not_publish(monkeypatch):
    from unittest.mock import MagicMock

    conn = MagicMock()
    del conn._connection
    conn.commit.side_effect = RuntimeError("commit failed")
    monkeypatch.setattr(base, "get_connection", lambda: conn)
    monkeypatch.setattr(base, "_close_connection", lambda c: None)
    callback = MagicMock()
    with pytest.raises(RuntimeError, match="commit failed"):
        with base.get_db() as owned:
            base.after_commit(owned, callback)
    callback.assert_not_called()
    conn.rollback.assert_called_once()


def test_nested_transaction_callbacks_belong_to_their_connection(monkeypatch):
    from unittest.mock import MagicMock

    outer, inner = MagicMock(), MagicMock()
    connections = iter((outer, inner))
    # Real psycopg connections have no _connection attribute.
    del outer._connection
    del inner._connection
    monkeypatch.setattr(base, "get_connection", lambda: next(connections))
    monkeypatch.setattr(base, "_close_connection", lambda c: None)
    delivered = []
    with base.transaction() as first:
        with base.get_db() as second:
            base.after_commit(first, lambda: delivered.append("outer"))
            base.after_commit(second, lambda: delivered.append("inner"))
        assert delivered == ["inner"]
    assert delivered == ["inner", "outer"]


@pytest.mark.parametrize("rollback", [False, True])
def test_event_revision_is_atomic_and_stable_id_retry_is_silent(rollback):
    from app.db import session_events

    sid = "event-revision"
    database.create_session(sid, "default")
    event = {"id": "delivered-event", "type": "message.user", "payload": {"content": "Answer"}}
    with patch.object(global_events, "lifecycle") as publish:
        try:
            with base.transaction() as conn:
                session_events.append_events(sid, [event], _conn=conn)
                publish.assert_not_called()
                if rollback:
                    raise ValueError("abort")
        except ValueError:
            assert rollback
        assert publish.call_count == (0 if rollback else 1)
        assert (stamp(sid) is None) == rollback
        assert database.get_session(sid)["last_user_message_seq"] == (None if rollback else 1)
        assert database.get_session(sid)["last_user_message_epoch"] == (None if rollback else "delivered-event")
        if not rollback:
            timestamp = stamp(sid)
            publish.reset_mock()
            session_events.append_events(sid, [event])
            publish.assert_not_called()
            assert stamp(sid) == timestamp
            assert database.get_session(sid)["last_user_message_seq"] == 1
            assert len(session_events.get_events(sid, 0)) == 1


@pytest.mark.parametrize("batch", [False, True])
def test_autonomous_legacy_rows_preserve_revision(batch):
    database.create_session("notes", "default")
    for source in ("agent_message", "wakeup", "voice_system_note"):
        message = {"session_id": "notes", "role": "user", "content": "not an answer",
                   "metadata": {"source": source}}
        if batch:
            sessions.add_session_messages_batch([message])
        else:
            sessions.add_session_message(**message)
    assert stamp("notes") is None
