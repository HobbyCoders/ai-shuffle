"""Release gate: does every covered worker have a passing smoke for the candidate?

Stdlib-only; ``scripts/ci/ship_release_status.py`` and ``python -m
scripts.live_smoke check`` both call :func:`evaluate`.

Per worker the gate blocks on: no result file for the candidate revision, a
file for another revision, an interrupted or over-cap run, a failed row, a
row of the current scenarios missing from the file, a script row not run, or
a browser row not recorded (unless the row is optional). Owner rows never
block; they are listed as owner to-dos. ``skip_reason`` overrides every
blocker and yields the line the release notes must carry.
"""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from scripts.live_smoke import results as results_mod


def _row_blocker(row: dict, got: dict | None) -> tuple[str | None, str | None]:
    """(blocker, owner_todo) for one scenario row."""
    rid, kind = row["id"], row.get("kind")
    if got is None:
        return f"{rid} missing from the result (scenarios changed since the run)", None
    status, reason = got.get("status"), got.get("reason") or ""
    if status == "pass":
        return None, None
    if status == "fail":
        return f"{rid} failed: {reason}".rstrip(": "), None
    if kind == "owner":
        return None, f"{rid} ({row.get('check')}) needs the owner"
    if kind == "browser" and row.get("optional"):
        return None, None
    if kind == "browser":
        return f"{rid} not recorded ({reason or 'needs browser'}); record it with `python -m scripts.live_smoke record`", None
    return f"{rid} not run: {reason}".rstrip(": "), None


def evaluate_worker(base: Path, revision: str, worker: str, scenario_rows: Iterable[dict]) -> dict:
    path = results_mod.result_path(base, revision, worker)
    out = {"worker": worker, "file": str(path), "verdict": "block", "blockers": [], "owner_todo": []}
    data = results_mod.read(path)
    if data is None:
        out["blockers"].append(f"no smoke result for {worker} at {revision[:9]} ({path})")
        return out
    if (data.get("revision") or "").lower() != revision.lower():
        out["blockers"].append(f"{worker}'s result is for revision {str(data.get('revision'))[:9]}, not {revision[:9]}")
        return out
    if data.get("outcome") == "interrupted":
        out["blockers"].append(f"{worker}'s run was interrupted")
    if data.get("cap_exceeded"):
        out["blockers"].append(f"{worker}'s run hit the token cap ({data.get('token_cap')})")
    got = {r.get("id"): r for r in data.get("rows") or []}
    for row in scenario_rows:
        blocker, todo = _row_blocker(row, got.get(row["id"]))
        if blocker:
            out["blockers"].append(f"{worker}: {blocker}")
        if todo:
            out["owner_todo"].append(f"{worker}: {todo}")
    out["verdict"] = "block" if out["blockers"] else "pass"
    return out


def discover_workers(base: Path, revision: str) -> list:
    folder = base / revision.lower()
    if not folder.is_dir():
        return []
    return sorted(
        (results_mod.read(p) or {}).get("worker") or p.stem
        for p in folder.glob("*.json") if "-partial-" not in p.name
    )


def evaluate(base: Path, revision: str, workers: list | None, scenario_rows: list,
             skip_reason: str | None = None) -> dict:
    """Gate verdict for ``revision``: ``pass``, ``block`` or ``skipped``."""
    revision = revision or ""
    if results_mod.REVISION.fullmatch(revision):
        workers = list(workers or []) or discover_workers(base, revision)
        per_worker = [evaluate_worker(base, revision, w, scenario_rows) for w in workers]
        blockers = [b for w in per_worker for b in w["blockers"]]
        if not workers:
            blockers.append(f"no smoke result for revision {revision[:9]} in {base}")
    else:
        workers, per_worker = [], []
        blockers = [f"no candidate revision to check (got {revision!r})"]
    report = {
        "revision": revision,
        "smoke_dir": str(base),
        "workers": per_worker,
        "owner_todo": [t for w in per_worker for t in w["owner_todo"]],
        "skip_reason": None,
        "blockers": blockers,
        "verdict": "block" if blockers else "pass",
        "release_notes_lines": [] if blockers else [
            f"Live smoke passed on {', '.join(workers)} at {revision[:9]}."],
    }
    if skip_reason is not None:
        reason = skip_reason.strip() or "no reason given"
        report.update(
            skip_reason=reason,
            verdict="skipped",
            would_block=blockers,
            blockers=[],
            release_notes_lines=[f"Live smoke skipped (--skip-smoke): {reason}"],
        )
    return report
