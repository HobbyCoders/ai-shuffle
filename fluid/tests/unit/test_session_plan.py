"""Session intent transitions and standalone persistence; no provider or database."""
import json
from copy import deepcopy

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
    assert "model-reported" in text and "Cancelled is not success" in text


def test_plan_prompt_only_when_offered_no_competing_note_checklist():
    for names in ({"Plan"}, {"Plan", "Ledger"}, {"Note"}, set()):
        text = compose_base_prompt(PromptProfile(tools=frozenset(names)))
        assert mentioned_tool_names(text) <= names
        if "Plan" in names:
            assert "skip it for simple requests" in text
            assert "Track multi-step work with Note" not in text
    assert "maxItems" not in recall.SPEC.input_schema["properties"]["ids"]
    assert "normalized unique" in recall.SPEC.input_schema["properties"]["ids"]["description"]
