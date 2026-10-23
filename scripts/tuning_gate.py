"""Offline gate: a tuning default ships only with a measured win.

A *tuning default* is a value the app picks for someone who has not chosen:
an effort a never-picked model starts at, a sampling value sent without an
explicit opt-in, and so on. Issue #95 requires each one to come with a
benchmark showing a positive outcome against current behaviour.

This script never calls a provider. It reads committed raw benchmark rows
(``scripts/effort_benchmark.py`` output) and recomputes the verdict, so the
same input always gives the same answer:

    python -m scripts.tuning_gate docs/benchmarks/effort-gpt-6.1-sol-2026-10-16.json \\
        --baseline default --candidate high
    python -m scripts.tuning_gate --check-ledger

Verdict rule (``win`` only when all hold):

1. Task success is not worse: the candidate passes at least as many trials
   as the baseline, over the same tasks and repetitions.
2. A cost or speed metric improves: for at least one of wall seconds, output
   tokens or input tokens, the whole paired task-cluster bootstrap 95%
   interval of (candidate - baseline) is below zero.
3. Nothing measurably regresses: no such interval lies wholly above zero.

Anything else is ``no measured win`` and must not ship as a default.
Explicit opt-ins (a preset the owner picks) change no default and are listed
in the ledger as such, with their source, instead of a verdict.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from scripts.effort_benchmark_analysis import paired_interval

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "docs" / "benchmarks" / "tuning-ledger.json"
METRICS = ("wall_seconds", "output_tokens", "input_tokens")


def verdict(data: dict, baseline: str, candidate: str, *, seed: int = 95) -> dict:
    rows = data["rows"]
    keys = [(r["task"], r["config"], r["repetition"]) for r in rows]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate task/configuration/repetition")
    base = {(r["task"], r["repetition"]): r for r in rows if r["config"] == baseline}
    cand = {(r["task"], r["repetition"]): r for r in rows if r["config"] == candidate}
    if not base or not cand:
        raise ValueError(f"results have no {baseline!r} or {candidate!r} arm")
    if base.keys() != cand.keys():
        # Unpaired arms would compare different task mixes.
        raise ValueError("baseline and candidate must cover the same tasks and repetitions")
    passed = {"baseline": sum(bool(r["passed"]) for r in base.values()),
              "candidate": sum(bool(r["passed"]) for r in cand.values()),
              "trials": len(base)}
    intervals = {m: paired_interval(rows, m, baseline=baseline, candidate=candidate, seed=seed)
                 for m in METRICS}
    improved = sorted(m for m, i in intervals.items() if i and i["ci95"][1] < 0)
    regressed = sorted(m for m, i in intervals.items() if i and i["ci95"][0] > 0)
    reasons = []
    if passed["candidate"] < passed["baseline"]:
        reasons.append(f"fewer passes ({passed['candidate']} vs {passed['baseline']} of {passed['trials']})")
    if regressed:
        reasons.append("measurably worse on " + ", ".join(regressed))
    if not improved:
        reasons.append("no metric's 95% interval is wholly below zero")
    return {"baseline": baseline, "candidate": candidate, "seed": seed,
            "verdict": "no measured win" if reasons else "win",
            "reasons": reasons, "passed": passed, "improved": improved, "regressed": regressed,
            "intervals": {m: (None if i is None else {"mean_difference": i["mean_difference"],
                                                       "ci95": i["ci95"], "pairs": i["pairs"],
                                                       "task_clusters": i["task_clusters"]})
                          for m, i in intervals.items()}}


def load_ledger(path: Path = LEDGER) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def check_ledger(path: Path = LEDGER) -> list[str]:
    """Problems with the ledger; empty when every default has a reproducible win."""
    problems = []
    ledger = load_ledger(path)
    for entry in ledger["defaults"]:
        name = entry["id"]
        evidence = ROOT / entry["evidence"]
        if not evidence.is_file():
            problems.append(f"{name}: evidence {entry['evidence']} is missing")
            continue
        got = verdict(json.loads(evidence.read_text(encoding="utf-8")),
                      entry["baseline"], entry["candidate"])
        if got["verdict"] != "win":
            problems.append(f"{name}: {got['verdict']} ({'; '.join(got['reasons'])})")
    for entry in ledger["opt_in"]:
        if not entry.get("source", "").startswith("https://"):
            problems.append(f"{entry['id']}: an opt-in preset needs a public source")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("results", nargs="?", type=Path, help="benchmark results JSON")
    parser.add_argument("--baseline", default="default")
    parser.add_argument("--candidate")
    parser.add_argument("--seed", type=int, default=95)
    parser.add_argument("--check-ledger", action="store_true",
                        help="recompute every shipped default in docs/benchmarks/tuning-ledger.json")
    args = parser.parse_args(argv)
    if args.check_ledger:
        problems = check_ledger()
        for problem in problems:
            print(problem)
        if not problems:
            print("Every shipped tuning default has a reproducible measured win.")
        return 1 if problems else 0
    if not args.results or not args.candidate:
        parser.error("give a results file and --candidate, or --check-ledger")
    result = verdict(json.loads(args.results.read_text(encoding="utf-8")),
                     args.baseline, args.candidate, seed=args.seed)
    print(json.dumps(result, indent=2))
    return 0 if result["verdict"] == "win" else 1


if __name__ == "__main__":
    raise SystemExit(main())
