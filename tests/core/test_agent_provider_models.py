"""Provider-scoped subagent roster and Task-contract tests."""

from types import SimpleNamespace

from app.core.agent_provider_models import (
    NATIVE_PROVIDER_KEY,
    available_subagents,
    build_task_contract,
    file_subagent_targets,
    model_for_provider,
    normalized_provider_models,
    parent_subagent_guidance,
    resolve_subagent,
)


def test_legacy_binding_is_provider_local():
    native = {"model": "sonnet"}
    openai = {"provider": "openai-row", "model": "gpt-5"}

    assert normalized_provider_models(native) == {NATIVE_PROVIDER_KEY: "sonnet"}
    assert model_for_provider(native, None) == "sonnet"
    assert model_for_provider(native, "openai-row") is None
    assert model_for_provider(openai, "openai-row") == "gpt-5"
    assert model_for_provider(openai, None) is None


def test_explicit_assignments_preserve_legacy_default():
    config = {
        "provider": "openai-row",
        "model": "gpt-default",
        "provider_models": {
            NATIVE_PROVIDER_KEY: "opus",
            "openai-row": "gpt-worker",
            "": "ignored",
        },
    }

    assert normalized_provider_models(config) == {
        NATIVE_PROVIDER_KEY: "opus",
        "openai-row": "gpt-worker",
    }


def test_native_roster_applies_allowlist_and_excludes_self(mock_db):
    from app.db import database

    database.create_profile(
        "caller",
        "Caller",
        None,
        {"model": "opus", "enabled_agents": ["native-helper", "openai-only"]},
    )
    database.create_profile(
        "native-helper",
        "Native Helper",
        "Checks native work",
        {"model": "sonnet"},
    )
    database.create_profile(
        "openai-only",
        "OpenAI Only",
        None,
        {"provider": "openai-row", "model": "gpt-5"},
    )
    database.create_profile("unlisted", "Unlisted", None, {"model": "haiku"})

    roster = available_subagents("caller", None)

    assert [target.id for target in roster] == ["native-helper"]
    assert roster[0].model == "sonnet"


def test_provider_roster_rejects_uncurated_assignment(mock_db, monkeypatch):
    from app.db import database

    database.create_profile("caller", "Caller", None, {"model": "sonnet"})
    database.create_profile(
        "good",
        "Good",
        None,
        {"provider_models": {"provider-1": "gpt-5"}},
    )
    database.create_profile(
        "stale",
        "Stale",
        None,
        {"provider_models": {"provider-1": "removed-model"}},
    )
    monkeypatch.setattr(
        database,
        "get_model_provider",
        lambda *args, **kwargs: {"id": "provider-1", "enabled": True},
    )

    def validate(_provider_id, model, **kwargs):
        return SimpleNamespace(ok=model == "gpt-5")

    monkeypatch.setattr("app.core.provider_agent.validate_provider_model", validate)

    roster = available_subagents("caller", "provider-1")

    assert [(target.id, target.model) for target in roster] == [("good", "gpt-5")]


def test_disabled_provider_has_no_roster(mock_db, monkeypatch):
    from app.db import database

    database.create_profile("caller", "Caller", None, {"model": "sonnet"})
    database.create_profile(
        "helper",
        "Helper",
        None,
        {"provider_models": {"disabled-row": "gpt-5"}},
    )
    monkeypatch.setattr(
        database,
        "get_model_provider",
        lambda *args, **kwargs: {"id": "disabled-row", "enabled": False},
    )

    assert available_subagents("caller", "disabled-row") == ()


def test_task_contract_is_roster_bound_and_has_no_model_override(mock_db):
    from app.db import database

    database.create_profile("caller", "Caller", None, {"model": "opus"})
    database.create_profile(
        "reviewer",
        "Reviewer",
        "Reviews correctness and risk.",
        {"model": "sonnet"},
    )

    contract = build_task_contract("caller", None)

    assert "reviewer" in contract.input_schema["properties"]["agent"]["enum"]
    assert "model" not in contract.input_schema["properties"]
    assert "subagent" in contract.description.lower()
    assert "reviewer" in contract.description
    assert "sonnet" in contract.description
    guidance = parent_subagent_guidance("caller", None)
    assert "# Subagent delegation" in guidance
    assert "current provider" in guidance
    assert "Never invent a subagent ID, model, or provider" in guidance


def test_task_contract_includes_file_subagents_without_shadowing_db_agents(mock_db):
    from app.db import database

    database.create_profile("caller", "Caller", None, {"model": "opus"})
    database.create_profile("reviewer", "Reviewer", None, {"model": "sonnet"})
    definitions = {
        "researcher": {
            "name": "researcher",
            "description": "Reads the workspace before reporting.",
        },
        "reviewer": {
            "name": "Reviewer",
            "description": "Must not shadow the DB profile.",
        },
    }

    contract = build_task_contract(
        "caller",
        None,
        file_subagent_targets(definitions, "opus"),
    )

    target_models = {target.id: target.model for target in contract.targets}
    assert target_models["reviewer"] == "sonnet"
    assert target_models["researcher"] == "opus"
    assert [target.id for target in contract.targets].count("reviewer") == 1


def test_resolve_subagent_never_escapes_roster(mock_db):
    from app.db import database

    database.create_profile("caller", "Caller", None, {"model": "opus"})
    database.create_profile("helper", "Helper", None, {"model": "sonnet"})
    roster = available_subagents("caller", None)

    target, error = resolve_subagent("Helper", roster)
    assert error is None and target and target.id == "helper"

    target, error = resolve_subagent("not-configured", roster)
    assert target is None
    assert "current provider" in error


def test_duplicate_subagent_names_require_an_id(mock_db):
    from app.db import database

    database.create_profile("caller", "Caller", None, {"model": "opus"})
    database.create_profile("helper-a", "Helper", None, {"model": "sonnet"})
    database.create_profile("helper-b", "Helper", None, {"model": "haiku"})
    roster = available_subagents("caller", None)

    target, error = resolve_subagent("Helper", roster)

    assert target is None
    assert "ambiguous" in error
    assert "helper-a" in error and "helper-b" in error
