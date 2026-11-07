"""Bulk offline-delta contracts against a private, disposable PostgreSQL 16.

Collected by the normal backend suite; only hosts without server binaries skip.
No configured application database or TEST_POSTGRES_DSN is ever consulted.
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from uuid import uuid4

import pytest

from app.db import base, database
from app.db import network as store
from app.db.schema_postgres import create_postgres_schema
from app.network import catalog, engine
from tests.core.network.test_owner_sync_speed import _append, _session
from tests.db.schema_lifecycle_helpers import pg16_bindir, pg_connect, private_postgres


@pytest.fixture(scope="module")
def private_cluster(tmp_path_factory):
    bindir = pg16_bindir()
    if bindir is None:
        pytest.skip("offline-delta PostgreSQL tests require PostgreSQL 16 server binaries")
    with private_postgres(bindir, tmp_path_factory.mktemp("offline-delta-pg")) as descriptor:
        yield descriptor


@pytest.fixture
def publisher(private_cluster, workers, monkeypatch):
    """A real producer connection factory; all its state is disposable."""
    worker = workers("publisher")
    schema = "publication_" + uuid4().hex
    with closing(pg_connect(private_cluster)) as admin:
        admin.cursor().execute("CREATE SCHEMA " + schema)

    def connect():
        conn = pg_connect(private_cluster)
        conn.cursor().execute("SET search_path TO " + schema)
        conn.cursor().execute("SET statement_timeout TO 10000")
        conn.autocommit = False
        return conn

    try:
        with closing(connect()) as conn, conn:
            create_postgres_schema(conn.cursor())
        with worker.active():
            monkeypatch.setattr(base, "get_connection", connect)
            monkeypatch.setattr(database, "get_connection", connect)
            monkeypatch.setattr(base, "_close_connection", lambda conn: conn.close())
            database.create_profile(profile_id="default", name="Default", description=None,
                                    config={"model": "claude-sonnet-4-20250514"})
            engine.create_network("Publication")
            _session("s1")
            yield connect
    finally:
        with closing(pg_connect(private_cluster)) as admin:
            admin.cursor().execute("DROP SCHEMA " + schema + " CASCADE")


@pytest.fixture
def receiver(private_cluster, workers, monkeypatch):
    source, target = workers("source"), workers("target")
    schemas = []

    def new_database():
        schema = "offline_delta_" + uuid4().hex
        with closing(pg_connect(private_cluster)) as admin:
            admin.cursor().execute("CREATE SCHEMA " + schema)
        schemas.append(schema)

        def connect():
            conn = pg_connect(private_cluster)
            conn.cursor().execute("SET search_path TO " + schema)
            conn.cursor().execute("SET statement_timeout TO 10000")
            conn.autocommit = False
            return conn

        with closing(connect()) as conn, conn:
            create_postgres_schema(conn.cursor())
        return connect

    @contextmanager
    def active(worker, connect):
        with worker.active(), monkeypatch.context() as patch:
            patch.setattr(base, "get_connection", connect)
            patch.setattr(database, "get_connection", connect)
            # These connections are deliberately outside the application's
            # pool; base otherwise leaves them open when no pool exists.
            patch.setattr(base, "_close_connection", lambda conn: conn.close())
            yield

    def seed_profile():
        database.create_profile(profile_id="default", name="Default Profile", description=None,
                                config={"model": "claude-sonnet-4-20250514"})

    try:
        source_db, target_db = new_database(), new_database()
        # Both producers and consumers use PostgreSQL: the SQLite shim's
        # integer booleans are not representative signed wire payloads.
        with active(source, source_db):
            seed_profile()
            engine.create_network("Offline delta")
            response = engine.admit_device(device_id=target.device_id,
                signing_public=target.identity.signing_public,
                encryption_public=target.identity.encryption_public, name="Target")
            _session("backlog")
            catalog.publish_owned()
            initial = store.ops_since({}, limit=10000)
            floor = store.local_watermarks()

        with active(target, target_db):
            seed_profile()
            engine.enroll_from_response(response, device_name="Target")
            engine.finish_bootstrap()
            result = engine.apply_batch(initial)
            assert result.retry == result.rejected == result.pending == 0

            def delta(count):
                with active(source, source_db):
                    started = time.perf_counter()
                    _append("backlog", count)
                    # A scanner pass has a real one-second fairness budget;
                    # honor its backlog signal instead of enlarging the budget
                    # or assuming 3,000 durable writes fit into one pass.
                    for _ in range(count // catalog.EVENT_BATCH + 2):
                        catalog.publish_owned()
                        if not catalog.take_events_backlog():
                            break
                    else:
                        pytest.fail("publisher made no bounded progress through the backlog")
                    elapsed = time.perf_counter() - started
                    ops = store.ops_since(floor, limit=count + 100)
                # Deliver the real producer's header first, leaving fresh,
                # contiguous event chunks eligible for the bulk SQL path.
                headers = [o for o in ops if o["collection"] != "session_events"]
                header_result = engine.apply_batch(headers)
                assert header_result.retry == header_result.rejected == header_result.pending == 0
                events = [o for o in ops if o["collection"] == "session_events"]
                assert len(events) == count
                return events, elapsed

            yield target_db, delta
    finally:
        with closing(pg_connect(private_cluster)) as admin:
            for schema in schemas:
                admin.cursor().execute("DROP SCHEMA " + schema + " CASCADE")


def _assert_complete(connect, ops):
    count = len(ops)
    with closing(connect()) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT seq FROM session_events WHERE session_id='backlog' ORDER BY seq")
        assert [r["seq"] for r in cursor.fetchall()] == list(range(1, count + 1))
        cursor.execute("SELECT COUNT(*) AS n FROM sync_records WHERE collection='session_events'")
        assert cursor.fetchone()["n"] == count
        cursor.execute("SELECT COUNT(*) AS n FROM sync_ops WHERE collection='session_events'")
        assert cursor.fetchone()["n"] == count
        cursor.execute("SELECT next_seq FROM session_event_counters WHERE session_id='backlog'")
        assert cursor.fetchone()["next_seq"] == count
        cursor.execute("SELECT published_seq FROM network_catalog_cursors WHERE session_id='backlog'")
        assert cursor.fetchone()["published_seq"] == count
        assert store.get_watermark(cursor, ops[-1]["origin_device_id"], ops[-1]["origin_incarnation"]) == ops[-1]["origin_seq"]
    assert not engine.applying_batch()


@pytest.mark.parametrize("fault", ["connection_drop", "serialization_failure"])
def test_postgres_bulk_failure_rolls_back_before_individual_retry(receiver, monkeypatch, fault):
    connect, delta = receiver
    ops, _ = delta(12)
    insert = store.insert_event_batch
    one = engine._apply_one
    attempted, retried = [], []
    before = store.local_watermarks()

    def fail_mid_batch(cursor, coll, rows, envelopes, hashes):
        attempted.append(len(rows))
        # Persist half the chunk inside the still-uncommitted transaction.
        insert(cursor, coll, rows[:6], envelopes[:6], hashes[:6])
        if fault == "connection_drop":
            with closing(connect()) as killer, killer:
                killer.cursor().execute("SELECT pg_terminate_backend(%s)", (cursor.connection.get_backend_pid(),))
            cursor.execute("SELECT 1")
        else:
            cursor.execute("DO $$ BEGIN RAISE EXCEPTION 'test serialization fault' USING ERRCODE='40001'; END $$")
        pytest.fail("PostgreSQL did not raise the requested fault")

    def check_rollback(data, ctx, result):
        if not retried:
            with closing(connect()) as conn:
                cursor = conn.cursor()
                for table in ("session_events", "sync_ops", "sync_records"):
                    predicate = " WHERE collection='session_events'" if table != "session_events" else ""
                    cursor.execute("SELECT COUNT(*) AS n FROM " + table + predicate)
                    assert cursor.fetchone()["n"] == 0
            assert store.local_watermarks() == before
        retried.append(data["origin_seq"])
        return one(data, ctx, result)

    monkeypatch.setattr(store, "insert_event_batch", fail_mid_batch)
    monkeypatch.setattr(engine, "_apply_one", check_rollback)
    result = engine.apply_batch(ops)
    assert attempted == [12]
    assert retried == [op["origin_seq"] for op in ops]
    assert result.applied == 12 and result.retry == result.rejected == result.pending == 0
    _assert_complete(connect, ops)
    assert engine.apply_batch(ops).duplicate == 12


def test_postgres_concurrent_duplicate_delivery_is_exactly_once(receiver, monkeypatch):
    connect, delta = receiver
    ops, _ = delta(128)
    ctx = engine.context()
    overlap = []
    barrier = threading.Barrier(2, action=lambda: overlap.append(engine._applying[ctx.device_id]))
    entered = threading.local()
    observe = store.observe_clock
    inserts = []
    real_insert = store.insert_event_batch

    def concurrent_observe(cursor, clock):
        if not getattr(entered, "once", False):
            entered.once = True
            # Both apply_batch calls are inside their first transaction,
            # immediately before contending for the real network-state lock.
            barrier.wait(timeout=10)
        return observe(cursor, clock)

    def insert(*args):
        inserts.append(len(args[2]))
        return real_insert(*args)

    monkeypatch.setattr(store, "insert_event_batch", insert)
    monkeypatch.setattr(store, "observe_clock", concurrent_observe)

    def deliver():
        return engine.apply_batch(ops, ctx=ctx)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(deliver) for _ in range(2)]
        results = [f.result(timeout=30) for f in futures]
    assert overlap == [2]
    assert sum(r.applied for r in results) == 128
    assert sum(r.duplicate for r in results) == 128
    assert all(r.retry == r.rejected == r.pending == 0 for r in results)
    assert sum(inserts) == 128
    _assert_complete(connect, ops)


def test_postgres_large_offline_backlog_measurement(receiver, monkeypatch, record_property):
    connect, delta = receiver
    ops, publish_seconds = delta(3000)
    chunks = []
    insert = store.insert_event_batch

    def counted(*args):
        chunks.append(len(args[2]))
        return insert(*args)

    monkeypatch.setattr(store, "insert_event_batch", counted)
    started = time.perf_counter()
    result = engine.apply_batch(ops)
    apply_seconds = time.perf_counter() - started
    assert result.applied == 3000 and result.retry == result.rejected == result.pending == 0
    assert chunks == [engine.APPLY_CHUNK] * (3000 // engine.APPLY_CHUNK) + [3000 % engine.APPLY_CHUNK]
    _assert_complete(connect, ops)
    record_property("backlog_publish_seconds", publish_seconds)
    record_property("backlog_apply_seconds", apply_seconds)
    print(f"BACKLOG events=3000 chunks={len(chunks)} publish_seconds={publish_seconds:.3f} apply_seconds={apply_seconds:.3f}")


@pytest.mark.parametrize("size", [1, catalog.EVENT_CHUNK, 1000])
def test_postgres_publication_statements_are_per_window(publisher, monkeypatch, record_property, size):
    from psycopg2.extras import RealDictCursor

    _append("s1", size)
    execute = RealDictCursor.execute
    statements = []

    def counted(self, sql, params=None):
        # The private fixture's two connection-setup SETs are not publisher SQL.
        if not sql.startswith("SET "):
            statements.append(sql)
        return execute(self, sql, params)

    monkeypatch.setattr(RealDictCursor, "execute", counted)
    assert catalog.publish_events(session_ids=["s1"], limit=size) == size
    windows = (size + catalog.EVENT_CHUNK - 1) // catalog.EVENT_CHUNK
    record_property("publisher_statements", len(statements))
    print(f"PUBLICATION events={size} windows={windows} statements={len(statements)}")
    assert len(statements) <= 9 * windows + 2
    assert sum("FOR UPDATE" in q and "network_state" in q for q in statements) == windows


class PublicationRollback(Exception):
    pass


def _publication_state(cursor):
    cursor.execute("SELECT clock FROM network_state WHERE id=1")
    clock = cursor.fetchone()["clock"]
    cursor.execute("SELECT envelope FROM sync_ops ORDER BY seq")
    ops = [r["envelope"] for r in cursor.fetchall()]
    cursor.execute("SELECT collection, record_id, clock, origin_device_id, content_hash, deleted, order_seq "
                   "FROM sync_records ORDER BY collection, record_id")
    records = [dict(r) for r in cursor.fetchall()]
    return clock, ops, records, store.catalog_cursor(cursor, "s1")


@pytest.mark.parametrize("size", [1, catalog.EVENT_CHUNK])
def test_postgres_publication_matches_single_event_bytes(publisher, monkeypatch, size):
    """Same DB snapshot, keys, nonce stream and timestamp => identical signed v1 bytes."""
    from datetime import datetime, timezone
    from unittest.mock import Mock
    from app.network import crypto, envelope, policy

    _append("s1", size)
    coll = policy.collection("session_events")
    with store.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM session_events WHERE session_id='s1' ORDER BY seq")
        rows = [dict(r) for r in cursor.fetchall()]
    fixed = datetime(2026, 11, 7, tzinfo=timezone.utc)
    monkeypatch.setattr(envelope, "datetime", Mock(now=Mock(return_value=fixed)))
    results = []
    for bulk in (False, True):
        nonces = iter(range(1, size + 1))
        monkeypatch.setattr(crypto.os, "urandom", lambda n: next(nonces).to_bytes(n, "big"))
        with pytest.raises(PublicationRollback), store.get_db() as conn:
            cursor = conn.cursor()
            if bulk:
                assert engine.record_event_batch(cursor, rows, ctx=engine.context()) == size
            else:
                for row in rows:
                    assert engine.record(cursor, coll.name, coll.record_id(row), row,
                                         order_seq=int(row["seq"])) is not None
            results.append(_publication_state(cursor))
            raise PublicationRollback()
    assert results[0][0] == results[1][0]
    assert crypto.canonical_json(results[0][1]) == crypto.canonical_json(results[1][1])
    assert results[0][2:] == results[1][2:]


@pytest.mark.parametrize("fault", ["sync_ops", "sync_records", "cursor"])
def test_postgres_publication_window_rollback_and_restart(publisher, monkeypatch, fault):
    _append("s1", catalog.EVENT_CHUNK)
    with store.get_db() as conn:
        before = _publication_state(conn.cursor())
    insert, set_cursor = store._insert_rows, store.set_catalog_cursor

    def failed_insert(cursor, table, *args, **kwargs):
        insert(cursor, table, *args, **kwargs)
        if table == fault:
            raise PublicationRollback()

    def failed_cursor(*args):
        set_cursor(*args)
        if fault == "cursor":
            raise PublicationRollback()

    with monkeypatch.context() as patch:
        patch.setattr(store, "_insert_rows", failed_insert)
        patch.setattr(store, "set_catalog_cursor", failed_cursor)
        with pytest.raises(PublicationRollback):
            catalog.publish_events(session_ids=["s1"], limit=catalog.EVENT_CHUNK)
    with store.get_db() as conn:
        assert _publication_state(conn.cursor()) == before
    # Reload the persisted network context, as a restarted scanner does.
    monkeypatch.setattr(engine, "_context", None)
    assert catalog.publish_events(session_ids=["s1"], limit=catalog.EVENT_CHUNK) == catalog.EVENT_CHUNK
    with store.get_db() as conn:
        clock, ops, records, published = _publication_state(conn.cursor())
    assert published == catalog.EVENT_CHUNK
    assert clock == before[0] + catalog.EVENT_CHUNK
    tail = ops[len(before[1]):]
    start = before[1][-1]["origin_seq"] + 1
    assert [o["origin_seq"] for o in tail] == list(range(start, start + catalog.EVENT_CHUNK))
    assert len(records) == len(before[2]) + catalog.EVENT_CHUNK


def test_postgres_publication_clear_retracts_then_restarts(publisher, monkeypatch):
    _append("s1", 5)
    assert catalog.publish_events(session_ids=["s1"]) == 5
    database.clear_session_conversation("s1")
    _append("s1", 2)
    monkeypatch.setattr(engine, "_context", None)
    # Three tombstones and the two rewritten low sequence numbers.
    assert catalog.publish_events(session_ids=["s1"]) == 5
    with store.get_db() as conn:
        cursor = conn.cursor()
        assert store.catalog_cursor(cursor, "s1") == 2
        known = store.records_for_collection(cursor, "session_events")
    assert [known[f"s1|{i}"]["deleted"] for i in range(1, 6)] == [False, False, True, True, True]
    _append("s1", 1)
    assert catalog.publish_events(session_ids=["s1"]) == 1


def test_postgres_publication_allows_local_append_and_serializes_network_writer(publisher, monkeypatch):
    _append("s1", catalog.EVENT_CHUNK + 1)
    ctx = engine.context()
    locked, release, writer_waiting = threading.Event(), threading.Event(), threading.Event()
    insert, lock = store.insert_published_events, store.lock_state
    publisher_thread = []
    durations = []

    def paused_insert(*args):
        if not locked.is_set():
            publisher_thread.append(threading.get_ident())
            locked.set()
            assert release.wait(5), "publication test did not release its window"
        return insert(*args)

    def writer_lock(cursor):
        if publisher_thread and threading.get_ident() != publisher_thread[0]:
            writer_waiting.set()
        return lock(cursor)

    monkeypatch.setattr(store, "insert_published_events", paused_insert)
    monkeypatch.setattr(store, "lock_state", writer_lock)

    def write_network():
        with store.get_db() as conn:
            return engine.record(conn.cursor(), "user_preferences", "admin|__owner__|theme",
                                 {"user_type": "admin", "user_id": "__owner__", "key": "theme", "value": "dark"},
                                 ctx=ctx)

    with ThreadPoolExecutor(max_workers=3) as pool:
        publishing = pool.submit(catalog.publish_events, ctx=ctx, session_ids=["s1"], limit=catalog.EVENT_CHUNK)
        try:
            assert locked.wait(5)
            # The production append's R/C/S locks must not wait for the network state lock.
            started = time.perf_counter()
            pool.submit(_append, "s1", 1).result(timeout=3)
            durations.append(time.perf_counter() - started)
            writing = pool.submit(write_network)
            assert writer_waiting.wait(3)
            assert not writing.done()
        finally:
            release.set()
        assert publishing.result(timeout=5) == catalog.EVENT_CHUNK
        assert writing.result(timeout=5) is not None
    assert catalog.publish_events(session_ids=["s1"]) == 2
    with store.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT seq FROM sync_ops ORDER BY seq")
        seqs = [r["seq"] for r in cursor.fetchall()]
        assert seqs == list(range(1, len(seqs) + 1))
        assert store.catalog_cursor(cursor, "s1") == catalog.EVENT_CHUNK + 2
    assert durations[0] < 3
