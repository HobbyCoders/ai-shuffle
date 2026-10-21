"""Opt-in, per-model knobs for declared local OpenAI-compatible servers.

No model-name guessing: a vendor preset must be selected explicitly. Unset
knobs leave server defaults intact; existing provider entries are unchanged.
"""
from __future__ import annotations

import math
from typing import Any

# Qwen3 model card: https://huggingface.co/Qwen/Qwen3-8B#best-practices
# Explicit opt-in only: these are recommendations, not measured quality wins.
_PRESETS = {
    "qwen3-thinking": {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0},
    "qwen3-non-thinking": {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "min_p": 0.0},
}
_RANGES = {"temperature": (0, 2), "top_p": (0, 1), "top_k": (0, None),
           "min_p": (0, 1), "repeat_penalty": (0, None)}


def validate_local_settings(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, dict) or set(value) - {"server", "preset", "sampling", "reasoning"}:
        raise ValueError("local_settings must contain only server, preset, sampling, reasoning")
    if value.get("server") not in ("llama_cpp", "vllm"):
        raise ValueError("local_settings.server must be llama_cpp or vllm")
    if "preset" in value and (not isinstance(value["preset"], str) or value["preset"] not in _PRESETS):
        raise ValueError("local_settings.preset must be qwen3-thinking or qwen3-non-thinking")
    if "reasoning" in value and not isinstance(value["reasoning"], bool):
        raise ValueError("local_settings.reasoning must be a boolean")
    sampling = value.get("sampling", {})
    if not isinstance(sampling, dict) or set(sampling) - _RANGES.keys():
        raise ValueError("local_settings.sampling contains an unknown parameter")
    for key, number in sampling.items():
        low, high = _RANGES[key]
        if (isinstance(number, bool) or not isinstance(number, (int, float))
                or (isinstance(number, float) and not math.isfinite(number)) or number < low
                or (high is not None and number > high)
                or (key == "top_k" and not isinstance(number, int))
                or (key == "repeat_penalty" and number == 0)):
            raise ValueError(f"invalid local_settings.sampling.{key}")


def local_request_extra(row: dict | None, model: str | None) -> dict:
    """Translate only an explicitly configured custom OpenAI model's knobs."""
    if not row or row.get("provider_type") != "custom" or row.get("api_dialect") != "openai":
        return {}
    entry = next((m for m in (row.get("models_json") or [])
                  if isinstance(m, dict) and m.get("id") == model), {})
    config = entry.get("local_settings")
    validate_local_settings(config)
    if config is None:
        return {}
    preset = config.get("preset")
    extra = {**_PRESETS.get(preset, {}), **config.get("sampling", {})}
    if config["server"] == "vllm":
        if "repeat_penalty" in extra:
            extra["repetition_penalty"] = extra.pop("repeat_penalty")
        # vLLM uses -1, not 0, to disable top-k filtering.
        if extra.get("top_k") == 0:
            extra["top_k"] = -1
    reasoning = config.get("reasoning")
    if reasoning is None and preset:
        reasoning = preset == "qwen3-thinking"
    if reasoning is not None:
        extra["chat_template_kwargs"] = {"enable_thinking": reasoning}
    return extra
