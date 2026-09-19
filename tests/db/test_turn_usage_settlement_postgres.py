"""Opt-in settlement transaction contracts on disposable PostgreSQL."""
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("ALLOW_REAL_PG") != "1",
                                reason="requires disposable TEST_POSTGRES_DSN")


@pytest.fixture(scope="module", autouse=True)
def pg_schema():
    from app.db import base
    from app.db.schema_postgres import create_postgres_schema
    assert os.environ.get("TEST_POSTGRES_DSN")
    base.init_pool(min_conn=1, max_conn=6)
    try:
        with base.get_db() as conn:
            create_postgres_schema(conn.cursor())
        yield
    finally:
        base.close_pool()


@pytest.fixture
def booking():
    from app.db import base
    sid = "pg-settlement-" + uuid4().hex
    with base.get_db() as conn:
        conn.cursor().execute("INSERT INTO sessions (id) VALUES (%s)", (sid,))
    return sid, dict(tokens_in=17, tokens_out=4, cache_read_tokens=11,
                     cache_creation_tokens=7, cost_usd=0.01, duration_ms=30,
                     model_calls=1, turn_increment=1)


def assert_booked(sid, count):
    from app.db import base
    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM sessions WHERE id=%s", (sid,))
        row = cursor.fetchone()
        for column, value in (("total_tokens_in", 17), ("total_tokens_out", 4),
                              ("total_cache_read_tokens", 11),
                              ("total_cache_creation_tokens", 7),
                              ("total_model_calls", 1), ("turn_count", 1)):
            assert row[column] == count * value
        assert row["total_cost_usd"] == pytest.approx(count * 0.01)
        cursor.execute("SELECT count(*) AS n FROM usage_log WHERE session_id=%s", (sid,))
        assert cursor.fetchone()["n"] == count


def test_simultaneous_settlement_waits_for_committed_claim(booking, monkeypatch):
    from app.db import base, sessions
    from app.db.analytics import settle_turn_usage
    sid, usage = booking
    claimed, release = threading.Event(), threading.Event()
    original = sessions.update_session

    def hold_claim(*args, **kwargs):
        claimed.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(sessions, "update_session", hold_claim)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(settle_turn_usage, sid, sid + ":attempt", **usage)
        try:
            assert claimed.wait(5)
            second = executor.submit(settle_turn_usage, sid, sid + ":attempt", **usage)
            deadline = time.monotonic() + 5
            blocked = False
            while time.monotonic() < deadline:
                with base.get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT count(*) AS n FROM pg_stat_activity "
                                   "WHERE datname=current_database() AND wait_event_type='Lock' "
                                   "AND query LIKE 'INSERT INTO usage_log%'")
                    blocked = cursor.fetchone()["n"] > 0
                if blocked:
                    break
                threading.Event().wait(0.02)
            assert blocked, "no actual PostgreSQL unique-claim lock wait observed"
            assert not second.done()
        finally:
            release.set()
        assert first.result(timeout=10) is True
        assert second.result(timeout=10) is False
    assert_booked(sid, 1)


def test_counter_failure_rolls_back_claim_and_counters(booking, monkeypatch):
    from app.db import sessions
    from app.db.analytics import settle_turn_usage
    sid, usage = booking
    original = sessions.update_session

    def fail_after_update(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected after counter update")

    with monkeypatch.context() as patch:
        patch.setattr(sessions, "update_session", fail_after_update)
        with pytest.raises(RuntimeError, match="injected after counter update"):
            settle_turn_usage(sid, sid + ":attempt", **usage)
    assert_booked(sid, 0)
    assert settle_turn_usage(sid, sid + ":attempt", **usage) is True
    assert_booked(sid, 1)


def test_distinct_attempts_with_identical_usage_are_both_booked(booking):
    from app.db.analytics import settle_turn_usage
    sid, usage = booking
    for attempt in ("first", "retry"):
        assert settle_turn_usage(sid, sid + ":" + attempt, **usage) is True
        assert settle_turn_usage(sid, sid + ":" + attempt, **usage) is False
    assert_booked(sid, 2)


def test_existing_usage_log_migrates_before_settlement_index(booking):
    from app.db import base, database
    from app.db.analytics import settle_turn_usage
    sid, usage = booking
    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("ALTER TABLE usage_log DROP COLUMN settlement_key CASCADE")
        cursor.execute("INSERT INTO usage_log (session_id, tokens_in) VALUES (%s, 5), (%s, 5)",
                       (sid, sid))
    database.init_database()
    database.init_database()
    with base.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT count(*) AS n FROM usage_log WHERE session_id=%s "
                       "AND settlement_key IS NULL", (sid,))
        assert cursor.fetchone()["n"] == 2
    assert settle_turn_usage(sid, sid + ":migrated", **usage) is True
    assert settle_turn_usage(sid, sid + ":migrated", **usage) is False
