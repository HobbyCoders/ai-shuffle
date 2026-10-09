"""Agent SessionSearch, opt-in disposable PostgreSQL planner and parity checks."""
from contextlib import contextmanager
from dataclasses import asdict
from hashlib import md5

import pytest
from psycopg2.extras import Json, execute_values

from tests.db.postgres_harness import pg_schema, requires_real_pg  # noqa: F401

pytestmark = requires_real_pg


@pytest.fixture(scope="module")
def corpus():
    from fluid.messages import Message, Text

    from app.db import base, mobile_tool_identity

    mobile_tool_identity.init_schema()
    # A selective needle amidst synthetic code/log-sized JSON in all three
    # stores. Incompressible inline text makes heap scan cost visible to the
    # planner. No transcript/configuration from an owner database is used.
    with base.get_db() as conn:
        cur = conn.cursor()
        for arm in ("native", "delegated", "remote"):
            execute_values(cur, "INSERT INTO sessions(id, sdk_session_id, title, owner_device_id) VALUES %s", [
                (f"recall-{arm}-{i}", None if arm == "remote" else f"ses_recall-{arm}-{i}",
                 f"Synthetic {arm} {i}", "test-remote" if arm == "remote" else "test-local")
                for i in range(1000)
            ])
        special = "recallneedle quokkaneedle capybaraneedle 東京 50%_complete a.b xy x"
        native = []
        delegated = []
        remote = []
        for i in range(1000):
            for j in range(6):
                filler = "code/log " + " ".join(
                    md5(f"{i}-{j}-{k}".encode()).hexdigest() for k in range(40)
                )
                body = special if i < 3 and j == 0 else filler
                ts = i * 100 + j
                native.append((f"ses_recall-native-{i}", str(j), ts,
                               Json(Message("user", [Text(body)]).to_dict())))
                delegated.append(("test", f"ses_recall-delegated-{i}",
                                  Json({"type": "user", "message": {"content": body}}), ts))
                remote.append((f"recall-remote-{i}-{j}", f"recall-remote-{i}", j, str(j),
                               "message.user", Json({"content": body, "kind": "text"}), ts))
        execute_values(cur, "INSERT INTO fluid_session_nodes(session_id,node_id,ts,message) VALUES %s", native)
        execute_values(cur, "INSERT INTO session_store_entries(project_key,session_id,entry,created_at) VALUES %s",
                       delegated, template="(%s,%s,%s,to_timestamp(%s))")
        execute_values(cur, "INSERT INTO session_events(id,session_id,seq,turn_id,event_type,payload,created_at) VALUES %s",
                       remote, template="(%s,%s,%s,%s,%s,%s,to_timestamp(%s))")
        # Flush GIN's write-side pending list before asking the cost planner
        # about a steady-state search (otherwise its cost includes bulk inserts).
        cur.execute("SELECT gin_clean_pending_list(indexrelid) FROM pg_index "
                    "WHERE indexrelid::regclass::text IN "
                    "('idx_fluid_nodes_trgm','idx_sse_entry_trgm','idx_session_events_recall_trgm','idx_fluid_recall_text_trgm','idx_sse_recall_text_trgm','idx_events_recall_text_trgm')")
        for table in ("sessions", "fluid_session_nodes", "session_store_entries", "session_events"):
            cur.execute("ANALYZE " + table)
    yield
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM sessions WHERE id LIKE 'recall-%%'")
        cur.execute("DELETE FROM fluid_session_nodes WHERE session_id LIKE 'ses_recall-%%'")
        cur.execute("DELETE FROM session_store_entries WHERE project_key = 'test'")
        cur.execute("DELETE FROM session_events WHERE session_id LIKE 'recall-%%'")


def _index_names():
    from app.db import base
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()")
        return {r["indexname"] for r in cur.fetchall()}


def test_bootstrap_creates_all_agent_recall_indexes():
    assert {"idx_fluid_nodes_trgm", "idx_sse_entry_trgm", "idx_session_events_recall_trgm"} <= _index_names()
    assert "idx_fluid_recall_text_trgm" not in _index_names()


def test_upgrade_creates_mirror_index_and_is_idempotent(monkeypatch):
    from app.db import base, database, schema_postgres

    database.init_database()
    # Simulate an existing install at the current schema version, with the
    # previous build's fingerprint and no mirrored-event index. This must use
    # the actual startup migration gate, not rerun the bootstrap or force it.
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("DROP INDEX IF EXISTS idx_session_events_recall_trgm")
        database._set_marker(cur, database.MIGRATIONS_FINGERPRINT_KEY, "before-mirror-index")

    def unexpected_bootstrap(_cursor):
        pytest.fail("An upgrade at the current version must use the column pass")

    monkeypatch.setattr(schema_postgres, "create_postgres_schema", unexpected_bootstrap)
    database.init_database()
    assert "idx_session_events_recall_trgm" in _index_names()
    database.init_database()
    assert "idx_session_events_recall_trgm" in _index_names()


def test_capacity_and_column_failure_leave_startup_and_legacy_search_intact(monkeypatch):
    from app.db import base, database, schema_postgres
    from app.maintenance.session_recall_cache import apply_cache

    with pytest.raises(RuntimeError, match="Insufficient free space"):
        with base.get_db() as conn:
            apply_cache(conn.cursor(), 0)

    class FailingCursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def execute(self, sql, params=None):
            if sql.startswith("ALTER TABLE session_store_entries"):
                raise RuntimeError("synthetic disk full after first table rewrite")
            return self.cursor.execute(sql, params)

        def fetchone(self):
            return self.cursor.fetchone()

        def fetchall(self):
            return self.cursor.fetchall()

    with pytest.raises(RuntimeError, match="synthetic disk full"):
        with base.get_db() as conn:
            apply_cache(FailingCursor(conn.cursor()), 100 * 1024 ** 3)
    with base.get_db() as conn:
        cur = conn.cursor()
        assert not schema_postgres.session_recall_cache_ready(cur)
        for table in ("fluid_session_nodes", "session_store_entries", "session_events"):
            assert not base.column_exists(cur, table, "recall_text")
        database._set_marker(cur, database.MIGRATIONS_FINGERPRINT_KEY, "after-failed-cache")
    monkeypatch.setattr(schema_postgres, "create_session_recall_text",
                        lambda _: pytest.fail("Startup retried failed maintenance"))
    database.init_database()
    from app.core.provider.fluid_pg_store import _search_sync

    assert _search_sync(["absentneedle"], 5, None) == []
    assert {"idx_fluid_nodes_trgm", "idx_sse_entry_trgm", "idx_session_events_recall_trgm"} <= _index_names()


def test_maintenance_lock_wait_is_bounded_and_rolls_back():
    import time

    from psycopg2.errors import LockNotAvailable

    from app.db import base
    from app.maintenance.session_recall_cache import apply_cache

    with base.get_db() as blocker:
        blocker.cursor().execute("LOCK TABLE fluid_session_nodes IN ACCESS SHARE MODE")
        started = time.perf_counter()
        with pytest.raises(LockNotAvailable):
            with base.get_db() as conn:
                apply_cache(conn.cursor(), 100 * 1024 ** 3, lock_timeout_ms=50)
        assert time.perf_counter() - started < 2
    with base.get_db() as conn:
        assert not base.column_exists(conn.cursor(), "fluid_session_nodes", "recall_text")


def test_maintenance_cli_uses_real_database_capacity_check(monkeypatch, capsys):
    from app.db import base, schema_postgres
    from app.maintenance.session_recall_cache import main

    import os
    from pathlib import Path
    from types import SimpleNamespace

    from app.core.config import settings
    from app.maintenance import session_recall_cache

    # The caller must own this disposable cluster. conftest deliberately replaces
    # DATA_DIR, so inject the fixture layout explicitly rather than inspecting
    # a connection descriptor (or a privileged SHOW setting).
    fixture_root = os.environ.get("RECALL_TEST_DATA_DIR")
    assert fixture_root, "Set RECALL_TEST_DATA_DIR to the owned disposable cluster's managed layout root"
    fixture_data = Path(fixture_root)
    assert fixture_data.is_absolute()
    assert (fixture_data / "postgres" / "data" / "PG_VERSION").is_file()
    monkeypatch.setattr(settings, "data_dir", fixture_data)
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT rolsuper, pg_has_role(current_user, 'pg_read_all_settings', 'USAGE') AS can_read "
                    "FROM pg_roles WHERE rolname = current_user")
        role = cur.fetchone()
        assert role["rolsuper"] is False
        assert role["can_read"] is False

    # Keep the fixture pool while driving the CLI's real local disk check,
    # capacity refusal, rollback, bounded transaction and success commit.
    monkeypatch.setattr(base, "init_pool", lambda **_: None)
    monkeypatch.setattr(base, "close_pool", lambda: None)
    disk_usage = session_recall_cache.shutil.disk_usage
    measured = []

    def no_capacity(path):
        measured.append(path)
        return SimpleNamespace(free=0)

    monkeypatch.setattr(session_recall_cache.shutil, "disk_usage", no_capacity)
    assert main(["--apply"]) == 1
    refusal = capsys.readouterr().out
    assert "Recall maintenance failed" in refusal
    assert measured == [fixture_data / "postgres" / "data"], refusal
    with base.get_db() as conn:
        cur = conn.cursor()
        assert not schema_postgres.session_recall_cache_ready(cur)
        for table in ("fluid_session_nodes", "session_store_entries", "session_events"):
            assert not base.column_exists(cur, table, "recall_text")

    monkeypatch.setattr(session_recall_cache.shutil, "disk_usage", disk_usage)
    assert main(["--apply"]) == 0
    assert "Recall cache committed" in capsys.readouterr().out
    with base.get_db() as conn:
        assert schema_postgres.session_recall_cache_ready(conn.cursor())


def test_offline_migration_backfills_stored_text_and_follows_updates(monkeypatch):
    from app.db import base, database, schema_postgres

    database.init_database()
    shapes = (
        ("fluid_session_nodes", "message", "session_id,node_id,ts,message", ("generated-native", "0", 1)),
        ("session_store_entries", "entry", "project_key,session_id,entry", ("generated", "generated-sdk")),
        ("session_events", "payload", "id,session_id,seq,event_type,payload",
         ("generated-event", "generated-remote", 0, "message.user")),
    )
    with base.get_db() as conn:
        cur = conn.cursor()
        for table, _column, columns, values in shapes:
            cur.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS recall_text CASCADE")
            placeholders = ",".join(["%s"] * (len(values) + 1))
            cur.execute(f"INSERT INTO {table}({columns}) VALUES ({placeholders})",
                        (*values, Json({"content": "Mixed CASE 東京 50%_complete a.b", "extra": "\\escape"})))
        database._set_marker(cur, database.MIGRATIONS_FINGERPRINT_KEY, "before-generated-recall-text")

    def unexpected_bootstrap(_cursor):
        pytest.fail("Current-version upgrade must use the fingerprint-gated column pass")

    monkeypatch.setattr(schema_postgres, "create_postgres_schema", unexpected_bootstrap)
    database.init_database()  # Normal startup must leave the optional cache alone.
    from app.maintenance.session_recall_cache import apply_cache

    with base.get_db() as conn:
        cur = conn.cursor()
        for table, _column, _columns, _values in shapes:
            assert not base.column_exists(cur, table, "recall_text")
        apply_cache(cur, 100 * 1024 ** 3)
    with base.get_db() as conn:
        apply_cache(conn.cursor(), 100 * 1024 ** 3)  # Explicit retry is idempotent.
        database._set_marker(conn.cursor(), database.MIGRATIONS_FINGERPRINT_KEY, "after-cache-test")
    database.init_database()  # Even a forced upgrade must not recreate retired indexes.
    with base.get_db() as conn:
        cur = conn.cursor()
        for table, column, _columns, _values in shapes:
            cur.execute(f"SELECT recall_text = {column}::text AS same FROM {table} "
                        "WHERE session_id LIKE 'generated-%%'")
            assert cur.fetchone()["same"] is True
            cur.execute(f"UPDATE {table} SET {column} = %s WHERE session_id LIKE 'generated-%%'",
                        (Json({"content": "updated quokka"}),))
            cur.execute(f"SELECT recall_text = {column}::text AS same FROM {table} "
                        "WHERE session_id LIKE 'generated-%%'")
            assert cur.fetchone()["same"] is True
            cur.execute("SELECT is_generated FROM information_schema.columns "
                        "WHERE table_schema = current_schema() AND table_name = %s AND column_name = 'recall_text'",
                        (table,))
            assert cur.fetchone()["is_generated"] == "ALWAYS"
            cur.execute(f"DELETE FROM {table} WHERE session_id LIKE 'generated-%%'")
    assert {"idx_fluid_recall_text_trgm", "idx_sse_recall_text_trgm", "idx_events_recall_text_trgm"} <= _index_names()
    assert {"idx_sse_entry_trgm", "idx_session_events_recall_trgm"}.isdisjoint(_index_names())


@pytest.mark.asyncio
async def test_real_store_and_event_writers_feed_stored_text_recall(monkeypatch):
    from fluid.messages import Message, Text
    from fluid.store.base import SessionNode

    from app.core.pg_session_store import PgSessionStore
    from app.core.provider import fluid_pg_store as recall
    from app.db import base, session_events

    monkeypatch.setattr(recall, "_my_device_id", lambda: "test-local")
    monkeypatch.setattr(recall, "_worker_name", lambda _: "Test mirror")
    native, sdk, remote = "ses_producer-native", "ses_producer-sdk", "producer-remote"
    body = "producerquokka 東京 50%_complete"
    with base.get_db() as conn:
        cur = conn.cursor()
        execute_values(cur, "INSERT INTO sessions(id,sdk_session_id,title,owner_device_id) VALUES %s", [
            ("producer-native", native, "Native writer", "test-local"),
            ("producer-sdk", sdk, "CLI writer", "test-local"),
            (remote, None, "Event writer", "test-remote"),
        ])
    store = recall.PgFluidStore()
    await store.create_session(native)
    await store.append(native, SessionNode(node_id="0", parent_id=None, message=Message("user", [Text(body)])))
    cli = PgSessionStore()
    key = {"project_key": "producer", "session_id": sdk}
    await cli.append(key, [{"type": "user", "message": {"content": body}}])
    await cli.append({**key, "subpath": "subagent.jsonl"}, [
        {"type": "user", "message": {"content": "sidechainonly"}},
    ])
    session_events.append_events(remote, [
        {"type": "message.user", "payload": {"content": body}},
        {"type": "tool.output", "payload": {"content": "toolonly"}},
    ])
    hits = recall._search_sync(["producerquokka"], 5, None)
    assert {h.session_id for h in hits} == {native, sdk, remote}
    assert recall._search_sync(["sidechainonly", "toolonly"], 5, None) == []
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT recall_text FROM session_store_entries WHERE subpath = 'subagent.jsonl' "
                    "AND project_key = 'producer'")
        assert cur.fetchone()["recall_text"] is None
        cur.execute("SELECT recall_text FROM session_events WHERE session_id = %s AND event_type = 'tool.output'",
                    (remote,))
        assert cur.fetchone()["recall_text"] is None
        for table in ("fluid_session_nodes", "fluid_sessions", "session_store_entries",
                      "session_store_summaries", "session_events", "session_event_counters"):
            cur.execute(f"DELETE FROM {table} WHERE session_id IN (%s, %s, %s)", (native, sdk, remote))
        cur.execute("DELETE FROM sessions WHERE id LIKE 'producer-%%'")


@pytest.fixture
def recall(monkeypatch, corpus):
    from app.core.provider import fluid_pg_store
    monkeypatch.setattr(fluid_pg_store, "_my_device_id", lambda: "test-local")
    monkeypatch.setattr(fluid_pg_store, "_worker_name", lambda _: "Test mirror")
    return fluid_pg_store


def test_incomplete_cache_falls_back_without_changing_ranked_hits(recall):
    from app.db import base, schema_postgres

    terms = ["recallneedle", "quokkaneedle", "capybaraneedle"]
    cached = [asdict(h) for h in recall._search_sync(terms, 5, "recall-remote-0")]
    with base.get_db() as conn:
        conn.cursor().execute("ALTER INDEX idx_sse_recall_text_trgm RENAME TO incomplete_cache_index")
    try:
        with base.get_db() as conn:
            assert not schema_postgres.session_recall_cache_ready(conn.cursor())
        fallback = [asdict(h) for h in recall._search_sync(terms, 5, "recall-remote-0")]
        assert cached == fallback and cached
    finally:
        with base.get_db() as conn:
            conn.cursor().execute("ALTER INDEX incomplete_cache_index RENAME TO idx_sse_recall_text_trgm")


def test_actual_agent_queries_choose_all_three_trigram_indexes(recall, monkeypatch):
    original = recall.get_db
    queries = []

    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def execute(self, sql, params=None):
            if sql.startswith(("WITH m AS", "SELECT session_id, MAX(h)")):
                queries.append((sql, params))
            return self.cursor.execute(sql, params)

        def __getattr__(self, key):
            return getattr(self.cursor, key)

    class Connection:
        def __init__(self, conn):
            self.conn = conn

        def cursor(self):
            return Cursor(self.conn.cursor())

    @contextmanager
    def capture():
        with original() as conn:
            yield Connection(conn)

    monkeypatch.setattr(recall, "get_db", capture)
    hits = recall._search_sync(["recallneedle", "quokkaneedle", "capybaraneedle"], 5, None)
    assert len(hits) == 9 and {h.worker for h in hits} == {None, "Test mirror"}
    assert len(queries) == 3
    for (sql, params), index in zip(queries, (
        "idx_fluid_recall_text_trgm", "idx_sse_recall_text_trgm", "idx_events_recall_text_trgm",
    )):
        with original() as conn:
            cur = conn.cursor()
            # Normal cost-based planner, not enable_seqscan=off. The scoring
            # and predicate must both read stored text, not serialize JSONB.
            assert "::text" not in sql and "recall_text ILIKE" in sql
            cur.execute("EXPLAIN (FORMAT JSON) " + sql, params)
            plan = str(cur.fetchone()["QUERY PLAN"])
            assert index in plan, plan


def test_toasted_cache_selective_plan_and_transaction_local_cost(recall, monkeypatch):
    from fluid.messages import Message, Text

    original = recall.get_db
    queries = []
    costs = []

    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def execute(self, sql, params=None):
            result = self.cursor.execute(sql, params)
            if sql.startswith("SELECT set_config"):
                costs.append(float(self.cursor.fetchone()["set_config"]))
            if sql.startswith("WITH m AS"):
                queries.append((sql, params))
            return result

        def __getattr__(self, name):
            return getattr(self.cursor, name)

    class Connection:
        def __init__(self, conn):
            self.conn = conn

        def cursor(self):
            return Cursor(self.conn.cursor())

    with original() as conn:
        cur = conn.cursor()
        cur.execute("SELECT current_setting('cpu_tuple_cost') AS cost, pg_backend_pid() AS pid")
        before = cur.fetchone()
        # Shadow just the native arm on this connection. The ordinary corpus
        # has inline rows, so it cannot catch the underestimated TOAST scan.
        cur.execute("CREATE TEMP TABLE fluid_session_nodes "
                    "(LIKE public.fluid_session_nodes INCLUDING ALL) ON COMMIT DROP")
        # Reproduce maintenance's heap rewrite and bulk index build, rather
        # than inserting into a cache/index that already exists.
        cur.execute("ALTER TABLE pg_temp.fluid_session_nodes DROP COLUMN recall_text")
        text = "log code sample " + " ".join(md5(str(k).encode()).hexdigest() for k in range(118))
        rows = []
        # 7,200 rows retain the transition; 3,584 still scan at the floor.
        for i in range(225):
            for j in range(32):
                body = "zebrafish quokka capybara " + text if i < 3 and j == 0 else text
                rows.append((f"toasted-{i}", str(j), i * 100 + j,
                             Json(Message("user", [Text(body)]).to_dict())))
        execute_values(cur, "INSERT INTO fluid_session_nodes(session_id,node_id,ts,message) VALUES %s", rows)
        cur.execute("ALTER TABLE pg_temp.fluid_session_nodes ADD COLUMN recall_text TEXT "
                    "GENERATED ALWAYS AS (CASE WHEN TRUE THEN message::text END) STORED")
        cur.execute("CREATE INDEX toasted_recall_gin ON pg_temp.fluid_session_nodes "
                    "USING gin (recall_text gin_trgm_ops)")
        cur.execute("ANALYZE pg_temp.fluid_session_nodes")

        @contextmanager
        def capture():
            yield Connection(conn)

        with monkeypatch.context() as mp:
            mp.setattr(recall, "get_db", capture)
            for configured, expected in (("0.01", 0.05), ("0.2", 0.2)):
                cur.execute("SELECT set_config('cpu_tuple_cost', %s, true)", (configured,))
                hits = recall._search_sync(["zebrafish", "quokka", "capybara"], 5, None)
                assert len([h for h in hits if h.session_id.startswith("toasted-")]) == 3
                assert costs[-1] == expected
                sql, params = queries[-2]  # Native, followed by delegated.
                assert "FROM fluid_session_nodes" in sql
                cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, params)
                plan = cur.fetchone()["QUERY PLAN"][0]["Plan"]

                def nodes(node):
                    yield node
                    for child in node.get("Plans", []):
                        yield from nodes(child)

                if configured == "0.01":
                    # The per-term UNION now avoids the old selective scan even
                    # at default cost. Keep the old query as the negative control
                    # for the TOAST/cost-floor regression, not a requirement that
                    # the new query must still suffer it.
                    captured = []

                    class Capture:
                        def execute(self, sql, params):
                            captured.append((sql, params))

                        def fetchall(self):
                            return []

                    _case_top_rows(Capture(), "fluid_session_nodes", "recall_text", "ts", "TRUE", [],
                                   "session_id, ts, message", ["%zebrafish%", "%quokka%", "%capybara%"])
                    old_sql, old_params = captured[0]
                    cur.execute("SELECT set_config('cpu_tuple_cost', '0.01', true)")
                    cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + old_sql, old_params)
                    baseline = cur.fetchone()["QUERY PLAN"][0]["Plan"]
                    assert any(n["Node Type"] == "Seq Scan" and
                               n.get("Relation Name") == "fluid_session_nodes"
                               for n in nodes(baseline)), baseline
                    cur.execute("SELECT set_config('cpu_tuple_cost', '0.05', true)")

                scans = list(nodes(plan))
                assert any(n["Node Type"] == "Bitmap Index Scan" and
                           n.get("Index Name") == "toasted_recall_gin" for n in scans), plan
                assert not any(n["Node Type"] == "Seq Scan" and
                               n.get("Relation Name") == "fluid_session_nodes" for n in scans), plan
    # A second transaction on the pooled connection must not inherit the
    # search's cost floor (nor the deliberately higher operator setting).
    with original() as conn:
        cur = conn.cursor()
        cur.execute("SELECT current_setting('cpu_tuple_cost') AS cost, pg_backend_pid() AS pid")
        assert cur.fetchone() == before


def test_cache_cost_does_not_leak_after_rollback_on_same_backend(recall, monkeypatch):
    from psycopg2.errors import DivisionByZero

    original = recall.get_db
    with original() as conn:
        cur = conn.cursor()
        cur.execute("SELECT current_setting('cpu_tuple_cost') AS cost, pg_backend_pid() AS pid")
        before = cur.fetchone()

    with pytest.raises(DivisionByZero), original() as conn:
        @contextmanager
        def shared_transaction():
            yield conn

        with monkeypatch.context() as mp:
            mp.setattr(recall, "get_db", shared_transaction)
            assert recall._search_sync(["recallneedle", "quokkaneedle"], 5, None)
        cur = conn.cursor()
        cur.execute("SELECT current_setting('cpu_tuple_cost') AS cost, pg_backend_pid() AS pid")
        during = cur.fetchone()
        assert during["pid"] == before["pid"]
        assert float(during["cost"]) == max(float(before["cost"]), 0.05)
        cur.execute("SELECT 1 / 0")

    # This is a real pool release/recheckout after get_db() rolls back.
    with original() as conn:
        cur = conn.cursor()
        cur.execute("SELECT current_setting('cpu_tuple_cost') AS cost, pg_backend_pid() AS pid")
        assert cur.fetchone() == before


def _case_top_rows(cur, table, column, ts, where, where_params, select, patterns):
    """Pre-#2084 CASE scoring control, with the shared deterministic tie policy."""
    from app.core.provider import fluid_pg_store as recall

    any_term = " OR ".join([f"{column} ILIKE %s"] * len(patterns))
    key = "node_id" if table == "fluid_session_nodes" else "id"
    selected = ", ".join(f"d.{name.strip()}" for name in select.split(","))
    cur.execute(
        f"WITH m AS (SELECT session_id, {key}, {ts} AS t, {recall._hits_sql(column, len(patterns))} AS h "
        f"FROM {table} WHERE {where} AND ({any_term})), "
        "s AS (SELECT session_id, MAX(h) AS best, MAX(t) AS last FROM m "
        f"GROUP BY session_id ORDER BY best DESC, last DESC, session_id LIMIT {recall._SEARCH_PREFILTER_SESSIONS}), "
        "r AS (SELECT m.*, ROW_NUMBER() OVER (PARTITION BY m.session_id "
        f"ORDER BY m.h DESC, m.t DESC, m.{key}) AS rn FROM m JOIN s ON s.session_id = m.session_id) "
        f"SELECT {selected} FROM r JOIN {table} d ON d.session_id = r.session_id "
        f"AND d.{key} = r.{key} WHERE rn <= {recall._SEARCH_ROWS_PER_SESSION} "
        f"ORDER BY r.session_id, r.h DESC, r.t DESC, r.{key}",
        list(patterns) + list(where_params) + list(patterns),
    )
    return [dict(r) for r in cur.fetchall()]


def _legacy_top_rows(cur, table, column, ts, where, where_params, select, patterns):
    """Pre-#2000 raw-JSON control, with the shared deterministic tie policy."""
    from app.core.provider import fluid_pg_store as recall

    column = "message::text" if table == "fluid_session_nodes" else "entry::text"
    key = "node_id" if table == "fluid_session_nodes" else "id"
    any_term = " OR ".join([f"{column} ILIKE %s"] * len(patterns))
    cur.execute(
        f"WITH m AS (SELECT {select}, {key} AS tie_key, {ts} AS t, {recall._hits_sql(column, len(patterns))} AS h "
        f"FROM {table} WHERE {where} AND ({any_term})), "
        "s AS (SELECT session_id, MAX(h) AS best, MAX(t) AS last FROM m "
        f"GROUP BY session_id ORDER BY best DESC, last DESC, session_id LIMIT {recall._SEARCH_PREFILTER_SESSIONS}), "
        "r AS (SELECT m.*, ROW_NUMBER() OVER (PARTITION BY m.session_id "
        "ORDER BY m.h DESC, m.t DESC, m.tie_key) AS rn FROM m JOIN s ON s.session_id = m.session_id) "
        f"SELECT {select} FROM r WHERE rn <= {recall._SEARCH_ROWS_PER_SESSION} "
        "ORDER BY r.session_id, r.h DESC, r.t DESC, r.tie_key",
        list(patterns) + list(where_params) + list(patterns),
    )
    return [dict(r) for r in cur.fetchall()]


def _legacy_candidate_sessions(cur, source, source_params, column, ts, where, where_params,
                               patterns, owner="NULL"):
    from app.core.provider import fluid_pg_store as recall

    source = source.replace("e.recall_text", "e.payload")
    column = "payload::text"
    any_term = " OR ".join([f"{column} ILIKE %s"] * len(patterns))
    cur.execute(
        "SELECT session_id, MAX(h) AS best, MAX(t) AS last, MAX(owner) AS owner FROM ("
        f"SELECT session_id, {ts} AS t, {recall._hits_sql(column, len(patterns))} AS h, "
        f"{owner} AS owner FROM {source} WHERE {where} AND ({any_term})"
        ") m GROUP BY session_id ORDER BY best DESC, last DESC "
        f"LIMIT {recall._SEARCH_PREFILTER_SESSIONS}",
        list(patterns) + list(source_params) + list(where_params) + list(patterns),
    )
    return [dict(r) for r in cur.fetchall()]


@pytest.mark.asyncio
@pytest.mark.parametrize("cached", [True, False])
async def test_tied_writer_rows_have_identical_capped_scores_and_snippets(recall, monkeypatch, cached):
    """Forty equal-time/hit rows must choose the same twenty under every plan.

    Some rows contain two matching text blocks, so choosing a different tied
    row changes both the Python score and snippets despite equal SQL scores.
    Drive both real writers; the CLI batch naturally shares its NOW() stamp.
    """
    from fluid.messages import Message, Text
    from fluid.store.base import SessionNode

    from app.core.pg_session_store import PgSessionStore
    from app.db import base, schema_postgres

    native, sdk = "ses_tied-native", "ses_tied-sdk"
    with base.get_db() as conn:
        schema_postgres.create_session_recall_text(conn.cursor())
    store = recall.PgFluidStore()
    await store.create_session(native)
    entries = []
    for i in reversed(range(40)):
        texts = [f"{i:02d} tiesneedle"]
        if i % 4 == 0:
            texts.append(f"bonus{i:02d} tiesneedle")
        await store.append(native, SessionNode(node_id=f"{i:02d}", parent_id=None, ts=42,
                                              message=Message("user", [Text(t) for t in texts])))
        entries.append({"type": "user", "message": {"content": [
            {"type": "text", "text": t} for t in texts
        ]}})
    await PgSessionStore().append({"project_key": "tied", "session_id": sdk}, entries)
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(DISTINCT created_at) AS stamps FROM session_store_entries WHERE session_id = %s",
                    (sdk,))
        assert cur.fetchone()["stamps"] == 1
        if not cached:
            cur.execute("ALTER INDEX idx_sse_recall_text_trgm RENAME TO tied_incomplete_cache")
        assert schema_postgres.session_recall_cache_ready(cur) is cached

    try:
        terms = ["tiesneedle", "absentneedle"]
        indexed = recall._search_sync(terms, 5, None)
        hits = {h.session_id: h for h in indexed}
        assert set(hits) == {native, sdk}
        # Native node_id ASC selects 00..19, while CLI id ASC selects the
        # earliest inserts (39..20); each contains exactly five double blocks.
        assert hits[native].score == hits[sdk].score == 25
        assert hits[native].texts == [("user", t) for t in
                                      ("00 tiesneedle", "bonus00 tiesneedle", "01 tiesneedle")]
        assert hits[sdk].texts == [("user", t) for t in
                                   ("39 tiesneedle", "38 tiesneedle", "37 tiesneedle")]
        with monkeypatch.context() as mp:
            mp.setattr(recall, "_top_rows", _case_top_rows)
            cases = recall._search_sync(terms, 5, None)
        with monkeypatch.context() as mp:
            mp.setattr(recall, "_top_rows", _legacy_top_rows)
            legacy = recall._search_sync(terms, 5, None)
        original = recall.get_db

        @contextmanager
        def scans():
            with original() as conn:
                conn.cursor().execute("SET LOCAL enable_indexscan = off")
                conn.cursor().execute("SET LOCAL enable_bitmapscan = off")
                yield conn

        with monkeypatch.context() as mp:
            mp.setattr(recall, "get_db", scans)
            scanned = recall._search_sync(terms, 5, None)
        expected = [asdict(h) for h in indexed]
        assert expected == [asdict(h) for h in cases] == [asdict(h) for h in legacy] == [asdict(h) for h in scanned]
    finally:
        with base.get_db() as conn:
            cur = conn.cursor()
            if not cached:
                cur.execute("ALTER INDEX tied_incomplete_cache RENAME TO idx_sse_recall_text_trgm")
            for table in ("fluid_session_nodes", "fluid_sessions", "session_store_entries", "session_store_summaries"):
                cur.execute(f"DELETE FROM {table} WHERE session_id IN (%s, %s)", (native, sdk))


@pytest.mark.parametrize("terms", [
    ["recallneedle", "quokkaneedle", "capybaraneedle"], ["code", "log", "recallneedle"], ["xy"], ["x"],
    ["東京"], ["a.b"], ["50%_complete"], ["%"], ["_"], ["absentneedle"],
    ["code", "code", "log"], ["%", "_", "xy"], ["50%_complete", "東京", "xy"],
])
def test_index_and_scan_return_identical_ranked_hits(recall, monkeypatch, terms):
    original = recall.get_db

    @contextmanager
    def scans():
        with original() as conn:
            cur = conn.cursor()
            cur.execute("SET LOCAL enable_indexscan = off")
            cur.execute("SET LOCAL enable_bitmapscan = off")
            yield conn

    indexed = recall._search_sync(terms, 5, "recall-remote-0")
    with monkeypatch.context() as mp:
        mp.setattr(recall, "get_db", scans)
        scanned = recall._search_sync(terms, 5, "recall-remote-0")
    assert [asdict(h) for h in indexed] == [asdict(h) for h in scanned]
    with monkeypatch.context() as mp:
        mp.setattr(recall, "_top_rows", _case_top_rows)
        cases = recall._search_sync(terms, 5, "recall-remote-0")
    assert [asdict(h) for h in indexed] == [asdict(h) for h in cases]
    assert "recall-remote-0" not in {h.session_id for h in indexed}
    with monkeypatch.context() as mp:
        mp.setattr(recall, "_top_rows", _legacy_top_rows)
        mp.setattr(recall, "_candidate_sessions", _legacy_candidate_sessions)
        legacy = recall._search_sync(terms, 5, "recall-remote-0")
    assert [asdict(h) for h in indexed] == [asdict(h) for h in legacy]


def test_events_heavy_toasted_recall_benchmark(recall, monkeypatch):
    """#2084: 5,400 TOASTed nodes / 86,400 events (16:1), no timing gate.

    Print warm end-to-end p50s for the frozen CASE query and the term union;
    gate exact ranked parity and fewer native buffer accesses instead of a
    flaky wall-clock budget. All data lives in ON COMMIT DROP temp tables.
    """
    import statistics
    import time

    from fluid.messages import Message, Text

    from app.db import base, schema_postgres

    optimized = recall._top_rows
    with base.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '60s'")
        # Also support running just this benchmark on a fresh disposable DB.
        schema_postgres.create_session_recall_text(cur)
        cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        assert cur.fetchone()["rolsuper"] is False
        for table in ("sessions", "fluid_session_nodes", "session_store_entries", "session_events"):
            cur.execute(f"CREATE TEMP TABLE {table} (LIKE public.{table} INCLUDING DEFAULTS) ON COMMIT DROP")
        cur.execute("ALTER TABLE pg_temp.sessions ADD PRIMARY KEY (id)")
        cur.execute("ALTER TABLE pg_temp.fluid_session_nodes ADD PRIMARY KEY (session_id, node_id)")
        cur.execute("CREATE INDEX ON pg_temp.session_events (session_id, seq)")
        for table in ("fluid_session_nodes", "session_store_entries", "session_events"):
            cur.execute(f"ALTER TABLE pg_temp.{table} DROP COLUMN recall_text")
        execute_values(cur, "INSERT INTO sessions(id,sdk_session_id,title,owner_device_id) VALUES %s", [
            (f"bench-{arm}-{i}", f"ses_bench-{i}" if arm == "native" else None,
             f"Benchmark {arm} {i}", "test-local" if arm == "native" else "test-remote")
            for arm in ("native", "remote") for i in range(225)
        ])
        nodes = []
        events = []
        for i in range(225):
            for j in range(24):
                k = i * 24 + j
                # Incompressible, TOAST-sized code/logs. Common terms overlap,
                # but most OR matches do NOT match all three scoring terms.
                tags = " ".join(t for t, stride in (("code", 2), ("log", 3), ("sample", 31), ("the", 1))
                                if k % stride == 0)
                if i < 3 and j == 0:
                    tags += " recallneedle quokkaneedle capybaraneedle"
                filler = " ".join(md5(f"{k}-{n}".encode()).hexdigest() for n in range(160))
                nodes.append((f"ses_bench-{i}", str(j), k,
                              Json(Message("user", [Text(tags + " " + filler)]).to_dict())))
            for j in range(384):
                textual = j % 16 == 0
                events.append((f"bench-{i}-{j}", f"bench-remote-{i}", j,
                               "message.user" if textual else "tool.result",
                               Json({"content": "code log sample the" if textual else "ignored tool output"}),
                               i * 384 + j))
        execute_values(cur, "INSERT INTO fluid_session_nodes(session_id,node_id,ts,message) VALUES %s", nodes)
        execute_values(cur, "INSERT INTO session_events(id,session_id,seq,event_type,payload,created_at) VALUES %s",
                       events, template="(%s,%s,%s,%s,%s,to_timestamp(%s))")
        shapes = (
            ("fluid_session_nodes", "message::text", "TRUE"),
            ("session_store_entries", "entry::text", "subpath IS NULL"),
            ("session_events", "payload::text",
             "event_type IN ('message.user', 'message.assistant', 'assistant.text_delta')"),
        )
        for table, expression, predicate in shapes:
            cur.execute(f"ALTER TABLE pg_temp.{table} ADD COLUMN recall_text TEXT GENERATED ALWAYS AS "
                        f"(CASE WHEN {predicate} THEN {expression} END) STORED")
            cur.execute(f"CREATE INDEX ON pg_temp.{table} USING gin (recall_text gin_trgm_ops) WHERE {predicate}")
            cur.execute(f"ANALYZE pg_temp.{table}")
        cur.execute("ANALYZE pg_temp.sessions")

        @contextmanager
        def shared():
            yield conn

        monkeypatch.setattr(recall, "get_db", shared)
        for terms in (["recallneedle", "quokkaneedle", "capybaraneedle"],
                      ["log", "code", "sample"], ["the"]):
            timings = {"before": [], "after": []}
            for run in range(4):
                results = {}
                for name in (("before", "after") if run % 2 == 0 else ("after", "before")):
                    monkeypatch.setattr(recall, "_top_rows", _case_top_rows if name == "before" else optimized)
                    start = time.perf_counter()
                    results[name] = [asdict(h) for h in recall._search_sync(terms, 5, "bench-remote-0")]
                    if run:  # discard one warm-up pair
                        timings[name].append((time.perf_counter() - start) * 1000)
                assert results["before"] == results["after"] and results["after"]
            print("events-heavy p50 ms", " ".join(terms),
                  {name: round(statistics.median(values), 2) for name, values in timings.items()})

        plans = {}
        for name, query in (("before", _case_top_rows), ("after", optimized)):
            class Explain:
                def execute(self, sql, params):
                    cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, params)
                    plans[name] = cur.fetchone()["QUERY PLAN"][0]["Plan"]

                def fetchall(self):
                    return []

            query(Explain(), "fluid_session_nodes", "recall_text", "ts", "TRUE", [],
                  "session_id, ts, message", ["%log%", "%code%", "%sample%"])

        def accesses(plan):
            return sum(plan.get(key, 0) for key in
                       ("Shared Hit Blocks", "Shared Read Blocks", "Local Hit Blocks", "Local Read Blocks"))

        print("events-heavy native buffer accesses", {name: accesses(plan) for name, plan in plans.items()})
        assert accesses(plans["after"]) < accesses(plans["before"]), plans
