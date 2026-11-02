"""Run the UI journeys against a fresh dev instance, non-interactively (CI).

    python -m scripts.devloop.ci plan [--shards N]
    python -m scripts.devloop.ci journeys [--shard I/N] [--out DIR] [--build]
    python -m scripts.devloop.ci refresh-weights RUN_ID [RUN_ID ...]

``journeys`` starts ``python -m scripts.devloop up --ci`` as its own child (so
the instance's parent lease is this process, not a CI shell step that
exits), waits until it is ready (its ``DEVLOOP_READY`` line, then
``python -m scripts.devloop status``), runs this shard's journeys, then
``python -m scripts.devloop down``. With one shard and a
``frontend/tests/journeys/run-all.mjs`` it runs that; otherwise it runs each
``*.journey.mjs`` of the shard, every one even after a failure. Shards split
the journey files by measured hosted runtime, each file exactly once. Unknown
journeys use their case count (``--list``) times the measured per-case median.

Outputs under ``--out``: ``devloop.log`` (the instance's whole log),
``logs/<journey>.log``, and the runner's per-case text timelines (and contact
sheets) under ``journeys/``. With ``--retry-failures``, the whole shard runs
first, then up to three failed cases/crashed files retry once on that same
instance. Evidence is separated under ``attempt-1/`` and ``attempt-2/``;
``summary.json`` records both verdicts. The retry shares a 180s budget and
reserves teardown time inside the existing 25-minute CI step. Local runs do
not retry unless opted in. Failing timelines and visible pass-on-retry signals
go to the log and ``$GITHUB_STEP_SUMMARY``. Exit 1 if a failure remains or the
instance could not be used.
"""
from __future__ import annotations

import argparse
import io
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
JOURNEYS = FRONTEND / "tests" / "journeys"
RUN_ALL = "run-all.mjs"
WEIGHTS = Path(__file__).with_name("journey_weights.json")
READY_SECONDS = 420
JOURNEY_SECONDS = 1500
SUMMARY_CASE_CHARS = 12_000
SUMMARY_TOTAL_CHARS = 400_000


# ─── plan ──────────────────────────────────────────────────────────────────


def journey_files(directory: Path = JOURNEYS) -> list[Path]:
    return sorted(directory.glob("*.journey.mjs"))


def case_count(path: Path) -> int:
    """How many cases ``--list`` names (1 if it cannot say: it still runs)."""
    try:
        listed = subprocess.run(["node", str(path), "--list"], cwd=FRONTEND, capture_output=True,
                                text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return 1
    lines = [line for line in listed.stdout.splitlines() if line.strip()]
    return max(1, len(lines)) if listed.returncode == 0 else 1


def partition(weights: dict[str, float], shards: int) -> list[list[str]]:
    """Greedy longest-first split: every name in exactly one shard, deterministic."""
    if shards < 1:
        raise ValueError("shards must be at least 1")
    buckets: list[list[str]] = [[] for _ in range(shards)]
    loads = [0.0] * shards
    for name in sorted(weights, key=lambda n: (-weights[n], n)):
        index = min(range(shards), key=lambda i: (loads[i], i))
        buckets[index].append(name)
        loads[index] += weights[name]
    union = [name for bucket in buckets for name in bucket]
    if sorted(union) != sorted(weights) or len(set(union)) != len(union):
        raise AssertionError("journey shards are not an exact partition")
    return [sorted(bucket) for bucket in buckets]


def estimated_seconds(directory: Path = JOURNEYS, weights_path: Path = WEIGHTS) -> dict[str, float]:
    """Use hosted medians; only unmeasured files need Node/``--list``."""
    files = journey_files(directory)
    if not files:
        raise SystemExit(f"No *.journey.mjs under {directory}: refusing to pass with no journeys")
    data = json.loads(weights_path.read_text(encoding="utf-8"))
    measured, per_case = data["seconds"], data["seconds_per_case"]
    for value in [per_case, *measured.values()]:
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value <= 0):
            raise ValueError(f"{weights_path}: runtime weights must be positive finite seconds")
    return {path.name: measured[path.name] if path.name in measured else case_count(path) * per_case
            for path in files}


def plan(shards: int, directory: Path = JOURNEYS, weights_path: Path = WEIGHTS) -> list[list[str]]:
    weights = estimated_seconds(directory, weights_path)
    buckets = partition(weights, shards)
    lines = ["### Estimated UI journey shard loads", "",
             "Journey runtime only; excludes dev-instance setup and teardown.", "",
             "| Shard | Journeys | Estimated minutes |", "| --- | --- | --- |"]
    for index, bucket in enumerate(buckets, 1):
        minutes = sum(weights[name] for name in bucket) / 60
        # stdout is the plan's JSON API, consumed by the workflow's command substitution.
        print(f"[ci] planned shard {index}/{shards}: {minutes:.1f} estimated minutes "
              f"({len(bucket)} journeys)", file=sys.stderr, flush=True)
        lines.append(f"| {index}/{shards} | {len(bucket)} | {minutes:.1f} |")
    _write_summary("\n".join(lines) + "\n")
    return buckets


# ─── refresh hosted runtime measurements ────────────────────────────────────


def parse_journey_log(log: str) -> list[tuple[str, float, int]]:
    """One job's timestamped log -> (file, seconds, cases), excluding teardown.

    A journey's own passing footer closes its interval, including the last
    journey in a shard. Missing/failed results must not become short weights.
    """
    samples = []
    pending = None
    for line in log.splitlines():
        stamp, _, message = line.partition(" ")
        start = re.fullmatch(r"\[ci\] running (\S+\.journey\.mjs)", message)
        if start:
            if pending is not None:
                raise ValueError(f"Incomplete journey log: {pending[0]}")
            pending = (start[1], datetime.fromisoformat(stamp))
        elif pending is not None:
            name, started = pending
            footer = re.fullmatch(re.escape(name.removesuffix(".journey.mjs"))
                                  + r": (\d+)/(\d+) passed; timelines in .+", message)
            if footer:
                seconds = (datetime.fromisoformat(stamp) - started).total_seconds()
                passed, cases = int(footer[1]), int(footer[2])
                if passed != cases or cases == 0 or seconds <= 0:
                    raise ValueError(f"Not a completed passing journey: {name}")
                samples.append((name, seconds, cases))
                pending = None
    if pending is not None:
        raise ValueError(f"Incomplete journey log: {pending[0]}")
    return samples


def refresh_weights(run_ids: list[int], weights_path: Path = WEIGHTS) -> None:
    """Recompute medians from completed green runs using the GitHub CLI's auth."""
    repo = subprocess.check_output(["gh", "repo", "view", "--json", "nameWithOwner",
                                    "--jq", ".nameWithOwner"], cwd=ROOT, text=True, timeout=120).strip()
    durations = defaultdict(list)
    per_case = []
    run_ids = sorted(set(run_ids))
    for run_id in run_ids:
        state = json.loads(subprocess.check_output(
            ["gh", "run", "view", str(run_id), "--repo", repo, "--json", "status,conclusion"],
            cwd=ROOT, text=True, timeout=120))
        if state != {"status": "completed", "conclusion": "success"}:
            raise ValueError(f"Run {run_id} must be completed and green: {state}")
        logs = subprocess.check_output(
            ["gh", "api", f"repos/{repo}/actions/runs/{run_id}/logs"], cwd=ROOT, timeout=120)
        samples = []
        with zipfile.ZipFile(io.BytesIO(logs)) as archive:
            for name in sorted(archive.namelist()):
                # The archive includes both whole-job logs and copies per step.
                if "/" not in name and "UI journeys" in name and name.endswith(".txt"):
                    samples.extend(parse_journey_log(archive.read(name).decode("utf-8-sig")))
        if not samples:
            raise ValueError(f"Run {run_id} has no completed UI journey measurements")
        if len({name for name, _, _ in samples}) != len(samples):
            raise ValueError(f"Run {run_id} has duplicate journey measurements")
        for name, seconds, cases in samples:
            durations[name].append(seconds)
            per_case.append(seconds / cases)
    data = {
        "_comment": "Median hosted journey seconds, excluding instance setup/teardown. Refresh from "
                    "green main-suite/ci-frontend runs: python -m scripts.devloop.ci refresh-weights "
                    "RUN_ID [RUN_ID ...]. seconds_per_case is the median of seconds/cases per sample.",
        "source_runs": run_ids,
        "seconds_per_case": round(median(per_case), 2),
        "seconds": {name: round(median(values), 2) for name, values in sorted(durations.items())},
    }
    # Do not replace the checked-in measurements until every run was validated.
    weights_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"[ci] refreshed {len(durations)} journey weights from {len(run_ids)} green runs: {weights_path}")


def commands_for(shard_files: list[str], shards: int, directory: Path = JOURNEYS) -> list[list[str]]:
    """What a shard runs: run-all.mjs when it is the only shard, else each file."""
    if shards == 1 and (directory / RUN_ALL).is_file():
        return [["node", str(directory / RUN_ALL)]]
    return [["node", str(directory / name)] for name in shard_files]


# ─── the dev instance ──────────────────────────────────────────────────────


class DevInstance:
    """``devloop up --ci`` as a child; its output goes to ``devloop.log``."""

    def __init__(self, out: Path, ttl: int) -> None:
        from scripts.devloop.__main__ import FAILED_LINE, READY_LINE

        self.ready_line, self.failed_line = READY_LINE, FAILED_LINE
        # A private (0700) directory of our own, as `up` requires.
        self.status = Path(tempfile.mkdtemp(prefix="ai-shuffle-devloop-ci-")) / "status.json"
        self.log_path = out / "devloop.log"
        self.ready = threading.Event()
        self.failure: str | None = None
        self.process = subprocess.Popen(
            [sys.executable, "-m", "scripts.devloop", "up", "--ci", "--status", str(self.status),
             "--ttl-seconds", str(ttl)],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        self.reader = threading.Thread(target=self._read, name="devloop-log", daemon=True)
        self.reader.start()

    def _read(self) -> None:
        with self.log_path.open("w", encoding="utf-8") as log:
            for line in self.process.stdout:
                log.write(line)
                log.flush()
                if line.startswith(("[devloop]", "DEVLOOP_", "Traceback")):
                    print(line, end="", flush=True)
                if line.startswith(self.ready_line):
                    self.ready.set()
                elif line.startswith(self.failed_line):
                    self.failure = line[len(self.failed_line):].strip()

    def wait_ready(self, timeout: float) -> str | None:
        """None once ready, else why not."""
        deadline = time.monotonic() + timeout
        while not self.ready.is_set():
            if self.failure:
                return self.failure
            if self.process.poll() is not None:
                self.reader.join(5)
                return self.failure or f"devloop up exited ({self.process.returncode}) before ready"
            if time.monotonic() >= deadline:
                return f"the dev instance was not ready within {timeout:.0f}s"
            time.sleep(0.5)
        # The same check every consumer uses: a live launcher and state "ready".
        checked = subprocess.run([sys.executable, "-m", "scripts.devloop", "status", "--status",
                                  str(self.status), "--wait", "30"], cwd=ROOT,
                                 capture_output=True, text=True, timeout=60, check=False)
        if checked.returncode != 0:
            return f"devloop status refused the instance: {checked.stdout}{checked.stderr}"
        return None

    def alive(self) -> bool:
        return self.process.poll() is None

    def down(self) -> None:
        if self.alive():
            subprocess.run([sys.executable, "-m", "scripts.devloop", "down", "--status",
                            str(self.status)], cwd=ROOT, timeout=90, check=False)
        try:
            self.process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)
        self.reader.join(10)
        # The guardian notes a cut-off teardown beside the status file: keep it visible.
        note = self.status.with_name(self.status.name + ".guardian.log")
        if note.is_file():
            print("[ci] guardian: " + note.read_text(encoding="utf-8"), end="", flush=True)
        shutil.rmtree(self.status.parent, ignore_errors=True)

    def log_tail(self, lines: int = 120) -> str:
        try:
            return "".join(self.log_path.read_text(encoding="utf-8").splitlines(True)[-lines:])
        except OSError:
            return ""


# ─── running ───────────────────────────────────────────────────────────────


def run_command(command: list[str], env: dict, log_path: Path, timeout: float) -> tuple[int, float]:
    """Run one journey command, streaming its output to the log and stdout."""
    started = time.monotonic()
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command, cwd=FRONTEND, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, bufsize=1,
                                   start_new_session=True)
        timer = threading.Timer(timeout, lambda: _kill_group(process))
        timer.start()
        try:
            for line in process.stdout:
                log.write(line)
                print(line, end="", flush=True)
            code = process.wait()
        finally:
            timer.cancel()
            _kill_group(process)
    if time.monotonic() - started >= timeout:
        print(f"::error::{' '.join(command)} exceeded {timeout:.0f}s and was killed", flush=True)
        code = code or 124
    return code, time.monotonic() - started


def _kill_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def case_reports(directory: Path) -> list[dict]:
    """Every per-case text report the runner wrote: ``# id  PASS|FAIL  (ms)``."""
    reports = []
    for path in sorted(directory.rglob("*.txt")):
        text = path.read_text(encoding="utf-8", errors="replace")
        head = text.split("\n", 1)[0].split()
        if len(head) >= 3 and head[0] == "#" and head[2] in ("PASS", "FAIL"):
            reports.append({"id": head[1], "ok": head[2] == "PASS", "path": path, "text": text})
    return reports


def summarize(label: str, results: list[dict], reports: list[dict], problem: str | None) -> str:
    lines = [f"### UI journeys ({label})", ""]
    if problem:
        lines += [f"**The dev instance could not be used:** {problem}", ""]
    if results:
        lines += ["| Journey | Result | Time |", "| --- | --- | --- |"]
        for result in results:
            verdict = "pass" if result["code"] == 0 else ("not run" if result["code"] is None
                                                          else f"FAIL (exit {result['code']})")
            lines.append(f"| `{result['name']}` | {verdict} | {result['seconds']:.0f}s |")
        lines.append("")
    passed = sum(report["ok"] for report in reports)
    lines.append(f"Cases: {passed}/{len(reports)} passed.")
    budget = SUMMARY_TOTAL_CHARS
    for report in (r for r in reports if not r["ok"]):
        text = report["text"]
        if len(text) > SUMMARY_CASE_CHARS:
            text = text[:SUMMARY_CASE_CHARS] + "\n… (truncated; the full report is in the artifact)"
        if budget - len(text) < 0:
            lines.append("\nMore failing cases: see the uploaded artifact.")
            break
        budget -= len(text)
        lines += ["", f"<details><summary>FAIL {report['id']}</summary>", "", "```text",
                  text.replace("```", "'''"), "```", "</details>"]
    return "\n".join(lines) + "\n"


def journeys(args: argparse.Namespace) -> int:
    from scripts.devloop import journey_retry as retry

    started = retry.time.monotonic()
    index, shards = args.shard
    out = args.out.resolve()
    attempt_out = out
    if args.retry_failures:
        # Never consume stale summaries or overwrite earlier evidence.
        attempt_out = out / "attempt-1"
        attempt_out.mkdir(parents=True, exist_ok=False)
        if (out / "attempt-2").exists():
            raise ValueError("retry output already exists; use a fresh --out directory")
    (attempt_out / "logs").mkdir(parents=True, exist_ok=True)
    (attempt_out / "journeys").mkdir(parents=True, exist_ok=True)
    label = f"shard {index}/{shards}"
    mine = plan(shards)[index - 1]
    commands = ([["node", str(JOURNEYS / name)] for name in mine]
                if args.retry_failures else commands_for(mine, shards))
    print(f"[ci] {label}: {', '.join(' '.join(Path(part).name for part in c[1:]) for c in commands) or 'nothing'}",
          flush=True)
    if not commands:
        _write_summary(f"### UI journeys ({label})\n\nNo journey files in this shard.\n")
        return 0
    if args.build:
        build_frontend()
    instance = None
    results: list[dict] = []
    attempts: list[dict] = []
    receipt = None
    problem = None

    def stop(signum, frame):
        raise SystemExit(128 + signum)

    old = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        instance = DevInstance(out, args.ttl_seconds)
        problem = instance.wait_ready(args.ready_timeout)
        env = {**os.environ, "DEVLOOP_STATUS": str(instance.status),
               "JOURNEY_OUT": str(attempt_out / "journeys")}
        if args.retry_failures:
            env.pop("JOURNEY_ONLY", None)  # a developer filter must not narrow a CI shard
        env.setdefault("JOURNEY_SHEETS", "1")
        for command in commands:
            name = " ".join(Path(part).name for part in command[1:])
            if problem is None and not instance.alive():
                problem = "the dev instance died during the journeys"
            if problem is not None:
                results.append({"name": name, "code": None, "seconds": 0.0})
                continue
            print(f"[ci] running {name}", flush=True)
            code, seconds = run_command(command, env, attempt_out / "logs" / f"{Path(command[1]).stem}.log",
                                        args.journey_timeout)
            results.append({"name": name, "code": code, "seconds": seconds})
            if args.retry_failures:
                attempts.append(retry.read_attempt(name, attempt_out, code, seconds))
        if args.retry_failures:
            receipt = retry.retry_failed(attempts, out, env, run_command, directory=JOURNEYS,
                                         timeout=args.journey_timeout, started=started,
                                         alive=lambda: problem is None and instance.alive())
            retry.write_receipt(out, receipt)
    finally:
        if instance is not None:
            if problem is not None:
                print(f"::error::{problem}", flush=True)
                print("[ci] last lines of the dev instance log:\n" + instance.log_tail(), flush=True)
            instance.down()
        for sig, handler in old.items():
            signal.signal(sig, handler)
    if receipt is not None:
        summary = retry.summarize(label, receipt)
        if problem:
            summary += f"\n**The dev instance could not be used:** {problem}\n"
        failed = problem is not None or any(r["status"] in retry.BAD for r in receipt["rows"])
    else:
        reports = case_reports(out / "journeys")
        summary = summarize(label, results, reports, problem)
        failed = problem is not None or any(result["code"] != 0 for result in results)
    _write_summary(summary)
    print(f"[ci] {label}: {'FAILED' if failed else 'passed'}; output in {out}", flush=True)
    return 1 if failed else 0


def build_frontend() -> None:
    """Build and stamp the desktop UI when the stamp is missing or stale."""
    from scripts.devloop.__main__ import stamped_build_problem

    if stamped_build_problem() is None:
        return
    env = {**os.environ, "SHUFFLE_SKIP_MOBILE_UI": "1"}
    subprocess.run(["npm", "run", "build"], cwd=FRONTEND, env=env, check=True)
    subprocess.run([sys.executable, "-m", "app.preview", "stamp"], cwd=ROOT, check=True)


def _write_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as summary:
            summary.write(text)


def _shard(value: str) -> tuple[int, int]:
    try:
        index, total = (int(part) for part in value.split("/"))
    except ValueError:
        raise argparse.ArgumentTypeError("use I/N, e.g. 2/3") from None
    if not 1 <= index <= total:
        raise argparse.ArgumentTypeError("I/N needs 1 <= I <= N")
    return index, total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.devloop.ci", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    show = commands.add_parser("plan", help="Print the journey files of every shard as JSON")
    show.add_argument("--shards", type=int, default=1)
    refresh = commands.add_parser("refresh-weights", help="Measure journey runtimes from green hosted runs")
    refresh.add_argument("run_ids", type=int, nargs="+", metavar="RUN_ID")
    run = commands.add_parser("journeys", help="Run a shard of the journeys on a fresh instance")
    run.add_argument("--shard", type=_shard, default=(1, 1), metavar="I/N")
    run.add_argument("--out", type=Path,
                     default=Path(tempfile.gettempdir()) / "ai-shuffle-journeys-ci")
    run.add_argument("--build", action="store_true",
                     help="Build and stamp the frontend first if its stamp is missing or stale")
    run.add_argument("--ttl-seconds", type=int, default=3600)
    run.add_argument("--ready-timeout", type=float, default=READY_SECONDS)
    run.add_argument("--journey-timeout", type=float, default=JOURNEY_SECONDS)
    run.add_argument("--retry-failures", action="store_true",
                     help="CI only: retry up to three failed cases/crashed files once, in the same instance")
    args = parser.parse_args(argv)
    if args.command == "plan":
        print(json.dumps(plan(args.shards)))
        return 0
    if args.command == "refresh-weights":
        refresh_weights(args.run_ids)
        return 0
    return journeys(args)


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
