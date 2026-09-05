"""Effort is a per-model, per-route capability, not a reasoning toggle."""
import pytest

from app.core.model_effort import effort_capabilities, normalize_effort, sanitize_effort_metadata


@pytest.mark.parametrize("model,ptype,expected", [
    ("sonnet", None, ["low", "medium", "high", "xhigh", "max"]),
    ("haiku", None, []),
    ("claude-opus-4-5", None, ["low", "medium", "high"]),
    ("claude-opus-4-6", None, ["low", "medium", "high", "max"]),
    ("anthropic/claude-sonnet-5", "openrouter", ["low", "medium", "high"]),
    ("gpt-5.6-sol", "openai", ["low", "medium", "high"]),
    ("gpt-5.4-pro", "chatgpt", ["medium", "high"]),
    ("grok-4.6", "xai", ["low", "medium", "high"]),
    ("gpt-4o", "openai", []),
    ("z-ai/glm-5", "openrouter", []),
    ("glm-4.7", "custom", []),
    ("unknown", "openrouter", []),
])
def test_conservative_fallback(model, ptype, expected):
    assert effort_capabilities(model, {"provider_type": ptype})["effort_levels"] == expected


@pytest.mark.parametrize("raw", [None, {}, "high", [None, {}, {"effort": []}, "none", "minimal"]])
def test_malformed_levels_fail_closed(raw):
    entry = {"supported_reasoning_levels": raw, "default_reasoning_level": {}}
    assert effort_capabilities("sonnet", model_entry=entry) == {"effort_levels": [], "default_effort": None}


def test_metadata_whitelist_and_route_intersection():
    entry = {"supported_reasoning_levels": [{"effort": "high", "description": "ignored"}, {"effort": "xhigh"}, "low", "high"],
             "default_reasoning_level": "high", "supported_parameters": ["reasoning", {}, "reasoning"], "secret": "ignored"}
    clean = sanitize_effort_metadata(entry)
    assert clean == {"supported_reasoning_levels": ["low", "high", "xhigh"], "default_reasoning_level": "high", "supported_parameters": ["reasoning"]}
    assert effort_capabilities("custom-id", {"provider_type": "openai"}, clean) == {"effort_levels": ["low", "high"], "default_effort": "high"}
    assert effort_capabilities("custom-id", {"provider_type": "custom"}, clean)["effort_levels"] == ["low", "high", "xhigh"]


@pytest.mark.parametrize("parameters,expected", [(["reasoning"], []), (["include_reasoning"], []), (None, []), (["reasoning_effort"], ["low", "medium", "high"])])
def test_reasoning_toggle_is_not_effort(parameters, expected):
    assert effort_capabilities("unknown-model", {"provider_type": "openrouter"}, {"supported_parameters": parameters})["effort_levels"] == expected


def test_openrouter_reasoning_object_preserves_known_model_effort():
    entry = {"supported_parameters": ["reasoning"]}
    assert effort_capabilities("openai/gpt-5.6-sol", {"provider_type": "openrouter"}, entry)["effort_levels"] == ["low", "medium", "high"]
    assert effort_capabilities("z-ai/glm-5", {"provider_type": "openrouter"}, entry)["effort_levels"] == []


def test_stale_effort_normalization():
    assert normalize_effort("max", "gpt-5.6-sol", {"provider_type": "openai"}) == "high"
    assert normalize_effort("max", "sonnet") == "max"
    assert normalize_effort("high", "haiku") is None
    assert normalize_effort("low", "gpt-5.4-pro", {"provider_type": "chatgpt"}) is None
    assert normalize_effort("high", "glm-5", {"provider_type": "custom"}) is None


def test_catalog_parser_preserves_only_sanitized_fields():
    from app.api.model_providers import _parse_models_response
    entries = _parse_models_response({"models": [{"slug": "custom-id", "supported_reasoning_levels": [{"effort": "high"}, "invalid"], "default_reasoning_level": "high", "supported_parameters": ["reasoning_effort", 4], "private": "discard"}, "bare-id"]})
    assert entries == [{"id": "custom-id", "label": "custom-id", "supported_reasoning_levels": ["high"], "default_reasoning_level": "high", "supported_parameters": ["reasoning_effort"]}, {"id": "bare-id", "label": "bare-id"}]


def test_registry_effort_contract(monkeypatch):
    from app.core import model_registry
    from app.db import database
    monkeypatch.setattr(database, "get_enabled_model_providers", lambda **kw: [{"id": "p", "provider_type": "openrouter", "models_json": [{"id": "z-ai/glm-5"}, {"id": "anthropic/claude-sonnet-5"}]}])
    models = model_registry._providers_payload()[0]["models"]
    assert models[0]["effort_levels"] == []
    assert models[1]["effort_levels"] == ["low", "medium", "high"]


def test_adapter_reconnect_suppresses_inherited_effort(monkeypatch):
    from app.core.provider import harness_adapter as ha
    from app.core.provider.types import SessionOptions
    adapter = ha.HarnessAdapter()
    adapter._options = SessionOptions(model="sonnet", effort="max")
    assert adapter.requires_reconnect(SessionOptions(model="haiku"))
    assert ha._effective_effort(SessionOptions(model="haiku"), "high") is None


@pytest.mark.parametrize("model,expected", [("sonnet", "max"), ("haiku", None)])
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
