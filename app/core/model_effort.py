"""Route-effective effort capabilities shared by catalog, query and adapter.

An empty list means omit effort, not disable the model's reasoning. Metadata
is authoritative when present; missing metadata uses a deliberately small roster.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def sanitize_effort_metadata(entry: Mapping[str, Any]) -> dict:
    """Whitelist catalog fields; malformed declarations fail closed, not fallback."""
    out = {}
    if "supported_parameters" in entry:
        raw = entry["supported_parameters"]
        out["supported_parameters"] = list(dict.fromkeys(
            v for v in (raw[:100] if isinstance(raw, list) else [])
            if isinstance(v, str) and len(v) <= 100
        ))
    if "supported_reasoning_levels" in entry:
        raw = entry["supported_reasoning_levels"]
        values = [v.get("effort") if isinstance(v, dict) else v
                  for v in (raw[:100] if isinstance(raw, list) else [])]
        out["supported_reasoning_levels"] = [v for v in EFFORT_LEVELS if v in values]
    if "default_reasoning_level" in entry:
        value = entry["default_reasoning_level"]
        out["default_reasoning_level"] = value if value in EFFORT_LEVELS else None
    return out


def effort_route(provider: Mapping[str, Any] | None) -> str:
    row = provider or {}
    if row.get("provider_type") in ("openai", "chatgpt", "xai", "grok") or row.get("auth_mode") in ("chatgpt", "grok"):
        return "openai_responses"
    if row.get("provider_type") == "openrouter" or row.get("api_dialect") == "openai":
        return "openrouter"
    return "anthropic"


def _fallback(model: str) -> list[str]:
    # Strip an author namespace, never rewrite the caller's actual API id.
    name = model.lower().split("/")[-1].removesuffix("[1m]")
    if name in ("sonnet", "opus", "fable"):
        return list(EFFORT_LEVELS)
    if re.fullmatch(r"claude-(sonnet|opus|fable)-5(?:[.-]\d+)?(?:-\d{8})?", name):
        return list(EFFORT_LEVELS)
    if re.fullmatch(r"claude-opus-4-[78](?:-\d{8})?", name):
        return list(EFFORT_LEVELS)
    if re.fullmatch(r"claude-opus-4-6(?:-\d{8})?", name):
        return ["low", "medium", "high", "max"]
    if re.fullmatch(r"claude-(opus-4-5|sonnet-4-6)(?:-\d{8})?", name):
        return list(EFFORT_LEVELS[:3])
    from app.core.model_catalog_presets import CODEX_PRESET_MODELS, OPENAI_PRESET_MODELS
    if name in {m["id"] for m in CODEX_PRESET_MODELS + OPENAI_PRESET_MODELS}:
        return list(EFFORT_LEVELS[:3])
    if name == "gpt-5.4-pro":
        return ["medium", "high"]
    if name in ("grok-4.6", "grok-4.5", "grok-4.3"):
        return list(EFFORT_LEVELS[:3])
    return []


def effort_capabilities(model: str | None, provider: Mapping[str, Any] | None = None,
                        model_entry: Mapping[str, Any] | None = None, *, route: str | None = None) -> dict:
    """Return the public registry contract for an exact selected model and route."""
    if model_entry is None:
        entries = (provider or {}).get("models_json") or []
        model_entry = next((m for m in entries if isinstance(m, dict) and m.get("id") == model), {})
    metadata = sanitize_effort_metadata(model_entry)
    route = route or effort_route(provider)
    if "supported_reasoning_levels" in metadata:
        levels = metadata["supported_reasoning_levels"]
    elif "supported_parameters" in metadata:
        parameters = metadata["supported_parameters"]
        if "reasoning_effort" in parameters:
            levels = list(EFFORT_LEVELS[:3])
        elif "reasoning" in parameters:
            # OpenRouter advertises its reasoning object, not each nested key.
            # Only known effort-capable models may infer levels from that object.
            levels = _fallback(model or "")
        else:
            levels = []
    else:
        levels = _fallback(model or "")
        # A non-Claude model served via Anthropic compatibility is not evidence
        # that it accepts Anthropic output_config.effort (notably GLM).
        if route in ("anthropic", "delegated") and not ((model or "").startswith("claude-") or model in ("sonnet", "opus", "fable")):
            levels = []
    if route in ("openrouter", "openai_responses"):
        levels = [v for v in levels if v in EFFORT_LEVELS[:3]]
    default = metadata.get("default_reasoning_level")
    return {"effort_levels": levels, "default_effort": default if default in levels else None}


def normalize_effort(value: Any, model: str | None, provider: Mapping[str, Any] | None = None,
                     *, route: str | None = None) -> str | None:
    """Suppress stale incompatible effort, preserving existing upper-level clamp."""
    route = route or effort_route(provider)
    levels = effort_capabilities(model, provider, route=route)["effort_levels"]
    if route in ("openrouter", "openai_responses") and value in ("xhigh", "max"):
        value = "high"
    return value if isinstance(value, str) and value in levels else None
