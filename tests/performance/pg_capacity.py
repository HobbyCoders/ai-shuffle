"""Opt-in disposable PostgreSQL recorder load; never imports app in the launcher."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from urllib.parse import urlparse

IMAGE = "postgres:16-alpine"
LABEL = "ai-shuffle.capacity-owner"
ROOT = Path(__file__).resolve().parents[2]


def clean_env(source):
    # Deliberately do not inherit DATABASE_URL, POSTGRES_*, PG*, app config,
    # provider keys, PYTHONPATH, Docker context/host, or credential file pointers.
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP",
               "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMFILES"}
    return {k: v for k, v in source.items() if k.upper() in allowed}


def validate_identity(name, token):
    if not re.fullmatch(r"[0-9a-f]{32}", token) or name != "shuffle-capacity-" + token:
        raise ValueError("invalid disposable identity")


def docker_args(name, token, cap):
    validate_identity(name, token)
    if not 2 <= cap <= 160:
        raise ValueError("invalid total pool cap")
    return ["docker", "--context", "default", "run", "--detach", "--rm", "--name", name,
            "--label", f"{LABEL}={token}", "--publish", "127.0.0.1::5432",
            "--tmpfs", "/var/lib/postgresql/data:rw,nosuid,nodev,size=1g",
            "--env", "POSTGRES_PASSWORD", "--env", "POSTGRES_USER=capacity",
            "--env", "POSTGRES_DB=capacity", IMAGE,
            "-c", f"max_connections={cap + 10}", "-c", "shared_buffers=128MB"]


def validate_target(target):
    validate_identity(target["name"], target["token"])
    if not re.fullmatch(r"[0-9a-f]{64}", target["container_id"]):
        raise ValueError("invalid container id")
    u = urlparse(target["url"])
    if (u.scheme != "postgresql" or u.hostname != "127.0.0.1"
            or u.username != "capacity" or u.path != "/capacity"
            or not u.port or not 1024 <= u.port <= 65535 or u.query or u.fragment
            or not re.fullmatch(r"[0-9a-f]{48}", u.password or "")):
        raise ValueError("invalid disposable target")
    return u


def docker(command, env):
    p = subprocess.run(["docker", "--context", "default", *command], env=env, capture_output=True,
                       text=True, timeout=90)
    if p.returncode:
        # Docker errors can echo commands/environment. Never emit their text.
        raise RuntimeError("docker operation failed: " + command[0])
    return p.stdout.strip()


def attest(target, env):
    u = validate_target(target)
    info = json.loads(docker(["inspect", target["container_id"]], env))[0]
    ports = info["NetworkSettings"]["Ports"].get("5432/tcp")
    if (info["Id"] != target["container_id"]
            or info["Name"] != "/" + target["name"]
            or info["Config"]["Image"] != IMAGE
            or info["Config"].get("Labels", {}).get(LABEL) != target["token"]
            or not info["State"]["Running"]
            or ports != [{"HostIp": "127.0.0.1", "HostPort": str(u.port)}]
            or any(m.get("Type") in {"bind", "volume"} for m in info.get("Mounts", []))):
        raise ValueError("container attestation failed")


def cleanup_owned(container_id, name, token, env):
    validate_identity(name, token)
    if not re.fullmatch(r"[0-9a-f]{64}", container_id):
        raise ValueError("invalid cleanup id")
    info = json.loads(docker(["inspect", container_id], env))[0]
    if (info["Id"] != container_id or info["Name"] != "/" + name
            or info["Config"].get("Labels", {}).get(LABEL) != token):
        raise ValueError("cleanup ownership mismatch")
    docker(["rm", "--force", container_id], env)
    remaining = docker(["ps", "--all", "--no-trunc", "--filter",
                        "id=" + container_id, "--format", "{{.ID}}"], env)
    if remaining:
        raise RuntimeError("owned container cleanup not confirmed")


def quantiles(values):
    values = sorted(values)
    def q(p):
        return values[min(len(values) - 1, int((len(values) - 1) * p))] if values else 0
    return {"samples": len(values), "p50": q(.5), "p95": q(.95), "p99": q(.99),
            "max": max(values, default=0)}


def worker_environment(target, directory, index):
    validate_target(target)
    env = clean_env(os.environ)
    env.update(TESTING="1", ALLOW_REAL_PG="1", TEST_POSTGRES_DSN=target["url"],
               AI_SHUFFLE_ENV_FILE=str(Path(directory) / "empty.env"),
               DATA_DIR=str(Path(directory) / f"data-{index}"),
               WORKSPACE_DIR=str(Path(directory) / f"workspace-{index}"),
               INSTANCE_ID=f"capacity-{index}", FILE_LOGGING_ENABLED="false",
               LOCAL_MODE="true", PYTHONPATH=str(ROOT),
               PGCONNECT_TIMEOUT="5", PGOPTIONS="-c statement_timeout=5000")
    return env


def child(payload):
    target = payload["target"]
    validate_target(target)
    if (os.environ.get("ALLOW_REAL_PG") != "1" or os.environ.get("TESTING") != "1"
            or os.environ.get("TEST_POSTGRES_DSN") != target["url"]):
        raise ValueError("child safety gate rejected")
    attest(target, clean_env(os.environ))
    # First application import occurs only after override validation and proof
    # that this is the launcher's owned, loopback-only disposable container.
    logging.disable(logging.CRITICAL)
    from app.core.config import settings
    from app.db import base
    connection = base.parse_database_url(target["url"])
    connection.update(hostaddr="127.0.0.1", options="-c statement_timeout=5000", connect_timeout=5, sslmode="disable")
    object.__setattr__(settings, "get_database_connection", lambda: dict(connection))
    base.init_pool(min_conn=1, max_conn=payload["pool_cap"])
    try:
        if payload.get("bootstrap"):
            from app.db.schema_postgres import create_postgres_schema
            with base.get_db() as conn:
                create_postgres_schema(conn.cursor())
            return {"ok": True, "schema_initialized": True}
        return asyncio.run(workload(payload, base))
    finally:
        base.close_pool()


async def workload(p, base):
    from app.core.turn_recorder import TurnRecorder
    from app.db import runs, session_events as events
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=min(128, p["turns"] * 2 + 8)))
    start = time.monotonic()
    errors = Counter()
    counts = Counter()
    append_ms, recorder_ms, pending_samples, buffered_samples, lag_samples = [], [], [], [], []
    pending = 0
    active = []
    peak = 1
    lock = threading.Lock()
    real_append = events.append_events
    opened = base._pg_pool._connect

    def measured_connect(key=None):
        nonlocal peak
        conn = opened(key)
        peak = max(peak, len(base._pg_pool._physical))
        return conn
    base._pg_pool._connect = measured_connect

    def measured_append(*args, **kwargs):
        t = time.monotonic()
        try:
            result = real_append(*args, **kwargs)
            with lock:
                counts["appended_rows"] += len(args[1])
            return result
        except Exception as exc:
            with lock:
                errors["append_" + type(exc).__name__] += 1
            raise
        finally:
            with lock:
                append_ms.append((time.monotonic() - t) * 1000)
    events.append_events = measured_append

    class MeasuredRecorder(TurnRecorder):
        async def _append(self, batch):
            nonlocal pending
            pending += 1
            began = time.monotonic()
            try:
                return await super()._append(batch)
            finally:
                recorder_ms.append((time.monotonic() - began) * 1000)
                pending -= 1

    async def sample():
        while True:
            pending_samples.append(pending)
            buffered_samples.append(sum(sum(len(s) for ss in r._delta_buffers.values()
                                            for s in ss) for r in active))
            t = time.monotonic()
            await asyncio.sleep(.1)
            lag_samples.append(max(0, time.monotonic() - t - .1) * 1000)

    async def turn(i):
        if p["launch"] == "staggered":
            await asyncio.sleep(i * p["stagger_ms"] / 1000)
        sid = f"capacity-{p['index']}-{i}"
        run = await asyncio.to_thread(runs.claim_run, sid, "capacity", 30)
        if not run:
            raise RuntimeError("claim rejected")
        rec = MeasuredRecorder(sid, None, run["id"], run["fence_token"], "capacity")
        # Direct constructor avoids unrelated legacy-history backfill/services.
        # The actual recorder, append, fence, renewal and release paths are real.
        rec._owns_lease = False
        active.append(rec)
        done = asyncio.Event()
        reader_rows = 0

        async def reader():
            nonlocal reader_rows
            while True:
                finishing = done.is_set()
                rows = await asyncio.to_thread(events.get_events, sid, reader_rows, 500)
                for row in rows:
                    reader_rows += 1
                    if row["seq"] != reader_rows:
                        raise RuntimeError("reader sequence gap")
                counts["reader_polls"] += 1
                if finishing and not rows:
                    return
                await asyncio.sleep(.1)

        async def renew():
            while True:
                if not await asyncio.to_thread(runs.renew_run, run["id"], "capacity",
                                                run["fence_token"], 30):
                    raise RuntimeError("lease renewal rejected")
                counts["lease_renewals"] += 1
                try:
                    await asyncio.wait_for(done.wait(), timeout=1)
                    return
                except asyncio.TimeoutError:
                    pass

        tasks = [asyncio.create_task(reader()), asyncio.create_task(renew())]
        expected = hashlib.sha256()
        expected_bytes = 0
        try:
            await rec.start("synthetic capacity input")
            deadline = time.monotonic() + p["seconds"]
            n = 0
            while time.monotonic() < deadline:
                text = f"{i}:{n}:" + "x" * 240 + "\n"
                raw = text.encode()
                expected.update(raw)
                expected_bytes += len(raw)
                counts["provider_deltas"] += 1
                await rec.on_event({"type": "stream_delta", "delta_type": "text", "content": text})
                n += 1
                await asyncio.sleep(p["interval_ms"] / 1000)
            await rec.on_event({"type": "done"})
            await rec.close()
            done.set()
            await asyncio.gather(*tasks)
            if rec.recording_failed or rec._superseded:
                raise RuntimeError("recorder degraded")
            digest, seq, types, actual_bytes = hashlib.sha256(), 0, Counter(), 0
            while True:
                rows = await asyncio.to_thread(events.get_events, sid, seq, 500)
                if not rows:
                    break
                for row in rows:
                    seq += 1
                    if row["seq"] != seq:
                        raise RuntimeError("audit sequence gap")
                    types[row["event_type"]] += 1
                    if row["event_type"] == "assistant.text_delta":
                        raw = row["payload"]["text"].encode()
                        digest.update(raw)
                        actual_bytes += len(raw)
            checks = {"content_hash": digest.digest() == expected.digest(),
                      "text_bytes": actual_bytes == expected_bytes,
                      "turn_started": types["turn.started"] == 1,
                      "user_message": types["message.user"] == 1,
                      "turn_completed": types["turn.completed"] == 1,
                      "reader_caught_up": reader_rows == seq}
            if not all(checks.values()):
                counts.update({"mismatch_" + key: 1 for key, ok in checks.items() if not ok})
                raise RuntimeError("content or lifecycle mismatch")
            counts["verified_turns"] += 1
            counts["verified_rows"] += seq
            counts["expected_text_bytes"] += expected_bytes
            counts["actual_text_bytes"] += actual_bytes
            counts.update({"event_" + k: v for k, v in types.items()})
        finally:
            done.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await rec.close()
            if not await asyncio.to_thread(runs.release_run, run["id"], run["fence_token"]):
                raise RuntimeError("release rejected")

    sampler = asyncio.create_task(sample())
    try:
        results = await asyncio.gather(*(turn(i) for i in range(p["turns"])), return_exceptions=True)
        for r in results:
            if isinstance(r, BaseException):
                errors["turn_" + type(r).__name__] += 1
    finally:
        sampler.cancel()
        await asyncio.gather(sampler, return_exceptions=True)
        events.append_events = real_append
    pressure = base.pressure_stats()
    healthy = (not errors and counts["verified_turns"] == p["turns"]
               and counts["verified_rows"] == counts["appended_rows"] and peak <= p["pool_cap"])
    faults = await fault_checks(base, TurnRecorder, events, real_append) if p["faults"] else {}
    return {"ok": healthy and all(x["ok"] for x in faults.values()),
            "scope": "synthetic recorder, readers and leases; no providers or full agent runtime",
            "turns": p["turns"], "seconds_per_turn": p["seconds"], "launch": p["launch"],
            "counts": dict(counts), "dense_sequences_and_content_verified": healthy,
            "errors": dict(errors), "db_append_ms": quantiles(append_ms),
            "recorder_append_ms_including_executor_queue": quantiles(recorder_ms),
            "pending_appends": quantiles(pending_samples),
            "buffered_text_chars": quantiles(buffered_samples),
            "event_loop_lag_ms": quantiles(lag_samples),
            "pool_cap": p["pool_cap"], "peak_physical_connections": peak,
            "pool_pressure_before_faults": pressure, "faults": faults,
            "wall_seconds": time.monotonic() - start}


async def fault_checks(base, Recorder, events, original):
    """Expected degradation must be visible, followed by a successful DB write."""
    results = {}
    for scenario in ("slow_database_timeout", "pool_saturation", "broken_connection"):
        held = []
        observed = []
        old_wait = base._POOL_ACQUIRE_MAX_WAIT_SECONDS
        rec = Recorder("fault-" + uuid.uuid4().hex, None, None, None, "capacity")
        rec._owns_lease = False
        notified = []
        rec.on_recording_failed = lambda: notified.append(True)
        def failing_append(*args, **kwargs):
            try:
                if scenario == "slow_database_timeout":
                    with base.get_db() as conn:
                        cur = conn.cursor()
                        cur.execute("SET LOCAL statement_timeout = '50ms'")
                        cur.execute("SELECT pg_sleep(0.2)")
                elif scenario == "broken_connection":
                    with base.get_db() as victim:
                        with base.get_db() as killer:
                            killer.cursor().execute("SELECT pg_terminate_backend(%s, 5000)",
                                                    (victim.get_backend_pid(),))
                        victim.cursor().execute("SELECT 1")
                return original(*args, **kwargs)
            except Exception as exc:
                observed.append(type(exc).__name__)
                raise
        try:
            if scenario == "pool_saturation":
                base._POOL_ACQUIRE_MAX_WAIT_SECONDS = .15
                held = [base.get_connection() for _ in range(base._pg_pool.maxconn)]
            events.append_events = failing_append
            await rec.start("expected fault")
        finally:
            events.append_events = original
            base._POOL_ACQUIRE_MAX_WAIT_SECONDS = old_wait
            for conn in held:
                base._close_connection(conn)
        detected = bool(observed and rec.recording_failed and notified)
        await rec.close()
        rows = await asyncio.to_thread(events.get_events, rec.session_id)
        recovery_sid = "recovery-" + uuid.uuid4().hex
        recovery = await asyncio.to_thread(original, recovery_sid,
                                           [{"type": "system.note", "payload": {}}])
        stats = base.pool_stats()
        results[scenario] = {"ok": detected and not rows and recovery == (1, 1)
                            and stats["in_use"] == 0,
                            "expected_errors": dict(Counter(observed)),
                            "recorder_failed": rec.recording_failed,
                            "failure_callback_count": len(notified),
                            "unexpected_persisted_rows": len(rows), "recovery_append": recovery,
                            "pool_after": stats}
    return results


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["smoke", "sustained"], default="smoke")
    parser.add_argument("--turns", type=int, default=80)
    parser.add_argument("--installs", type=int, default=1)
    parser.add_argument("--pool-cap", type=int, default=10)
    parser.add_argument("--launch", choices=["simultaneous", "staggered"], default="simultaneous")
    parser.add_argument("--stagger-ms", type=int, default=25)
    parser.add_argument("--interval-ms", type=int, default=100)
    parser.add_argument("--faults", action="store_true")
    args = parser.parse_args(argv)
    if not (1 <= args.turns <= 128 and 1 <= args.installs <= min(args.turns, 8)
            and 2 <= args.pool_cap <= 20 and 0 <= args.stagger_ms <= 1000
            and 10 <= args.interval_ms <= 1000):
        parser.error("bounds: turns 1..128, installs 1..min(turns,8), pool 2..20, stagger 0..1000, interval 10..1000")
    return args


def launch(args):
    if os.environ.get("ALLOW_REAL_PG", "0") != "1":
        raise RuntimeError("set ALLOW_REAL_PG=1 explicitly to create a disposable container")
    token = uuid.uuid4().hex
    name = "shuffle-capacity-" + token
    env = clean_env(os.environ)
    env["POSTGRES_PASSWORD"] = secrets.token_hex(24)
    container_id = None
    children = []
    target = None
    started = time.monotonic()
    report = {"ok": False, "scope": "synthetic recorder load only", "installs": []}
    try:
        command = docker_args(name, token, args.pool_cap * args.installs)
        container_id = docker(command[3:], env)
        if not re.fullmatch(r"[0-9a-f]{64}", container_id):
            raise ValueError("unexpected docker container id")
        mapping = docker(["port", container_id, "5432/tcp"], env)
        if not re.fullmatch(r"127\.0\.0\.1:[0-9]+", mapping):
            raise ValueError("unexpected port mapping")
        target = {"name": name, "token": token, "container_id": container_id,
                  "url": f"postgresql://capacity:{env['POSTGRES_PASSWORD']}@{mapping}/capacity"}
        attest(target, env)
        deadline = time.monotonic() + 60
        while True:
            try:
                docker(["exec", container_id, "pg_isready", "-h", "127.0.0.1", "-U", "capacity", "-d", "capacity"], env)
                break
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("disposable postgres readiness timeout") from None
                time.sleep(.5)
        with tempfile.TemporaryDirectory(prefix="shuffle-capacity-") as directory:
            Path(directory, "empty.env").write_text("", encoding="utf-8")
            def spawn(payload):
                proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker"],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, cwd=directory,
                                        env=worker_environment(target, directory, payload["index"]))
                children.append(proc)
                proc.stdin.write(json.dumps(payload))
                proc.stdin.close()
                proc.stdin = None
                return proc
            common = dict(target=target, pool_cap=args.pool_cap, seconds=5 if args.mode == "smoke" else 600,
                          launch=args.launch, stagger_ms=args.stagger_ms,
                          interval_ms=args.interval_ms, faults=args.faults)
            bootstrap = spawn(dict(common, bootstrap=True, index="schema"))
            output, _ = bootstrap.communicate(timeout=120)
            report["bootstrap"] = json.loads(output)
            if bootstrap.returncode or not report["bootstrap"]["ok"]:
                raise RuntimeError("schema bootstrap failed")
            workers = [spawn(dict(common, index=i, turns=args.turns // args.installs
                                 + (i < args.turns % args.installs))) for i in range(args.installs)]
            for proc in workers:
                output, _ = proc.communicate(timeout=common["seconds"] + 180)
                result = json.loads(output)
                report["installs"].append(result)
                if proc.returncode:
                    result["ok"] = False
            report["ok"] = all(r["ok"] for r in report["installs"])
    except Exception as exc:
        report["error_type"] = type(exc).__name__
    finally:
        for proc in children:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        # Exact ID returned by our docker run, never an existing name or URL.
        if container_id and re.fullmatch(r"[0-9a-f]{64}", container_id):
            try:
                cleanup_owned(container_id, name, token, env)
                report["cleanup"] = "owned container removed"
            except Exception as exc:
                report["cleanup_error_type"] = type(exc).__name__
                report["ok"] = False
        report["wall_seconds"] = time.monotonic() - started
        report["aggregate_pool_cap"] = args.pool_cap * args.installs
    return report


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["--worker"]:
            result = child(json.load(sys.stdin))
        else:
            result = launch(parse_args())
    except Exception as exc:
        # Never print exception text/tracebacks: a driver can include a DSN.
        result = {"ok": False, "error_type": type(exc).__name__}
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(0 if result["ok"] else 1)
