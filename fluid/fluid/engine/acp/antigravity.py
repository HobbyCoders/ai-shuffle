"""Antigravity specifics for the ACP engine.

Google's Antigravity CLI (``agy``, binary ``antigravity``) serves ACP with
``agy acp``. What this module knows about it was read from its own
``initialize`` reply (1.5.6) and Google's ACP docs; see
docs/internal/antigravity-acp-spike.md.

* The binary is run unmodified. Its sign-in lives in its own state directory
  under ``HOME`` (``~/.gemini/antigravity-cli``) or the OS keyring. Fluid never
  opens, copies or refreshes any of it: each provider row gets its own
  ``HOME`` (on Windows also ``APPDATA`` and ``LOCALAPPDATA`` inside it) so the
  agent keeps its own sign-in there, and only the agent reads it.
* ``gemini-api-key`` reads ``GEMINI_API_KEY`` from the agent's environment.
* Instructions travel as ``_meta.antigravity.systemPrompt.append`` on
  ``session/new`` (the agent advertises ``_meta.antigravity.systemPrompt.append``;
  it does not advertise the standard ``sessionCapabilities.systemPrompt``).
* A failed model call is reported as message text
  ("Agent execution terminated due to error.") with ``stopReason: end_turn``.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from fluid.engine.acp.client import AcpError, AgentInfo
from fluid.engine.acp.loop import AcpAgentSpec

PROVIDER = "antigravity"
OAUTH_PERSONAL = "oauth-personal"
OAUTH_BUSINESS = "oauth-business"
GEMINI_API_KEY = "gemini-api-key"
AGENT_PLATFORM = "agent-platform"
TERMINAL_LOGIN = "terminal-login"
AUTH_METHODS = (OAUTH_PERSONAL, OAUTH_BUSINESS, GEMINI_API_KEY, AGENT_PLATFORM, TERMINAL_LOGIN)
# Methods whose sign-in the agent keeps itself (a person signed in once).
STORED_SIGN_IN = (OAUTH_PERSONAL, OAUTH_BUSINESS, TERMINAL_LOGIN)
# The agent's thought_level values we know, least thinking first. The agent
# offers a per-model subset (1.5.6: low/high on Gemini 3.1 Pro; 1.5.9's
# combined ids name gemini-4-hydrogen-xhigh). "extra" is the older spelling of
# the step above high: it ranks with "xhigh", and whichever of the two the
# model offers is the one asked for.
THOUGHT_LEVELS = ("low", "medium", "high", "xhigh", "extra", "max")
_LEVEL_RANK = {"low": 0, "medium": 1, "high": 2, "xhigh": 3, "extra": 3, "max": 4}
# Our effort scale → the agent's level (matched against what it offers).
EFFORT_TO_THOUGHT = {"minimal": "low", "low": "low", "medium": "medium", "high": "high",
                     "xhigh": "xhigh", "max": "max"}
# The agent's level → our effort scale, for the levels a picker offers.
THOUGHT_TO_EFFORT = {"low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh",
                     "extra": "xhigh", "max": "max"}
_ERROR_TEXT = "Agent execution terminated due to error"
# Environment the agent may inherit from the host; everything else (API keys,
# database URLs, tokens) is left out.
_PASS_ENV = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT",
             "COMSPEC", "PATHEXT", "WINDIR", "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTPS_PROXY",
             "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy")


def agent_env(home: str, *, api_key: str | None = None, extra: Mapping[str, str] | None = None,
              source: Mapping[str, str] | None = None) -> dict[str, str]:
    """The agent's whole environment: a short pass-through list, its HOME (and
    on Windows its APPDATA/LOCALAPPDATA under it), and the row's key for
    ``gemini-api-key``."""
    src = os.environ if source is None else source
    env = {k: src[k] for k in _PASS_ENV if k in src}
    env["HOME"] = home
    env["USERPROFILE"] = home
    if sys.platform == "win32":
        # Windows programs keep their state under %APPDATA% / %LOCALAPPDATA%,
        # not HOME: point both into the row's own home too.
        for name, parts in (("APPDATA", ("AppData", "Roaming")), ("LOCALAPPDATA", ("AppData", "Local"))):
            path = os.path.join(home, *parts)
            try:
                os.makedirs(path, exist_ok=True)
            except OSError:
                pass
            env[name] = path
        # agy's own updater runs detached (no console), and its `agy --version`
        # child then gets a new console: a terminal window flashes on screen.
        # The app installs and updates agy itself (Settings > Providers). Only
        # "true" is honoured; agy 1.5.17 ignores "1".
        env["AGY_CLI_DISABLE_AUTO_UPDATE"] = "true"
    env.setdefault("TERM", "dumb")
    env["NO_BROWSER"] = "1"
    if api_key:
        env["GEMINI_API_KEY"] = api_key
    env.update(extra or {})
    return env


def split_model_id(model: str | None) -> tuple[str | None, str | None]:
    """``gemini-3.1-pro-high`` (the agent's combined model id) → (model, level)."""
    if not model:
        return None, None
    for level in THOUGHT_LEVELS:
        if model.endswith("-" + level):
            return model[: -len(level) - 1], level
    return model, None


def listed_models(text: str) -> list[dict[str, Any]]:
    """The models ``agy models`` prints, each ``{"id", "label", "levels"}``
    like :func:`offered_models`.

    One model per line, ``<combined id>\\t<label>`` (1.5.8/1.5.9:
    ``gemini-4-hydrogen-high\\tGemini 4 Hydrogen (High)``). Combined ids fold
    into one model per base id with its levels; the label loses its
    "(Level)" suffix. An id with no level suffix is its own model, its levels
    unknown (None). Lines that are not ``id\\tlabel`` are skipped.
    """
    order: list[str] = []
    labels: dict[str, str] = {}
    levels: dict[str, list[str]] = {}
    for line in (text or "").splitlines():
        model_id, sep, label = line.strip().partition("\t")
        model_id, label = model_id.strip(), label.strip()
        if not sep or not model_id or any(c.isspace() for c in model_id):
            continue
        base, level = split_model_id(model_id)
        base = base or model_id
        if level and label.lower().endswith(f"({level})"):
            label = label[: -len(level) - 2].rstrip()
        if base not in labels:
            order.append(base)
            labels[base] = label or base
        if level and level not in levels.setdefault(base, []):
            levels[base].append(level)
    return [{"id": base, "label": labels[base],
             "levels": sorted(levels[base], key=lambda v: _LEVEL_RANK.get(v, len(_LEVEL_RANK)))
             if levels.get(base) else None}
            for base in order]


def _option(options: Sequence[Mapping[str, Any]], kind: str) -> Mapping[str, Any] | None:
    for option in options:
        if option.get("id") == kind or option.get("category") == kind:
            return option
    return None


def _values(option: Mapping[str, Any] | None) -> list[tuple[str, str]]:
    """``(value, name)`` of a select option's choices."""
    out: list[tuple[str, str]] = []
    for choice in (option or {}).get("options") or []:
        if isinstance(choice, Mapping) and choice.get("value") is not None:
            value = str(choice["value"])
            out.append((value, str(choice.get("name") or value)))
    return out


def offered_models(options: Sequence[Mapping[str, Any]],
                   models: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The models the agent offers, each ``{"id", "label", "levels"}``.

    The ``model`` config option is the list (it is what ``set_config_option``
    can select); without one, the base ids of the legacy ``models`` reply.
    ``levels`` are the agent's own thought_level values for that model, from
    the combined ids in ``models.availableModels`` (``gemini-3.1-pro-high``);
    for the current model the ``thought_level`` option instead, when it has
    one. None when the agent said nothing about a model's levels.
    """
    choices = _values(_option(options, "model"))
    combined: list[str] = []
    if isinstance(models, Mapping):
        for entry in models.get("availableModels") or []:
            if isinstance(entry, Mapping) and entry.get("modelId"):
                combined.append(str(entry["modelId"]))
    if not choices:
        seen: dict[str, None] = {}
        for model_id in combined:
            seen.setdefault(split_model_id(model_id)[0] or model_id, None)
        choices = [(model_id, model_id) for model_id in seen]
    bases = sorted((value for value, _ in choices), key=len, reverse=True)
    levels: dict[str, list[str]] = {}
    for model_id in combined:
        # Longest offered id first: a level the agent names that we do not
        # know (yet) is still read from what follows the model's own id.
        base = next((b for b in bases if model_id.startswith(b + "-")), None)
        if base is not None:
            level = model_id[len(base) + 1:]
            if level and level not in levels.setdefault(base, []):
                levels[base].append(level)
    model_option = _option(options, "model")
    current = str(model_option.get("currentValue")) if model_option and model_option.get("currentValue") else None
    thought = [value for value, _ in _values(_option(options, "thought_level"))]
    if current and thought:
        # The thought_level option is the current model's list right now: it
        # wins over the combined ids (it may add or drop a level). It says
        # nothing about any other model.
        levels[current] = thought
    return [{"id": value, "label": name,
             "levels": sorted(levels[value], key=lambda v: _LEVEL_RANK.get(v, len(_LEVEL_RANK)))
             if value in levels else None}
            for value, name in choices]


@dataclass
class AntigravitySpec(AcpAgentSpec):
    provider: str = PROVIDER

    @classmethod
    def build(cls, binary: str, *, auth_mode: str, home: str, api_key: str | None = None,
              add_dirs: Sequence[str] = (), extra_env: Mapping[str, str] | None = None,
              version: str = "1") -> "AntigravitySpec":
        argv = [binary, "acp", "--disable-telemetry"]
        for directory in add_dirs:
            argv += ["--add-dir", directory]
        # Stored sign-ins need no authenticate call; the agent answers -32000
        # when there is none. Asking it to authenticate oauth-* here would try
        # to open a browser on the worker.
        method = auth_mode if auth_mode in (GEMINI_API_KEY, AGENT_PLATFORM) else None
        return cls(argv=argv, auth_method=method, env=agent_env(home, api_key=api_key, extra=extra_env),
                   client_info={"name": "ai-shuffle", "version": version})

    def client_capabilities(self) -> dict[str, Any]:
        caps = super().client_capabilities()
        caps["auth"] = {"terminal": True}
        return caps

    def system_prompt_params(self, info: AgentInfo, text: str | None) -> dict[str, Any]:
        if not text:
            return {}
        if info.standard_system_prompt:
            return {"systemPrompt": text}
        meta = (info.capabilities.get("_meta") or {}).get("antigravity") or {}
        if isinstance(meta, Mapping) and (meta.get("systemPrompt") or {}).get("append"):
            return {"_meta": {"antigravity": {"systemPrompt": {"append": text}}}}
        return {}

    def config_value(self, options: Sequence[Mapping[str, Any]], kind: str,
                     wanted: str | None) -> tuple[str, str] | None:
        choice = super().config_value(options, kind, wanted)
        if choice is not None:
            return choice
        if kind == "model":
            # A combined id (gemini-4-hydrogen-xhigh) names the model before its level.
            base = split_model_id(wanted)[0]
            return super().config_value(options, kind, base) if base != wanted else None
        if kind != "thought_level" or wanted not in _LEVEL_RANK:
            return None
        option = _option(options, "thought_level")
        if option is None:
            return None
        # The model's own spelling of the same step ("extra" for "xhigh"),
        # else the nearest level it offers, preferring the lower one.
        ranked = [v for v, _ in _values(option) if v in _LEVEL_RANK]
        if not ranked:
            return None
        target = _LEVEL_RANK[wanted]
        best = min(ranked, key=lambda v: (abs(_LEVEL_RANK[v] - target), _LEVEL_RANK[v]))
        return str(option.get("id")), best

    def effort_value(self, effort: str | None) -> str | None:
        return EFFORT_TO_THOUGHT.get(effort or "") if effort else None

    def offered_models(self, options: Sequence[Mapping[str, Any]],
                       models: Mapping[str, Any] | None) -> list[dict[str, Any]] | None:
        return offered_models(options, models) or None

    def turn_failure(self, text: str) -> str | None:
        if _ERROR_TEXT in (text or ""):
            return ("Antigravity ended the turn with an error. Check the provider's sign-in or "
                    "API key, and its quota.")
        return None

    def auth_message(self, exc: AcpError) -> str:
        return ("Antigravity is not signed in on this worker. Sign in again from "
                "Settings → Providers → Google Antigravity.")


def initialize_summary(raw: Mapping[str, Any]) -> dict[str, Any]:
    """The parts of ``initialize`` the status endpoint shows."""
    info = AgentInfo(dict(raw))
    caps = info.capabilities
    meta = (caps.get("_meta") or {}).get("antigravity") or {}
    return {
        "agent": info.name,
        "version": info.version,
        "protocol_version": raw.get("protocolVersion"),
        "auth_methods": [m.get("id") for m in info.auth_methods],
        "images": info.prompt_images,
        "resume": info.session_capability("resume"),
        "mcp_transports": sorted(info.mcp_transports),
        "system_prompt": "standard" if info.standard_system_prompt else (
            "append" if isinstance(meta, Mapping) and (meta.get("systemPrompt") or {}).get("append") else "none"),
    }
