"""One global subagent default in place of a required model per agent (#1827).

Resolution when a subagent starts: a model the lead names for this task (only
from the owner's list, the default or the agent's pin), else the agent's pin
for the caller's provider, else the default, else a refusal that asks the
owner once. Never the session's model.
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

async def test_explicit_model_from_the_allowed_list_wins(lane):
    _, seen, _ = lane
    subagent_default.write("sonnet", ["opus"])
    result = await _task({"agent": "pinned", "model": "opus"})
    assert result["ok"], result
    assert (seen[0]["provider_id"], seen[0]["model_id"]) == (None, "opus")


@pytest.mark.parametrize("model", ["sonnet", "haiku"])
async def test_explicit_model_equal_to_the_default_or_the_pin_is_accepted(lane, model):
    _, seen, _ = lane
    subagent_default.write("sonnet", [])
    result = await _task({"agent": "pinned", "model": model})
    assert result["ok"], result
    assert seen[0]["model_id"] == model


async def test_explicit_model_outside_the_list_is_refused(lane):
    _, seen, _ = lane
    subagent_default.write("sonnet", ["haiku"])
    result = await _task({"agent": "plain", "model": "opus"})
    assert not result["ok"]
    assert "not one the owner allows" in result["error"]
    assert "Choose one of: sonnet, haiku" in result["error"]
    assert not seen


async def test_pin_beats_the_default(lane):
    _, seen, _ = lane
    subagent_default.write("sonnet", [])
    result = await _task({"agent": "pinned"})
    assert result["ok"], result
    assert seen[0]["model_id"] == "haiku"


async def test_unpinned_agent_runs_on_the_default(lane):
    _, seen, _ = lane
    subagent_default.write("sonnet", [])
    result = await _task({"agent": "plain"})
    assert result["ok"], result
    assert (seen[0]["provider_id"], seen[0]["model_id"]) == (None, "sonnet")


async def test_changing_the_default_changes_every_unpinned_agent(lane):
    _, seen, _ = lane
    subagent_default.write("sonnet", [])
    await _task({"agent": "plain"})
    subagent_default.write("opus", [])
    await _task({"agent": "plain"})
    await _task({"agent": "pinned"})
    assert [s["model_id"] for s in seen] == ["sonnet", "opus", "haiku"]


async def test_a_default_on_another_provider_pins_that_provider(lane):
    """Like an assignment that names another leg: the worker runs there."""
    _, seen, _ = lane
    subagent_default.write("prov:lane:sol", [])
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


async def test_no_default_still_takes_an_allowed_model_for_this_task(lane):
    _, seen, _ = lane
    subagent_default.write(None, ["haiku"])
    refused = await _task({"agent": "plain"})
    assert "pass model= one of the models the owner allows" in refused["error"]
    assert "haiku" in refused["error"]
    result = await _task({"agent": "plain", "model": "haiku"})
    assert result["ok"], result
    assert seen[0]["model_id"] == "haiku"


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
    subagent_default.write("sonnet", [])
    assert subagent_default._asked is False


async def test_file_agent_uses_the_default_on_its_own_leg_else_the_session(lane):
    _, seen, _ = lane
    registry = {"reader": {"name": "reader", "description": "Read", "prompt": "Read"}}
    db = lane[0]
    db.update_session("parent", launched_model="opus")
    # No default: the file agent keeps borrowing the caller's model, as before.
    await _task({"agent": "reader"}, agent_registry=registry)
    subagent_default.write("sonnet", [])
    await _task({"agent": "reader"}, agent_registry=registry)
    # A default on another leg is not taken: file agents never switch legs.
    subagent_default.write("prov:lane:sol", [])
    await _task({"agent": "reader"}, agent_registry=registry)
    assert [(s["provider_id"], s["model_id"]) for s in seen] == [
        (None, "opus"), (None, "sonnet"), (None, "opus")]


def test_voice_roster_ignores_the_default(lane):
    db, _, _ = lane
    subagent_default.write("sonnet", [])
    db.create_profile("voiced", "Voiced", None, {"provider_models": {"__voice__": "haiku"}})
    roster = apm.available_voice_subagents()
    assert [(t.id, t.model) for t in roster] == [("voiced", "haiku")]


# ---------------------------------------------------------------------------
# Task tool contract
# ---------------------------------------------------------------------------

def test_task_model_choices_are_the_default_plus_allowed(lane):
    subagent_default.write("sonnet", ["haiku", "prov:lane:sol", "haiku"])
    contract = apm.build_task_contract("caller", None)
    prop = contract.input_schema["properties"]["model"]
    assert prop["enum"] == ["sonnet", "haiku", "prov:lane:sol"]
    assert "Omitted, the agent runs on its own pinned model" in prop["description"]
    assert "never runs on this session's model" in prop["description"]
    assert "- plain (model: sonnet, the subagent default; runs on Claude)" in contract.description
    assert "- pinned (model: haiku; runs on Claude)" in contract.description


def test_without_a_default_unpinned_agents_are_listed_as_needing_one(lane):
    contract = apm.build_task_contract("caller", None)
    assert "model" not in contract.input_schema["properties"]
    assert "plain" in contract.input_schema["properties"]["agent"]["enum"]
    assert "- plain (no model: not pinned and the owner has not chosen" in contract.description


def test_an_unrunnable_default_reads_as_none(lane):
    db, _, _ = lane
    subagent_default.write("prov:lane:sol", ["prov:lane:astra"])
    db.update_model_provider("lane", enabled=False)
    by_id = {t.id: t for t in apm.available_subagents("caller", None)}
    assert by_id["plain"].model_source == "unset"
    assert apm.task_model_allowed_choices() == ()


# ---------------------------------------------------------------------------
# Validation and storage
# ---------------------------------------------------------------------------

def test_write_normalizes_and_validates(lane):
    saved = subagent_default.write("claude-opus-4-6", ["prov:lane:sol", "haiku", "haiku"])
    assert saved == subagent_default.SubagentDefault("opus", ("prov:lane:sol", "haiku"))
    assert subagent_default.read() == saved


@pytest.mark.parametrize("model, allowed", [
    ("gpt-5", []),                     # a bare non-Claude id
    ("prov:lane:unlisted", []),        # not curated on the provider
    ("prov:missing:sol", []),          # no such provider
    ("prov:lane", []),                 # malformed composite
    ("claude-3-opus", []),             # not a Claude model the app offers
    (None, ["haiku", "prov:lane:nope"]),
])
def test_write_refuses_models_this_install_cannot_run(lane, model, allowed):
    subagent_default.write("sonnet", ["haiku"])
    with pytest.raises(ValueError):
        subagent_default.write(model, allowed)
    assert subagent_default.read().model == "sonnet"


def test_write_caps_the_allowed_list(lane):
    with pytest.raises(ValueError):
        subagent_default.write(None, ["haiku"] * (subagent_default.MAX_ALLOWED + 1))


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
