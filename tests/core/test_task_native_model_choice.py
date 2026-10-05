"""A Claude caller picks a Claude model per Task, like a provider-row caller.

Native targets (no provider) run on Claude: the Claude Code CLI on a
subscription, or the Anthropic API with a key. A Task ``model`` may be any
Claude alias, or another provider's model written as its ``prov:`` id, which
moves that one worker to that provider (#1830). The user's message in the
parent names the models, so no owner prompt is involved here.
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
    from tests.core.test_task_model_choices import _user_says
    _user_says("parent", "Try haiku, opus, fable or gpt-5.6-sol for this.")
    monkeypatch.setattr(td, "_credential_gap", lambda provider: None)
    monkeypatch.setattr(project_resolver, "resolve_existing_cwd", lambda row: str(tmp_path))
    seen = []

    async def worker(**kwargs):
        seen.append(kwargs)
        return WorkerOutcome(ok=True, session_id="worker", report="done")

    monkeypatch.setattr("app.core.provider_agent.dispatch_worker", worker)
    return db, seen


def test_native_caller_contract_takes_any_model_string(native):
    contract = apm.build_task_contract("caller", None)
    assert "enum" not in contract.input_schema["properties"]["model"]


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


@pytest.mark.parametrize("model", ["gpt-5.6-sol", "claude-3-opus", "prov:chatgpt:unlisted"])
async def test_native_dispatch_refuses_a_model_this_install_cannot_run(native, model):
    _, seen = native
    result = await td.dispatch_task_tool(
        {"agent": "helper", "prompt": "go", "model": model, "mode": "sync"},
        caller_session_id="parent", caller_provider_id=None,
    )
    assert not result["ok"]
    assert "cannot run on this worker" in result["error"]
    assert not seen


async def test_mixed_roster_reads_bare_ids_on_each_targets_own_leg(native):
    """A bare id is read on the target's own leg; a Claude alias or a prov: id
    names its own provider and may move a worker the user asked to move."""
    db, seen = native
    db.create_profile(
        "gpt-helper", "GPT Helper", None,
        {"provider_models": {apm.NATIVE_PROVIDER_KEY: "prov:chatgpt:gpt-5.6-luna"}},
    )
    contract = apm.build_task_contract("caller", None)
    assert "gpt-helper" in contract.input_schema["properties"]["agent"]["enum"]

    for agent, model, ok in [
        ("gpt-helper", "haiku", True),        # a Claude alias moves it to Claude
        ("helper", "gpt-5.6-sol", False),     # a bare non-Claude id on the native leg
        ("gpt-helper", "gpt-5.6-sol", True),
        ("helper", "haiku", True),
        ("helper", "prov:chatgpt:gpt-5.6-sol", True),  # moved to ChatGPT
    ]:
        result = await td.dispatch_task_tool(
            {"agent": agent, "prompt": "go", "model": model, "mode": "sync"},
            caller_session_id="parent", caller_provider_id=None,
        )
        assert result["ok"] is ok, (agent, model, result)

    assert [(s["provider_id"], s["model_id"]) for s in seen] == [
        (None, "haiku"),
        ("chatgpt", "gpt-5.6-sol"),
        (None, "haiku"),
        ("chatgpt", "gpt-5.6-sol"),
    ]


async def test_refusal_says_how_to_write_a_providers_model(native):
    """A bare Sol id on a Claude target says how to name a provider's model."""
    _, seen = native
    result = await td.dispatch_task_tool(
        {"agent": "helper", "prompt": "go", "model": "gpt-5.6-sol", "mode": "sync"},
        caller_session_id="parent", caller_provider_id=None,
    )
    assert not result["ok"]
    assert "prov:<providerId>:<modelId>" in result["error"]
    assert not seen


def test_contract_lists_each_targets_leg_and_its_choices(native):
    db, _ = native
    db.create_profile(
        "gpt-helper", "GPT Helper", None,
        {"provider_models": {apm.NATIVE_PROVIDER_KEY: "prov:chatgpt:gpt-5.6-luna"}},
    )
    description = apm.build_task_contract("caller", None).description
    assert "- helper (model: sonnet; runs on Claude)" in description
    assert "- gpt-helper (model: gpt-5.6-luna; runs on ChatGPT)" in description
    assert "Models the owner allows" not in description
    assert apm.TASK_MODEL_SETTINGS_HINT in description


def test_empty_roster_names_where_subagents_are_configured(mock_db):
    description = apm.build_task_contract("nobody", None).description
    assert "No subagents are configured" in description
    assert apm.TASK_MODEL_SETTINGS_HINT in description
