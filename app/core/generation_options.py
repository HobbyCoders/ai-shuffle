"""
Generation call option builder.

Builds a `AgentOptions` for one-shot AI generation calls (commit
messages, PR descriptions, branch names, profile / subagent generation, etc.)
from the **text-generation setting** — one model + effort pair configured under
Settings > Default models, shared by every internal generation call.

All generation calls run through the provider adapter seam
(`app.core.provider.factory.make_adapter`), exactly like chat: a
Claude-subscription binding takes the delegated Claude Code CLI leg, anything else
takes the fluid native leg. One throwaway adapter per call — no warm pool, no
cross-call context bleed.

Config honoring:
- ``text_generation_config()`` supplies model / provider / effort from the
  system setting. Callers may still pass a config dict of their own (the
  profile-shaped path, kept for tests and for a caller with a bespoke binding),
  in which case its model, permission_mode, setting_sources, thinking_mode,
  effort, max_thinking_tokens, system_prompt, env and extra_args are honored.
- Always force-disabled for one-shot generation: tools, agents, MCP servers,
  hooks, file checkpointing, partial messages. These would add cold-start
  overhead without benefit.

Workspace override:
- Every generation subprocess runs in the configured workspace directory
  (`settings.effective_workspace_dir`) so CLAUDE.md / .claude/ auto-discovery
  is predictable and matches the user's dev environment.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from app.core.agent_types import AgentOptions

from app.core.model_registry import DEFAULT_MODEL

logger = logging.getLogger(__name__)

# Sentinel distinguishing "caller passed no binding" (use the profile's
# config.provider — today's behavior) from an explicit per-session binding
# (authoritative, including an explicit None = native Claude). Multi-provider
# phase-2 Chunk E: the generation path is session-less today, so callers that
# have a bound session can pass its provider; everyone else keeps config.provider.
_UNSET_PROVIDER = object()


# Working directory for ALL generation-subprocess spawns. Resolved to the
# configured workspace_dir on first use so CLAUDE.md / .claude/ auto-discovery
# behavior is predictable across installs (as opposed to a random tempdir which
# can confuse hooks or downstream tools that key off the cwd). Plugin loading
# is suppressed separately via the `settings` override below, so the cwd's
# proximity to user-level config files no longer matters for per-call latency.
def get_generation_cwd() -> Optional[str]:
    """Return the cwd used for generation subprocess calls.

    Uses the configured workspace directory so the subprocess runs somewhere
    stable and mirrors the user's dev environment. Returns None if settings
    aren't importable yet (startup race) — callers skip cwd in that case.
    """
    try:
        from app.core.config import settings
        return str(settings.effective_workspace_dir)
    except Exception:
        return None


# JSON merged into `--settings` for every generation subprocess spawn. Forces
# `enabledPlugins` to the empty map so user-global plugins registered in
# `~/.claude/settings.json` (e.g. caveman, skill packs) don't attach their
# SessionStart / UserPromptSubmit hooks to generation calls. Those hooks
# typically shell out to PowerShell/bash per query; on Windows each PowerShell
# spawn is ~1-3s cold, which stacks into a 6-10s floor on short prompts.
# Generation calls never need plugin behavior (commit messages, PR
# descriptions, branch names — all short one-shots).
_GEN_SETTINGS_OVERRIDE = json.dumps({"enabledPlugins": {}})


# Thinking budget mapping — mirrors `app.core.query_options.THINKING_TOKEN_BUDGETS`.
# Replicated here to avoid importing the heavy `query_options` module (which
# pulls in MCP, agents, hooks, and provider wiring irrelevant to generation).
_THINKING_TOKEN_BUDGETS: Dict[str, int] = {
    "minimal": 4000,
    "basic": 10000,
    "medium": 20000,
    "thorough": 32000,
    "extensive": 60000,
}


# ---------------------------------------------------------------------------
# Profile -> AgentOptions
# ---------------------------------------------------------------------------


def _resolve_setting_sources(
    profile_config: Optional[Dict[str, Any]],
) -> List[str]:
    """
    Resolve `setting_sources` for generation calls from the caller's config.

    Generation calls (commit messages, PR descriptions, branch names, etc.) are
    short one-shot prompts that have nothing to do with user-global Claude Code
    settings. We therefore default to blocking user + project settings and only
    opt-in to extra scopes when the user explicitly configures a non-empty list.

    Blocking matters because `~/.claude/settings.json` commonly carries
    `effortLevel: medium`. Haiku 4.5 doesn't support effort, and when the CLI
    receives it the call takes a slow fallback path (12-17s for a 50-byte
    prompt vs 1-3s baseline). Chat sessions use their own options builder and
    are unaffected by this default.

    An empty list is never returned: `["local"]` pins the scope filter to one
    explicit, usually absent file.

    Behavior:
    - Non-empty list in profile: pass through unchanged — user explicitly opted
      in to loading those scopes for generation tasks.
    - `None`, `[]`, missing key, or non-list value: return `["local"]`, which
      scopes loading to `.claude/settings.local.json` in
      the working directory (usually absent), cutting off the user-global
      leak path.
    """
    if profile_config:
        sources = profile_config.get("setting_sources")
        if isinstance(sources, list) and len(sources) > 0:
            return sources
    # All "fall back to default" cases — still return a non-empty list.
    # `local` is the safest sentinel since the file it
    # points at rarely exists and never carries `effortLevel`.
    return ["local"]


def _resolve_thinking_and_effort(
    profile_config: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Map profile's `thinking_mode` + `effort` to AgentOptions kwargs.

    Mirrors `app.core.query_options._build_thinking_config` so generation calls
    behave the same as chat sessions for extended-thinking / effort config.
    """
    result: Dict[str, Any] = {}
    if not profile_config:
        return result

    thinking_mode = profile_config.get("thinking_mode", "default")
    if thinking_mode == "disabled":
        result["thinking"] = {"type": "disabled"}
    elif thinking_mode == "adaptive":
        result["thinking"] = {"type": "adaptive"}
    elif thinking_mode in _THINKING_TOKEN_BUDGETS:
        result["thinking"] = {
            "type": "enabled",
            "budget_tokens": _THINKING_TOKEN_BUDGETS[thinking_mode],
        }
    # "default" or unrecognized: omit, so the leg uses its default.

    effort = profile_config.get("effort")
    if effort in ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"):
        result["effort"] = effort

    max_thinking_tokens = profile_config.get("max_thinking_tokens")
    if max_thinking_tokens:
        result["max_thinking_tokens"] = max_thinking_tokens

    return result


def _resolve_thinking_display(profile_config: Optional[Dict[str, Any]]) -> Optional[str]:
    """Mirror of ``query_options._resolve_thinking_display`` for generation calls.

    Returned value is applied to ``options.extra_args["thinking-display"]``
    after construction so it reaches the CLI regardless of whether the
    installed 0.1.65 wheel includes the typed ``display`` field.
    """
    if not profile_config:
        return None
    value = profile_config.get("thinking_display")
    if value in ("summarized", "omitted"):
        return value
    return None


def _resolve_env(
    profile_config: Optional[Dict[str, Any]],
    model: Optional[str] = None,
    provider_id: Optional[str] = None,
) -> Dict[str, str]:
    """
    Merge profile's env with the fixed generation overrides.

    Always forces `CLAUDECODE=""` to prevent "nested session" detection when
    AI Shuffle is started from within a Claude Code session. Other env vars
    from the profile pass through.

    Claude Code authentication is added only when the adapter chooses the
    delegated CLI leg, mirroring chat. These options never carry stored Claude
    cloud credentials into a native generation call.

    When ``provider_id`` is set the profile targets a non-Anthropic model
    provider: the Anthropic key is NOT injected, and the gateway env is overlaid
    via ``apply_provider_env`` (base URL, bearer token, mitigation flags,
    small-fast/subagent model) — same as the chat path. A misconfigured provider
    raises ``ProviderConfigError``; we never silently fall back to native
    Anthropic.
    """
    env: Dict[str, str] = {}
    if profile_config:
        profile_env = profile_config.get("env")
        if isinstance(profile_env, dict):
            env.update({str(k): str(v) for k, v in profile_env.items()})
    env["CLAUDECODE"] = ""

    # The adapter chooses the runtime later. CLI authentication is injected
    # there, only for DelegatedLoop; a NativeLoop must not receive cloud auth.
    from fluid.tools.child_env import scrub_claude_auth_env

    env = scrub_claude_auth_env(env)

    # Overlay the model-provider env when a provider is set. NOT wrapped
    # in a best-effort try/except: a misconfigured provider must surface,
    # never silently route generation through native Anthropic.
    if provider_id:
        from app.core.environment import apply_provider_env
        apply_provider_env(env, provider_id, model)
    return env


# ---------------------------------------------------------------------------
# The text-generation setting
# ---------------------------------------------------------------------------
#
# One model + effort pair, stored in ``system_settings`` and edited under
# Settings > Default models. Every internal generation call runs on it: session and
# thread titles, commit messages, PR descriptions, branch names, agent and
# subagent generation. It replaced the old "Default AI" agent, which pinned
# these on an agent row even though generation never runs in a session.

TEXT_GENERATION_MODEL_KEY = "text_generation_model"
TEXT_GENERATION_EFFORT_KEY = "text_generation_effort"

VALID_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")


def text_generation_config() -> Dict[str, Any]:
    """Return the configured text-generation binding as a config dict.

    Shaped like the profile config ``build_generation_options`` already
    consumes: ``model`` (a bare id — a ``prov:<providerId>:<modelId>`` setting
    is split here), ``provider`` (``model_providers.id``, or None for native
    Anthropic) and ``effort``. When the setting is unset or unreadable it falls
    back to ``credential_gate.default_run_model(prefer_most_used=False)`` (the
    registry default while Claude is set up, else a usable provider's default
    model; never the owner's most used chat model), so generation always has
    somewhere to run.
    """
    model: Optional[str] = None
    effort: Optional[str] = None
    try:
        from app.db import database

        model = (database.get_system_setting(TEXT_GENERATION_MODEL_KEY) or "").strip() or None
        effort = (database.get_system_setting(TEXT_GENERATION_EFFORT_KEY) or "").strip() or None
    except Exception:  # noqa: BLE001 — an unreadable setting is not a failure
        logger.debug("Could not read the text-generation setting", exc_info=True)

    provider_id: Optional[str] = None
    if model:
        from app.core.query_options import decode_provider_model

        decoded = decode_provider_model(model)
        if decoded:
            provider_id, model = decoded
    else:
        # Unset: a usable provider's default, without the owner's most used
        # chat model (that may be an expensive model for titles and
        # suggestions). Cached Claude probe only: some callers run on the
        # event loop.
        from app.core.credential_gate import default_run_model

        provider_id, model = default_run_model(probe=False, prefer_most_used=False)

    return {
        "model": model or DEFAULT_MODEL,
        "provider": provider_id,
        "effort": effort if effort in VALID_EFFORTS else None,
    }


def build_generation_options(
    profile_config: Optional[Dict[str, Any]],
    model_override: Optional[str] = None,
    max_turns: Optional[int] = None,
    session_provider_id: Any = _UNSET_PROVIDER,
) -> AgentOptions:
    """
    Build a `AgentOptions` for a one-shot generation call.

    Honored from ``profile_config`` (callers pass ``text_generation_config()``):
    - `model` (unless `model_override` is supplied)
    - `permission_mode` (default `bypassPermissions`)
    - `setting_sources` (via `_resolve_setting_sources` workaround)
    - `thinking_mode` / `effort` / `max_thinking_tokens`
    - `system_prompt` (when present and non-empty)
    - `env` (merged with `CLAUDECODE=""`)
    - `extra_args`

    Always forced off for generation calls:
    - tools, agents, MCP servers, hooks, file checkpointing, partial
      messages.

    `max_turns` is passed through verbatim; callers typically set 1 for
    single-prompt generation.

    `session_provider_id` (optional): a per-session provider binding. When
    supplied it is authoritative over the profile's `config.provider` (an
    explicit None keeps the call native). Omit it for the ordinary session-less
    generation path, which uses `config.provider`.
    """
    config: Dict[str, Any] = profile_config or {}

    # Model: explicit override > profile > hard fallback
    model = model_override or config.get("model") or DEFAULT_MODEL

    # Model provider. None =
    # native Anthropic; when set, the env is overlaid with the provider's vars
    # inside _resolve_env. When a caller supplies a per-session binding
    # (session_provider_id) it is authoritative — the session's bound provider
    # wins over the profile's config.provider, and an explicit None keeps the
    # session native even if the profile now carries a provider. Session-less
    # callers (the default) fall back to config.provider (today's behavior).
    if session_provider_id is _UNSET_PROVIDER:
        provider_id = config.get("provider")
    else:
        provider_id = session_provider_id

    # Permission mode: profile value or bypassPermissions
    permission_mode = config.get("permission_mode") or "bypassPermissions"

    kwargs: Dict[str, Any] = {
        "model": model,
        "permission_mode": permission_mode,
        "env": _resolve_env(config, model, provider_id),
        "tools": [],  # Generation never uses tools.
        "include_partial_messages": False,
    }

    if max_turns is not None:
        kwargs["max_turns"] = max_turns

    # `_resolve_setting_sources` always returns a non-empty list.
    kwargs["setting_sources"] = _resolve_setting_sources(config)

    # Thinking / effort / max_thinking_tokens — spread into options.
    kwargs.update(_resolve_thinking_and_effort(config))

    # System prompt — only forward when the user actually set one. An empty
    # string or missing field means "use the default prompt" so we don't accidentally
    # pass a blank custom prompt that overrides Claude's built-in behavior.
    system_prompt = config.get("system_prompt")
    if isinstance(system_prompt, dict):
        content = system_prompt.get("content")
        if content:
            kwargs["system_prompt"] = system_prompt
    elif isinstance(system_prompt, str) and system_prompt.strip():
        kwargs["system_prompt"] = system_prompt

    # Extra args pass-through for future CLI flags the user may configure.
    extra_args = dict(config.get("extra_args") or {})
    thinking_display = _resolve_thinking_display(config)
    if thinking_display is not None:
        extra_args["thinking-display"] = thinking_display
    if extra_args:
        kwargs["extra_args"] = extra_args

    # Use the configured workspace dir as cwd so the subprocess spawns in a
    # stable, user-expected location.
    cwd = get_generation_cwd()
    if cwd:
        kwargs["cwd"] = cwd

    # Suppress user-global plugins (caveman, skill packs, etc.) whose hooks
    # run per-query and dominate the latency floor for short generation calls.
    # This merges with `setting_sources=["local"]` — the local settings file
    # still loads if present, but `enabledPlugins` is forcibly emptied.
    kwargs["settings"] = _GEN_SETTINGS_OVERRIDE

    # session_store is not set: generation sessions are one-shot and never
    # persisted.
    options = AgentOptions(**kwargs)

    # ── Provider binding stamp ───────────────────────────────────────────────
    # Mirrors `app.core.query_options` (the chat path): the HarnessAdapter reads
    # these four attributes in `connect_with_sdk_options` and projects them onto
    # SessionOptions.extra, which is how `_decide_provider_kind` picks the leg
    # from the bound provider ROW instead of sniffing the model string. Without
    # them a generation call on a non-Claude profile model would fall through to
    # the delegated CLI leg and hang.
    #
    # The env overlay above (`_resolve_env` → `apply_provider_env`) has already
    # validated the row for a provider-bound call, so this lookup cannot see a
    # missing/disabled provider that the overlay let through. Always stamped
    # (None everywhere for a native Anthropic call) so the attribute's presence
    # is never itself a signal.
    main_provider_row = None
    if provider_id:
        from app.db import database

        main_provider_row = database.get_model_provider(
            provider_id, include_token=False
        )

    options.aih_provider_id = provider_id
    options.aih_provider_type = (main_provider_row or {}).get("provider_type")
    options.aih_provider_auth_mode = (main_provider_row or {}).get("auth_mode")
    # A custom row's wire format ('anthropic' default | 'openai') — decides
    # which native client serves it. None everywhere else.
    options.aih_provider_dialect = (main_provider_row or {}).get("api_dialect")

    return options
