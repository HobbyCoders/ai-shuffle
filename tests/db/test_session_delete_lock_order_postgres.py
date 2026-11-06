"""Delete versus the real event recorder, interleaved on private PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time
from uuid import uuid4

import psycopg2
from psycopg2.extras import RealDictCursor
import pytest

from app.db import base, runs, session_events, sessions
from tests.db.postgres_harness import requires_real_pg
from tests.db import schema_lifecycle_helpers as lifecycle

pytestmark = requires_real_pg


@pytest.fixture(scope="module", autouse=True)
def private_pool(tmp_path_factory):
    from app.core.config import settings
    from app.db.schema_postgres import create_postgres_schema

    bindir = lifecycle.pg16_bindir()
    if bindir is None:
        pytest.skip("PostgreSQL 16 server binaries not found")
    with lifecycle.private_postgres(bindir, tmp_path_factory.mktemp("delete-lock-pg")) as d:
        kwargs = dict(host=d["host"], port=d["port"], user=d["user"],
                      password=d["password"], dbname=d["database"])
        original = settings.get_database_connection
        object.__setattr__(settings, "get_database_connection", lambda: dict(kwargs))
        base.init_pool(min_conn=1, max_conn=6)
        try:
            with base.get_db() as conn:
                create_postgres_schema(conn.cursor())
            yield kwargs
        finally:
            base.close_pool()
            object.__setattr__(settings, "get_database_connection", original)


@pytest.mark.parametrize("fenced", [True, False], ids=["run-fence", "unfenced-counter"])
def test_delete_waits_before_removing_event_runtime(private_pool, monkeypatch, fenced):
    sid = "delete-lock-" + uuid4().hex
    with base.get_db() as conn:
        conn.cursor().execute("INSERT INTO sessions (id) VALUES (%s)", (sid,))
    run = runs.claim_run(sid, "recorder", 60)
    session_events.append_events(sid, [{"type": "system.note", "payload": {}}])
    # No filesystem or wakeup service is needed to reproduce the DB transaction.
    monkeypatch.setattr(sessions, "_cleanup_working_set_ref", lambda sid: None)
    monkeypatch.setattr(sessions, "_cleanup_pending_wakeups", lambda sid: None)
    locked, resume = threading.Event(), threading.Event()
    appender_pid = []

    class InterleavedCursor(RealDictCursor):
        def execute(self, sql, params=None):
            result = super().execute(sql, params)
            if threading.current_thread().name.startswith("recorder"):
                fence_lock = "FROM runs WHERE id = %s FOR SHARE" in sql
                counter_lock = (sql.startswith("INSERT INTO session_event_counters")
                                and "RETURNING next_seq" in sql)
                if (fence_lock if fenced else counter_lock) and not locked.is_set():
                    appender_pid.append(self.connection.get_backend_pid())
                    locked.set()
                    assert resume.wait(10), "test never released the recorder"
            return result

    cursor = base._PgDictConnection.cursor

    def instrumented(self, *args, **kwargs):
        kwargs["cursor_factory"] = InterleavedCursor
        return cursor(self, *args, **kwargs)

    monkeypatch.setattr(base._PgDictConnection, "cursor", instrumented)

    def append():
        return session_events.append_events(
            sid, [{"type": "turn.completed", "payload": {}}],
            run_id=run["id"] if fenced else None,
            fence_token=run["fence_token"] if fenced else None,
        )

    with psycopg2.connect(**private_pool) as observer, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="recorder") as recorder, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="delete") as deleter:
        observer.autocommit = True
        recording = recorder.submit(append)
        deleting = None
        try:
            assert locked.wait(5), "recorder never acquired its first lock"
            deleting = deleter.submit(sessions.delete_session, sid)
            # Observe a real lock wait, not a sleep guessing which transaction won.
            deadline = time.monotonic() + 5
            with observer.cursor() as probe:
                while True:
                    probe.execute("SELECT pg_stat_clear_snapshot()")
                    probe.execute("SELECT pid FROM pg_stat_activity WHERE %s = ANY(pg_blocking_pids(pid))",
                                  (appender_pid[0],))
                    if probe.fetchone():
                        break
                    if deleting.done():
                        deleting.result()  # Surface SQL setup failures, not a timing error.
                        pytest.fail("delete finished while the recorder held its lock")
                    assert time.monotonic() < deadline, "delete did not wait behind recorder"
                    time.sleep(0.01)
            assert not deleting.done()
        finally:
            resume.set()
        assert recording.result(timeout=5) == (2, 2)
        assert deleting.result(timeout=5)

    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM sessions WHERE id=%s", (sid,))
        assert cur.fetchone() is None
        for table in sessions._SESSION_RUNTIME_TABLES:
            cur.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE session_id=%s", (sid,))
            assert cur.fetchone()["n"] == 0, f"delete left orphan rows in {table}"
