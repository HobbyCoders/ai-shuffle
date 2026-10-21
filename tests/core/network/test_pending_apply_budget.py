"""Pending catch-up must yield the state lock between bounded transactions."""
from contextlib import contextmanager
from time import perf_counter, sleep
from types import SimpleNamespace
import json
import os

import pytest

from app.db import database, network as store, session_events
from app.network import catalog, engine
from tests.core.network.harness import converge, join


@pytest.fixture(autouse=True)
def pending_semantics_clock(monkeypatch):
    # These tests assert committed prefixes, chunk sizes, and SQL shape, not
    # host speed. Freeze only this module's clock (not the shared time module)
    # so a slow database cannot exhaust the real 250 ms request deadline.
    # test_pending_drain_deadline.py separately advances a fake clock to check
    # that deadline and background handoff with the unchanged product budget.
    monkeypatch.setattr(engine, "time", SimpleNamespace(monotonic=lambda: 0.0))


@pytest.fixture
def pending_chunk_delay(request, monkeypatch):
    """Regress slow chunks without changing the engine's real 250 ms budget."""
    delay = request.param
    real_chunk = engine._apply_pending_chunk

    def chunk(*args, **kwargs):
        committed = real_chunk(*args, **kwargs)
        if committed:
            sleep(delay)
        return committed

    monkeypatch.setattr(engine, "_apply_pending_chunk", chunk)


def backlog(workers, count, *, withhold=()):
    a, b = workers("publisher"), workers("receiver")
    with a.active():
        engine.create_network("Pending budget")
    join(a, b)
    with a.active():
        database.create_session(session_id="chat", profile_id="default", title="Chat")
        catalog.publish_owned()
    converge([a, b])
    with b.active():
        marks = store.local_watermarks()
    with a.active():
        session_events.append_events("chat", [
            {"type": "message.user", "payload": {"content": f"message {i}"}}
            for i in range(count)])
        catalog.hydrate("chat")
        ops = store.ops_since(marks, limit=count + 10, exclude_origin=b.device_id)
    assert len(ops) >= count
    with b.active():
        # Real producer envelopes and real out-of-order receive, not seeded mirrors.
        delivered = [op for index, op in enumerate(ops) if index > 0 and index not in withhold]
        result = engine.apply_batch(delivered)
        assert result.pending == len(delivered)
    return a, b, ops


@contextmanager
def measured_apply(monkeypatch, worker):
    transactions = []
    active = []
    real_db, real_apply = engine.get_db, engine._apply_next
    real_append = engine._apply_event_append
    real_lock, real_observe = store.lock_state, store.observe_clock

    def lock_call(fn, *args, **kwargs):
        value = fn(*args, **kwargs)
        if active:
            active[-1].setdefault("lock_started", perf_counter())
        return value

    @contextmanager
    def transaction():
        metric = {"ops": 0, "seconds": 0}
        start = perf_counter()
        with real_db() as conn:
            active.append(metric)
            try:
                yield conn
            finally:
                active.pop()
        ended = perf_counter()
        metric["seconds"] = ended - start
        metric["lock_ms"] = 1000 * (ended - metric.get("lock_started", ended))
        transactions.append(metric)

    def apply(*args, **kwargs):
        active[-1]["ops"] += 1
        return real_apply(*args, **kwargs)

    def append(cursor, chunk, *args, **kwargs):
        appended = real_append(cursor, chunk, *args, **kwargs)
        if appended:
            active[-1]["ops"] += len(chunk)
        return appended

    statements = []
    trace = getattr(worker.raw, "set_trace_callback", None)
    previous_wait = None
    if trace is not None:
        trace(statements.append)
    else:
        # One libpq wait per database operation (execute/commit), including
        # savepoints. No SQL/parameter contents or private descriptors logged.
        from psycopg2.extensions import get_wait_callback, set_wait_callback
        from psycopg2.extras import wait_select
        previous_wait = get_wait_callback()

        def wait(conn):
            statements.append("round_trip")
            wait_select(conn)

        set_wait_callback(wait)
    with monkeypatch.context() as patch:
        patch.setattr(engine, "get_db", transaction)
        patch.setattr(engine, "_apply_next", apply)
        patch.setattr(engine, "_apply_event_append", append)
        patch.setattr(store, "lock_state", lambda *a, **kw: lock_call(real_lock, *a, **kw))
        patch.setattr(store, "observe_clock", lambda *a, **kw: lock_call(real_observe, *a, **kw))
        start = perf_counter()
        try:
            yield transactions, statements
        finally:
            if trace is not None:
                trace(None)
            else:
                set_wait_callback(previous_wait)
            elapsed = perf_counter() - start
            print("PENDING_METRICS " + json.dumps({
                "ops": sum(t["ops"] for t in transactions),
                "transactions": len(transactions),
                "max_ops_per_transaction": max((t["ops"] for t in transactions), default=0),
                "max_transaction_ms": 1000 * max((t["seconds"] for t in transactions), default=0),
                "elapsed_ms": elapsed * 1000,
                "max_lock_hold_ms": max((t["lock_ms"] for t in transactions), default=0),
                "db_operations": len(statements),
                "db_metric": "sqlite_statements" if trace else "libpq_round_trips",
            }))


def test_pending_backlog_has_a_transaction_work_budget(workers, monkeypatch):
    count = int(os.environ.get("SYNC_LAB_OPS", "193"))
    a, b, ops = backlog(workers, count)
    with b.active(), measured_apply(monkeypatch, b) as (transactions, statements):
        result = engine.apply_batch(ops[:1])
    assert result.applied == len(ops)
    assert result.retry == result.rejected == 0
    assert max(t["ops"] for t in transactions) <= engine.APPLY_CHUNK
    with b.active():
        assert store.pending_holes() == []
        assert engine.apply_batch(ops).duplicate == len(ops)
        assert b.sql("SELECT COUNT(*) AS n FROM session_events WHERE session_id='chat'")[0]["n"] == count
        with store.get_db() as conn:
            assert store.get_watermark(conn.cursor(), a.device_id, 1) == ops[-1]["origin_seq"]


@pytest.fixture(scope="module")
def private_cluster(tmp_path_factory):
    from tests.db.schema_lifecycle_helpers import pg16_bindir, private_postgres
    bindir = pg16_bindir()
    if bindir is None:
        pytest.skip("requires disposable PostgreSQL 16 server binaries")
    with private_postgres(bindir, tmp_path_factory.mktemp("pending-sync-pg")) as descriptor:
        yield descriptor


@pytest.fixture
def pg_workers(workers, private_cluster, monkeypatch):
    from uuid import uuid4
    from app.db.schema_postgres import create_postgres_schema
    from tests.db.schema_lifecycle_helpers import pg_connect

    def make(name):
        worker = workers(name)
        worker.close()
        conn = pg_connect(private_cluster)
        schema = "pending_" + uuid4().hex
        conn.cursor().execute("CREATE SCHEMA " + schema)
        conn.cursor().execute("SET search_path TO " + schema)
        conn.autocommit = False
        create_postgres_schema(conn.cursor())
        conn.commit()
        worker.raw = worker.shim = conn
        monkeypatch.setattr(worker, "close", conn.close)

        def sql(query, params=()):
            with conn.cursor() as cursor:
                cursor.execute(query.replace("?", "%s"), params)
                return [dict(row) for row in cursor.fetchall()] if cursor.description else []

        monkeypatch.setattr(worker, "sql", sql)
        with worker.active():
            database.create_profile(profile_id="default", name="Default Profile", description=None, config={})
        return worker

    return make


def test_real_postgres_pending_budget(pg_workers, monkeypatch):
    test_pending_backlog_has_a_transaction_work_budget(pg_workers, monkeypatch)




@pytest.mark.parametrize("use_postgres", [False, True], ids=["shim", "postgres"])
@pytest.mark.parametrize("pending_chunk_delay", [0.0, 0.3], ids=["fast", "slow"], indirect=True)
def test_unexpected_buffered_error_commits_good_predecessors(request, use_postgres, monkeypatch, pending_chunk_delay):
    factory = request.getfixturevalue("pg_workers" if use_postgres else "workers")
    a, b, ops = backlog(factory, 145)
    bad = ops[79]["origin_seq"]  # 80th envelope: commit the 79 good predecessors.
    real_apply = engine._apply_next

    def unexpected(cursor, ctx, env, member, result):
        if env.origin_seq == bad:
            if use_postgres:
                # Exercise a genuinely aborted PostgreSQL transaction too.
                cursor.execute("SELECT 1/0")
            raise RuntimeError("unexpected buffered error")
        return real_apply(cursor, ctx, env, member, result)

    with b.active():
        with monkeypatch.context() as patch:
            patch.setattr(engine, "_apply_next", unexpected)
            # Exercise the general per-op failure handler explicitly. Bulk
            # failure rollback/fallback has separate integration coverage.
            patch.setattr(engine, "_apply_event_append", lambda *a, **kw: False)
            result = engine.apply_batch(ops[:1])
        assert result.applied == 79 and result.retry == 1
        assert result.blocked[0]["seq"] == bad
        with store.get_db() as conn:
            cur = conn.cursor()
            assert store.get_watermark(cur, a.device_id, 1) == bad - 1
            assert store.count_pending(cur, a.device_id, 1) == len(ops) - 79
        assert engine.apply_batch(ops[:1]).applied == len(ops) - 79
        assert not store.pending_holes()


@pytest.mark.parametrize("use_postgres", [False, True], ids=["shim", "postgres"])
def test_background_resumes_interrupted_buffer_without_peer_traffic(request, use_postgres, monkeypatch):
    factory = request.getfixturevalue("pg_workers" if use_postgres else "workers")
    a, b, ops = backlog(factory, 193)
    real_drain = engine._apply_pending_chunk
    calls = 0

    def interrupted(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("interrupted between committed chunks")
        return real_drain(*args)

    with b.active():
        # A real hole must not look ready just because later ops are buffered.
        assert engine.resume_pending().applied == 0
        with monkeypatch.context() as patch:
            patch.setattr(engine, "_apply_pending_chunk", interrupted)
            with pytest.raises(RuntimeError, match="interrupted between"):
                engine.apply_batch(ops[:1])
        applied = []
        for _ in range(4):
            result = engine.resume_pending()
            assert result.retry == 0
            applied.append(result.applied)
        assert sum(applied) == len(ops) - engine.APPLY_CHUNK - 1
        assert max(applied) <= engine.APPLY_CHUNK
        assert not store.pending_holes()
        with store.get_db() as conn:
            assert store.get_watermark(conn.cursor(), a.device_id, 1) == ops[-1]["origin_seq"]


def test_failed_pending_commit_keeps_rows_and_frontier_retryable(pg_workers, monkeypatch):
    a, b, ops = backlog(pg_workers, 145)
    real_db = engine.get_db
    calls = 0

    @contextmanager
    def failing_commit():
        nonlocal calls
        calls += 1
        with real_db() as conn:
            yield conn
            if calls == 2:
                raise RuntimeError("simulated pending commit failure")

    with b.active():
        with monkeypatch.context() as patch:
            patch.setattr(engine, "get_db", failing_commit)
            result = engine.apply_batch(ops[:1])
        assert result.applied == 1 and result.retry == 1
        # The whole failed chunk rolled back: diagnostics name its first op,
        # not the later operation reached before the failed commit.
        assert result.blocked[0]["seq"] == ops[1]["origin_seq"]
        with store.get_db() as conn:
            cur = conn.cursor()
            assert store.get_watermark(cur, a.device_id, 1) == ops[0]["origin_seq"]
            assert store.count_pending(cur, a.device_id, 1) == len(ops) - 1
        result = engine.apply_batch(ops[:1])
        assert result.duplicate == 1 and result.applied == len(ops) - 1
        assert not store.pending_holes()
        with store.get_db() as conn:
            # The next local write must observe clocks from the committed tail.
            assert store.bump_clock(conn.cursor()) > max(op["clock"] for op in ops)


def test_real_writer_gets_state_lock_before_backlog_finishes(pg_workers, private_cluster, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from tests.db.schema_lifecycle_helpers import pg_connect

    a, b, ops = backlog(pg_workers, 193)
    schema = b.raw.cursor()
    schema.execute("SELECT current_schema() AS name")
    schema_name = schema.fetchone()["name"]
    schema.close()
    waiting = threading.Event()
    acquired = threading.Event()
    writer_pid = []
    visible = []

    def local_writer():
        conn = pg_connect(private_cluster)
        try:
            conn.autocommit = False
            cur = conn.cursor()
            cur.execute("SET search_path TO " + schema_name)
            cur.execute("SET LOCAL statement_timeout='5s'")
            writer_pid.append(conn.get_backend_pid())
            waiting.set()
            cur.execute("SELECT id FROM network_state WHERE id=1 FOR UPDATE")
            cur.execute("SELECT COUNT(*) AS n FROM session_events WHERE session_id='chat'")
            visible.append(cur.fetchone()["n"])
            conn.commit()
            acquired.set()
        finally:
            conn.close()

    real_apply = engine._apply_next
    future = None
    with ThreadPoolExecutor(max_workers=1) as pool:
        def apply(cursor, ctx, env, member, result):
            nonlocal future
            if future is None:
                future = pool.submit(local_writer)
                assert waiting.wait(5)
                # Wait for PostgreSQL itself to report the queued lock. No
                # scheduler timing assumption decides whether the test passes.
                deadline = perf_counter() + 5
                while perf_counter() < deadline:
                    cursor.execute("SELECT 1 FROM pg_locks WHERE pid=%s AND NOT granted", (writer_pid[0],))
                    if cursor.fetchone():
                        break
                else:
                    raise AssertionError("local writer never queued for the state lock")
            return real_apply(cursor, ctx, env, member, result)

        monkeypatch.setattr(engine, "_apply_next", apply)
        with b.active():
            result = engine.apply_batch(ops[:1])
        future.result(timeout=5)
    assert acquired.is_set()
    assert result.applied == len(ops)
    assert 0 < visible[0] < len(ops), "a local writer waited for the entire pending backlog"


@pytest.mark.parametrize("use_postgres", [False, True], ids=["shim", "postgres"])
def test_pending_transient_preserves_frontier_and_resumes(request, use_postgres, monkeypatch):
    factory = request.getfixturevalue("pg_workers" if use_postgres else "workers")
    a, b, ops = backlog(factory, 145)
    blocked = ops[80]["origin_seq"]
    real_apply = engine._apply_next

    def temporarily_unavailable(cursor, ctx, env, member, result):
        if env.origin_seq == blocked:
            raise engine.TransientApplyError("parent not yet available")
        return real_apply(cursor, ctx, env, member, result)

    with b.active():
        with monkeypatch.context() as patch:
            patch.setattr(engine, "_apply_next", temporarily_unavailable)
            # This test isolates the general fallback's per-op retry boundary.
            patch.setattr(engine, "_apply_event_append", lambda *a, **kw: False)
            result = engine.apply_batch(ops[:1])
        assert result.applied == 80 and result.retry == 1
        with store.get_db() as conn:
            assert store.get_watermark(conn.cursor(), a.device_id, 1) == blocked - 1
            assert store.count_pending(conn.cursor(), a.device_id, 1) == len(ops) - 80
        # A replay of a committed predecessor also resumes interrupted drains.
        result = engine.apply_batch(ops[:1])
        assert result.duplicate == 1 and result.applied == len(ops) - 80
        assert not store.pending_holes()


@pytest.mark.parametrize("use_postgres", [False, True], ids=["shim", "postgres"])
def test_permanently_bad_buffered_op_does_not_strand_successors(request, use_postgres):
    factory = request.getfixturevalue("pg_workers" if use_postgres else "workers")
    a, b, ops = backlog(factory, 145)
    bad = dict(ops[80], signature="bad")
    with b.active():
        with store.get_db() as conn:
            conn.cursor().execute(
                "UPDATE sync_pending SET envelope=%s WHERE origin_device_id=%s AND incarnation=1 AND seq=%s",
                (json.dumps(bad), a.device_id, bad["origin_seq"]))
        result = engine.apply_batch(ops[:1])
        assert result.rejected == 1 and result.applied == len(ops) - 1
        assert result.retry == 0
        assert not store.pending_holes()


@pytest.mark.parametrize("use_postgres", [False, True], ids=["shim", "postgres"])
def test_committed_pending_chunks_survive_interrupted_catchup(request, use_postgres, monkeypatch):
    factory = request.getfixturevalue("pg_workers" if use_postgres else "workers")
    a, b, ops = backlog(factory, 193)
    real_drain = engine._apply_pending_chunk
    calls = 0

    def interrupted(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated process interruption between transactions")
        return real_drain(*args)

    with b.active():
        with monkeypatch.context() as patch:
            patch.setattr(engine, "_apply_pending_chunk", interrupted)
            with pytest.raises(RuntimeError, match="simulated process interruption"):
                engine.apply_batch(ops[:1])
        with store.get_db() as conn:
            assert store.get_watermark(conn.cursor(), a.device_id, 1) == ops[engine.APPLY_CHUNK]["origin_seq"]
        result = engine.apply_batch(ops[:1])
        assert result.duplicate == 1
        assert result.applied == len(ops) - engine.APPLY_CHUNK - 1
        assert not store.pending_holes()
