"""Concurrent same-stream applies on independent PostgreSQL connections.

Adapted from the independent review probe for the bounded pending-drain fix.
"""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.db import base, database, network as store
from app.network import engine
from tests.core.network.test_pending_apply_budget import backlog, pg_workers, private_cluster  # noqa: F401
from tests.db.schema_lifecycle_helpers import pg_connect


class PerThread:
    def __init__(self, descriptor, schema):
        self.local = threading.local()
        self.descriptor, self.schema = descriptor, schema
        self.connections = []
        self.lock = threading.Lock()

    def get(self):
        conn = getattr(self.local, "conn", None)
        if conn is None:
            conn = pg_connect(self.descriptor)
            conn.cursor().execute("SET search_path TO " + self.schema)
            conn.cursor().execute("SET statement_timeout='10s'")
            conn.autocommit = False
            self.local.conn = conn
            with self.lock:
                self.connections.append(conn)
        return conn

    def close(self):
        for conn in self.connections:
            conn.close()


@pytest.mark.parametrize("rep", range(3))
@pytest.mark.parametrize("second", ["push_all", "push_tail", "replay_head", "push_mid"])
def test_concurrent_same_stream_apply_is_exactly_once(pg_workers, private_cluster, monkeypatch, second, rep):
    a, b, ops = backlog(pg_workers, 300)
    with b.raw.cursor() as cursor:
        cursor.execute("SELECT current_schema() AS name")
        schema = cursor.fetchone()["name"]
    b.raw.commit()
    pool = PerThread(private_cluster, schema)
    first_chunk_done, second_started = threading.Event(), threading.Event()
    role = threading.local()
    real_chunk = engine._apply_pending_chunk

    def chunk(*args):
        real_chunk(*args)
        if getattr(role, "name", "") == "drain" and not first_chunk_done.is_set():
            first_chunk_done.set()
            assert second_started.wait(10)

    monkeypatch.setattr(engine, "_apply_pending_chunk", chunk)
    try:
        with b.active():
            monkeypatch.setattr(base, "get_connection", pool.get)
            monkeypatch.setattr(database, "get_connection", pool.get)
            ctx = engine.require_context()
            batch = {"push_all": ops, "push_tail": ops[100:], "replay_head": ops[:5],
                     "push_mid": ops[60:200]}[second]

            def run(name, items):
                role.name = name
                if name == "other":
                    second_started.set()
                return engine.apply_batch(items, ctx=ctx)

            with ThreadPoolExecutor(max_workers=2) as executor:
                drain = executor.submit(run, "drain", ops[:1])
                assert first_chunk_done.wait(20)
                other = executor.submit(run, "other", batch)
                results = [drain.result(timeout=60), other.result(timeout=60)]
            assert all(result.retry == 0 for result in results), [result.reasons for result in results]
            assert sum(result.applied for result in results) == len(ops)
            conn = pool.get()
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) AS n FROM session_events WHERE session_id='chat'")
            assert cursor.fetchone()["n"] == 300
            cursor.execute("SELECT COUNT(*) AS n FROM sync_conflicts")
            assert cursor.fetchone()["n"] == 0
            assert store.get_watermark(cursor, a.device_id, 1) == ops[-1]["origin_seq"]
            assert store.count_pending(cursor, a.device_id, 1) == 0
            conn.commit()
    finally:
        second_started.set()
        pool.close()
