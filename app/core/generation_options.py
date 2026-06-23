"""
Generation call option builder.

Builds a `ClaudeAgentOptions` for one-shot AI generation calls (commit
messages, PR descriptions, branch names, profile / subagent generation,
etc.) from the Default AI profile.

All generation calls spawn a fresh `claude.exe` subprocess via the SDK's
top-level `query()` — no warm pool, no cross-call context bleed.

Profile honoring:
- Honored from the Default AI profile config: model, permission_mode,
  setting_sources, thinking_mode, effort, max_thinking_tokens, system_prompt,
  env, extra_args.
- Always force-disabled for one-shot generation: tools, agents, MCP servers,
  hooks, file checkpointing, partial messages, fork_session,
  continue_conversation. These would add cold-start overhead without benefit.

Workspace override:
- Every generation subprocess runs in the configured workspace directory
  (`settings.effective_workspace_dir`) so CLAUDE.md / .claude/ auto-discovery
  is predictable and matches the user's dev environment.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from claude_agent_sdk import ClaudeAgentOptions

from app.core.model_registry import DEFAULT_MODEL

logger = logging.getLogger(__name__)


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
# Profile -> ClaudeAgentOptions
# ---------------------------------------------------------------------------


def _resolve_setting_sources(
    profile_config: Optional[Dict[str, Any]],
) -> List[str]:
    """
    Resolve `setting_sources` for generation calls from the Default AI profile.

    Generation calls (commit messages, PR descriptions, branch names, etc.) are
    short one-shot prompts that have nothing to do with user-global Claude Code
    settings. We therefore default to blocking user + project settings and only
    opt-in to extra scopes when the user explicitly configures a non-empty list.

    Blocking matters because `~/.claude/settings.json` commonly carries
    `effortLevel: medium`. Haiku doesn't support effort, and when the CLI
    receives it the call takes a slow fallback path (12-17s for a 50-byte
    prompt vs 1-3s baseline). Chat sessions use their own options builder and
    are unaffected by this default.

    SDK quirk worked around here: `subprocess_cli.py:283` uses
    `if self._options.setting_sources:` to decide whether to emit
    `--setting-sources`. An empty list is falsy, so the flag is omitted and
    `claude.exe` falls back to loading ALL scopes. We therefore always return a
    non-empty list so the flag is emitted.

    Behavior:
    - Non-empty list in profile: pass through unchanged — user explicitly opted
      in to loading those scopes for generation tasks.
    - `None`, `[]`, missing key, or non-list value: return `["local"]`. Forces
      the flag to be emitted and scopes it to `.claude/settings.local.json` in
      the working directory (usually absent), cutting off the user-global
      leak path.
    """
    if profile_config:
        sources = profile_config.get("setting_sources")
        if isinstance(sources, list) and len(sources) > 0:
            return sources
    # All "fall back to default" cases — still return a non-empty list so the
    # SDK emits the flag. `local` is the safest sentinel since the file it
    # points at rarely exists and never carries `effortLevel`.
    return ["local"]


def _resolve_thinking_and_effort(
    profile_config: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Map profile's `thinking_mode` + `effort` to ClaudeAgentOptions kwargs.

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
    # "default" or unrecognized: omit, let SDK use its default.

    effort = profile_config.get("effort")
    if effort in ("low", "medium", "high", "max"):
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


def _resolve_env(profile_config: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """
    Merge profile's env with the fixed generation overrides.

    Always forces `CLAUDECODE=""` to prevent "nested session" detection when
    AI Shuffle is started from within a Claude Code session. Other env vars
    from the profile pass through.

    Also injects the Claude Code auth credential (`ANTHROPIC_API_KEY`) when a
    stored anthropic_api_key is set, mirroring the main chat subprocess. AI
    generation authenticates exactly like every other Claude Code session: key
    set → API-key auth wins; key absent → connected OAuth account is used.
    """
    env: Dict[str, str] = {}
    if profile_config:
        profile_env = profile_config.get("env")
        if isinstance(profile_env, dict):
            env.update({str(k): str(v) for k, v in profile_env.items()})
    env["CLAUDECODE"] = ""

    # Inject the stored anthropic_api_key as the Claude Code auth credential
    # (same precedence as chat sessions). Imported lazily to keep this module
    # free of the heavier environment wiring at import time.
    try:
        from app.core.environment import inject_claude_code_auth
        inject_claude_code_auth(env)
    except Exception as e:  # noqa: BLE001 - auth injection is best-effort
        logger.debug(f"Could not inject Claude Code auth for generation: {e}")
    return env


def build_generation_options(
    profile_config: Optional[Dict[str, Any]],
    model_override: Optional[str] = None,
    max_turns: Optional[int] = None,
) -> ClaudeAgentOptions:
    """
    Build a `ClaudeAgentOptions` for a one-shot generation call.

    Honored from the Default AI profile config:
    - `model` (unless `model_override` is supplied)
    - `permission_mode` (default `bypassPermissions`)
    - `setting_sources` (via `_resolve_setting_sources` workaround)
    - `thinking_mode` / `effort` / `max_thinking_tokens`
    - `system_prompt` (when present and non-empty)
    - `env` (merged with `CLAUDECODE=""`)
    - `extra_args`

    Always forced off for generation calls:
    - tools, agents, MCP servers, hooks, file checkpointing, partial
      messages, fork_session, continue_conversation.

    `max_turns` is passed through verbatim; callers typically set 1 for
    single-prompt generation.
    """
    config: Dict[str, Any] = profile_config or {}

    # Model: explicit override > profile > hard fallback
    model = model_override or config.get("model") or DEFAULT_MODEL

    # Permission mode: profile value or bypassPermissions
    permission_mode = config.get("permission_mode") or "bypassPermissions"

    kwargs: Dict[str, Any] = {
        "model": model,
        "permission_mode": permission_mode,
        "env": _resolve_env(config),
        "tools": [],  # Generation never uses tools.
        "include_partial_messages": False,
        "continue_conversation": False,
        "fork_session": False,
    }

    if max_turns is not None:
        kwargs["max_turns"] = max_turns

    # `_resolve_setting_sources` always returns a non-empty list so the SDK
    # reliably emits `--setting-sources` (workaround for falsy-check quirk).
    kwargs["setting_sources"] = _resolve_setting_sources(config)

    # Thinking / effort / max_thinking_tokens — spread into options.
    kwargs.update(_resolve_thinking_and_effort(config))

    # System prompt — only forward when the user actually set one. An empty
    # string or missing field means "use SDK default" so we don't accidentally
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

    # Hook event forwarding (SDK 0.1.81+) — enable so callers can observe
    # hook decisions even on one-shot generation paths.
    kwargs["include_hook_events"] = True

    # Strict MCP config (SDK 0.1.81+) — generation calls have no profile-level
    # toggle; default to False.  session_store is intentionally NOT set here:
    # generation sessions are one-shot fire-and-forget and must not be
    # persisted to the session store (would pollute the transcript history).
    kwargs["strict_mcp_config"] = False

    return ClaudeAgentOptions(**kwargs)
