"""Reproducible snapshot relevance lab; optional live files are copied, never opened for writes.

python scripts/measure_memory_budget.py --baseline-ref 9d8067571
--memory-file, --requests-file and --labels-file accept private local fixtures;
stdout contains only aggregate counts. Token figures are ceil(chars/4), not billing.
"""
from __future__ import annotations
import argparse, hashlib, inspect, json, math, shutil, subprocess, sys, tempfile, types
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "fluid"))
from fluid.codemode.skills import SkillStore


def seed(store):
    labels = {"off_task_ids": [], "relevant_ids": []}
    for raw in json.loads((ROOT / "fluid/tests/fixtures/memory_budget.json").read_text()):
        row = dict(raw)
        label = row.pop("label", None)
        entry = store.save_memory(**row)
        if label in {"off_task", "relevant"}:
            labels[{"off_task": "off_task_ids", "relevant": "relevant_ids"}[label]].append(entry.id)
    return labels


def summarize(store, project, queries, labels):
    extra = "task_query" in inspect.signature(store.render_memory_section).parameters
    rows = []
    for query in queries:
        kwargs = {"task_query": query} if extra else {}
        plan = store.memory_snapshot_plan(project, **kwargs)
        section = store.render_memory_section(project, **kwargs)
        ids = set(plan["shown_ids"])
        rows.append({"shown": len(ids), "known_off_task": len(ids & set(labels["off_task_ids"])),
                     "relevant_corrections_shown": len(ids & set(labels["relevant_ids"])),
                     "snapshot_chars": len(section), "estimated_snapshot_tokens": math.ceil(len(section) / 4)})
    shown = sum(r["shown"] for r in rows)
    off = sum(r["known_off_task"] for r in rows)
    return {"requests": rows, "shown": shown, "known_off_task": off,
            "known_off_task_rate": off / shown if shown else 0,
            "relevant_corrections_shown": sum(r["relevant_corrections_shown"] for r in rows)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref")
    parser.add_argument("--memory-file", type=Path)
    parser.add_argument("--requests-file", type=Path)
    parser.add_argument("--labels-file", type=Path)
    parser.add_argument("--project", default="project:fixture")
    parser.add_argument("--project-from-id")
    args = parser.parse_args()
    if args.memory_file and not args.labels_file:
        parser.error("--memory-file requires --labels-file; do not seed synthetic facts into a source fixture")
    queries = ["memory backend delegated approval"]
    if args.requests_file:
        queries = [r["query"] if isinstance(r, dict) else r for r in json.loads(args.requests_file.read_text())]
    baseline_type = None
    if args.baseline_ref:
        source = subprocess.check_output(["git", "show", args.baseline_ref + ":fluid/fluid/codemode/skills.py"], cwd=ROOT, text=True)
        module = types.ModuleType("fluid.codemode._budget_baseline")
        module.__file__ = str(ROOT / "fluid/fluid/codemode/skills.py")
        sys.modules[module.__name__] = module
        exec(compile(source, module.__file__, "exec"), module.__dict__)
        baseline_type = module.SkillStore
    before_hash = hashlib.sha256(args.memory_file.read_bytes()).hexdigest() if args.memory_file else None
    with tempfile.TemporaryDirectory(prefix="fluid-budget-lab-") as temp:
        path = Path(temp)
        if args.memory_file:
            (path / "candidate").mkdir()
            shutil.copyfile(args.memory_file, path / "candidate/memories.json")
        candidate = SkillStore(path / "candidate")
        labels = json.loads(args.labels_file.read_text()) if args.labels_file else seed(candidate)
        project = args.project
        if args.project_from_id:
            project = next(e.scope for e in candidate.memories() if e.id == args.project_from_id)
        result = {}
        if baseline_type:
            shutil.copytree(path / "candidate", path / "baseline")
            result["before"] = summarize(baseline_type(path / "baseline"), project, queries, labels)
        result["after"] = summarize(candidate, project, queries, labels)
    if args.memory_file:
        result["source_hash_unchanged"] = before_hash == hashlib.sha256(args.memory_file.read_bytes()).hexdigest()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
