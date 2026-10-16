"""Check recorded compaction decisions: was the cache really cold when priced cold?

Reads ``events.jsonl`` dumps (one ``{"seq": ..., "event": {...}}`` row per
line, the shape the session store and the audit dumps write; a directory is
searched for ``events.jsonl`` files). Every main-prompt Usage event that
carries a priced ``compaction_decision`` is one decision. Each decision is
grouped by the cold reason it recorded, the reason the current
``CacheSignal.cold_reason`` gives for the same recorded signal, and its gap
band. For every group the report shows how many cold-priced batches it
committed and the cache hit rate of the calls that did not rewrite anything,
which says whether the provider cache really was warm.

The last lines are the field check for short-gap cold pricing: the share of
cold-priced batches whose gap was known and at most ``COLD_GAP_SECONDS``,
leaving out real system prompt or tool changes, as recorded and as the
current rule would price the same signals.

Usage::

    python scripts/cache_cold_audit.py path/to/export [more paths] [--json]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fluid.context.cache_signal import (
    COLD_GAP_SECONDS,
    MISS_HIT_RATE,
    CacheSignal,
)

# Decision reasons that commit a batch, and those that commit it because
# the cache was priced cold.
COMMIT_REASONS = {"cold_opportunistic", "cold_commit", "break_even", "hard_ceiling"}
COLD_COMMIT_REASONS = {"cold_opportunistic", "cold_commit"}


def event_files(paths: Iterable[Path]) -> list[Path]:
    found: list[Path] = []
    for path in paths:
        if path.is_dir():
            found.extend(sorted(path.rglob("events.jsonl")))
        else:
            found.append(path)
    return found


def decisions(path: Path) -> Iterator[dict[str, Any]]:
    """One row per priced decision in one session's event log."""
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            event = json.loads(line).get("event") or {}
            if event.get("_cls") != "Usage":
                continue
            raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
            if raw.get("auxiliary_source"):
                continue
            decision = raw.get("compaction_decision") or {}
            signal = decision.get("cache_signal")
            if not isinstance(signal, dict):
                continue
            gap = signal.get("gap_s")
            current = CacheSignal(
                gap_s=None if gap is None else float(gap),
                static_changed=bool(signal.get("static_changed")),
                resumed=bool(signal.get("resumed")),
            ).cold_reason
            prompt = sum(int(event.get(k) or 0) for k in (
                "input_tokens", "cache_read_tokens", "cache_creation_tokens"))
            yield {
                "recorded": decision.get("cold_reason") or "",
                "current": current,
                "gap_s": gap,
                "static_changed": bool(signal.get("static_changed")),
                "reason": decision.get("reason") or "",
                "hit_rate": (int(event.get("cache_read_tokens") or 0) / prompt
                             if prompt > 0 else None),
            }


def band(gap: float | None) -> str:
    if gap is None:
        return "unknown"
    return f"<={COLD_GAP_SECONDS:g}s" if gap <= COLD_GAP_SECONDS else f">{COLD_GAP_SECONDS:g}s"


def short_gap(row: dict[str, Any]) -> bool:
    """A known gap within the cache lifetime and no static change."""
    return (row["gap_s"] is not None and row["gap_s"] <= COLD_GAP_SECONDS
            and not row["static_changed"])


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (row["recorded"] or "warm", row["current"] or "warm", band(row["gap_s"]))
        groups.setdefault(key, []).append(row)
    table = []
    for key, members in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        hits = [m["hit_rate"] for m in members
                if m["reason"] not in COMMIT_REASONS and m["hit_rate"] is not None]
        table.append({
            "recorded": key[0], "current": key[1], "gap": key[2],
            "decisions": len(members),
            "cold_commits": sum(m["reason"] in COLD_COMMIT_REASONS for m in members),
            "idle_calls": len(hits),
            "idle_median_hit": round(statistics.median(hits), 3) if hits else None,
            "idle_warm_share": (round(sum(h >= MISS_HIT_RATE for h in hits) / len(hits), 3)
                                if hits else None),
        })
    cold = [r for r in rows if r["reason"] in COLD_COMMIT_REASONS]
    # The same committed batches as the current rule would price them; a
    # batch it prices warm would have been deferred or skipped instead.
    still_cold = [r for r in cold if r["current"]]
    return {
        "decisions": len(rows),
        "table": table,
        "recorded": {"cold_commits": len(cold),
                     "short_gap": sum(short_gap(r) for r in cold)},
        "current_rule": {"cold_commits": len(still_cold),
                         "short_gap": sum(short_gap(r) for r in still_cold)},
    }


def _share(part: int, whole: int) -> str:
    return f"{part}/{whole} ({100 * part / whole:.1f}%)" if whole else f"{part}/0"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("paths", type=Path, nargs="+")
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = parser.parse_args(argv)
    files = event_files(args.paths)
    rows = [row for path in files for row in decisions(path)]
    summary = summarize(rows)
    summary["sessions"] = len(files)
    if args.json:
        print(json.dumps(summary, indent=2))
        return 0
    print(f"{summary['decisions']} priced decisions in {len(files)} event logs")
    print(f"{'recorded':>15} {'current':>15} {'gap':>8} {'decisions':>9} "
          f"{'cold commits':>12} {'idle hit':>8} {'warm share':>10}")
    for g in summary["table"]:
        hit = "-" if g["idle_median_hit"] is None else f"{g['idle_median_hit']:.3f}"
        warm = "-" if g["idle_warm_share"] is None else f"{g['idle_warm_share']:.3f}"
        print(f"{g['recorded']:>15} {g['current']:>15} {g['gap']:>8} {g['decisions']:>9} "
              f"{g['cold_commits']:>12} {hit:>8} {warm:>10}")
    for label, key in (("recorded", "recorded"), ("current rule", "current_rule")):
        part = summary[key]
        print(f"cold-priced batches with a known gap <= {COLD_GAP_SECONDS:g}s "
              f"({label}): {_share(part['short_gap'], part['cold_commits'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
