#!/usr/bin/env python3
"""Offline, privacy-minimized arithmetic for schema-1 harness audit snapshots.

No collector, provider probes, database access, or source-file mutation.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import statistics


def count(row, name):
    value = row.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer or null")
    return value


def cache_count(row, name):
    flag = row.get(name.replace("_tokens", "_reported"))
    if flag is not None and not isinstance(flag, bool):
        raise ValueError("cache reporting flags must be booleans")
    value = count(row, name)
    return None if flag is False else value


def prompt_total(row):
    """Accept inclusive raw counts or exclusive normalized counts, never both."""
    mode = row["input_semantics"]
    fresh = count(row, "input_tokens")
    read = cache_count(row, "cache_read_tokens")
    write = cache_count(row, "cache_creation_tokens")
    if mode == "unknown" or fresh is None:
        return None
    if mode == "inclusive":
        if sum(v for v in (read, write) if v is not None) > fresh:
            raise ValueError("cached subsets exceed inclusive input_tokens")
        return fresh
    if read is None or write is None:
        return None
    return fresh + read + write


def stats(values):
    values = sorted(values)
    if not values:
        return {"n": 0, "sum": None, "median": None, "p95": None, "max": None}
    return {"n": len(values), "sum": sum(values),
            "median": statistics.median(values),
            "p95": values[math.ceil(len(values) * .95) - 1], "max": values[-1]}


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def summarize(rows):
    successful = [r for r in rows if not r.get("transport_failed", False)]
    prompts, outputs, utilizations, reads, paired = [], [], [], [], []
    for row in successful:
        total = prompt_total(row)
        output = count(row, "output_tokens")
        read = cache_count(row, "cache_read_tokens")
        window = count(row, "context_limit_tokens")
        if total is not None:
            prompts.append(total)
            if window:
                utilizations.append(total / window)
        if output is not None:
            outputs.append(output)
        if read is not None:
            reads.append(read)
            if total is not None:
                paired.append((read, total))
    return {
        "calls": len(rows), "successful_calls": len(successful),
        "transport_failed_calls": len(rows) - len(successful),
        "prompt_tokens": stats(prompts), "output_tokens": stats(outputs),
        "context_utilization": stats(utilizations),
        "unknown_prompt_calls": len(successful) - len(prompts),
        "cache_read_known_calls": len(reads),
        "cache_read_unknown_calls": len(successful) - len(reads),
        "cache_read_coverage": ratio(len(reads), len(successful)),
        "calls_with_cache_reads": sum(v > 0 for v in reads),
        "cache_hit_call_fraction": ratio(sum(v > 0 for v in reads), len(reads)),
        "cache_paired_calls": len(paired),
        "cache_read_tokens_paired": sum(r for r, _ in paired) if paired else None,
        "prompt_tokens_paired": sum(t for _, t in paired) if paired else None,
        "weighted_cache_read_fraction": ratio(sum(r for r, _ in paired),
                                               sum(t for _, t in paired)),
    }


# Promotion reasons (ContextCompaction.reason) and the decision that committed
# them (ContextCompaction.decision_reason), from fluid/context/compaction.py and
# cache_signal.py. Anything else is counted as "other".
COMPACTION_REASONS = {"trigger", "overflow", "image_consumed", "sweep", "turn_budget",
                      "cli_auto_compact", "cli_manual_compact", "unknown"}
DECISION_REASONS = {"meaningful_reclaim", "hard_ceiling", "window_protection",
                    "below_threshold", "insufficient_reclaim",
                    "awaiting_reclaimable_growth", "cold_commit", "cold_opportunistic",
                    "break_even", "warm_defer", "sweep", "provider_overflow",
                    "provider_payload_rejection", "unknown"}


def _label(value, allowed):
    return value if isinstance(value, str) and value in allowed else "other"


def compaction_summary(events):
    pairs = []
    tiers, reasons, decision_reasons = Counter(), Counter(), Counter()
    for event in events:
        tier = event.get("tier")
        tiers[str(tier) if type(tier) is int and tier in (1, 2, 3) else "unknown"] += 1
        reasons[_label(event.get("reason", "unknown"), COMPACTION_REASONS)] += 1
        decision = event.get("decision_reason") or "unknown"
        decision_reasons[_label(decision, DECISION_REASONS)] += 1
        before, after = count(event, "before_tokens"), count(event, "after_tokens")
        if before is not None and before > 0 and after is not None:
            pairs.append((before, after))
    return {"events": len(events), "tiers": dict(tiers), "reasons": dict(reasons),
            "decision_reasons": dict(decision_reasons),
            "token_pairs_reported": len(pairs),
            "token_pairs_unknown": len(events) - len(pairs),
            "reported_tokens_reclaimed": sum(b - a for b, a in pairs) if pairs else None,
            "reported_reduction_fraction": ratio(sum(b - a for b, a in pairs),
                                                 sum(b for b, _ in pairs))}


def analyze(snapshot):
    if not isinstance(snapshot, dict):
        raise ValueError("snapshot must be an object")
    if type(snapshot.get("schema_version")) is not int or snapshot["schema_version"] != 1:
        raise ValueError("expected schema_version 1")
    if not isinstance(snapshot.get("sessions"), list):
        raise ValueError("sessions must be a list")
    cohorts, groups, warm_groups, compactions = (defaultdict(list) for _ in range(4))
    session_counts = Counter()
    session_ids = set()
    for session in snapshot["sessions"]:
        key, cohort = session["key"], session["cohort"]
        if not isinstance(key, str) or not key or key in session_ids:
            raise ValueError("session keys must be unique nonempty aliases")
        if not isinstance(cohort, str) or not cohort:
            raise ValueError("cohort must be a nonempty label")
        if not isinstance(session.get("calls"), list):
            raise ValueError("calls must be a list")
        session_ids.add(key)
        session_counts[cohort] += 1
        # Include sessions with no calls, rather than silently dropping them.
        cohorts[cohort]
        compactions[cohort].extend(session.get("compactions", []))
        seen, legs = set(), set()
        for row in session["calls"]:
            call_id = row["call_id"]
            if not isinstance(call_id, str) or not call_id or call_id in seen:
                raise ValueError("call IDs must be unique within a session; deduplicate first")
            seen.add(call_id)
            if row["input_semantics"] not in {"exclusive", "inclusive", "unknown"}:
                raise ValueError("input_semantics must be exclusive, inclusive, or unknown")
            leg = tuple(row[name] for name in ("provider", "model", "engine"))
            if not all(isinstance(v, str) and v for v in leg):
                raise ValueError("provider/model/engine must be nonempty labels")
            if type(row.get("transport_failed", False)) is not bool:
                raise ValueError("transport_failed must be boolean")
            # Validate even failed rows; they are not used as successful usage.
            prompt_total(row)
            count(row, "output_tokens")
            count(row, "context_limit_tokens")
            group = (cohort,) + leg
            cohorts[cohort].append(row)
            groups[group].append(row)
            # A failed first attempt does not establish a warm prefix.
            if not row.get("transport_failed", False):
                if leg in legs:
                    warm_groups[group].append(row)
                legs.add(leg)
    result = {"schema_version": 1, "selected_sessions": len(session_ids),
              "cohorts": {}, "groups": []}
    for cohort, rows in sorted(cohorts.items()):
        result["cohorts"][cohort] = {"sessions": session_counts[cohort],
                                      **summarize(rows),
                                      "compactions": compaction_summary(compactions[cohort])}
    for group, rows in sorted(groups.items()):
        labels = dict(zip(("cohort", "provider", "model", "engine"), group))
        result["groups"].append({**labels, **summarize(rows),
                                 "warm_candidates": summarize(warm_groups[group])})
    return result


def compare(current, baseline):
    names = ("cohort", "provider", "model", "engine")
    def index(result):
        return {tuple(g[n] for n in names): g for g in result["groups"]}
    now, old = index(current), index(baseline)
    paired = []
    for key in sorted(now.keys() & old.keys()):
        n, b = now[key], old[key]
        def delta(a, z):
            return a - z if a is not None and z is not None else None
        paired.append({**dict(zip(names, key)), "current_calls": n["calls"],
                       "baseline_calls": b["calls"],
                       "cache_fraction_delta": delta(n["weighted_cache_read_fraction"],
                                                     b["weighted_cache_read_fraction"]),
                       "prompt_median_delta": delta(n["prompt_tokens"]["median"],
                                                    b["prompt_tokens"]["median"]),
                       "cache_coverage_delta": delta(n["cache_read_coverage"],
                                                     b["cache_read_coverage"])})
    return {"matched_groups": paired,
            "unmatched_current_groups": [dict(zip(names, k)) for k in sorted(now.keys() - old.keys())],
            "unmatched_baseline_groups": [dict(zip(names, k)) for k in sorted(old.keys() - now.keys())],
            "interpretation": "Matched labels only; task mix, revisions and coverage need review. Not causal proof."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        result = analyze(json.loads(args.snapshot.read_text(encoding="utf-8")))
        if args.baseline:
            previous = analyze(json.loads(args.baseline.read_text(encoding="utf-8")))
            result["comparison"] = compare(result, previous)
        text = json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n"
        if args.output:
            # Atomic exclusive creation: never clobber evidence or the input.
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(text)
        else:
            print(text, end="")
    except (ValueError, KeyError, TypeError, OSError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
