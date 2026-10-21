#!/usr/bin/env python3
"""Disposable PostgreSQL lab: node retention/resume and large op-log pruning.

Uses the production container supervisor to provision an OWNED temporary cluster,
never DATABASE_URL or a live descriptor. Requires PostgreSQL 16 binaries on PATH.
Run: PYTHONPATH=.:fluid python scripts/profile_backend_costs.py --nodes 5000 --ops 300000
No provider calls, transcript content, credentials or connection details emitted.
"""
import argparse
import asyncio
from contextlib import contextmanager
import gc
import importlib.util
import json
from pathlib import Path
import socket
import tempfile
import time
import tracemalloc
from types import SimpleNamespace

import psycopg2
from psycopg2.extras import RealDictCursor

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def runtime():
    spec = importlib.util.spec_from_file_location('perf_container_runtime', ROOT / 'docker/container_runtime.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def nodes_lab(count, get_db):
    from app.core.provider import fluid_pg_store as module
    from fluid.messages.content import Message, Text
    from fluid.store.base import SessionNode
    module.get_db = get_db
    results = {}
    for cached in (True, False):
        gc.collect()
        tracemalloc.start()
        store = module.PgFluidStore(cache_nodes=cached)
        sid = 'cached' if cached else 'lazy'
        await store.create_session(sid)
        start = time.perf_counter()
        parent = None
        for i in range(count):
            item = SessionNode(node_id=str(i), parent_id=parent,
                               message=Message(role='user', content=[Text(text=str(i) + ':' + 'x' * 8192)]))
            await store.append(sid, item)
            parent = item.node_id
        del item
        gc.collect()
        retained, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        result = dict(append_ms=(time.perf_counter() - start) * 1000,
                      retained_bytes=retained, peak_bytes=peak,
                      cached_nodes=sum(len(tree) for tree in store._nodes.values()))
        del store
        gc.collect()
        resume = []
        for _ in range(3):
            store = module.PgFluidStore(cache_nodes=cached)
            start = time.perf_counter()
            meta = await store.get_session(sid)
            resume.append((time.perf_counter() - start) * 1000)
            assert meta.head_node_id == str(count - 1)
            del store
        store = module.PgFluidStore(cache_nodes=cached)
        path = await store.load_path(sid)
        assert len(path) == count
        assert path[0].message.text().startswith('0:')
        result['resume_ms_samples'] = resume
        result['durable_path_nodes'] = len(path)
        results[sid] = result
        del path, store
    return results


def prune_lab(count, get_db, connection):
    from app.db import network, retention
    retention.get_db = get_db
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""CREATE TABLE sync_ops (ingest_id BIGSERIAL PRIMARY KEY,
                    collection TEXT NOT NULL, record_id TEXT NOT NULL, created_at TEXT NOT NULL)""")
        cur.execute('CREATE INDEX idx_sync_ops_record_ingest ON sync_ops(collection, record_id, ingest_id)')
        cur.execute("""INSERT INTO sync_ops(collection, record_id, created_at)
                    SELECT 'settings', (n % %s)::text, '2000-01-01T00:00:00+00:00'
                    FROM generate_series(1, %s) n""", (count // 10, count))
        cur.execute('ANALYZE sync_ops')
    expected = count - count // 10
    legacy = dict(timeout_seconds=10)
    cur = connection.cursor()
    try:
        cur.execute("SET LOCAL statement_timeout = '10s'")
        cur.execute("SET LOCAL work_mem = '64kB'")
        start = time.perf_counter()
        cur.execute("""DELETE FROM sync_ops WHERE created_at < '2020-01-01'
            AND ingest_id NOT IN (SELECT MAX(ingest_id) FROM sync_ops GROUP BY collection, record_id)""")
        legacy.update(deleted=cur.rowcount, timed_out=False)
    except psycopg2.errors.QueryCanceled:
        legacy['timed_out'] = True
    finally:
        legacy['wall_ms'] = (time.perf_counter() - start) * 1000
        connection.rollback()  # compare identical rows, even on success
    start = time.perf_counter()
    deleted = network.prune_ops()
    elapsed = (time.perf_counter() - start) * 1000
    assert deleted == expected, (deleted, expected)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute('SELECT COUNT(*) AS n, MIN(ingest_id) AS first FROM sync_ops')
        row = cur.fetchone()
        assert row['n'] == count // 10 and row['first'] > expected
    assert network.prune_ops() == 0
    return dict(rows=count, legacy=legacy,
                current=dict(wall_ms=elapsed, deleted=deleted, survivors=count // 10,
                             maximum_delete_rows=retention.BATCH_SIZE))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--nodes', type=int, default=5000)
    parser.add_argument('--ops', type=int, default=300000)
    args = parser.parse_args()
    if args.nodes < 1 or args.ops < 10 or args.ops % 10:
        parser.error('nodes positive; ops a positive multiple of ten')
    supervisor = runtime()
    with tempfile.TemporaryDirectory(prefix='shuffle-perf-') as temporary:
        db = supervisor.Database(SimpleNamespace(effective_data_dir=Path(temporary) / 'data'), port=free_port())
        connection = None
        try:
            db.open()
            # This URL belongs only to the cluster just provisioned above.
            connection = psycopg2.connect(db.url, cursor_factory=RealDictCursor)
            @contextmanager
            def get_db():
                with connection:
                    yield connection
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute('CREATE TABLE fluid_sessions (session_id TEXT PRIMARY KEY, head_node_id TEXT, '
                            'created_at DOUBLE PRECISION NOT NULL, cli_session_id TEXT, project_root TEXT)')
                cur.execute('CREATE TABLE fluid_session_nodes (session_id TEXT NOT NULL, node_id TEXT NOT NULL, '
                            'parent_id TEXT, ts DOUBLE PRECISION NOT NULL, message JSONB NOT NULL, '
                            'PRIMARY KEY (session_id, node_id))')
            result = dict(nodes=asyncio.run(nodes_lab(args.nodes, get_db)),
                          pruning=prune_lab(args.ops, get_db, connection))
            print(json.dumps(result, indent=2))
        finally:
            if connection is not None:
                connection.close()
            db.close()


if __name__ == '__main__':
    main()
