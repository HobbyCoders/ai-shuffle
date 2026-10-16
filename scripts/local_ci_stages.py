"""Checkpointed full gate: bounded chunks of work, one per Monitor invocation.

Receipts are local evidence, like passes.json, not a replacement for any suite.
The backend keeps its original directory targets and collection hooks, ignoring
only the other batches. All batches must pass before the map or gate is saved.

Each invocation runs stages back to back while the next stage, by its last
recorded duration on this machine, still fits the time budget (``--budget``,
default 40 minutes, inside the Monitor tool's 45). A stage that outlives its
Monitor budget gets no receipt and runs first in the next invocation, which
records its real duration.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

BACKEND_BATCH_SIZE = 20
DEFAULT_BUDGET_MINUTES = 40.0
# Estimate for a stage this machine has never timed.
UNKNOWN_STAGE_SECONDS = 10 * 60
# Stages vary between runs; leave headroom over the last recorded duration.
ESTIMATE_MARGIN = 1.2
_clock = time.monotonic


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


def write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temporary.replace(path)


def _plan(ci, args, python: str, revision: str) -> tuple[list, list, dict]:
    batches = backend_batches(ci)
    plan = ["security", *[f"backend-{i + 1}" for i in range(len(batches))],
            *[suite.name for suite in ci.SUITES if suite.name != "backend"], "finish"]
    # Attestation is not part of the identity: a stage set completed without
    # --attest can be attested later (all stages are the same either way).
    identity = {"revision": revision, "tree": ci.tree_of("HEAD"),
                "python": str(Path(python).absolute()), "machine": ci.machine(),
                "workers": ci.WORKERS, "strict_audit": getattr(args, "strict_audit", False),
                "plan": plan, "batches": batches}
    return batches, plan, identity


def _state_path(ci) -> Path:
    return ci.records_path().with_name("full-stages.json")


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def completed(ci, args, python: str, revision: str) -> bool:
    """Whether every stage already passed for this exact checkout, interpreter and machine."""
    if not ci.clean_checkout():
        return False
    _, plan, identity = _plan(ci, args, python, revision)
    state = _load(_state_path(ci))
    return state.get("identity") == identity and state.get("completed") == plan


def _times_path(ci) -> Path:
    # Beside the backend test map: shared by this machine's worktrees.
    return ci.MAP_PATH.parent / "full-stage-times.json"


def estimate(times: dict, stage: str) -> float:
    """Seconds the stage is expected to take on this machine."""
    if stage in times:
        return times[stage] * ESTIMATE_MARGIN
    if stage.startswith("backend-") and stage[len("backend-"):].isdigit():
        batches = [v for k, v in times.items() if k.startswith("backend-") and k[len("backend-"):].isdigit()]
        if batches:
            return max(batches) * ESTIMATE_MARGIN
    return UNKNOWN_STAGE_SECONDS


def _attest_completed(ci, args, state: dict, path: Path, revision: str) -> int:
    if args.attest and not state.get("attested"):
        # attest_preflight (local_ci.cmd_check) has just rechecked public main,
        # a clean tree and update reach for this revision.
        ci.attest("success", f"Full staged gate passed on {ci.machine()}", revision)
        state["attested"] = True
        write_state(path, state)
        print(f"COMPLETE: posted {ci.ATTEST_CONTEXT} on {revision[:9]} from the completed stage set "
              f"({len(state['completed'])} stages); nothing was rerun.")
    else:
        print("COMPLETE: full gate already passed for this checkout. Use --restart for a fresh run.")
    for note in state["skipped"] + state["notes"]:
        print(f"Not covered here: {note}")
    return 0


def _run_stage(ci, args, python: str, revision: str, stage: str, batches: list, state: dict) -> None:
    if stage == "security":
        ci.security_check(python, args.strict_audit)
    elif stage.startswith("backend-") and stage != "backend-linux":
        batch = batches[int(stage.split("-")[1]) - 1]
        import tempfile
        hits = Path(tempfile.mkdtemp(prefix=f"{stage}-", dir=state["hits"]))
        ctx = ci.Context(mode="full", base=revision, files=None, python=python,
                         backend_selected=batch, map_hits=hits)
        ci.backend(ctx)
        # Prefix by stage so recycled process IDs cannot overwrite prior hits.
        # Failed/interrupted attempts stay in subdirectories and are not merged.
        for hit in hits.glob("hits-*.json"):
            hit.replace(Path(state["hits"]) / f"hits-{stage}-{hit.name}")
        if stage == f"backend-{len(batches)}":
            state["ran"].append("backend")
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


def run_step(ci, args, python: str, revision: str) -> int:
    if not ci.clean_checkout():
        raise ci.Failure("full --step requires a clean checkout; commit changes first")
    batches, plan, identity = _plan(ci, args, python, revision)
    path = _state_path(ci)
    state = _load(path)
    if getattr(args, "restart", False) or state.get("identity") != identity:
        state = {"identity": identity, "completed": [], "ran": [], "skipped": [],
                 "notes": [], "started": time.time()}
        # Each fresh run has its own map hits; no stale partial map can leak in.
        import tempfile
        state["hits"] = tempfile.mkdtemp(prefix="local-ci-stages-map-")
        write_state(path, state)
    completed_stages = state["completed"]
    if completed_stages != plan[:len(completed_stages)]:
        raise ci.Failure("Invalid stage receipts; use full --step --restart")
    if len(completed_stages) == len(plan):
        return _attest_completed(ci, args, state, path, revision)
    budget = getattr(args, "budget", None)
    budget = (DEFAULT_BUDGET_MINUTES if budget is None else budget) * 60
    times_path = _times_path(ci)
    times = _load(times_path)
    began = _clock()
    stage_began = None
    while True:
        stage = plan[len(completed_stages)]
        print(f"Full gate stage {len(completed_stages) + 1}/{len(plan)}: {stage}", flush=True)
        if args.attest and stage_began is None:  # once per invocation, not per stage
            ci.attest("pending", f"Full gate from stage {stage} on {ci.machine()}", revision)
        stage_began = _clock()
        try:
            _run_stage(ci, args, python, revision, stage, batches, state)
            if stage == "finish":
                ci.save_record(identity["tree"], {"security_gate": ci.SECURITY_GATE_VERSION,
                               "audit_refreshed": True, "mode": "full", "suites": state["ran"],
                               "not_covered": state["skipped"], "machine": ci.machine(),
                               "commit": revision, "at": time.time()})
                if args.attest:
                    ci.attest("success", f"Full staged gate passed on {ci.machine()}", revision)
                    state["attested"] = True
            completed_stages.append(stage)
            write_state(path, state)
        except ci.Failure as error:
            if args.attest:
                ci.attest("failure", f"Stage {stage} failed on {ci.machine()}: {error}", revision)
            raise
        times[stage] = round(_clock() - stage_began, 1)
        try:
            write_state(times_path, times)
        except OSError:
            pass  # timings only guide chunking
        if stage == "finish":
            print("COMPLETE: every available full-gate suite passed.")
            for note in state["skipped"] + state["notes"]:
                print(f"Not covered here: {note}")
            return 0
        upcoming = plan[len(completed_stages)]
        expected = estimate(times, upcoming)
        elapsed = _clock() - began
        if budget <= 0 or elapsed + expected > budget:
            print(f"Stage passed; {len(completed_stages)}/{len(plan)} complete after {elapsed / 60:.1f} min. "
                  f"Next: {upcoming} (about {expected / 60:.0f} min). No full pass recorded yet. "
                  "Run the same command in a new Monitor invocation to continue.")
            return 0
