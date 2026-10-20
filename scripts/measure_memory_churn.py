"""Measure reminder churn without providers or live-store writes.

python scripts/measure_memory_churn.py --baseline-ref <commit>
Optional --manifest/--audit read session exports and emit aggregates only.
Historical replay preserves recorded full-restore boundaries; it is not a
provider cache-hit or billing measurement. Tokens are ceil(characters/4).
"""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import types
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "fluid")]
from app.core import memory_reminder as mr
from fluid.codemode.skills import SkillStore
FIXTURE = ROOT / "tests/fixtures/memory_churn.json"


def controlled_sections(store, config=None):
    """Real durable producers, consumed lazily by native/delegated tests."""
    config = config or json.loads(FIXTURE.read_text())
    saved = [store.save_memory(
        f"Backend convention {i}: use deterministic pytest fixtures. "
        + "Preserve durable correctness and clear results. " * 3,
        key=f"coding.rule{i}", scope="project:fixture", protected=True)
        for i in range(config["facts"])]
    yield store.render_memory_section("project:fixture")
    for phase in range(config["phases"]):
        for i in range(config["foreign_per_phase"]):
            store.save_memory(f"Unrelated foreign project convention {phase}-{i}.",
                              scope="project:foreign", protected=True)
            yield store.render_memory_section("project:fixture")
        if phase == 0:
            store.save_memory(
                "Backend convention corrected: run deterministic pytest fixtures before review.",
                memory_id=saved[0].id, expected_revision=saved[0].revision,
                scope="project:fixture", protected=True)
        else:
            store.delete_memory(saved[1].id, explicit=True)
        yield store.render_memory_section("project:fixture")


def summarize(sequence, module=mr):
    held = None
    counts = Counter()
    for section in sequence:
        counts["turns"] += 1
        plan = module.plan_memory_note(section, held)
        counts[plan.kind] += 1
        if not plan.note:
            continue
        counts["delivered_chars"] += len(plan.note)
        kind, body = module.memory_blocks(plan.note)[0]
        if kind == "full":
            held = module.HeldMemory(frozenset(module.section_items(body)), 0, "fixture")
        else:
            counts["delta_chars"] += len(plan.note)
            for chunk in body.split("\n\n"):
                head, _, tail = chunk.partition("\n")
                if head == module._ADDED:
                    counts["unchanged_fact_repetitions"] += sum(
                        line in held.items and bool(module._fact_id(line))
                        for line in module.section_items(tail))
            held = module.HeldMemory(frozenset(module.apply_changes(sorted(held.items), body)),
                                     held.changes_chars + len(plan.note), "fixture")
    counts["estimated_delivered_tokens"] = math.ceil(counts["delivered_chars"] / 4)
    return dict(counts)


def replay_export(manifest, audit=None):
    """Replay only host-pinned carriers; raw counts are reported separately."""
    limits = {}
    if audit:
        limits = {row["session"]: row["changes"]
                  for group in json.loads(audit.read_text())["sessions"].values() for row in group}
    counts = Counter()
    digest = hashlib.sha256()
    for entry in json.loads(manifest.read_text()):
        original = candidate = None
        seen = 0
        for raw in (Path(entry["dir"]) / "nodes.jsonl").read_text().splitlines():
            digest.update(raw.encode())
            digest.update(b"\n")
            message = json.loads(raw).get("message", {})
            if message.get("role") != "user":
                continue
            text = "".join(block.get("text", "") for block in message.get("content", [])
                           if block.get("kind") == "text")
            for kind, body in mr.memory_blocks(text):
                if kind == "changes":
                    counts["raw_changes"] += 1
                    counts["raw_change_chars"] += len(body)
            pin = message.get("context_pin")
            if not (isinstance(pin, (list, tuple)) and len(pin) == 2
                    and all(type(value) is int for value in pin)
                    and 0 <= pin[0] < pin[1] <= len(text)):
                continue
            for kind, body in mr.memory_blocks(text[pin[0]:pin[1]]):
                if kind == "full":
                    original = list(mr.section_items(body))
                    candidate = list(original)
                    counts["host_full"] += 1
                    continue
                if audit and seen >= limits.get(entry["session_id"], 0):
                    continue
                seen += 1
                counts["host_changes"] += 1
                counts["before_body_chars"] += len(body)
                counts["before_wrapped_chars"] += len(mr.wrap(body))
                if original is None:
                    counts["unknown_previous_snapshot"] += 1
                    counts["after_changes"] += 1
                    counts["after_body_chars"] += len(body)
                    counts["after_wrapped_chars"] += len(mr.wrap(body))
                    continue  # unknown held state earns no speculative savings
                current = mr.apply_changes(original, body)
                if mr._substantive_memory_items(original) == mr._substantive_memory_items(current):
                    counts["aggregate_only_changes"] += 1
                    counts["aggregate_only_body_chars"] += len(body)
                for chunk in body.split("\n\n"):
                    head, _, tail = chunk.partition("\n")
                    if head == mr._ADDED:
                        counts["unchanged_fact_repetitions"] += sum(
                            line in original and bool(mr._fact_id(line))
                            for line in mr.section_items(tail))
                original = current
                if mr._substantive_memory_items(candidate) == mr._substantive_memory_items(current):
                    continue
                after = mr.render_changes(candidate, current)
                if after:
                    counts["after_changes"] += 1
                    counts["after_body_chars"] += len(after)
                    counts["after_wrapped_chars"] += len(mr.wrap(after))
                    candidate = list(current)
    return {"counts": dict(counts), "source_digest": digest.hexdigest(),
            "counterfactual": "delta-only; historical full restores retained; no model/cache measurement"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--audit", type=Path)
    args = parser.parse_args()
    baseline = None
    if args.baseline_ref:
        source = subprocess.check_output(
            ["git", "show", args.baseline_ref + ":app/core/memory_reminder.py"], cwd=ROOT, text=True)
        baseline = types.ModuleType("_churn_baseline")
        sys.modules[baseline.__name__] = baseline
        exec(compile(source, "<baseline memory_reminder>", "exec"), baseline.__dict__)
    with tempfile.TemporaryDirectory(prefix="fluid-memory-churn-") as temporary:
        sequence = list(controlled_sections(SkillStore(Path(temporary))))
        result = {"controlled_after": summarize(sequence)}
        if baseline:
            result["controlled_before"] = summarize(sequence, baseline)
    if args.manifest:
        result["historical"] = replay_export(args.manifest, args.audit)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()