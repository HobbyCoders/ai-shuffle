"""
SDK option building for Claude queries.

Contains functions that construct ClaudeAgentOptions, assemble system prompts,
configure MCP servers, build tool/permission configurations, and manage
subagent definitions.
"""

import logging
import platform
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime, timezone

from claude_agent_sdk import ClaudeAgentOptions, AgentDefinition

from app.db import database
from app.core.config import settings
from app.core.profiles import get_profile
from app.core.platform import detect_deployment_mode, DeploymentMode
from app.core import encryption

from app.core.project_utils import get_project_working_dir, get_worktree_working_dir
from app.core.mcp_service import get_mcp_service, McpServerType
from app.core.query_streaming import DEFAULT_SDK_BUFFER_SIZE

logger = logging.getLogger(__name__)


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
    effort = overrides.get('effort') or config.get('effort')
    if effort and effort in ('low', 'medium', 'high', 'max'):
        result['effort'] = effort
        logger.debug(f"Using effort={effort}")

    return result


def write_agents_to_filesystem(agents_dict: Dict[str, AgentDefinition], cwd: str) -> Optional[Path]:
    """
    Write subagent definitions to .claude/agents/ directory in the working directory.

    WINDOWS-ONLY WORKAROUND: The Claude Code CLI has a known bug where --agents flag
    doesn't work properly on Windows. Instead, we write agent files to .claude/agents/
    which the CLI discovers on startup.

    On Docker/Linux, agents are passed directly via the SDK's --agents flag (which works
    correctly there) and this function should NOT be called.

    Args:
        agents_dict: Dict mapping agent IDs to AgentDefinition objects
        cwd: Working directory where .claude/agents/ will be created

    Returns:
        Path to the agents directory if created, None if no agents
    """
    if not agents_dict:
        return None

    # Create .claude/agents directory in the working directory
    agents_dir = Path(cwd) / ".claude" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)

    written_files = []

    for agent_id, agent_def in agents_dict.items():
        # Build YAML frontmatter with proper escaping
        def _yaml_quote(s: str) -> str:
            """Quote a string if it contains YAML special characters."""
            if not s:
                return '""'
            # Characters that require quoting in YAML values
            needs_quoting = any(c in s for c in ':{}[]|>&*!#%@`\'"?,\n\\')
            if needs_quoting or s.strip() != s:
                # Use double quotes with escaped internal double quotes
                escaped = s.replace('\\', '\\\\').replace('"', '\\"')
                return f'"{escaped}"'
            return s

        frontmatter_lines = [
            "---",
            f"name: {_yaml_quote(agent_id)}",
            f"description: {_yaml_quote(agent_def.description)}",
        ]

        # Add tools if specified
        if agent_def.tools:
            tools_str = ", ".join(agent_def.tools)
            frontmatter_lines.append(f"tools: {_yaml_quote(tools_str)}")

        # Add model if specified
        if agent_def.model:
            frontmatter_lines.append(f"model: {_yaml_quote(agent_def.model)}")

        # Add skills if specified (SDK 0.1.49+)
        if agent_def.skills:
            skills_str = ", ".join(agent_def.skills)
            frontmatter_lines.append(f"skills: {_yaml_quote(skills_str)}")

        # Add memory if specified (SDK 0.1.49+)
        if agent_def.memory:
            frontmatter_lines.append(f"memory: {_yaml_quote(agent_def.memory)}")

        # Add mcpServers if specified (SDK 0.1.49+)
        if agent_def.mcpServers:
            # mcpServers can be a list of strings or dicts — serialize as YAML list
            mcp_items = []
            for s in agent_def.mcpServers:
                if isinstance(s, str):
                    mcp_items.append(s)
                else:
                    # Complex config — skip for filesystem workaround (SDK handles these via --agents)
                    pass
            if mcp_items:
                mcp_str = ", ".join(mcp_items)
                frontmatter_lines.append(f"mcpServers: {_yaml_quote(mcp_str)}")

        # New AgentDefinition fields (SDK 0.1.51+)
        if getattr(agent_def, 'disallowedTools', None):
            dt_str = ", ".join(agent_def.disallowedTools)
            frontmatter_lines.append(f"disallowedTools: {_yaml_quote(dt_str)}")
        if getattr(agent_def, 'maxTurns', None) is not None:
            frontmatter_lines.append(f"maxTurns: {agent_def.maxTurns}")
        if getattr(agent_def, 'initialPrompt', None):
            frontmatter_lines.append(f"initialPrompt: {_yaml_quote(agent_def.initialPrompt)}")

        frontmatter_lines.append("---")
        frontmatter_lines.append("")  # Empty line after frontmatter

        # Build full file content
        content = "\n".join(frontmatter_lines) + agent_def.prompt

        # Write to file
        agent_file = agents_dir / f"{agent_id}.md"
        agent_file.write_text(content, encoding="utf-8")
        written_files.append(agent_file)
        logger.debug(f"Wrote subagent file: {agent_file}")

    logger.debug(f"Wrote {len(written_files)} subagent files to {agents_dir}")
    return agents_dir


def cleanup_agents_directory(agents_dir: Path, agent_ids: list) -> None:
    """
    Clean up subagent files that were written for this session.

    Only removes files that match the agent IDs we wrote, to avoid
    removing user-created agents.

    Args:
        agents_dir: Path to .claude/agents/ directory
        agent_ids: List of agent IDs to remove
    """
    if not agents_dir or not agents_dir.exists():
        return

    for agent_id in agent_ids:
        agent_file = agents_dir / f"{agent_id}.md"
        if agent_file.exists():
            try:
                agent_file.unlink()
                logger.debug(f"Removed subagent file: {agent_file}")
            except Exception as e:
                logger.warning(f"Failed to remove subagent file {agent_file}: {e}")


def resolve_note_references(prompt: str) -> str:
    """Resolve legacy @Notes/note-id references in a prompt.

    New notes use physical file paths that the SDK reads natively.
    This handles legacy virtual references from older messages by
    redirecting to the physical file or inlining the content.
    """
    def replace_note(match):
        note_id = match.group(1)
        note = database.get_note(note_id)
        if note:
            # If the note has a physical file, redirect to it
            if note.get("file_path"):
                from app.core.note_files import get_absolute_file_path
                abs_path = str(get_absolute_file_path(note["file_path"]))
                return f"@{abs_path}"
            # Otherwise inline the content (pre-migration fallback)
            title = note.get("title", "Untitled")
            content = note.get("content", "")
            return f"[Note: {title}]\n{content}"
        return match.group(0)  # Keep original if not found

    return re.sub(r'@Notes/(note-[a-f0-9]+)', replace_note, prompt)


def _build_mcp_servers_dict(
    project_dir: Optional[Path] = None,
) -> Optional[dict]:
    """
    Build the mcp_servers dict for ClaudeAgentOptions from registered MCP servers.

    Reads MCP servers from two sources:
    1. Scope-based .mcp.json files (user, local, project) via McpService
    2. Installed+enabled plugin directories (each plugin can have its own .mcp.json)

    Args:
        project_dir: Optional project directory for project-scoped servers.

    Returns:
        Dictionary mapping server names to SDK MCP server configs, or None if empty.
    """
    import json as _json

    mcp_servers: dict = {}

    # --- Source 1: Scope-based .mcp.json files ---
    service = None
    try:
        service = get_mcp_service(project_dir)
        all_servers = service.get_all_servers()
    except Exception as e:
        logger.warning(f"Failed to load MCP servers from scopes: {e}")
        all_servers = {}

    for scope_servers in all_servers.values():
        for server in scope_servers:
            if not server.enabled:
                continue
            if server.name in mcp_servers:
                continue
            if server.type == McpServerType.STDIO or server.type == "stdio":
                config: dict = {"command": server.command}
                if server.args:
                    config["args"] = server.args
                if server.env:
                    config["env"] = server.env
                mcp_servers[server.name] = config
            elif server.type == McpServerType.SSE or server.type == "sse":
                config = {"type": "sse", "url": server.url}
                if server.headers:
                    config["headers"] = server.headers
                mcp_servers[server.name] = config
            elif server.type == McpServerType.HTTP or server.type == "http":
                config = {"type": "http", "url": server.url}
                if server.headers:
                    config["headers"] = server.headers
                mcp_servers[server.name] = config

    # NOTE: Plugin MCP servers (e.g. Context7) are NOT added here.
    # The SDK loads installed plugins automatically from ~/.claude/plugins/,
    # just like the CLI does. Manually adding them would create duplicates
    # (e.g. mcp__context7__* AND mcp__plugin_context7_context7__*).

    if mcp_servers:
        logger.info(f"Passing {len(mcp_servers)} MCP server(s) to SDK: {list(mcp_servers.keys())}")
        return mcp_servers

    return None


def _build_mcp_servers_dict_with_ai_tools(
    project_dir: Optional[Path] = None,
    ai_tools_config: Optional[Dict[str, Any]] = None,
    tool_env: Optional[Dict[str, str]] = None,
) -> Optional[dict]:
    """
    Build MCP servers dict including AI content generation tools.

    Wraps _build_mcp_servers_dict() and adds the AI tools SDK MCP server
    when any AI tools are enabled in the profile.

    Args:
        project_dir: Optional project directory for project-scoped servers.
        ai_tools_config: Optional dict with individual AI tool toggles.
        tool_env: Environment variables (API keys, provider settings) to inject
            into AI tool Node.js subprocesses.

    Returns:
        Dictionary mapping server names to SDK MCP server configs, or None if empty.
    """
    mcp_servers = _build_mcp_servers_dict(
        project_dir=project_dir,
    ) or {}

    # Add AI tools SDK MCP server if any AI tools are enabled
    if ai_tools_config and any(ai_tools_config.values()):
        try:
            from app.mcp.ai_tools_sdk import create_ai_tools_server
            tools_dir = settings.effective_tools_dir
            if tools_dir:
                ai_server = create_ai_tools_server(
                    ai_tools_config, str(tools_dir),
                    tool_env=tool_env,
                    tool_cwd=str(project_dir) if project_dir else str(settings.effective_workspace_dir),
                )
                if ai_server:
                    mcp_servers["ai-tools"] = ai_server
                    logger.debug("Added in-process SDK MCP server for ai-tools")
        except Exception as e:
            logger.warning(f"Failed to create AI tools SDK MCP server: {e}")

    # Add connector tools SDK MCP server if any connectors with tools are active
    try:
        from app.mcp.connector_tools_sdk import create_connector_tools_server
        connector_server = create_connector_tools_server()
        if connector_server:
            mcp_servers["connector-tools"] = connector_server
            logger.debug("Added in-process SDK MCP server for connector-tools")
    except Exception as e:
        logger.warning(f"Failed to create connector tools SDK MCP server: {e}")

    if mcp_servers:
        return mcp_servers

    return None


def _build_plugins_list(
    profile_enabled_plugins: Optional[list],
    setting_sources: Optional[list] = None
) -> Optional[list]:
    """
    Build the plugins list for ClaudeAgentOptions.

    IMPORTANT: When setting_sources includes "user", the SDK auto-loads plugins from
    ~/.claude/settings.json. We must NOT pass those same plugins explicitly, or they
    will be loaded twice (causing duplicate skills, agents, etc.).

    This function only adds plugins from profile config when:
    1. setting_sources does NOT include "user" (we need to load all plugins explicitly)
    2. OR the plugin is specified in profile config but NOT in user settings

    According to the Claude Agent SDK documentation, plugins are specified as:
    [{"type": "local", "path": "./my-plugin"}, ...]

    Supports two formats for plugin entries:
    1. Plugin IDs: "plugin-name@marketplace" - resolved via installed_plugins.json
    2. Direct paths: "/path/to/plugin" or "relative/path" - used directly

    Args:
        profile_enabled_plugins: List of plugin IDs or paths from profile config
        setting_sources: List of setting sources (e.g., ["user", "project"])

    Returns:
        List of plugin dicts for SDK, or None if no plugins enabled/needed
    """
    # Import here to avoid circular imports
    from app.core.plugin_service import get_plugin_service
    service = get_plugin_service()

    # Check if SDK will auto-load plugins from user settings
    # When "user" is in setting_sources, SDK loads plugins from ~/.claude/settings.json
    sdk_loads_user_plugins = setting_sources and "user" in setting_sources

    # Load installed plugins registry to resolve plugin IDs
    installed_plugins = {}
    installed_plugins_file = service.INSTALLED_FILE
    try:
        if installed_plugins_file.exists():
            import json
            with open(installed_plugins_file, 'r') as f:
                data = json.load(f)
                installed_plugins = data.get("plugins", {})
    except Exception as e:
        logger.warning(f"Failed to load installed_plugins.json: {e}")

    # Get plugins enabled in Claude's settings.json (for reference/logging)
    settings_enabled = service.get_enabled_plugins()  # Returns Dict[str, bool]
    settings_plugin_ids = {pid for pid, enabled in settings_enabled.items() if enabled}
    logger.debug(f"Plugins in user settings.json: {list(settings_plugin_ids)}")

    # Build list of plugins to pass explicitly
    all_plugin_entries = []

    if sdk_loads_user_plugins:
        # SDK will auto-load user settings plugins - only add profile-specific plugins
        # that are NOT already in user settings (to avoid duplicates)
        logger.debug("SDK will auto-load user plugins (setting_sources includes 'user')")
        if profile_enabled_plugins:
            for entry in profile_enabled_plugins:
                if entry and entry not in settings_plugin_ids:
                    all_plugin_entries.append(entry)
                    logger.debug(f"Adding profile-only plugin (not in user settings): {entry}")
    else:
        # SDK won't auto-load - we need to pass all plugins explicitly
        logger.debug("SDK won't auto-load plugins - adding all enabled plugins explicitly")
        for plugin_id, is_enabled in settings_enabled.items():
            if is_enabled:
                all_plugin_entries.append(plugin_id)

        # Add plugins from profile config (may be paths or IDs)
        if profile_enabled_plugins:
            for entry in profile_enabled_plugins:
                if entry and entry not in all_plugin_entries:
                    all_plugin_entries.append(entry)

    if not all_plugin_entries:
        logger.debug("No plugins enabled in settings.json or profile config")
        return None

    plugins = []
    seen_paths = set()  # Avoid duplicates

    for plugin_entry in all_plugin_entries:
        if not plugin_entry:
            continue

        resolved_path = None

        # Check if this is a plugin ID format (name@marketplace)
        if "@" in plugin_entry and not plugin_entry.startswith("/") and not plugin_entry.startswith("."):
            # Look up in installed_plugins registry
            if plugin_entry in installed_plugins:
                installations = installed_plugins[plugin_entry]
                if installations:
                    resolved_path = Path(installations[0].get("installPath", ""))
                    if not resolved_path.exists():
                        logger.warning(f"Plugin install path not found: {resolved_path}")
                        resolved_path = None
            else:
                logger.warning(f"Plugin not found in installed_plugins.json: {plugin_entry}")
        else:
            # Direct path format
            path = Path(plugin_entry)
            if path.is_absolute():
                resolved_path = path
            else:
                resolved_path = settings.workspace_dir / plugin_entry

        if resolved_path and str(resolved_path) not in seen_paths:
            # Verify the plugin directory exists (manifest path checked for existence)
            _plugin_manifest = resolved_path / ".claude-plugin" / "plugin.json"
            if resolved_path.exists():
                plugins.append({
                    "type": "local",
                    "path": str(resolved_path)
                })
                seen_paths.add(str(resolved_path))
                logger.debug(f"Adding plugin: {resolved_path}")
            else:
                logger.warning(f"Plugin directory not found: {resolved_path}")

    if not plugins:
        return None

    logger.debug(f"Built plugins list with {len(plugins)} plugin(s)")
    return plugins


# Default instructions for displaying media and files in chat
# Can be customized via admin settings API
DEFAULT_SECURITY_INSTRUCTIONS = """
## App-Identity
You are Claude Code running inside AI Shuffle — a self-hosted web chat application (SvelteKit + FastAPI + Claude Agent SDK). When the user mentions "this app", "the app", or discusses bugs/features without specifying another project, they mean the current project that is in your workspace. Never forget this context.

## File-Sharing
MANDATORY: When you create, generate, or modify ANY file the user may want to download or view, you MUST provide a download link. Do NOT just mention the file path in prose — the user cannot click a raw path.

Format: 📎[filename.ext](/api/files/by-path?path=/full/path/to/file.ext)

## Generated Media in Chat

When an AI tool (generateImage, editImage, generateWithReference, generateVideo, etc.) returns a successful result, display the media inline using the `image_url` or `video_url` from the tool result:

- **Images:** `![Description](IMAGE_URL_FROM_RESULT)`
- **Videos:** `[Description](VIDEO_URL_FROM_RESULT)`

Example - after generateImage returns `{"success": true, "image_url": "/api/generated-images/by-path?path=..."}`:

## Chat-History
When the user references a .jsonl chat history file from ~/.claude/projects/: this is a previous conversation for context, NOT a file to edit. Scan for key decisions, code discussed, and user preferences. Summarize what you found. Do not read every line unless explicitly asked to summarize the full conversation.
"""


def get_security_instructions() -> str:
    """
    Get the active security instructions to prepend to system prompts.

    Returns custom instructions if set by admin, otherwise returns the default.
    """
    custom_instructions = database.get_system_setting("security_instructions")
    if custom_instructions:
        return custom_instructions
    return DEFAULT_SECURITY_INSTRUCTIONS


def _is_git_repo(working_dir: str) -> bool:
    """Check if a directory is a git repository."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=working_dir,
            capture_output=True,
            text=True,
            timeout=5
        )
        return result.returncode == 0
    except Exception:
        return False


def _get_os_version() -> str:
    """Get the OS version string."""
    try:
        if sys.platform == "linux":
            return f"Linux {platform.release()}"
        elif sys.platform == "darwin":
            return f"macOS {platform.mac_ver()[0]}"
        elif sys.platform == "win32":
            return f"Windows {platform.release()}"
        else:
            return f"{sys.platform} {platform.release()}"
    except Exception:
        return sys.platform


def _get_decrypted_api_key(setting_name: str) -> Optional[str]:
    """
    Get an API key from the database and decrypt it if encrypted.

    Args:
        setting_name: The name of the setting (e.g., "openai_api_key")

    Returns:
        The decrypted API key, or None if not found or decryption fails
    """
    value = database.get_system_setting(setting_name)
    if not value:
        return None

    # Check if the value is encrypted
    if encryption.is_encrypted(value):
        if not encryption.is_encryption_ready():
            logger.warning(f"Cannot decrypt {setting_name}: encryption key not available")
            return None
        try:
            decrypted = encryption.decrypt_value(value)
            return decrypted
        except Exception as e:
            logger.error(f"Failed to decrypt {setting_name}: {e}")
            return None

    # Return plaintext value (for backwards compatibility during migration)
    return value


def _resolve_api_credential(
    credential_type: str,
    api_user_id: Optional[str] = None
) -> Optional[str]:
    """
    Resolve an API credential based on policy (user key vs admin fallback).

    Resolution order based on policy:
    1. If policy is 'user_provided' or 'optional': check user's credential first
    2. If not found and policy is 'admin_provided' or 'optional': use admin's key
    3. If not found: return None

    Args:
        credential_type: The credential type (e.g., "openai_api_key", "gemini_api_key")
        api_user_id: The API user ID (None for admin/local users)

    Returns:
        The resolved API key, or None if not available
    """
    # Map credential types to admin settings
    admin_setting_map = {
        "fal_api_key": "fal_api_key",
        "github_pat": None  # No admin fallback for GitHub
    }

    if credential_type not in admin_setting_map:
        logger.warning(f"Unknown credential type: {credential_type}")
        return None

    # Get per-user policy for this credential
    # Policies are always per-user, no global fallback
    # Default to "user_provided" if no policy exists (shouldn't happen for properly created users)
    policy_obj = None
    if api_user_id:
        policy_obj = database.get_user_credential_policy(api_user_id, credential_type)
    policy = policy_obj.get("policy", "user_provided") if policy_obj else "user_provided"

    resolved_key = None
    source = None

    # Try user's credential if applicable
    if api_user_id and policy in ["user_provided", "optional"]:
        user_cred = database.get_user_credential(api_user_id, credential_type)
        if user_cred:
            encrypted = user_cred.get("encrypted_value")
            if encrypted:
                if encryption.is_encrypted(encrypted) and encryption.is_encryption_ready():
                    try:
                        resolved_key = encryption.decrypt_value(encrypted)
                        source = "user"
                    except Exception as e:
                        logger.error(f"Failed to decrypt user credential {credential_type}: {e}")
                elif not encryption.is_encrypted(encrypted):
                    # Plaintext (shouldn't happen in production, but handle it)
                    resolved_key = encrypted
                    source = "user"

    # Try admin's key if applicable
    if not resolved_key and policy in ["admin_provided", "optional"]:
        admin_setting = admin_setting_map.get(credential_type)
        if admin_setting:
            resolved_key = _get_decrypted_api_key(admin_setting)
            if resolved_key:
                source = "admin"

    if resolved_key:
        logger.debug(f"Resolved {credential_type} from {source} (policy={policy})")
    elif policy == "user_provided":
        logger.debug(f"Credential {credential_type} not found - user must provide (policy={policy})")
    else:
        logger.debug(f"Credential {credential_type} not configured (policy={policy})")

    return resolved_key


def _get_hook_tool_names(hooks: Optional[Dict[str, list]]) -> list:
    """
    Extract tool names from hook matchers.

    When we have hooks that handle specific tools (like AskUserQuestion),
    we need to know which tools to enable so the model can call them.
    The hook then intercepts and handles the tool call.

    Args:
        hooks: Dict of hook configurations (e.g., {"PreToolUse": [HookMatcher(...)]})

    Returns:
        List of tool names that have hooks configured
    """
    tools = []

    if not hooks:
        return tools

    # Check for PreToolUse hooks and extract tool names from matchers
    pre_tool_hooks = hooks.get("PreToolUse", [])
    for hook_matcher in pre_tool_hooks:
        if hasattr(hook_matcher, "matcher") and hook_matcher.matcher:
            # The matcher can be a tool name like "AskUserQuestion"
            # or a pattern like "Bash|Edit" - we add all parts
            tool_names = hook_matcher.matcher.split("|")
            for tool_name in tool_names:
                tool_name = tool_name.strip()
                if tool_name and tool_name not in tools:
                    tools.append(tool_name)
                    logger.debug(f"Found hooked tool: '{tool_name}'")

    return tools


def _build_extra_args(
    base_extra_args: Dict[str, str],
    hooks: Optional[Dict[str, list]],
    deployment_mode: Optional["DeploymentMode"] = None,
) -> Dict[str, str]:
    """
    Build extra_args dict, adding --allowed-tools for any hooked tools.

    When we have hooks that handle specific tools (like AskUserQuestion),
    we must pass them via --allowed-tools CLI flag so the model can call them.
    The SDK's allowed_tools option only filters existing tools, it doesn't add new ones.

    Args:
        base_extra_args: Base extra args from profile config
        hooks: Dict of hook configurations (e.g., {"PreToolUse": [HookMatcher(...)]})
        deployment_mode: Current deployment mode (LOCAL or DOCKER)

    Returns:
        Dict of extra args to pass to CLI
    """
    extra_args = dict(base_extra_args) if base_extra_args else {}

    # CLI debug output to stderr: opt-in only via LOG_LEVEL=DEBUG.
    # Previously this was always-on in Docker mode, but the Claude CLI
    # generates tens of thousands of debug lines per query during tool use.
    # Even with Python's log level filtering, every line still requires:
    # pipe read -> bytes decode -> string format -> callback -> logger check,
    # causing significant CPU overhead in production.
    if deployment_mode and deployment_mode != DeploymentMode.LOCAL:
        if settings.log_level.upper() == "DEBUG" and "debug-to-stderr" not in extra_args:
            extra_args["debug-to-stderr"] = None
            logger.debug("Added --debug-to-stderr for Docker CLI diagnostics (LOG_LEVEL=DEBUG)")

    # Get tool names from hooks
    hook_tools = _get_hook_tool_names(hooks)

    if hook_tools:
        # Merge with any existing allowed-tools
        existing = extra_args.get("allowed-tools", "")
        existing_tools = [t.strip() for t in existing.split(",") if t.strip()] if existing else []

        # Add hook tools that aren't already in the list
        for tool in hook_tools:
            if tool not in existing_tools:
                existing_tools.append(tool)
                logger.debug(f"Adding '{tool}' to --allowed-tools (required for hook handler)")

        if existing_tools:
            extra_args["allowed-tools"] = ",".join(existing_tools)

    return extra_args


def _build_env_with_ai_tools(
    base_env: Optional[Dict[str, str]],
    ai_tools_config: Optional[Dict[str, Any]],
    api_user_id: Optional[str] = None
) -> Dict[str, str]:
    """
    Build environment variables dict, injecting AI tool credentials when enabled.

    This allows the Claude CLI to execute scripts that use AI tools
    with the necessary API keys, without exposing them in the prompt.

    NEW DESIGN: Inject ALL available API keys so Claude can dynamically switch
    between providers without needing settings changes. The default provider/model
    is still set, but Claude can override per-request.

    CREDENTIAL RESOLUTION: When api_user_id is provided, credentials are resolved
    based on admin policy (admin_provided, user_provided, optional). This allows
    API users to use their own keys when required by policy.

    Args:
        base_env: Base environment variables from profile config
        ai_tools_config: Dict with individual tool toggles (e.g., {"image_generation": True})
        api_user_id: Optional API user ID for user-specific credential resolution

    Returns:
        Dict of environment variables to pass to Claude CLI
    """
    env = dict(base_env) if base_env else {}

    # Strip CLAUDECODE env var to prevent "nested session" detection when
    # the server is started from within a Claude Code session.  The SDK
    # merges os.environ with this dict, so setting it to empty overrides
    # the inherited value.
    env.pop("CLAUDECODE", None)
    env["CLAUDECODE"] = ""

    if not ai_tools_config:
        return env

    # Check if any AI tools are enabled
    any_image_tools = ai_tools_config.get("image_generation", False) or ai_tools_config.get("image_editing", False) or ai_tools_config.get("image_reference", False)
    any_video_tools = ai_tools_config.get("video_generation", False) or ai_tools_config.get("image_to_video", False) or ai_tools_config.get("video_extend", False)
    any_3d_tools = ai_tools_config.get("text_to_3d", False) or ai_tools_config.get("image_to_3d", False)
    any_audio_tools = ai_tools_config.get("tts", False) or ai_tools_config.get("stt", False)

    if not any_image_tools and not any_video_tools and not any_3d_tools and not any_audio_tools:
        return env

    # Resolve fal.ai API key (single provider architecture)
    if api_user_id:
        fal_api_key = _resolve_api_credential("fal_api_key", api_user_id)
        logger.debug(f"Resolved fal.ai credential for API user {api_user_id}")
    else:
        fal_api_key = _get_decrypted_api_key("fal_api_key")

    if fal_api_key:
        env["FAL_KEY"] = fal_api_key
        logger.debug("Injected FAL_KEY for fal.ai tools")

    # Set default provider/model from settings
    if any_image_tools:
        image_model = database.get_system_setting("image_model")
        image_edit_model = database.get_system_setting("image_edit_model")
        env["IMAGE_PROVIDER"] = "fal-ai"
        if image_model:
            env["IMAGE_MODEL"] = image_model
            logger.debug(f"Set default IMAGE_MODEL={image_model}")
        if image_edit_model:
            env["IMAGE_EDIT_MODEL"] = image_edit_model
            logger.debug(f"Set default IMAGE_EDIT_MODEL={image_edit_model}")
        image_aspect_ratio = database.get_system_setting("image_aspect_ratio")
        image_resolution = database.get_system_setting("image_resolution")
        if image_aspect_ratio:
            env["IMAGE_ASPECT_RATIO"] = image_aspect_ratio
        if image_resolution:
            env["IMAGE_RESOLUTION"] = image_resolution

    if any_video_tools:
        video_model = database.get_system_setting("video_model")
        i2v_model = database.get_system_setting("i2v_model")
        env["VIDEO_PROVIDER"] = "fal-ai"
        if video_model:
            env["VIDEO_MODEL"] = video_model
            logger.debug(f"Set default VIDEO_MODEL={video_model}")
        if i2v_model:
            env["I2V_MODEL"] = i2v_model
            logger.debug(f"Set default I2V_MODEL={i2v_model}")
        video_aspect_ratio = database.get_system_setting("video_aspect_ratio")
        video_duration = database.get_system_setting("video_duration")
        video_resolution = database.get_system_setting("video_resolution")
        if video_aspect_ratio:
            env["VIDEO_ASPECT_RATIO"] = video_aspect_ratio
        if video_duration:
            env["VIDEO_DURATION"] = video_duration
        if video_resolution:
            env["VIDEO_RESOLUTION"] = video_resolution

    if any_3d_tools:
        model3d_model = database.get_system_setting("model3d_model")
        image_to_3d_model = database.get_system_setting("image_to_3d_model")
        env["MODEL3D_PROVIDER"] = "fal-ai"
        if model3d_model:
            env["MODEL3D_MODEL"] = model3d_model
            logger.debug(f"Set default MODEL3D_MODEL={model3d_model}")
        if image_to_3d_model:
            env["IMAGE_TO_3D_MODEL"] = image_to_3d_model
            logger.debug(f"Set default IMAGE_TO_3D_MODEL={image_to_3d_model}")

    if any_audio_tools:
        tts_model = database.get_system_setting("tts_model")
        stt_model = database.get_system_setting("stt_model")
        if tts_model:
            env["TTS_MODEL"] = tts_model
        if stt_model:
            env["STT_MODEL"] = stt_model
        stt_language = database.get_system_setting("stt_language")
        if stt_language:
            env["STT_LANGUAGE"] = stt_language

    return env


def generate_environment_details(
    working_dir: str,
    ai_tools_config: Optional[Dict[str, Any]] = None,
    execution_mode: str = "local",
    worktree_info: Optional[Dict[str, str]] = None
) -> str:
    """
    Generate environment details block for custom system prompts.
    Similar to Claude Code's dynamic environment injection.

    Args:
        working_dir: The working directory path
        ai_tools_config: Deprecated, ignored. AI tools are registered as native MCP tools
            with their own descriptions — no system prompt injection needed.
        execution_mode: Either "local" or "worktree"
        worktree_info: Dict with branch and base_branch info when in worktree mode
    """
    is_git = _is_git_repo(working_dir)
    os_version = _get_os_version()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Build execution mode lines
    execution_lines = f"\nExecution mode: {execution_mode}"
    if execution_mode == "worktree" and worktree_info:
        branch = worktree_info.get("branch", "unknown")
        base_branch = worktree_info.get("base_branch", "main")
        execution_lines += f"\nCurrent branch: {branch}\nBase branch: {base_branch}"

    return f"""Here is useful information about the environment you are running in:
<env>
Working directory: {working_dir}
Is directory a git repo: {"Yes" if is_git else "No"}
Platform: {sys.platform}
OS Version: {os_version}
Today's date: {today}{execution_lines}
</env>"""


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

    Note: On Windows local mode, --agents CLI flag has a bug where agents are not discovered.
    Callers should use write_agents_to_filesystem() on Windows only. On Docker/Linux, agents
    are passed directly via the SDK's --agents flag (which works correctly).
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

    # Determine permission mode
    permission_mode = overrides.get("permission_mode") or config.get("permission_mode", "default")

    # Create stderr callback to log CLI output
    # Note: We only log stderr, don't forward to frontend. The SDK sends proper
    # typed messages for important events (compact_boundary, api errors via
    # synthetic AssistantMessage with isApiErrorMessage, etc.)
    # Use lazy %-formatting to avoid f-string overhead when debug is disabled
    def stderr_callback(line: str):
        logger.debug("[Claude CLI stderr] %s", line)

    # Process model selection
    # Models like "opus[1m]" are native CLI aliases — pass them through as-is.
    # Only map custom shorthand names (e.g., "sonnet-1m") that the CLI doesn't
    # natively recognize to base model + beta headers.
    raw_model = overrides.get("model") or config.get("model")
    model_to_use = raw_model
    betas_to_use = []

    # Map custom shorthand names to base model + beta headers.
    # Do NOT map CLI-native aliases like "opus[1m]" — the CLI handles those directly.
    MODEL_1M_MAPPING = {
        "sonnet-1m": ("sonnet", ["context-1m-2025-08-07"]),
        "claude-sonnet-4-5-1m": ("claude-sonnet-4-5-20250514", ["context-1m-2025-08-07"]),
        "claude-sonnet-4-1m": ("claude-sonnet-4-20250514", ["context-1m-2025-08-07"]),
    }

    if raw_model in MODEL_1M_MAPPING:
        model_to_use, betas_to_use = MODEL_1M_MAPPING[raw_model]
        logger.debug(f"1M context model selected: {raw_model} -> model={model_to_use}, betas={betas_to_use}")

    # Fast mode: add beta header when enabled and using an Opus model.
    # Fast mode (internally "Penguin Mode") uses the fast-mode-2026-02-01 beta header
    # to enable faster output. Only applicable to Opus 4.6+ models.
    FAST_MODE_BETA_HEADER = "fast-mode-2026-02-01"
    fast_mode_requested = overrides.get("fast_mode") == "true"
    if fast_mode_requested and model_to_use and "opus" in str(model_to_use).lower():
        if FAST_MODE_BETA_HEADER not in betas_to_use:
            betas_to_use.append(FAST_MODE_BETA_HEADER)
            logger.debug(f"Fast mode enabled for model={model_to_use}, added beta={FAST_MODE_BETA_HEADER}")

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
        mcp_servers=_build_mcp_servers_dict_with_ai_tools(
            project_dir=Path(working_dir) if working_dir else None,
            ai_tools_config=ai_tools_config,
            tool_env=ai_tool_env,
        ) or {},

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

        # Subagents - On Docker/Linux, pass via --agents (works correctly)
        # On Windows, agents=None because --agents CLI flag has a bug - must write to filesystem instead
        agents=agents_dict if detect_deployment_mode() != DeploymentMode.LOCAL else None,

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
    if deployment_mode == DeploymentMode.LOCAL:
        logger.debug(f"Windows local mode: Agents to write to filesystem: {list(agents_dict.keys()) if agents_dict else 'None'}")
    else:
        logger.debug(f"Docker mode: Agents passed via SDK: {list(agents_dict.keys()) if agents_dict else 'None'}")

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
