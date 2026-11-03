#!/usr/bin/env python3
"""Offline synthetic memory-render timings; never reads worker state or calls a model.

Run against any checkout with PYTHONPATH=<checkout>/fluid python this_script.py.
Uses the independent review's timing.py fixture; reports cold and warmed samples.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import statistics
import tempfile
import time

from fluid.codemode.skills import SkillStore


def measure(count: int, *, routing: bool = False, answer: str = "none") -> dict:
    with tempfile.TemporaryDirectory(prefix="memory-render-measure-") as home:
        root = Path(home)
        store = SkillStore(root)
        store.save_memory("seed fact for timing", protected=True)
        doc = store._facts._load()
        template = doc["entries"][0]
        for i in range(1, count):
            row = dict(template)
            row.update(id=f"m{i + 10}", text=(
                f"Rule {i}: when working on subsystem {i} prefer the {i % 7} approach "
                f"and keep tests in module {i % 11}; the owner decided this for deploy {i % 5}."
            ) * 3, updated_at=template["updated_at"] + i,
                created_at=template["created_at"] + i, fingerprint=None)
            doc["entries"].append(row)
        if routing:
            for i, row in enumerate(doc["entries"]):
                row["text"] = f"Code work for subsystem {i} goes to {'Opus' if i % 2 else 'Sol'}."
        if answer != "none":
            from fluid.codemode.facts import global_id
            from fluid.codemode.preferences import coexistence_key, COEXIST_KEY_PREFIX
            a, b = doc["entries"][:2]
            if answer.startswith("legacy"):
                values = sorted((global_id(e), e["text"], e.get("when"), bool(e.get("always")),
                                 e.get("scope", "global"), e.get("worker")) for e in (a, b))
                key = COEXIST_KEY_PREFIX + hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()[:32]
                if answer == "legacy-stale":
                    key = key[:-1] + ("1" if key[-1] == "0" else "0")
            else:
                key = coexistence_key(a, b)
            doc["entries"].append(dict(template, id="m9999", text="Both stand.", key=key))
        store._facts._write(doc)
        try:
            from fluid.codemode import preferences as pref
            for name in ("_cached_conflict_refs", "_cached_answer_matches"):
                cached = getattr(pref, name, None)
                if cached is not None:
                    cached.cache_clear()
        except ImportError:
            pass  # older main revisions have no question detector
        start = time.perf_counter()
        store.render_memory_section()
        cold_ms = (time.perf_counter() - start) * 1000
        samples = []
        for _ in range(3):
            start = time.perf_counter()
            section = store.render_memory_section()
            samples.append((time.perf_counter() - start) * 1000)
        return {"facts": count, "routing": routing, "answer": answer,
                "cold_ms": round(cold_ms, 2),
                "samples_ms": [round(t, 2) for t in samples],
                "median_ms": round(statistics.median(samples), 2),
                "section_chars": len(section or "")}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--counts", type=int, nargs="+", default=[147, 512])
    parser.add_argument("--routing", action="store_true", help="Every fact names a job and model")
    parser.add_argument("--answer", choices=["none", "current", "legacy", "legacy-stale"], default="none")
    args = parser.parse_args()
    for count in args.counts:
        print(json.dumps(measure(count, routing=args.routing, answer=args.answer), sort_keys=True))
