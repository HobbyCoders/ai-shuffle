"""Release gate: does every covered worker have a passing smoke for the candidate?

Stdlib-only; ``scripts/ci/ship_release_status.py`` and ``python -m
scripts.live_smoke check`` both call :func:`evaluate`.

Per worker the gate blocks on: no result file for the candidate revision, a
file for another revision, an interrupted or over-cap run, a failed row, a
row of the current scenarios missing from the file, a script row not run, or
a browser row not recorded (unless the row is optional). Owner rows never
block; they are listed as owner to-dos. ``skip_reason`` overrides every
blocker and yields the line the release notes must carry.

Rows of kind ``journey`` take no result from any worker's file: their evidence
is the UI journeys' pass in CI at the candidate (``journey_ci``, see
``scripts/live_smoke/journeys.py``). Unless that state is ``success`` the gate
blocks, naming the rows that rely on it; ``--skip-smoke-rows`` cannot excuse
them.

``skip_rows`` is the narrower policy: ``[(selector, reason)]`` where the
selector is ``two_worker`` (every row that needs the other worker) or
comma-separated row ids. A matching row that was *not run* (peer offline, an
isolated preview instance with no peer) no longer blocks, and the release
notes name the rows and the reason. A matching row that ran and failed, or
is missing from the file, still blocks.

A result file written by ``run --preview`` (an isolated preview instance of
the candidate checkout, see docs/internal/live-smoke.md) counts like a
worker's file and is named as a preview run in the release notes. It blocks
unless its preview stamp is the candidate revision and the checkout stayed
unchanged through the run.
"""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from scripts.live_smoke import results as results_mod
from scripts.live_smoke import scenarios as scen

TWO_WORKER = "two_worker"
JOURNEY = "journey"


def journey_rows(scenario_rows: Iterable[dict]) -> list:
    return [row for row in scenario_rows if row.get("kind") == JOURNEY]


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


def skip_policy(skip_rows: Iterable | None, scenario_rows: Iterable[dict]) -> tuple[list, list]:
    """``([(row_ids, selector, reason)], blockers)`` for ``--skip-smoke-rows`` entries."""
    rows = list(scenario_rows)
    known = {row["id"] for row in rows}
    policy, blockers = [], []
    for selector, reason in skip_rows or []:
        selector, reason = (selector or "").strip(), (reason or "").strip()
        if not reason:
            blockers.append(f"--skip-smoke-rows {selector!r} needs a reason for the release notes")
            continue
        if selector == TWO_WORKER:
            ids = {row["id"] for row in rows if scen.is_two_worker(row)}
        else:
            ids = {token.strip() for token in selector.split(",") if token.strip()}
            unknown = sorted(ids - known)
            if unknown or not ids:
                named = ", ".join(unknown) or repr(selector)
                blockers.append(f"--skip-smoke-rows names unknown rows: {named}")
                continue
            journeys = sorted(ids & {row["id"] for row in journey_rows(rows)})
            if journeys:
                blockers.append(f"--skip-smoke-rows cannot excuse UI journey rows ({', '.join(journeys)}); "
                                "their evidence is the journeys' CI pass at the candidate")
                continue
        policy.append((ids, selector, reason))
    return policy, blockers


def _excused(policy: list, row_id: str) -> tuple | None:
    return next(((selector, reason) for ids, selector, reason in policy if row_id in ids), None)


def evaluate_worker(base: Path, revision: str, worker: str, scenario_rows: Iterable[dict],
                    policy: list | None = None) -> dict:
    path = results_mod.result_path(base, revision, worker)
    out = {"worker": worker, "file": str(path), "verdict": "block", "blockers": [], "owner_todo": [],
           "skipped_rows": [], "preview": False}
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
    preview = data.get("preview")
    if preview:
        out["preview"] = True
        if (preview.get("commit") or "").lower() != revision.lower():
            out["blockers"].append(f"{worker}'s preview instance was stamped at "
                                   f"{str(preview.get('commit'))[:9]}, not {revision[:9]}")
        if preview.get("verified_at_end") is not True:
            out["blockers"].append(f"{worker}'s preview checkout changed during the run")
    got = {r.get("id"): r for r in data.get("rows") or []}
    for row in scenario_rows:
        if row.get("kind") == JOURNEY:
            continue  # evidence is CI, not this file (see journey_ci in evaluate)
        result = got.get(row["id"])
        blocker, todo = _row_blocker(row, result)
        excuse = _excused(policy or [], row["id"])
        if blocker and excuse and result is not None and result.get("status") == "not_run":
            out["skipped_rows"].append({"row": row["id"], "selector": excuse[0], "reason": excuse[1],
                                        "why_not_run": result.get("reason")})
            continue
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


def _skip_lines(per_worker: list) -> list:
    """One release-notes line per --skip-smoke-rows entry that excused something."""
    grouped: dict = {}
    for w in per_worker:
        for item in w["skipped_rows"]:
            key = (item["selector"], item["reason"])
            grouped.setdefault(key, {}).setdefault(item["row"], []).append(w["worker"])
    lines = []
    for (selector, reason), rows in grouped.items():
        detail = "; ".join(f"{row} on {', '.join(workers)}" for row, workers in sorted(rows.items()))
        lines.append(f"Live smoke rows not run (--skip-smoke-rows {selector}): {detail}. Reason: {reason}")
    return lines


def _journey_check(revision: str, rows: list, journey_ci: dict | None) -> tuple[dict | None, str | None]:
    """(report entry, blocker) for the rows whose evidence is the UI journeys' CI pass."""
    if not rows:
        return None, None
    ci = journey_ci or {}
    state, url = ci.get("state") or "unknown", ci.get("url")
    entry = {"state": state, "url": url, "rows": [row["id"] for row in rows]}
    if state == "success":
        return entry, None
    where = f" ({url})" if url else ""
    return entry, (f"UI journeys at {revision[:9]} are {state}{where}; {len(rows)} rows rely on them "
                   f"({', '.join(entry['rows'])}). They need a main-suite run on main at this revision "
                   "with every 'UI journeys' shard green")


def evaluate(base: Path, revision: str, workers: list | None, scenario_rows: list,
             skip_reason: str | None = None, skip_rows: list | None = None,
             journey_ci: dict | None = None) -> dict:
    """Gate verdict for ``revision``: ``pass``, ``block`` or ``skipped``.

    ``journey_ci`` is ``{"state", "url"}`` for the UI journeys in CI at
    ``revision`` (``journeys.fetch``); it is needed only when a scenario row is
    of kind ``journey``, and anything but ``success`` blocks.
    """
    revision = revision or ""
    policy, blockers = skip_policy(skip_rows, scenario_rows)
    journeys, journey_blocker = None, None
    if results_mod.REVISION.fullmatch(revision):
        workers = list(workers or []) or discover_workers(base, revision)
        per_worker = [evaluate_worker(base, revision, w, scenario_rows, policy) for w in workers]
        blockers += [b for w in per_worker for b in w["blockers"]]
        if not workers:
            blockers.append(f"no smoke result for revision {revision[:9]} in {base}")
        journeys, journey_blocker = _journey_check(revision, journey_rows(scenario_rows), journey_ci)
        if journey_blocker:
            blockers.append(journey_blocker)
    else:
        workers, per_worker = [], []
        blockers.append(f"no candidate revision to check (got {revision!r})")
    names = [f"{w['worker']} (isolated preview instance of the candidate)" if w["preview"] else w["worker"]
             for w in per_worker]
    report = {
        "revision": revision,
        "smoke_dir": str(base),
        "workers": per_worker,
        "owner_todo": [t for w in per_worker for t in w["owner_todo"]],
        "skip_reason": None,
        "skipped_rows": [{"worker": w["worker"], **item} for w in per_worker for item in w["skipped_rows"]],
        "journeys": journeys,
        "blockers": blockers,
        "verdict": "block" if blockers else "pass",
        "release_notes_lines": [] if blockers else [
            f"Live smoke passed on {', '.join(names)} at {revision[:9]}.", *_skip_lines(per_worker),
            *([(f"UI journeys passed in CI at {revision[:9]} ({journeys['url']}), standing in for "
                f"{len(journeys['rows'])} live smoke rows.")] if journeys else [])],
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
