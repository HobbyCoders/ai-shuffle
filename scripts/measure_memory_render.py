#!/usr/bin/env python3
"""Offline synthetic memory-render timings; never reads worker state or calls a model.

Run against any checkout with PYTHONPATH=<checkout>/fluid python this_script.py.
Uses the independent review's timing.py fixture; reports cold and warmed samples.
"""
from __future__ import annotations

import json
from pathlib import Path
import statistics
import tempfile
import time

from fluid.codemode.skills import SkillStore


def measure(count: int) -> dict:
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
        store._facts._write(doc)
        start = time.perf_counter()
        store.render_memory_section()
        cold_ms = (time.perf_counter() - start) * 1000
        samples = []
        for _ in range(3):
            start = time.perf_counter()
            section = store.render_memory_section()
            samples.append((time.perf_counter() - start) * 1000)
        return {"facts": count, "cold_ms": round(cold_ms, 2),
                "samples_ms": [round(t, 2) for t in samples],
                "median_ms": round(statistics.median(samples), 2),
                "section_chars": len(section or "")}


if __name__ == "__main__":
    for count in (147, 512):
        print(json.dumps(measure(count), sort_keys=True))
