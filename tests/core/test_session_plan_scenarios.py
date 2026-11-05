"""New native Plan fixtures use real handlers/storage, never seed UI or Plan state."""
import json
from pathlib import Path

import pytest
from fluid.tools.builtins import note, register_builtins
from fluid.tools.registry import ToolRegistry
from app.db import session_events, session_plans
from scripts.devloop.fake_ai import Scenarios, validate_scenario
from tests.core.test_session_plan import active_plan  # noqa: F401

SCENARIOS = Path(__file__).resolve().parents[2] / "scripts" / "devloop" / "scenarios"


def fixture(name):
    data = json.loads((SCENARIOS / (name + ".json")).read_text())
    return validate_scenario(name, data)


async def produce(name, registry, context):
    results = []
    for step in fixture(name)["steps"]:
        for call in step.get("tool_calls", []):
            assert call["name"] in {"Plan", "Note"}
            outcome = await registry.dispatch(call["name"], call["input"], context)
            assert not outcome.is_error, outcome.text
            results.append((call, outcome.text))
    return results


def test_new_json_contracts_are_loaded_by_existing_fake_ai():
    loaded = Scenarios(SCENARIOS).load()
    for name in ("plan-update", "plan-complete", "plan-note"):
        assert loaded[name] == fixture(name)
    updates = [call["input"] for name in ("plan-update", "plan-complete")
               for step in fixture(name)["steps"] for call in step.get("tool_calls", [])
               if call["input"]["action"] == "update"]
    assert [args["expected_revision"] for args in updates] == [1, 2, 3]
    assert len({args["request_id"] for args in updates}) == 3
    assert updates[-1]["rollover"] is True
    note_calls = [call for step in fixture("plan-note")["steps"] for call in step.get("tool_calls", [])]
    assert [(c["name"], c["input"]) for c in note_calls] == [
        ("Note", {"action": "list"}), ("Plan", {"action": "get"})]


@pytest.mark.parametrize("has_legacy_note", [False, True], ids=["empty-scratch", "existing-scratch"])
async def test_real_fixture_sequence_preserves_history_and_optional_notes(active_plan, tmp_path, has_legacy_note):
    sid, _run, context = active_plan
    context.extra["harness_state_root"] = str(tmp_path / "state")
    registry = ToolRegistry()
    register_builtins(registry)
    if has_legacy_note:
        result = await registry.dispatch("Note", {"action": "add", "text": "Existing optional research scratch; not a Plan checklist."}, context)
        assert not result.is_error
    previous = note.read_notes(context)
    path = note._notes_path(context)
    previous_bytes = path.read_bytes() if path.exists() else None
    await produce("plan", registry, context)
    assert session_plans.read(sid)["plan"]["revision"] == 1
    await produce("plan-update", registry, context)
    updated = session_plans.read(sid)["plan"]
    assert updated["revision"] == 2
    tasks = {task["id"]: task for task in updated["tasks"]}
    assert tasks["inspect"]["status"] == "completed"
    assert tasks["inspect"]["evidence"] and tasks["inspect"]["references"]
    assert tasks["implement"]["status"] == "blocked" and tasks["implement"]["blocked_reason"]
    assert tasks["verify"]["status"] == "cancelled"
    assert "blocked_reason" not in tasks["verify"]
    await produce("plan-complete", registry, context)
    current = session_plans.read(sid)["plan"]
    assert (current["revision"], current["generation"]) == (4, 2)
    assert {task["id"] for task in current["tasks"]} == {"followup", "review"}
    assert {task["status"] for task in current["tasks"]} == {"in_progress", "pending"}
    history = [event["payload"]["plan"] for event in session_events.get_events(sid) if event["event_type"] == "plan.updated"]
    assert [plan["revision"] for plan in history] == [1, 2, 3, 4]
    assert history[2]["generation"] == 1
    assert {task["status"] for task in history[2]["tasks"]} == {"completed", "cancelled"}
    results = await produce("plan-note", registry, context)
    assert previous in results[0][1]
    assert note.read_notes(context) == previous
    assert (path.read_bytes() if path.exists() else None) == previous_bytes
    assert session_plans.read(sid)["plan"] == current
    assert len([event for event in session_events.get_events(sid) if event["event_type"] == "plan.updated"]) == 4


async def test_complete_fixture_cannot_skip_required_update(active_plan):
    sid, _run, context = active_plan
    registry = ToolRegistry()
    register_builtins(registry)
    await produce("plan", registry, context)
    call = fixture("plan-complete")["steps"][0]["tool_calls"][0]
    outcome = await registry.dispatch(call["name"], call["input"], context)
    assert outcome.is_error and "Revision conflict" in outcome.text
    assert session_plans.read(sid)["plan"]["revision"] == 1
    assert len([event for event in session_events.get_events(sid) if event["event_type"] == "plan.updated"]) == 1
