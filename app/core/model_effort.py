"""Validated, versioned reasoning capabilities. None means unknown, [] unsupported."""
from __future__ import annotations
from typing import Any, Mapping

EFFORT_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


def _capability(levels, default=None, mandatory=None):
    if levels is not None:
        if not isinstance(levels, (list, tuple)):
            levels = None
        else:
            values = [v.get("effort") if isinstance(v, dict) else v for v in levels[:100]]
            # Invalid declarations are unknown, not evidence of no support.
            levels = ([v for v in EFFORT_LEVELS if v in values]
                      if all(isinstance(v, str) and v in EFFORT_LEVELS for v in values) else None)
    if mandatory is True and levels is not None:
        levels = [v for v in levels if v != "none"]
    result = {"version": 1, "effort_levels": levels,
              "default_effort": default if isinstance(default, str) and levels is not None and default in levels else None}
    if isinstance(mandatory, bool):
        result["mandatory"] = mandatory
    return result


def sanitize_effort_metadata(entry: Mapping[str, Any], *, provider_type=None, complete=False) -> dict:
    """Normalize public OR, Codex and xAI catalog schemas (never infer from names)."""
    canonical = entry.get("reasoning_capabilities")
    if isinstance(canonical, Mapping) and canonical.get("version") == 1:
        cap = _capability(canonical.get("effort_levels"), canonical.get("default_effort"), canonical.get("mandatory"))
    elif "reasoning_capabilities" in entry:
        cap = _capability(None)
    elif isinstance(entry.get("reasoning"), Mapping):
        reasoning = entry["reasoning"]
        if "supported_efforts" in reasoning:
            levels = reasoning["supported_efforts"]
            # OpenRouter null explicitly means the entire gateway vocabulary.
            if levels is None:
                levels = list(EFFORT_LEVELS)
            cap = _capability(levels, reasoning.get("default_effort"), reasoning.get("mandatory"))
        else:
            cap = _capability([] if complete and provider_type == "openrouter" else None)
    elif "supported_reasoning_levels" in entry:
        cap = _capability(entry["supported_reasoning_levels"], entry.get("default_reasoning_level"))
    else:
        features = entry.get("features")
        options = (features.get("reasoningEffortOptions") if isinstance(features, Mapping) else None)
        options = options or entry.get("reasoningEffortOptions")
        if isinstance(options, Mapping):
            cap = _capability(options.get("supportedEfforts"), options.get("defaultEffort"))
        elif "supportedEfforts" in entry:
            cap = _capability(entry["supportedEfforts"], entry.get("defaultEffort"))
        elif "supported_efforts" in entry:
            cap = _capability(entry["supported_efforts"], entry.get("default_effort"), entry.get("mandatory"))
        elif complete and provider_type == "openrouter":
            cap = _capability([])
        elif complete and isinstance(features, Mapping) and provider_type in ("xai", "grok"):
            cap = _capability([])
        elif complete or "supported_parameters" in entry or "default_reasoning_level" in entry:
            cap = _capability(None)
        else:
            return {}
    return {"reasoning_capabilities": cap}


def effort_route(provider: Mapping[str, Any] | None) -> str:
    row = provider or {}
    if row.get("provider_type") in ("openai", "chatgpt", "xai", "grok") or row.get("auth_mode") in ("chatgpt", "grok"):
        return "openai_responses"
    if row.get("provider_type") == "openrouter" or row.get("api_dialect") == "openai":
        return "openrouter"
    return "anthropic"


# Exact native API entries, not gateway or subscription inferences.
# https://platform.claude.com/docs/en/build-with-claude/effort.md
_ANTHROPIC = {name: _capability(list(EFFORT_LEVELS[2:]), "high") for name in (
    "claude-fable-5-1", "claude-mythos-5-1", "claude-fable-5", "claude-mythos-5",
    "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-sonnet-5")}
_ANTHROPIC.update({name: _capability(["low", "medium", "high", "max"], "high")
                   for name in ("claude-mythos-preview", "claude-opus-4-6", "claude-sonnet-4-6")})
_ANTHROPIC["claude-opus-4-5-20251101"] = _capability(["low", "medium", "high"], "high")
# Native aliases are resolved by app/core/model_registry.py, not model-name patterns.
_ALIASES = {"sonnet": "claude-sonnet-5", "opus": "claude-opus-5", "fable": "claude-fable-5"}
# https://code.claude.com/docs/en/model-config.md#adjust-effort-level
_CLI = {name: dict(_ANTHROPIC[name]) for name in (
    "claude-fable-5-1", "claude-fable-5", "claude-opus-5", "claude-sonnet-5",
    "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6")}
_CLI["claude-opus-4-7"]["default_effort"] = "xhigh"
for _name in _ANTHROPIC:
    _CLI.setdefault(_name, _capability([]))
for _name in ("haiku", "claude-haiku-4-5", "claude-haiku-4-5-20251001"):
    _ANTHROPIC[_name] = _capability([])
    _CLI[_name] = _capability([])
# https://developers.openai.com/api/docs/models/<exact-id>.md
_OPENAI = {name: _capability(["none", "low", "medium", "high", "xhigh", "max"], "medium")
           for name in ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")}
_OPENAI["gpt-6-astra"] = _capability(list(EFFORT_LEVELS[2:]))
_OPENAI.update({name: _capability(["none", "low", "medium", "high", "xhigh"], "none")
                for name in ("gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano")})
_OPENAI["gpt-5.5"] = _capability(["none", "low", "medium", "high", "xhigh"], "medium")
# https://docs.x.ai/developers/model-capabilities/text/reasoning
# The public embedded catalog advertises xhigh for 4.5, while prose says it is
# treated as high. Keep catalog metadata authoritative when present; absent that
# record, only the current documented native scale is established.
_XAI = {
    "grok-4.6": _capability(["low", "medium", "high", "xhigh"], "high"),
    "grok-4.5": _capability(["low", "medium", "high"], "high"),
}


def effort_capabilities(model: str | None, provider: Mapping[str, Any] | None = None,
                        model_entry: Mapping[str, Any] | None = None, *, route: str | None = None) -> dict:
    if model_entry is None:
        model_entry = next((m for m in (provider or {}).get("models_json", [])
                            if isinstance(m, dict) and m.get("id") == model), {})
    cap = sanitize_effort_metadata(model_entry).get("reasoning_capabilities")
    version = int(isinstance(model_entry.get("reasoning_capabilities"), Mapping)
                  and model_entry["reasoning_capabilities"].get("version") == 1)
    route = route or effort_route(provider)
    if cap is None:
        if provider is None:
            cap = (_CLI if route == "delegated" else _ANTHROPIC).get(_ALIASES.get(model, model))
        elif provider.get("provider_type") == "openai" and provider.get("auth_mode") not in ("chatgpt", "grok"):
            cap = _OPENAI.get(model)
        elif provider.get("provider_type") in ("xai", "grok"):
            cap = _XAI.get(model)
    cap = cap or _capability(None)
    levels = cap["effort_levels"]
    if route in ("anthropic", "delegated") and levels is not None:
        levels = [v for v in levels if v not in ("none", "minimal")]
    default = cap["default_effort"]
    return {"effort_levels": levels, "default_effort": default if levels is not None and default in levels else None,
            "effort_metadata_version": version}


def normalize_effort(value: Any, model: str | None, provider: Mapping[str, Any] | None = None,
                     *, route: str | None = None) -> str | None:
    route = route or effort_route(provider)
    if not isinstance(value, str) or value not in EFFORT_LEVELS:
        return None
    if route in ("anthropic", "delegated") and value in ("none", "minimal"):
        return None
    levels = effort_capabilities(model, provider, route=route)["effort_levels"]
    return value if levels is None or value in levels else None
