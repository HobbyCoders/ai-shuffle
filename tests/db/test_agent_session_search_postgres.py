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
                    "('idx_fluid_nodes_trgm','idx_sse_entry_trgm','idx_session_events_recall_trgm')")
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


@pytest.fixture
def recall(monkeypatch, corpus):
    from app.core.provider import fluid_pg_store
    monkeypatch.setattr(fluid_pg_store, "_my_device_id", lambda: "test-local")
    monkeypatch.setattr(fluid_pg_store, "_worker_name", lambda _: "Test mirror")
    return fluid_pg_store


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
        "idx_fluid_nodes_trgm", "idx_sse_entry_trgm", "idx_session_events_recall_trgm",
    )):
        with original() as conn:
            cur = conn.cursor()
            # Normal cost-based planner, not enable_seqscan=off.
            cur.execute("EXPLAIN (FORMAT JSON) " + sql, params)
            plan = str(cur.fetchone()["QUERY PLAN"])
            assert index in plan, plan


@pytest.mark.parametrize("terms", [
    ["recallneedle", "quokkaneedle", "capybaraneedle"], ["xy"], ["x"],
    ["東京"], ["a.b"], ["50%_complete"], ["%"], ["_"], ["absentneedle"],
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
    assert "recall-remote-0" not in {h.session_id for h in indexed}
