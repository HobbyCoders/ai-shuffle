"""Task model choices follow the owner's subagent list (#1827), checked against
each provider's persisted model list at dispatch."""
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
    from app.core import subagent_default
    subagent_default.write(None, ["prov:lane:astra", "prov:lane:sol"])
    monkeypatch.setattr(td, "_credential_gap", lambda provider: None)
    monkeypatch.setattr(project_resolver, "resolve_existing_cwd", lambda row: str(tmp_path))
    seen = []

    async def worker(**kwargs):
        seen.append(kwargs)
        return WorkerOutcome(ok=True, session_id="worker", report="done")

    monkeypatch.setattr("app.core.provider_agent.dispatch_worker", worker)
    return db, seen




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
        result = await _task_handler(profile_id="caller", provider_id="lane")(args)
        assert not result["isError"], result
    assert seen[0]["model_id"] == "sol"
    assert seen[0]["provider_id"] == "lane"


def test_schema_offers_the_owners_list_not_every_provider_model(lane):
    db, _ = lane
    contract = apm.build_task_contract("caller", "lane")
    assert contract.input_schema["properties"]["model"]["enum"] == [
        "prov:lane:astra", "prov:lane:sol"]
    assert "- helper (model: astra; runs on Lane)" in contract.description
    # A model the provider adds is not offered until the owner allows it; one
    # it drops leaves the list at once.
    db.update_model_provider("lane", models_json=[{"id": "astra"}, {"id": "new-model"}])
    refreshed = apm.build_task_contract("caller", "lane")
    assert refreshed.input_schema["properties"]["model"]["enum"] == ["prov:lane:astra"]


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
    assert "prov:lane:sol" in apm.build_task_contract("caller", "lane").input_schema["properties"]["model"]["enum"]
    db.update_model_provider("lane", models_json=[{"id": "astra"}])
    result = await td.dispatch_task_tool(
        {"agent": "helper", "prompt": "go", "model": "sol"},
        caller_session_id="parent", caller_provider_id="lane",
    )
    assert not result["ok"]
    assert "not one the owner allows" in result["error"]
    assert not seen


async def test_pinned_provider_not_callers_list_controls_override(lane):
    db, seen = lane
    db.create_model_provider("other", "Other", "openai", "https://example.invalid/v1",
                             models=[{"id": "other-default"}, {"id": "other-choice"}])
    db.create_profile("pinned", "Pinned", None,
                      {"provider_models": {"lane": "prov:other:other-default"}})
    from app.core import subagent_default
    subagent_default.write(None, ["prov:lane:astra", "prov:lane:sol", "prov:other:other-choice"])
    contract = apm.build_task_contract("caller", "lane")
    assert contract.input_schema["properties"]["model"]["enum"] == [
        "prov:lane:astra", "prov:lane:sol", "prov:other:other-choice"]
    for model, ok in [("sol", False), ("other-choice", True)]:
        result = await td.dispatch_task_tool(
            {"agent": "pinned", "prompt": "go", "model": model},
            caller_session_id="parent", caller_provider_id="lane",
        )
        assert result["ok"] is ok, result
    assert len(seen) == 1
    assert seen[0]["provider_id"] == "other"
    assert seen[0]["model_id"] == "other-choice"


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
    assert "model" not in apm.build_task_contract("caller", "lane").input_schema["properties"]
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
    assert "reader" in contract.input_schema["properties"]["agent"]["enum"]
    result = await td.dispatch_task_tool(
        {"agent": "reader", "prompt": "go", "model": "sol"},
        caller_session_id="parent", caller_provider_id="lane", agent_registry=registry,
    )
    assert result["ok"], result
    assert seen[0]["model_id"] == "sol"
    assert seen[0]["provider_id"] == "lane"


async def test_uncurated_refusal_lists_the_providers_choices(lane):
    _, seen = lane
    result = await td.dispatch_task_tool(
        {"agent": "helper", "prompt": "go", "model": "unlisted"},
        caller_session_id="parent", caller_provider_id="lane",
    )
    assert not result["ok"]
    assert "not one the owner allows" in result["error"]
    assert "runs on Lane. Choose one of: prov:lane:astra, prov:lane:sol" in result["error"]
    assert "Agents page" in result["error"]
    assert not seen
