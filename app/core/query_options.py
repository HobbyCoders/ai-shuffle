"""
SDK option building for Claude queries.

Contains functions that construct ClaudeAgentOptions, assemble system prompts,
configure MCP servers, build tool/permission configurations, and manage
subagent definitions.

Core types, thinking config, and the main build_options_from_profile function
live here. MCP config, agent config, and environment setup are split into
sibling modules and re-exported for backward compatibility.
"""

import logging
from pathlib import Path
from typing import Optional, Dict, Any

from claude_agent_sdk import ClaudeAgentOptions, AgentDefinition

from app.db import database
from app.core.config import settings
from app.core.platform import detect_deployment_mode, DeploymentMode

from app.core.project_utils import get_project_working_dir
from app.core.query_streaming import DEFAULT_SDK_BUFFER_SIZE

# --- Re-exports for backward compatibility ---
# All symbols that were previously defined here are re-exported so that
# existing ``from app.core.query_options import X`` statements keep working.
from app.core.mcp_config import (  # noqa: F401
    _build_mcp_servers_dict,
    _build_mcp_servers_dict_with_ai_tools,
    _build_plugins_list,
)
from app.core.agent_config import (  # noqa: F401
    resolve_note_references,
    resolve_subagent_frontmatter_model,
)
from app.core.environment import (  # noqa: F401
    DEFAULT_SECURITY_INSTRUCTIONS,
    get_security_instructions,
    _is_git_repo,
    _get_os_version,
    _get_decrypted_api_key,
    _resolve_api_credential,
    _get_hook_tool_names,
    _build_extra_args,
    _build_env_with_ai_tools,
    apply_context_window_env,
    apply_provider_env,
    generate_environment_details,
)

logger = logging.getLogger(__name__)


# The CLI's interactive question tool, blocked for ProviderAgent worker
# sessions (ruling A3, 2026-07-25 — workers cannot ask user questions).
WORKER_DISALLOWED_TOOL = "AskUserQuestion"


# The CLI ships a bundled ``claude-api`` skill (Anthropic SDK reference docs).
# Invoking it injects ~595K chars / ~142K tokens of documentation in one shot,
# which is fine on a native Claude session but catastrophic on a gateway-routed
# NON-Claude session: its trigger text is broad ("the prompt names Claude in any
# form"), so a GPT-class model fires it constantly and blows the turn's context
# (see the ``CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE`` note in
# ``app/core/environment.py`` — the same injection is what forced that override
# to exist). The skill is also useless there: those sessions are not writing
# Anthropic SDK code, they are being SERVED by a translator gateway.
#
# Mechanism: a ``Skill(<name>)`` deny rule. Verified against the pinned
# claude-agent-sdk (0.2.126) and its bundled CLI:
#   - ``ClaudeAgentOptions.disallowed_tools`` → ``--disallowedTools`` (SDK
#     ``_internal/transport/subprocess_cli.py``);
#   - ``--disallowedTools`` entries are parsed as ``ToolName(ruleContent)`` and
#     land in the session's deny rules;
#   - the CLI's ``Skill`` tool ``checkPermissions`` walks the deny rules for tool
#     name ``Skill`` and matches each rule's content against the skill name,
#     returning ``{behavior: "deny", message: "Skill execution blocked by
#     permission rules"}`` on a hit. (The same specifier form the SDK itself
#     emits for its ``skills`` option, which maps each entry to
#     ``Skill(<name>)`` in ``allowedTools``.)
NON_CLAUDE_DISALLOWED_SKILL = "Skill(claude-api)"


# ---------------------------------------------------------------------------
# Provider-model composite codec (`prov:<providerId>:<modelId>`) — PA13
# ---------------------------------------------------------------------------
#
# The frontend counterpart lives in
# ``frontend/src/lib/constants/models.svelte.ts``
# (``encodeProviderModel`` / ``decodeProviderModel``). Both the profile editor's
# ModelPicker and the chat composer's model override emit this shape; only the
# composer's value reaches here (as ``overrides["model"]``), where the decoded
# provider id REBINDS the turn's provider.

PROVIDER_MODEL_PREFIX = "prov:"


def decode_provider_model(value: Any) -> Optional[tuple]:
    """Decode ``prov:<providerId>:<modelId>`` → ``(provider_id, model_id)``.

    Returns ``None`` when ``value`` is not a well-formed composite (a plain
    model alias, a custom id, a non-string, an empty half, …).

    Splits on the FIRST colon after the prefix only: provider model ids
    routinely contain ``/`` and ``:`` themselves (e.g. OpenRouter's
    ``meta-llama/llama-3.1-8b-instruct:free``), which a naive ``split(":")``
    would corrupt.
    """
    if not isinstance(value, str) or not value.startswith(PROVIDER_MODEL_PREFIX):
        return None
    rest = value[len(PROVIDER_MODEL_PREFIX):]
    sep = rest.find(":")
    if sep <= 0:
        return None
    provider_id, model_id = rest[:sep], rest[sep + 1:]
    if not provider_id or not model_id:
        return None
    return provider_id, model_id


def is_native_claude_model(value: Any) -> bool:
    """Whether ``value`` names a model the NATIVE/delegated Claude leg serves.

    True for the registry's short aliases (``sonnet``/``opus``/``fable``/
    ``haiku``, ``[1m]`` variants included) and for raw ``claude-*`` ids. Mirrors
    the same test ``harness_adapter._decide_provider_kind`` applies to the model
    string, so "the composer picked a Claude model" and "the leg decision routes
    it to Claude" can never disagree.
    """
    if not isinstance(value, str) or not value:
        return False
    from app.core.model_registry import get_model_family

    return get_model_family(value) is not None or value.lower().startswith("claude")


# Back-compat aliases for tool names that were renamed. Applied at config-read
# time only (never migrated in the DB) so saved profiles keep working. The
# builtin ``PushNotification`` leaf tool was renamed to ``NotifyDevices`` to
# resolve a collision with a bare stub of the same name (issue #610).
_LEGACY_TOOL_ALIASES = {
    "PushNotification": "mcp__builtins__NotifyDevices",
    "mcp__builtins__PushNotification": "mcp__builtins__NotifyDevices",
}


def _apply_legacy_tool_aliases(tool_names: list) -> list:
    """Translate legacy tool names to their current equivalents, in order,
    without introducing duplicates."""
    out: list = []
    for name in tool_names:
        mapped = _LEGACY_TOOL_ALIASES.get(name, name)
        if mapped not in out:
            out.append(mapped)
    return out


class WorktreeUnavailableError(Exception):
    """Raised when a worktree-flagged session's working directory no longer exists
    on disk and we refuse to silently fall back to the main checkout.

    Running a worktree session in the project's main directory is never correct —
    it lands the user's work on the wrong branch. A worktree session only needs
    its initial working directory to exist (exactly like a local session); we do
    NOT recreate anything on the resume path. When the directory is genuinely
    gone, callers (the query engines) convert this into a user-visible error
    event instead of executing in main.
    """

    def __init__(
        self,
        session_id: Optional[str],
        branch_name: Optional[str] = None,
        reason: str = "",
        path: Optional[str] = None,
    ):
        self.session_id = session_id
        self.branch_name = branch_name
        self.reason = reason
        self.path = path
        branch = f" (branch '{branch_name}')" if branch_name else ""
        where = f" at {path}" if path else ""
        detail = f" ({reason})" if reason else ""
        hint = (
            f" — you can ask the agent there to recreate the worktree from "
            f"branch '{branch_name}'" if branch_name else ""
        )
        super().__init__(
            f"The working directory for this session{branch} no longer exists"
            f"{where}{detail}. Start a new session{hint}. Refusing to run in the "
            f"main checkout."
        )


# Operating guidance appended to the system prompt when computer-use is active.
COMPUTER_USE_GUIDANCE = (
    "## Computer use\n"
    "You can control this **Windows** computer with the `mcp__computer-use__*` tools "
    "(screenshot, left_click, type, key, scroll, open_application, zoom, request_access, "
    "computer_batch, …) — the standard computer-use interface, with the usual conventions "
    "(coordinates are [x, y] pixels in the most recent screenshot).\n"
    "- Call `request_access` FIRST with the apps you'll need (Start-menu names, e.g. "
    "'Notepad', 'File Explorer') and a one-line reason; wait for approval, then take a "
    "`screenshot` to begin.\n"
    "- To launch or focus an app, use `open_application` (e.g. open_application app='Notepad') "
    "rather than hunting for its window — this app (AI Shuffle) runs in a browser that is "
    "usually in front and won't move itself.\n"
    "- Take a fresh `screenshot` after actions; use `zoom` to read small text. Use "
    "`computer_batch` for predictable sequences (click → type → Return).\n"
    "- For multiple monitors, read the display note on screenshots and use `switch_display`.\n"
    "- You share one physical cursor with the user; work in short, verifiable steps. The "
    "user can engage an emergency Stop at any time, after which actions refuse until "
    "resumed.\n"
    "- Synthetic input cannot operate the Windows UAC / secure desktop or the lock screen. "
    "If you hit an elevation prompt or a black screenshot, stop and ask the user to handle it."
)

# ─── Session store singleton (Phase 1.5) ────────────────────────────────────
# Constructed once per process. Wrapped in try/except so Postgres
# unavailability is non-fatal — callers get None and degrade gracefully.
_session_store_singleton = None
_session_store_init_attempted = False


def _get_session_store():
    """Return the module-level PgSessionStore singleton, or None if unavailable."""
    global _session_store_singleton, _session_store_init_attempted
    if _session_store_init_attempted:
        return _session_store_singleton
    _session_store_init_attempted = True
    try:
        from app.core.pg_session_store import PgSessionStore
        _session_store_singleton = PgSessionStore()
        logger.info("PgSessionStore singleton created")
    except Exception:
        logger.warning(
            "PgSessionStore construction failed — proceeding without session_store. "
            "Check the POSTGRES_* settings and Postgres connectivity.",
            exc_info=True,
        )
        _session_store_singleton = None
    return _session_store_singleton


# Token budgets for thinking modes (token-based thinking)
THINKING_TOKEN_BUDGETS = {
    'think': 8000,
    'think_harder': 24000,
    'ultra_think': 64000
}


def _build_thinking_config(config: Dict[str, Any], model: Optional[str], overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Build thinking/effort configuration for ClaudeAgentOptions.

    SDK 0.1.36+ decouples thinking and effort into independent fields:
    - thinking: ThinkingConfigAdaptive|ThinkingConfigEnabled|ThinkingConfigDisabled
      - 'adaptive': model decides when/how much to think
      - 'enabled' + budget_tokens: token-budget based thinking
      - 'disabled': suppress extended thinking
    - effort: 'low'|'medium'|'high'|'max' - controls reasoning depth
      Works independently of (and alongside) thinking config.

    Chat-level overrides (thinking_mode, effort) take priority over profile config.

    Args:
        config: Profile configuration dict
        model: Model name (e.g., 'opus', 'sonnet', 'haiku')
        overrides: Optional query-level overrides dict (may contain 'thinking_mode' and/or 'effort')

    Returns:
        Dict with thinking/effort configuration to spread into ClaudeAgentOptions
    """
    result: Dict[str, Any] = {}
    overrides = overrides or {}

    # Resolve thinking config: override takes priority over profile config
    thinking_mode = overrides.get('thinking_mode') or config.get('thinking_mode', 'default')

    if thinking_mode == 'disabled':
        result['thinking'] = {'type': 'disabled'}
        logger.debug("Thinking disabled by config")
    elif thinking_mode == 'adaptive':
        result['thinking'] = {'type': 'adaptive'}
        logger.debug("Using adaptive thinking")
    elif thinking_mode in THINKING_TOKEN_BUDGETS:
        budget_tokens = THINKING_TOKEN_BUDGETS[thinking_mode]
        result['thinking'] = {'type': 'enabled', 'budget_tokens': budget_tokens}
        logger.debug(f"Using token-based thinking with budget={budget_tokens}")
    # 'default' or unrecognized: omit thinking field, let SDK use its default

    # Resolve effort: override takes priority over profile config (independent of thinking)
    # 'xhigh' is only valid for Opus 4.7+ models
    effort = overrides.get('effort') or config.get('effort')
    if effort and effort in ('low', 'medium', 'high', 'max', 'xhigh'):
        result['effort'] = effort
        logger.debug(f"Using effort={effort}")

    return result


def _resolve_thinking_display(
    config: Dict[str, Any],
    overrides: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Resolve the thinking-display CLI flag value ("summarized" | "omitted").

    SDK 0.1.65 / CLI 2.1.111+: Opus 4.7 silently defaults ``display`` to
    ``"omitted"`` (signature-only, no thinking text). Callers that want
    summarized thinking text must pass ``summarized``.

    Forwarded via ``extra_args={"thinking-display": ...}`` rather than the
    typed ``ThinkingConfig.display`` field because the 0.1.65 wheel on PyPI
    does not yet contain PR #830 — the field is silently dropped by the
    SubprocessCLITransport there. The raw CLI flag is stable either way.
    """
    overrides = overrides or {}
    value = overrides.get('thinking_display') or config.get('thinking_display')
    if value in ('summarized', 'omitted'):
        return value
    return None


def build_options_from_profile(
    profile: Dict[str, Any],
    project: Optional[Dict[str, Any]] = None,
    overrides: Optional[Dict[str, Any]] = None,
    resume_session_id: Optional[str] = None,
    can_use_tool: Optional[callable] = None,
    hooks: Optional[Dict[str, list]] = None,
    api_user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> tuple[ClaudeAgentOptions, Optional[Dict[str, AgentDefinition]]]:
    """
    Convert a profile to ClaudeAgentOptions with all available options.

    Returns:
        Tuple of (ClaudeAgentOptions, agents_dict)
        - ClaudeAgentOptions: SDK options with agents included for Docker/Linux, excluded for Windows
        - agents_dict: Dict of agent_id -> AgentDefinition, or None if no agents

    Args:
        profile: Profile configuration dict
        project: Optional project configuration
        overrides: Optional override settings
        resume_session_id: Optional SDK session ID to resume (from Claude SDK)
        can_use_tool: Optional permission callback
        hooks: Optional hooks configuration
        api_user_id: Optional API user ID for user-specific credential resolution
        session_id: Optional our session ID (for worktree lookup)

    Note: Agents are always passed programmatically via options.agents for tool
    enforcement. On Windows local mode, callers also write agent files to
    .claude/agents/ for supplemental CLI discovery.
    """
    config = profile["config"]
    overrides = overrides or {}

    # Determine working directory and execution mode (needed for env details injection)
    # Priority order:
    # 1. Override cwd (explicit worktree path from agent engine)
    # 2. Session worktree (for resumed sessions)
    # 3. Project path
    # 4. Profile config cwd
    # 5. Default workspace
    execution_mode = "local"
    worktree_info = None
    working_dir_source = "workspace"

    # The session row, fetched ONCE below and reused by both the v2 resolver seam
    # and the fail-fast rebind guard (so neither re-fetches). None outside the
    # session_id branch (e.g. override / project-only spawns).
    session_row = None

    if overrides.get("cwd"):
        # Override takes highest priority - used by agent engine for worktrees
        working_dir = overrides.get("cwd")
        working_dir_source = "override"
        # Check if override includes worktree info
        if overrides.get("worktree_info"):
            execution_mode = "worktree"
            worktree_info = overrides.get("worktree_info")
    elif session_id:
        # V2 projects: the origin-keyed resolver is the SINGLE cwd authority for
        # every session (resolver-FIRST, no project_v2_id gate). This is the SPAWN
        # path, so resolve_cwd materializes the checkout (git worktree add) and
        # lazily self-heals project_v2_id + sessions.branch off the legacy
        # project_id / worktree_branch. Fetch the session row ONCE.
        try:
            session_row = database.get_session(session_id)
        except Exception:  # noqa: BLE001 — a lookup failure falls through to workspace
            session_row = None

        from app.core import project_resolver

        v2_cwd = project_resolver.resolve_cwd(session_row) if session_row else None
        if v2_cwd is not None:
            working_dir = v2_cwd
            working_dir_source = "project-v2"
            branch = session_row.get("branch") if session_row else None
            if branch:
                # A branch-bound v2 checkout is a worktree; record minimal info so
                # the environment-details prompt reflects the branch. base_branch
                # comes from the legacy worktree row when the session has one, else
                # the conventional default.
                execution_mode = "worktree"
                base_branch = "main"
                try:
                    wt_row = database.get_worktree_by_session(session_id)
                    if wt_row and wt_row.get("base_branch"):
                        base_branch = wt_row["base_branch"]
                except Exception:  # noqa: BLE001 — best-effort base_branch enrichment
                    pass
                worktree_info = {"branch": branch, "base_branch": base_branch}
        else:
            # resolve_cwd returned None. Two cases, distinguished by whether the
            # session resolves to a project_v2 at all:
            #  (i) it HAS a project_v2 but that project is not materialized on THIS
            #      runner (its checkout lives only on another install) → refuse
            #      rather than silently running in the wrong place. A worktree-
            #      flagged session raises WorktreeUnavailableError (carrying the
            #      branch); the query engines convert it to a user-visible event. A
            #      plain session raises an actionable "not checked out here" error.
            #  (ii) it has NO project_v2 and no live legacy project row (orphan /
            #      project-less session) → legacy parity: the profile-config cwd,
            #      else the default workspace.
            project_v2 = (
                project_resolver.resolve_project_v2(session_row)
                if session_row
                else None
            )
            if project_v2 is not None:
                row = session_row or {}
                branch = row.get("branch") or row.get("worktree_branch")
                if branch or row.get("worktree_id") or row.get("worktree_path"):
                    raise WorktreeUnavailableError(
                        session_id,
                        branch_name=branch,
                        reason=(
                            "its checkout is not materialized on this device "
                            "(or could not be prepared — see server logs)"
                        ),
                    )
                raise ValueError(
                    f"This session's project is not checked out on this device "
                    f"(project '{project_v2.get('name') or project_v2.get('origin')}'). "
                    f"Clone the repository here and open the project, then try again."
                )
            elif config.get("cwd"):
                working_dir = config.get("cwd")
                working_dir_source = "profile config"
            else:
                working_dir = str(settings.workspace_dir)
    elif project:
        working_dir = get_project_working_dir(project)
        working_dir_source = "project"
    elif config.get("cwd"):
        working_dir = config.get("cwd")
        working_dir_source = "profile config"
    else:
        working_dir = str(settings.workspace_dir)

    # Fail fast when the resolved cwd doesn't exist: an invalid cwd otherwise
    # surfaces as an opaque OS-level spawn failure (WinError 267 "directory name
    # is invalid") deep inside the SDK subprocess connect. Shared-DB installs can
    # carry project rows whose paths only exist on another machine — name the
    # path so the failure is actionable. The default workspace is ours to create.
    if not Path(working_dir).is_dir():
        if working_dir_source == "workspace":
            Path(working_dir).mkdir(parents=True, exist_ok=True)
        else:
            # No cross-install project rebind here anymore: the origin resolver
            # (resolve_cwd, above) owns cwd for every session and already
            # materializes / self-heals onto THIS runner's checkout, so a session
            # cwd never reaches this point stale. The remaining sources — a
            # project-only spawn, an override, or a profile-config cwd — name a
            # concrete path we do NOT own, so surface it honestly.
            detail = (
                f"project '{project['name']}'"
                if working_dir_source == "project" and project and project.get("name")
                else working_dir_source
            )
            raise ValueError(
                f"Working directory does not exist: {working_dir} (from {detail}). "
                f"The path may belong to a different install or have been moved — "
                f"update or remove the project/profile path, or pick another project."
            )

    # Get AI tools config (used for MCP tool registration + env vars, not system prompt)
    ai_tools_config = config.get("ai_tools")

    # Build system prompt
    system_prompt = config.get("system_prompt")
    override_append = overrides.get("system_prompt_append", "")

    # Get the active security instructions (custom or default)
    security_instructions = get_security_instructions()

    if system_prompt is None:
        # No preset - just security instructions as plain text
        final_system_prompt = security_instructions

        if override_append:
            final_system_prompt += "\n\n" + override_append
    elif isinstance(system_prompt, dict):
        prompt_type = system_prompt.get("type", "preset")

        if prompt_type == "custom":
            # Custom system prompt - use content directly with security instructions
            # Content can be empty/blank
            custom_content = system_prompt.get("content", "") or ""
            inject_env = system_prompt.get("inject_env_details", False)

            # Start building the prompt
            prompt_parts = [security_instructions]

            # Add environment details if enabled (includes AI tools based on ai_tools_config)
            if inject_env:
                prompt_parts.append(generate_environment_details(
                    working_dir,
                    execution_mode=execution_mode,
                    worktree_info=worktree_info
                ))

            # Add custom content if provided
            if custom_content:
                prompt_parts.append(custom_content)

            # Add override append if provided
            if override_append:
                prompt_parts.append(override_append)

            final_system_prompt = "\n\n".join(prompt_parts)
        else:
            # Preset system prompt - pass to SDK with append
            existing_append = system_prompt.get("append", "")
            full_append = security_instructions
            if existing_append:
                full_append += "\n\n" + existing_append
            if override_append:
                full_append += "\n\n" + override_append

            final_system_prompt = {
                "type": "preset",
                "preset": system_prompt.get("preset", "claude_code"),
                "append": full_append
            }
    else:
        # String system prompt - use it directly with security instructions
        final_system_prompt = security_instructions + "\n\n" + str(system_prompt)

        if override_append:
            final_system_prompt += "\n\n" + override_append

    # Build agents dict from profile's enabled_agents
    # P2c (rulings A6/A10): subagents no longer exist as SDK AgentDefinitions.
    # ``enabled_agents`` is now the OPTIONAL invoke allowlist for the unified
    # Task tool (empty/None = any agent may be invoked) — it is read by
    # ``app.core.task_dispatch`` at dispatch time, not projected onto the
    # engine. The CLI's native Task/Agent/Workflow tools are banned on the
    # delegated leg (harness_adapter passes --disallowed-tools), so nothing
    # consumes agent definitions any more.
    enabled_agent_ids = config.get("enabled_agents", [])
    agents_dict = None
    enabled_subagents: Dict[str, Any] = {}

    # Per-agent memory (ruling A11): the profile's durable memory text rides
    # into the prompt on every leg — as append text on the preset shape (so the
    # delegated CLI sees it) and as part of the string prompt otherwise (the
    # omni compiler folds it into the compiled output on native legs). The
    # Memory builtin tool is the write path.
    agent_memory = (config.get("memory") or "").strip()
    if agent_memory:
        memory_block = (
            "# Agent memory\n"
            "Yours, persistent across sessions. Update it with the Memory tool "
            "when you learn something durable about the user or your work.\n\n"
            + agent_memory
        )
        if isinstance(final_system_prompt, dict):
            current_append = final_system_prompt.get("append", "")
            final_system_prompt["append"] = current_append + "\n\n" + memory_block
        else:
            final_system_prompt += "\n\n" + memory_block

    # (P2c) The old built-in-subagent prompt instructions died with SDK
    # AgentDefinitions — the Task tool's own description tells the model how
    # to dispatch agents, and the agents themselves carry their prompts.

    # Inject app-config system prompt instructions when tools are enabled (admin only)
    if config.get("app_config_tools") and not api_user_id:
        from app.mcp.app_config_sdk import APP_CONFIG_SYSTEM_PROMPT
        if isinstance(final_system_prompt, dict):
            current_append = final_system_prompt.get("append", "")
            final_system_prompt["append"] = current_append + "\n\n" + APP_CONFIG_SYSTEM_PROMPT
        else:
            final_system_prompt += "\n\n" + APP_CONFIG_SYSTEM_PROMPT

    # Determine permission mode
    permission_mode = overrides.get("permission_mode") or config.get("permission_mode", "default")

    # Create stderr callback to log CLI output
    # Note: We only log stderr, don't forward to frontend. The SDK sends proper
    # typed messages for important events (compact_boundary, api errors via
    # synthetic AssistantMessage with isApiErrorMessage, etc.)
    # Use lazy %-formatting to avoid f-string overhead when debug is disabled
    def stderr_callback(line: str):
        logger.debug("[Claude CLI stderr] %s", line)

    # Process model selection.
    # The preset model values (haiku, sonnet, opus, fable) and their [1m] variants
    # are native Agent SDK / CLI shorthand aliases; custom model IDs pass through
    # as-is too.
    raw_model = overrides.get("model") or config.get("model")
    model_to_use = raw_model
    # Model provider (multi-provider phase-2 Chunk E, ruling G2-r). Resolution:
    # a query-level override wins; otherwise the SESSION's bound provider
    # (sessions.provider_id, stamped at creation from the profile) is
    # authoritative for a real session spawn — editing the profile later never
    # reroutes a live session, and a NULL binding stays native even if the
    # profile now carries a provider. Only session-LESS builds (e.g. tests /
    # project-only spawns) fall back to the profile's config.provider (which is
    # also what seeds the binding at creation). None = native Anthropic; when
    # set, the env is overlaid with the gateway vars (apply_provider_env).
    _binding_row = session_row
    if _binding_row is None and session_id:
        try:
            _binding_row = database.get_session(session_id)
        except Exception:  # noqa: BLE001 — a lookup failure falls back to config
            _binding_row = None
    if _binding_row is not None:
        provider_id = overrides.get("provider") or _binding_row.get("provider_id")
    else:
        provider_id = overrides.get("provider") or config.get("provider")

    # ── PA13: the composer's model override may REBIND the provider ─────────
    #
    # Until W-R4 the ``or``-chain above could only ever SET a provider, never
    # clear one, and ``model_to_use`` was taken verbatim — so the composer had to
    # be locked to the session's own provider (R7). Two override shapes now
    # re-resolve the binding for THIS turn (stickiness stays client-side, via the
    # per-session ``modelOverride`` the composer already persists — there is no
    # ``sessions.provider_id`` rebind):
    #
    #   1. a ``prov:<pid>:<mid>`` composite → provider_id = pid (beats the
    #      session binding), model = mid. Validated against the provider row so
    #      an unknown/disabled provider or an uncurated model fails LOUDLY here
    #      (ValueError → a user-visible error frame in both query engines)
    #      instead of shipping garbage upstream and 4xx-ing mid-turn.
    #   2. a plain native-Claude pick (registry alias or ``claude-*``) on a
    #      provider-bound session, where the bound provider does NOT curate that
    #      id → provider_id = None, i.e. back to the native/delegated Claude leg.
    #      The curated-id exemption keeps an Anthropic-API provider row that
    #      serves real ``claude-*`` ids on its own provider (case 3).
    #
    #   3. anything else (a bare model id belonging to the bound provider, a
    #      custom id, no override at all) keeps the pre-PA13 behavior verbatim.
    #
    # An explicit ``overrides["provider"]`` (internal callers: ProviderAgent
    # workers, generation) still pins the provider and is never cleared by a
    # model pick.
    _override_model = overrides.get("model")
    _decoded_model = decode_provider_model(_override_model)
    _bound_provider_row = None
    if _decoded_model is not None:
        _decoded_pid, _decoded_mid = _decoded_model
        from app.core.provider_agent import validate_provider_model

        _validation = validate_provider_model(
            _decoded_pid, _decoded_mid, require_servable=False
        )
        if not _validation.ok:
            raise ValueError(
                _validation.reason
                or f"Model '{_decoded_mid}' is not available on provider "
                   f"'{_decoded_pid}'."
            )
        provider_id = _decoded_pid
        model_to_use = _decoded_mid
        _bound_provider_row = _validation.provider
    elif _override_model and provider_id and not overrides.get("provider"):
        _bound_provider_row = database.get_model_provider(
            provider_id, include_token=False
        )
        if is_native_claude_model(_override_model):
            from app.core.provider_agent import curated_model_ids

            if _override_model not in curated_model_ids(_bound_provider_row):
                logger.info(
                    "[Provider] Model override %r clears the session's provider "
                    "binding %r for this turn (native Claude pick).",
                    _override_model,
                    provider_id,
                )
                provider_id = None
                _bound_provider_row = None

    # (P2c) The D4-era subagent frontmatter-model resolution died with SDK
    # AgentDefinitions — the Task tool resolves the target agent's own binding
    # at dispatch time instead.

    # True only for a provider-bound session whose MAIN model is NOT a Claude
    # model. PRESERVED ACROSS D4 (see PR #775): this used to read
    # ``SubagentLegPlan.main_serves_claude_ids``, but that field was only ever
    # the main provider row's ``provider_type == "anthropic"`` — the leg-map
    # assembly contributed nothing to it, so it survives the leg removal
    # verbatim off the session's bound provider row. Deliberately a provider-
    # SHAPE signal, not a model-id prefix sniff: a native session (no binding)
    # and an anthropic-type provider row both serve real ``claude-*`` ids and
    # stay False. Consumed by the ``claude-api`` skill exclusion below.
    is_non_claude_provider_session = False
    main_provider_row = None
    if provider_id:
        # Reuse the row the PA13 resolution above already fetched (validated
        # composite / binding-clear check) rather than hitting the DB twice.
        main_provider_row = _bound_provider_row
        if main_provider_row is None:
            main_provider_row = database.get_model_provider(
                provider_id, include_token=False
            )
        is_non_claude_provider_session = (
            (main_provider_row or {}).get("provider_type") != "anthropic"
        )

    betas_to_use = []

    # Build tool configuration (SDK 0.1.12+ supports new 'tools' parameter)
    # Priority: tools > tools_preset > allowed_tools (legacy)
    tools_config = config.get("tools")  # Explicit list of tools
    tools_preset = config.get("tools_preset")  # "claude_code" preset
    allowed_tools = _apply_legacy_tool_aliases(config.get("allowed_tools") or [])
    disallowed_tools = _apply_legacy_tool_aliases(config.get("disallowed_tools") or [])

    # Block the bundled ``claude-api`` skill on every non-Claude provider session
    # (see NON_CLAUDE_DISALLOWED_SKILL). This sits here — on the shared
    # ``disallowed_tools`` list built from the profile — rather than in the
    # ProviderAgent branch below, so it covers BOTH a non-Claude main session and
    # a non-Claude ProviderAgent worker: a worker is an ordinary session with its
    # own provider binding, so it flows through this same build. Claude sessions
    # (native, or an anthropic-type provider row) are untouched.
    if (
        is_non_claude_provider_session
        and NON_CLAUDE_DISALLOWED_SKILL not in disallowed_tools
    ):
        disallowed_tools = [*disallowed_tools, NON_CLAUDE_DISALLOWED_SKILL]

    # Determine tools parameter for SDK
    # New SDK way: use 'tools' param with list or preset dict
    # Legacy way: use 'allowed_tools' param
    sdk_tools = None
    if tools_config:
        # Explicit tool list provided
        sdk_tools = tools_config
    elif tools_preset == "claude_code":
        # Use preset (TypedDict format for SDK)
        sdk_tools = {"type": "preset", "preset": "claude_code"}

    # Collect MCP tool names that always need to be in allowed_tools
    # (MCP tools must be explicitly allowed regardless of the tools preset)
    mcp_allowed_tools: list = []
    if ai_tools_config and any(ai_tools_config.values()):
        from app.mcp.ai_tools_sdk import get_enabled_tool_names
        mcp_allowed_tools = get_enabled_tool_names(ai_tools_config)
        if mcp_allowed_tools:
            logger.debug(f"AI MCP tools to allow: {mcp_allowed_tools}")

    # Add app-config tool names when enabled (admin sessions only)
    if config.get("app_config_tools") and not api_user_id:
        from app.mcp.app_config_sdk import get_app_config_tool_names
        mcp_allowed_tools.extend(get_app_config_tool_names())
        logger.debug("App-config MCP tools added to allowed_tools")

    # Always expose Claude Code builtin bridge tools (ScheduleWakeup, Monitor,
    # EnterWorktree, CronCreate, NotifyDevices, ToolSearch, etc). These are
    # harness-level tools that the CLI ships natively; under the SDK we provide
    # our own implementations bridged to AI Shuffle infrastructure.
    from app.mcp.builtin_tools import get_builtin_tool_names
    mcp_allowed_tools.extend(get_builtin_tool_names())

    # P2c: the standalone ProviderAgent tool is superseded by the unified Task
    # tool (mcp__builtins__Task / omni's bare Task builtin), which is depth-
    # gated in app.core.task_dispatch rather than denied to workers outright —
    # workers may dispatch sub-workers up to settings.task_tool_max_depth (A9).
    if settings.provider_agent_enabled and session_id:
        try:
            from app.core.provider_agent import is_provider_agent_worker
            if is_provider_agent_worker(session_id):
                # Ruling A3 (2026-07-25): a worker must not be able to ask the
                # human anything. Worker tool APPROVALS are redirected to the
                # parent, but ``AskUserQuestion`` is a different interaction
                # kind with its own handler/TTL and is NOT redirected — a
                # worker that asked would simply stall until
                # ``user_question_timeout_seconds`` expired. The tool stays
                # removed for workers at every nesting depth.
                if WORKER_DISALLOWED_TOOL not in disallowed_tools:
                    disallowed_tools = [*disallowed_tools, WORKER_DISALLOWED_TOOL]
        except Exception:
            logger.exception("Failed to apply worker tool restrictions")

    # Built-in/managed MCP servers (e.g. the headless browser server) are
    # passed via mcp_servers (loaded through McpService inside
    # _build_mcp_servers_dict). Their tool names must be explicitly allowed —
    # like other MCP tools — or they would be blocked when a tools preset is
    # active (final_allowed_tools = mcp_allowed_tools in that case). We add a
    # server-level wildcard (mcp__<server>) so every tool from each enabled
    # built-in server is permitted, and skip disabled ones so the
    # disabledMcpServers toggle is honoured here too.
    try:
        from app.core.builtin_mcp_servers import get_builtin_servers
        from app.core.mcp_service import get_mcp_service
        # get_disabled_servers() reads the GLOBAL disable list (settings.json ->
        # disabledMcpServers), which is independent of project_dir, so the
        # unscoped service instance is equivalent here. Built-in servers are
        # toggled globally (the project-level toggle routes to this same path).
        _disabled = get_mcp_service().get_disabled_servers()
        for _srv in get_builtin_servers():
            if _srv.name not in _disabled:
                mcp_allowed_tools.append(f"mcp__{_srv.name}")
    except Exception:
        logger.exception("Failed to add built-in MCP server tools to allowed_tools")

    # Computer-use (host desktop control). Only allow-list its tools when the
    # profile explicitly enables it AND the host can actually run it (native /
    # non-Docker with a usable display). Off by default; tools never reach the
    # model otherwise.
    computer_use_cfg = config.get("computer_use") or {}
    computer_use_enabled = bool(computer_use_cfg.get("enabled"))
    if computer_use_enabled:
        try:
            from app.mcp.computer_use.scaling import computer_use_available
            _cu_ok, _cu_reason = computer_use_available()
            if _cu_ok:
                from app.mcp.computer_use_sdk import get_computer_use_tool_names
                mcp_allowed_tools.extend(get_computer_use_tool_names())
            else:
                computer_use_enabled = False
                logger.info("computer-use requested but unavailable (%s)", _cu_reason)
        except Exception:
            computer_use_enabled = False
            logger.exception("Failed to evaluate computer-use availability")

    # Build final allowed_tools list:
    # - When sdk_tools is set (preset), built-in tools are handled by the preset.
    #   Only MCP tool names need to go in allowed_tools.
    # - When sdk_tools is not set (legacy mode), pass all configured tools + MCP names.
    if sdk_tools:
        final_allowed_tools = mcp_allowed_tools
    else:
        final_allowed_tools = list(allowed_tools) + mcp_allowed_tools

    # Build AI tool environment (API keys + provider settings) ONCE, then pass to both:
    # 1. MCP server creation (so Node.js tool subprocesses get the keys)
    # 2. ClaudeAgentOptions env (so the Claude CLI subprocess also gets them)
    full_env = _build_env_with_ai_tools(
        config.get("env"), ai_tools_config, api_user_id, provider_id
    )

    if not provider_id:
        # Force the standard 200k context window unless the profile opted into 1M
        # via the model's `[1m]` suffix (natively-1M models like `fable` otherwise
        # default to 1M in the CLI). See apply_context_window_env. The `[1m]`/1M
        # logic is Claude-specific, so it is skipped for provider sessions —
        # apply_provider_env owns the window vars there.
        apply_context_window_env(full_env, model_to_use)
    else:
        # Overlay the model-provider env (base URL, bearer token, mitigation
        # flags, small-fast model, optional window/output caps). Raises
        # ProviderConfigError (a ValueError) for a missing/disabled provider or an
        # undecryptable token — the query engines convert it into a user-visible
        # error rather than silently running native Anthropic.
        apply_provider_env(
            full_env,
            provider_id,
            model_to_use,
            session_id=session_id,
        )

    ai_tool_env = full_env if full_env else None

    # Build MCP servers dict (pre-compute so we can add app-config server)
    mcp_servers_dict = _build_mcp_servers_dict_with_ai_tools(
        project_dir=Path(working_dir) if working_dir else None,
        ai_tools_config=ai_tools_config,
        tool_env=ai_tool_env,
    ) or {}

    # Add app-config MCP server for admin sessions with the toggle enabled
    if config.get("app_config_tools") and not api_user_id:
        try:
            from app.mcp.app_config_sdk import create_app_config_server
            app_config_srv = create_app_config_server(
                is_admin=True,
                project_id=project.get("id") if project else None,
                project_dir=working_dir,
            )
            if app_config_srv:
                mcp_servers_dict["app-config"] = app_config_srv
                logger.debug("App-config MCP server registered for admin session")
        except Exception:
            logger.exception("Failed to create app-config MCP server")

    # Register Claude Code builtin bridge tools for every session.
    # Session context (session_id, profile_id, project_id) is baked in so
    # ScheduleWakeup can re-fire prompts back at this session. The effective
    # model is baked in too, so a scheduled wake replays THIS turn's model
    # instead of firing on the profile default (which re-primes the prompt
    # cache on the wrong model).
    try:
        from app.mcp.builtin_tools import create_builtin_tools_server
        builtins_srv = create_builtin_tools_server(
            session_id=session_id,
            profile_id=profile.get("id"),
            project_id=project.get("id") if project else None,
            is_admin=not api_user_id,
            model=model_to_use,
        )
        if builtins_srv:
            mcp_servers_dict["builtins"] = builtins_srv
            logger.debug("Builtin bridge MCP server registered")
    except Exception:
        logger.exception("Failed to create builtin bridge MCP server")

    # (P2c) The standalone ProviderAgent MCP server is retired: the unified
    # Task tool rides the builtins server above (mcp__builtins__Task) and, on
    # native legs, omni's bare-named Task builtin. Workers get it too —
    # nesting is depth-gated in app.core.task_dispatch (max_depth=5, A9)
    # instead of denied at registration (the old P14 one-level rule).

    # Register the computer-use server (host desktop control) when enabled and
    # the host supports it. computer_use_enabled was already validated against
    # computer_use_available() above, so this just builds the server.
    if computer_use_enabled:
        try:
            from app.mcp.computer_use_sdk import create_computer_use_server
            cu_srv = create_computer_use_server(
                enabled=True,
                target_display=computer_use_cfg.get("target_display"),
                virtual_size=(
                    int(computer_use_cfg.get("virtual_width", 1280)),
                    int(computer_use_cfg.get("virtual_height", 800)),
                ),
                permission_mode=permission_mode,
                session_id=session_id,
                profile_id=profile.get("id"),
            )
            if cu_srv:
                mcp_servers_dict["computer-use"] = cu_srv
                logger.info("computer-use MCP server registered for session %s", session_id)
            else:
                computer_use_enabled = False
        except Exception:
            computer_use_enabled = False
            logger.exception("Failed to create computer-use MCP server")

    # When computer-use is active, append operating guidance to the system prompt
    # so the model uses the tools correctly (coordinate space, screenshot-first,
    # shared cursor, UAC limits, emergency stop). Handle both string and preset
    # (dict with "append") prompt shapes.
    if computer_use_enabled and "computer-use" in mcp_servers_dict:
        if isinstance(final_system_prompt, dict):
            _existing = final_system_prompt.get("append", "")
            final_system_prompt["append"] = (
                f"{_existing}\n\n{COMPUTER_USE_GUIDANCE}" if _existing else COMPUTER_USE_GUIDANCE
            )
        else:
            _base = str(final_system_prompt) if final_system_prompt else ""
            final_system_prompt = f"{_base}\n\n{COMPUTER_USE_GUIDANCE}" if _base else COMPUTER_USE_GUIDANCE

    # Build options with all ClaudeAgentOptions fields
    # Note: We don't set cli_path - let the SDK use its bundled CLI or find system CLI automatically
    # The SDK handles finding Claude properly on all platforms
    options = ClaudeAgentOptions(
        # Core settings
        model=model_to_use,
        permission_mode=permission_mode,
        max_turns=overrides.get("max_turns") or config.get("max_turns"),

        # Tool configuration (SDK 0.1.12+)
        # 'tools' is the new way - explicit list or preset
        # 'allowed_tools' is legacy - used if tools not set, but MCP tools always go here
        # 'disallowed_tools' always applies as a blocklist
        tools=sdk_tools,
        allowed_tools=final_allowed_tools,
        disallowed_tools=disallowed_tools,

        # System prompt
        system_prompt=final_system_prompt,

        # Streaming behavior - enable partial messages for real-time streaming
        include_partial_messages=config.get("include_partial_messages", True),

        # Session behavior
        continue_conversation=config.get("continue_conversation", False),
        fork_session=config.get("fork_session", False),

        # Settings loading
        setting_sources=config.get("setting_sources"),

        # MCP servers - pass all registered MCP servers directly to the SDK
        # This ensures tools from MCP servers are available regardless of
        # whether the CLI discovers them from .mcp.json files
        # Note: ai_tool_env is passed so Node.js tool subprocesses get API keys
        mcp_servers=mcp_servers_dict,

        # Environment and arguments - inject AI tool credentials if enabled
        # Pass api_user_id for policy-based credential resolution
        env=full_env,
        extra_args=_build_extra_args(config.get("extra_args") or {}, hooks, detect_deployment_mode()),

        # Buffer settings - Default to 50MB to handle large file reads (images, PDFs)
        # The SDK's default is 1MB which causes "JSON message exceeded maximum buffer size" errors
        # when reading images or PDFs (their base64 content easily exceeds 1MB)
        max_buffer_size=config.get("max_buffer_size") or DEFAULT_SDK_BUFFER_SIZE,

        # User identification
        user=config.get("user"),

        # Token budget management (SDK 0.1.51+)
        **({"task_budget": config["task_budget"]} if config.get("task_budget") else {}),

        # Subagents - always pass programmatically so AgentDefinition.tools
        # whitelist is enforced by the SDK.  Filesystem .claude/agents/ files
        # are still written on Windows for prompt/description discovery, but
        # only the programmatic path restricts tool access at runtime.
        agents=agents_dict,

        # Permission callback - set if callback provided (handles AskUserQuestion in all modes)
        can_use_tool=can_use_tool if can_use_tool else None,

        # Hooks - for interactive tool handling like AskUserQuestion
        hooks=hooks,

        # Stderr callback for debugging
        stderr=stderr_callback,

        # Plugins - load from enabled_plugins in profile config
        # Each plugin path is resolved relative to workspace_dir if relative
        # Pass setting_sources to avoid duplicating plugins that Claude Code auto-loads
        plugins=_build_plugins_list(
            config.get("enabled_plugins"),
            config.get("setting_sources")
        ),

        # Beta features - e.g., 1M context window
        betas=betas_to_use,

        # File checkpointing for rewind (SDK 0.1.15+)
        enable_file_checkpointing=config.get("enable_file_checkpointing", False),

        # Extended thinking configuration (SDK 0.1.36+)
        # For Opus 4.6+: use effort-based adaptive thinking
        # For Sonnet/Haiku: use token budget-based thinking
        **_build_thinking_config(config, model_to_use, overrides),
    )

    deployment_mode = detect_deployment_mode()
    logger.debug(f"Built options with model={model_to_use}, betas={betas_to_use}, permission_mode={permission_mode}, can_use_tool={options.can_use_tool is not None}, hooks={hooks is not None}, deployment_mode={deployment_mode}")
    logger.debug(f"Agents passed via SDK: {list(agents_dict.keys()) if agents_dict else 'None'}")
    if deployment_mode == DeploymentMode.LOCAL:
        logger.debug("Windows local mode: agents also written to filesystem for discovery")

    # Apply working directory (already computed above for env details)
    options.cwd = working_dir

    # Forward thinking-display as a raw CLI flag (see _resolve_thinking_display).
    thinking_display = _resolve_thinking_display(config, overrides)
    if thinking_display is not None:
        if options.extra_args is None:
            options.extra_args = {}
        options.extra_args["thinking-display"] = thinking_display

    # Enable any CLI-flag built-in integrations (generic mechanism: a built-in
    # whose McpServerConfig sets ``cli_flag`` is turned on by adding ``--<flag>``
    # to the CLI rather than being spawned as a server). No built-in currently
    # uses this path — the browser built-in is now a spawned STDIO server (added
    # via mcp_servers with a ``mcp__browser`` wildcard above), so this loop
    # no-ops and never sets ``--chrome``. Kept as generic infra for future flags.
    try:
        from app.core.builtin_mcp_servers import get_builtin_servers
        from app.core.mcp_service import get_mcp_service
        _disabled = get_mcp_service().get_disabled_servers()
        for _srv in get_builtin_servers():
            _flag = getattr(_srv, "cli_flag", None)
            if _flag and _srv.name not in _disabled:
                if options.extra_args is None:
                    options.extra_args = {}
                options.extra_args[_flag] = None
    except Exception:
        logger.exception("Failed to apply CLI-flag built-in MCP servers")

    # Additional directories.
    # Notes live in the data dir (e.g. /data/notes in Docker), which is OUTSIDE
    # every possible working directory (workspace / project / worktree all live
    # under the workspace root). The SDK's file tools can only touch the cwd plus
    # add_dirs, so an @-referenced note path like /data/notes/foo.md would be
    # unreachable. Always register the notes dir so referenced notes (and their
    # attachments, stored beneath it) resolve from any session.
    merged_add_dirs: list = list(config.get("add_dirs") or [])
    try:
        from app.core.note_files import get_notes_dir
        notes_dir = str(get_notes_dir())
        if notes_dir not in (str(d) for d in merged_add_dirs):
            merged_add_dirs.append(notes_dir)
    except Exception:
        logger.exception("Failed to register notes dir as an accessible directory")
    if merged_add_dirs:
        options.add_dirs = merged_add_dirs

    # Resume existing session
    if resume_session_id:
        options.resume = resume_session_id

    # Hook event forwarding (SDK 0.1.81+) — always enable so the frontend
    # receives HookEventMessage payloads without requiring profile changes.
    options.include_hook_events = True

    # Strict MCP config validation (SDK 0.1.81+) — off by default; a future
    # profile field (Slice F) will allow per-profile override.
    options.strict_mcp_config = bool(getattr(config, "strict_mcp_config", False) or config.get("strict_mcp_config", False))

    # Session store wiring (Phase 1.5) — attach PgSessionStore when enabled.
    _store = _get_session_store()
    if _store is not None:
        options.session_store = _store
        options.session_store_flush = "eager"
        # The SDK forbids combining a session_store with file checkpointing
        # ("checkpoints are local-disk only and would diverge from the mirrored
        # transcript") and raises at connect time, which fails the whole query.
        # When the store is attached it is the source of truth, so suppress
        # checkpointing rather than let the connection fail.
        if getattr(options, "enable_file_checkpointing", False):
            logger.warning(
                "Disabling enable_file_checkpointing because a session_store is "
                "attached — the SDK rejects combining the two, and the "
                "mirrored transcript supersedes local-disk checkpoints."
            )
            options.enable_file_checkpointing = False

    # Carry the resolved model-provider binding on the options object. The Agent
    # SDK ignores unknown attributes, so this is inert on the default (flag-off)
    # path; the omni HarnessAdapter reads it in connect_with_sdk_options and
    # projects it onto SessionOptions.extra["provider_id"], which is how a native
    # (non-delegated) omni leg gets the provider row's decrypted credentials
    # instead of failing with a missing-credential 401. Always stamped (None for
    # a native Anthropic session) so the attribute's presence is not a signal.
    options.aih_provider_id = provider_id

    # The bound row's SHAPE, carried alongside the id so the omni
    # HarnessAdapter can decide the leg from the provider row instead of
    # sniffing the model string (PA13 / W-R4): a cross-provider pick like
    # "claude-3.5-sonnet on an OpenRouter row" reads as delegated-Claude to the
    # sniff, which is the wrong leg entirely. Both are None for a native
    # session; the adapter falls back to a lookup (then the sniff) when a
    # provider id arrives without them.
    options.aih_provider_type = (main_provider_row or {}).get("provider_type")
    options.aih_provider_auth_mode = (main_provider_row or {}).get("auth_mode")

    # Same trick for the app's own session id. The omni HarnessAdapter projects
    # it onto SessionOptions.extra["app_session_id"], which is how a native leg
    # knows which `sessions` row to parent a Task worker onto — the omni engine
    # only ever knows its own `ses_...` handle, and a brand-new session has no
    # omni id at connect time at all.
    options.aih_session_id = session_id

    return options, agents_dict
