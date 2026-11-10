"""Run the UI journeys against a fresh dev instance, non-interactively (CI).

    python -m scripts.devloop.ci plan [--shards N] [--only NAMES]
    python -m scripts.devloop.ci matrix [--shards N]
    python -m scripts.devloop.ci journeys [--shard I/N] [--only NAMES] [--out DIR] [--build]
    python -m scripts.devloop.ci refresh-weights RUN_ID [RUN_ID ...] [--out PATH]

``journeys`` starts ``python -m scripts.devloop up --ci`` as its own child (so
the instance's parent lease is this process, not a CI shell step that
exits), waits until it is ready (its ``DEVLOOP_READY`` line, then
``python -m scripts.devloop status``), runs this shard's journeys, then
``python -m scripts.devloop down``. With one shard and a
``frontend/tests/journeys/run-all.mjs`` it runs that; otherwise it runs each
``*.journey.mjs`` of the shard, every one even after a failure. Shards split
the journey files by measured hosted runtime, each file exactly once. Unknown
or changed case inventories use their current case count (``--list``) times the
measured per-case median, with conservative floors (not measurements). A stale
measurement is also a lower bound, scaled by its per-case cost when cases grow.

``matrix`` is the workflow's one selection step. A pull request runs the
journeys its changed files need (``frontend/tests/journeys/select.mjs --ci``
over the merge commit's diff); every other event, and a diff or selection that
cannot be read, runs them all. It prints ``only=`` (``*`` for every journey,
else the comma-separated names, empty for none) and ``shards=`` (the shard
numbers that got a file) as step outputs; ``plan`` and ``journeys`` take that
``only`` value so each shard splits the same selection.

Outputs under ``--out``: ``devloop.log`` (the instance's whole log),
``logs/<journey>.log``, and the runner's per-case text timelines (and contact
sheets) under ``journeys/``. With ``--retry-failures``, the whole shard runs
first, then up to three failed cases/crashed files retry once on that same
instance. Evidence is separated under ``attempt-1/`` and ``attempt-2/``;
``summary.json`` records both verdicts. Exact cases retry before whole files;
a whole file needs a measured estimate that fits the remaining retry time.
The retry shares a 180s budget and reserves teardown time inside the existing
25-minute CI step. Local runs do
not retry unless opted in. Failing timelines and visible pass-on-retry signals
go to the log and ``$GITHUB_STEP_SUMMARY``. Exit 1 if a failure remains or the
instance could not be used.
"""
from __future__ import annotations

import argparse
import io
import hashlib
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
HOSTED_SHARDS = 18
EVERY_JOURNEY = "*"
SELECTOR = JOURNEYS / "select.mjs"
SUFFIX = ".journey.mjs"
JOURNEY_MAP = "frontend/tests/journeys/map.json"
# Clean shards across six main-suite runs on 2026-11-06 had 7–14s step overhead.
# Keep the existing setup, retry and teardown allowances. Planning also reserves
# the 120s error allowed by the independent hosted calibration: point estimates
# must not spend that uncertainty out of the full retry/teardown reserves.
SETUP_SECONDS = 30
CALIBRATION_ERROR_SECONDS = 120
READY_SECONDS = 420
JOURNEY_SECONDS = 1500
SUMMARY_CASE_CHARS = 12_000
SUMMARY_TOTAL_CHARS = 400_000


# ─── plan ──────────────────────────────────────────────────────────────────


def journey_files(directory: Path = JOURNEYS) -> list[Path]:
    return sorted(directory.glob("*.journey.mjs"))


def case_count(path: Path, *, strict: bool = False) -> int:
    """Count all listed cases; strict callers reject unreadable inventories."""
    try:
        listed = subprocess.run(["node", str(path), "--list", "--only="], cwd=FRONTEND,
                                capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        if strict:
            raise ValueError(f"Cannot read case count for {path.name}: --list failed") from error
        return 1
    lines = [line for line in listed.stdout.splitlines() if line.strip()]
    if listed.returncode != 0 or not lines or len(lines) != len(set(lines)):
        if strict:
            raise ValueError(f"Cannot read case count for {path.name}: --list did not return unique cases")
        return 1
    return len(lines)


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


def parse_only(value: str | None, directory: Path = JOURNEYS) -> list[str] | None:
    """An ``only`` value as journey file names; None (or ``*``) is every journey."""
    if value is None or value.strip() == EVERY_JOURNEY:
        return None
    names = [name if name.endswith(SUFFIX) else name + SUFFIX
             for name in (part.strip() for part in value.split(",")) if name]
    known = {path.name for path in journey_files(directory)}
    unknown = sorted(set(names) - known)
    if unknown:
        raise ValueError(f"--only names no journey file under {directory}: {', '.join(unknown)}")
    return sorted(set(names))


def estimated_seconds(directory: Path = JOURNEYS, weights_path: Path = WEIGHTS,
                      only: list[str] | None = None) -> dict[str, float]:
    """Use matched hosted medians; stale/unknown inventories get conservative estimates."""
    files = journey_files(directory)
    if not files:
        raise SystemExit(f"No *.journey.mjs under {directory}: refusing to pass with no journeys")
    if only is not None:
        files = [path for path in files if path.name in set(only)]
    data = json.loads(weights_path.read_text(encoding="utf-8"))
    measured, per_case = data["seconds"], data["seconds_per_case"]
    for value in [per_case, *measured.values()]:
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value <= 0):
            raise ValueError(f"{weights_path}: runtime weights must be positive finite seconds")
    inventory = data.get("case_counts", {})
    if (not isinstance(inventory, dict)
            or any(type(cases) is not int or cases <= 0 for cases in inventory.values())):
        raise ValueError(f"{weights_path}: inventory must contain positive integer case counts")
    weights = {}
    for path in files:
        name, cases = path.name, case_count(path, strict=True)
        recorded_cases = inventory.get(name)
        if name in measured and cases == recorded_cases:
            weights[name] = measured[name]
            continue
        fallback = cases * per_case
        if name in measured:
            # Never make a grown slow file cheaper by using the global median.
            # Nor assume removed cases were the expensive ones. Keep the old
            # evidence intact: this is an estimate until a matched green refresh.
            growth = max(1, cases / recorded_cases) if recorded_cases else 1
            fallback = max(fallback, measured[name] * growth)
            print(f"[ci] case-count fallback for {name} ({recorded_cases} -> {cases}): "
                  f"{fallback:.2f}s; historical measurement is stale", file=sys.stderr)
        weights[name] = fallback
    return weights


def shard_budget_seconds(runtime: float) -> float:
    """Full first pass, the existing setup allowance, retry phase and safe teardown."""
    from scripts.devloop import journey_retry as retry

    if runtime == 0:  # The workflow does not start an instance for empty shards.
        return 0
    return runtime + SETUP_SECONDS + retry.RETRY_SECONDS + retry.TEARDOWN_SECONDS


def planning_budget_seconds(runtime: float) -> float:
    """Keep the calibration's allowed error outside the full operational reserves."""
    return shard_budget_seconds(runtime) + (CALIBRATION_ERROR_SECONDS if runtime else 0)


def plan(shards: int, directory: Path = JOURNEYS, weights_path: Path = WEIGHTS,
         only: list[str] | None = None) -> list[list[str]]:
    """Split the journeys (``only`` these file names, when given) across the shards."""
    # Two-argument call when unfiltered: tests replace estimated_seconds with that shape.
    weights = (estimated_seconds(directory, weights_path) if only is None
               else estimated_seconds(directory, weights_path, only))
    buckets = partition(weights, shards)
    from scripts.devloop import journey_retry as retry

    lines = ["### Estimated UI journey shard loads", "",
             f"Step estimate includes {SETUP_SECONDS}s setup/normal teardown. "
             f"Retry budget also reserves the full {retry.RETRY_SECONDS}s retry phase "
             f"and {retry.TEARDOWN_SECONDS}s teardown, plus {CALIBRATION_ERROR_SECONDS}s "
             "for the calibration's allowed estimate error. "
             "The step deadline stays 25 minutes.", "",
             "| Shard | Journeys | Journey minutes | Step minutes | With retry/reserves | Headroom seconds |",
             "| --- | --- | --- | --- | --- | --- |"]
    for index, bucket in enumerate(buckets, 1):
        runtime = sum(weights[name] for name in bucket)
        step = runtime + SETUP_SECONDS if bucket else 0
        budget = planning_budget_seconds(runtime)
        headroom = retry.STEP_SECONDS - budget
        # stdout is the plan's JSON API, consumed by the workflow's command substitution.
        print(f"[ci] planned shard {index}/{shards}: {step / 60:.1f} estimated step minutes "
              f"({len(bucket)} journeys, {budget / 60:.1f}m with retry/reserves, "
              f"{headroom:.0f}s headroom)", file=sys.stderr, flush=True)
        lines.append(f"| {index}/{shards} | {len(bucket)} | {runtime / 60:.1f} | {step / 60:.1f} | "
                     f"{budget / 60:.1f} | {headroom:.0f} |")
    _write_summary("\n".join(lines) + "\n")
    return buckets


# ─── which journeys a run needs ────────────────────────────────────────────


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                          timeout=60, check=True).stdout


def files_since(base: str, root: Path = ROOT) -> list[str] | None:
    """Files that differ between ``base`` and HEAD (a rename lists both paths); None if unreadable."""
    try:
        listed = _git(root, "diff", "--name-only", "--no-renames", "-z", base, "HEAD")
    except (OSError, subprocess.SubprocessError):
        return None
    return sorted({name for name in listed.split("\0") if name})


def pull_request_files(root: Path = ROOT) -> list[str] | None:
    """Files a pull request changes, read from its merge commit; None if unreadable.

    ``pull_request`` runs check out GitHub's merge of the branch into its base:
    the first parent is the base, so that diff is exactly what merging adds.
    Needs the parents fetched (``fetch-depth: 2``).
    """
    try:
        if len(_git(root, "rev-list", "--parents", "-n", "1", "HEAD").split()) != 3:
            return None  # not a two-parent merge commit
    except (OSError, subprocess.SubprocessError):
        return None
    return files_since("HEAD^1", root)


def base_journey_map(root: Path = ROOT, base: str = "HEAD^1") -> str | None:
    """``base``'s map.json (by default the merge commit's first parent); None if unreadable."""
    try:
        text = _git(root, "show", f"{base}:{JOURNEY_MAP}")
        json.loads(text)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return text


def last_green_main(root: Path = ROOT) -> str | None:
    """The newest ancestor of HEAD that main-suite passed on; None if unknown.

    Every journey held there: by running, or because that run narrowed the same
    way from an older green commit. Needs main's history fetched.
    """
    from scripts.ci import proven

    where = proven.environment()
    if where is None:
        return None
    try:
        head = _git(root, "rev-parse", "HEAD").strip()
        for commit in proven.green_main_commits(*where):
            if commit != head and subprocess.run(["git", "merge-base", "--is-ancestor", commit, "HEAD"],
                                                 cwd=root, capture_output=True, timeout=60).returncode == 0:
                return commit
    except Exception as error:  # noqa: BLE001 - not knowing means running every journey
        print(f"[ci] last green main commit unknown: {error}", file=sys.stderr)
    return None


def select_journeys(files: list[str], selector: Path = SELECTOR, directory: Path = JOURNEYS,
                    previous_map: str | None = None) -> list[str] | None:
    """The journey files those changed files need; None for all, or when unsure.

    ``previous_map`` is the base's map.json text. With it an edited map selects
    what either map names for the other files; without it an edit runs them all.
    """
    with tempfile.TemporaryDirectory() as temporary:
        listing = Path(temporary) / "changed-files.txt"
        listing.write_text("\n".join(files) + "\n", encoding="utf-8")
        command = ["node", str(selector), "--ci", "--json", "--files-from", str(listing)]
        if previous_map is not None:
            previous = Path(temporary) / "base-map.json"
            previous.write_text(previous_map, encoding="utf-8")
            command += ["--previous-map", str(previous)]
        try:
            done = subprocess.run(command,
                                  cwd=ROOT, capture_output=True, text=True, timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None
    if done.returncode != 0:
        return None
    try:
        data = json.loads(done.stdout)
        if data["all"] is not False:
            return None
        return parse_only(",".join(data["journeys"]) if data["journeys"] else "", directory)
    except (ValueError, KeyError, TypeError):
        return None


def selection_for_event(event: str | None, root: Path = ROOT) -> tuple[list[str] | None, str]:
    """(journey files or None for all, why) for a workflow event.

    A pull request runs what its merge changes. A push to main runs what
    changed since the last commit main-suite passed on. Both narrow only when
    the diff and the selection are readable; schedules, dispatches and other
    callers run all.
    """
    if event == "pull_request":
        base, files, what = "HEAD^1", pull_request_files(root), "the pull request's merge diff"
    elif event == "push":
        base = last_green_main(root)
        if base is None:
            return None, "every journey: no earlier commit that main-suite passed on was found"
        files, what = files_since(base, root), f"the changes since {base[:12]}, which main-suite passed on"
    else:
        return None, f"every journey: a {event or 'local'} run is not narrowed"
    if files is None:
        return None, f"every journey: {what} could not be read"
    only = select_journeys(files, previous_map=base_journey_map(root, base) if JOURNEY_MAP in files else None)
    if only is None:
        return None, f"every journey: {len(files)} changed files need the whole suite, or selection failed"
    return only, f"{len(only)} of the journeys cover the {len(files)} files in {what}"


def selection_key(only: list[str] | None) -> str:
    """Names a selection in a pass record: which journeys a run proved."""
    if only is None:
        return "all"
    return hashlib.sha256("\n".join(sorted(only)).encode()).hexdigest()[:16]


def already_proven(key: str, root: Path = ROOT) -> bool:
    """True on a push to main when a pull request passed this selection on these exact files."""
    from scripts.ci import proven

    where = proven.environment()
    if where is None:
        return False
    try:
        tree = proven.head_tree(root)
        return any(proven.exists(proven.record_name("journeys", tree, name), *where, area="journeys", tree=tree) for name in dict.fromkeys((key, "all")))
    except Exception as error:  # noqa: BLE001 - a failed lookup means running the journeys
        print(f"[ci] pass-record lookup failed: {error}", file=sys.stderr)
        return False


def matrix(shards: int) -> int:
    only, why = selection_for_event(os.environ.get("GITHUB_EVENT_NAME"))
    key = selection_key(only)
    buckets = plan(shards) if only is None else plan(shards, only=only)
    numbers = [index for index, bucket in enumerate(buckets, 1) if bucket]
    if numbers and already_proven(key):
        numbers, why = [], f"none: a pull request already passed them on these exact files ({why})"
    print(f"[ci] UI journeys: {why}", file=sys.stderr, flush=True)
    _write_summary(f"\n**UI journeys for this run:** {why}.\n")
    # stdout is the step's output file: exactly these three lines.
    print(f"only={EVERY_JOURNEY if only is None else ','.join(name.removesuffix(SUFFIX) for name in only)}")
    print(f"shards={json.dumps(numbers, separators=(',', ':'))}")
    print(f"key={key}")
    return 0


# ─── refresh hosted runtime measurements ────────────────────────────────────


def parse_journey_log(log: str) -> list[tuple[str, float, int]]:
    """One job's timestamped log -> (file, seconds, cases), excluding teardown.

    A journey's own completed footer closes its interval, including the last
    journey in a shard. Known bugs count as completed cases, as in the runner;
    missing/failed results must not become short weights.
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
                                  + r": (\d+)/(\d+) passed(?:, (\d+) known bug)?; timelines in .+", message)
            if footer:
                seconds = (datetime.fromisoformat(stamp) - started).total_seconds()
                passed, cases = int(footer[1]), int(footer[2])
                known_bugs = int(footer[3] or 0)
                if passed + known_bugs != cases or cases == 0 or seconds <= 0:
                    raise ValueError(f"Not a completed passing journey: {name}")
                samples.append((name, seconds, cases))
                pending = None
    if pending is not None:
        raise ValueError(f"Incomplete journey log: {pending[0]}")
    return samples


RETRY_MARKER = re.compile(r"\[ci\] retrying \S+\.journey\.mjs:")


def _run_samples(run_id: int, repo: str) -> tuple[str, list[tuple[str, float, int]]]:
    """(revision, first-pass samples) of one clean green main-suite run.

    The run itself must qualify, or nothing is refreshed. A job log with a
    retry or an unfinished journey is skipped whole: a retry's footer must never
    pass for the first attempt's runtime.
    """
    state = json.loads(subprocess.check_output(
        ["gh", "run", "view", str(run_id), "--repo", repo, "--json",
         "status,conclusion,attempt,headSha,event,headBranch,workflowName"],
        cwd=ROOT, text=True, timeout=120))
    if state.get("status") != "completed" or state.get("conclusion") != "success":
        raise ValueError(f"Run {run_id} must be completed and green: {state}")
    if state.get("attempt") != 1:
        raise ValueError(f"Run {run_id} cannot be used for runtime weights: "
                         f"attempt {state.get('attempt')} is not 1; re-run logs omit earlier jobs. "
                         "Choose a clean first-attempt run.")
    # main-suite push jobs check out headSha. PR runs check out a merge revision
    # the run's headSha does not name, so their source is not recorded.
    if (state.get("event"), state.get("headBranch"), state.get("workflowName")) != (
            "push", "main", "main-suite"):
        raise ValueError(f"Run {run_id} must be a push to main from main-suite; "
                         "PR/other runs do not establish the checkout revision. "
                         "No weights were replaced.")
    revision = state.get("headSha")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError(f"Run {run_id} has no valid source revision")
    logs = subprocess.check_output(
        ["gh", "api", f"repos/{repo}/actions/runs/{run_id}/logs"], cwd=ROOT, timeout=120)
    with zipfile.ZipFile(io.BytesIO(logs)) as archive:
        # The archive includes both whole-job logs and copies per step.
        job_logs = [(name, archive.read(name).decode("utf-8-sig")) for name in sorted(archive.namelist())
                    if "/" not in name and "UI journeys" in name and name.endswith(".txt")]
    samples = []
    for name, log in job_logs:
        if RETRY_MARKER.search(log):
            print(f"[ci] run {run_id}: skipping {name}: it retried a journey", file=sys.stderr)
            continue
        try:
            samples.extend(parse_journey_log(log))
        except ValueError as error:
            print(f"[ci] run {run_id}: skipping {name}: {error}", file=sys.stderr)
    measured = [name for name, _, _ in samples]
    if len(set(measured)) != len(measured):
        raise ValueError(f"Run {run_id} has duplicate journey measurements")
    return revision, samples


def refresh_weights(run_ids: list[int], weights_path: Path = WEIGHTS, out: Path | None = None) -> None:
    """Median hosted seconds per journey file from clean green first-attempt main-suite runs.

    A sample counts only when its case count matches the file's current
    ``--list`` count. A run that lacks a file, or measured it with other cases,
    adds nothing for that file. A file with no usable sample keeps its existing
    entry and the case count it was measured at, so planning still treats a
    changed file as stale. Files no longer in the checkout are dropped. Writes
    ``out`` (default: ``weights_path``) only after every run has been read.
    """
    out = weights_path if out is None else out
    expected_cases = {path.name: case_count(path, strict=True) for path in journey_files(JOURNEYS)}
    repo = subprocess.check_output(["gh", "repo", "view", "--json", "nameWithOwner",
                                    "--jq", ".nameWithOwner"], cwd=ROOT, text=True, timeout=120).strip()
    durations = defaultdict(list)
    per_case = []
    revisions = {}
    for run_id in sorted(set(run_ids)):
        revision, samples = _run_samples(run_id, repo)
        for name, seconds, cases in samples:
            if name not in expected_cases:
                print(f"[ci] run {run_id}: {name} is no longer a journey file", file=sys.stderr)
            elif cases != expected_cases[name]:
                print(f"[ci] run {run_id}: {name} ran {cases} cases, the file now has "
                      f"{expected_cases[name]}; sample not used", file=sys.stderr)
            else:
                durations[name].append(seconds)
                per_case.append(seconds / cases)
                revisions[str(run_id)] = revision
    if not durations:
        raise ValueError("No usable UI journey measurements in those runs; no weights were replaced")
    existing = json.loads(weights_path.read_text(encoding="utf-8")) if weights_path.is_file() else {}
    old_seconds, old_counts = existing.get("seconds", {}), existing.get("case_counts", {})
    kept = sorted(name for name in expected_cases
                  if name not in durations and name in old_seconds and name in old_counts)
    for name in sorted(set(expected_cases) - set(durations)):
        print(f"[ci] no usable sample for {name}: "
              + ("kept its existing weight" if name in kept else "it keeps the per-case fallback"),
              file=sys.stderr)
    sources = dict(revisions)
    if kept:  # Kept values still come from the runs that measured them.
        sources = {**existing.get("source_revisions", {}), **sources}
    seconds = {name: round(median(values), 2) for name, values in durations.items()}
    seconds.update({name: old_seconds[name] for name in kept})
    counts = {name: expected_cases[name] for name in durations}
    counts.update({name: old_counts[name] for name in kept})
    data = {
        "_comment": "Median hosted journey seconds, excluding instance setup/teardown. Refresh from "
                    "green main-suite push-to-main runs: python -m scripts.devloop.ci refresh-weights "
                    "RUN_ID [RUN_ID ...] [--out PATH]. Only samples whose case count matches the file "
                    "count; a file with no such sample keeps its entry. seconds_per_case is the median "
                    "of seconds/cases per sample.",
        "source_runs": sorted(int(run_id) for run_id in sources),
        "source_revisions": dict(sorted(sources.items(), key=lambda item: int(item[0]))),
        "case_counts": dict(sorted(counts.items())),
        "seconds_per_case": round(median(per_case), 2),
        "seconds": dict(sorted(seconds.items())),
    }
    out.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"[ci] refreshed {len(durations)} journey weights from {len(revisions)} green runs "
          f"({len(kept)} kept): {out}")


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
    only = parse_only(args.only)
    mine = (plan(shards) if only is None else plan(shards, only=only))[index - 1]
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
                                         alive=lambda: problem is None and instance.alive(),
                                         weights_path=WEIGHTS)
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
    only_help = f"Comma-separated journey names to split instead of all ({EVERY_JOURNEY} = all)"
    show.add_argument("--only", help=only_help)
    choose = commands.add_parser("matrix", help="Print this workflow event's journey selection and shards")
    choose.add_argument("--shards", type=int, default=HOSTED_SHARDS)
    refresh = commands.add_parser("refresh-weights", help="Measure journey runtimes from green hosted runs")
    refresh.add_argument("run_ids", type=int, nargs="+", metavar="RUN_ID")
    refresh.add_argument("--out", type=Path, default=None,
                         help=f"Write the refreshed weights here instead of {WEIGHTS.name}")
    run = commands.add_parser("journeys", help="Run a shard of the journeys on a fresh instance")
    run.add_argument("--shard", type=_shard, default=(1, 1), metavar="I/N")
    run.add_argument("--only", help=only_help)
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
        only = parse_only(args.only)
        print(json.dumps(plan(args.shards) if only is None else plan(args.shards, only=only)))
        return 0
    if args.command == "matrix":
        return matrix(args.shards)
    if args.command == "refresh-weights":
        refresh_weights(args.run_ids, out=args.out)
        return 0
    return journeys(args)


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
