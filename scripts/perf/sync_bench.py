"""Disposable two-worker sync benchmark. Never attaches to an existing app.

python -m scripts.perf.sync_bench run --output /tmp/sync-bench.json
Internal lab/child modes are invoked only by run. All probes live in this file;
there are no production instrumentation hooks or changed sync constants.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, deque
from contextlib import contextmanager
import contextvars
import functools
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import resource
import re
import runpy
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
SCOPE = contextvars.ContextVar("sync_bench_scope", default="other")
TRACE = contextvars.ContextVar("sync_bench_trace", default=None)


def summarize(values):
    """Nearest-rank percentiles, matching scripts/perf/stats.mjs."""
    xs = sorted(x for x in values if isinstance(x, (float, int)) and math.isfinite(x))
    def percentile(p):
        return xs[max(0, math.ceil(p * len(xs)) - 1)] if xs else None
    return dict(n=len(xs), p50=percentile(.50), p75=percentile(.75),
                max=max(xs) if xs else None)


def op_key(env):
    return [env["origin_device_id"], env["origin_incarnation"], env["origin_seq"]]


def delta_lags(owner_rows, peer_rows):
    """Compare common seq endpoints even if replay coalesces different frames."""
    by_seq = {row["seq"]: row["t"] for row in owner_rows}
    lags = [(row["t"] - by_seq[row["seq"]]) * 1000
            for row in peer_rows if row["seq"] in by_seq]
    if len(lags) < 2:
        raise RuntimeError("fewer than two matched deltas: not a steady streaming sample")
    return lags


def recall_visible(store, text):
    """Replicas qualify local mN aliases; match the unique fact, not its alias."""
    return any(entry.text == text for entry, _ in store.search_memories(text, limit=100))


def sanitize_log(text):
    return re.sub(r"((?:token|secret)=)[^&\s\"]+", r"\1[redacted]", text, flags=re.I)


def capture_log(stream, destination):
    """Drop URL-bearing announcements/readiness lines before any log write."""
    for line in stream:
        if not re.search(r"\b(?:https?|wss?)://", line, flags=re.I):
            destination.write(sanitize_log(line))
            destination.flush()


@contextmanager
def attempt(samples, save, *journeys):
    """Keep failed attempts out of latency percentiles, not out of the report."""
    start, first = time.monotonic(), len(samples)
    try:
        yield
    except BaseException as exc:
        samples[first:] = [dict(journey=name, status="failed", total_ms=None,
                                failure=type(exc).__name__,
                                observed_seconds=time.monotonic()-start)
                           for name in journeys]
        save()
        raise


def sample_summary(samples):
    metrics, counts = {}, {}
    for row in samples:
        name, status = row["journey"], row.get("status", "ok")
        counts.setdefault(name, dict(ok=0, failed=0, censored=0))[status] += 1
        for key, value in row.items():
            if key.endswith("_ms") or key.endswith("_per_minute") or key == "envelopes":
                values = metrics.setdefault(name + "." + key, [])
                if status == "ok" and isinstance(value, (int, float)):
                    values.append(value)
    summary = {key: dict(summarize(values),
                         failed=counts[key.rpartition(".")[0]]["failed"],
                         censored=counts[key.rpartition(".")[0]]["censored"])
               for key, values in metrics.items()}
    return summary, counts


def completed_scans(first, last):
    """A crossing scan belongs to the snapshot window in which it completes."""
    return [e for e in last["events"] if e["kind"] == "scan_full"
            and first["now"] <= e["t"] < last["now"]]


def stop_lab(process, report, save):
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=90)
    except subprocess.TimeoutExpired as exc:
        report.update(teardown_exit=process.poll(), teardown_error="cleanup_timeout")
        save()
        raise RuntimeError("devloop did not finish cleanup; inspect its owned lease") from exc
    report["teardown_exit"] = process.returncode
    save()
    if process.returncode != 0:
        raise RuntimeError(f"devloop cleanup failed: exit {process.returncode}")


def request(port, token, path, body=None, method=None, timeout=180):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method or ("POST" if body is not None else "GET"), path,
                     json.dumps(body) if body is not None else None,
                     {"X-Sync-Bench": token, "Content-Type": "application/json"})
        response = conn.getresponse()
        data = response.read()
        if response.status >= 400:
            raise RuntimeError(f"probe {path}: HTTP {response.status}: {data[:300]!r}")
        return json.loads(data)
    finally:
        conn.close()


def lab(directory):
    """Use the real devloop CLI/lease, replacing only the child entrypoint."""
    from app import preview_run
    from scripts.devloop import workers
    original_env = workers.worker_environment
    original_supervise = preview_run.supervise

    def environment(*args, **kwargs):
        env, vite = original_env(*args, **kwargs)
        home = Path(env["SHUFFLE_DEV_ACCESS_FILE"]).parent
        token = secrets.token_hex(32)
        env["SYNC_BENCH_TOKEN"] = token
        env["SYNC_BENCH_RECEIPT"] = str(Path(directory) / (home.name + ".json"))
        return env, vite

    def supervise(command, env, *args, **kwargs):
        return original_supervise([sys.executable, "-m", "scripts.perf.sync_bench", "child"],
                                  env, *args, **kwargs)

    workers.worker_environment = environment
    preview_run.supervise = supervise
    sys.argv = ["devloop", "up", "--workers", "2", "--no-vite", "--ci",
                "--status", str(Path(directory) / "status.json"), "--ttl-seconds", "10800"]
    runpy.run_module("scripts.devloop", run_name="__main__")


def child():
    """Process-local measurement hooks, only in an owned disposable preview."""
    if os.environ.get("AI_SHUFFLE_PREVIEW") != "1" or not os.environ.get("SYNC_BENCH_TOKEN"):
        raise RuntimeError("benchmark child requires its private devloop environment")
    from app.db import base
    from app.network import engine, transport
    import psycopg2.extras

    lock = threading.RLock()
    events = deque(maxlen=400000)
    totals = Counter()
    writes = {}
    loop = None
    from starlette.applications import Starlette
    original_asgi = Starlette.__call__
    async def measured_asgi(self, scope, receive, send):
        token = SCOPE.set("peer_api" if scope.get("path", "").startswith("/api/v1/network/peer/") else SCOPE.get())
        try:
            return await original_asgi(self, scope, receive, send)
        finally:
            SCOPE.reset(token)
    Starlette.__call__ = measured_asgi

    def emit(kind, **fields):
        with lock:
            events.append(dict(kind=kind, t=time.monotonic(), **fields))

    # Patch methods, not the cursor type: engine._apply_chunk uses the real
    # psycopg2 type's module to decide whether savepoints are supported.
    original_execute = psycopg2.extras.RealDictCursor.execute
    original_executemany = psycopg2.extras.RealDictCursor.executemany
    def execute(self, query, vars=None):
        start = time.monotonic()
        try:
            result = original_execute(self, query, vars)
            match = re.match(r"\s*(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+(user_preferences|sessions|network_session_active|projects_v2|projects)\b", str(query), re.I)
            if TRACE.get() and match and self.rowcount > 0:
                with lock:
                    writes.setdefault(id(self.connection), set()).add(match[1].lower())
            return result
        finally:
            scope = SCOPE.get()
            with lock:
                totals[scope + ".statements"] += 1
                totals[scope + ".sql_seconds"] += time.monotonic() - start

    def executemany(self, query, vars_list):
        values = list(vars_list)
        start = time.monotonic()
        try:
            return original_executemany(self, query, values)
        finally:
            scope = SCOPE.get()
            with lock:
                totals[scope + ".statements"] += len(values)
                totals[scope + ".sql_seconds"] += time.monotonic() - start
    psycopg2.extras.RealDictCursor.execute = execute
    psycopg2.extras.RealDictCursor.executemany = executemany

    original_to_thread = asyncio.to_thread
    async def measured_to_thread(func, /, *args, **kwargs):
        def call():
            scope, started = SCOPE.get(), time.thread_time()
            try:
                return func(*args, **kwargs)
            finally:
                with lock:
                    totals[scope + ".offload_cpu_seconds"] += time.thread_time()-started
        return await original_to_thread(call)
    asyncio.to_thread = measured_to_thread

    original_commit = base._PgDictConnection.commit
    original_rollback = base._PgDictConnection.rollback
    def rollback(self):
        try:
            return original_rollback(self)
        finally:
            with lock:
                writes.pop(id(self._conn), None)

    def commit(self):
        result = original_commit(self)
        with lock:
            tables = writes.pop(id(self._conn), set())
        if TRACE.get() and tables:
            emit("commit", trace=TRACE.get(), tables=sorted(tables))
        return result

    base._PgDictConnection.rollback = rollback
    base._PgDictConnection.commit = commit

    def on_commit(cur, callback):
        # Match the physical cursor connection to its app-managed owner.
        for owner, callbacks in reversed(base._commit_callbacks.get()):
            if getattr(owner, "_conn", None) is cur.connection:
                callbacks.append(callback)
                return
        raise RuntimeError("probe could not identify record transaction")

    original_record = engine.record
    @functools.wraps(original_record)
    def record(cur, collection, record_id, row, **kwargs):
        env = original_record(cur, collection, record_id, row, **kwargs)
        if env:
            fields = dict(op=op_key(env), collection=collection, record_id=record_id,
                          scope=SCOPE.get(), bytes=len(json.dumps(env).encode()))
            on_commit(cur, lambda: emit("published", **fields))
        return env
    engine.record = record

    original_apply = engine.apply_envelope
    @functools.wraps(original_apply)
    def apply(cur, env, **kwargs):
        arrival = time.monotonic()
        result = original_apply(cur, env, **kwargs)
        if result == "applied":
            fields = dict(op=op_key(env), collection=env["collection"], record_id=env["record_id"],
                          arrival=arrival)
            on_commit(cur, lambda: emit("applied", **fields))
        return result
    engine.apply_envelope = apply
    original_append = engine._apply_event_append
    @functools.wraps(original_append)
    def apply_append(cur, chunk, *args, **kwargs):
        arrival = time.monotonic()
        result = original_append(cur, chunk, *args, **kwargs)
        if result:
            for env in chunk:
                fields = dict(op=op_key(env), collection=env["collection"], record_id=env["record_id"], arrival=arrival)
                on_commit(cur, lambda fields=fields: emit("applied", **fields))
        return result
    engine._apply_event_append = apply_append

    original_scan = engine.scan_all
    @functools.wraps(original_scan)
    def scan(*args, **kwargs):
        scope = "scan_targeted" if kwargs.get("collections") is not None else "scan_full"
        token = SCOPE.set(scope)
        start, cpu = time.monotonic(), time.thread_time()
        with lock:
            statements = totals[scope + ".statements"]
            sql_seconds = totals[scope + ".sql_seconds"]
        try:
            result = original_scan(*args, **kwargs)
            with lock:
                statements = totals[scope + ".statements"]-statements
                sql_seconds = totals[scope + ".sql_seconds"]-sql_seconds
            emit(scope, start=start, seconds=time.monotonic()-start,
                 cpu=time.thread_time()-cpu, published=result,
                 statements=statements, sql_seconds=sql_seconds)
            return result
        finally:
            SCOPE.reset(token)
    engine.scan_all = scan

    original_sync = transport.PeerLink._sync_once
    @functools.wraps(original_sync)
    async def sync(*args, **kwargs):
        token = SCOPE.set("exchange")
        start = time.monotonic()
        try:
            return await original_sync(*args, **kwargs)
        finally:
            emit("exchange", start=start, seconds=time.monotonic()-start)
            SCOPE.reset(token)
    transport.PeerLink._sync_once = sync
    original_request = transport.PeerLink._request
    @functools.wraps(original_request)
    async def exchange_request(self, client, method, path, **kwargs):
        for env in (kwargs.get("json_body") or {}).get("ops", []):
            emit("push_send", op=op_key(env))
        result = await original_request(self, client, method, path, **kwargs)
        if path == "/api/v1/network/peer/exchange":
            for env in result.get("ops", []):
                emit("pull_received", op=op_key(env))
        return result
    transport.PeerLink._request = exchange_request
    original_loop = transport.sync_loop
    async def sync_loop():
        nonlocal loop
        loop = asyncio.get_running_loop()
        await original_loop()
    transport.sync_loop = sync_loop

    async def action(body):
        from app.db import database
        from app.db.async_database import run_sync
        from scripts.devloop.seed import DEV_AGENT_ID
        from app.core.config import settings
        trace = body.get("trace") or uuid.uuid4().hex
        token = TRACE.set(trace)
        scope = SCOPE.set("driver")
        started = time.monotonic()
        try:
            kind = body["kind"]
            sid = body.get("sid")
            if kind == "preference":
                from app.core.preference_identity import principal_preference_identity
                identity = await run_sync(principal_preference_identity, {"kind": "admin"})
                result = await run_sync(database.set_user_preference, *identity, body["key"], body["value"])
            elif kind == "new_chat":
                sid = sid or str(uuid.uuid4())
                result = await run_sync(database.create_session, session_id=sid, profile_id=DEV_AGENT_ID,
                                        title=body.get("title", "Sync benchmark"), is_active=True)
            elif kind == "rename":
                result = await run_sync(database.update_session, sid, title=body["title"])
            elif kind == "settle":
                from app.network.session_active import set_shared
                result = await set_shared(sid, False)
            elif kind == "project":
                from app.api.projects import create_project_record
                from app.core.models import ProjectCreate
                result = await create_project_record(ProjectCreate(id=body["id"], name=body["id"]))
            elif kind == "project_resolve":
                # Same access resolution used when starting a chat in this project.
                from app.api.common import require_origin_access
                result = await run_sync(require_origin_access, project_id=body["id"])
            elif kind == "memory":
                from app.core.fluid_memory import store_for_owner
                entry = await run_sync(store_for_owner("local").save_memory, body["text"],
                                       scope="global", provenance="session", when="Sync benchmark")
                result = {"id": entry.id}
            elif kind == "memory_burst":
                from app.core.fluid_memory import store_for_owner
                def save_burst():
                    store = store_for_owner("local")
                    return [store.save_memory(text, scope="global", provenance="session",
                                              when="Sync benchmark").id for text in body["texts"]]
                result = await run_sync(save_burst)
            elif kind == "recall":
                from app.core.fluid_memory import store_for_owner
                result = await run_sync(recall_visible, store_for_owner("local"), body["text"])
            elif kind == "long_seed":
                from scripts.devloop.seed_long import seed_long
                result = await seed_long()
            elif kind == "append":
                from scripts.devloop.seed import Transcript
                transcript = Transcript(sid, DEV_AGENT_ID, "sync-bench")
                for i in range(body["count"] // 5):
                    transcript.turn(f"Offline catch-up {i}", reply="Synthetic history", answer="Done")
                result = {"events": await run_sync(transcript.save)}
            elif kind == "stream":
                from app.api.chat_ws_handler import ChatWebSocketHandler
                from app.core.mobile_tool_identity import principal
                class Sender(ChatWebSocketHandler):
                    async def send_json(self, data):
                        if data.get("type") in ("error", "runner_unreachable"):
                            raise RuntimeError(data.get("message", "chat intake refused"))
                sender = Sender(None, None, "admin", None, principal(
                    "admin", "install", "local-transport:" + settings.effective_instance_id))
                sender.current_session_id = sid  # no synthetic WebSocket registration
                result = await sender.handle_query(dict(
                    prompt=body.get("prompt", "[script:default tps=12]"),
                    profile=DEV_AGENT_ID, session_id=sid))
                if not result or not result.get("turn_id"):
                    raise RuntimeError("native chat intake did not queue a turn")
            elif kind == "count":
                def count():
                    with base.get_db() as conn:
                        cur = conn.cursor()
                        cur.execute("SELECT count(*) AS n FROM session_events WHERE session_id=%s", (sid,))
                        return cur.fetchone()["n"]
                result = await run_sync(count)
            else:
                raise ValueError("unknown action")
            return dict(result=result, sid=sid, trace=trace, start=started, end=time.monotonic())
        finally:
            TRACE.reset(token)
            SCOPE.reset(scope)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.do_POST()

        def do_POST(self):
            if not secrets.compare_digest(self.headers.get("X-Sync-Bench", ""), os.environ["SYNC_BENCH_TOKEN"]):
                self.send_error(403)
                return
            try:
                if self.path in ("/metrics", "/idle"):
                    with lock:
                        selected = list(events) if self.path == "/metrics" else [
                            e for e in events if e["kind"] in ("scan_full", "scan_targeted", "exchange")]
                        result = dict(totals=dict(totals), events=selected,
                                      cpu=resource.getrusage(resource.RUSAGE_SELF).ru_utime +
                                          resource.getrusage(resource.RUSAGE_SELF).ru_stime,
                                      now=time.monotonic())
                elif self.path == "/action" and loop is not None:
                    body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    result = asyncio.run_coroutine_threadsafe(action(body), loop).result(timeout=1800)
                else:
                    self.send_error(503)
                    return
                payload = json.dumps(result, default=str).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except Exception as exc:
                payload = json.dumps({"error": type(exc).__name__, "detail": str(exc)[:300]}).encode()
                self.send_response(500)
                self.end_headers()
                self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    receipt = dict(port=server.server_port, token=os.environ["SYNC_BENCH_TOKEN"], pid=os.getpid(),
                   owner_port=int(os.environ["PORT"]),
                   owner_token=os.environ["SHUFFLE_DESKTOP_LISTENER_TOKEN"])
    path = Path(os.environ["SYNC_BENCH_RECEIPT"])
    path.write_text(json.dumps(receipt))
    path.chmod(0o600)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    runpy.run_module("app.main", run_name="__main__")


class Worker:
    def __init__(self, receipt):
        self.receipt = receipt

    def probe(self, path, body=None):
        return request(self.receipt["port"], self.receipt["token"], path, body, timeout=1900)

    def action(self, kind, **kwargs):
        return self.probe("/action", dict(kind=kind, **kwargs))

    def api(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.receipt["owner_port"], timeout=30)
        try:
            conn.request("GET", "/api/v1/" + path,
                         headers={"X-Shuffle-Desktop-Listener": self.receipt["owner_token"]})
            response = conn.getresponse()
            raw = response.read()
            if response.status >= 400:
                raise RuntimeError(f"API {path}: HTTP {response.status}")
            return json.loads(raw)
        finally:
            conn.close()


class EventReader:
    """Read real API SSE, retaining delta sequence boundaries and arrival times."""
    def __init__(self, worker, sid):
        self.worker, self.sid = worker, sid
        self.rows = []
        self.error = None
        self.ready = threading.Event()
        self.done = threading.Event()
        self.thread = threading.Thread(target=self.read, daemon=True)

    def read(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.worker.receipt["owner_port"], timeout=120)
        try:
            conn.request("GET", f"/api/v1/sessions/{self.sid}/events",
                         headers={"X-Shuffle-Desktop-Listener": self.worker.receipt["owner_token"]})
            response = conn.getresponse()
            if response.status != 200:
                raise RuntimeError(f"SSE HTTP {response.status}")
            self.ready.set()
            name = None
            while line := response.readline():
                if line.startswith(b"event:"):
                    name = line[6:].strip().decode()
                if line.startswith(b"data:"):
                    data = json.loads(line[5:])
                    if name == "assistant.text_delta":
                        self.rows.append(dict(t=time.monotonic(), seq=data["seq"],
                                              text=data.get("text", "")))
                    if name == "turn.completed":
                        break
        except Exception as exc:
            self.error = type(exc).__name__ + ": " + str(exc)
        finally:
            conn.close()
            self.done.set()
            self.ready.set()

    def start(self):
        self.thread.start()
        if not self.ready.wait(30) or self.error:
            raise RuntimeError(self.error or "SSE did not connect")

    def finish(self):
        if not self.done.wait(150) or self.error:
            raise RuntimeError(self.error or "SSE did not complete")
        self.thread.join()
        if not self.rows:
            raise RuntimeError("fake provider produced no visible text deltas")


def wait_for(predicate, timeout=150, interval=.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value, time.monotonic()
        time.sleep(interval)
    raise TimeoutError("benchmark convergence deadline exceeded")


def stages(start, visible, source, peer, collection, record_id=None):
    """Correlate by signed origin sequence, never by whichever op arrived last."""
    published = [e for e in source["events"] if e["kind"] == "published"
                 and e["collection"] == collection and e["t"] >= start
                 and (record_id is None or e["record_id"] == record_id)]
    pairs = [(p, a) for p in published for a in peer["events"]
             if a["kind"] == "applied" and a["op"] == p["op"]]
    if not pairs:
        return {}
    p, a = min(pairs, key=lambda pair: pair[1]["t"])
    result = dict(commit_to_publish_ms=(p["t"]-start)*1000,
                  publish_to_apply_entry_ms=(a["arrival"]-p["t"])*1000,
                  apply_ms=(a["t"]-a["arrival"])*1000,
                  apply_to_visible_ms=(visible-a["t"])*1000,
                  publish_scope=p["scope"], envelope_bytes=p["bytes"])
    boundaries = [e for e in source["events"] + peer["events"]
                  if e["kind"] in ("push_send", "pull_received") and e["op"] == p["op"]
                  and p["t"] <= e["t"] <= a["arrival"]]
    if boundaries:
        boundary = min(boundaries, key=lambda e: e["t"])
        result.update(publish_to_exchange_ms=(boundary["t"]-p["t"])*1000,
                      exchange_to_apply_ms=(a["arrival"]-boundary["t"])*1000,
                      exchange_boundary=boundary["kind"])
    return result


def run(args):
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    samples = []
    def interrupted(signum, frame):
        raise KeyboardInterrupt("benchmark interrupted; tearing down its lease")
    signal.signal(signal.SIGTERM, interrupted)
    report = dict(samples=samples, setup=dict(platform=sys.platform, machine=os.uname().machine,
                                             kernel=os.uname().release, logical_cpus=os.cpu_count(),
                                             commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                                             seed="default + long after pairing", polling_ms=100), gaps=[
        "API/SSE visibility, not browser rendering; same-host loopback, no WAN shaping.",
        "Writers reuse production stores/services; no click/composer/Remember-tool dispatch overhead.",
        "CPU: whole app process and scoped to_thread CPU, not PostgreSQL CPU or exact async-task CPU.",
        "SQL: instrumented default dict cursors; explicit cursor factories and PostgreSQL internal statements excluded.",
        "Memory timestamp is file-save return (upper bound); DB actions use first traced domain commit.",
        "Catch-up pauses B process without closing its sockets; not a disconnected-host/restart experiment.",
        "Hand-off install_history not measured.",
        "Per-cursor timers/counters and per-op JSON/event recording overhead included; not calibrated against uninstrumented apps.",
        "Streaming and catch-up report combined lag/convergence, not separately attributed publication/exchange/apply stages.",
    ])
    def save():
        report["summary"], report["counts"] = sample_summary(samples)
        output.write_text(json.dumps(report, indent=2))

    with tempfile.TemporaryDirectory(prefix="shuffle-sync-bench-") as raw:
        directory = Path(raw)
        log = (directory / "lab.log").open("w+")
        process = subprocess.Popen([sys.executable, "-m", "scripts.perf.sync_bench", "lab", raw],
                                   cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, bufsize=1)
        log_thread = threading.Thread(target=capture_log, args=(process.stdout, log), daemon=True)
        log_thread.start()
        b = None
        try:
            with attempt(samples, save, "setup"):
                def ready():
                    path = directory / "status.json"
                    if process.poll() is not None:
                        raise RuntimeError("devloop failed; see the sanitized .log beside the output")
                    try:
                        return json.loads(path.read_text()).get("state") == "ready"
                    except (FileNotFoundError, json.JSONDecodeError):
                        return False
                wait_for(ready, timeout=600, interval=1)
                a, b = [Worker(json.loads((directory / (name + ".json")).read_text()))
                        for name in ("worker-a", "worker-b")]
                report["setup"]["seed"] = json.loads((directory / "status.json").read_text())["seed"]
                print("Two disposable workers paired", flush=True)

            def measure(name, action, visible, collection, record_id=None):
                with attempt(samples, save, name):
                    result = action()
                    _, end = wait_for(visible)
                    sm, pm = a.probe("/metrics"), b.probe("/metrics")
                    commits = [e["t"] for e in sm["events"] if e["kind"] == "commit" and e["trace"] == result["trace"]]
                    committed = min(commits) if commits else result["end"]
                    row = dict(journey=name, total_ms=(end-committed)*1000,
                               write_ms=(result["end"]-result["start"])*1000,
                               **stages(committed, end, sm, pm, collection, record_id))
                    samples.append(row)
                    save()
                    print(name, round(row["total_ms"], 1), "ms", flush=True)
                    return result

            if args.memory_only:
                def warm_scanner(i):
                    value = f"memory-bench-{i}-{uuid.uuid4().hex}"
                    a.action("preference", key="lastSelectedModel", value=value)
                    wait_for(lambda: (b.api("preferences/lastSelectedModel") or {}).get("value") == value)

                for i in range(args.runs):
                    with attempt(samples, save, "memory"):
                        warm_scanner(i)
                        text = "benchmarkfruit" + uuid.uuid4().hex
                        measure("memory", lambda: a.action("memory", text=text),
                                lambda: b.action("recall", text=text)["result"] is True, "memory")

                for i in range(args.runs):
                    with attempt(samples, save, "memory_burst"):
                        warm_scanner(i)
                        texts = ["burstfruit" + uuid.uuid4().hex for _ in range(10)]
                        result = a.action("memory_burst", texts=texts)
                        wait_for(lambda: all(b.action("recall", text=text)["result"] for text in texts))
                        # Include follow-up passes: count durable envelopes, not scan wakes.
                        time.sleep(3)
                        envelopes = [e for e in a.probe("/metrics")["events"]
                                     if e["kind"] == "published" and e["collection"] == "memory"
                                     and e["t"] >= result["start"]]
                        samples.append(dict(journey="memory_burst", saves=10, envelopes=len(envelopes),
                                            envelope_bytes=sum(e["bytes"] for e in envelopes),
                                            write_ms=(result["end"]-result["start"])*1000))
                        save()
                        print("memory_burst", len(envelopes), "envelopes", flush=True)

                for i in range(args.runs):
                    with attempt(samples, save, "memory_cold"):
                        # No polling or mutations during the scanner's idle/backoff window.
                        print("memory_cold idle", args.memory_cold_seconds, "seconds", flush=True)
                        time.sleep(args.memory_cold_seconds)
                        text = "coldfruit" + uuid.uuid4().hex
                        measure("memory_cold", lambda: a.action("memory", text=text),
                                lambda: b.action("recall", text=text)["result"] is True, "memory")

            for i in range(0 if args.stream_only or args.memory_only else args.runs):
                key, value = "lastSelectedModel", f"sync-bench-{i}"
                measure("preference", lambda: a.action("preference", key=key, value=value),
                        lambda: (b.api("preferences/" + key) or {}).get("value") == value,
                        "user_preferences")
                sid = str(uuid.uuid4())
                measure("new_chat", lambda: a.action("new_chat", sid=sid),
                        lambda: any(r["id"] == sid for r in b.api("sessions")), "sessions", sid)
                title = "Sync rename " + uuid.uuid4().hex[:8]
                measure("rename", lambda: a.action("rename", sid=sid, title=title),
                        lambda: any(r["id"] == sid and r["title"] == title for r in b.api("sessions")), "sessions", sid)
                measure("settle", lambda: a.action("settle", sid=sid),
                        lambda: not any(r["id"] == sid for r in b.api("sessions?active_only=true&limit=100")), "session_active")
                pid = "sync-bench-" + uuid.uuid4().hex[:10]
                with attempt(samples, save, "project_create"):
                    a.action("project", id=pid)
                    created = time.monotonic()
                    try:
                        _, seen = wait_for(lambda: any(r["name"] == pid for r in b.api("cluster/projects")["projects"]),
                                           timeout=3)
                        samples.append(dict(journey="project_create", total_ms=(seen-created)*1000, visible=True))
                    except TimeoutError:
                        samples.append(dict(journey="project_create", status="censored", total_ms=None,
                                            visible=False, observed_seconds=time.monotonic()-created))
                    save()
                measure("project_resolve", lambda: a.action("project_resolve", id=pid),
                        lambda: any(r["name"] == pid for r in b.api("cluster/projects")["projects"]), "projects_v2")
                text = "benchmarkfruit" + uuid.uuid4().hex[:10]
                measure("memory", lambda: a.action("memory", text=text),
                        lambda: b.action("recall", text=text)["result"] is True, "memory")

            for i in range(0 if args.memory_only else args.runs):
                with attempt(samples, save, "stream_first_token", "stream_steady_delta"):
                    sid = str(uuid.uuid4())
                    a.action("new_chat", sid=sid)
                    wait_for(lambda: any(r["id"] == sid for r in b.api("sessions?limit=100")))
                    local, remote = EventReader(a, sid), EventReader(b, sid)
                    local.start()
                    remote.start()
                    started = time.monotonic()
                    a.action("stream", sid=sid)
                    local.finish()
                    remote.finish()
                    # Backfill can coalesce deltas. Match common sequence endpoints,
                    # never assume the same number of SSE frames on both workers.
                    lags = delta_lags(local.rows, remote.rows)
                    samples.append(dict(journey="stream_first_token", total_ms=(remote.rows[0]["t"]-started)*1000,
                                        peer_behind_owner_ms=(remote.rows[0]["t"]-local.rows[0]["t"])*1000,
                                        matched_deltas=len(lags), owner_frames=len(local.rows), peer_frames=len(remote.rows)))
                    for lag in lags:
                        samples.append(dict(journey="stream_steady_delta", total_ms=lag))
                    save()
                    print("stream", i + 1, "matched deltas", len(lags), flush=True)

            # Offline is SIGSTOP of this lab's exact B child, never an installed worker.
            for count in ([] if args.stream_only or args.memory_only else args.catchup):
                for i in range(args.runs):
                    with attempt(samples, save, f"catchup_{count}"):
                        sid = str(uuid.uuid4())
                        a.action("new_chat", sid=sid)
                        wait_for(lambda: any(r["id"] == sid for r in b.api("sessions")))
                        os.kill(b.receipt["pid"], signal.SIGSTOP)
                        try:
                            wrote = a.action("append", sid=sid, count=count)
                            expected = wrote["result"]["events"]
                            # Offline interval includes normal scanner publication, not just replay.
                            time.sleep(args.offline_seconds)
                        finally:
                            start = time.monotonic()
                            os.kill(b.receipt["pid"], signal.SIGCONT)
                        _, end = wait_for(lambda: b.action("count", sid=sid)["result"] == expected,
                                          timeout=1200, interval=.5)
                        samples.append(dict(journey=f"catchup_{count}", total_ms=(end-start)*1000,
                                            events=expected, offline_seconds=args.offline_seconds))
                        save()
                        print(f"catchup_{count}", round((end-start)*1000), "ms", flush=True)

            if args.long_seed and not args.stream_only:
                with attempt(samples, save, "long_seed_setup"):
                    report["setup"]["long"] = a.action("long_seed")["result"]
                    # Baseline idle only after the peer has all seeded durable events.
                    for size in report["setup"]["long"]["sizes"].values():
                        wait_for(lambda size=size: b.action("count", sid=size["id"])["result"] == size["events"],
                                 timeout=1800, interval=2)
                    print("Long seed converged; waiting for quiet scanner", flush=True)
                    time.sleep(90)
            before = [w.probe("/idle") for w in (a, b)]
            report["idle"] = []
            remaining = 0 if args.stream_only or args.memory_only else args.idle_seconds
            while remaining > 0:
                seconds = min(60, remaining)
                time.sleep(seconds)
                after = [w.probe("/idle") for w in (a, b)]
                for label, first, last in zip(("A", "B"), before, after):
                    minutes = (last["now"]-first["now"])/60
                    scans = completed_scans(first, last)
                    row = dict(worker=label, start=first["now"], end=last["now"], seconds=minutes*60,
                               cpu_seconds_per_minute=(last["cpu"]-first["cpu"])/minutes,
                               counters_per_minute={k: (last["totals"].get(k, 0)-first["totals"].get(k, 0))/minutes
                                                    for k in last["totals"]}, full_scans=scans)
                    report["idle"].append(row)
                before = after
                remaining -= seconds
                save()
            report["idle_summary"] = {}
            for label in ("A", "B"):
                rows = [r for r in report["idle"] if r["worker"] == label]
                report["idle_summary"][label] = dict(
                    cpu_seconds_per_minute=summarize([r["cpu_seconds_per_minute"] for r in rows]),
                    counters={k: summarize([r["counters_per_minute"].get(k, 0) for r in rows])
                              for k in {k for r in rows for k in r["counters_per_minute"]}},
                    full_scan_seconds=summarize([s["seconds"] for r in rows for s in r["full_scans"]]),
                    full_scan_statements=summarize([s["statements"] for r in rows for s in r["full_scans"]]))
            save()
        finally:
            if b is not None:
                try:
                    os.kill(b.receipt["pid"], signal.SIGCONT)
                except ProcessLookupError:
                    pass
            try:
                stop_lab(process, report, save)
            finally:
                log_thread.join(timeout=5)
                log.flush()
                output.with_name(output.name + ".lab.log").write_text((directory / "lab.log").read_text())
                report["log_capture_complete"] = not log_thread.is_alive()
                save()
                if log_thread.is_alive():
                    raise RuntimeError("devloop log capture did not finish")
                log.close()
                process.stdout.close()
    if any(cell["failed"] or cell["censored"] for cell in report["counts"].values()):
        raise RuntimeError("benchmark has failed/censored attempts; see saved results")
    print("Results:", output, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("child")
    sub.add_parser("lab").add_argument("directory")
    p = sub.add_parser("run")
    p.add_argument("--output", required=True)
    p.add_argument("--runs", type=int, default=10)
    p.add_argument("--catchup", type=int, nargs="*", default=[1000, 10000])
    p.add_argument("--offline-seconds", type=float, default=10)
    p.add_argument("--idle-seconds", type=float, default=180)
    p.add_argument("--long-seed", action="store_true")
    p.add_argument("--stream-only", action="store_true", help="fresh paired lab; omit mutations/catch-up/idle")
    p.add_argument("--memory-only", action="store_true", help="warm/cold facts and ten-save envelope bursts only")
    p.add_argument("--memory-cold-seconds", type=float, default=65, help="quiet interval before each cold fact (>60s)")
    args = parser.parse_args()
    if args.mode == "child":
        child()
    elif args.mode == "lab":
        lab(args.directory)
    else:
        if args.runs < 1 or any(n <= 0 or n % 5 for n in args.catchup) or args.idle_seconds <= 0:
            parser.error("runs/idle must be positive; catch-up counts must be positive multiples of five")
        if args.memory_cold_seconds <= 60:
            parser.error("memory cold interval must exceed the 60-second scanner backoff")
        run(args)


if __name__ == "__main__":
    main()
