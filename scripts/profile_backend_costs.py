#!/usr/bin/env python3
"""Disposable PostgreSQL lab: node retention/resume and large op-log pruning.

Uses the production container supervisor to provision an OWNED temporary cluster,
never DATABASE_URL or a live descriptor. Requires PostgreSQL 16 binaries on PATH.
Run: PYTHONPATH=.:fluid python scripts/profile_backend_costs.py --nodes 1000 --ops 300000
No provider calls, transcript content, credentials or connection details emitted.
"""
import argparse
import asyncio
from contextlib import contextmanager
import gc
import importlib.util
import json
import os
from pathlib import Path
import socket
import signal
import tempfile
import time
import tracemalloc
from types import SimpleNamespace

import psycopg2
from psycopg2.extras import RealDictCursor

ROOT = Path(__file__).resolve().parents[1]


def isolated_environment(directory):
    """Fail closed before any app import, even if a future path misses a patch."""
    home = directory / "home"
    home.mkdir()
    workspace = directory / "workspace"
    workspace.mkdir()
    empty = directory / "empty.env"
    empty.write_text("")
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LD_LIBRARY_PATH")}
    env.update(HOME=str(home), DATA_DIR=str(directory / "data"), WORKSPACE_DIR=str(workspace),
               XDG_CONFIG_HOME=str(home / ".config"), XDG_DATA_HOME=str(home / ".local/share"),
               XDG_CACHE_HOME=str(home / ".cache"), AI_SHUFFLE_ENV_FILE=str(empty),
               LOCAL_MODE="true", PYTHONPATH=str(ROOT) + ":" + str(ROOT / "fluid"))
    return env


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
    from app.core.provider.harness_adapter import HarnessAdapter
    from app.core.memory_reminder import wrap
    note = wrap("# Memory\n- [m1 r1; session] Synthetic benchmark fact")

    def adapter_for(store, sid):
        adapter = HarnessAdapter()
        adapter._engine = SimpleNamespace(store=store, model="lab", provider="lab")
        adapter.sdk_session_id = sid
        # Model steady turns, after the one-time event/provenance bootstrap.
        adapter._memory_rehydrated = True
        adapter._memory_trusted_node = "0"
        return adapter

    # Warm both paths, executor and codecs before tracing either mode.
    for cached in (True, False):
        warm = module.PgFluidStore(cache_nodes=cached)
        sid = f"warmup-{cached}"
        await warm.create_session(sid)
        await warm.append(sid, SessionNode(node_id="0", parent_id=None,
            message=Message(role="user", content=[Text(text="warmup:" + "x" * 8192 + note)]).pin_context(note)))
        adapter = adapter_for(warm, sid)
        assert await adapter._memory_held() is not None
        del warm, adapter
    gc.collect()
    full_loads = []
    original_load = module._load_session_sync
    def counted_load(sid, *, include_nodes=True):
        if include_nodes:
            full_loads.append(sid)
        return original_load(sid, include_nodes=include_nodes)
    module._load_session_sync = counted_load
    results = {}
    for cached in (True, False):
        gc.collect()
        tracemalloc.start()
        store = module.PgFluidStore(cache_nodes=cached)
        sid = 'cached' if cached else 'lazy'
        await store.create_session(sid)
        adapter = adapter_for(store, sid)
        initial_loads = len(full_loads)
        start = time.perf_counter()
        parent = None
        for i in range(count):
            message = Message(role='user', content=[Text(text=str(i) + ':' + 'x' * 8192 + (note if i == 0 else ''))])
            message.pin_context(note if i == 0 else None)
            item = SessionNode(node_id=str(i), parent_id=parent, message=message)
            await store.append(sid, item)
            assert await adapter._memory_held() is not None
            parent = item.node_id
        del item, message
        gc.collect()
        retained, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        result = dict(append_ms=(time.perf_counter() - start) * 1000,
                      retained_bytes=retained, peak_bytes=peak,
                      cached_nodes=sum(len(tree) for tree in store._nodes.values()),
                      memory_reads=count, full_tree_loads_in_turns=len(full_loads) - initial_loads)
        assert result['full_tree_loads_in_turns'] == 0
        del store, adapter
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
        if not cached:
            projection = await store.load_memory_path(sid)
            assert projection is not None and [n.node_id for n in projection] == ['0']
            result['memory_projection_nodes'] = len(projection)
        results[sid] = result
        del path, store
    return results


def prune_lab(count, get_db, connection, work_mem="4MB"):
    from app.db import network, retention
    retention.get_db = get_db
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""CREATE TABLE sync_ops (ingest_id BIGSERIAL PRIMARY KEY,
                    collection TEXT NOT NULL, record_id TEXT NOT NULL, created_at TEXT NOT NULL)""")
        cur.execute('CREATE INDEX idx_sync_ops_record_ingest ON sync_ops(collection, record_id, ingest_id)')
        cur.execute("""INSERT INTO sync_ops(collection, record_id, created_at)
                    SELECT 'settings', MOD(n, %s)::text, '2000-01-01T00:00:00+00:00'
                    FROM generate_series(1, %s) n""", (count // 10, count))
        cur.execute('ANALYZE sync_ops')
        # 64kB deliberately simulates the >work_mem regime at reduced scale;
        # 4MB is the production default, reported separately without that claim.
        cur.execute("SET work_mem = %s", (work_mem,))
    expected = count - count // 10
    legacy = dict(timeout_seconds=10)
    cur = connection.cursor()
    start = time.perf_counter()
    try:
        cur.execute("SET LOCAL statement_timeout = '10s'")
        cur.execute("SET LOCAL work_mem = %s", (work_mem,))
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
    return dict(rows=count, work_mem=work_mem, legacy=legacy,
                current=dict(wall_ms=elapsed, deleted=deleted, survivors=count // 10,
                             maximum_delete_rows=retention.BATCH_SIZE))


def main():
    def terminate(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--nodes', type=int, default=1000)
    parser.add_argument('--ops', type=int, default=300000)
    parser.add_argument('--skip-nodes', action='store_true', help='pruning-only repeat')
    parser.add_argument('--work-mem', choices=('4MB', '64kB'), default='4MB',
                        help='64kB simulates the >work_mem regime at reduced scale')
    args = parser.parse_args()
    if args.nodes < 1 or args.ops < 10 or args.ops % 10:
        parser.error('nodes positive; ops a positive multiple of ten')
    with tempfile.TemporaryDirectory(prefix='shuffle-perf-') as temporary:
        directory = Path(temporary)
        env = isolated_environment(directory)
        os.environ.clear()
        os.environ.update(env)
        supervisor = runtime()
        db = supervisor.Database(SimpleNamespace(effective_data_dir=directory / 'data'), port=free_port())
        connection = None
        try:
            db.open()
            db.publish_descriptor()
            # This URL belongs only to the cluster just provisioned above.
            connection = psycopg2.connect(db.url, cursor_factory=RealDictCursor)
            @contextmanager
            def get_db():
                with connection:
                    yield connection
            from app.core.config import settings
            from app.db import base
            assert settings.effective_data_dir == directory / 'data'
            from app.core.plugin_models import _get_fluid_dir, _get_claude_dir
            assert _get_fluid_dir().is_relative_to(directory / 'home')
            assert _get_claude_dir().is_relative_to(directory / 'home')
            base.get_db = get_db
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute('CREATE TABLE fluid_sessions (session_id TEXT PRIMARY KEY, head_node_id TEXT, '
                            'created_at DOUBLE PRECISION NOT NULL, cli_session_id TEXT, project_root TEXT)')
                cur.execute('CREATE TABLE fluid_session_nodes (session_id TEXT NOT NULL, node_id TEXT NOT NULL, '
                            'parent_id TEXT, ts DOUBLE PRECISION NOT NULL, message JSONB NOT NULL, '
                            'PRIMARY KEY (session_id, node_id))')
            result = dict(nodes={} if args.skip_nodes else asyncio.run(nodes_lab(args.nodes, get_db)),
                          pruning=prune_lab(args.ops, get_db, connection, args.work_mem))
            # Drain only this owned cluster before its normal shutdown guard;
            # never relax the production supervisor deadline.
            db.admin_script("CHECKPOINT", timeout=120)
        finally:
            if connection is not None:
                connection.close()
            db.close()
        # A result is published only after owned-service cleanup succeeds.
        print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
