"""Tests for the model registry.

The presets are Agent SDK shorthand aliases (``haiku``, ``sonnet``, ``opus``,
``fable``). The 1M context window is a capability toggled on a base model by
appending ``[1m]`` to the stored value. These tests pin the registry contents,
the flipped legacy map (older full model IDs → shorthand), and the helpers that
round-trip the suffix.
"""

import json

from app.core import model_registry as m
from app.db import database as db


def test_registry_contents_and_default():
    # Presets are the four SDK shorthand aliases, in this order.
    assert [opt.value for opt in m.MODELS] == ["haiku", "sonnet", "opus", "fable"]
    # No registry entry carries the [1m] suffix — it's derived on demand.
    assert all(not opt.value.endswith(m.CONTEXT_1M_SUFFIX) for opt in m.MODELS)
    by_value = {opt.value: opt for opt in m.MODELS}
    assert by_value["haiku"].supports_1m is False
    assert by_value["sonnet"].supports_1m is True
    assert by_value["opus"].supports_1m is True
    assert by_value["fable"].supports_1m is True
    assert by_value["fable"].label == "Fable"
    assert m.DEFAULT_MODEL == "sonnet"


def test_legacy_map_flips_full_ids_to_shorthand():
    # Older stored full model IDs normalize to the shorthand alias.
    assert m.normalize_model("claude-haiku-4-5") == "haiku"
    assert m.normalize_model("claude-sonnet-4-6") == "sonnet"
    assert m.normalize_model("claude-sonnet-4-6[1m]") == "sonnet[1m]"
    assert m.normalize_model("claude-opus-4-6") == "opus"
    assert m.normalize_model("claude-opus-4-7") == "opus"
    assert m.normalize_model("claude-opus-4-8") == "opus"
    assert m.normalize_model("claude-opus-4-8[1m]") == "opus[1m]"


def test_normalize_passes_through_custom_and_shorthand():
    # Unknown custom model strings pass through unchanged.
    assert m.normalize_model("claude-sonnet-4-5") == "claude-sonnet-4-5"
    assert m.normalize_model("some-future-model") == "some-future-model"
    # Shorthand presets are already canonical — returned as-is.
    assert m.normalize_model("sonnet") == "sonnet"
    assert m.normalize_model("fable[1m]") == "fable[1m]"
    # Inherit / empty.
    assert m.normalize_model("") == ""
    assert m.normalize_model(None) == ""


def test_has_1m_context():
    assert m.has_1m_context("opus[1m]") is True
    assert m.has_1m_context("opus") is False
    assert m.has_1m_context("") is False
    assert m.has_1m_context(None) is False
    # Legacy full ID normalizes to a 1M shorthand value.
    assert m.has_1m_context("claude-opus-4-8[1m]") is True


def test_strip_context_suffix():
    assert m.strip_context_suffix("opus[1m]") == "opus"
    assert m.strip_context_suffix("opus") == "opus"
    assert m.strip_context_suffix("claude-opus-4-8[1m]") == "opus"
    assert m.strip_context_suffix("") == ""


def test_with_context_suffix_toggles():
    assert m.with_context_suffix("opus", True) == "opus[1m]"
    assert m.with_context_suffix("opus[1m]", False) == "opus"
    # Idempotent — enabling an already-1M model does not double the suffix.
    assert m.with_context_suffix("opus[1m]", True) == "opus[1m]"
    # Inherit / empty has nothing to toggle.
    assert m.with_context_suffix("", True) == ""
    # Works on custom models too.
    assert m.with_context_suffix("my-model", True) == "my-model[1m]"


def test_preset_detection_includes_1m_variants():
    assert m.is_preset_model("opus") is True
    assert m.is_preset_model("opus[1m]") is True
    assert m.is_preset_model("fable") is True
    # Legacy full ID resolves through the map to a preset.
    assert m.is_preset_model("claude-opus-4-8[1m]") is True
    assert m.is_preset_model("totally-custom") is False
    assert m.is_preset_model("") is False


def test_label_and_family_resolve_through_suffix():
    assert m.get_model_label("sonnet") == "Sonnet"
    assert m.get_model_label("sonnet[1m]") == "Sonnet 1M"
    assert m.get_model_family("fable") == "fable"
    assert m.get_model_family("opus[1m]") == "opus"


def test_context_window_for_1m_variant():
    assert m.get_context_window("sonnet") == 200_000
    assert m.get_context_window("sonnet[1m]") == m.CONTEXT_1M_WINDOW
    # Custom model with the suffix still reports the 1M window.
    assert m.get_context_window("my-model[1m]") == m.CONTEXT_1M_WINDOW


def test_model_values_includes_1m_for_supported_models():
    values = m.model_values()
    assert "sonnet" in values
    assert "sonnet[1m]" in values
    assert "fable" in values
    assert "fable[1m]" in values
    assert "opus" in values
    assert "opus[1m]" in values
    # Haiku has no 1M variant.
    assert "haiku" in values
    assert "haiku[1m]" not in values


def test_registry_payload_shape():
    payload = m.registry_payload()
    assert payload["context_1m_suffix"] == m.CONTEXT_1M_SUFFIX
    assert payload["default"] == "sonnet"
    models = payload["models"]
    assert isinstance(models, list)
    assert "supports_1m" in models[0]
    # Flipped legacy map is serialized for the frontend.
    assert payload["legacy_map"]["claude-opus-4-8"] == "opus"


class TestRegistryPayloadProviders:
    """``providers`` is a lazily-sourced projection of enabled
    ``model_providers`` rows — never carries the encrypted auth token, and
    degrades to an empty list rather than exploding the whole registry
    payload when the db isn't ready."""

    def test_empty_table_returns_empty_list(self, mock_db):
        payload = m.registry_payload()
        assert payload["providers"] == []

    def test_providers_sourced_from_enabled_rows_only(self, mock_db):
        db.create_model_provider(
            provider_id="prov-1",
            name="OpenRouter",
            provider_type="openrouter",
            base_url="https://openrouter.ai/api",
            auth_token="enc:tok-blob",
            models=[{"id": "openai/gpt-5.6", "label": "GPT-5.6"}],
            default_model="openai/gpt-5.6",
            enabled=True,
        )
        db.create_model_provider(
            provider_id="prov-2",
            name="Disabled Gateway",
            provider_type="custom",
            base_url="https://gw.example/api",
            auth_token="enc:other-blob",
            enabled=False,
        )
        payload = m.registry_payload()
        providers = payload["providers"]
        assert len(providers) == 1
        entry = providers[0]
        assert entry == {
            "id": "prov-1",
            "name": "OpenRouter",
            "provider_type": "openrouter",
            "models": [{
                "id": "openai/gpt-5.6",
                "label": "GPT-5.6",
                # Additive metadata (Chunk D.4) — null when the row omits them.
                "context_window": None,
                "max_output_tokens": None,
            }],
            "default_model": "openai/gpt-5.6",
        }

    def test_model_entries_carry_provider_context_and_output_metadata(self, mock_db):
        """Chunk D.4 additive keys: each model entry echoes the provider row's
        ``context_window`` / ``max_output_tokens`` (one value shared by all of
        that provider's models — no per-model store)."""
        db.create_model_provider(
            provider_id="prov-meta",
            name="Gateway",
            provider_type="custom",
            base_url="https://gw.example/api",
            models=[
                {"id": "model-a", "label": "Model A"},
                {"id": "model-b", "label": "Model B"},
            ],
            context_window=200000,
            max_output_tokens=8192,
            enabled=True,
        )
        payload = m.registry_payload()
        entry = payload["providers"][0]
        assert entry["models"] == [
            {"id": "model-a", "label": "Model A",
             "context_window": 200000, "max_output_tokens": 8192},
            {"id": "model-b", "label": "Model B",
             "context_window": 200000, "max_output_tokens": 8192},
        ]

    def test_no_auth_token_anywhere_in_payload(self, mock_db):
        db.create_model_provider(
            provider_id="prov-1",
            name="OpenRouter",
            provider_type="openrouter",
            base_url="https://openrouter.ai/api",
            auth_token="enc:super-secret-blob",
            models=[{"id": "openai/gpt-5.6", "label": "GPT-5.6"}],
            enabled=True,
        )
        payload = m.registry_payload()
        serialized = json.dumps(payload)
        assert "auth_token" not in serialized
        assert "enc:super-secret-blob" not in serialized

    def test_db_not_initialized_degrades_to_empty_list(self, monkeypatch):
        """Early-boot: the pg pool isn't initialized yet. ``get_db()`` raises
        ``RuntimeError`` in that state — the payload must still build."""

        def _raise_not_initialized(*_args, **_kwargs):
            raise RuntimeError("PostgreSQL connection pool not initialized. Call init_pool() first.")

        monkeypatch.setattr(db, "get_enabled_model_providers", _raise_not_initialized)
        payload = m.registry_payload()
        assert payload["providers"] == []

    def test_db_generic_exception_degrades_to_empty_list(self, monkeypatch):
        """FIX 6: a rolling-deploy missing table (psycopg2 ProgrammingError) or
        pool exhaustion (PoolError) is a plain Exception, not RuntimeError. The
        registry payload must still build (never 500 GET /api/models app-wide)."""

        def _raise_generic(*_args, **_kwargs):
            raise Exception('relation "model_providers" does not exist')

        monkeypatch.setattr(db, "get_enabled_model_providers", _raise_generic)
        payload = m.registry_payload()
        assert payload["providers"] == []
        # And the rest of the payload is intact.
        assert payload["default"] == "sonnet"
        assert isinstance(payload["models"], list)
