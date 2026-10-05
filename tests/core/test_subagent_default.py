"""One global subagent default in place of a required model per agent (#1827).

Resolution when a subagent starts: a model named for this task (used only
when the user asked for it, #1830; see test_model_override.py), else the
agent's pin for the caller's provider, else the default, else a refusal that
asks the owner once. Never the session's model.
"""
import asyncio

import pytest

from app.core import agent_provider_models as apm, subagent_default, task_dispatch as td
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
    db.create_profile("pinned", "Pinned", None, {"provider_models": {"__native__": "haiku"}})
    db.create_profile("plain", "Plain", None, {"description": "no model of its own"})
    db.create_session(session_id="parent", profile_id="caller", title="Parent")
    monkeypatch.setattr(td, "_credential_gap", lambda provider: None)
    monkeypatch.setattr(project_resolver, "resolve_existing_cwd", lambda row: str(tmp_path))
    subagent_default.reset_asked()
    asked = []
    monkeypatch.setattr(subagent_default, "ask_owner_once",
                        lambda session_id=None: asked.append(session_id) or True)
    seen = []

    async def worker(**kwargs):
        seen.append(kwargs)
        return WorkerOutcome(ok=True, session_id="worker", report="done")

    monkeypatch.setattr("app.core.provider_agent.dispatch_worker", worker)
    return db, seen, asked


async def _task(args, provider=None, **kwargs):
    return await td.dispatch_task_tool(
        {"prompt": "go", "mode": "sync", **args},
        caller_session_id="parent", caller_provider_id=provider, **kwargs,
    )


# ---------------------------------------------------------------------------
# Resolution order
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("agent, model", [("pinned", "haiku"), ("plain", "sonnet")])
async def test_explicit_model_equal_to_the_pin_or_default_needs_no_check(lane, monkeypatch, agent, model):
    from app.core import model_override

    async def never(*args, **kwargs):
        raise AssertionError("a model the agent runs on anyway needs no check")

    monkeypatch.setattr(model_override, "confirm", never)
    _, seen, _ = lane
    subagent_default.write("sonnet")
    result = await _task({"agent": agent, "model": model})
    assert result["ok"], result
    assert seen[0]["model_id"] == model
    assert "model_override" not in result


async def test_explicit_model_this_install_cannot_run_is_refused(lane):
    _, seen, _ = lane
    subagent_default.write("sonnet")
    result = await _task({"agent": "plain", "model": "prov:lane:unlisted"})
    assert not result["ok"]
    assert "cannot run on this worker" in result["error"]
    assert "Nothing was dispatched" in result["error"]
    assert not seen


async def test_pin_beats_the_default(lane):
    _, seen, _ = lane
    subagent_default.write("sonnet")
    result = await _task({"agent": "pinned"})
    assert result["ok"], result
    assert seen[0]["model_id"] == "haiku"


async def test_unpinned_agent_runs_on_the_default(lane):
    _, seen, _ = lane
    subagent_default.write("sonnet")
    result = await _task({"agent": "plain"})
    assert result["ok"], result
    assert (seen[0]["provider_id"], seen[0]["model_id"]) == (None, "sonnet")


async def test_changing_the_default_changes_every_unpinned_agent(lane):
    _, seen, _ = lane
    subagent_default.write("sonnet")
    await _task({"agent": "plain"})
    subagent_default.write("opus")
    await _task({"agent": "plain"})
    await _task({"agent": "pinned"})
    assert [s["model_id"] for s in seen] == ["sonnet", "opus", "haiku"]


async def test_a_default_on_another_provider_pins_that_provider(lane):
    """Like an assignment that names another leg: the worker runs there."""
    _, seen, _ = lane
    subagent_default.write("prov:lane:sol")
    result = await _task({"agent": "plain"})
    assert result["ok"], result
    assert (seen[0]["provider_id"], seen[0]["model_id"]) == ("lane", "sol")


async def test_no_default_refuses_and_asks_the_owner_never_the_session_model(lane):
    _, seen, asked = lane
    result = await _task({"agent": "plain"})
    assert not result["ok"]
    assert result["reason"] == "subagent_default_missing"
    assert "owner has not chosen a subagent default" in result["error"]
    assert "never runs on this session's model" in result["error"]
    assert "Agents page" in result["error"]
    assert asked == ["parent"]
    assert not seen


async def test_no_default_refusal_points_at_a_user_named_model(lane):
    _, _, _ = lane
    refused = await _task({"agent": "plain"})
    assert "If the user named a model for this task, pass it as model" in refused["error"]


def test_ask_owner_once_notifies_a_single_time(mock_db, monkeypatch):
    sent = []

    async def emit(user_id, **kwargs):
        sent.append((user_id, kwargs["category"]))

    monkeypatch.setattr("app.services.notification_service.emit_notification", emit)
    subagent_default.reset_asked()

    async def run():
        first = subagent_default.ask_owner_once("s1")
        second = subagent_default.ask_owner_once("s2")
        await asyncio.sleep(0)
        return first, second

    assert asyncio.run(run()) == (True, False)
    assert sent == [("admin", subagent_default.NOTIFICATION_CATEGORY)]
    # Saving a default re-arms it for a later "cleared again".
    subagent_default.write("sonnet")
    assert subagent_default._asked is False


async def test_file_agent_uses_the_default_on_its_own_leg_else_the_session(lane):
    _, seen, _ = lane
    registry = {"reader": {"name": "reader", "description": "Read", "prompt": "Read"}}
    db = lane[0]
    db.update_session("parent", launched_model="opus")
    # No default: the file agent keeps borrowing the caller's model, as before.
    await _task({"agent": "reader"}, agent_registry=registry)
    subagent_default.write("sonnet")
    await _task({"agent": "reader"}, agent_registry=registry)
    # A default on another leg is not taken: file agents never switch legs.
    subagent_default.write("prov:lane:sol")
    await _task({"agent": "reader"}, agent_registry=registry)
    assert [(s["provider_id"], s["model_id"]) for s in seen] == [
        (None, "opus"), (None, "sonnet"), (None, "opus")]


def test_voice_roster_ignores_the_default(lane):
    db, _, _ = lane
    subagent_default.write("sonnet")
    db.create_profile("voiced", "Voiced", None, {"provider_models": {"__voice__": "haiku"}})
    roster = apm.available_voice_subagents()
    assert [(t.id, t.model) for t in roster] == [("voiced", "haiku")]


# ---------------------------------------------------------------------------
# Task tool contract
# ---------------------------------------------------------------------------

def test_task_model_takes_any_model_and_states_the_user_rule(lane):
    subagent_default.write("sonnet")
    contract = apm.build_task_contract("caller", None)
    prop = contract.input_schema["properties"]["model"]
    assert prop["type"] == "string" and "enum" not in prop
    assert "Omitted, the agent runs on its own pinned model" in prop["description"]
    assert "never runs on this session's model" in prop["description"]
    assert "Only for when the user named a model" in prop["description"]
    assert "The host checks that the user asked" in prop["description"]
    assert "The owner's subagent default: sonnet." in contract.description
    assert "- plain (model: sonnet, the subagent default; runs on Claude)" in contract.description
    assert "- pinned (model: haiku; runs on Claude)" in contract.description


def test_without_a_default_unpinned_agents_are_listed_as_needing_one(lane):
    contract = apm.build_task_contract("caller", None)
    assert "enum" not in contract.input_schema["properties"]["model"]
    assert "plain" in contract.input_schema["properties"]["agent"]["enum"]
    assert "- plain (no model: not pinned and the owner has not chosen" in contract.description
    assert "subagent default:" not in contract.description


def test_an_unrunnable_default_reads_as_none(lane):
    db, _, _ = lane
    subagent_default.write("prov:lane:sol")
    db.update_model_provider("lane", enabled=False)
    by_id = {t.id: t for t in apm.available_subagents("caller", None)}
    assert by_id["plain"].model_source == "unset"
    assert subagent_default.effective().model is None


# ---------------------------------------------------------------------------
# Validation and storage
# ---------------------------------------------------------------------------

def test_write_normalizes_and_validates(lane):
    saved = subagent_default.write("claude-opus-4-6")
    assert saved == subagent_default.SubagentDefault("opus")
    assert subagent_default.read() == saved


def test_a_stored_allowed_list_from_before_1830_is_ignored(lane):
    from app.db import database

    database.set_system_setting(
        subagent_default.SETTING_KEY, '{"model": "sonnet", "allowed": ["haiku"]}')
    assert subagent_default.read() == subagent_default.SubagentDefault("sonnet")
    assert subagent_default.payload() == {"model": "sonnet"}


@pytest.mark.parametrize("model", [
    "gpt-5",                 # a bare non-Claude id
    "prov:lane:unlisted",    # not curated on the provider
    "prov:missing:sol",      # no such provider
    "prov:lane",             # malformed composite
    "claude-3-opus",         # not a Claude model the app offers
])
def test_write_refuses_models_this_install_cannot_run(lane, model):
    subagent_default.write("sonnet")
    with pytest.raises(ValueError):
        subagent_default.write(model)
    assert subagent_default.read().model == "sonnet"


# ---------------------------------------------------------------------------
# Upgrade behaviour
# ---------------------------------------------------------------------------

async def test_upgrade_with_pins_and_no_default_seeds_nothing_and_keeps_working(lane):
    _, seen, asked = lane
    assert subagent_default.read() == subagent_default.SubagentDefault()
    result = await _task({"agent": "pinned"})
    assert result["ok"], result
    assert seen[0]["model_id"] == "haiku"
    assert not asked
    assert subagent_default.read() == subagent_default.SubagentDefault()


# ---------------------------------------------------------------------------
# The model a subagent runs on rides its task.updated frames
# ---------------------------------------------------------------------------

def test_task_updated_frames_carry_the_registry_model(monkeypatch):
    from app.contract.events import TaskUpdated
    from app.core import turn_recorder
    from app.core.task_registry import task_registry

    task_registry.start_task("s-model", "w-model", "agent", "Fix it",
                             model="Claude · sonnet", agent="Plain")
    try:
        assert turn_recorder._registry_worker("s-model", "w-model") == {"model": "Claude · sonnet"}
        task_registry.start_task("s-model", "sh-1", "shell", "npm test")
        assert turn_recorder._registry_worker("s-model", "sh-1") == {}
    finally:
        task_registry.clear_session("s-model")
    assert "model" in TaskUpdated.model_fields
