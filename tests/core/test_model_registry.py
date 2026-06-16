"""Tests for the model registry, focused on the 1M-context suffix handling.

The 1M context window is no longer a separate dropdown model — it is a
capability toggled on a base model by appending ``[1m]`` to the stored ID.
These tests pin the helpers that round-trip that suffix.
"""

from app.core import model_registry as m


def test_registry_has_only_base_models():
    # No registry entry should carry the [1m] suffix anymore.
    assert all(not opt.value.endswith(m.CONTEXT_1M_SUFFIX) for opt in m.MODELS)
    # The base models that support 1M are flagged accordingly.
    by_value = {opt.value: opt for opt in m.MODELS}
    assert by_value["claude-opus-4-8"].supports_1m is True
    assert by_value["claude-haiku-4-5"].supports_1m is False


def test_has_1m_context():
    assert m.has_1m_context("claude-opus-4-8[1m]") is True
    assert m.has_1m_context("claude-opus-4-8") is False
    assert m.has_1m_context("") is False
    assert m.has_1m_context(None) is False
    # Legacy slug normalizes to a 1M canonical value.
    assert m.has_1m_context("opus[1m]") is True


def test_strip_context_suffix():
    assert m.strip_context_suffix("claude-opus-4-8[1m]") == "claude-opus-4-8"
    assert m.strip_context_suffix("claude-opus-4-8") == "claude-opus-4-8"
    assert m.strip_context_suffix("opus[1m]") == "claude-opus-4-8"
    assert m.strip_context_suffix("") == ""


def test_with_context_suffix_toggles():
    assert m.with_context_suffix("claude-opus-4-8", True) == "claude-opus-4-8[1m]"
    assert m.with_context_suffix("claude-opus-4-8[1m]", False) == "claude-opus-4-8"
    # Idempotent — enabling an already-1M model does not double the suffix.
    assert m.with_context_suffix("claude-opus-4-8[1m]", True) == "claude-opus-4-8[1m]"
    # Inherit / empty has nothing to toggle.
    assert m.with_context_suffix("", True) == ""
    # Works on custom models too.
    assert m.with_context_suffix("my-model", True) == "my-model[1m]"


def test_preset_detection_includes_1m_variants():
    assert m.is_preset_model("claude-opus-4-8") is True
    assert m.is_preset_model("claude-opus-4-8[1m]") is True
    assert m.is_preset_model("opus[1m]") is True
    assert m.is_preset_model("totally-custom") is False
    assert m.is_preset_model("") is False


def test_label_and_family_resolve_through_suffix():
    assert m.get_model_label("claude-opus-4-8") == "Opus 4.8"
    assert m.get_model_label("claude-opus-4-8[1m]") == "Opus 4.8 1M"
    assert m.get_model_family("claude-opus-4-8[1m]") == "opus"


def test_context_window_for_1m_variant():
    assert m.get_context_window("claude-opus-4-8") == 200_000
    assert m.get_context_window("claude-opus-4-8[1m]") == m.CONTEXT_1M_WINDOW
    # Custom model with the suffix still reports the 1M window.
    assert m.get_context_window("my-model[1m]") == m.CONTEXT_1M_WINDOW


def test_model_values_includes_1m_for_supported_models():
    values = m.model_values()
    assert "claude-opus-4-8" in values
    assert "claude-opus-4-8[1m]" in values
    # Haiku has no 1M variant.
    assert "claude-haiku-4-5" in values
    assert "claude-haiku-4-5[1m]" not in values


def test_registry_payload_shape():
    payload = m.registry_payload()
    assert payload["context_1m_suffix"] == m.CONTEXT_1M_SUFFIX
    models = payload["models"]
    assert isinstance(models, list)
    assert "supports_1m" in models[0]
