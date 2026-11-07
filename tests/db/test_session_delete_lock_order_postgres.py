"""Delete versus the real event recorder, interleaved on private PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time
from uuid import uuid4

import psycopg2
from psycopg2.extras import RealDictCursor
import pytest

from app.db import base, runs, session_events, sessions, turns
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


def _wait_for_lock_wait(observer, blocker_pid, pending):
    deadline = time.monotonic() + 5
    with observer.cursor() as probe:
        while True:
            probe.execute("SELECT pg_stat_clear_snapshot()")
            probe.execute("SELECT pid FROM pg_stat_activity WHERE %s = ANY(pg_blocking_pids(pid))",
                          (blocker_pid,))
            if probe.fetchone():
                return
            if pending.done():
                pending.result()
                pytest.fail("transaction finished without the required lock wait")
            assert time.monotonic() < deadline, "transaction never waited for its competitor"
            time.sleep(0.01)


def _assert_deleted(sid):
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM sessions WHERE id=%s", (sid,))
        assert cur.fetchone() is None
        for table in sessions._SESSION_RUNTIME_TABLES:
            cur.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE session_id=%s", (sid,))
            assert cur.fetchone()["n"] == 0, f"delete left orphan rows in {table}"


@pytest.mark.parametrize("path,with_run", [("park-repair", True), ("park-repair", False), ("expired-settle", True)])
@pytest.mark.parametrize("first", ["settler", "delete"])
def test_delete_vs_settlement_both_lock_orders(private_pool, monkeypatch, path, with_run, first):
    from types import SimpleNamespace
    from app.core.session_settlement import _capture_and_dismiss

    sid = "delete-settle-" + uuid4().hex
    with base.get_db() as conn:
        conn.cursor().execute("INSERT INTO sessions (id) VALUES (%s)", (sid,))
    run = runs.claim_run(sid, "settlement", 60) if with_run else None
    turn = turns.enqueue_turn(sid, "user", prompt="settlement interleaving")
    turns.start_turn(turn["id"], run_id=run["id"] if run else None)
    if path == "park-repair":
        parked = turns.park_turn(turn["id"], usage={"input_tokens": 1})
        # The real ProviderAgent repair arguments, including the admission guard.
        settle = lambda: turns.settle_parked_turns(
            sid, turn_ids=[turn["id"]], only_if_idle=True,
            observed_park=parked, can_settle=lambda: True,
        )
    else:
        with base.get_db() as conn:
            conn.cursor().execute("UPDATE runs SET lease_expires_at=NOW()-INTERVAL '1 minute' WHERE id=%s",
                                  (run["id"],))
        # An in-process owner wait makes the production capture reach the
        # expired-run fence, rather than its deliberately empty-snapshot branch.
        broker = SimpleNamespace(snapshot_pending=lambda _: ([SimpleNamespace(session_id=sid)], [], []))
        settle = lambda: _capture_and_dismiss(sid, broker, None)
    session_events.append_events(sid, [{"type": "system.note", "payload": {}}])
    monkeypatch.setattr(sessions, "_cleanup_working_set_ref", lambda sid: None)
    monkeypatch.setattr(sessions, "_cleanup_pending_wakeups", lambda sid: None)
    locked, resume = threading.Event(), threading.Event()
    first_pid = []

    class InterleavedCursor(RealDictCursor):
        def execute(self, sql, params=None):
            result = super().execute(sql, params)
            name = threading.current_thread().name
            if not name.startswith(first) or locked.is_set():
                return result
            run_lock = "FROM runs WHERE session_id" in sql and "FOR UPDATE" in sql and self.rowcount > 0
            counter_lock = sql.startswith("INSERT INTO session_event_counters")
            counter_row_lock = "FROM session_event_counters WHERE" in sql and "FOR UPDATE" in sql
            session_lock = "FROM sessions WHERE id=" in sql and "FOR UPDATE" in sql
            if first == "delete":
                boundary = counter_lock if path == "park-repair" else run_lock
            else:
                # The old path starts with session/counter; the fixed path
                # starts with the shared run lock. Pause at either first lock.
                boundary = run_lock or counter_lock or (session_lock if path == "park-repair" else counter_row_lock)
            if boundary:
                first_pid.append(self.connection.get_backend_pid())
                locked.set()
                assert resume.wait(10), "test never released the first transaction"
            return result

    original_cursor = base._PgDictConnection.cursor

    def instrumented(self, *args, **kwargs):
        kwargs["cursor_factory"] = InterleavedCursor
        return original_cursor(self, *args, **kwargs)

    monkeypatch.setattr(base._PgDictConnection, "cursor", instrumented)
    with psycopg2.connect(**private_pool) as observer, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="settler") as settler, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="delete") as deleter:
        observer.autocommit = True
        leading = settler.submit(settle) if first == "settler" else deleter.submit(sessions.delete_session, sid)
        try:
            assert locked.wait(5), "first transaction never reached its lock boundary"
            trailing = deleter.submit(sessions.delete_session, sid) if first == "settler" else settler.submit(settle)
            _wait_for_lock_wait(observer, first_pid[0], trailing)
        finally:
            resume.set()
        settle_result = (leading if first == "settler" else trailing).result(timeout=5)
        assert (trailing if first == "settler" else leading).result(timeout=5)
        if first == "settler":
            if path == "park-repair":
                assert settle_result == 1
            else:
                assert [row["id"] for row in settle_result["aborted"]] == [turn["id"]]
        elif path == "park-repair":
            assert settle_result == 0
        else:
            assert settle_result is None
    _assert_deleted(sid)


@pytest.mark.parametrize("backfill", [False, True], ids=["unfenced-append", "backfill"])
def test_delete_wins_late_event_write_cannot_resurrect_runtime(private_pool, monkeypatch, backfill):
    sid = "delete-late-event-" + uuid4().hex
    with base.get_db() as conn:
        conn.cursor().execute("INSERT INTO sessions (id) VALUES (%s)", (sid,))
    session_events.append_events(sid, [{"type": "system.note", "payload": {}}])
    monkeypatch.setattr(sessions, "_cleanup_working_set_ref", lambda sid: None)
    monkeypatch.setattr(sessions, "_cleanup_pending_wakeups", lambda sid: None)
    locked, resume = threading.Event(), threading.Event()
    delete_pid = []

    class InterleavedCursor(RealDictCursor):
        def execute(self, sql, params=None):
            result = super().execute(sql, params)
            if (threading.current_thread().name.startswith("delete")
                    and sql.startswith("INSERT INTO session_event_counters") and not locked.is_set()):
                delete_pid.append(self.connection.get_backend_pid())
                locked.set()
                assert resume.wait(10), "test never released deletion"
            return result

    original_cursor = base._PgDictConnection.cursor

    def instrumented(self, *args, **kwargs):
        kwargs["cursor_factory"] = InterleavedCursor
        return original_cursor(self, *args, **kwargs)

    monkeypatch.setattr(base._PgDictConnection, "cursor", instrumented)
    append = session_events.append_backfill_events if backfill else session_events.append_events
    with psycopg2.connect(**private_pool) as observer, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="delete") as deleter, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="late-event") as writer:
        observer.autocommit = True
        deleting = deleter.submit(sessions.delete_session, sid)
        try:
            assert locked.wait(5), "delete never owned the counter lock"
            writing = writer.submit(append, sid, [{"type": "system.note", "payload": {}}])
            _wait_for_lock_wait(observer, delete_pid[0], writing)
        finally:
            resume.set()
        assert deleting.result(timeout=5)
        # Rejection unwinds the append's transaction, including its counter
        # upsert. Neither an event nor a counter may reappear after deletion.
        with pytest.raises(ValueError, match="session.*does not exist"):
            writing.result(timeout=5)
    _assert_deleted(sid)
