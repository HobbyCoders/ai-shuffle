"""Tests for the model registry.

The presets are Agent SDK shorthand aliases (``haiku``, ``sonnet``, ``opus``,
``fable``). The 1M context window is a capability toggled on a base model by
appending ``[1m]`` to the stored value. These tests pin the registry contents,
the flipped legacy map (older full model IDs → shorthand), and the helpers that
round-trip the suffix.
"""

from app.core import model_registry as m


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
