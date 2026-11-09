"""Focused real-PostgreSQL regressions for bounded event pruning and online vacuum."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import threading

import psycopg2
from psycopg2.extras import RealDictCursor
import pytest

from app.db import base, database, fluid_events, retention, session_events
from tests.db.schema_lifecycle_helpers import pg16_bindir, private_postgres


@pytest.fixture(scope="module")
def cluster(tmp_path_factory):
    bindir = pg16_bindir()
    if not bindir:
        pytest.skip("PostgreSQL 16 binaries required")
    with private_postgres(bindir, tmp_path_factory.mktemp("delta-prune-pg")) as descriptor:
        yield descriptor


@pytest.fixture
def pg(cluster, monkeypatch):
    args = dict(host="127.0.0.1", port=cluster["port"], user=cluster["user"],
                password=cluster["password"], dbname=cluster["database"],
                cursor_factory=RealDictCursor)

    def open_connection():
        return psycopg2.connect(**args)

    @contextmanager
    def connect():
        conn = open_connection()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    with connect() as conn:
        cur = conn.cursor()
        cur.execute("DROP TABLE IF EXISTS fluid_session_events, sessions")
        cur.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY, sdk_session_id TEXT, updated_at TIMESTAMPTZ)")
        cur.execute("CREATE TABLE fluid_session_events(session_id TEXT, seq BIGINT, event JSONB, PRIMARY KEY(session_id,seq))")
    monkeypatch.setattr(retention, "get_db", connect)
    monkeypatch.setattr(fluid_events, "BATCH_SIZE", 2)
    return connect, open_connection


def seed(connect, name, count=6):
    with connect() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO sessions VALUES (%s,%s,%s)",
                    (name, name, datetime.now(timezone.utc) - timedelta(days=30)))
        cur.execute("INSERT INTO fluid_session_events SELECT %s,g,jsonb_build_object('_cls','ToolArgsDelta') "
                    "FROM generate_series(0,%s) g", (name, count - 1))


def test_timeout_commits_progress_continues_other_sessions_and_retries(pg, monkeypatch, caplog):
    connect, open_connection = pg
    seed(connect, "a", 4)
    seed(connect, "b", 2)

    def short_timeout(cur):
        cur.execute("SET LOCAL statement_timeout = '100ms'")
        cur.execute("SET LOCAL lock_timeout = 0")

    monkeypatch.setattr(fluid_events, "set_timeout", short_timeout)
    blocker = open_connection()
    try:
        blocker.cursor().execute("SELECT seq FROM fluid_session_events WHERE session_id='a' AND seq=2 FOR UPDATE")
        with pytest.raises(RuntimeError, match=r"1 session.*pruned 4 rows.*will retry"):
            fluid_events.prune_fluid_delta_events()
        with connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT session_id,seq FROM fluid_session_events ORDER BY session_id,seq")
            assert [(r["session_id"], r["seq"]) for r in cur.fetchall()] == [("a", 2), ("a", 3)]
        assert "other sessions will continue" in caplog.text
    finally:
        blocker.rollback()
        blocker.close()
    assert fluid_events.prune_fluid_delta_events() == 2
    assert fluid_events.prune_fluid_delta_events() == 0


def test_delete_rechecks_idle_and_stop_is_immediate(pg, monkeypatch):
    connect, _ = pg
    seed(connect, "a")
    original = fluid_events.set_timeout
    calls = 0

    def refresh_between_selection_and_delete(cur):
        nonlocal calls
        original(cur)
        calls += 1
        if calls == 2:
            with connect() as writer:
                writer.cursor().execute("UPDATE sessions SET updated_at=now() WHERE id='a'")

    monkeypatch.setattr(fluid_events, "set_timeout", refresh_between_selection_and_delete)
    assert fluid_events.prune_fluid_delta_events() == 0
    stopped = threading.Event()
    stopped.set()
    before = calls
    assert fluid_events.prune_fluid_delta_events(stop=stopped) == 0
    assert calls == before


@pytest.mark.parametrize("streaming", [True, False])
def test_plain_vacuum_during_streaming_restores_connection(pg, monkeypatch, streaming):
    connect, open_connection = pg
    seed(connect, "a")
    queries, returned = [], []

    class Cursor(RealDictCursor):
        def execute(self, query, params=None):
            queries.append(str(query))
            return super().execute(query, params)

    vacuum_connection = open_connection()
    vacuum_connection.cursor_factory = Cursor
    # No process-global pool or production database is consulted by this test.
    monkeypatch.setattr(database, "get_db", connect)
    monkeypatch.setattr(database, "get_connection", lambda: vacuum_connection)
    monkeypatch.setattr(database, "exempt_from_start_pressure", lambda conn: None)
    def close(conn):
        returned.append(conn.autocommit)
        cur = conn.cursor()
        cur.execute("SHOW statement_timeout")
        assert cur.fetchone()["statement_timeout"] == "30s"
        conn.rollback()
        conn.close()
    monkeypatch.setattr(base, "_close_connection", close)
    monkeypatch.setattr(retention, "mutate_batches", lambda *a, **k: 0)
    monkeypatch.setattr(database, "_prune_notifications_cap", lambda *a, **k: 0)
    monkeypatch.setattr(fluid_events, "prune_fluid_delta_events", lambda *a, **k: 0)
    monkeypatch.setattr(session_events, "compact_idle_session_deltas", lambda **k: {"rows": 0})
    writer = open_connection()
    try:
        writer.cursor().execute("UPDATE fluid_session_events SET event=event WHERE seq=0")
        result = database.run_database_maintenance(skip_vacuum=streaming)
        assert result["vacuum"] == "ok"
        assert returned == [False]
        vacuum = next(q for q in queries if q.startswith("VACUUM"))
        assert "TRUNCATE FALSE" in vacuum and "SKIP_LOCKED" in vacuum and "PARALLEL 0" in vacuum
        assert ("ANALYZE" in vacuum) is (not streaming)
        assert "FULL" not in vacuum
        writer.commit()
    finally:
        writer.close()


@pytest.mark.parametrize("failure", [
    "autocommit_on", "cursor", "budget_set", "worker_set", "vacuum", "worker_reset",
    "autocommit_off", "budget_restore",
])
def test_vacuum_failure_never_uses_released_analyze_cursor(pg, cluster, monkeypatch, failure):
    _, open_connection = pg
    state = {"released": False, "armed": False}
    late_analyze_sql, checked_out = [], []

    class AnalyzeCursor(RealDictCursor):
        def execute(self, query, params=None):
            if state["released"]:
                late_analyze_sql.append(str(query))
            return super().execute(query, params)

    analyze_conn = open_connection()
    analyze_conn.cursor_factory = AnalyzeCursor

    @contextmanager
    def analyze_checkout():
        try:
            with analyze_conn:
                yield analyze_conn
        finally:
            state["released"] = True  # A real pool could lend it to someone else now.

    class FaultCursor(RealDictCursor):
        def execute(self, query, params=None):
            triggers = {"budget_set": "SET lock_timeout",
                        "worker_set": "SET max_parallel_maintenance_workers",
                        "vacuum": "VACUUM", "worker_reset": "RESET max_parallel_maintenance_workers",
                        "budget_restore": "SELECT set_config("}
            if state["armed"] and str(query).startswith(triggers.get(failure, "never")):
                raise psycopg2.OperationalError("injected " + failure)
            return super().execute(query, params)

    class FaultConnection(psycopg2.extensions.connection):
        def __setattr__(self, name, value):
            if state["armed"] and name == "autocommit" and (
                (failure == "autocommit_on" and value is True)
                or (failure == "autocommit_off" and value is False)
            ):
                raise psycopg2.OperationalError("injected " + failure)
            super().__setattr__(name, value)

        def cursor(self, *args, **kwargs):
            if state["armed"] and failure == "cursor":
                raise psycopg2.OperationalError("injected cursor")
            return super().cursor(*args, **kwargs)

    pool = base._RetainingThreadedConnectionPool(
        1, 1, reserved=0, host="127.0.0.1", port=cluster["port"], user=cluster["user"],
        password=cluster["password"], dbname=cluster["database"],
        connection_factory=FaultConnection, cursor_factory=FaultCursor,
        options="-c statement_timeout=30s -c lock_timeout=30s",
    )

    def vacuum_checkout():
        conn = pool.getconn()
        checked_out.append(conn)
        state["armed"] = True
        return conn

    monkeypatch.setattr(base, "_pg_pool", pool)  # Exercise the real slot-release helper.
    monkeypatch.setattr(database, "get_db", analyze_checkout)
    monkeypatch.setattr(database, "get_connection", vacuum_checkout)
    monkeypatch.setattr(database, "exempt_from_start_pressure", lambda conn: None)
    monkeypatch.setattr(retention, "mutate_batches", lambda *a, **k: 0)
    monkeypatch.setattr(database, "_prune_notifications_cap", lambda *a, **k: 0)
    monkeypatch.setattr(fluid_events, "prune_fluid_delta_events", lambda *a, **k: 0)
    monkeypatch.setattr(session_events, "compact_idle_session_deltas", lambda **k: {"rows": 0})
    try:
        result = database.run_database_maintenance(skip_vacuum=True)
        assert result["analyze"] == "ok"
        assert state["released"] and not late_analyze_sql
        assert not pool._used and not pool._rused and not pool._returning
        conn = checked_out[0]
        discard = failure not in ("budget_set", "worker_set", "vacuum")
        assert bool(conn.closed) is discard
        state["armed"] = False
        if not discard:
            assert conn.autocommit is False
            cur = conn.cursor()
            cur.execute("SELECT current_setting('statement_timeout') AS statement, "
                        "current_setting('lock_timeout') AS lock, "
                        "current_setting('max_parallel_maintenance_workers') AS workers")
            assert cur.fetchone() == {"statement": "30s", "lock": "30s", "workers": "2"}
            conn.rollback()
    finally:
        state["armed"] = False
        analyze_conn.close()
        pool.closeall()
