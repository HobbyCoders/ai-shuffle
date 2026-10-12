"""Question-row locks must coexist with composer foreign-key checks on PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event, current_thread
from uuid import uuid4

import pytest

from app.db import base, composer_suggestions, database, session_events, turns
from tests.db.postgres_harness import pg_schema, requires_real_pg  # noqa: F401

pytestmark = requires_real_pg


@pytest.mark.parametrize("activity", ["turn.started", "turn.completed"])
def test_question_revision_lock_allows_concurrent_suggestion_save(monkeypatch, activity):
    sid = "question-lock-" + uuid4().hex
    database.create_session(sid, None)
    turn = turns.enqueue_turn(sid, "user", prompt="Question lock regression")
    assert turns.start_turn(turn["id"], None, "runner")
    assert turns.settle_turn(turn["id"], "completed")
    session_events.append_events(sid, [
        {"type": "message.assistant", "turn_id": turn["id"], "payload": {"content": "First?"}},
        {"type": "turn.completed", "turn_id": turn["id"], "payload": {}},
    ])
    before = database.get_session(sid)
    save_locked, append_at_advisory = Event(), Event()
    real_lock = composer_suggestions._lock

    @contextmanager
    def timed_db():
        with base.get_db() as conn:
            conn.cursor().execute("SET LOCAL statement_timeout = '5s'")
            yield conn

    def ordered_lock(cursor, session_id):
        if current_thread().name.startswith("question-save"):
            real_lock(cursor, session_id)
            save_locked.set()
            assert append_at_advisory.wait(10), "append never reached its advisory lock"
        else:
            # append has already projected the question and holds its row lock.
            append_at_advisory.set()
            real_lock(cursor, session_id)

    monkeypatch.setattr(composer_suggestions, "get_db", timed_db)
    monkeypatch.setattr(session_events, "get_db", timed_db)
    monkeypatch.setattr(composer_suggestions, "_lock", ordered_lock)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="question-save") as saver, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="question-append") as appender:
        save = saver.submit(composer_suggestions.save, sid, turn["id"], "Follow up?")
        try:
            assert save_locked.wait(10), "save never acquired the advisory lock"
            append = appender.submit(session_events.append_events, sid, [
                {"type": "message.assistant", "turn_id": turn["id"], "payload": {"content": "Next?"}},
                {"type": activity, "turn_id": turn["id"], "payload": {}},
            ])
            # FOR UPDATE deadlocks here: save needs FK KEY SHARE while append
            # holds the session row and waits for save's composer advisory lock.
            assert save.result(timeout=15) is True
            assert append.result(timeout=15) == (3, 4)
        finally:
            append_at_advisory.set()
    after = database.get_session(sid)
    assert after["last_assistant_question_seq"] == 3
    assert after["updated_at"] > before["updated_at"]
    if activity == "turn.started":
        assert composer_suggestions.get(sid) is None
    else:
        assert composer_suggestions.get(sid)["suggestion"] == "Follow up?"
