"""CI-only, bounded retry of failed journey cases on the existing dev instance.

Each attempt has its own fresh evidence directory. The runner's structured verdict
is authoritative: known-bug is accepted, not a failure. Never infer success just
from an exit code, an absent summary, or a substring matching another case.
"""
from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from pathlib import Path

# Hosted median: 11.17s/case (journey_weights.json). Three failures normally add
# ~34s, not another 22-25 minute shard. A crashed file counts as one target, but
# shares the same hard phase budget: even a whole-file retry cannot run long.
MAX_FAILURES = 3
RETRY_SECONDS = 180
STEP_SECONDS = 25 * 60
TEARDOWN_SECONDS = 180  # down: 90s + wait: 60s + kill: 10s + reader: 10s
BAD = {"fail", "crashed"}


def _tail(path: Path) -> str:
    return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-12:])


def read_attempt(name: str, directory: Path, code: int, seconds: float,
                 expected: list[str] | None = None) -> dict:
    """Read only this attempt's summary; malformed/missing evidence fails closed."""
    journey = name.removesuffix(".journey.mjs")
    log = directory / "logs" / f"{Path(name).stem}.log"
    result = {"name": name, "code": code, "seconds": seconds, "log": str(log)}
    try:
        data = json.loads((directory / "journeys" / journey / "summary.json").read_text())
        cases = data["cases"]
        if data["journey"] != journey or not isinstance(cases, list) or not cases:
            raise ValueError("wrong journey or empty summary")
        ids = [c["id"] for c in cases]
        if any(not isinstance(id, str) or not id for id in ids) or len(set(ids)) != len(ids):
            raise ValueError("missing or duplicate case IDs")
        if expected is not None and set(ids) != set(expected):
            raise ValueError(f"summary IDs differ from exact selection: {ids!r} != {expected!r}")
        for c in cases:
            if c["status"] not in {"pass", "fail", "known-bug"} or c["ok"] is not (c["status"] != "fail"):
                raise ValueError("inconsistent case verdict")
        if code not in (0, 1) or (code != 0 and all(c["ok"] for c in cases)):
            raise ValueError(f"process exited {code} despite its summary")
        result["cases"] = [{
            "id": c["id"], "status": c["status"], "ms": c.get("ms"),
            "detail": c.get("error") or " | ".join(c.get("anomalies", [])) or c.get("knownBug") or "",
            "timeline": str(directory / "journeys" / journey / f"{c['id']}.txt"),
        } for c in cases]
    except (OSError, ValueError, KeyError, TypeError) as error:
        result.update(cases=None, error=f"No usable per-case summary (exit {code}): {error}\n{_tail(log)}")
    return result


def _attempt_row(attempt: dict, case: dict | None) -> dict:
    if case is not None:
        return {**case, "log": attempt["log"]}
    return {"status": "crashed", "detail": attempt["error"], "log": attempt["log"]}


def _escape(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def retry_failed(first: list[dict], out: Path, env: dict, run, *, directory: Path,
                 timeout: float, started: float, alive) -> dict:
    """One retry phase after the whole shard. No instance is started or restarted."""
    rows = []
    for attempt in first:
        for c in attempt["cases"] or [None]:
            evidence = _attempt_row(attempt, c)
            rows.append({"journey": attempt["name"].removesuffix(".journey.mjs"),
                         "case": c["id"] if c else "(whole journey)",
                         "status": evidence["status"], "attempts": [evidence]})
    receipt = {"rows": rows, "retried": 0, "retry_seconds": 0.0, "skipped": None,
               "first_attempt_seconds": sum(a["seconds"] for a in first)}
    failures = [r for r in rows if r["status"] in BAD]
    if not failures:
        return receipt
    if len(failures) > MAX_FAILURES:
        receipt["skipped"] = f"{len(failures)} failures exceeds the small retry limit ({MAX_FAILURES})."
        return receipt
    phase_started = time.monotonic()
    deadline = min(phase_started + RETRY_SECONDS, started + STEP_SECONDS - TEARDOWN_SECONDS)
    groups = defaultdict(list)
    for row in failures:
        groups[row["journey"]].append(row)
    for journey, failed in groups.items():
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not alive():
            receipt["skipped"] = "No retry headroom before teardown, or the dev instance died."
            break
        attempt_out = out / "attempt-2"
        (attempt_out / "logs").mkdir(parents=True, exist_ok=True)
        ids = None if failed[0]["status"] == "crashed" else [r["case"] for r in failed]
        name = f"{journey}.journey.mjs"
        command = ["node", str(directory / name), *[f"--case={id}" for id in ids or []]]
        print(f"[ci] retrying {name}: {', '.join(ids) if ids else '(whole journey)'} "
              f"(budget {remaining:.0f}s)", flush=True)
        code, seconds = run(command, {**env, "JOURNEY_OUT": str(attempt_out / "journeys")},
                            attempt_out / "logs" / f"{Path(name).stem}.log", min(timeout, remaining))
        receipt["retried"] += len(failed)
        receipt["retry_seconds"] += seconds
        second = read_attempt(name, attempt_out, code, seconds, expected=ids)
        for row in failed:
            if ids is None and second["cases"]:
                status = "pass" if code == 0 and all(c["status"] not in BAD for c in second["cases"]) else "fail"
                evidence = {"status": status, "detail": "\n".join(
                    f"{c['id']}: {c['status']} {c['detail']}" for c in second["cases"]), "log": second["log"]}
            else:
                c = next((c for c in second["cases"] or [] if c["id"] == row["case"]), None)
                evidence = _attempt_row(second, c)
            row["attempts"].append(evidence)
            row["status"] = "passed-on-retry" if evidence["status"] not in BAD else evidence["status"]
            if row["status"] == "passed-on-retry":
                print("::warning title=UI journey passed on retry::" + _escape(
                    f"{journey} / {row['case']} failed attempt 1, passed on retry; inspect both timelines."),
                    flush=True)
    return receipt


def summarize(label: str, receipt: dict) -> str:
    lines = [f"### UI journeys ({label})", "",
             f"Retried {receipt['retried']} failed case(s)/crashed file(s), once; "
             f"retry time {receipt['retry_seconds']:.1f}s. Both attempts are kept separately.", ""]
    if receipt["skipped"]:
        lines += [f"**Retry skipped:** {receipt['skipped']}", ""]
    recovered = [r for r in receipt["rows"] if r["status"] == "passed-on-retry"]
    if recovered:
        lines += ["### Passed on retry (flaky journeys)", "",
                  "These are still bugs to investigate, not silent passes. Both attempts' evidence is uploaded.", ""]
        lines += [f"- `{r['journey']}` / `{r['case']}`" for r in recovered]
        lines.append("")
    lines += ["| Journey | Case | Attempt 1 | Attempt 2 | Overall |",
              "| --- | --- | --- | --- | --- |"]
    for row in receipt["rows"]:
        statuses = [a["status"] for a in row["attempts"]]
        final = "failed both attempts" if len(statuses) == 2 and row["status"] in BAD else row["status"]
        cells = [row["journey"], row["case"], statuses[0], statuses[1] if len(statuses) == 2 else "—", final]
        lines.append("| " + " | ".join(c.replace("|", "\\|").replace("\n", " ") for c in cells) + " |")
    budget = 400_000
    for row in receipt["rows"]:
        for index, attempt in enumerate(row["attempts"], 1):
            if attempt["status"] not in BAD:
                continue
            text = attempt["detail"]
            if attempt.get("timeline") and Path(attempt["timeline"]).is_file():
                text = Path(attempt["timeline"]).read_text(encoding="utf-8", errors="replace")
            text = text[:12_000]
            if len(text) > budget:
                lines += ["More failing evidence: see the uploaded artifact."]
                return "\n".join(lines) + "\n"
            budget -= len(text)
            lines += ["", f"<details><summary>{row['journey']} / {row['case']} — attempt {index} {attempt['status']}</summary>",
                      "", "```text", text.replace("```", "` ` `"), "```", "</details>"]
    return "\n".join(lines) + "\n"


def write_receipt(out: Path, receipt: dict) -> None:
    (out / "summary.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    if receipt["retried"] and os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write("retried=true\n")
