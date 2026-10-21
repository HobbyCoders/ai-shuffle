"""Validation and server-specific translation of explicit local model settings."""
import pytest

from app.core.local_model_settings import local_request_extra, validate_local_settings


def row(config):
    return {"provider_type": "custom", "api_dialect": "openai", "models_json": [
        {"id": "local", "local_settings": config}]}


@pytest.mark.parametrize("config", [
    {}, [], {"server": "unknown"}, {"server": "vllm", "preset": []},
    {"server": "vllm", "reasoning": "true"},
    {"server": "vllm", "sampling": {"temperature": True}},
    {"server": "vllm", "sampling": {"temperature": float("nan")}},
    {"server": "vllm", "sampling": {"temperature": -1}},
    {"server": "vllm", "sampling": {"top_p": 1.1}},
    {"server": "vllm", "sampling": {"min_p": -0.1}},
    {"server": "vllm", "sampling": {"top_k": 2.5}},
    {"server": "vllm", "sampling": {"repeat_penalty": 0}},
    {"server": "vllm", "sampling": {"messages": []}},
    {"server": "vllm", "extra": {}},
])
def test_reject_invalid(config):
    with pytest.raises(ValueError):
        validate_local_settings(config)


@pytest.mark.parametrize("server,penalty,top_k", [
    ("llama_cpp", "repeat_penalty", 0), ("vllm", "repetition_penalty", -1),
])
def test_sampling_translation_and_disabled_reasoning(server, penalty, top_k):
    config = {"server": server, "sampling": {"repeat_penalty": 1.1, "top_k": 0},
              "reasoning": False}
    assert local_request_extra(row(config), "local") == {
        penalty: 1.1, "top_k": top_k, "chat_template_kwargs": {"enable_thinking": False}}


@pytest.mark.parametrize("preset,temp,top_p,thinking", [
    ("qwen3-thinking", 0.6, 0.95, True), ("qwen3-non-thinking", 0.7, 0.8, False),
])
def test_vendor_presets_are_explicit_and_overridable(preset, temp, top_p, thinking):
    config = {"server": "vllm", "preset": preset}
    assert local_request_extra(row(config), "local") == {
        "temperature": temp, "top_p": top_p, "top_k": 20, "min_p": 0.0,
        "chat_template_kwargs": {"enable_thinking": thinking}}
    config.update(sampling={"temperature": 0.0}, reasoning=not thinking)
    extra = local_request_extra(row(config), "local")
    assert extra["temperature"] == 0.0
    assert extra["chat_template_kwargs"] == {"enable_thinking": not thinking}


@pytest.mark.parametrize("config", [
    {"server": "sglang"},
    {"server": "vllm", "sampling": {"temperature": "private-invalid-value"}},
    {"server": "vllm", "future_setting": "private-invalid-value"},
    "private-invalid-value",
])
def test_invalid_synced_settings_keep_sessions_usable_without_logging_values(caplog, config):
    with caplog.at_level("WARNING", logger="app.core.local_model_settings"):
        assert local_request_extra(row(config), "local") == {}
    assert len(caplog.records) == 1
    assert "local model settings" in caplog.text
    assert "private-invalid-value" not in caplog.text
    assert "sglang" not in caplog.text
    assert "temperature" not in caplog.text


def test_unset_unknown_models_and_other_routes_keep_server_defaults():
    assert local_request_extra(row(None), "local") == {}
    assert local_request_extra(row({"server": "vllm"}), "local") == {}
    configured = row({"server": "vllm", "reasoning": False})
    assert local_request_extra(configured, "different") == {}
    configured["provider_type"] = "openrouter"
    assert local_request_extra(configured, "local") == {}
