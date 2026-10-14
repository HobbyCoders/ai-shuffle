"""Task model choices (#1827, #1830): any model on an enabled provider's
persisted list, checked at dispatch. A model other than the agent's own runs
only when the user asked for it; here the user's message names the models
(test_model_override.py covers the owner prompt)."""
import pytest

from app.core import agent_provider_models as apm, task_dispatch as td
from app.core.provider_agent import WorkerOutcome


@pytest.fixture
def lane(mock_db, monkeypatch, tmp_path):
    from app.db import database as db
    from app.core import project_resolver

    db.create_model_provider(
        "lane", "Lane", "openai", "https://example.invalid/v1",
        models=[{"id": "astra"}, {"id": "sol"}], default_model="astra",
    )
    db.create_profile("caller", "Caller", None, {"model": "opus"})
    db.create_profile("helper", "Helper", None,
                      {"provider_models": {"lane": "astra"}})
    db.create_session(session_id="parent", profile_id="caller", title="Parent")
    _user_says("parent", "Run it on sol, or on other-choice where that agent runs.")
    monkeypatch.setattr(td, "_credential_gap", lambda provider: None)
    monkeypatch.setattr(project_resolver, "resolve_existing_cwd", lambda row: str(tmp_path))
    seen = []

    async def worker(**kwargs):
        seen.append(kwargs)
        return WorkerOutcome(ok=True, session_id="worker", report="done")

    monkeypatch.setattr("app.core.provider_agent.dispatch_worker", worker)
    return db, seen


def _user_says(session_id, text):
    from app.db import turns
    from app.db.base import get_db

    turn = turns.enqueue_turn(session_id, "user", prompt=text, profile_id="caller")
    with get_db() as conn:
        conn.cursor().execute("UPDATE turns SET state='completed' WHERE id=%s", (turn["id"],))


@pytest.mark.parametrize("surface", ["fluid", "mcp"])
async def test_tool_adapters_preserve_selected_model(lane, surface):
    db, seen = lane
    args = {"agent": "helper", "prompt": "go", "model": "sol", "mode": "sync"}
    if surface == "fluid":
        from fluid.tools.builtins.task import handler
        from fluid.tools.registry import ToolCtx
        result = await handler(args, ToolCtx(extra={
            "task_dispatch": td.make_fluid_dispatcher("parent", "caller", "lane"),
        }))
        assert not result.is_error, result.text
    else:
        from tests.mcp.test_task_tool_contract import _task_handler
        db.create_session(session_id="session-parent", profile_id="caller", title="MCP Parent")
        _user_says("session-parent", "Use sol for this one.")
        result = await _task_handler(profile_id="caller", provider_id="lane")(args)
        assert not result["isError"], result
    assert seen[0]["model_id"] == "sol"
    assert seen[0]["provider_id"] == "lane"


def test_schema_takes_a_string_not_every_provider_model(lane):
    contract = apm.build_task_contract("caller", "lane")
    prop = contract.input_schema["properties"]["model"]
    assert prop["type"] == "string" and "enum" not in prop
    assert "astra" not in prop["description"] and "sol" not in prop["description"]
    assert "- helper (model: astra; runs on Lane)" in contract.description


@pytest.mark.parametrize("model, expected", [("sol", "sol"), (None, "astra"), ("", "astra")])
async def test_dispatch_uses_choice_or_default(lane, model, expected):
    _, seen = lane
    result = await td.dispatch_task_tool(
        {"agent": "helper", "prompt": "go", "model": model},
        caller_session_id="parent", caller_provider_id="lane",
    )
    assert result["ok"], result
    assert seen[0]["model_id"] == expected
    assert seen[0]["provider_id"] == "lane"


@pytest.mark.parametrize("model", ["unlisted", "prov:other:sol", 42, ["sol"]])
async def test_invalid_choice_never_launches(lane, model):
    _, seen = lane
    result = await td.dispatch_task_tool(
        {"agent": "helper", "prompt": "go", "model": model},
        caller_session_id="parent", caller_provider_id="lane",
    )
    assert not result["ok"]
    assert not seen


async def test_removed_choice_is_revalidated_after_schema_creation(lane):
    db, seen = lane
    db.update_model_provider("lane", models_json=[{"id": "astra"}])
    result = await td.dispatch_task_tool(
        {"agent": "helper", "prompt": "go", "model": "sol"},
        caller_session_id="parent", caller_provider_id="lane",
    )
    assert not result["ok"]
    assert "cannot run on this worker" in result["error"]
    assert not seen


async def test_owner_named_override_may_move_away_from_pinned_provider(lane):
    db, seen = lane
    db.create_model_provider("other", "Other", "openai", "https://example.invalid/v1",
                             models=[{"id": "other-default"}, {"id": "other-choice"}])
    db.create_profile("pinned", "Pinned", None,
                      {"provider_models": {"lane": "prov:other:other-default"}})
    # A full, unambiguous owner-named model can move away from Other's pin.
    for model, ok in [("sol", True), ("other-choice", True)]:
        result = await td.dispatch_task_tool(
            {"agent": "pinned", "prompt": "go", "model": model},
            caller_session_id="parent", caller_provider_id="lane",
        )
        assert result["ok"] is ok, result
    assert [(s["provider_id"], s["model_id"]) for s in seen] == [
        ("lane", "sol"), ("other", "other-choice")]


async def test_disabled_provider_and_invoke_allowlist_still_refuse(lane):
    db, seen = lane
    db.update_profile("caller", config={"model": "opus", "agent_invoke_mode": "custom", "enabled_agents": []})
    result = await td.dispatch_task_tool(
        {"agent": "helper", "prompt": "go", "model": "sol"},
        caller_session_id="parent", caller_provider_id="lane",
    )
    assert not result["ok"]
    db.update_profile("caller", config={"model": "opus", "agent_invoke_mode": "all"})
    db.update_model_provider("lane", enabled=False)
    result = await td.dispatch_task_tool(
        {"agent": "helper", "prompt": "go", "model": "sol"},
        caller_session_id="parent", caller_provider_id="lane",
    )
    assert not result["ok"]
    assert not seen


async def test_file_agent_choice_keeps_callers_provider(lane):
    db, seen = lane
    # A valid explicit choice does not require a fallback/default model.
    db.update_model_provider("lane", default_model=None)
    registry = {"reader": {"name": "reader", "description": "Read files", "prompt": "Read files"}}
    contract = apm.build_task_contract("caller", "lane", apm.file_subagent_targets(registry, "astra"))
    assert "enum" not in contract.input_schema["properties"]["agent"]
    assert "reader" in {target.id for target in contract.targets}
    result = await td.dispatch_task_tool(
        {"agent": "reader", "prompt": "go", "model": "sol"},
        caller_session_id="parent", caller_provider_id="lane", agent_registry=registry,
    )
    assert result["ok"], result
    assert seen[0]["model_id"] == "sol"
    assert seen[0]["provider_id"] == "lane"


async def test_uncurated_refusal_says_it_cannot_run_here(lane):
    _, seen = lane
    result = await td.dispatch_task_tool(
        {"agent": "helper", "prompt": "go", "model": "unlisted"},
        caller_session_id="parent", caller_provider_id="lane",
    )
    assert not result["ok"]
    assert "Model 'unlisted' cannot run on this worker" in result["error"]
    assert not seen
