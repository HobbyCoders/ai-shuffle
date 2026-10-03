"""A Claude caller picks a Claude model per Task, like a provider-row caller.

Native targets (no provider) run on Claude: the Claude Code CLI on a
subscription, or the Anthropic API with a key. Their Task ``model`` choices
are the model registry's aliases; the worker stays native.
"""
import pytest

from app.core import agent_provider_models as apm, task_dispatch as td
from app.core.provider_agent import WorkerOutcome


@pytest.fixture
def native(mock_db, monkeypatch, tmp_path):
    from app.db import database as db
    from app.core import project_resolver

    db.create_model_provider(
        "chatgpt", "ChatGPT", "openai", "https://example.invalid/v1",
        models=[{"id": "gpt-5.6-luna"}, {"id": "gpt-5.6-sol"}],
        default_model="gpt-5.6-luna",
    )
    db.create_profile("caller", "Caller", None, {"model": "opus"})
    db.create_profile("helper", "Helper", None, {"model": "sonnet"})
    db.create_session(session_id="parent", profile_id="caller", title="Parent")
    monkeypatch.setattr(td, "_credential_gap", lambda provider: None)
    monkeypatch.setattr(project_resolver, "resolve_existing_cwd", lambda row: str(tmp_path))
    seen = []

    async def worker(**kwargs):
        seen.append(kwargs)
        return WorkerOutcome(ok=True, session_id="worker", report="done")

    monkeypatch.setattr("app.core.provider_agent.dispatch_worker", worker)
    return db, seen


def test_native_caller_contract_offers_the_claude_aliases(native):
    contract = apm.build_task_contract("caller", None)
    assert contract.input_schema["properties"]["model"]["enum"] == [
        "fable", "haiku", "opus", "sonnet",
    ]


@pytest.mark.parametrize("model, expected", [
    ("haiku", "haiku"),
    ("opus", "opus"),
    (" fable ", "fable"),
    ("claude-haiku-4-5", "haiku"),  # a legacy full id normalizes onto its alias
])
async def test_native_dispatch_runs_the_chosen_claude_model(native, model, expected):
    _, seen = native
    result = await td.dispatch_task_tool(
        {"agent": "helper", "prompt": "go", "model": model, "mode": "sync"},
        caller_session_id="parent", caller_provider_id=None,
    )
    assert result["ok"], result
    assert seen[0]["model_id"] == expected
    assert seen[0]["provider_id"] is None


async def test_native_dispatch_without_a_model_keeps_the_stored_assignment(native):
    _, seen = native
    result = await td.dispatch_task_tool(
        {"agent": "helper", "prompt": "go", "mode": "sync"},
        caller_session_id="parent", caller_provider_id=None,
    )
    assert result["ok"], result
    assert seen[0]["model_id"] == "sonnet"
    assert seen[0]["provider_id"] is None


@pytest.mark.parametrize("model", ["gpt-5.6-sol", "prov:chatgpt:gpt-5.6-sol", "claude-3-opus"])
async def test_native_dispatch_refuses_a_model_claude_does_not_offer(native, model):
    _, seen = native
    result = await td.dispatch_task_tool(
        {"agent": "helper", "prompt": "go", "model": model, "mode": "sync"},
        caller_session_id="parent", caller_provider_id=None,
    )
    assert not result["ok"]
    assert "not a Claude model" in result["error"]
    assert not seen


async def test_mixed_roster_keeps_each_providers_own_list(native):
    """A native caller with one target pinned to a ChatGPT row sees both lists;
    dispatch still checks the choice against the chosen target's own leg."""
    db, seen = native
    db.create_profile(
        "gpt-helper", "GPT Helper", None,
        {"provider_models": {apm.NATIVE_PROVIDER_KEY: "prov:chatgpt:gpt-5.6-luna"}},
    )
    contract = apm.build_task_contract("caller", None)
    assert "gpt-helper" in contract.input_schema["properties"]["agent"]["enum"]
    assert contract.input_schema["properties"]["model"]["enum"] == [
        "fable", "gpt-5.6-luna", "gpt-5.6-sol", "haiku", "opus", "sonnet",
    ]

    for agent, model, ok in [
        ("gpt-helper", "haiku", False),       # a Claude alias on the ChatGPT leg
        ("helper", "gpt-5.6-sol", False),     # a ChatGPT model on the native leg
        ("gpt-helper", "gpt-5.6-sol", True),
        ("helper", "haiku", True),
    ]:
        result = await td.dispatch_task_tool(
            {"agent": agent, "prompt": "go", "model": model, "mode": "sync"},
            caller_session_id="parent", caller_provider_id=None,
        )
        assert result["ok"] is ok, (agent, model, result)

    assert [(s["provider_id"], s["model_id"]) for s in seen] == [
        ("chatgpt", "gpt-5.6-sol"),
        (None, "haiku"),
    ]


async def test_refusal_lists_choices_and_names_the_settings(native):
    """A Sol request on a Claude target says what the target CAN take, where
    Sol is configured, and which settings change either — not just 'no'."""
    _, seen = native
    result = await td.dispatch_task_tool(
        {"agent": "helper", "prompt": "go", "model": "gpt-5.6-sol", "mode": "sync"},
        caller_session_id="parent", caller_provider_id=None,
    )
    assert not result["ok"]
    error = result["error"]
    assert "runs on Claude" in error
    assert "haiku, sonnet, opus, fable" in error
    assert "configured on ChatGPT" in error
    assert "cannot switch providers" in error
    assert "Agents > (agent) > Models" in error
    assert "Settings > Providers" in error
    assert not seen


def test_contract_lists_each_targets_leg_and_its_choices(native):
    db, _ = native
    db.create_profile(
        "gpt-helper", "GPT Helper", None,
        {"provider_models": {apm.NATIVE_PROVIDER_KEY: "prov:chatgpt:gpt-5.6-luna"}},
    )
    description = apm.build_task_contract("caller", None).description
    assert "- helper (default model: sonnet; runs on Claude)" in description
    assert "- gpt-helper (default model: gpt-5.6-luna; runs on ChatGPT)" in description
    assert "- Claude: haiku, sonnet, opus, fable" in description
    assert "- ChatGPT: gpt-5.6-luna, gpt-5.6-sol" in description
    assert apm.TASK_MODEL_SETTINGS_HINT in description


def test_empty_roster_names_where_subagents_are_configured(mock_db):
    description = apm.build_task_contract("nobody", None).description
    assert "No subagents are configured" in description
    assert apm.TASK_MODEL_SETTINGS_HINT in description
