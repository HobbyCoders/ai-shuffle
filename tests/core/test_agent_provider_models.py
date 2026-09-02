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
    assert roster[0].provider_pinned is False
    assert roster[0].provider_id is None


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


def test_contract_documents_the_sync_result_shape(mock_db):
    """R4(a): every audited delegating session burned turns rediscovering this
    by KeyError, so the contract states it up front."""
    from app.db import database

    database.create_profile("caller", "Caller", None, {"model": "opus"})
    database.create_profile("helper", "Helper", None, {"model": "sonnet"})

    description = build_task_contract("caller", None).description

    assert "report_head" in description
    assert "report_chars" in description
    assert "report_path" in description
    assert "NO 'report' key" in description
    assert "Read(offset=..., limit=...)" in description


def test_background_default_targets_lose_the_mode_parameter(mock_db):
    """R4(b): when every reachable target is Explore-class the model cannot
    pass mode at all — the key is simply not in the schema."""
    from app.db import database

    database.create_profile(
        "caller",
        "Caller",
        None,
        {
            "model": "opus",
            "agent_invoke_mode": "custom",
            "enabled_agents": ["explorer"],
        },
    )
    database.create_profile(
        "explorer",
        "Explorer",
        None,
        {"model": "sonnet", "allowed_tools": ["Read", "Grep", "Glob"]},
    )

    contract = build_task_contract("caller", None)

    assert [t.background_default for t in contract.targets] == [True]
    assert "mode" not in contract.input_schema["properties"]
    assert "there is no mode parameter" in contract.description


def test_mixed_roster_keeps_mode_and_names_the_targets_that_ignore_it(mock_db):
    from app.db import database

    database.create_profile(
        "caller",
        "Caller",
        None,
        {
            "model": "opus",
            "agent_invoke_mode": "custom",
            "enabled_agents": ["explorer", "editor"],
        },
    )
    database.create_profile(
        "explorer",
        "Explorer",
        None,
        {"model": "sonnet", "allowed_tools": ["Read", "Grep", "Glob"]},
    )
    database.create_profile(
        "editor",
        "Editor",
        None,
        {"model": "sonnet", "allowed_tools": ["Read", "Edit"]},
    )

    contract = build_task_contract("caller", None)
    flags = {t.id: t.background_default for t in contract.targets}
    assert flags == {"explorer": True, "editor": False}

    mode = contract.input_schema["properties"]["mode"]
    assert mode["enum"] == ["sync", "background"]
    assert "omit mode" in mode["description"]
    assert "cannot take a single further step" in mode["description"]
    assert "explorer" in mode["description"]
    assert "always run in the background and ignore mode" in contract.description


def test_unrestricted_roster_keeps_plain_mode_guidance(mock_db):
    from app.db import database

    database.create_profile(
        "caller",
        "Caller",
        None,
        {
            "model": "opus",
            "agent_invoke_mode": "custom",
            "enabled_agents": ["helper"],
        },
    )
    database.create_profile("helper", "Helper", None, {"model": "sonnet"})

    contract = build_task_contract("caller", None)

    assert [t.background_default for t in contract.targets] == [False]
    assert "mode" in contract.input_schema["properties"]
    assert "Ignored for these always-background targets" not in (
        contract.input_schema["properties"]["mode"]["description"]
    )


def test_file_agent_tools_frontmatter_sets_the_background_default(mock_db):
    definitions = {
        "researcher": {
            "name": "researcher",
            "description": "Reads before reporting.",
            "tools": ["Read", "Grep", "Glob"],
        },
        "builder": {
            "name": "builder",
            "description": "Writes code.",
            "tools": ["Read", "Write"],
        },
        "inheritor": {"name": "inheritor", "description": "No tools declared."},
    }

    flags = {
        target.id: target.background_default
        for target in file_subagent_targets(definitions, "opus")
    }

    assert flags == {"researcher": True, "builder": False, "inheritor": False}


def test_empty_roster_still_exposes_mode(mock_db):
    """No targets means no classification to act on; the schema stays whole."""
    from app.db import database

    database.create_profile(
        "caller",
        "Caller",
        None,
        {"model": "opus", "agent_invoke_mode": "custom", "enabled_agents": []},
    )

    contract = build_task_contract("caller", None)

    assert contract.targets == ()
    assert "mode" in contract.input_schema["properties"]


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


# ─── voice roster ─────────────────────────────────────────────────────────────

def test_decode_voice_binding_shapes():
    from app.core.agent_provider_models import decode_voice_binding

    assert decode_voice_binding("sonnet") == (None, "sonnet")
    assert decode_voice_binding("  opus ") == (None, "opus")
    assert decode_voice_binding("prov:row-1:meta/llama:free") == ("row-1", "meta/llama:free")
    assert decode_voice_binding("prov:broken") is None
    assert decode_voice_binding("") is None
    assert decode_voice_binding(None) is None


def test_voice_roster_is_the_voice_assigned_agents(mock_db, monkeypatch):
    from types import SimpleNamespace
    from app.core import provider_agent as pa
    from app.core.agent_provider_models import (
        VOICE_PROVIDER_KEY,
        available_voice_subagents,
    )
    from app.db import database

    database.create_profile("plain", "Plain", None, {"model": "opus"})
    database.create_profile(
        "native-voice", "Native Voice", "Helps by voice",
        {"provider_models": {NATIVE_PROVIDER_KEY: "opus", VOICE_PROVIDER_KEY: "sonnet"}},
    )
    database.create_model_provider(
        provider_id="row-1", name="Row", provider_type="openrouter",
        base_url="https://x", auth_token="k", enabled=True,
    )
    database.create_profile(
        "bound-voice", "Bound Voice", None,
        {"provider_models": {VOICE_PROVIDER_KEY: "prov:row-1:meta/llama"}},
    )
    database.create_profile(
        "missing-provider", "Missing", None,
        {"provider_models": {VOICE_PROVIDER_KEY: "prov:nope:m"}},
    )
    database.create_profile(
        "read-only", "Explorer", None,
        {"provider_models": {VOICE_PROVIDER_KEY: "haiku"}, "allowed_tools": ["Read", "Grep"]},
    )
    monkeypatch.setattr(
        pa, "validate_provider_model", lambda *a, **kw: SimpleNamespace(ok=True),
    )

    roster = available_voice_subagents()
    by_id = {t.id: t for t in roster}
    assert list(by_id) == ["bound-voice", "read-only", "native-voice"]
    assert all(t.provider_pinned for t in roster)
    assert by_id["native-voice"].provider_id is None
    assert by_id["native-voice"].model == "sonnet"
    assert by_id["bound-voice"].provider_id == "row-1"
    assert by_id["bound-voice"].model == "meta/llama"
    assert by_id["read-only"].background_default is True

    # The voice key never leaks into a provider-scoped roster.
    assert model_for_provider(
        {"provider_models": {VOICE_PROVIDER_KEY: "sonnet"}}, None
    ) is None
    assert available_voice_subagents(exclude_profile_id="native-voice") and \
        "native-voice" not in {t.id for t in available_voice_subagents("native-voice")}


def test_voice_roster_drops_a_disabled_provider(mock_db):
    from app.core.agent_provider_models import (
        VOICE_PROVIDER_KEY,
        available_voice_subagents,
    )
    from app.db import database

    database.create_model_provider(
        provider_id="row-off", name="Off", provider_type="openrouter",
        base_url="https://x", auth_token="k", enabled=False,
    )
    database.create_profile(
        "off-voice", "Off Voice", None,
        {"provider_models": {VOICE_PROVIDER_KEY: "prov:row-off:m"}},
    )
    assert available_voice_subagents() == ()


def test_decode_subagent_binding_shapes():
    from app.core.agent_provider_models import decode_subagent_binding

    assert decode_subagent_binding("__native__", "sonnet") == (None, "sonnet")
    assert decode_subagent_binding("__native__", "prov:row-1:meta/llama:free") == (
        "row-1",
        "meta/llama:free",
    )
    # New UI: a Claude alias under a provider key is the native leg.
    assert decode_subagent_binding("openai-row", "sonnet") == (None, "sonnet")
    assert decode_subagent_binding("openai-row", "claude-opus-4-7") == (
        None,
        "claude-opus-4-7",
    )
    # Legacy: a bare non-Claude id under a provider key stays on that provider.
    assert decode_subagent_binding("openai-row", "gpt-5") == ("openai-row", "gpt-5")
    assert decode_subagent_binding("openai-row", "prov:row-2:llama") == (
        "row-2",
        "llama",
    )
    assert decode_subagent_binding("openai-row", "prov:broken") is None
    assert decode_subagent_binding("openai-row", "") is None
    assert decode_subagent_binding("", "sonnet") is None


def test_native_roster_admits_cross_provider_composite(mock_db, monkeypatch):
    from app.db import database

    database.create_profile(
        "caller",
        "Caller",
        None,
        {"model": "opus", "enabled_agents": ["cross", "native-helper"]},
    )
    database.create_profile(
        "cross",
        "Cross",
        None,
        {"provider_models": {NATIVE_PROVIDER_KEY: "prov:row-1:gpt-5"}},
    )
    database.create_profile(
        "native-helper",
        "Native Helper",
        None,
        {"provider_models": {NATIVE_PROVIDER_KEY: "sonnet"}},
    )
    monkeypatch.setattr(
        database,
        "get_model_provider",
        lambda *args, **kwargs: {"id": "row-1", "enabled": True},
    )
    monkeypatch.setattr(
        "app.core.provider_agent.validate_provider_model",
        lambda *a, **kw: SimpleNamespace(ok=True),
    )

    roster = available_subagents("caller", None)
    by_id = {t.id: t for t in roster}
    assert set(by_id) == {"cross", "native-helper"}
    assert by_id["cross"].model == "gpt-5"
    assert by_id["cross"].provider_id == "row-1"
    assert by_id["cross"].provider_pinned is True
    assert by_id["native-helper"].model == "sonnet"
    assert by_id["native-helper"].provider_pinned is False
    assert by_id["native-helper"].provider_id is None


def test_provider_roster_pins_native_alias_and_other_provider(mock_db, monkeypatch):
    from app.db import database

    database.create_profile("caller", "Caller", None, {"model": "sonnet"})
    database.create_profile(
        "claude-on-openai",
        "Claude On OpenAI",
        None,
        {"provider_models": {"provider-1": "sonnet"}},
    )
    database.create_profile(
        "other-leg",
        "Other Leg",
        None,
        {"provider_models": {"provider-1": "prov:row-2:llama"}},
    )
    database.create_profile(
        "legacy-same",
        "Legacy Same",
        None,
        {"provider_models": {"provider-1": "gpt-5"}},
    )

    def get_provider(provider_id, *args, **kwargs):
        return {"id": provider_id, "enabled": True}

    monkeypatch.setattr(database, "get_model_provider", get_provider)
    monkeypatch.setattr(
        "app.core.provider_agent.validate_provider_model",
        lambda *a, **kw: SimpleNamespace(ok=True),
    )

    roster = available_subagents("caller", "provider-1")
    by_id = {t.id: t for t in roster}
    assert set(by_id) == {"claude-on-openai", "other-leg", "legacy-same"}
    assert by_id["claude-on-openai"].model == "sonnet"
    assert by_id["claude-on-openai"].provider_id is None
    assert by_id["claude-on-openai"].provider_pinned is True
    assert by_id["other-leg"].model == "llama"
    assert by_id["other-leg"].provider_id == "row-2"
    assert by_id["other-leg"].provider_pinned is True
    assert by_id["legacy-same"].model == "gpt-5"
    assert by_id["legacy-same"].provider_pinned is False
    assert by_id["legacy-same"].provider_id is None


def test_disabled_target_provider_drops_only_that_agent(mock_db, monkeypatch):
    from app.db import database

    database.create_profile(
        "caller",
        "Caller",
        None,
        {"model": "opus", "enabled_agents": ["good", "bad"]},
    )
    database.create_profile(
        "good",
        "Good",
        None,
        {"provider_models": {NATIVE_PROVIDER_KEY: "sonnet"}},
    )
    database.create_profile(
        "bad",
        "Bad",
        None,
        {"provider_models": {NATIVE_PROVIDER_KEY: "prov:row-off:m"}},
    )

    def get_provider(provider_id, *args, **kwargs):
        return {"id": provider_id, "enabled": provider_id != "row-off"}

    monkeypatch.setattr(database, "get_model_provider", get_provider)
    monkeypatch.setattr(
        "app.core.provider_agent.validate_provider_model",
        lambda *a, **kw: SimpleNamespace(ok=True),
    )

    roster = available_subagents("caller", None)
    assert [t.id for t in roster] == ["good"]
