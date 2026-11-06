"""Online retirement and reader parity on disposable PostgreSQL only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from dataclasses import asdict
import logging
import threading
import time

import pytest
from psycopg2.extras import Json

from app.db import base, history_indexing, recall_index_retirement as retirement, session_recall
from tests.db.postgres_harness import pg_schema, requires_real_pg  # noqa: F401

pytestmark = requires_real_pg


@pytest.fixture(scope="module", autouse=True)
def clean_retirement_state(pg_schema):  # noqa: F811
    # Other real-PG suites share this disposable DB. Do not inherit their ready
    # stamps or leak our rows/explicit history opt-in into a later module.
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM session_recall_backfill")
        cur.execute("SELECT value FROM system_settings WHERE key=%s", (history_indexing.KEY,))
        previous = cur.fetchone()
        cur.execute("DELETE FROM system_settings WHERE key=%s", (history_indexing.KEY,))
    yield
    with base.get_db() as conn:
        cur = conn.cursor()
        for table in ("fluid_session_nodes", "fluid_sessions", "session_store_entries",
                      "session_events", "session_event_counters", "session_recall_text"):
            cur.execute(f"DELETE FROM {table} WHERE session_id LIKE 'retire-%%'")
        cur.execute("DELETE FROM sessions WHERE id LIKE 'retire-%%'")
        cur.execute("DELETE FROM session_recall_backfill")
        if previous:
            cur.execute("UPDATE system_settings SET value=%s WHERE key=%s",
                        (previous["value"], history_indexing.KEY))
        else:
            cur.execute("DELETE FROM system_settings WHERE key=%s", (history_indexing.KEY,))


@pytest.fixture
def ready_projection():
    assert session_recall.run_backfill(max_seconds=5, pause_seconds=0)
    assert retirement.run_pass(seconds=10)


SHAPES = (
    ("fluid_session_nodes", "message", "TRUE", "idx_fluid_nodes_trgm", "idx_fluid_recall_text_trgm"),
    ("session_store_entries", "entry", "subpath IS NULL", "idx_sse_entry_trgm", "idx_sse_recall_text_trgm"),
    ("session_events", "payload", "event_type IN ('message.user','message.assistant','assistant.text_delta')",
     "idx_session_events_recall_trgm", "idx_events_recall_text_trgm"),
)


def names():
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT indexname FROM pg_indexes WHERE schemaname=current_schema()")
        return {r["indexname"] for r in cur.fetchall()}


def seed_old_indexes():
    # Tiny disposable tables only: model every legacy shape, including debris
    # left by an interrupted history migration. Production never builds these.
    with base.get_db() as conn:
        cur = conn.cursor()
        for table, column, predicate, raw, cached in SHAPES:
            if not base.column_exists(cur, table, "recall_text"):
                cur.execute(f"ALTER TABLE {table} ADD COLUMN recall_text text GENERATED ALWAYS AS "
                            f"(CASE WHEN {predicate} THEN {column}::text END) STORED")
            for name, expression in ((raw, f"({column}::text)"), (cached, "recall_text")):
                for suffix in ("", "_h0", "_h2"):
                    cur.execute(f"CREATE INDEX IF NOT EXISTS {name}{suffix} ON {table} "
                                f"USING gin ({expression} gin_trgm_ops) WHERE {predicate}")


def listed(query):
    from app.db.sessions import search_sessions, search_sessions_paginated
    rows = search_sessions(query)
    paged, total = search_sessions_paginated(query)
    assert {r["id"] for r in rows} == {r["id"] for r in paged}
    assert total == len(rows)
    return {r["id"]: r["match_snippet"] for r in rows}


def test_projection_cutover_cannot_select_missing_generated_columns(monkeypatch):
    from app.core.provider import fluid_pg_store as recall

    checks = 0

    def becomes_ready(_cursor):
        nonlocal checks
        checks += 1
        return checks > 1  # background publishes readiness after the first check

    monkeypatch.setattr(session_recall, "ready", becomes_ready)
    monkeypatch.setattr(recall, "_my_device_id", lambda: "test-local")
    # A fresh install has no recall_text columns. Re-checking projection
    # readiness as evidence for those columns would raise UndefinedColumn.
    assert recall._search_sync(["cutoverneedle"], 10, None) == []


@pytest.mark.asyncio
async def test_retirement_preserves_real_writer_recall_and_both_session_lists(monkeypatch):
    from fluid.messages import Message, Text
    from fluid.store.base import SessionNode
    from app.core.provider import fluid_pg_store as recall
    from app.core.pg_session_store import PgSessionStore
    from app.db import database, schema_postgres, session_events

    monkeypatch.setattr(recall, "_my_device_id", lambda: "test-local")
    monkeypatch.setattr(recall, "_worker_name", lambda _: "Peer")
    # Hold off the background: first compare the historical raw/cache reader.
    seed_old_indexes()
    with base.get_db() as conn:
        cur = conn.cursor()
        assert not session_recall.ready(cur)
        for arm, owner in (("native", "test-local"), ("sdk", "test-local"), ("remote", "test-peer")):
            cur.execute("INSERT INTO sessions(id,sdk_session_id,title,owner_device_id) VALUES (%s,%s,%s,%s)",
                        (f"retire-{arm}", f"retire-sdk-{arm}", "Synthetic chat", owner))
    store = recall.PgFluidStore()
    sid = "retire-sdk-native"
    await store.create_session(sid)
    await store.append(sid, SessionNode(node_id="one", parent_id=None,
                                      message=Message("user", [Text("needlealpha"), Text("needlebeta 50%_done 東京")])))
    await store.append(sid, SessionNode(node_id="two", parent_id="one",
                                      message=Message("assistant", [Text("x" * 950 + "boundaryneedle" + "x" * 150)])))
    # These must never become visible session-list matches.
    await store.append(sid, SessionNode(node_id="system", parent_id="two",
                                      message=Message("system", [Text("systemonlyneedle")])))
    await PgSessionStore().append({"project_key": "retire", "session_id": "retire-sdk-sdk"},
                                 [{"type": "user", "message": {"content": "needlealpha needlebeta 50%_done 東京"}}])
    session_events.append_events("retire-remote", [
        {"type": "message.user", "payload": {"content": "needlealpha needlebeta 50%_done 東京"}},
    ])
    terms = ["needlealpha", "needlebeta"]
    old_hits = [asdict(h) for h in recall._search_sync(terms, 10, None)]
    assert {h["session_id"] for h in old_hits} == {sid, "retire-sdk-sdk", "retire-remote"}
    queries = ("needlealpha needlebeta", "50%_done", "東京", "boundaryneedle", "x" * 120, "systemonlyneedle")
    old_lists = {q: listed(q) for q in queries}
    assert old_lists[queries[0]] and old_lists[queries[-1]] == {}
    assert session_recall.run_backfill(max_seconds=2, pause_seconds=0)
    assert retirement.run_pass()
    expected_absent = {n + suffix for n in retirement.INDEXES for suffix in ("", "_h0", "_h2")}
    assert expected_absent.isdisjoint(names())
    assert [asdict(h) for h in recall._search_sync(terms, 10, None)] == old_hits
    assert {q: listed(q) for q in queries} == old_lists
    with base.get_db() as conn:
        cur = conn.cursor()
        assert schema_postgres.session_recall_cache_ready(cur)
        # Explicit legacy maintenance is also a no-op after projection readiness.
        schema_postgres.create_session_recall_text(cur)
        for table, *_ in SHAPES:
            assert base.column_exists(cur, table, "recall_text")
        # A stale saved history definition must never resurrect a retired GIN.
        cur.execute("CREATE TABLE IF NOT EXISTS history_index_migrations (name text PRIMARY KEY, tail text NOT NULL)")
        cur.execute("INSERT INTO history_index_migrations VALUES ('idx_fluid_nodes_trgm',"
                    "'((message::text) gin_trgm_ops)') ON CONFLICT DO NOTHING")
    for _ in range(10):
        if history_indexing.migrate():
            break
    else:
        pytest.fail("history migration did not finish")
    assert set(retirement.INDEXES).isdisjoint(history_indexing.INDEXES)
    assert expected_absent.isdisjoint(names())
    # Restart / repeat both actual schema paths, not only the retirement helper.
    base.close_pool()
    base.init_pool(min_conn=1, max_conn=6)
    database.init_database()
    with base.get_db() as conn:
        cur = conn.cursor()
        schema_postgres.create_postgres_schema(cur)
        database._set_marker(cur, database.MIGRATIONS_FINGERPRINT_KEY, "before-retirement")
    database.init_database()
    database.init_database()
    assert expected_absent.isdisjoint(names())
    assert {q: listed(q) for q in queries} == old_lists
    assert [asdict(h) for h in recall._search_sync(terms, 10, None)] == old_hits


def test_not_ready_and_advisory_lock_defer_retirement(monkeypatch, ready_projection):
    seed_old_indexes()
    with monkeypatch.context() as mp:
        mp.setattr(session_recall, "ready", lambda _: False)
        assert not retirement.run_pass()
    assert set(retirement.INDEXES) <= names()
    with history_indexing._online_cursor() as cur:
        assert cur is not None
        assert not retirement.run_pass()
    assert retirement.run_pass()
    assert set(retirement.INDEXES).isdisjoint(names())


def test_waiting_drop_allows_insert_update_and_search(monkeypatch, ready_projection):
    seed_old_indexes()
    started = threading.Event()
    original = history_indexing._online_cursor
    drops = []

    class TrackingCursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def execute(self, sql, params=None):
            if sql.startswith("DROP INDEX"):
                assert self.cursor.connection.autocommit
                assert sql.startswith("DROP INDEX CONCURRENTLY IF EXISTS ")
                self.cursor.execute("SELECT current_setting('lock_timeout') AS timeout")
                assert self.cursor.fetchone()["timeout"] == "0"
                drops.append(sql)
                started.set()
            return self.cursor.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self.cursor, name)

    @contextmanager
    def tracked():
        with original() as cur:
            yield TrackingCursor(cur) if cur is not None else None

    monkeypatch.setattr(history_indexing, "_online_cursor", tracked)
    # This reader pins the first GIN until commit. A non-concurrent DROP would
    # queue an ACCESS EXCLUSIVE lock and block the independent INSERT behind it.
    with ThreadPoolExecutor(max_workers=1) as executor:
        with base.get_db() as reader:
            reader.cursor().execute("SELECT * FROM fluid_session_nodes LIMIT 1")
            future = executor.submit(retirement.run_pass)
            assert started.wait(2)
            with base.get_db() as writer:
                cur = writer.cursor()
                # Hard assertion enforced by PG, not a noisy wall-clock threshold.
                cur.execute("SET LOCAL statement_timeout = '250ms'")
                cur.execute("INSERT INTO fluid_session_nodes(session_id,node_id,ts,message) VALUES "
                            "('retire-concurrent','one',0,%s)",
                            (Json({"role": "user", "content": [{"kind": "text", "text": "insertneedle"}]}),))
                cur.execute("UPDATE fluid_session_nodes SET ts = 1 WHERE session_id='retire-concurrent'")
            listed("needlealpha")  # both real session-list searches while DROP waits
            assert not future.done()
        # Release the reader before waiting for the concurrent DROP's result.
        assert future.result(timeout=5)
    assert drops
    assert set(retirement.INDEXES).isdisjoint(names())


def _wait_for_pg(cur, sql, params=()):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        cur.execute("SELECT pg_stat_clear_snapshot()")
        cur.execute(sql, params)
        if cur.fetchone()["ok"]:
            return
        time.sleep(0.01)
    pytest.fail("expected PostgreSQL lock wait was not observed")


def test_startup_cancels_and_later_repairs_a_reader_pinned_drop(monkeypatch, ready_projection):
    from app.db import database

    database.init_database()  # stamp the current fingerprint before pinning a source table
    seed_old_indexes()
    monkeypatch.setattr(database, "_SCHEMA_MAINTENANCE_GRACE_SECONDS", 0.1)
    with ThreadPoolExecutor(max_workers=2) as executor:
        with base.get_db() as reader:
            cur = reader.cursor()
            cur.execute("SELECT * FROM fluid_session_nodes LIMIT 1")
            dropping = executor.submit(retirement.run_pass, seconds=10)
            _wait_for_pg(cur, "SELECT EXISTS (SELECT FROM pg_locks WHERE "
                         "relation='fluid_session_nodes'::regclass "
                         "AND mode='ShareUpdateExclusiveLock' AND granted) AS ok")
            executor.submit(database.init_database).result(timeout=5)
            assert dropping.result(timeout=2) is False
            cur.execute("SELECT indisvalid FROM pg_index WHERE indexrelid=to_regclass('idx_fluid_nodes_trgm')")
            assert cur.fetchone()["indisvalid"] is False
        assert retirement.run_pass(seconds=10)
    assert set(retirement.INDEXES).isdisjoint(names())


def test_startup_ddl_cannot_queue_table_lock_behind_waiting_drop(monkeypatch, ready_projection):
    from app.db import database

    seed_old_indexes()
    startup_started = threading.Event()
    ddl_started = threading.Event()
    startup_pid = []
    original_db = database.get_db

    @contextmanager
    def startup_db():
        with original_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT pg_backend_pid() AS pid")
            startup_pid.append(cur.fetchone()["pid"])
            startup_started.set()
            yield conn

    def startup_ddl(cur):
        ddl_started.set()
        # The existing-index no-op still requests ShareLock. This is the real
        # unconditional statement from startup's column-migration pass.
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sse_main "
                    "ON session_store_entries(project_key,session_id,id) WHERE subpath IS NULL")

    monkeypatch.setattr(database, "get_db", startup_db)
    monkeypatch.setattr(database, "migrations_fingerprint", lambda: "retire-queue-order-test")
    monkeypatch.setattr(database, "_run_column_migrations", startup_ddl)
    with ThreadPoolExecutor(max_workers=2) as executor:
        with base.get_db() as reader:
            cur = reader.cursor()
            cur.execute("SELECT * FROM session_store_entries LIMIT 1")
            dropping = executor.submit(retirement.run_pass, seconds=10)
            _wait_for_pg(cur, "SELECT EXISTS (SELECT FROM pg_locks WHERE "
                         "relation='session_store_entries'::regclass "
                         "AND mode='ShareUpdateExclusiveLock' AND granted) AS ok")
            starting = executor.submit(database.init_database)
            assert startup_started.wait(2)
            _wait_for_pg(cur, "SELECT state='idle' AND backend_xmin IS NULL AS ok "
                         "FROM pg_stat_activity WHERE pid=%s",
                         (startup_pid[0],))
            assert not ddl_started.is_set()
            cur.execute("SELECT count(*) AS n FROM pg_locks WHERE pid=%s "
                        "AND relation='session_store_entries'::regclass AND mode='ShareLock'",
                        (startup_pid[0],))
            assert cur.fetchone()["n"] == 0
            with base.get_db() as writer:
                write = writer.cursor()
                write.execute("SET LOCAL statement_timeout='250ms'")
                write.execute("INSERT INTO session_store_entries(project_key,session_id,entry) "
                              "VALUES ('retire-queue','retire-queue','{}') RETURNING id")
                row_id = write.fetchone()["id"]
                write.execute("UPDATE session_store_entries SET created_at=NOW() WHERE id=%s", (row_id,))
                write.execute("DELETE FROM session_store_entries WHERE id=%s", (row_id,))
            listed("needlealpha")
            assert not dropping.done() and not starting.done()
        assert dropping.result(timeout=5)
        starting.result(timeout=5)
    assert ddl_started.is_set()
    assert set(retirement.INDEXES).isdisjoint(names())


def test_startup_lock_defers_online_and_projection_ddl(monkeypatch, ready_projection):
    from app.db.database import SCHEMA_INIT_LOCK_KEY

    seed_old_indexes()
    with base.get_db() as startup:
        startup.cursor().execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_INIT_LOCK_KEY,))
        before = time.monotonic()
        assert retirement.run_pass() is False
        assert time.monotonic() - before < 0.5
        with history_indexing._online_cursor() as cur:
            assert cur is None  # history migration uses the same gate
        with base.get_db() as repair:
            assert session_recall.install_objects(repair.cursor()) is False
        with monkeypatch.context() as mp:
            # Simulate trigger repair in a second app process. It must not issue
            # CREATE TRIGGER (ShareRowExclusiveLock) while online DDL holds the key.
            mp.setattr(session_recall, "_source_triggers", lambda *_: set())
            assert session_recall.install_triggers() is False
        with pytest.raises(RuntimeError, match="schema maintenance busy"):
            session_recall.uninstall()
        assert "idx_fluid_nodes_trgm" in names()
    assert retirement.run_pass(seconds=10)  # no advisory lock leaked on the skip path


def test_slow_tail_index_build_outlives_drop_budget_and_is_not_restarted(monkeypatch, ready_projection):
    with history_indexing._online_cursor() as cur:
        cur.execute(f"DROP INDEX CONCURRENTLY {session_recall.NATIVE_TAIL_INDEX}")
    original = history_indexing._online_cursor
    builds = []
    drops = []

    class SlowBuildCursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def execute(self, sql, params=None):
            # Scale down only the drop timeout. A slow build must outlive it.
            if sql == "SET statement_timeout = '3min'":
                sql = "SET statement_timeout = '50ms'"
            if sql.startswith("CREATE INDEX CONCURRENTLY"):
                self.cursor.execute("SHOW statement_timeout")
                assert self.cursor.fetchone()["statement_timeout"] == "0"
                self.cursor.execute("SELECT pg_sleep(0.2)")
                builds.append(sql)
            if sql.startswith("DROP INDEX CONCURRENTLY"):
                self.cursor.execute("SHOW statement_timeout")
                assert self.cursor.fetchone()["statement_timeout"] == "50ms"
                drops.append(sql)
            return self.cursor.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self.cursor, name)

    @contextmanager
    def slow_cursor():
        with original() as cur:
            yield SlowBuildCursor(cur) if cur is not None else None

    monkeypatch.setattr(history_indexing, "_online_cursor", slow_cursor)
    assert retirement.run_pass(seconds=0.1) is False  # budget expires after successful build
    with base.get_db() as conn:
        assert session_recall.native_tail_index_ready(conn.cursor())
    assert retirement.run_pass(seconds=10)
    assert len(builds) == 1 and len(drops) > 1


@pytest.mark.parametrize("yield_to_startup", [False, True], ids=["natural-completion", "startup-priority"])
def test_startup_during_concurrent_tail_build_has_no_snapshot_deadlock(
    monkeypatch, caplog, ready_projection, yield_to_startup,
):
    from app.db import database

    with history_indexing._online_cursor() as cur:
        cur.execute(f"DROP INDEX CONCURRENTLY {session_recall.NATIVE_TAIL_INDEX}")
    retried = threading.Event()
    startup_pid = []
    original_db = database.get_db

    class StartupConnection:
        def __init__(self, conn):
            self.conn = conn

        def rollback(self):
            self.conn.rollback()
            retried.set()

        def __getattr__(self, name):
            return getattr(self.conn, name)

    @contextmanager
    def startup_db():
        with original_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT pg_backend_pid() AS pid")
            startup_pid.append(cur.fetchone()["pid"])
            yield StartupConnection(conn)

    monkeypatch.setattr(database, "get_db", startup_db)
    caplog.set_level(logging.INFO, logger=database.__name__)
    with ThreadPoolExecutor(max_workers=2) as executor:
        with base.get_db() as old_snapshot:
            cur = old_snapshot.cursor()
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            cur.execute("SELECT * FROM session_recall_text LIMIT 1")
            building = executor.submit(retirement.run_pass, seconds=10)
            with base.get_db() as observer:
                watch = observer.cursor()
                # A real concurrent CREATE is executing, not a pre-statement
                # test delay. Pin only its final older-snapshots phase.
                _wait_for_pg(watch, "SELECT EXISTS (SELECT FROM pg_stat_progress_create_index p "
                             "JOIN pg_stat_activity a USING (pid) "
                             "WHERE p.relid='session_recall_text'::regclass "
                             "AND p.phase='waiting for old snapshots' "
                             "AND a.application_name=%s) AS ok", (database.ONLINE_DDL_APPLICATION_NAME,))
            before = time.monotonic()
            starting = executor.submit(database.init_database)
            assert retried.wait(2)  # an unsuccessful try-lock transaction has ended
            with base.get_db() as observer:
                watch = observer.cursor()
                _wait_for_pg(watch, "SELECT state='idle' AND backend_xmin IS NULL AS ok "
                             "FROM pg_stat_activity WHERE pid=%s", (startup_pid[0],))
            if yield_to_startup:
                # Keep the old snapshot pinned: only startup's targeted cancel
                # can make this build yield. Use the real ten-second grace.
                starting.result(timeout=15)
                assert time.monotonic() - before < 15
                assert building.result(timeout=2) is False
                with base.get_db() as observer:
                    watch = observer.cursor()
                    assert not session_recall.native_tail_index_ready(watch)
                    watch.execute("SELECT indisvalid FROM pg_index WHERE indexrelid=to_regclass(%s)",
                                  (session_recall.NATIVE_TAIL_INDEX,))
                    assert watch.fetchone()["indisvalid"] is False
            else:
                old_snapshot.commit()
                assert building.result(timeout=5)
                starting.result(timeout=5)  # no DeadlockDetected propagates out of startup
        if yield_to_startup:
            assert retirement.run_pass(seconds=10)  # repair invalid CIC after reader leaves
    with base.get_db() as conn:
        assert session_recall.native_tail_index_ready(conn.cursor())
    waits = [r for r in caplog.records if r.getMessage().startswith("Waiting for database schema lock")]
    assert len(waits) == 1


@pytest.mark.parametrize("kind", ["ordinary-startup", "wrong-key", "two-integer-namespace"])
def test_startup_never_cancels_unrelated_lock_holders(kind):
    from app.db import database

    key = database.SCHEMA_INIT_LOCK_KEY
    with base.get_db() as holder:
        cur = holder.cursor()
        name = "retire-ordinary-startup" if kind == "ordinary-startup" else database.ONLINE_DDL_APPLICATION_NAME
        cur.execute("SELECT set_config('application_name',%s,true)", (name,))
        if kind == "two-integer-namespace":
            high = ((key >> 32) + 2 ** 31) % 2 ** 32 - 2 ** 31
            low = (key + 2 ** 31) % 2 ** 32 - 2 ** 31
            cur.execute("SELECT pg_advisory_xact_lock(%s,%s)", (high, low))
        else:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (key + (kind == "wrong-key"),))
        with base.get_db() as startup:
            assert database._cancel_schema_maintenance(startup.cursor()) is False
        cur.execute("SELECT 1 AS ok")
        assert cur.fetchone()["ok"] == 1


@pytest.mark.parametrize("fail", [False, True])
def test_online_cursor_discards_backend_before_pool_handoff(monkeypatch, fail):
    from app.db.database import ONLINE_DDL_APPLICATION_NAME

    with base.get_db() as conn:
        dsn = conn._conn.dsn
    # One slot forces ordinary traffic to reuse maintenance's physical backend
    # unless it was discarded, exercising the real get_db/pool return path.
    pool = base._RetainingThreadedConnectionPool(1, 1, dsn=dsn)
    monkeypatch.setattr(base, "_pg_pool", pool)
    try:
        expected = pytest.raises(RuntimeError, match="test exit") if fail else nullcontext()
        with expected:
            with history_indexing._online_cursor() as online:
                raw = online.connection
                maintenance_pid = raw.get_backend_pid()
                online.execute("SHOW application_name")
                assert online.fetchone()["application_name"] == ONLINE_DDL_APPLICATION_NAME
                if fail:
                    raise RuntimeError("test exit")
        assert raw.closed
        assert base.pool_stats()["in_use"] == 0
        assert base.pressure_stats()["connections_discarded"] == 1
        with base.get_db() as conn:
            assert conn._conn.get_backend_pid() != maintenance_pid
            cur = conn.cursor()
            # Startup's already-selected cancellation may arrive after handoff.
            # It can only target the old maintenance process, never this request.
            cur.execute("SELECT pg_cancel_backend(%s)", (maintenance_pid,))
            cur.execute("SELECT pg_sleep(0.01), 42 AS answer")
            assert cur.fetchone()["answer"] == 42
        assert base.pool_stats()["in_use"] == 0
        assert base.pressure_stats()["connections_opened"] == 2
    finally:
        pool.closeall()


def test_retirement_finishes_while_staggered_writers_keep_running(ready_projection):
    seed_old_indexes()
    stop = threading.Event()
    active = [threading.Event(), threading.Event()]
    commits = [0, 0]
    latencies = []

    def write(worker):
        i = 0
        while not stop.is_set():
            with base.get_db() as conn:
                cur = conn.cursor()
                cur.execute("SET LOCAL statement_timeout = '250ms'")
                before = time.monotonic()
                cur.execute("INSERT INTO fluid_session_nodes(session_id,node_id,ts,message) "
                            "VALUES (%s,%s,0,%s)", (f"retire-busy-{worker}", str(i),
                            Json({"role": "user", "content": [{"kind": "text", "text": "writerneedle"}]})))
                cur.execute("UPDATE fluid_session_nodes SET ts=1 WHERE session_id=%s AND node_id=%s",
                            (f"retire-busy-{worker}", str(i)))
                latencies.append(time.monotonic() - before)
                active[worker].set()
                # Constantly overlap transactions longer than the former 250ms
                # lock timeout; only the DML itself must obey the small bound.
                stop.wait(0.6)
            commits[worker] += 1
            i += 1

    def retire():
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if retirement.run_pass():
                return True
        return False

    with ThreadPoolExecutor(max_workers=3) as executor:
        writers = []
        try:
            writers.append(executor.submit(write, 0))
            assert active[0].wait(2)
            time.sleep(0.3)
            writers.append(executor.submit(write, 1))
            assert active[1].wait(2)
            assert executor.submit(retire).result(timeout=12)
            assert all(not writer.done() for writer in writers)
            assert all(commits)  # retirement progressed through real writer commits
            assert set(retirement.INDEXES).isdisjoint(names())
        finally:
            stop.set()
            for writer in writers:
                writer.result(timeout=3)
    assert latencies and max(latencies) < 0.5


def test_oversize_marker_stays_searchable_before_promotion(ready_projection):
    from app.db.sessions import _native_search_cte
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO sessions(id,sdk_session_id,title) VALUES ('retire-marker','retire-marker-sdk','Marker')")
        # Drive the actual INSERT trigger, not a fixture that fabricates a marker.
        cur.execute("INSERT INTO fluid_session_nodes(session_id,node_id,ts,message) VALUES "
                    "('retire-marker-sdk','one',0,%s)",
                    (Json({"role": "user", "content": [{"kind": "text", "text": "markerneedle " + "x" * 210000}]}),))
        cur.execute("SELECT part FROM session_recall_text WHERE source='n' AND session_id='retire-marker-sdk'")
        assert {r["part"] for r in cur.fetchall()} == {session_recall.PENDING}
        # Give the cost planner a selective corpus, not a four-row table where
        # walking the tiny ts btree is legitimately cheaper than opening a GIN.
        cur.execute("INSERT INTO sessions(id,sdk_session_id,title) "
                    "SELECT 'retire-plan-'||g, 'retire-plan-sdk-'||g, 'Filler' FROM generate_series(1,2000) g")
        cur.execute("INSERT INTO fluid_session_nodes(session_id,node_id,ts,message) "
                    "SELECT 'retire-plan-sdk-'||g, 'one', 0, jsonb_build_object('role','user','content',"
                    "jsonb_build_array(jsonb_build_object('kind','text','text', repeat(md5(g::text), 30)))) "
                    "FROM generate_series(1,2000) g")
        for table in ("sessions", "fluid_session_nodes", "session_recall_text"):
            cur.execute("ANALYZE " + table)
        # Actual CTE, ordinary planner, history partial indexes included.
        cte, pattern = _native_search_cte("s.id", "%s", "", cur, "boundaryneedle")
        cur.execute("EXPLAIN " + "WITH " + cte.rstrip().rstrip(",") + " SELECT * FROM native_matches",
                    ("boundaryneedle", pattern, "%boundaryneedle%"))
        plan = "\n".join(r["QUERY PLAN"] for r in cur.fetchall())
        assert "idx_session_recall_text_trgm_n" in plan, plan
        assert "message)::text" not in plan
    assert "retire-marker" in listed("markerneedle")


def test_cap_reaching_parts_preserve_historical_promoted_tail_matches(ready_projection):
    from app.db import sessions

    # Model an already-ready old installation: its capped rows have no pending
    # markers, and the new small tail index does not exist yet.
    with history_indexing._online_cursor() as cur:
        cur.execute(f"DROP INDEX CONCURRENTLY {session_recall.NATIVE_TAIL_INDEX}")
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO sessions(id,sdk_session_id,title) "
                    "VALUES ('retire-capped','retire-capped-sdk','Synthetic')")
        cur.execute("INSERT INTO fluid_session_nodes(session_id,node_id,ts,message) "
                    "VALUES ('retire-capped-sdk','one',0,jsonb_build_object('role','user','content',"
                    "jsonb_build_array(jsonb_build_object('kind','text','text',"
                    "repeat('x',5000010) || ' tailneedle'))))")
    before = listed("tailneedle")
    assert set(before) == {"retire-capped"}
    assert session_recall.promote_overflow(max_seconds=10, pause_seconds=0)
    with base.get_db() as conn:
        cur = conn.cursor()
        assert session_recall.ready(cur) and not session_recall.native_search_ready(cur)
        cur.execute("SELECT max(part) AS last, min(part) AS first FROM session_recall_text "
                    "WHERE source='n' AND session_id='retire-capped-sdk'")
        assert dict(cur.fetchone()) == {"last": session_recall.NATIVE_CAPPED_PART, "first": 0}
        cur.execute("SELECT pg_relation_filenode('session_recall_text') AS node, "
                    "(SELECT count(*) FROM session_recall_text) AS rows")
        original = cur.fetchone()
    # Before the index is built, legacy fallback must still see beyond the cap.
    assert listed("tailneedle") == before
    assert retirement.run_pass(seconds=10)
    assert listed("tailneedle") == before
    assert listed("tailneedle absent") == {}
    with base.get_db() as conn:
        cur = conn.cursor()
        assert session_recall.native_search_ready(cur)
        cur.execute("SELECT pg_relation_filenode('session_recall_text') AS node, "
                    "(SELECT count(*) FROM session_recall_text) AS rows")
        assert cur.fetchone() == original  # no rewrite, replay, or shape/version change
        tail = history_indexing.search_table(cur, "session_recall_text", "r")
        cur.execute(f"EXPLAIN SELECT r.session_id,r.row_key FROM {tail} "
                    f"WHERE r.source='n' AND r.part >= {session_recall.NATIVE_CAPPED_PART}")
        assert session_recall.NATIVE_TAIL_INDEX in str(cur.fetchall())
        cte, _ = sessions._native_search_cte("s.id", "%s", "", cur, "tailneedle")
        assert "message::text ILIKE" not in cte


def test_pg_trgm_install_failure_keeps_unindexed_search_correct(monkeypatch):
    from app.core.provider import fluid_pg_store as recall
    from app.db import schema_postgres

    class UnavailableTrigrams:
        def __init__(self, cursor):
            self.cursor = cursor

        def execute(self, sql, params=None):
            if sql.startswith("CREATE EXTENSION"):
                raise RuntimeError("synthetic unavailable pg_trgm")
            return self.cursor.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self.cursor, name)

    monkeypatch.setattr(recall, "_my_device_id", lambda: "test-local")
    session_recall.uninstall(lock_timeout_ms=250)
    try:
        with base.get_db() as conn:
            cur = conn.cursor()
            schema_postgres.create_postgres_schema(UnavailableTrigrams(cur))
            assert not session_recall.ready(cur)
            cur.execute("INSERT INTO sessions(id,sdk_session_id,title) "
                        "VALUES ('retire-degraded','retire-degraded-sdk','Synthetic')")
            cur.execute("INSERT INTO fluid_session_nodes(session_id,node_id,ts,message) "
                        "VALUES ('retire-degraded-sdk','one',0,%s)",
                        (Json({"role": "user", "content": [{"kind": "text", "text": "degradedneedle"}]}),))
        assert "retire-degraded" in listed("degradedneedle")
        assert {h.session_id for h in recall._search_sync(["degradedneedle"], 10, None)} == {"retire-degraded-sdk"}
        assert set(retirement.INDEXES).isdisjoint(names())
        assert not retirement.run_pass()
    finally:
        with base.get_db() as conn:
            schema_postgres.create_postgres_schema(conn.cursor())
        assert session_recall.run_backfill(max_seconds=5, pause_seconds=0)
