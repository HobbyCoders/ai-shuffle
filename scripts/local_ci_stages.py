"""Checkpointed full gate: one bounded unit of work per Monitor invocation.

Receipts are local evidence, like passes.json, not a replacement for any suite.
The backend keeps its original directory targets and collection hooks, ignoring
only the other batches. All batches must pass before the map or gate is saved.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

BACKEND_BATCH_SIZE = 20


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


def run_step(ci, args, python: str, revision: str) -> int:
    if not ci.clean_checkout():
        raise ci.Failure("full --step requires a clean checkout; commit changes first")
    if args.attest:
        base = ci.resolve_base(public_only=True)
        ci.fetch_base(base, strict=True)
        if revision != ci.git("rev-parse", base):
            raise ci.Failure("--attest requires the current public main")
        ci.attest_update_reach(revision)
    batches = backend_batches(ci)
    plan = ["security", *[f"backend-{i + 1}" for i in range(len(batches))],
            *[suite.name for suite in ci.SUITES if suite.name != "backend"], "finish"]
    identity = {"revision": revision, "tree": ci.tree_of("HEAD"),
                "python": str(Path(python).absolute()), "machine": ci.machine(),
                "workers": ci.WORKERS, "strict_audit": args.strict_audit,
                "attest": args.attest, "plan": plan, "batches": batches}
    path = ci.records_path().with_name("full-stages.json")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if args.restart or state.get("identity") != identity:
        state = {"identity": identity, "completed": [], "ran": [], "skipped": [],
                 "notes": [], "started": time.time()}
        # Each fresh run has its own map hits; no stale partial map can leak in.
        import tempfile
        state["hits"] = tempfile.mkdtemp(prefix="local-ci-stages-map-")
        write_state(path, state)
    completed = state["completed"]
    if completed != plan[:len(completed)]:
        raise ci.Failure("Invalid stage receipts; use full --step --restart")
    if len(completed) == len(plan):
        print("COMPLETE: full gate already passed for this checkout. Use --restart for a fresh run.")
        return 0
    stage = plan[len(completed)]
    print(f"Full gate stage {len(completed) + 1}/{len(plan)}: {stage}", flush=True)
    if args.attest:
        ci.attest("pending", f"Full gate stage {stage} on {ci.machine()}", revision)
    try:
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
        if stage == "finish":
            ci.save_record(identity["tree"], {"security_gate": ci.SECURITY_GATE_VERSION,
                           "audit_refreshed": True, "mode": "full", "suites": state["ran"],
                           "not_covered": state["skipped"], "machine": ci.machine(),
                           "commit": revision, "at": time.time()})
            if args.attest:
                ci.attest("success", f"Full staged gate passed on {ci.machine()}", revision)
        completed.append(stage)
        write_state(path, state)
    except ci.Failure as error:
        if args.attest:
            ci.attest("failure", f"Stage {stage} failed on {ci.machine()}: {error}", revision)
        raise
    if stage == "finish":
        print("COMPLETE: every available full-gate suite passed.")
        for note in state["skipped"] + state["notes"]:
            print(f"Not covered here: {note}")
    else:
        print(f"Stage passed; {len(completed)}/{len(plan)} complete. No full pass recorded yet. "
              "Run the same command in a new Monitor invocation to continue.")
    return 0
