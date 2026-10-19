"""Checkpointed full gate: bounded chunks of work, one per Monitor invocation.

Receipts are local evidence, like passes.json, not a replacement for any suite.
The backend keeps its original directory targets and collection hooks, ignoring
only the other batches. All batches must pass before the map or gate is saved.

Each invocation runs units back to back while the next one, by its last
recorded duration on this machine, still fits the time budget (``--budget``,
default 40 minutes, inside the Monitor tool's 45). The budget is enforced:
local_ci.run stops the running command and its process tree at the deadline.

Test stages (every backend batch, and fluid) run as shards: sorted file lists
kept in the state file, so a resumed run uses exactly the same partition. A
stage this machine has timed above the budget starts as several shards; a shard
that cannot finish within a whole budget is halved, keeping finished shards. A
stage completes only when its finished shards cover its files exactly once. A
unit that cannot be split (a non-test stage, or one test file) and still does
not fit fails instead of looping.

Interruption: the running unit is written to the state file before it starts
and a heartbeat file is refreshed while the invocation lives, so a run stopped
from outside (Monitor timeout, closed terminal, reboot) is found by the next
invocation and judged by how long it ran.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import signal
import tempfile
import threading
import time
import uuid
from pathlib import Path

BACKEND_BATCH_SIZE = 20
DEFAULT_BUDGET_MINUTES = 40.0
# Estimate for a stage this machine has never timed.
UNKNOWN_STAGE_SECONDS = 10 * 60
# Stages vary between runs; leave headroom over the last recorded duration.
ESTIMATE_MARGIN = 1.2
# A unit stopped after this share of a budget cannot fit in one invocation.
OVERRUN_SHARE = 0.9
# A unit that had a whole budget and was killed from outside this many times is
# treated as too long, whatever the budget (a --budget above the Monitor's).
INTERRUPTIONS_BEFORE_SPLIT = 2
HEARTBEAT_SECONDS = 30
_clock = time.monotonic
_wall = time.time
# The current unit's deadline in _clock() terms; local_ci.DEADLINE is the real one.
_deadline: float | None = None


def backend_batches(ci) -> list[list[str]]:
    files = set()
    for directory in ci.FAST_SUBSET:
        files.update(p.relative_to(ci.ROOT).as_posix()
                     for p in (ci.ROOT / directory).rglob("test_*.py"))
    files.update(p.relative_to(ci.ROOT).as_posix()
                 for p in (ci.ROOT / "tests").glob("test_*.py"))
    files.difference_update(ci.FAST_SUBSET_EXCLUDED)
    files.discard("tests/test_scan_blocking_db.py")  # backend() runs this separately
    ordered = sorted(files)
    return [ordered[i:i + BACKEND_BATCH_SIZE]
            for i in range(0, len(ordered), BACKEND_BATCH_SIZE)] or [[]]


def fluid_files(ci) -> list[str]:
    return sorted(p.relative_to(ci.ROOT).as_posix()
                  for p in (ci.ROOT / ci.FLUID_TESTS).rglob("test_*.py"))


def _is_batch(stage: str) -> bool:
    return stage.startswith("backend-") and stage[len("backend-"):].isdigit()


def stage_files(stage: str, batches: list, fluid: list) -> list[str]:
    """The test files a stage can be sharded by; empty when it cannot be split."""
    if _is_batch(stage):
        return batches[int(stage[len("backend-"):]) - 1]
    return fluid if stage == "fluid" else []


def write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique per process: the timings file is shared by this machine's worktrees.
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(state, indent=2))
        handle.flush()
        os.fsync(handle.fileno())  # a reboot right after a receipt keeps it
    for attempt in range(5):
        try:
            temporary.replace(path)
            return
        except PermissionError:  # Windows: a scanner briefly holding the target
            if attempt == 4:
                raise
            time.sleep(0.2)


def _plan(ci, args, python: str, revision: str) -> tuple[list, list, list, dict]:
    batches = backend_batches(ci)
    fluid = fluid_files(ci)
    plan = ["security", *[f"backend-{i + 1}" for i in range(len(batches))],
            *[suite.name for suite in ci.SUITES if suite.name != "backend"], "finish"]
    # Attestation is not part of the identity: a stage set completed without
    # --attest can be attested later (all stages are the same either way).
    # Shards are not either: they partition these same files.
    identity = {"revision": revision, "tree": ci.tree_of("HEAD"),
                "python": str(Path(python).absolute()), "machine": ci.machine(),
                "workers": ci.WORKERS, "strict_audit": getattr(args, "strict_audit", False),
                "plan": plan, "batches": batches, "fluid": fluid}
    return batches, fluid, plan, identity


def _state_path(ci) -> Path:
    return ci.records_path().with_name("full-stages.json")


def _beat_path(ci) -> Path:
    return ci.records_path().with_name("full-stages.beat")


def _hits_root(ci) -> Path:
    # Beside the receipts, not in the system temp directory: a reboot or a temp
    # cleaner between invocations must not lose a finished batch's map hits.
    return ci.records_path().with_name("full-stages-hits")


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def completed(ci, args, python: str, revision: str) -> bool:
    """Whether every stage already passed for this exact checkout, interpreter and machine."""
    if not ci.clean_checkout():
        return False
    _, _, plan, identity = _plan(ci, args, python, revision)
    state = _load(_state_path(ci))
    return state.get("identity") == identity and state.get("completed") == plan


def started(ci, revision: str) -> bool:
    """Whether a staged run for this exact clean checkout is already under way (or done)."""
    if not ci.clean_checkout():
        return False
    identity = _load(_state_path(ci)).get("identity") or {}
    return identity.get("revision") == revision and identity.get("tree") == ci.tree_of("HEAD")


def _times_path(ci) -> Path:
    # Beside the backend test map: shared by this machine's worktrees.
    return ci.MAP_PATH.parent / "full-stage-times.json"


def estimate(times: dict, stage: str, share: float = 1.0) -> float:
    """Seconds the stage (or ``share`` of its test files) is expected to take here."""
    if stage in times:
        return times[stage] * ESTIMATE_MARGIN * share
    if _is_batch(stage):
        batches = [v for k, v in times.items() if _is_batch(k)]
        if batches:
            return max(batches) * ESTIMATE_MARGIN * share
    return UNKNOWN_STAGE_SECONDS * share


def _timed(times: dict, stage: str) -> bool:
    """Whether estimate() rests on a duration measured here, not the default."""
    return stage in times or (_is_batch(stage) and any(_is_batch(k) for k in times))


def unit_estimate(times: dict, state: dict, stage: str, files: list[str] | None,
                  all_files: list[str]) -> tuple[float, bool]:
    """Seconds one unit is expected to take, and whether that was measured.

    A recorded duration for the stage wins; otherwise the per-file rate of the
    stage's shards already finished in this run; otherwise estimate()'s guess.
    """
    share = len(files) / len(all_files) if files else 1.0
    done = [s for s in state.get("shards", {}).get(stage, []) if s["done"]] if files else []
    if stage not in times and done:
        rate = sum(s["seconds"] for s in done) / sum(len(s["files"]) for s in done)
        return rate * len(files) * ESTIMATE_MARGIN, True
    return estimate(times, stage, share), _timed(times, stage)


def remaining() -> float | None:
    """Seconds left before the current unit is stopped, or None without a budget."""
    return None if _deadline is None else _deadline - _clock()


def _key(files: list[str]) -> str:
    return hashlib.sha1("\n".join(files).encode()).hexdigest()[:10]


def _unit_key(stage: str, files: list[str] | None) -> str:
    return f"{stage}:{_key(files)}" if files else stage


def _initial_shards(times: dict, stage: str, files: list[str], budget: float) -> list[dict]:
    count = 1
    if budget > 0 and _timed(times, stage):  # split up front on evidence, never on the guess
        count = max(1, min(len(files), math.ceil(estimate(times, stage) / budget)))
    # Round robin over the sorted list spreads a slow directory across shards.
    return [{"files": files[i::count], "done": False} for i in range(count)]


def _split(state: dict, stage: str, files: list[str]) -> tuple[int, int]:
    shards = state["shards"][stage]
    index = next(i for i, s in enumerate(shards) if s["files"] == files and not s["done"])
    middle = len(files) // 2
    shards[index:index + 1] = [{"files": files[:middle], "done": False},
                               {"files": files[middle:], "done": False}]
    state.get("interrupted", {}).pop(_unit_key(stage, files), None)
    return middle, len(files) - middle


def _label(stage: str, state: dict, files: list[str] | None) -> str:
    shards = state.get("shards", {}).get(stage) or []
    if not files or len(shards) < 2:
        return stage
    index = next(i for i, s in enumerate(shards) if s["files"] == files)
    return f"{stage} shard {index + 1}/{len(shards)} ({len(files)} test files)"


def _verify_coverage(ci, state: dict, plan: list, batches: list, fluid: list) -> None:
    """Every stage done, and every sharded stage covered by its shards exactly once."""
    if state["completed"] != plan:
        raise ci.Failure("Stage receipts do not match the plan; use full --step --restart")
    for stage in plan:
        files = stage_files(stage, batches, fluid)
        if not files:
            continue
        shards = state.get("shards", {}).get(stage) or []
        covered = [name for shard in shards if shard["done"] for name in shard["files"]]
        if not shards or not all(s["done"] for s in shards) or sorted(covered) != sorted(files):
            raise ci.Failure(f"Receipts for {stage} do not cover its {len(files)} test files exactly "
                             "once; use full --step --restart")


def _lock_path(ci) -> Path:
    return ci.records_path().with_name("full-stages.lock")


def _claim(ci, path: Path):
    """Hold this worktree's staged gate for the life of this process.

    An OS file lock, taken atomically and released by the system when the
    process ends however it ends, so a killed run never leaves a stale claim.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")  # noqa: SIM115 - held until _release
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise ci.Failure("Another full --step invocation is still running in this worktree. "
                         "Wait for it to finish, then run the same command again.") from None
    return handle


def _release(handle) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    handle.close()  # closing also drops a POSIX flock


class _Heartbeat:
    """Refreshes full-stages.beat while this invocation lives.

    It tells the next invocation how long a unit ran if this process is killed
    outright (full-stages.lock, not this file, keeps a second invocation out).
    """

    def __init__(self, path: Path):
        self.path, self.attempt = path, None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="local-ci-heartbeat", daemon=True)

    def write(self, *, released: bool = False) -> None:
        try:
            write_state(self.path, {"pid": None if released else os.getpid(),
                                    "beat": _wall(), "attempt": self.attempt})
        except OSError:
            pass  # only guides recovery

    def _run(self) -> None:
        while not self._stop.wait(HEARTBEAT_SECONDS):
            self.write()

    def start(self) -> None:
        self.write()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()
        self.attempt = None
        self.write(released=True)  # no process holds the worktree now


def _install_stop_signals() -> dict:
    """Turn termination signals into SystemExit so the stopped unit is recorded."""
    def stop(signum, frame):
        raise SystemExit(128 + signum)

    previous = {}
    for name in ("SIGTERM", "SIGHUP", "SIGBREAK"):
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            previous[number] = signal.signal(number, stop)
        except (ValueError, OSError):  # not the main thread, or unsupported here
            pass
    return previous


def _restore_signals(previous: dict) -> None:
    for number, handler in previous.items():
        try:
            signal.signal(number, handler)
        except (ValueError, OSError):
            pass


def _stopped(ci, state: dict, times: dict, stage: str, files: list[str] | None, all_files: list[str],
             ran_for: float, budget: float, first: bool, external: bool, had: float | None = None) -> str:
    """Account for a unit that did not finish: split it, or defer it to the next invocation.

    ``had`` is the time the unit had when it started (the budget left). Only a
    unit that ran first, with a whole budget, can be judged too long; any other
    is deferred, since it runs first next time. A unit that cannot be split
    raises OutOfTime: a time limit, not a test failure.
    """
    label = _label(stage, state, files)
    share = len(files) / len(all_files) if files else 1.0
    # A lower bound on the whole stage, so later estimates and fresh runs plan for it.
    times[stage] = round(max(times.get(stage, 0), ran_for / share), 1)
    key = _unit_key(stage, files)
    interrupted = state.setdefault("interrupted", {})
    if external and first:
        interrupted[key] = interrupted.get(key, 0) + 1
    had = budget if had is None else had
    overran = budget > 0 and first and (ran_for >= OVERRUN_SHARE * had
                                        or interrupted.get(key, 0) >= INTERRUPTIONS_BEFORE_SPLIT)
    if not overran:
        why = ("with no budget set" if budget <= 0 else
               "before it had a whole budget" if not first else "before it used its whole budget")
        return (f"{label} was stopped after {ran_for / 60:.1f} min, {why}; "
                "it runs first in the next invocation.")
    if not files or len(files) < 2:
        what = "it is not a test stage" if not files else f"it is one test file, {files[0]}"
        raise ci.OutOfTime(
            f"{label} needs more than a whole {budget / 60:g}-minute budget and cannot be split further "
            f"({what}). No receipt and no failure status were recorded. Run the same command once with "
            "a larger --budget that still fits the job's time limit (for example --budget 44 under a "
            "45-minute Monitor job), or with --budget 0 (this stage alone, no limit) where nothing "
            "limits the run's time; then continue with the usual command")
    halves = _split(state, stage, files)
    return (f"{label} does not fit in a {budget / 60:g}-minute budget; split it into shards of "
            f"{halves[0]} and {halves[1]} test files. Finished shards are kept.")


def _attest_completed(ci, args, state: dict, path: Path, revision: str, coverage: tuple) -> int:
    _verify_coverage(ci, state, *coverage)
    if args.attest and not state.get("attested"):
        # attest_preflight (local_ci.cmd_check) has just rechecked public main,
        # a clean tree and update reach for this revision.
        ci.attest("success", _success_description(ci, state), revision)
        state["attested"] = True
        write_state(path, state)
        print(f"COMPLETE: posted {ci.ATTEST_CONTEXT} on {revision[:9]} from the completed stage set "
              f"({len(state['completed'])} stages); nothing was rerun.")
    else:
        print("COMPLETE: full gate already passed for this checkout. Use --restart for a fresh run.")
    for note in state["skipped"] + state["notes"]:
        print(f"Not covered here: {note}")
    return 0


def _success_description(ci, state: dict) -> str:
    shards = sum(len(s) for s in state.get("shards", {}).values())
    return (f"Full staged gate passed on {ci.machine()}: tree {state['identity']['tree'][:9]}, "
            f"{len(state['completed'])} stages, {shards} test shards")


def _run_unit(ci, args, python: str, revision: str, stage: str, files: list[str] | None,
              all_files: list[str], batches: list, state: dict) -> None:
    if stage == "security":
        ci.security_check(python, args.strict_audit)
    elif _is_batch(stage):
        selected = files if files is not None else batches[int(stage.split("-")[1]) - 1]
        root = Path(state["hits"])
        root.mkdir(parents=True, exist_ok=True)
        hits = Path(tempfile.mkdtemp(prefix=f"{stage}-", dir=root))
        try:
            ctx = ci.Context(mode="full", base=revision, files=None, python=python,
                             backend_selected=selected, map_hits=hits)
            ci.backend(ctx)
            # Prefixed by shard so recycled process IDs cannot overwrite prior
            # hits, and a rerun after a kill between move and receipt replaces
            # its own earlier copy. Unfinished attempts are deleted unmerged.
            prefix = f"hits-{stage}-{_key(selected)}-"
            for old in root.glob(prefix + "*"):
                old.unlink()
            for hit in hits.glob("hits-*.json"):
                hit.replace(root / f"{prefix}{hit.name}")
        finally:
            shutil.rmtree(hits, ignore_errors=True)
    elif files:
        suite = next(s for s in ci.SUITES if s.name == stage)
        whole = len(files) == len(all_files)  # one shard: exactly the original command
        ctx = ci.Context(mode="full", base=revision, files=None, python=python,
                         backend_selected=None if whole else files)
        suite.run(ctx)
        state["notes"] = sorted(set(state["notes"] + ctx.notes))
    elif stage != "finish":
        ran, skipped, notes = ci.run_suites("full", [stage], python)
        state["ran"].extend(ran)
        state["skipped"] = sorted(set(state["skipped"] + skipped))
        state["notes"] = sorted(set(state["notes"] + notes))
    else:
        # Recheck security and release eligibility after the last suite.
        ci.security_check(python, args.strict_audit)
        if ci.sys.version_info >= (3, 12):
            ci.MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
            ci.run([python, "scripts/ci/backend_test_map.py", "merge", "--hits", state["hits"],
                    "--out", str(ci.MAP_PATH), "--commit", revision], env=ci.python_env())
        if args.attest:
            ci.attest_update_reach(revision)
    if not ci.clean_checkout() or ci.git("rev-parse", "HEAD") != revision:
        raise ci.Failure("Checkout changed during a stage; no receipt recorded")


def _fresh_state(ci, identity: dict, old: dict) -> dict:
    root = _hits_root(ci)
    previous = old.get("hits")
    if previous and Path(previous).resolve().is_relative_to(root.resolve()):
        shutil.rmtree(previous, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    # Each fresh run has its own map hits; no stale partial map can leak in.
    return {"identity": identity, "completed": [], "ran": [], "skipped": [], "notes": [],
            "shards": {}, "interrupted": {}, "started": time.time(),
            "hits": tempfile.mkdtemp(prefix="run-", dir=root)}


def run_step(ci, args, python: str, revision: str) -> int:
    if not ci.clean_checkout():
        raise ci.Failure("full --step requires a clean checkout; commit changes first")
    batches, fluid, plan, identity = _plan(ci, args, python, revision)
    path, beat_path = _state_path(ci), _beat_path(ci)
    lock = _claim(ci, _lock_path(ci))
    try:
        return _step_claimed(ci, args, python, revision, batches, fluid, plan, identity, path, beat_path)
    finally:
        _release(lock)


def _step_claimed(ci, args, python, revision, batches, fluid, plan, identity, path, beat_path) -> int:
    beat = _load(beat_path)
    state = _load(path)
    if getattr(args, "restart", False) or state.get("identity") != identity:
        state = _fresh_state(ci, identity, state)
        write_state(path, state)
    state.setdefault("shards", {})
    state.setdefault("interrupted", {})
    if state["completed"] != plan[:len(state["completed"])]:
        raise ci.Failure("Invalid stage receipts; use full --step --restart")
    budget = getattr(args, "budget", None)
    budget = (DEFAULT_BUDGET_MINUTES if budget is None else budget) * 60
    times_path = _times_path(ci)
    times = _load(times_path)
    heartbeat = _Heartbeat(beat_path)
    heartbeat.start()
    previous = _install_stop_signals()
    try:
        return _advance(ci, args, python, revision, state, path, times, times_path, beat,
                        heartbeat, budget, (plan, batches, fluid))
    finally:
        global _deadline
        ci.DEADLINE = _deadline = None
        _restore_signals(previous)
        heartbeat.stop()


def _save_times(path: Path, times: dict) -> None:
    try:
        write_state(path, times)
    except OSError:
        pass  # timings only guide chunking


def _recover(ci, args, state: dict, times: dict, beat: dict, revision: str, all_files) -> None:
    """Account for a unit the previous invocation started but never finished."""
    attempt = state.pop("attempt", None)
    if not attempt:
        return
    ran_for = attempt.get("stopped_after")
    if ran_for is None:  # killed outright: the last heartbeat is a lower bound
        ran_for = beat["beat"] - attempt["started"] if beat.get("attempt") == attempt["id"] else 0.0
    print(f"The previous invocation was stopped during {_label(attempt['stage'], state, attempt['files'])} "
          f"after about {max(ran_for, 0) / 60:.1f} min; no receipt was recorded for it.")
    try:
        print(_stopped(ci, state, times, attempt["stage"], attempt["files"], all_files(attempt["stage"]),
                       max(ran_for, 0.0), attempt["budget"], attempt["first"], external=True,
                       had=attempt.get("available")))
    except ci.Failure as error:
        if args.attest and not isinstance(error, ci.OutOfTime):
            ci.attest("failure", f"Stage {attempt['stage']} on {ci.machine()}: {error}", revision)
        raise


def _advance(ci, args, python, revision, state, path, times, times_path, beat, heartbeat,
             budget, coverage) -> int:
    global _deadline
    plan, batches, fluid = coverage
    completed_stages = state["completed"]

    def all_files(stage):
        return stage_files(stage, batches, fluid)

    try:
        _recover(ci, args, state, times, beat, revision, all_files)
    finally:
        write_state(path, state)
        _save_times(times_path, times)
    if len(completed_stages) == len(plan):
        return _attest_completed(ci, args, state, path, revision, coverage)
    if completed_stages:
        print(f"Resuming the staged full gate: {len(completed_stages)}/{len(plan)} stages complete.")
    began = _clock()
    first, posted = True, False
    while True:
        stage = plan[len(completed_stages)]
        stage_all = all_files(stage)
        files = None
        if stage_all:
            if stage not in state["shards"]:
                state["shards"][stage] = _initial_shards(times, stage, stage_all, budget)
            shards = state["shards"][stage]
            files = next(s for s in shards if not s["done"])["files"]
            # Halve before starting what measured timings say cannot fit a whole budget.
            while first and budget > 0 and len(files) > 1:
                guess, measured = unit_estimate(times, state, stage, files, stage_all)
                if not measured or guess <= budget:
                    break
                _split(state, stage, files)
                files = next(s for s in shards if not s["done"])["files"]
        expected, _ = unit_estimate(times, state, stage, files, stage_all)
        elapsed = _clock() - began
        label = _label(stage, state, files)
        if not first and (budget <= 0 or elapsed + expected > budget):
            write_state(path, state)
            print(f"Stage passed; {len(completed_stages)}/{len(plan)} complete after {elapsed / 60:.1f} min. "
                  f"Next: {label} (about {expected / 60:.0f} min). No full pass recorded yet. "
                  "Run the same command in a new Monitor invocation to continue.")
            return 0
        print(f"Full gate stage {len(completed_stages) + 1}/{len(plan)}: {label}", flush=True)
        if args.attest and not posted:  # once per invocation, not per stage
            ci.attest("pending", f"Full gate from stage {stage} on {ci.machine()}", revision)
            posted = True
        attempt = {"id": uuid.uuid4().hex, "stage": stage, "files": files, "started": _wall(),
                   "budget": budget, "available": budget - elapsed, "first": first}
        state["attempt"] = attempt
        heartbeat.attempt = attempt["id"]
        write_state(path, state)
        heartbeat.write()
        unit_began = _clock()
        if budget > 0:
            _deadline = unit_began + budget - elapsed
            ci.DEADLINE = time.monotonic() + budget - elapsed
        try:
            _run_unit(ci, args, python, revision, stage, files, stage_all, batches, state)
            if stage == "finish":
                completed_stages.append(stage)
                _verify_coverage(ci, state, *coverage)
                ci.save_record(state["identity"]["tree"], {
                    "security_gate": ci.SECURITY_GATE_VERSION, "audit_refreshed": True, "mode": "full",
                    "suites": state["ran"], "not_covered": state["skipped"], "machine": ci.machine(),
                    "commit": revision, "at": time.time()})
                if args.attest:
                    ci.attest("success", _success_description(ci, state), revision)
                    state["attested"] = True
        except ci.OutOfTime:
            ran_for = _clock() - unit_began
            state.pop("attempt", None)
            try:
                message = _stopped(ci, state, times, stage, files, stage_all, ran_for, budget,
                                   first, external=False, had=budget - elapsed)
            except ci.Failure as error:
                if args.attest and not isinstance(error, ci.OutOfTime):  # a time limit, not a failed test
                    ci.attest("failure", f"Stage {stage} on {ci.machine()}: {error}", revision)
                raise
            finally:
                write_state(path, state)
                _save_times(times_path, times)
            print(f"\nStopped at the end of the {budget / 60:g}-minute budget. {message} "
                  f"{len(completed_stages)}/{len(plan)} stages complete; no full pass recorded yet. "
                  "Run the same command in a new Monitor invocation to continue.")
            return 0
        except ci.Failure as error:
            if stage == "finish" and completed_stages[-1:] == ["finish"]:
                completed_stages.pop()
            state.pop("attempt", None)
            write_state(path, state)
            if args.attest:
                ci.attest("failure", f"Stage {stage} failed on {ci.machine()}: {error}", revision)
            raise
        except (KeyboardInterrupt, SystemExit):
            # Ctrl-C or a termination signal: the next invocation judges this unit.
            if completed_stages[-1:] == ["finish"] and stage == "finish":
                completed_stages.pop()
            attempt["stopped_after"] = round(_clock() - unit_began, 1)
            write_state(path, state)
            raise
        finally:
            ci.DEADLINE = _deadline = None
            heartbeat.attempt = None
        seconds = round(_clock() - unit_began, 1)
        state.pop("attempt", None)
        state["interrupted"].pop(_unit_key(stage, files), None)
        if files:
            shard = next(s for s in state["shards"][stage] if s["files"] == files)
            shard.update(done=True, seconds=seconds)
            if all(s["done"] for s in state["shards"][stage]):
                covered = sorted(f for s in state["shards"][stage] for f in s["files"])
                if covered != sorted(stage_all):
                    raise ci.Failure(f"Shards of {stage} do not cover its test files exactly once; "
                                     "use full --step --restart")
                times[stage] = round(sum(s["seconds"] for s in state["shards"][stage]), 1)
                completed_stages.append(stage)
                if stage == "fluid":
                    state["ran"].append("fluid")
        else:
            times[stage] = seconds
            if stage != "finish":
                completed_stages.append(stage)
        if stage == f"backend-{len(batches)}" and completed_stages[-1:] == [stage]:
            state["ran"].append("backend")
        write_state(path, state)
        _save_times(times_path, times)
        if stage == "finish":
            print("COMPLETE: every available full-gate suite passed.")
            for note in state["skipped"] + state["notes"]:
                print(f"Not covered here: {note}")
            return 0
        first = False
