"""A Claude caller picks a Claude model per Task, like a provider-row caller.

Native targets (no provider) run on Claude: the Claude Code CLI on a
subscription, or the Anthropic API with a key. A Task ``model`` may be an eligible
Claude family name, or an unambiguous ID/display name from an enabled provider, which
moves that one worker to that provider (#1830, #2030). The user's message in the
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
    db.create_project("task-project", "Task project", None, str(tmp_path))
    db.create_session(session_id="parent", profile_id="caller", title="Parent", project_id="task-project")
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


async def test_a_native_family_beside_a_provider_version_keeps_owner_consent(native, monkeypatch):
    from app.core.agent_types import PermissionResultDeny
    from app.core.permission_handler import permission_handler
    from tests.core.test_task_model_choices import _user_says

    _, seen = native
    _user_says("parent", "Try haiku, opus, fable or gpt-5.6-sol for this.")
    prompts = []

    async def deny(**kwargs):
        prompts.append(kwargs)
        return PermissionResultDeny(message="User denied permission")

    monkeypatch.setattr(permission_handler, "request_permission", deny)
    result = await td.dispatch_task_tool(
        {"agent": "helper", "prompt": "go", "model": "haiku", "mode": "sync"},
        caller_session_id="parent", caller_provider_id=None,
    )
    assert result["ok"] and result["model_override"]["source"] == "named_by_user"
    assert not prompts and seen[0]["model_id"] == "haiku"


def test_native_caller_contract_takes_any_model_string(native):
    contract = apm.build_task_contract("caller", None)
    assert "enum" not in contract.input_schema["properties"]["model"]


@pytest.mark.parametrize("model, expected", [
    ("haiku", "haiku"),
    ("opus", "opus"),
    (" fable ", "fable"),
    ("H a i k u", "haiku"),
    ("O-p-u-s", "opus"),
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


@pytest.mark.parametrize("model", [
    "claude-haiku-5-5", "Haiku 5.5", "claude-opus-5-5", "Opus 5.5",
    "claude-opus-4-7", "claude-sonnet-4-6", "opus[1m]",
])
async def test_explicit_versioned_native_names_require_a_family_name(native, model):
    # Owner decision (2026-11-03): explicit overrides must not silently upgrade
    # a pinned Claude version. Saved profile migration is a separate path.
    _, seen = native
    result = await td.dispatch_task_tool(
        {"agent": "helper", "prompt": "go", "model": model, "mode": "sync"},
        caller_session_id="parent", caller_provider_id=None,
    )
    assert not result["ok"]
    assert "use a family name" in result["error"]
    assert "Nothing was dispatched" in result["error"]
    assert not seen


async def test_native_dispatch_without_a_model_keeps_the_stored_assignment(native):
    _, seen = native
    result = await td.dispatch_task_tool(
        {"agent": "helper", "prompt": "go", "mode": "sync"},
        caller_session_id="parent", caller_provider_id=None,
    )
    assert result["ok"], result
    assert seen[0]["model_id"] == "sonnet"
    assert seen[0]["provider_id"] is None


@pytest.mark.parametrize("model", ["gpt-99-imaginary", "claude-3-opus", "prov:chatgpt:unlisted"])
async def test_native_dispatch_refuses_a_model_this_install_cannot_run(native, model):
    _, seen = native
    result = await td.dispatch_task_tool(
        {"agent": "helper", "prompt": "go", "model": model, "mode": "sync"},
        caller_session_id="parent", caller_provider_id=None,
    )
    assert not result["ok"]
    assert "cannot run on this worker" in result["error"]
    assert not seen


async def test_mixed_roster_resolves_owner_named_models_across_enabled_providers(native):
    """An unambiguous bare name can move a worker off its pin, like a prov: id."""
    db, seen = native
    db.create_profile(
        "gpt-helper", "GPT Helper", None,
        {"provider_models": {apm.NATIVE_PROVIDER_KEY: "prov:chatgpt:gpt-5.6-luna"}},
    )
    contract = apm.build_task_contract("caller", None)
    assert "enum" not in contract.input_schema["properties"]["agent"]
    assert "gpt-helper" in {target.id for target in contract.targets}

    for agent, model, ok in [
        ("gpt-helper", "haiku", True),        # a Claude alias moves it to Claude
        ("helper", "gpt-5.6-sol", True),      # an unambiguous id moves it to ChatGPT
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
        ("chatgpt", "gpt-5.6-sol"),
        (None, "haiku"),
        ("chatgpt", "gpt-5.6-sol"),
    ]


@pytest.mark.parametrize("wording", ["gpt-5.6-sol", "GPT 5.6 SOL"])
async def test_owner_wording_resolves_a_providers_model_without_fabricating_an_id(native, wording):
    _, seen = native
    result = await td.dispatch_task_tool(
        {"agent": "helper", "prompt": "go", "model": wording, "mode": "sync"},
        caller_session_id="parent", caller_provider_id=None,
    )
    assert result["ok"], result
    assert result["model_override"]["source"] == "named_by_user"
    assert [(s["provider_id"], s["model_id"]) for s in seen] == [("chatgpt", "gpt-5.6-sol")]


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
