"""Recompute effort results and paired task-cluster bootstrap intervals offline.

python -m scripts.effort_benchmark_analysis /tmp/effort-results.json
"""
from __future__ import annotations

import argparse
import collections
import json
import random
import statistics
from pathlib import Path

from scripts.effort_benchmark import summarize


def paired_interval(rows, metric, *, baseline="default", candidate="high", seed=95):
    base = {(r["task"], r["repetition"]): r for r in rows if r["config"] == baseline}
    other = {(r["task"], r["repetition"]): r for r in rows if r["config"] == candidate}
    groups = collections.defaultdict(list)
    for key in base.keys() & other.keys():
        a, b = base[key].get(metric), other[key].get(metric)
        if a is not None and b is not None:
            groups[key[0]].append(b - a)
    if not groups:
        return None
    # Sort before sampling: hash randomization must not affect the interval.
    values = [groups[k] for k in sorted(groups)]
    rng = random.Random(seed)
    samples = sorted(statistics.mean(x for group in rng.choices(values, k=len(values)) for x in group)
                     for _ in range(10000))
    differences = [x for group in values for x in group]
    return {"baseline": baseline, "candidate": candidate, "metric": metric,
            "pairs": len(differences), "task_clusters": len(values), "seed": seed,
            "mean_difference": statistics.mean(differences),
            "ci95": [samples[249], samples[9749]],
            "candidate_lower_pairs": sum(x < 0 for x in differences)}


def analyze(data):
    rows = data["rows"]
    keys = [(r["task"], r["config"], r["repetition"]) for r in rows]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate task/configuration/repetition")
    return {"summary": summarize(rows),
            "summary_by_kind": {kind: summarize([r for r in rows if r["kind"] == kind])
                                for kind in sorted({r["kind"] for r in rows})},
            "high_vs_default": {metric: paired_interval(rows, metric)
                                for metric in ("wall_seconds", "output_tokens")}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    args = parser.parse_args()
    print(json.dumps(analyze(json.loads(args.results.read_text())), indent=2))


if __name__ == "__main__":
    main()
