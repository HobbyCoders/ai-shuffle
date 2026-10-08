"""Validated, versioned reasoning capabilities. None means unknown, [] unsupported."""
from __future__ import annotations
from typing import Any, Mapping

OPENROUTER_EFFORT_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
ANTHROPIC_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
# Ultra is advertised by the native Responses subscription catalog, not gateways.
RESPONSES_EFFORT_LEVELS = (*OPENROUTER_EFFORT_LEVELS, "ultra")
EFFORT_LEVELS = RESPONSES_EFFORT_LEVELS


def _capability(levels, default=None, mandatory=None):
    if levels is not None:
        if not isinstance(levels, (list, tuple)):
            levels = None
        else:
            values = [v.get("effort") if isinstance(v, dict) else v for v in levels[:100]]
            # Future string tokens must not erase recognized support. Malformed
            # declarations and all-unknown lists are unknown; only [] is explicit
            # evidence that reasoning effort is unsupported.
            known = [v for v in EFFORT_LEVELS if v in values]
            levels = (known if known or not values else None) if all(
                isinstance(v, str) for v in values
            ) else None
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
                levels = list(OPENROUTER_EFFORT_LEVELS)
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
_ANTHROPIC = {name: _capability(list(ANTHROPIC_EFFORT_LEVELS), "high") for name in (
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
_OPENAI["gpt-6-astra"] = _capability(["low", "medium", "high", "xhigh", "max"])
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
    allowed = (ANTHROPIC_EFFORT_LEVELS if route in ("anthropic", "delegated") else
               OPENROUTER_EFFORT_LEVELS if route == "openrouter" else EFFORT_LEVELS)
    if levels is not None:
        levels = [v for v in levels if v in allowed]
    default = cap["default_effort"]
    return {"effort_levels": levels, "default_effort": default if levels is not None and default in levels else None,
            "effort_metadata_version": version}


def clamp_effort(value: Any, model: str | None, provider: Mapping[str, Any] | None = None,
                 *, route: str | None = None) -> str | None:
    """Fit a remembered/inherited pick to declared levels, preserving unknown pins."""
    route = route or effort_route(provider)
    cap = effort_capabilities(model, provider, route=route)
    levels = cap["effort_levels"]
    if value not in EFFORT_LEVELS:
        return cap["default_effort"]
    if levels is None:
        return normalize_effort(value, model, provider, route=route)
    if not levels:
        return None
    rank = EFFORT_LEVELS.index(value)
    return min(levels, key=lambda level: (abs(EFFORT_LEVELS.index(level) - rank),
                                        EFFORT_LEVELS.index(level)))


MODEL_EFFORT_PREFIX = "composer.modelEffort."


def _effort_bare_model(model: str | None) -> str:
    from app.core.query_options import decode_provider_model
    decoded = decode_provider_model(model) if model else None
    bare = ((decoded[1] if decoded else model) or "").removesuffix("[1m]")
    return bare.removeprefix("anthropic/") if bare.startswith("anthropic/claude-") else bare


def canonical_effort_model(model: str | None) -> str:
    """Preserve concrete versions; unwrap only true aliases of that version."""
    import re
    from app.core.model_registry import API_MODEL_IDS
    bare = _effort_bare_model(model)
    bare = API_MODEL_IDS.get(bare, bare)
    return re.sub(r"-\d{8}$", "", bare) if bare.startswith("claude-") else bare


def legacy_effort_family(model: str | None) -> str | None:
    """Read-only compatibility with preferences previously collapsed by family."""
    import re
    match = re.match(r"^claude-(haiku|sonnet|opus|fable)-\d", canonical_effort_model(model))
    return match[1] if match else None


def effort_preference_key(model: str | None) -> str:
    from urllib.parse import quote
    return MODEL_EFFORT_PREFIX + quote(canonical_effort_model(model), safe="-_.!~*'()")


MODEL_FAST_PREFIX = "composer.modelFast."
#: Per-model composer records whose clears are kept as replicated nulls.
MODEL_PREFERENCE_PREFIXES = (MODEL_EFFORT_PREFIX, MODEL_FAST_PREFIX)


def fast_preference_key(model: str | None) -> str:
    from urllib.parse import quote
    return MODEL_FAST_PREFIX + quote(canonical_effort_model(model), safe="-_.!~*'()")


def canonical_model_preference_key(key: str) -> str:
    """One stored key per model, however the client spelled the model id."""
    from urllib.parse import unquote
    for prefix, build in ((MODEL_EFFORT_PREFIX, effort_preference_key),
                          (MODEL_FAST_PREFIX, fast_preference_key)):
        if key.startswith(prefix):
            return build(unquote(key[len(prefix):]))
    return key


def _caller_preference_identity(caller: Mapping[str, Any] | None) -> tuple[str, str]:
    """The preference owner behind a calling session row (the owner, or its API user)."""
    from app.core import preference_identity
    from app.db import mobile_tool_identity

    row = caller or {}
    principal = mobile_tool_identity.read(row["id"]) if row.get("id") else None
    if row.get("api_user_id"):
        principal = {"kind": "api_user", "principal_id": row["api_user_id"]}
    return preference_identity.principal_preference_identity(principal or {"kind": "admin"})


def _saved_model_preference(key: str, caller: Mapping[str, Any] | None) -> tuple[bool, Any]:
    """``(found, value)`` of the caller's owner's per-model record.

    A null record is a clear and counts as found.
    """
    from app.db import database

    pref = database.get_user_preference(*_caller_preference_identity(caller), key)
    return (True, pref.get("value")) if pref is not None else (False, None)


def resolve_child_effort(model: str | None, provider_id: str | None, *,
                         explicit: Any = None, parent: Any = None,
                         caller: Mapping[str, Any] | None = None) -> str | None:
    """Explicit child choice > authenticated owner's model pick > parent > default."""
    import logging
    from app.core.query_options import decode_provider_model
    from app.core.provider.harness_adapter import _decide_provider_kind
    from app.core.provider.types import SessionOptions
    from app.db import database
    from app.db.model_providers import get_model_provider

    decoded = decode_provider_model(model) if model else None
    bare = decoded[1] if decoded else model
    provider_id = decoded[0] if decoded else provider_id
    provider = get_model_provider(provider_id) if provider_id else None
    route = _decide_provider_kind(SessionOptions(
        model=bare, extra={"provider_id": provider_id} if provider_id else {}))
    saved = None
    if explicit not in EFFORT_LEVELS and bare:
        try:
            identity = _caller_preference_identity(caller)
            pref = database.get_user_preference(*identity, effort_preference_key(bare))
            if pref is not None:
                # A null per-model record is a clear, not permission to resurrect
                # an older whole-map or single-composer preference.
                saved = pref.get("value")
            else:
                from app.core.model_registry import API_MODEL_IDS
                legacy = database.get_user_preference(*identity, "composer.modelEfforts")
                values = legacy.get("value") if legacy else None
                values = values if isinstance(values, dict) else {}
                # A concrete choice in the old map still outranks any family fallback.
                concrete = next((key for key in values if _effort_bare_model(key) not in API_MODEL_IDS
                                 and canonical_effort_model(key) == canonical_effort_model(bare)), None)
                if concrete is not None:
                    saved = values[concrete]
                else:
                    family = legacy_effort_family(bare)
                    family_pref = database.get_user_preference(*identity, MODEL_EFFORT_PREFIX + family,
                                                               canonical=False) if family else None
                    if family_pref is not None:
                        saved = family_pref.get("value")
                    else:
                        saved = next((value for key, value in values.items()
                                      if _effort_bare_model(key) == family), None)
        except Exception:
            logging.getLogger(__name__).warning("Could not read model effort preference; inheriting caller effort", exc_info=True)
    selected = next((v for v in (explicit, saved, parent) if isinstance(v, str) and v in EFFORT_LEVELS), None)
    return clamp_effort(selected, bare, provider, route=route)


def resolve_child_fast(model: str | None, provider_id: str | None, *, parent: Any = None,
                       caller: Mapping[str, Any] | None = None,
                       profile_id: str | None = None) -> bool | None:
    """The owner's last fast-mode pick for the child's model, else the parent's.

    The result is fitted like any inherited fast mode
    (:func:`app.core.provider_agent.worker_fast`): on only where the child's
    model supports it. A child with no model of its own uses its agent's.
    """
    import logging
    from app.core.provider_agent import worker_fast
    from app.core.query_options import decode_provider_model

    decoded = decode_provider_model(model) if model else None
    bare = decoded[1] if decoded else model
    provider_id = decoded[0] if decoded else provider_id
    lookup = bare
    if not lookup and profile_id:
        try:
            from app.db import database
            lookup = ((database.get_profile(profile_id) or {}).get("config") or {}).get("model")
        except Exception:
            lookup = None
    selected = parent if isinstance(parent, bool) else None
    if lookup:
        try:
            found, saved = _saved_model_preference(fast_preference_key(lookup), caller)
            if found and isinstance(saved, bool):
                selected = saved
        except Exception:
            logging.getLogger(__name__).warning("Could not read model fast-mode preference; inheriting caller fast mode", exc_info=True)
    return worker_fast(selected, provider_id, bare, profile_id)


def normalize_effort(value: Any, model: str | None, provider: Mapping[str, Any] | None = None,
                     *, route: str | None = None) -> str | None:
    route = route or effort_route(provider)
    if not isinstance(value, str) or value not in EFFORT_LEVELS:
        return None
    if route in ("anthropic", "delegated") and value not in ANTHROPIC_EFFORT_LEVELS:
        return None
    if value == "ultra" and route != "openai_responses":
        return None
    levels = effort_capabilities(model, provider, route=route)["effort_levels"]
    return value if levels is None or value in levels else None
