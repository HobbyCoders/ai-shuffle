"""Measure retained supersession, never opening a source store for writes.

Run from the repo: python scripts/measure_memory_supersession.py
Optional: --source-file /path/to/memories.json --old-id ID --new-id ID
Only aggregate counts/sizes are printed. Real input is copied to a temporary
store; no source contents, identities, project IDs or facts are printed.
"""
from __future__ import annotations
import argparse, ast, hashlib, json, math, shutil, subprocess, sys, tempfile, types
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fluid"))
from fluid.codemode.skills import SkillStore
from fluid.codemode.continual import _SYNTH_SYSTEM, _CONSOLIDATE_SYSTEM

OLD = "Backend/general subagent work runs on GPT Astra (gpt-6-astra); Opus handles frontend work."
NEW = "Code work by subagents runs on GPT 6.1 Sol, replacing gpt-6-astra. Code review and frontend work use Opus."


def scenario(store, old_id, new_id, project):
    def describe():
        section = store.render_memory_section(project) or ""
        entries = store.memories() + store.archived_memories()
        old = next(e for e in entries if e.id == old_id)
        new = next(e for e in entries if e.id == new_id)
        return {
            "old_value_injected": old.text in section,
            "new_value_injected": new.text in section,
            "predecessor_retained": old.text == original,
            "has_supersession_link": bool(old.superseded_by),
            "predecessor_archived": any(e.id == old_id for e in store.archived_memories()),
            "owner_conflict_marker": f"[{old.id} r{old.revision}; conflict]" in section,
            "snapshot_chars": len(section), "snapshot_tokens_est": math.ceil(len(section)/4),
        }
    original = next(e.text for e in store.memories() if e.id == old_id)
    before = describe()
    store.curate(force=True)
    return {"before": before, "after": describe()}


def proposal_withdrawal(root, fact_type=None):
    """Real save/proposal/Forget/retire producers, never a model call."""
    results = {}
    for action in ("forget", "retire"):
        store = SkillStore(root / action)
        if fact_type is not None:
            store._facts = fact_type(store.root)
        old, middle, final = [store.save_memory(f"Backend workflow jobs run on worker {worker} nightly.")
                              for worker in ("Orion", "Lyra", "Vega")]
        store.propose_memory_supersession(old.id, middle.id,
                                          old_revision=old.revision, new_revision=middle.revision)
        store.propose_memory_supersession(middle.id, final.id,
                                          old_revision=middle.revision, new_revision=final.revision)
        if action == "forget":
            store.delete_memory(final.id, explicit=True)
        else:
            store.retire_memory(final.id, reason="consolidation")
        section = store.render_memory_section() or ""
        results[action] = {"obsolete_value_injected": old.text in section,
                           "nearest_predecessor_injected": middle.text in section,
                           "snapshot_chars": len(section),
                           "snapshot_tokens_est": math.ceil(len(section) / 4)}
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-file", type=Path)
    parser.add_argument("--old-id")
    parser.add_argument("--new-id")
    parser.add_argument("--baseline-ref")
    args = parser.parse_args()
    result = {}
    with tempfile.TemporaryDirectory(prefix="fluid-supersession-") as temp:
        root = Path(temp)
        if args.source_file:
            if not args.old_id or not args.new_id:
                parser.error("source-file requires old-id and new-id")
            digest = hashlib.sha256(args.source_file.read_bytes()).hexdigest()
            shutil.copyfile(args.source_file, root / "memories.json")
            store = SkillStore(root)
            old = next(e for e in store.memories() if e.id == args.old_id)
            result["copied_store"] = scenario(store, old.id, args.new_id, old.scope)
            result["source_unchanged"] = digest == hashlib.sha256(args.source_file.read_bytes()).hexdigest()
        else:
            for protected in (False, True):
                store = SkillStore(root / str(protected))
                apply = store._facts._apply_supersessions
                store._facts._apply_supersessions = lambda *a, **k: []
                old = store.save_memory(OLD, key="subagents.model-routing", scope="project:fixture", protected=protected)
                new = store.save_memory(NEW, key="delegation.model", protected=True)
                store._facts._apply_supersessions = apply
                result["explicit" if protected else "inferred"] = scenario(store, old.id, new.id, "project:fixture")
        result["proposal_withdrawal"] = {"after": proposal_withdrawal(root / "proposal-after")}
        if args.baseline_ref:
            # A trusted repository revision, isolated to disposable stores.
            source = subprocess.check_output(["git", "show", args.baseline_ref + ":fluid/fluid/codemode/facts.py"], text=True)
            module = types.ModuleType("fluid.codemode._supersession_baseline")
            exec(compile(source, "baseline_facts.py", "exec"), module.__dict__)
            result["proposal_withdrawal"]["before"] = proposal_withdrawal(root / "proposal-before", module.FactStore)
    current = {"_SYNTH_SYSTEM": _SYNTH_SYSTEM, "_CONSOLIDATE_SYSTEM": _CONSOLIDATE_SYSTEM}
    sections = {key: {"chars": len(text), "tokens_est": math.ceil(len(text)/4)} for key,text in current.items()}
    if args.baseline_ref:
        tree = ast.parse(subprocess.check_output(["git", "show", args.baseline_ref + ":fluid/fluid/codemode/continual.py"], text=True))
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in current:
                        text = ast.literal_eval(node.value)
                        sections[target.id].update(before_chars=len(text), before_tokens_est=math.ceil(len(text)/4))
    result["learner_prompt_sections"] = sections
    print(json.dumps(result, indent=2))

if __name__ == "__main__":
    main()
