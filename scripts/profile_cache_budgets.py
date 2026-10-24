#!/usr/bin/env python3
"""Owned-PostgreSQL cache/allocator lab; never connects to a running worker.

PYTHONPATH=.:fluid python scripts/profile_cache_budgets.py
Compare the same arguments at base/head. Outputs payload sizes, not contents.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import ctypes
import gc
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
import tracemalloc
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def owned_environment(directory):
    home = directory / "home"
    home.mkdir()
    workspace = directory / "workspace"
    workspace.mkdir()
    empty = directory / "empty.env"
    empty.write_text("")
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LD_LIBRARY_PATH")}
    env.update(HOME=str(home), DATA_DIR=str(directory / "data"), WORKSPACE_DIR=str(workspace),
               XDG_CONFIG_HOME=str(home / ".config"), XDG_DATA_HOME=str(home / ".local/share"),
               XDG_CACHE_HOME=str(home / ".cache"), AI_SHUFFLE_ENV_FILE=str(empty), LOCAL_MODE="true")
    return env


def resident():
    wanted = {"VmRSS", "VmHWM", "RssAnon", "RssFile"}
    return {key + "_kib": int(value.split()[0])
            for line in Path("/proc/self/status").read_text().splitlines()
            for key, _, value in [line.partition(":")] if key in wanted}


def process_cpu_seconds(pid):
    # This PID belongs to the disposable cluster's current connection, not the
    # running worker. /proc stat fields 14/15 are user/system CPU clock ticks.
    fields = (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


def trim_lab():
    """This process owns the fixture. Never inject allocator calls into the app."""
    gc.collect()
    before = resident()
    try:
        trim = ctypes.CDLL(None).malloc_trim
    except AttributeError:
        return dict(available=False, before=before)
    trim.argtypes = [ctypes.c_size_t]
    trim.restype = ctypes.c_int
    changed = trim(0)
    return dict(available=True, returned=changed, before=before, after=resident())


async def resume_exercise(get_db, args):
    """Pair the old client-side scans with SQL-filtered scans over identical rows."""
    from app.core.provider import fluid_pg_store as module
    from fluid.events import ToolResult, Usage, TurnStarted, ContextCompaction
    from fluid.store.base import iter_events_of_class

    module.get_db = get_db
    store = module.PgFluidStore()
    await store.create_session("resume")
    for index in range(args.events):
        seq = index * 2 + 1
        await store.append_event("resume", ToolResult(session_id="resume", seq=seq,
            tool_use_id=str(index), name="synthetic", output="x" * args.event_bytes))
        if index % 16 == 0:
            await store.append_event("resume", Usage(session_id="resume", seq=seq + 1))
    await store.flush_events("resume")
    decoded = 0
    original = module.event_from_dict
    def decode(row):
        nonlocal decoded
        decoded += 1
        return original(row)
    module.event_from_dict = decode
    try:
        gc.collect()
        tracemalloc.start()
        started = time.perf_counter()
        sequences = []
        # Resume's usage, provenance, compaction and fingerprint scans.
        for classes in ((Usage,), (TurnStarted,), (ContextCompaction,), (Usage,)):
            if args.resume_filter == "sql":
                stream = iter_events_of_class(store, "resume", 0, classes)
            else:
                stream = store.iter_events("resume", 0)
            sequences.append([e.seq async for e in stream if isinstance(e, classes)])
        seconds = time.perf_counter() - started
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        expected = [index * 2 + 2 for index in range(args.events) if index % 16 == 0]
        assert sequences == [expected, [], [], expected]
        return dict(resume_filter=args.resume_filter, seconds=seconds, rows_decoded=decoded,
                    traced_peak_bytes=peak, **resident())
    finally:
        module.event_from_dict = original


async def exercise(get_db, args):
    from app.core.provider import fluid_pg_store as module
    from fluid.events import ToolResult
    from fluid.events.bus import EventBus
    from fluid.messages import Message, Text
    from fluid.store.base import SessionNode

    module.get_db = get_db
    # Warm codecs/executor outside the traced interval.
    warm = module.PgFluidStore()
    await warm.create_session("warm")
    await warm.append("warm", SessionNode(node_id="warm", parent_id=None,
        message=Message(role="user", content=[Text(text="warm")])))
    del warm
    gc.collect()
    tracemalloc.start()
    store = module.PgFluidStore()
    await store.create_session("native")
    points = []
    start = time.perf_counter()
    for index in range(args.nodes):
        node = SessionNode(node_id=str(index), parent_id=str(index - 1) if index else None,
                           message=Message(role="user", content=[Text(text=str(index) + ":" + "x" * args.payload_bytes)]))
        await store.append("native", node)
        if (index + 1) % args.read_path_every == 0:
            path = await store.load_path("native")
            assert len(path) == index + 1 and path[-1].node_id == str(index)
            del path
        del node
        if index + 1 in {args.nodes // 4, args.nodes // 2, args.nodes}:
            gc.collect()
            nodes = list(store._nodes["native"].values())
            payload = sum(len(json.dumps(n.message.to_dict()).encode()) for n in nodes)
            point = dict(appended=index + 1, retained_nodes=len(nodes), message_json_bytes=payload,
                         traced_heap_bytes=tracemalloc.get_traced_memory()[0], **resident())
            points.append(point)
            del nodes
    native_seconds = time.perf_counter() - start
    assert len(await store.load_path("native")) == args.nodes
    native_trim = trim_lab()
    del store
    gc.collect()
    released_native_trim = trim_lab()
    tracemalloc.stop()

    # Real persistence -> the app's live page/iterator paths (compatibility
    # events_since is opt-in). Tool output can exceed a streaming delta.
    store = module.PgFluidStore()
    await store.create_session("events")
    bus = EventBus()
    for index in range(args.events):
        event = ToolResult(session_id="events", seq=index + 1, tool_use_id=str(index),
                           name="synthetic", output="x" * args.event_bytes + str(index))
        await store.append_event("events", event)
        await bus.publish(event)
    del event
    pages = []
    original_page = module._events_page_sync
    def observed_page(*page_args):
        page = original_page(*page_args)
        pages.append(dict(events=len(page), **resident()))
        return page
    module._events_page_sync = observed_page

    async def replay(after):
        if args.replay_path == "events_since":
            return [event.seq for event in await store.events_since("events", after)]
        if args.replay_path == "iter_events":
            return [event.seq async for event in store.iter_events("events", after)]
        sequences = []
        while page := await store.events_page("events", after):
            sequences.extend(event.seq for event in page)
            after = page[-1].seq
            del page
        return sequences

    with get_db() as conn:
        backend_pid = conn.get_backend_pid()
    gc.collect()
    tracemalloc.start()
    client_cpu_start = time.process_time()
    database_cpu_start = process_cpu_seconds(backend_pid)
    start = time.perf_counter()
    assert await replay(0) == list(range(1, args.events + 1))
    replay_seconds = time.perf_counter() - start
    client_cpu_seconds = time.process_time() - client_cpu_start
    database_cpu_seconds = process_cpu_seconds(backend_pid) - database_cpu_start
    # Release the completed executor Future's result, not just our page ref.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    gc.collect()
    cached = store._events.get("events", [])
    event_result = dict(replay_path=args.replay_path, seconds=replay_seconds,
                        client_cpu_seconds=client_cpu_seconds, database_cpu_seconds=database_cpu_seconds,
                        page_calls=len(pages), max_page_events=max(p["events"] for p in pages),
                        sampled_peak_rss_kib=max(p["VmRSS_kib"] for p in pages),
                        persisted=args.events, retained=len(cached),
                        payload_bytes=sum(len(e.output.encode()) for e in cached),
                        traced_heap_bytes=tracemalloc.get_traced_memory()[0],
                        traced_peak_bytes=tracemalloc.get_traced_memory()[1],
                        bus=bus.memory_stats(), **resident())
    del cached
    # A cursor older than the retained cache must still receive every row.
    assert await replay(1) == list(range(2, args.events + 1))
    assert await replay(args.events) == []
    module._events_page_sync = original_page
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    event_trim = trim_lab()
    del store, bus
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    gc.collect()
    released_event_trim = trim_lab()
    tracemalloc.stop()
    return dict(native=dict(seconds=native_seconds, points=points, trim_while_held=native_trim,
                            trim_after_release=released_native_trim),
                events=dict(**event_result, replay_verified=True, trim_while_held=event_trim,
                            trim_after_release=released_event_trim))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", type=int, default=2048)
    parser.add_argument("--payload-bytes", type=int, default=8192)
    parser.add_argument("--read-path-every", type=int, default=1)
    parser.add_argument("--replay-path", choices=("events_page", "iter_events", "events_since"),
                        default="events_page")
    parser.add_argument("--resume-filter", choices=("client", "sql"),
                        help="measure only synthetic native resume scans")
    parser.add_argument("--events", type=int, default=128)
    parser.add_argument("--event-bytes", type=int, default=1024 * 1024)
    args = parser.parse_args()
    if min(value for value in vars(args).values() if isinstance(value, int)) < 1:
        parser.error("all fixture sizes must be positive")
    with tempfile.TemporaryDirectory(prefix="shuffle-cache-lab-") as temporary:
        directory = Path(temporary)
        env = owned_environment(directory)
        os.environ.clear()
        os.environ.update(env)
        spec = importlib.util.spec_from_file_location("cache_lab_runtime", ROOT / "docker/container_runtime.py")
        runtime = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = runtime
        spec.loader.exec_module(runtime)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        db = runtime.Database(SimpleNamespace(effective_data_dir=directory / "data"), port=port)
        connection = None
        try:
            db.open()
            db.publish_descriptor()
            import psycopg2
            from psycopg2.extras import RealDictCursor
            connection = psycopg2.connect(db.url, cursor_factory=RealDictCursor)
            @contextmanager
            def get_db():
                try:
                    yield connection
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
            from app.core.config import settings
            from app.db import base
            assert settings.effective_data_dir == directory / "data"
            base.get_db = get_db
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("CREATE TABLE fluid_sessions (session_id TEXT PRIMARY KEY, head_node_id TEXT, "
                            "created_at TEXT, cli_session_id TEXT, project_root TEXT)")
                cur.execute("CREATE TABLE fluid_session_nodes (session_id TEXT, node_id TEXT, parent_id TEXT, "
                            "ts TEXT, message JSONB, PRIMARY KEY(session_id, node_id))")
                cur.execute("CREATE TABLE fluid_session_events (session_id TEXT, seq BIGINT, event JSONB, "
                            "PRIMARY KEY(session_id, seq))")
            result = asyncio.run(resume_exercise(get_db, args) if args.resume_filter
                                 else exercise(get_db, args))
        finally:
            if connection is not None:
                connection.close()
            try:
                if db.process is not None and db.process.poll() is None:
                    db.admin_script("SET statement_timeout = '120s'; CHECKPOINT;", timeout=125)
            finally:
                db.close()
        print(json.dumps(dict(fixture=vars(args), result=result), indent=2), flush=True)


if __name__ == "__main__":
    main()
