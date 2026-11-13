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
# Our effort scale → the agent's thought_level values, in order. The agent
# offers a per-model subset (1.5.6: low/high on Gemini 3.1 Pro).
THOUGHT_LEVELS = ("low", "medium", "high", "extra", "max")
EFFORT_TO_THOUGHT = {"minimal": "low", "low": "low", "medium": "medium", "high": "high",
                     "xhigh": "extra", "max": "max"}
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
        if kind == "model":
            wanted = split_model_id(wanted)[0]
        choice = super().config_value(options, kind, wanted)
        if choice is not None or kind != "thought_level" or wanted not in THOUGHT_LEVELS:
            return choice
        # Nearest level the model offers, preferring the lower one.
        for option in options:
            if option.get("id") != "thought_level" and option.get("category") != "thought_level":
                continue
            offered = [str(v.get("value")) for v in option.get("options") or [] if isinstance(v, Mapping)]
            ranked = [v for v in THOUGHT_LEVELS if v in offered]
            if not ranked:
                return None
            target = THOUGHT_LEVELS.index(wanted)
            best = min(ranked, key=lambda v: (abs(THOUGHT_LEVELS.index(v) - target), THOUGHT_LEVELS.index(v)))
            return str(option.get("id")), best
        return None

    def effort_value(self, effort: str | None) -> str | None:
        return EFFORT_TO_THOUGHT.get(effort or "") if effort else None

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
