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
from typing import Optional, Dict, Any, Tuple, cast

from claude_agent_sdk import ClaudeAgentOptions, AgentDefinition

from app.db import database
from app.core.config import settings
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


class WorktreeUnavailableError(Exception):
    """Raised when a worktree-flagged session cannot resolve to a valid, active
    worktree directory and we refuse to silently fall back to the main checkout.

    Running a worktree session in the project's main directory is never correct —
    it lands the user's work on the wrong branch. Callers (the query engines)
    convert this into a user-visible error event instead of executing in main.
    """

    def __init__(self, session_id: Optional[str], branch_name: Optional[str] = None, reason: str = ""):
        self.session_id = session_id
        self.branch_name = branch_name
        self.reason = reason
        branch = f" for branch '{branch_name}'" if branch_name else ""
        detail = f" ({reason})" if reason else ""
        super().__init__(
            f"The worktree for this session{branch} is unavailable and could not be "
            f"recreated{detail}. Reopen the worktree or start a new session — refusing "
            f"to run in the main checkout."
        )


def _resolve_session_worktree_cwd(
    session_id: str, project: Optional[Dict[str, Any]]
) -> Tuple[Optional[str], Optional[str], Optional[Dict[str, Any]]]:
    """Resolve the working directory for a session that may be a worktree session.

    A worktree session must NEVER silently execute in the project's main checkout.
    When the linked worktree is missing or not active we auto-recreate it from
    its branch (last git state). Even when NO worktree record is linked to the
    session, we create a fresh worktree from the session's backup branch column
    and link it — worktree sessions are never a dead end. We only raise when git
    genuinely fails, so we never fall back to running in main.

    Returns:
        (working_dir, execution_mode, worktree_info) when the session resolves to
        a worktree, OR ``(None, None, None)`` when the session is NOT a worktree
        session and the caller should use normal project/local resolution.

    Raises:
        WorktreeUnavailableError: the session IS a worktree session but a valid,
        active worktree could not be resolved or created (git failure).
    """
    # Lazy import to avoid a circular import (worktree_manager → query_engine).
    from app.core.worktree_manager import worktree_manager

    worktree = cast(Optional[Dict[str, Any]], database.get_worktree_by_session(session_id))

    def _build_info(wt: Dict[str, Any]):
        return {
            "branch": wt.get("branch_name", "unknown"),
            "base_branch": wt.get("base_branch", "main"),
        }

    if (
        worktree
        and worktree.get("status") == "active"
        and worktree_manager.is_worktree_valid(worktree)
    ):
        working_dir = get_worktree_working_dir(worktree)
        logger.debug(f"[Options] Session {session_id} using worktree cwd: {working_dir}")
        return working_dir, "worktree", _build_info(worktree)

    if worktree:
        # Worktree row exists but is inactive (completed/deleted) or missing on
        # disk. NEVER fall back to main — try to auto-recreate, else raise.
        branch_name = worktree.get("branch_name")
        logger.warning(
            f"[Options] Session {session_id} worktree {worktree.get('id')} is "
            f"status='{worktree.get('status')}' / invalid on disk — attempting "
            f"auto-recreate instead of falling back to main"
        )
        try:
            recreated = worktree_manager.recreate_worktree(worktree["id"])
        except Exception as e:  # noqa: BLE001
            raise WorktreeUnavailableError(session_id, branch_name, str(e)) from e
        if not (
            recreated
            and recreated.get("status") == "active"
            and worktree_manager.is_worktree_valid(recreated)
        ):
            raise WorktreeUnavailableError(
                session_id, branch_name, "recreated worktree is still not valid"
            )
        working_dir = get_worktree_working_dir(recreated)
        logger.info(
            f"[Options] Session {session_id} auto-recreated worktree "
            f"{worktree['id']} at {working_dir}"
        )
        return working_dir, "worktree", _build_info(recreated)

    # No worktree row found via the session join. This is either a normal local
    # session OR a worktree session whose worktree row was hard-deleted (the
    # session's worktree_id was NULLed but worktree_branch/path remain). Inspect
    # the session columns to tell them apart — a worktree-flagged session must
    # still not run in main.
    session = database.get_session(session_id)
    if session and (
        session.get("worktree_id")
        or session.get("worktree_branch")
        or session.get("worktree_path")
    ):
        # Worktree-flagged session with no linked record. Rather than dead-end,
        # CREATE a fresh worktree from the branch (last git state) and link it.
        # Only raise if git genuinely fails — we must NEVER silently run in main.
        branch_name = session.get("worktree_branch")
        if not branch_name:
            raise WorktreeUnavailableError(
                session_id, None, "worktree session has no branch to recreate from"
            )
        project_id = session.get("project_id")
        if not project_id:
            raise WorktreeUnavailableError(
                session_id, branch_name, "worktree session has no project to recreate in"
            )
        try:
            repo = database.get_git_repository_by_project(project_id)
            project = database.get_project(project_id)
            if not repo or not project:
                raise WorktreeUnavailableError(
                    session_id,
                    branch_name,
                    "project is not a git repository — cannot recreate worktree",
                )
            # Detect whether the branch already exists locally; create a new
            # branch off the default branch only when it doesn't (mirrors
            # recreate_worktree's branch-existence detection).
            main_dir = get_project_working_dir(project)
            branches = worktree_manager.git_service.list_branches(
                main_dir, include_remote=False
            )
            local_branch_names = [b["name"] for b in branches if not b["is_remote"]]
            branch_exists = branch_name in local_branch_names
            create_new = not branch_exists
            base_branch = repo.get("default_branch", "main") if create_new else None
            new_wt = worktree_manager.create_worktree(
                project_id,
                branch_name,
                create_new_branch=create_new,
                base_branch=base_branch,
            )
            database.update_session(session_id, worktree_id=new_wt["id"])
        except WorktreeUnavailableError:
            raise
        except Exception as e:  # noqa: BLE001
            raise WorktreeUnavailableError(session_id, branch_name, str(e)) from e
        if not worktree_manager.is_worktree_valid(new_wt):
            raise WorktreeUnavailableError(
                session_id, branch_name, "recreated worktree is still not valid"
            )
        working_dir = get_worktree_working_dir(new_wt)
        logger.info(
            f"[Options] Session {session_id} had no linked worktree record — "
            f"created+linked worktree {new_wt['id']} from branch "
            f"'{branch_name}' at {working_dir}"
        )
        return working_dir, "worktree", _build_info(new_wt)

    # Genuinely not a worktree session — caller uses project/local resolution.
    return None, None, None


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
# Constructed once per process when session_store_mode != "disabled".
# Wrapped in try/except so Postgres unavailability is non-fatal — callers
# get None and behave identically to the disabled case.
_session_store_singleton = None
_session_store_init_attempted = False


def _get_session_store():
    """Return the module-level PgSessionStore singleton, or None if disabled / unavailable."""
    global _session_store_singleton, _session_store_init_attempted
    if _session_store_init_attempted:
        return _session_store_singleton
    _session_store_init_attempted = True
    if settings.session_store_mode == "disabled":
        return None
    try:
        from app.core.pg_session_store import PgSessionStore
        _session_store_singleton = PgSessionStore()
        logger.info(f"PgSessionStore singleton created (mode={settings.session_store_mode})")
    except Exception:
        logger.warning(
            "PgSessionStore construction failed — proceeding without session_store. "
            "Check DATABASE_URL and Postgres connectivity.",
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

    if overrides.get("cwd"):
        # Override takes highest priority - used by agent engine for worktrees
        working_dir = overrides.get("cwd")
        working_dir_source = "override"
        # Check if override includes worktree info
        if overrides.get("worktree_info"):
            execution_mode = "worktree"
            worktree_info = overrides.get("worktree_info")
    elif session_id:
        # Resolve a worktree cwd if this session is a worktree session. This
        # raises WorktreeUnavailableError (rather than silently using main) when
        # the session is worktree-flagged but its worktree can't be recovered.
        wt_dir, wt_mode, wt_info = _resolve_session_worktree_cwd(session_id, project)
        if wt_dir is not None:
            working_dir = wt_dir
            working_dir_source = "worktree"
            execution_mode = wt_mode or "worktree"
            worktree_info = wt_info
        elif project:
            working_dir = get_project_working_dir(project)
            working_dir_source = "project"
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
            detail = f"project '{project['name']}'" if working_dir_source == "project" and project.get("name") else working_dir_source
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
    # The preset model values (haiku, sonnet, opus, fable) and their [1m] variants
    # are native Agent SDK / CLI shorthand aliases; custom model IDs pass through
    # as-is too.
    raw_model = overrides.get("model") or config.get("model")
    model_to_use = raw_model
    betas_to_use = []

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
    full_env = _build_env_with_ai_tools(config.get("env"), ai_tools_config, api_user_id)

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

    # Enable CLI-flag built-in integrations (e.g. Claude in Chrome -> --chrome).
    # These built-ins are not spawnable MCP servers; the CLI itself starts an
    # internal browser MCP server and exposes mcp__claude-in-chrome__* when the
    # flag is present. Their tool wildcard is already added to allowed_tools
    # above; here we add the flag for each enabled (non-disabled) one.
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
                "attached (session_store_mode=%s) — the SDK rejects combining the "
                "two, and the mirrored transcript supersedes local-disk checkpoints.",
                settings.session_store_mode,
            )
            options.enable_file_checkpointing = False

    return options, agents_dict
