"""Public catalog fixtures: parsing -> persisted metadata -> registry -> effective effort."""
import asyncio
from unittest.mock import AsyncMock
import pytest
from app.core.model_effort import EFFORT_LEVELS, OPENROUTER_EFFORT_LEVELS, effort_capabilities, normalize_effort, sanitize_effort_metadata

# https://openrouter.ai/api/v1/models (public catalog, 2026-09-05)
OR_FIXTURES = [
    ("openai/gpt-6-astra", ["max", "xhigh", "high", "medium", "low"], "medium", True),
    ("openai/gpt-5.6-sol", ["max", "xhigh", "high", "medium", "low", "none"], "medium", False),
    ("z-ai/glm-5.3-flash", ["max", "high", "low"], "max", True),
]

@pytest.mark.parametrize("model,levels,default,mandatory", OR_FIXTURES)
def test_catalog_persist_registry_request(monkeypatch, model, levels, default, mandatory):
    from app.api.model_providers import _parse_models_response
    from app.db.model_providers import _normalize_model_capabilities
    from app.core import model_registry
    from app.db import database
    from app.core.provider import harness_adapter as ha
    from app.core.provider.types import SessionOptions
    entries = _parse_models_response({"data": [{"id": model, "reasoning": {
        "supported_efforts": levels, "default_effort": default, "mandatory": mandatory}}]}, provider_type="openrouter")
    row = {"id": "p", "provider_type": "openrouter", "models_json": _normalize_model_capabilities(entries)}
    monkeypatch.setattr(database, "get_enabled_model_providers", lambda **kw: [row])
    monkeypatch.setattr(ha, "_lookup_provider_row", lambda _: row)
    public = model_registry._providers_payload()[0]["models"][0]
    assert public["effort_levels"] == [v for v in EFFORT_LEVELS if v in levels]
    assert public["default_effort"] == default
    assert public["effort_metadata_version"] == 1
    options = SessionOptions(model=model, extra={"provider_id": "p"})
    for value in EFFORT_LEVELS:
        assert ha._effective_effort(options, value) == (value if value in levels else None)

@pytest.mark.parametrize("mandatory", [True, False])
def test_openrouter_null_and_absence(mandatory):
    cap = sanitize_effort_metadata({"reasoning": {"supported_efforts": None, "mandatory": mandatory}}, provider_type="openrouter", complete=True)["reasoning_capabilities"]
    assert cap["effort_levels"] == [v for v in OPENROUTER_EFFORT_LEVELS if not mandatory or v != "none"]
    assert sanitize_effort_metadata({}, provider_type="openrouter", complete=True)["reasoning_capabilities"]["effort_levels"] == []
    assert sanitize_effort_metadata({"supported_parameters": ["reasoning_effort"]})["reasoning_capabilities"]["effort_levels"] is None

@pytest.mark.parametrize("raw", [None, {}, "high", [None, {}, {"effort": []}]])
def test_malformed_declaration_is_unknown(raw):
    assert sanitize_effort_metadata({"supported_reasoning_levels": raw})["reasoning_capabilities"]["effort_levels"] is None


def test_codex_and_xai_schema():
    # https://github.com/openai/codex/blob/main/codex-rs/protocol/src/openai_models.rs
    codex = {"supported_reasoning_levels": [{"effort": "minimal", "description": "Fast"}, {"effort": "xhigh", "description": "Deep"}], "default_reasoning_level": "xhigh"}
    assert sanitize_effort_metadata(codex)["reasoning_capabilities"] == {"version": 1, "effort_levels": ["minimal", "xhigh"], "default_effort": "xhigh"}
    # https://docs.x.ai/developers/model-capabilities/text/reasoning
    options = {"supportedEfforts": ["low", "medium", "high", "xhigh"], "defaultEffort": "high"}
    expected = {"version": 1, "effort_levels": options["supportedEfforts"], "default_effort": "high"}
    for raw in ({"features": {"reasoningEffortOptions": options}}, {"reasoningEffortOptions": options}, options):
        assert sanitize_effort_metadata(raw)["reasoning_capabilities"] == expected

@pytest.mark.parametrize("route", ["openai_responses", "openrouter", "anthropic", "delegated"])
def test_unknown_routes_preserve_explicit_union(route):
    row = {"provider_type": "custom"}
    assert effort_capabilities("gpt-6-astra", row)["effort_levels"] is None
    for value in EFFORT_LEVELS:
        expected = None if (route in ("anthropic", "delegated") and value in ("none", "minimal")
                            or value == "ultra" and route != "openai_responses") else value
        assert normalize_effort(value, "gpt-6-astra", row, route=route) == expected


@pytest.mark.parametrize("route", ["anthropic", "delegated"])
@pytest.mark.parametrize("model", ["haiku", "claude-haiku-5-5", "claude-opus-5-5", "claude-sonnet-5-5"])
def test_claude_5_5_capabilities(model, route):
    cap = effort_capabilities(model, route=route)
    assert cap["effort_levels"] == ["low", "medium", "high", "xhigh", "max"]
    assert cap["default_effort"] == "medium"
    for level in cap["effort_levels"]:
        assert normalize_effort(level, model, route=route) == level


@pytest.mark.parametrize("route", ["anthropic", "delegated"])
@pytest.mark.parametrize("model", ["claude-haiku-4-5", "claude-haiku-4-5-20251001", "claude-haiku-4-5-20261101"])
def test_haiku_4_5_remains_effortless(model, route):
    assert effort_capabilities(model, route=route)["effort_levels"] == []
    assert effort_capabilities(model, route=route)["default_effort"] is None
    assert normalize_effort("max", model, route=route) is None


def test_cloud_cli_haiku_alias_remains_effortless(monkeypatch):
    from app.core import claude_auth_mode
    monkeypatch.setattr(claude_auth_mode, "current_mode", lambda: "cloud")
    assert effort_capabilities("haiku", route="delegated")["effort_levels"] == []
    assert effort_capabilities("haiku", route="delegated")["default_effort"] is None
    assert normalize_effort("max", "haiku", route="delegated") is None
    assert effort_capabilities("claude-haiku-5-5", route="delegated")["default_effort"] == "medium"
    assert effort_capabilities("haiku", route="anthropic")["default_effort"] == "medium"


def test_official_defaults_and_cli_difference():
    # https://developers.openai.com/api/docs/models/gpt-5.6-sol.md
    assert effort_capabilities("gpt-5.6-sol", {"provider_type": "openai"})["default_effort"] == "medium"
    assert normalize_effort("max", "gpt-5.6-sol", {"provider_type": "openai"}) == "max"
    assert effort_capabilities("gpt-5.6-sol", {"provider_type": "chatgpt"})["effort_levels"] is None
    # https://platform.claude.com/docs/en/build-with-claude/effort.md
    # https://code.claude.com/docs/en/model-config.md#adjust-effort-level
    assert effort_capabilities("claude-opus-4-7")["default_effort"] == "high"
    assert effort_capabilities("claude-opus-4-7", route="delegated")["default_effort"] == "xhigh"
    assert effort_capabilities("claude-opus-4-5-20251101", route="delegated")["effort_levels"] == []
    assert effort_capabilities("claude-sonnet-4-6")["effort_levels"] == ["low", "medium", "high", "max"]


def test_native_registry_matches_default_delegated_route(monkeypatch):
    from types import SimpleNamespace
    from app.core import model_registry
    monkeypatch.setattr(model_registry, "MODELS", [
        SimpleNamespace(value=model, label=model, family="opus", context_window=200000)
        for model in ("claude-opus-4-5-20251101", "claude-opus-4-7")
    ])
    monkeypatch.setattr(model_registry, "_providers_payload", lambda: [])
    monkeypatch.setattr(model_registry, "_claude_payload", lambda: {})
    models = model_registry.registry_payload()["models"]
    assert models[0]["effort_levels"] == []
    assert models[1]["default_effort"] == "xhigh"
    assert effort_capabilities("claude-mythos-5-1", route="delegated")["effort_levels"] == []
    assert effort_capabilities("claude-mythos-5-1", route="anthropic")["effort_levels"] == ["low", "medium", "high", "xhigh", "max"]


@pytest.mark.parametrize("explicit,saved,parent,expected", [
    ("low", "max", "high", "low"),
    (None, "max", "high", "max"),
    (None, None, "medium", "medium"),
    (None, None, None, "high"),
    (None, "invalid", "medium", "medium"),
    ("ultra", "low", "medium", "max"),
])
def test_child_effort_priority_and_preference_roundtrip(mock_db, monkeypatch, explicit, saved, parent, expected):
    from app.core import preference_identity
    from app.core.model_effort import resolve_child_effort
    from app.db import database
    monkeypatch.setattr(preference_identity, "principal_preference_identity", lambda p: ("account", "owner"))
    from app.core.model_effort import effort_preference_key
    key = effort_preference_key("opus" if saved else "another-model")
    database.set_user_preference("account", "owner", key, saved or "low")
    assert database.get_user_preference("account", "owner", key)["value"] == (saved or "low")
    assert resolve_child_effort("opus", None, explicit=explicit, parent=parent) == expected


@pytest.mark.parametrize("model", ["opus", "prov:api:claude-opus-5", "opus[1m]", "prov:api:claude-opus-5[1m]"])
def test_effort_aliases_share_reads_writes_and_clears(mock_db, monkeypatch, model):
    from app.core import preference_identity
    from app.core.model_effort import effort_preference_key, resolve_child_effort
    from app.db import database
    monkeypatch.setattr(preference_identity, "principal_preference_identity", lambda p: ("account", "owner"))
    database.set_user_preference("account", "owner", "composer.modelEfforts", {"opus": "max"})
    database.set_user_preference("account", "owner", "composer.modelEffort." + model, "low")
    assert effort_preference_key(model) == "composer.modelEffort.claude-opus-5"
    assert database.get_user_preference("account", "owner", effort_preference_key("opus"))["value"] == "low"
    assert resolve_child_effort(model, None, parent="high") == "low"
    assert database.delete_user_preference("account", "owner", "composer.modelEffort." + model)
    assert database.get_user_preference("account", "owner", effort_preference_key("opus"))["value"] is None
    assert resolve_child_effort(model, None, parent="medium") == "medium"


@pytest.mark.parametrize("older", ["claude-opus-4-6", "claude-opus-4-7", "claude-opus-4-8"])
def test_concrete_versions_keep_independent_effort_and_clears(mock_db, monkeypatch, older):
    from app.core import preference_identity
    from app.core.model_effort import effort_preference_key, resolve_child_effort
    from app.db import database, settings
    monkeypatch.setattr(preference_identity, "principal_preference_identity", lambda p: ("account", "owner"))
    # An existing record written by the previous family-keyed implementation.
    with monkeypatch.context() as migration:
        migration.setattr(settings, "_canonical_preference_key", lambda key: key)
        database.set_user_preference("account", "owner", "composer.modelEffort.opus", "high")
    database.set_user_preference("account", "owner", effort_preference_key(older), "low")
    database.set_user_preference("account", "owner", effort_preference_key("opus"), "max")
    assert resolve_child_effort(older, None, parent="medium") == "low"
    assert resolve_child_effort("claude-opus-5", None, parent="medium") == "max"
    for alias in (older + "-20260205", "prov:api:" + older + "[1m]", "anthropic/" + older):
        assert effort_preference_key(alias) == effort_preference_key(older)
        assert resolve_child_effort(alias, None, parent="medium") == "low"
    database.delete_user_preference("account", "owner", effort_preference_key(older))
    assert resolve_child_effort(older, None, parent="medium") == "medium"
    assert resolve_child_effort("opus", None, parent="low") == "max"
    assert resolve_child_effort("claude-opus-4-9", None, parent="low") == "high"
    assert database.get_user_preference("account", "owner", "composer.modelEffort.opus", canonical=False)["value"] == "high"


def test_legacy_map_concrete_entry_beats_family_fallback(mock_db, monkeypatch):
    from app.core import preference_identity
    from app.core.model_effort import resolve_child_effort
    from app.db import database
    monkeypatch.setattr(preference_identity, "principal_preference_identity", lambda p: ("account", "owner"))
    database.set_user_preference("account", "owner", "composer.modelEfforts",
                                 {"opus": "max", "claude-opus-4-6": "low"})
    assert resolve_child_effort("claude-opus-4-6", None, parent="high") == "low"
    assert resolve_child_effort("claude-opus-5", None, parent="high") == "max"


@pytest.mark.parametrize("model,parent,expected", [
    ("haiku", "max", "max"),
    ("claude-haiku-4-5", "max", None),
    ("never-seen-model", "xhigh", "xhigh"),
    ("never-seen-model", None, None),
])
def test_child_effort_unknown_and_unsupported(mock_db, monkeypatch, model, parent, expected):
    from app.core import preference_identity
    from app.core.model_effort import resolve_child_effort
    monkeypatch.setattr(preference_identity, "principal_preference_identity", lambda p: ("account", "owner"))
    assert resolve_child_effort(model, None, parent=parent) == expected


def test_child_effort_uses_bare_model_across_providers_and_keeps_api_users_separate(mock_db, monkeypatch):
    from app.core import preference_identity
    from app.core.model_effort import resolve_child_effort
    from app.db import database, model_providers
    monkeypatch.setattr(preference_identity, "principal_preference_identity",
                        lambda p: ("api_user", p["principal_id"]) if p["kind"] == "api_user" else ("account", "owner"))
    provider = {"provider_type": "openai", "models_json": [{"id": "shared-model",
                "reasoning_capabilities": {"version": 1, "effort_levels": ["low", "medium", "high"], "default_effort": "low"}}]}
    monkeypatch.setattr(model_providers, "get_model_provider", lambda _: provider)
    from app.core.provider import harness_adapter
    monkeypatch.setattr(harness_adapter, "_lookup_provider_row", lambda _: provider)
    database.set_user_preference("account", "owner", "composer.modelEfforts", {"shared-model": "xhigh"})
    for pid in ("first", "second"):
        assert resolve_child_effort(f"prov:{pid}:shared-model", pid, parent="low") == "high"
    assert resolve_child_effort("shared-model", "first", caller={"api_user_id": "key-user"}) == "low"


@pytest.mark.parametrize("saved,parent,expected", [
    (True, False, True),
    (False, True, False),
    (None, True, True),
    (None, False, False),
    (None, None, None),
    ("yes", True, True),
])
def test_child_fast_prefers_the_owner_pick_for_the_child_model(mock_db, monkeypatch, saved, parent, expected):
    from app.core import preference_identity
    from app.core.model_effort import fast_preference_key, resolve_child_fast
    from app.db import database
    monkeypatch.setattr(preference_identity, "principal_preference_identity", lambda p: ("account", "owner"))
    if saved is not None:
        database.set_user_preference("account", "owner", fast_preference_key("opus"), saved)
    database.set_user_preference("account", "owner", fast_preference_key("another-model"), not parent)
    assert resolve_child_fast("opus", None, parent=parent) is expected


@pytest.mark.parametrize("model", ["opus", "prov:api:claude-opus-5", "opus[1m]", "prov:api:claude-opus-5[1m]"])
def test_fast_aliases_share_reads_writes_and_clears(mock_db, monkeypatch, model):
    from app.core import preference_identity
    from app.core.model_effort import fast_preference_key, resolve_child_fast
    from app.db import database
    monkeypatch.setattr(preference_identity, "principal_preference_identity", lambda p: ("account", "owner"))
    database.set_user_preference("account", "owner", "composer.modelFast." + model, True)
    assert fast_preference_key(model) == "composer.modelFast.claude-opus-5"
    assert database.get_user_preference("account", "owner", fast_preference_key("opus"))["value"] is True
    assert resolve_child_fast(model, None, parent=False) is True
    assert database.delete_user_preference("account", "owner", "composer.modelFast." + model)
    # The clear stays as a replicated null and the parent's choice applies again.
    assert database.get_user_preference("account", "owner", fast_preference_key("opus"))["value"] is None
    assert resolve_child_fast(model, None, parent=False) is False


@pytest.mark.parametrize("model", ["sonnet", "haiku", "never-seen-model"])
def test_child_fast_is_never_on_where_the_model_lacks_it(mock_db, monkeypatch, model):
    from app.core import preference_identity
    from app.core.model_effort import fast_preference_key, resolve_child_fast
    from app.db import database
    monkeypatch.setattr(preference_identity, "principal_preference_identity", lambda p: ("account", "owner"))
    assert resolve_child_fast(model, None, parent=True) is None
    assert resolve_child_fast(model, None, parent=False) is False
    # A saved pick cannot turn the tier on for a model that lacks it.
    database.set_user_preference("account", "owner", fast_preference_key(model), True)
    assert resolve_child_fast(model, None, parent=False) is None


@pytest.mark.parametrize("older_pick,newer_pick", [(True, False), (False, True)])
def test_child_fast_concrete_opus_versions_are_independent(mock_db, monkeypatch, older_pick, newer_pick):
    from app.core import preference_identity
    from app.core.model_effort import fast_preference_key, resolve_child_fast
    from app.db import database
    monkeypatch.setattr(preference_identity, "principal_preference_identity", lambda p: ("account", "owner"))
    database.set_user_preference("account", "owner", fast_preference_key("claude-opus-4-6"), older_pick)
    database.set_user_preference("account", "owner", fast_preference_key("opus"), newer_pick)
    assert resolve_child_fast("claude-opus-4-6", None, parent=not older_pick) is older_pick
    assert resolve_child_fast("claude-opus-5", None, parent=not newer_pick) is newer_pick
    database.delete_user_preference("account", "owner", fast_preference_key("claude-opus-4-6"))
    assert resolve_child_fast("claude-opus-4-6", None, parent=not older_pick) is not older_pick
    assert resolve_child_fast("opus", None, parent=not newer_pick) is newer_pick


def test_child_fast_reads_the_agent_model_and_keeps_api_users_separate(mock_db, monkeypatch):
    from app.core import preference_identity
    from app.core.model_effort import fast_preference_key, resolve_child_fast
    from app.db import database
    monkeypatch.setattr(preference_identity, "principal_preference_identity",
                        lambda p: ("api_user", p["principal_id"]) if p["kind"] == "api_user" else ("account", "owner"))
    monkeypatch.setattr(database, "get_profile", lambda pid: {"config": {"model": "opus"}} if pid == "reviewer" else None)
    database.set_user_preference("account", "owner", fast_preference_key("opus"), True)
    assert resolve_child_fast(None, None, parent=False, profile_id="reviewer") is True
    assert resolve_child_fast("opus", None, parent=False, caller={"api_user_id": "key-user"}) is False


def test_metadata_beats_official_and_legacy_upgrade():
    old = {"id": "gpt-5.6-sol", "supported_reasoning_levels": ["none", "max"], "default_reasoning_level": "none"}
    from app.db.model_providers import _normalize_model_capabilities
    before = effort_capabilities(old["id"], {"provider_type": "openai"}, old)
    assert before["effort_metadata_version"] == 0
    upgraded = _normalize_model_capabilities([old])[0]
    assert effort_capabilities(old["id"], {"provider_type": "openai"}, upgraded) == {**before, "effort_metadata_version": 1}
    assert before["effort_levels"] == ["none", "max"]


def test_saving_legacy_parameter_hints_still_requires_catalog_refresh():
    from app.db.model_providers import _normalize_model_capabilities
    old = {"id": "gpt-6-astra", "supported_parameters": ["reasoning"]}
    saved = _normalize_model_capabilities([old])[0]
    assert saved == old
    assert effort_capabilities(old["id"], {"provider_type": "openrouter"}, saved)["effort_metadata_version"] == 0
    refreshed = {**old, "reasoning_capabilities": {"version": 1, "effort_levels": None, "default_effort": None}}
    assert _normalize_model_capabilities([refreshed])[0] == refreshed


def test_adapter_reconnect_suppresses_inherited_effort(monkeypatch):
    from app.core.provider import harness_adapter as ha
    from app.core.provider.types import SessionOptions
    adapter = ha.HarnessAdapter()
    adapter._options = SessionOptions(model="sonnet", effort="max")
    assert adapter.requires_reconnect(SessionOptions(model="claude-haiku-4-5"))
    assert not adapter.requires_reconnect(SessionOptions(model="haiku"))
    assert ha._effective_effort(SessionOptions(model="haiku"), "high") == "high"
    assert ha._effective_effort(SessionOptions(model="claude-haiku-4-5"), "high") is None


@pytest.mark.parametrize("model,expected", [("sonnet", "max"), ("haiku", "max"), ("claude-haiku-4-5", None)])
def test_query_preserves_effort_until_adapter_resolves_route(mock_db, model, expected):
    from app.core.query_options import build_options_from_profile
    options, _ = build_options_from_profile(
        {"id": "p", "name": "p", "config": {"model": "sonnet", "effort": "max"}},
        overrides={"model": model},
    )
    from app.core.provider.harness_adapter import _effective_effort
    from app.core.provider.types import SessionOptions

    assert options.effort == "max"
    adapter_options = SessionOptions(model=options.model, effort=options.effort)
    assert _effective_effort(adapter_options, adapter_options.effort) == expected


def test_catalog_change_reconnects_from_baked_effort(monkeypatch):
    from app.core.provider import harness_adapter as ha
    from app.core.provider.types import SessionOptions

    row = {"provider_type": "openrouter", "models_json": [{"id": "custom-model", "supported_reasoning_levels": []}]}
    monkeypatch.setattr(ha, "_lookup_provider_row", lambda _: row)
    options = SessionOptions(model="custom-model", effort="high", extra={"provider_id": "p"})
    adapter = ha.HarnessAdapter()
    adapter._options = options
    assert adapter.requires_reconnect(options)


def test_missing_provider_lookup_requires_reconnect(monkeypatch):
    from app.core.provider import harness_adapter as ha
    from app.core.provider.types import SessionOptions

    monkeypatch.setattr(ha, "_lookup_provider_row", lambda _: None)
    adapter = ha.HarnessAdapter()
    adapter._options = SessionOptions(model="custom-model", extra={"provider_id": "p"})
    assert adapter.requires_reconnect(SessionOptions(model="custom-model", effort="high", extra={"provider_id": "p"}))
    assert not adapter.requires_reconnect(SessionOptions(model="other-model", extra={"provider_id": "p"}))


@pytest.mark.asyncio
async def test_refresh_preserves_curation_singleflight_and_failure(monkeypatch):
    from app.api import model_providers as api
    api._capability_locks.clear()
    api._capability_results.clear()
    row = {"id": "p", "enabled": True, "provider_type": "openrouter", "default_model": "m",
           "models_json": [{"id": "m", "label": "Curated", "context_window": 123,
                            "supported_reasoning_levels": ["high"], "pricing": {"prompt": "1"}}]}
    monkeypatch.setattr(api.db, "get_model_provider", lambda ident, **kw: row if ident == "p" else None)
    writes = []
    def update(ident, **kwargs):
        writes.append(kwargs)
        row.update(kwargs)
        return row
    monkeypatch.setattr(api.db, "update_model_provider", update)
    started, release = asyncio.Event(), asyncio.Event()
    async def fetch(*args, **kwargs):
        started.set()
        await release.wait()
        return api.FetchModelsResponse(source="upstream", models=[{"id": "m", "label": "Upstream",
            "reasoning_capabilities": {"version": 1, "effort_levels": ["low", "max"], "default_effort": "max"}}, {"id": "not-curated"}])
    upstream = AsyncMock(side_effect=fetch)
    monkeypatch.setattr(api, "fetch_models", upstream)
    first = asyncio.create_task(api._refresh_provider_capabilities("p"))
    await started.wait()
    second = asyncio.create_task(api._refresh_provider_capabilities("p"))
    release.set()
    assert await asyncio.gather(first, second) == [True, True]
    assert upstream.await_count == 1
    assert list(writes[0]) == ["models_json", "expected_revision"]
    entry = row["models_json"][0]
    assert len(row["models_json"]) == 1
    assert entry["label"] == "Curated" and entry["context_window"] == 123
    assert entry["pricing"] == {"prompt": "1"}
    assert entry["reasoning_capabilities"]["effort_levels"] == ["low", "max"]
    api._capability_results.clear()
    upstream.side_effect = RuntimeError("sensitive upstream detail")
    body = api.RefreshCapabilitiesRequest(provider_ids=["p", "p", "disabled"])
    assert await api.refresh_capabilities(body, token="test") == {"refreshed": [], "failed": ["p", "disabled"]}
    assert not await api._refresh_provider_capabilities("p")
    assert upstream.await_count == 2  # Failed refresh is also on cooldown.
    assert len(writes) == 1
    api._capability_results.clear()
    upstream.side_effect = None
    upstream.return_value = api.FetchModelsResponse(source="upstream", models=[])
    assert not await api._refresh_provider_capabilities("p")
    assert row["models_json"][0]["reasoning_capabilities"]["effort_levels"] == ["low", "max"]


@pytest.mark.asyncio
async def test_refresh_upgrades_legacy_ids_and_preserves_concurrent_curation(monkeypatch):
    from app.api import model_providers as api
    api._capability_locks.clear()
    api._capability_results.clear()
    row = {"id": "p", "enabled": True, "models_json": ["old-selection"]}
    monkeypatch.setattr(api.db, "get_model_provider", lambda *a, **k: row)
    async def fetch(*args, **kwargs):
        row["models_json"] = ["new-selection"]
        return api.FetchModelsResponse(source="upstream", models=[
            {"id": model, "reasoning_capabilities": {"version": 1,
                "effort_levels": ["low", "max"], "default_effort": "max"}}
            for model in ("old-selection", "new-selection")
        ])
    monkeypatch.setattr(api, "fetch_models", fetch)
    writes = []
    def update(_id, **kwargs):
        writes.append(kwargs)
        row.update(kwargs)
        return row
    monkeypatch.setattr(api.db, "update_model_provider", update)
    assert await api._refresh_provider_capabilities("p")
    assert writes == [{"expected_revision": None, "models_json": [{"id": "new-selection", "label": "new-selection",
        "reasoning_capabilities": {"version": 1, "effort_levels": ["low", "max"], "default_effort": "max"}}]}]


def test_refresh_requires_authentication():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import model_providers as api
    from app.api.auth import require_auth
    from fastapi import HTTPException
    app = FastAPI()
    app.include_router(api.router)
    def deny():
        raise HTTPException(status_code=401, detail="Authentication required")
    app.dependency_overrides[require_auth] = deny
    with TestClient(app) as client:
        response = client.post("/api/v1/model-providers/refresh-capabilities", json={"provider_ids": ["p"]})
    assert response.status_code == 401

@pytest.mark.parametrize("value", ["none", "minimal", "xhigh", "max", "ultra"])
def test_sibling_option_paths_accept_union(value):
    from app.core.provider.harness_adapter import _valid_effort
    from app.core.generation_options import _resolve_thinking_and_effort, VALID_EFFORTS
    from app.core.query_options import _build_thinking_config
    assert _valid_effort(value) == value
    assert value in VALID_EFFORTS
    assert _resolve_thinking_and_effort({"effort": value})["effort"] == value
    assert _build_thinking_config({"effort": value}, "custom")["effort"] == value

# Sanitized Codex catalog shape observed 2026-09-05; subscription capabilities
# differ from the static native API table (notably Sol's low default).
@pytest.mark.parametrize("model,default,top", [
    ("gpt-6-astra", "medium", "ultra"),
    ("gpt-5.6-sol", "low", "ultra"),
    ("gpt-5.6-terra", "medium", "ultra"),
    ("gpt-5.6-luna", "medium", "max"),
])
def test_codex_source_metadata_persists_and_reaches_registry(monkeypatch, model, default, top):
    from app.api.model_providers import _parse_models_response
    from app.db.model_providers import _normalize_model_capabilities
    from app.core import model_registry
    from app.db import database
    levels = ["low", "medium", "high", "xhigh", "max"] + (["ultra"] if top == "ultra" else [])
    raw = {"slug": model, "display_name": model, "supported_reasoning_levels": [
        {"effort": value} for value in levels], "default_reasoning_level": default}
    entries = _normalize_model_capabilities(_parse_models_response(
        {"models": [raw]}, provider_type="chatgpt"))
    row = {"id": "codex", "provider_type": "openai", "auth_mode": "chatgpt", "models_json": entries}
    monkeypatch.setattr(database, "get_enabled_model_providers", lambda **kw: [row])
    public = model_registry._providers_payload()[0]["models"][0]
    assert public["effort_levels"] == levels
    assert public["default_effort"] == default
    assert public["effort_metadata_version"] == 1
    assert normalize_effort("ultra", model, row) == ("ultra" if top == "ultra" else None)
    # An absent Codex declaration is unknown, never the native API fallback.
    assert effort_capabilities(model, {**row, "models_json": []})["effort_levels"] is None


@pytest.mark.parametrize("levels,expected", [
    (["low", "future", "ultra"], ["low", "ultra"]),
    ([{"effort": "high"}, {"effort": "future"}], ["high"]),
    (["future"], None),
    (["high", {}], None),
    (["high", {"effort": []}], None),
    ([], []),
])
def test_future_tokens_preserve_known_levels_but_malformed_is_unknown(levels, expected):
    cap = sanitize_effort_metadata({"supported_reasoning_levels": levels,
                                    "default_reasoning_level": "future"})["reasoning_capabilities"]
    assert cap == {"version": 1, "effort_levels": expected, "default_effort": None}


@pytest.mark.parametrize("route", ["openrouter", "anthropic", "delegated"])
@pytest.mark.parametrize("levels", [None, ["high", "ultra"], ["ultra"]])
def test_ultra_never_crosses_gateway_or_claude_routes(route, levels):
    row = {"provider_type": "custom", "models_json": [{"id": "m", "reasoning_capabilities": {
        "version": 1, "effort_levels": levels, "default_effort": "ultra"}}]}
    assert normalize_effort("ultra", "m", row, route=route) is None
    cap = effort_capabilities("m", row, route=route)
    assert cap["effort_levels"] is None or "ultra" not in cap["effort_levels"]
    assert cap["default_effort"] is None


def test_static_tables_do_not_inherit_codex_ultra():
    from app.core.model_effort import _OPENAI, _CLI, _ANTHROPIC
    for table in (_OPENAI, _CLI, _ANTHROPIC):
        assert all("ultra" not in cap["effort_levels"] for cap in table.values())
    assert _OPENAI["gpt-6-astra"]["effort_levels"] == ["low", "medium", "high", "xhigh", "max"]
