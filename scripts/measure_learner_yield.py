"""Deterministic learner replay. No provider/network calls or live store writes.

python scripts/measure_learner_yield.py --baseline-ref 931978892
Optional private copied inputs: --before-journal FILE --after-journal FILE
and --memory-file FILE. Only aggregates/hashes are printed. Gold facts are
injected extraction-oracle controls, not a judgment of historical fact quality.
Pacing is held open to isolate filtering/batching; tokens are chars/4 estimates.
"""
from __future__ import annotations
import argparse
import asyncio
import hashlib
import json
import math
import re
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "fluid"))
from fluid.codemode import continual
from fluid.codemode.skills import SkillStore
from fluid.providers.base import StreamChunk

GOLD = [f"The fixture owner prefers documented reproducible workflow variant {i}." for i in range(4)]


class Oracle:
    name = "scripted-oracle"
    def __init__(self):
        self.calls = self.tokens = self.output_tokens = 0
        self.sections = {"system": 0, "library": 0, "memory": 0, "journal_and_guidance": 0}
    async def stream(self, request):
        self.calls += 1
        prompt = request.messages[0].text()
        journal = prompt.partition("## Session journal")[2]
        output = "\n".join("<memory>" + fact + "</memory>" for fact in re.findall(r"<lab_fact>([^<]+)</lab_fact>", journal)) or "NO_CHANGES"
        memory_start, journal_start = prompt.index("## Current memory"), prompt.index("## Session journal")
        self.sections["system"] += len(request.system)
        self.sections["library"] += memory_start
        self.sections["memory"] += journal_start - memory_start
        self.sections["journal_and_guidance"] += len(prompt) - journal_start
        tokens, out = math.ceil((len(request.system) + len(prompt)) / 4), math.ceil(len(output) / 4)
        self.tokens += tokens
        self.output_tokens += out
        yield StreamChunk("text_delta", {"text": output})
        yield StreamChunk("usage", {"usage": {"input_tokens": tokens, "output_tokens": out}, "stop_reason": "end_turn"})


def generated():
    config = json.loads((ROOT / "fluid/tests/fixtures/learner_yield.json").read_text())
    rows = []
    for i in range(config["entries"]):
        entry = {"ts": 1_792_022_400 + i * config["interval_seconds"], "project": config["project"]}
        if i % config["signal_every"] == 0:
            text = "Review the reproducible fixture workflow."
            if i in config["gold_positions"]:
                text += " <lab_fact>" + GOLD[config["gold_positions"].index(i)] + "</lab_fact>"
            entry.update(kind="user", text=text)
        else:
            entry.update(kind="cell", code=f"root='/workspace/fixture'; Read(file_path=root+'/fixture{i}.py')",
                         stdout="fixture data\n" * 12, host_calls=1, tools=["Read"],
                         calls=[{"name": "Read", "ok": True, "outside_cwd": False}])
        rows.append(entry)
    return rows


def controlled_private(path):
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    for i, fact in reversed(list(enumerate(GOLD))):
        at = min(len(rows) - 1, (i + 1) * len(rows) // 5)
        rows.insert(at, {"kind": "user", "text": "<lab_fact>" + fact + "</lab_fact>",
                         "ts": rows[at].get("ts", 0), "project": rows[at].get("project")})
    return rows


async def replay(rows, root, store_type, synth_type, module, memory_file=None, raw=False):
    root.mkdir()
    if memory_file:
        doc = json.loads(memory_file.read_text())
        doc["inference_budget"] = {}
        (root / "memories.json").write_text(json.dumps(doc))
    store, provider = store_type(root), Oracle()
    store.journal_context(cwd="/workspace/fixture")
    learner = synth_type(provider, "scripted-oracle", store)
    learner._paced = lambda: True  # isolate signal batching, not UTC pacing
    clock = [float(rows[0].get("ts", time.time()))]
    selected, trigger_ms = 0, []
    with patch.object(module, "time", types.SimpleNamespace(time=lambda: clock[0], monotonic=time.monotonic)):
        for entry in rows:
            clock[0] = max(clock[0], float(entry.get("ts", 0)))
            if raw:
                # Private inputs are already read-only rehydrated journal copies.
                with store._journal_path.open("a") as stream:
                    stream.write(json.dumps(entry) + "\n")
            else:
                store.journal_append(entry)
            started = time.perf_counter()
            due = learner.due()
            trigger_ms.append((time.perf_counter() - started) * 1000)
            if due:
                selected += 1
                await learner.run()
        for _ in range(100):
            if store.synth_cursor() >= store.journal_count():
                break
            cursor = store.synth_cursor()
            await learner.run()  # explicit end-of-replay flush, not due()
            if cursor == store.synth_cursor():
                raise RuntimeError("Replay made no cursor progress")
        else:
            raise RuntimeError("Replay flush did not terminate")
    retained = sum(fact.text in GOLD for fact in store.memories() + store.archived_memories())
    return {"selected_auto_passes": selected, "model_calls": provider.calls,
            "estimated_input_tokens": provider.tokens, "estimated_output_tokens": provider.output_tokens,
            "controlled_gold_facts_retained": retained,
            "estimated_input_tokens_per_controlled_gold_fact": provider.tokens / retained if retained else None,
            "prompt_section_characters": provider.sections,
            "total_trigger_ms": sum(trigger_ms),
            "mean_trigger_ms": sum(trigger_ms) / len(trigger_ms)}


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", required=True)
    parser.add_argument("--before-journal", type=Path)
    parser.add_argument("--after-journal", type=Path)
    parser.add_argument("--memory-file", type=Path)
    parser.add_argument("--usage-log", type=Path)
    args = parser.parse_args()
    if bool(args.before_journal) != bool(args.after_journal):
        parser.error("private replay requires both before and after journals")
    old = {}
    for name in ("skills", "continual"):
        source = subprocess.check_output(["git", "show", args.baseline_ref + f":fluid/fluid/codemode/{name}.py"], cwd=ROOT, text=True)
        module = types.ModuleType("fluid.codemode._yield_baseline_" + name)
        module.__file__ = str(ROOT / f"fluid/fluid/codemode/{name}.py")
        sys.modules[module.__name__] = module
        exec(compile(source, module.__file__, "exec"), module.__dict__)
        old[name] = module
    inputs = [p for p in (args.before_journal, args.after_journal, args.memory_file, args.usage_log) if p]
    hashes = [hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs]
    before = controlled_private(args.before_journal) if args.before_journal else generated()
    after = controlled_private(args.after_journal) if args.after_journal else generated()
    with tempfile.TemporaryDirectory(prefix="fluid-learner-yield-") as temporary:
        root = Path(temporary)
        result = {"baseline_ref": args.baseline_ref,
                  "before": await replay(before, root / "before", old["skills"].SkillStore, old["continual"].Synthesizer, old["continual"], args.memory_file, bool(args.before_journal)),
                  "after": await replay(after, root / "after", SkillStore, continual.Synthesizer, continual, args.memory_file, bool(args.after_journal))}
        result["exhausted_shared_budget"] = {
            "before": exhausted_triggers(root / "budget-before", old["continual"].Synthesizer),
            "after": exhausted_triggers(root / "budget-after", continual.Synthesizer)}
    result["source_hashes"] = hashes
    result["sources_unchanged"] = hashes == [hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs]
    result["historical_usage"] = usage_accounting(args.usage_log) if args.usage_log else None
    result["semantic_quality_audited"] = False
    print(json.dumps(result, indent=2))


def exhausted_triggers(root, synth_type):
    store = SkillStore(root)
    reserve = continual.CONSOLIDATE_RESERVED_CALLS
    for _ in range(continual.MAX_INFERENCE_CALLS_PER_DAY - reserve):
        assert store._facts.claim_inference_call(reserve=reserve)
    store.journal_append({"kind": "remember", "text": "Fixture preference."})
    selected = 0
    for _ in range(8):
        learner = synth_type(Oracle(), "scripted-oracle", store)
        learner.mark_urgent()
        selected += learner.due()
    return {"fresh_instances": 8, "selected_urgent_attempts": selected}


def usage_accounting(path):
    """Frozen seven-day accounting; updates are not semantic-quality labels."""
    from collections import Counter
    raw = path.read_bytes()
    rows = [json.loads(line) for line in raw.splitlines()]
    as_of = max((float(row.get("ts", 0)) for row in rows), default=0)
    rows = [row for row in rows if float(row.get("ts", 0)) >= as_of - 7 * 86400]
    results = {}
    for kind in ("synthesis", "consolidation"):
        selected = [row for row in rows if row.get("kind") == kind]
        updates = sum(str(item).startswith("memory:") for row in selected for item in row.get("changed", []))
        tokens = sum((row.get("usage") or {}).get("input_tokens", 0) +
                     (row.get("usage") or {}).get("output_tokens", 0) for row in selected)
        errors = Counter(next((label for needle, label in (
            ("InferenceBudgetExhausted", "budget_exhausted"), ("skipped:", "explicit_skip"),
            ("exceeded", "time_limit"), ("429", "rate_limit"))
            if needle in str(row.get("error", ""))), "other_error")
            for row in selected if row.get("error"))
        results[kind] = {"attempt_rows": len(selected), "usage_rows": sum(bool(row.get("usage")) for row in selected),
                         "changing_rows": sum(bool(row.get("changed")) for row in selected),
                         "accepted_memory_updates": updates, "recorded_tokens": tokens,
                         "recorded_tokens_per_accepted_memory_update": tokens / updates if updates else None,
                         "error_buckets": dict(errors)}
    return {"source_sha256": hashlib.sha256(raw).hexdigest(), "as_of": as_of,
            "window_days": 7, "by_kind": results}


if __name__ == "__main__":
    asyncio.run(main())
