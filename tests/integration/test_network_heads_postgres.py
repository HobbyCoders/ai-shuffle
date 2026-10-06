"""Head-query contracts against an explicitly selected disposable PostgreSQL."""
import os
from contextlib import closing, contextmanager
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("ALLOW_REAL_PG") != "1",
                                reason="requires disposable TEST_POSTGRES_DSN")


@pytest.fixture
def pg(tmp_path, monkeypatch):
    import psycopg2
    from psycopg2.extensions import parse_dsn
    from psycopg2.extras import RealDictCursor

    params = parse_dsn(os.environ["TEST_POSTGRES_DSN"])
    assert params.get("host") in {"localhost", "127.0.0.1"}
    params.update(host="127.0.0.1", hostaddr="127.0.0.1", connect_timeout=5, sslmode="disable")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app.db import network as store

    schema = "head_test_" + uuid4().hex
    with closing(psycopg2.connect(**params)) as conn, conn:
        conn.cursor().execute("CREATE SCHEMA " + schema)

    @contextmanager
    def connect():
        conn = psycopg2.connect(**params, cursor_factory=RealDictCursor,
                               options=f"-c search_path={schema} -c statement_timeout=10000")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    monkeypatch.setattr(store, "get_db", connect)
    with connect() as conn:
        store.create_schema(conn.cursor())
    try:
        yield store, connect
    finally:
        with closing(psycopg2.connect(**params)) as conn, conn:
            conn.cursor().execute("DROP SCHEMA " + schema + " CASCADE")


def insert(store, cursor, origin, incarnation, seq, record="same"):
    store.insert_op(cursor, {"origin_device_id": origin, "origin_incarnation": incarnation,
        "origin_seq": seq, "collection": "tags", "record_id": record, "op": "upsert", "clock": seq}, "settings")


@pytest.mark.parametrize("streams", [0, 1, 255, 256, 259])
def test_complete_heads_and_ops_across_probe_boundary(pg, streams):
    store, connect = pg
    with connect() as conn:
        cur = conn.cursor()
        for n in range(streams):
            # Multiple incarnations of an origin are independent streams.
            for seq in (1, 7):
                insert(store, cur, f"worker-{n // 3:04d}", n % 3 + 1, seq, f"r{n}")
        cur.execute("SELECT origin_device_id,incarnation,MAX(seq) AS s FROM sync_ops GROUP BY origin_device_id,incarnation")
        expected = {}
        for row in cur.fetchall():
            expected.setdefault(row["origin_device_id"], {})[str(row["incarnation"])] = row["s"]
    assert store.local_watermarks() == expected
    assert len(store.ops_since({}, limit=1000)) == streams * 2
    assert store.ops_since(expected) == []
    if streams:
        assert store.requires_snapshot({})  # gaps must not look like a complete log


def test_pruning_rollback_and_snapshot_watermarks_stay_authoritative(pg):
    store, connect = pg
    with connect() as conn:
        cur = conn.cursor()
        insert(store, cur, "old-owner", 1, 1)
        insert(store, cur, "new-owner", 1, 2)
        cur.execute("UPDATE sync_ops SET created_at='2000-01-01'")
        store.set_watermark(cur, "old-owner", 1, 5)
        store.set_watermark(cur, "snapshot-only", 4, 12)
    assert store.prune_ops() == 1
    assert store.local_watermarks() == {"new-owner": {"1": 2}, "old-owner": {"1": 5}, "snapshot-only": {"4": 12}}
    assert store.requires_snapshot({"new-owner": {"1": 2}})
    with connect() as conn:
        cur = conn.cursor()
        # Simulate an atomic restore/maintenance transaction; no durable head
        # or cache may continue to advertise the removed log tail.
        cur.execute("DELETE FROM sync_ops WHERE origin_device_id='new-owner'")
        insert(store, cur, "new-owner", 1, 1)
    assert store.local_watermarks()["new-owner"] == {"1": 1}
    with pytest.raises(RuntimeError), connect() as conn:
        insert(store, conn.cursor(), "new-owner", 2, 99)
        raise RuntimeError("rollback")
    assert store.local_watermarks()["new-owner"] == {"1": 1}


@pytest.mark.parametrize("streams", [2, 257])
def test_concurrent_changes_never_truncate_the_returned_frontier(pg, streams):
    store, connect = pg
    with connect() as conn:
        for n in range(streams):
            insert(store, conn.cursor(), f"worker-{n:04d}", 1, 1, f"r{n}")
    with connect() as conn:
        real = conn.cursor()
        class InterleavedCursor:
            calls = 0

            def execute(self, query, params=None):
                real.execute(query, params)
                self.calls += 1
                if self.calls == 1:
                    with connect() as writer:
                        insert(store, writer.cursor(), "worker-0000", 1, 2)
                        insert(store, writer.cursor(), "zzz-new-stream", 7, 3)

            def fetchall(self):
                return real.fetchall()

        cursor = InterleavedCursor()
        rows = store._log_heads(cursor)
        heads = {(r["origin_device_id"], r["incarnation"]): r["s"] for r in rows}
    if streams < store.HEAD_PROBE_LIMIT:
        assert cursor.calls == 1
        assert len(heads) == streams and heads[("worker-0000", 1)] == 1
        assert ("zzz-new-stream", 7) not in heads
    else:
        assert cursor.calls == 2
        assert len(heads) == streams + 1 and heads[("worker-0000", 1)] == 2
        assert heads[("zzz-new-stream", 7)] == 3
    assert store.local_watermarks()["worker-0000"]["1"] == 2


def _rows_read(cursor, query, params):
    """Index or heap entries the plan of ``query`` reads from sync_ops."""
    import json as _json
    cursor.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + query, params)
    raw = next(iter(cursor.fetchone().values()))
    plan = (_json.loads(raw) if isinstance(raw, str) else raw)[0]["Plan"]
    total, stack = 0, [plan]
    while stack:
        node = stack.pop()
        if node.get("Relation Name") == "sync_ops":
            total += int(node.get("Actual Rows", 0)) * int(node.get("Actual Loops", 1))
        stack.extend(node.get("Plans", []))
    return total


def test_stream_head_reads_one_entry_while_a_stream_outgrows_its_statistics(pg):
    """#1978: a first publish of a large backlog grows the worker's own stream
    far past what the planner last saw. Each op's sequence allocation (and the
    advertised head) must still read the stream's last entry only, not every
    entry of the stream once per op."""
    store, connect = pg
    with connect() as conn:
        cur = conn.cursor()
        # The log of a newly enrolled worker: never analyzed yet.
        cur.execute("ALTER TABLE sync_ops SET (autovacuum_enabled = false)")
        for n in range(20):
            insert(store, cur, f"peer-{n:02d}", 1, 1, f"r{n}")
        for seq in range(1, 3001):
            insert(store, cur, "self", 1, seq, f"own-{seq}")
    with connect() as conn:
        cur = conn.cursor()
        seen = []

        class Recording:
            def execute(self, query, params=None):
                seen.append((query, params))
                cur.execute(query, params)

            def fetchone(self):
                return cur.fetchone()

            def fetchall(self):
                return cur.fetchall()

        assert store.next_origin_seq(Recording(), "self", 1) == 3001
        assert store._log_heads(Recording())
        seq_query, heads_query = seen
        assert _rows_read(cur, *seq_query) <= 2
        # One last entry per stream (21), plus the seeks between streams.
        assert _rows_read(cur, *heads_query) <= 21 * 4
