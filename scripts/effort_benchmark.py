"""Credential-safe agent effort benchmark, driven through the running app.

Run on the worker itself, under Monitor. Never reads or refreshes credentials.
Example: python -m scripts.effort_benchmark --output /tmp/effort-results.json
Use --task ID --repetitions 1 --configs default for an inexpensive preflight.
Only synthetic tasks/metrics go in results; transcripts stay in the app.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import random
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from scripts.live_smoke.client import HttpWorkerClient, ClientError

TASK_FILE = Path(__file__).with_name("effort_benchmark_tasks.json")
MODEL = "gpt-6.1-sol"


def load_tasks():
    return json.loads(TASK_FILE.read_text())["tasks"]


def prepare(task, path):
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    for name, text in task["files"].items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)


def check_task(task, path):
    # Tests and all input fixtures are immutable, even if an agent edits them.
    for name, text in task["files"].items():
        if name != "solution.py" and (not (path / name).is_file()
                                      or (path / name).read_text() != text):
            return False
    if task["check"] == "unittest":
        return subprocess.run([sys.executable, "-m", "unittest", "-q"], cwd=path,
                              capture_output=True, timeout=15).returncode == 0
    try:
        return json.loads((path / "answer.json").read_text()) == task["expected"]
    except (OSError, ValueError):
        return False


def read_metrics(client, sid):
    """Sum per-call usage (not repeated cumulative turn totals) from SSE backfill."""
    usage = []
    tools = 0
    event = None
    seen = set()
    with client._http.stream("GET", f"/api/v1/sessions/{sid}/events") as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if line.startswith("event: "):
                event = line[7:]
                if event in ("caught_up", "reset"):
                    break
            elif line.startswith("data: "):
                record = json.loads(line[6:])
                if event == "tool.use":
                    tools += 1
                if event != "turn.usage":
                    continue
                u = record.get("usage") or {}
                # A call index is stable only within a turn/run.
                index = u.get("model_call_index")
                key = (record.get("turn_id"), record.get("run_id"), index)
                if index is not None and key not in seen:
                    seen.add(key)
                    usage.append(u)
    def total(key):
        vals = [u[key] for u in usage if isinstance(u.get(key), (int, float))]
        return sum(vals) if vals else None
    return {"input_tokens": total("input_tokens"),
            "output_tokens": total("output_tokens"),
            "reasoning_tokens": total("reasoning_tokens"),
            "cache_read_tokens": total("cache_read_input_tokens"),
            "model_calls": len(usage), "tool_calls": tools}


def summarize(rows):
    result = {}
    for config in sorted({r["config"] for r in rows}):
        group = [r for r in rows if r["config"] == config]
        summary = {"runs": len(group), "passed": sum(r["passed"] for r in group),
                   "pass_rate": sum(r["passed"] for r in group) / len(group)}
        for key in ("input_tokens", "output_tokens", "reasoning_tokens", "cache_read_tokens",
                    "wall_seconds", "tool_calls", "model_calls"):
            values = [r[key] for r in group if r.get(key) is not None]
            summary[key] = ({"mean": statistics.mean(values),
                             "stdev": statistics.stdev(values) if len(values) > 1 else 0,
                             "min": min(values), "max": max(values), "observations": len(values)}
                            if values else None)
        result[config] = summary
    return result


class Benchmark:
    def __init__(self, args):
        self.args = args
        self.lock = threading.Lock()
        self.sessions = set()
        self.rows = []
        self.stopped = threading.Event()
        self.model = None
        self.revision = None
        self.capability = None
        self.task_hash = hashlib.sha256(TASK_FILE.read_bytes()).hexdigest()
        # Recovery journal is private local evidence, never part of public results.
        self.journal = Path(args.output).with_suffix(".sessions.json")

    def write_journal(self):
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        self.journal.write_text(json.dumps(sorted(self.sessions)))

    def client(self):
        return HttpWorkerClient(self.args.url, timeout=30)

    def preflight(self):
        c = self.client()
        try:
            workers = c.deploy_status().get("workers") or []
            self.revision = next(w["revision"] for w in workers if w.get("is_self"))
            for p in c.models().get("providers", []):
                if p.get("provider_type") != "chatgpt":
                    continue
                for m in p.get("models", []):
                    if m.get("id") == MODEL:
                        self.model = f"prov:{p['id']}:{MODEL}"
                        self.capability = {k: m.get(k) for k in ("effort_levels", "default_effort")}
            if not self.model:
                raise ValueError("configured ChatGPT subscription does not advertise " + MODEL)
            levels = self.capability["effort_levels"] or []
            if any(v != "default" and v not in levels for v in self.args.configs):
                raise ValueError("requested effort missing from the live model catalog")
            if not any(a.get("id") == self.args.agent for a in c.agents()):
                raise ValueError("agent not available")
        finally:
            c.close()

    def persist(self):
        # No session identifiers, paths, responses or account data in public output.
        data = {"version": 1, "model": MODEL, "worker_revision": self.revision,
                "catalog": self.capability, "seed": self.args.seed,
                "repetitions": self.args.repetitions, "concurrency": self.args.jobs,
                "task_sha256": self.task_hash,
                "summary_by_kind": {kind: summarize([r for r in self.rows if r["kind"] == kind])
                                    for kind in sorted({r["kind"] for r in self.rows})},
                "method": "fresh app sessions, session-only effort override; default omits override",
                "limitations": ["Synthetic lab tasks, not production outcome evidence",
                                "reasoning is a subset of output, never add it twice",
                                "Input excludes separately reported cache reads",
                                "No per-call background effort arm: requires isolated authenticated preview"],
                "rows": self.rows, "summary": summarize(self.rows)}
        out = Path(self.args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        temp = out.with_suffix(out.suffix + ".tmp")
        temp.write_text(json.dumps(data, indent=2) + "\n")
        temp.replace(out)

    def one(self, job):
        task, config, repetition = job
        if self.stopped.is_set():
            return
        c = self.client()
        sid = None
        row = {"task": task["id"], "kind": task["kind"], "config": config,
               "repetition": repetition, "passed": False}
        start = time.monotonic()
        try:
            with tempfile.TemporaryDirectory(prefix="shuffle-effort-") as directory:
                path = Path(directory) / "repo"
                prepare(task, path)
                overrides = {"model": self.model, "permission_mode": "bypassPermissions"}
                if config != "default":
                    overrides["effort"] = config
                prompt = (f"Live test effort benchmark. Work ONLY in this disposable repository: {path}. "
                          "Perform this task inline; do not delegate, commit, push, create a PR, or touch "
                          "other repositories, settings or credentials. Do not save memories or skills. "
                          + task["prompt"] + " Finish with a concise result.")
                request_id = uuid.uuid4().hex
                frame = {"type": "query", "prompt": prompt, "request_id": request_id,
                         "profile": self.args.agent, "overrides": overrides}
                created = c._exchange(frame, lambda r: r.get("type") == "session_created"
                                      and r.get("request_id") == request_id, wait_s=30, linger_s=0)
                if not created:
                    raise ClientError("new session was not acknowledged")
                sid = created["session_id"]
                with self.lock:
                    self.sessions.add(sid)
                    self.write_journal()
                deadline = time.monotonic() + self.args.timeout
                while not self.stopped.is_set() and time.monotonic() < deadline:
                    s = c.session(sid)
                    messages = s.get("messages") or []
                    replied = any(m.get("role") == "assistant" and m.get("content")
                                  and m.get("type") in ("text", None) for m in messages)
                    busy = c.state(sid).get("is_streaming") or s.get("has_running_tasks")
                    if replied and not busy:
                        row["wall_seconds"] = time.monotonic() - start
                        row["passed"] = check_task(task, path)
                        row.update(read_metrics(c, sid))
                        # Persist the session's observed effort, not only our requested label.
                        row["observed_effort"] = s.get("effort")
                        if config != "default" and row["observed_effort"] != config:
                            row["passed"] = False
                            row["error"] = "effort_readback_mismatch"
                        break
                    time.sleep(3)
                else:
                    row["error"] = "timeout_or_interrupted"
                    c.stop(sid)
        except Exception as exc:
            # Deliberately keep exception messages and responses out of public results.
            row["error"] = type(exc).__name__
        finally:
            row.setdefault("wall_seconds", time.monotonic() - start)
            if sid:
                try:
                    c.stop(sid)
                    c.archive(sid)
                    # Verify the write before claiming cleanup.
                    if c.session(sid).get("status") != "archived":
                        raise RuntimeError("archive not confirmed")
                    with self.lock:
                        self.sessions.discard(sid)
                        self.write_journal()
                except Exception:
                    row["cleanup_failed"] = True
            c.close()
            with self.lock:
                self.rows.append(row)
                self.persist()
                print(task["id"], config, repetition, "PASS" if row["passed"] else "FAIL",
                      row.get("error", ""), round(row["wall_seconds"], 1), flush=True)

    def cleanup(self):
        c = self.client()
        try:
            for sid in list(self.sessions):
                c.stop(sid)
                c.archive(sid)
                if c.session(sid).get("status") != "archived":
                    raise RuntimeError("archive not confirmed")
                self.sessions.discard(sid)
                self.write_journal()
        finally:
            c.close()

    def run(self):
        self.preflight()
        tasks = [t for t in load_tasks() if not self.args.task or t["id"] in self.args.task]
        if not tasks:
            raise ValueError("no tasks selected")
        if self.args.resume and Path(self.args.output).exists():
            previous = json.loads(Path(self.args.output).read_text())
            if (previous["task_sha256"] != self.task_hash or previous["worker_revision"] != self.revision
                    or previous["model"] != MODEL or previous["concurrency"] != self.args.jobs):
                raise ValueError("resume requires identical tasks, worker revision, model and concurrency")
            self.rows = previous["rows"]
        if self.journal.exists():
            self.sessions.update(json.loads(self.journal.read_text()))
            self.cleanup()
        completed = {(r["task"], r["config"], r["repetition"]) for r in self.rows}
        jobs = [(t, c, r) for t in tasks for c in self.args.configs
                for r in range(self.args.repetitions)
                if (t["id"], c, r) not in completed]
        random.Random(self.args.seed).shuffle(jobs)
        def stop(*_):
            self.stopped.set()
        old = {s: signal.signal(s, stop) for s in (signal.SIGINT, signal.SIGTERM)}
        timer = threading.Timer(self.args.max_seconds, self.stopped.set)
        timer.daemon = True
        timer.start()
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.args.jobs) as pool:
                list(pool.map(self.one, jobs))
        finally:
            timer.cancel()
            self.cleanup()
            for s, handler in old.items():
                signal.signal(s, handler)
        return 1 if self.sessions or self.stopped.is_set() else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", required=True)
    parser.add_argument("--configs", nargs="+", default=["default", "low", "medium", "high", "xhigh"])
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--seed", type=int, default=95)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--max-seconds", type=int, default=2400,
                        help="Stop and clean up before Monitor's deadline; continue with --resume")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--task", action="append")
    parser.add_argument("--agent", default="coding")
    args = parser.parse_args()
    if args.repetitions < 1 or not 1 <= args.jobs <= 4:
        parser.error("positive repetitions and 1-4 jobs required")
    return Benchmark(args).run()


if __name__ == "__main__":
    raise SystemExit(main())
