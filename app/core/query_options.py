"""
SDK option building for Claude queries.

Contains functions that construct ClaudeAgentOptions, assemble system prompts,
configure MCP servers, build tool/permission configurations, and manage
subagent definitions.

Core types, thinking config, and the main build_options_from_profile function
live here. MCP config, agent config, and environment setup are split into
sibling modules and re-exported for backward compatibility.
"""

import json
import logging
import tempfile
import threading
from pathlib import Path
from typing import Optional, Dict, Any

from claude_agent_sdk import ClaudeAgentOptions, AgentDefinition

from app.db import database
from app.core.config import settings
from app.core.model_registry import is_fast_mode_eligible
from app.core.platform import detect_deployment_mode, DeploymentMode

from app.core.project_utils import get_project_working_dir, get_worktree_working_dir
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
    generate_environment_details,
)

logger = logging.getLogger(__name__)

# Path to Claude's user settings file
_CLAUDE_SETTINGS_FILE = Path.home() / ".claude" / "settings.json"
# Lock to prevent concurrent writes to settings.json
_settings_file_lock = threading.Lock()
# Directory for per-session fast mode settings files (avoids race conditions)
_FAST_MODE_SETTINGS_DIR = Path(tempfile.gettempdir()) / "claude-fast-mode"


def _create_fast_mode_settings_file(session_id: str) -> str:
    """Create a per-session settings file with fastMode enabled.

    Returns the file path as a string for use with ``--settings``.

    Previous approach wrote fastMode to the shared ``~/.claude/settings.json``,
    but this caused a race condition: concurrent queries (background tasks,
    commit generation, etc.) without fast mode would overwrite ``fastMode``
    to ``false`` before the CLI subprocess could read the file.

    New approach: each fast-mode session gets its own temp settings file.
    This eliminates the race entirely — no shared mutable state.
    """
    _FAST_MODE_SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    settings_file = _FAST_MODE_SETTINGS_DIR / f"fast-{session_id}.json"
    settings_file.write_text(json.dumps({
        "fastMode": True,
        "penguinModeOrgEnabled": True,
    }), encoding="utf-8")
    logger.debug(f"Created per-session fast mode settings: {settings_file}")
    return str(settings_file)


def cleanup_fast_mode_settings(session_id: str) -> None:
    """Remove the per-session fast mode settings file after use."""
    settings_file = _FAST_MODE_SETTINGS_DIR / f"fast-{session_id}.json"
    try:
        settings_file.unlink(missing_ok=True)
    except Exception:
        pass  # Best-effort cleanup


def _set_fast_mode_in_user_settings(enabled: bool) -> None:
    """Write fastMode and penguinModeOrgEnabled to ~/.claude/settings.json.

    This is a backup mechanism alongside the per-session settings file.
    The Claude CLI reads fastMode from the user settings file (loaded via
    ``--setting-sources user``).

    penguinModeOrgEnabled tells the CLI that the org supports fast mode,
    bypassing the network-based org eligibility check that fails in
    SDK/Docker mode (no OAuth token available for the check).

    Thread-safe: acquires a lock before read-modify-write.
    """
    with _settings_file_lock:
        try:
            if _CLAUDE_SETTINGS_FILE.exists():
                data = json.loads(_CLAUDE_SETTINGS_FILE.read_text(encoding="utf-8"))
            else:
                data = {}

            current_fast = data.get("fastMode", False)
            current_penguin = data.get("penguinModeOrgEnabled", False)
            if current_fast == enabled and current_penguin == enabled:
                return  # No-op: already in desired state

            if enabled:
                data["fastMode"] = True
                data["penguinModeOrgEnabled"] = True
            else:
                data.pop("fastMode", None)
                data.pop("penguinModeOrgEnabled", None)

            _CLAUDE_SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            _CLAUDE_SETTINGS_FILE.write_text(
                json.dumps(data, indent=2), encoding="utf-8"
            )
            logger.debug(f"Set fastMode={enabled}, penguinModeOrgEnabled={enabled} in {_CLAUDE_SETTINGS_FILE}")
        except Exception:
            logger.exception("Failed to update fastMode in user settings")


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

    if overrides.get("cwd"):
        # Override takes highest priority - used by agent engine for worktrees
        working_dir = overrides.get("cwd")
        # Check if override includes worktree info
        if overrides.get("worktree_info"):
            execution_mode = "worktree"
            worktree_info = overrides.get("worktree_info")
    elif session_id:
        # Check if this session has an associated worktree
        worktree = database.get_worktree_by_session(session_id)
        if worktree and worktree.get("status") == "active":
            working_dir = get_worktree_working_dir(worktree)
            execution_mode = "worktree"
            worktree_info = {
                "branch": worktree.get("branch_name", "unknown"),
                "base_branch": worktree.get("base_branch", "main")
            }
            logger.debug(f"[Options] Session {session_id} using worktree cwd: {working_dir}")
        elif worktree:
            # Worktree exists but is not active — log for debugging
            logger.warning(
                f"[Options] Session {session_id} has worktree {worktree.get('id')} "
                f"but status is '{worktree.get('status')}', falling back to project cwd"
            )
            working_dir = get_project_working_dir(project) if project else str(settings.workspace_dir)
        elif project:
            working_dir = get_project_working_dir(project)
        else:
            working_dir = str(settings.workspace_dir)
    elif project:
        working_dir = get_project_working_dir(project)
    elif config.get("cwd"):
        working_dir = config.get("cwd")
    else:
        working_dir = str(settings.workspace_dir)

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
    # Profile stores a list of subagent IDs that reference global subagents in the database
    # The SDK expects AgentDefinition dataclass instances, not raw dicts
    # All enabled agents are always sent — our definitions supersede CLI built-ins
    enabled_agent_ids = config.get("enabled_agents", [])
    agents_dict = None
    logger.debug(f"Building agents from enabled_agent_ids: {enabled_agent_ids}")
    if enabled_agent_ids:
        agents_dict = {}
        for agent_id in enabled_agent_ids:
            subagent = database.get_subagent(agent_id)
            if subagent:
                agent_kwargs = {
                    "description": subagent.get("description", ""),
                    "prompt": subagent.get("prompt", ""),
                    "tools": subagent.get("tools"),
                    "model": subagent.get("model"),
                    "skills": subagent.get("skills"),
                    "memory": subagent.get("memory"),
                    "mcpServers": subagent.get("mcp_servers"),
                }
                # New AgentDefinition fields (SDK 0.1.51+)
                if subagent.get("disallowed_tools"):
                    agent_kwargs["disallowedTools"] = subagent["disallowed_tools"]
                if subagent.get("max_turns") is not None:
                    agent_kwargs["maxTurns"] = subagent["max_turns"]
                if subagent.get("initial_prompt"):
                    agent_kwargs["initialPrompt"] = subagent["initial_prompt"]
                agent_def = AgentDefinition(**agent_kwargs)
                agents_dict[agent_id] = agent_def
                logger.debug(f"Loaded subagent '{agent_id}': description='{agent_def.description[:50]}...', tools={agent_def.tools}, model={agent_def.model}")
            else:
                logger.warning(f"Subagent not found in database: {agent_id}")
        if not agents_dict:
            agents_dict = None
        logger.debug(f"Final agents_dict keys: {list(agents_dict.keys()) if agents_dict else 'None'}")

    # Inject built-in agent instructions into system prompt (only for enabled built-in agents)
    if enabled_agent_ids:
        from app.core.builtin_subagents import get_system_prompt_instructions
        builtin_instructions = get_system_prompt_instructions(enabled_agent_ids)
        if builtin_instructions:
            if isinstance(final_system_prompt, dict):
                # Append to the append field
                current_append = final_system_prompt.get("append", "")
                final_system_prompt["append"] = current_append + builtin_instructions
            else:
                # Append to string system prompt
                final_system_prompt += builtin_instructions

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
    # All supported model IDs (claude-haiku-4-5, claude-sonnet-4-6, claude-opus-4-6,
    # claude-opus-4-7) and their [1m] variants are native CLI aliases — pass through as-is.
    raw_model = overrides.get("model") or config.get("model")
    model_to_use = raw_model
    betas_to_use = []

    # Fast mode: three-pronged approach to enable fastMode reliably.
    #
    # 1. Per-session settings FILE passed via --settings (file path, not inline JSON).
    #    This sets flagSettings.fastMode in the CLI, which is required for SDK mode.
    #
    # 2. Write fastMode + penguinModeOrgEnabled to ~/.claude/settings.json as backup.
    #    The CLI reads these from user settings loaded via --setting-sources user.
    #    penguinModeOrgEnabled bypasses the org eligibility check that otherwise
    #    disables fast mode when running with API keys (non-OAuth).
    #
    # 3. Set CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK=1 env var.
    #    The CLI checks this env var to skip the org-level fast mode eligibility
    #    check entirely. Without this, the CLI tries to verify org eligibility
    #    via network call, which fails in SDK/Docker mode and falls back to
    #    cached penguinModeOrgEnabled (default false) → fast mode disabled.
    #
    # Previous bug: the else branch wrote fastMode=false to the shared settings
    # file, causing a race condition — concurrent queries (background tasks,
    # commit generation) without fast mode would overwrite fastMode before the
    # fast-mode CLI subprocess could read it. Now each session gets its own
    # temp file, and we only write TRUE to the shared file (never false).
    fast_mode_requested = overrides.get("fast_mode") == "true"
    fast_mode_settings: Optional[str] = None
    # Fast-mode eligibility is a per-model capability declared in the
    # registry (app/core/model_registry.py) rather than inferred from the
    # model ID — a future non-Opus model could ship fast mode, and the old
    # substring check would have silently misfired.
    if fast_mode_requested and is_fast_mode_eligible(model_to_use):
        # Per-session temp file — no race with concurrent queries
        fast_mode_settings = _create_fast_mode_settings_file(session_id or "default")
        # Also write to shared settings as backup (no false-write race now)
        _set_fast_mode_in_user_settings(True)
        logger.debug(f"Fast mode enabled for model={model_to_use}, settings file={fast_mode_settings}")
        # Inject fast mode awareness into system prompt
        fast_mode_note = "\n\nFast mode is enabled. You are running with faster output using the same model. When asked about your speed or mode, you can confirm fast mode is active."
        if isinstance(final_system_prompt, dict):
            current_append = final_system_prompt.get("append", "")
            final_system_prompt["append"] = current_append + fast_mode_note
        else:
            final_system_prompt += fast_mode_note

    # Build tool configuration (SDK 0.1.12+ supports new 'tools' parameter)
    # Priority: tools > tools_preset > allowed_tools (legacy)
    tools_config = config.get("tools")  # Explicit list of tools
    tools_preset = config.get("tools_preset")  # "claude_code" preset
    allowed_tools = config.get("allowed_tools") or []
    disallowed_tools = config.get("disallowed_tools") or []

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
    # EnterWorktree, CronCreate, PushNotification, ToolSearch, etc). These are
    # harness-level tools that the CLI ships natively; under the SDK we provide
    # our own implementations bridged to AI Shuffle infrastructure.
    from app.mcp.builtin_tools import get_builtin_tool_names
    mcp_allowed_tools.extend(get_builtin_tool_names())

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
    full_env = _build_env_with_ai_tools(config.get("env"), ai_tools_config, api_user_id)

    # Inject fast mode org check bypass into CLI environment.
    # The CLI's org eligibility check fails in SDK/Docker mode because there's
    # no OAuth session to query. This env var tells the CLI to skip the check
    # and assume the org supports fast mode.
    if fast_mode_settings:
        if not full_env:
            full_env = {}
        full_env["CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK"] = "1"
        logger.debug("Injected CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK=1 for fast mode")

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
            )
            if app_config_srv:
                mcp_servers_dict["app-config"] = app_config_srv
                logger.debug("App-config MCP server registered for admin session")
        except Exception:
            logger.exception("Failed to create app-config MCP server")

    # Register Claude Code builtin bridge tools for every session.
    # Session context (session_id, profile_id, project_id) is baked in so
    # ScheduleWakeup can re-fire prompts back at this session.
    try:
        from app.mcp.builtin_tools import create_builtin_tools_server
        builtins_srv = create_builtin_tools_server(
            session_id=session_id,
            profile_id=profile.get("id"),
            project_id=project.get("id") if project else None,
            is_admin=not api_user_id,
        )
        if builtins_srv:
            mcp_servers_dict["builtins"] = builtins_srv
            logger.debug("Builtin bridge MCP server registered")
    except Exception:
        logger.exception("Failed to create builtin bridge MCP server")

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
        settings=fast_mode_settings,

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
    logger.debug(f"Built options with model={model_to_use}, betas={betas_to_use}, settings={fast_mode_settings}, permission_mode={permission_mode}, can_use_tool={options.can_use_tool is not None}, hooks={hooks is not None}, deployment_mode={deployment_mode}")
    logger.debug(f"Agents passed via SDK: {list(agents_dict.keys()) if agents_dict else 'None'}")
    if deployment_mode == DeploymentMode.LOCAL:
        logger.debug("Windows local mode: agents also written to filesystem for discovery")

    # Apply working directory (already computed above for env details)
    options.cwd = working_dir

    # Additional directories
    add_dirs = config.get("add_dirs")
    if add_dirs:
        options.add_dirs = add_dirs

    # Resume existing session
    if resume_session_id:
        options.resume = resume_session_id

    return options, agents_dict
