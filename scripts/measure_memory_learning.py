"""Read-only memory lab. Live inputs are copied before SkillStore opens them.
Run PYTHONPATH=fluid python scripts/measure_memory_learning.py [--store DIR] [--prefix].
Outputs counts/latencies only; never fact text, queries, paths or identifiers.
Tokens are ceil(chars/4), not provider billing. No network/model calls.
"""
from __future__ import annotations
import argparse
import asyncio
from collections import Counter
import json
from pathlib import Path
import shutil
import statistics
import sys
import tempfile
import time
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "fluid")]
from fluid.codemode.skills import SkillStore
from fluid.tools.builtins.remember import handler
from fluid.tools.registry import ToolCtx


def p75(samples):
    return sorted(samples)[(len(samples) * 3) // 4]


def measure(root: Path, source: Path | None = None):
    if source:
        # Copy only the fact document. Opening SkillStore can migrate/reap it.
        shutil.copyfile(source / "memories.json", root / "memories.json")
    store = SkillStore(root)
    if not source:
        facts = [
            ("The theme uses purple accents and dark surfaces.", "theme"),
            ("The security controller validates deployment permissions.", "security"),
            ("Install dependencies in the checkout before running tests.", "install"),
            ("The fluid harness runs native provider loops.", "harness"),
            ("The staging build ships through a protected pipeline.", "deploy"),
            ("Use uv to manage Python dependencies.", "python"),
        ]
        for text, key in facts:
            store.save_memory(text, key=key, protected=True)
    entries = store.memories()
    texts = Counter(" ".join(e.text.casefold().split()) for e in entries)
    snapshot = store.render_memory_section() or ""
    result = {"live": len(entries), "archived": len(store.archived_memories()),
              "exact_duplicate_excess": sum(n-1 for n in texts.values()),
              "conflicted": sum(e.conflicted for e in entries),
              "snapshot_chars": len(snapshot), "snapshot_tokens_est": (len(snapshot)+3)//4}
    if not source:
        cases = [("UI theme colors", "theme"), ("native fluid harness", "harness"),
                 ("uv Python", "python"), ("staging pipeline", "deploy")]
        precisions, recalls, latencies = [], [], []
        for query, key in cases:
            start = time.perf_counter()
            hits = store.search_memories(query, limit=3)
            latencies.append((time.perf_counter()-start)*1000)
            correct = sum(e.key == key for e, _ in hits)
            precisions.append(correct / len(hits) if hits else 0)
            recalls.append(float(correct > 0))
        result.update(recall_precision_at_3=statistics.mean(precisions),
                      recall_recall_at_3=statistics.mean(recalls), recall_p75_ms=p75(latencies))
    # Producer-path correction with omitted scope. Owner content is synthetic.
    seed = store.save_memory("Fixture project uses runner alpha.", scope="project:lab", protected=True)
    before = store.render_memory_section("project:other") or ""
    ctx = ToolCtx(extra={"memory_store": store, "memory_project": "project:lab"})
    start = time.perf_counter()
    outcome = asyncio.run(handler({"text": "Fixture project uses runner beta.", "memory_id": seed.id,
                                  "expected_revision": seed.revision, "user_requested": True}, ctx))
    elapsed = (time.perf_counter()-start)*1000
    after = store.render_memory_section("project:other") or ""
    result.update(correction_succeeded=not outcome.is_error,
                  cross_project_correction_leaks=int("runner beta" in after and "runner alpha" not in before),
                  remember_ms=elapsed)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path)
    parser.add_argument("--prefix", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="fluid-memory-lab-") as tmp:
        result = measure(Path(tmp), args.store)
    if args.prefix:
        from scripts.measure_prompt_catalog import measure as prefix
        result["prefix"] = {leg: prefix(native=native) for leg, native in (("native", True), ("delegated", False))}
    print(json.dumps(result, indent=2, sort_keys=True))

if __name__ == "__main__":
    main()
