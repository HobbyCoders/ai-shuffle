"""Online retirement and reader parity on disposable PostgreSQL only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict
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
