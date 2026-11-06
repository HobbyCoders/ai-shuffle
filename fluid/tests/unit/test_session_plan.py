"""Session intent transitions and standalone persistence; no provider or database."""
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fluid.tools.builtins import plan, note, recall, register_builtins
from fluid.tools.registry import ToolCtx, ToolRegistry
from fluid.cli.prompt import PromptProfile, compose_base_prompt, mentioned_tool_names


def start(**overrides):
    return {"action": "update", "request_id": "start", "expected_revision": 0,
            "goal": "Ship the requested fix", "done_when": "Targeted checks pass",
            "tasks": [{"id": "a", "title": "Implement", "status": "pending"},
                      {"id": "b", "title": "Check", "status": "pending", "depends_on": ["a"]}], **overrides}


def test_atomic_updates_revisions_retries_and_no_mutation_on_error():
    original = plan.empty_plan()
    document, receipts, result, changed = plan.apply_update(original, [], start())
    assert changed and document["revision"] == 1 and original["tasks"] == []
    assert "plan" not in result and result["status_source"] == "model_reported"
    replay = plan.apply_update(document, receipts, start())
    assert replay[2]["replayed"] and not replay[3]
    before = deepcopy(document)
    for args, error in ((start(goal="changed"), "request_id"),
                        (start(request_id="stale"), "Revision conflict")):
        with pytest.raises(ValueError, match=error):
            plan.apply_update(document, receipts, args)
        assert document == before


@pytest.mark.parametrize("tasks,match", [
    ([{"id": "a", "title": "x", "status": "bogus"}], "status"),
    ([{"id": "a", "title": "x", "status": "blocked"}], "blocked_reason"),
    ([{"id": "a", "title": "x", "status": "pending", "depends_on": ["missing"]}], "Unknown task dependency"),
    ([{"id": "a", "title": "x", "status": "pending", "depends_on": ["b"]},
      {"id": "b", "title": "y", "status": "pending", "depends_on": ["a"]}], "cycle"),
    ([{"id": "a", "title": "x", "status": "cancelled"},
      {"id": "b", "title": "y", "status": "completed", "depends_on": ["a"]}], "cancellation is not success"),
    ([{"id": "a", "title": "x" * 201, "status": "pending"}], "title"),
    ([{"id": "a", "title": "x", "status": "pending", "references": ["x"] * 6}], "references"),
    ([{"id": "a", "title": "x", "status": "pending"}] * 2, "unique"),
    ([{"id": str(i), "title": "x", "status": "pending"} for i in range(51)], "50"),
])
def test_invalid_batch_rejected_whole(tasks, match):
    with pytest.raises(ValueError, match=match):
        plan.apply_update(plan.empty_plan(), [], start(tasks=tasks))


def test_payload_bound_and_receipt_window():
    with pytest.raises(ValueError, match="32KiB"):
        plan.apply_update(plan.empty_plan(), [], start(goal="x" * 33000))
    document, receipts, _, _ = plan.apply_update(plan.empty_plan(), [], start())
    for revision in range(1, 70):
        document, receipts, _, _ = plan.apply_update(document, receipts, {
            "action": "update", "expected_revision": revision, "request_id": str(revision)})
    assert len(receipts) == plan.MAX_RECEIPTS
    with pytest.raises(ValueError, match="Revision conflict"):
        plan.apply_update(document, receipts, start())


def test_rollover_cannot_hide_open_work_and_preserves_monotonic_revision():
    document, receipts, _, _ = plan.apply_update(plan.empty_plan(), [], start())
    rollover = start(expected_revision=1, request_id="next", rollover=True, goal="Second goal")
    with pytest.raises(ValueError, match="all tasks"):
        plan.apply_update(document, receipts, rollover)
    document, receipts, _, _ = plan.apply_update(document, receipts, start(
        expected_revision=1, request_id="finish", tasks=[
            {"id": "a", "title": "Implement", "status": "completed"},
            {"id": "b", "title": "Check", "status": "cancelled", "depends_on": []}]))
    updated, _, _, _ = plan.apply_update(document, receipts, {**rollover, "expected_revision": 2})
    assert updated["revision"] == 3 and updated["generation"] == 2
    assert updated["goal"] == "Second goal" and document["goal"] != updated["goal"]
    assert updated["tasks"][0]["status"] == "pending"


async def test_standalone_fresh_context_and_note_file_untouched(tmp_path):
    ctx = ToolCtx(cwd=str(tmp_path), session_id="s")
    ctx.extra["harness_state_root"] = str(tmp_path / "state")
    await note.handler({"action": "add", "text": "legacy scratch"}, ctx)
    notes = note.read_notes(ctx)
    registry = ToolRegistry()
    register_builtins(registry)
    outcome = await registry.dispatch("Plan", start(), ctx)
    assert not outcome.is_error
    fresh = ToolCtx(cwd=str(tmp_path), session_id="s", extra=dict(ctx.extra))
    got = await plan.handler({"action": "get"}, fresh)
    assert json.loads(got.text)["plan"]["revision"] == 1
    assert "Ship the requested fix" in plan.reminder(fresh)
    assert note.read_notes(ctx) == notes
    other = ToolCtx(cwd=str(tmp_path), session_id="other", extra=dict(ctx.extra))
    assert json.loads((await plan.handler({"action": "get"}, other)).text)["plan"]["revision"] == 0


async def test_host_failure_never_falls_back(tmp_path):
    def denied(args):
        raise ValueError("superseded")
    ctx = ToolCtx(cwd=str(tmp_path), session_id="s", extra={"session_plan": denied})
    outcome = await plan.handler(start(), ctx)
    assert outcome.is_error and outcome.text == "superseded"
    assert not list(tmp_path.rglob("*.sqlite3"))


def test_reminder_is_bounded_and_honest():
    document, _, _, _ = plan.apply_update(plan.empty_plan(), [], start(tasks=[
        {"id": str(i), "title": "x" * 120, "status": "blocked", "blocked_reason": "why" * 60}
        for i in range(50)]))
    text = plan.render_reminder(document)
    assert len(text) <= 2440 and "blocked=50" in text
    assert text.startswith("<system-reminder>\n")
    assert text.endswith("\n</system-reminder>")
    assert "model-reported" in text and "Cancelled is not success" in text


def test_plan_prompt_only_when_offered_no_competing_note_checklist():
    for names in ({"Plan"}, {"Plan", "Ledger"}, {"Note"}, set()):
        text = compose_base_prompt(PromptProfile(tools=frozenset(names)))
        assert mentioned_tool_names(text) <= names
        if "Plan" in names:
            assert text.count(plan.GUIDANCE) == 1
            assert "~5+ real steps or multi-turn" in text and "else skip" in text
        if "Note" in names:
            assert "optional session scratchpad" in text
        assert "Track multi-step work with Note" not in text
        assert "every current step" not in text
    assert "maxItems" not in recall.SPEC.input_schema["properties"]["ids"]
    ids_help = recall.SPEC.input_schema["properties"]["ids"]["description"]
    assert "10 normalized unique" in ids_help and "trimmed; blanks/duplicates ignored" in ids_help
    assert "before relying on it" in recall.describe()
    assert "archived too" in recall.describe() and "saved-skill index" in recall.describe()
    assert "per category with sources" in recall.describe()
    assert "not transcripts" in recall.describe() and "SessionSearch" not in recall.describe(set())


def test_plan_and_note_documentation_budget_and_field_contract():
    from pathlib import Path

    fixture = json.loads((Path(__file__).parents[1] / "fixtures" / "session_plan_contract.json").read_text())

    def without_descriptions(value):
        if isinstance(value, dict):
            return {k: without_descriptions(v) for k, v in value.items() if k != "description"}
        if isinstance(value, list):
            return [without_descriptions(v) for v in value]
        return value

    for spec in (plan.SPEC, note.SPEC):
        assert without_descriptions(spec.input_schema) == fixture["schemas"][spec.name]
        assert spec.risk == "safe"
        ceiling = fixture["description_chars"][spec.name] * (100 + fixture["allowance_percent"]) // 100
        assert len(spec.description) <= ceiling
        paragraphs = [p.strip() for p in spec.description.split("\n\n") if p.strip()]
        assert len(paragraphs) == len(set(paragraphs))


def test_deferred_plan_help_preserves_all_semantic_and_safety_rules():
    # These moved from eager guidance or were deduplicated, not removed.
    help_text = plan.SPEC.description
    for rule in (
        "Session-owned intent, not execution or independent verification",
        "cancelled is not success", "atomically upserts", "omitted tasks survive",
        "goal/done_when acceptance", "revision 0", "expected_revision", "unique request_id",
        "Retry verbatim (last 64 receipts)", "on conflict get before editing",
        "Initial/rollover updates require goal, done_when, tasks",
        "rollover=true only after all old tasks completed/cancelled",
        "host retains history", "IDs stable per generation", "Tasks require id/title/status",
        "max 50/generation, 32KiB payload", "Dependencies must exist, be acyclic",
        "completed before dependents complete", "blocked needs blocked_reason",
        "Evidence/existing execution references optional, not proof",
        "Batch meaningful transitions", "keep blockers visible",
        "Never starts workers, polls or schedules wakes", "Do not copy checklists across stores",
        "If offered: WorkingRecord for owner-wide commitments/results",
        "Ledger for constraints/decisions", "Remember for lasting facts",
    ):
        assert rule in help_text, rule

    for names in ({"Plan"}, {"Plan", "Note"}, {"Plan", "Note", "Ledger", "Remember"}):
        text = compose_base_prompt(PromptProfile(tools=frozenset(names)))
        assert text.count(plan.GUIDANCE) == 1
        assert help_text not in text
        assert "expected_revision" not in text
        assert "every current step" not in text
    assert "use list" in note.describe()  # legacy notes remain accessible
    assert "session Plan" not in note.describe({"Note"})

async def test_recall_cap_counts_normalized_unique_ids_not_raw_array_length():
    seen = []
    def memories(ids, project=None):
        seen.append(ids)
        return []
    ctx = ToolCtx(cwd="/unused", session_id="s", extra={"recall_store": SimpleNamespace(memories_by_id=memories, memories=lambda: [])})
    await recall.handler({"ids": [" a ", "", "a", "  "] * 100}, ctx)
    assert seen == [["a"]]
    rejected = await recall.handler({"ids": [str(i) for i in range(11)]}, ctx)
    assert rejected.is_error and "10" in rejected.text
