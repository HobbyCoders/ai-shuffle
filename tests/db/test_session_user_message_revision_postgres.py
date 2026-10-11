"""Opt-in real PostgreSQL upgrade and user-message transaction checks."""
import os
from datetime import datetime

import pytest

from app.db import base, database, sessions
from app.db.schema_postgres import create_postgres_schema

pytestmark = pytest.mark.skipif(
    os.environ.get("ALLOW_REAL_PG") != "1", reason="requires disposable TEST_POSTGRES_DSN"
)


@pytest.fixture(scope="module", autouse=True)
def pg_schema():
    assert os.environ.get("TEST_POSTGRES_DSN")
    base.init_pool(min_conn=1, max_conn=2)
    try:
        with base.get_db() as conn:
            create_postgres_schema(conn.cursor())
        yield
    finally:
        base.close_pool()


def test_upgrade_nullable_timestamp_and_transactional_write():
    # The explicit disposable DB is owned by this test run, never a worker DB.
    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO sessions (id, title) VALUES ('revision-old', 'kept')")
        cursor.execute("ALTER TABLE sessions DROP COLUMN last_user_message_at")
    database.init_database()
    database.init_database()
    assert database.get_session("revision-old")["last_user_message_at"] is None
    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT data_type, is_nullable FROM information_schema.columns "
                       "WHERE table_name='sessions' AND column_name='last_user_message_at'")
        assert dict(cursor.fetchone()) == {"data_type": "timestamp with time zone", "is_nullable": "YES"}
    prompt = sessions.add_session_message("revision-old", "user", "initial")
    initial = database.get_session("revision-old")["last_user_message_at"]
    if isinstance(initial, str):
        initial = datetime.fromisoformat(initial)
    assert initial == datetime.fromisoformat(prompt["created_at"])
    with base.transaction() as conn:
        steer = sessions.add_session_message("revision-old", "user", "steer", _conn=conn)
    latest = database.get_session("revision-old")["last_user_message_at"]
    if isinstance(latest, str):
        latest = datetime.fromisoformat(latest)
    assert latest == datetime.fromisoformat(steer["created_at"])
    assert latest > initial
    with pytest.raises(ValueError):
        with base.transaction() as conn:
            sessions.add_session_message("revision-old", "user", "rolled back", _conn=conn)
            raise ValueError("abort")
    assert [row["content"] for row in sessions.get_session_messages("revision-old")] == ["initial", "steer"]


@pytest.mark.asyncio
async def test_authoritative_recorder_delivery_dedup_and_provenance():
    from app.core.turn_recorder import TurnRecorder
    from app.db import session_events

    database.create_session("pg-events", None)
    recorder = await TurnRecorder.maybe_create("pg-events")
    assert recorder is not None
    try:
        await recorder.start("API/connector/relay prompt", source="user")
        first = database.get_session("pg-events")["last_user_message_at"]
        assert first is not None
        assert database.get_session_messages("pg-events") == []
        assert await recorder.record_steered_user_message("Peer note", source="agent_message")
        assert database.get_session("pg-events")["last_user_message_at"] == first
        assert await recorder.record_steered_user_message("Owner answer", source="user")
        latest = database.get_session("pg-events")["last_user_message_at"]
        assert latest > first
        event = next(e for e in session_events.get_events("pg-events", 0)
                     if e["event_type"] == "message.user")
        # Retrying an already committed stable event cannot look like an answer.
        session_events.append_events("pg-events", [{"id": event["id"], "type": "message.user",
                                                     "payload": event["payload"]}])
        assert database.get_session("pg-events")["last_user_message_at"] == latest
        with pytest.raises(ValueError):
            with base.transaction() as conn:
                session_events.append_events("pg-events", [{"type": "message.user", "payload": {}}], _conn=conn)
                raise ValueError("abort")
        assert database.get_session("pg-events")["last_user_message_at"] == latest
    finally:
        await recorder.close()
