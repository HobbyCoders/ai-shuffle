"""
Query engine for executing Claude queries with profiles

Based on patterns from Anvil's SessionManager:
- Keep SDK clients connected for the lifetime of a session
- Don't disconnect after every query (causes async context issues)
- Create new client with 'resume' option when resuming after app restart
"""

import logging
import platform
import re
import subprocess
import sys
import uuid
import asyncio
from pathlib import Path
from typing import Optional, Dict, Any, AsyncGenerator
from datetime import datetime

from claude_agent_sdk import query, ClaudeAgentOptions, ClaudeSDKClient, AgentDefinition
from claude_agent_sdk import (
    AssistantMessage, UserMessage, TextBlock, ToolUseBlock, ToolResultBlock,
    ResultMessage, SystemMessage
)
from claude_agent_sdk.types import StreamEvent, PermissionResultAllow, PermissionResultDeny

# Monkey-patch SDK message parser to handle unknown message types gracefully.
#
# Root cause: The bundled CLI (v2.1.49) emits "rate_limit_event" messages, but
# the Python SDK v0.1.39 (latest on PyPI) raises MessageParseError for unknown
# types. The upstream fix (returning None for unknown types) is merged on main
# at github.com/anthropics/claude-agent-sdk-python but not yet released.
#
# Fix: Convert unknown message types into SystemMessage with the raw data
# preserved, so they flow through the pipeline normally instead of crashing.
# Remove this patch once a new SDK version is released with the catch-all fix.
def _patch_sdk_message_parser():
    import claude_agent_sdk._internal.message_parser as _parser
    import claude_agent_sdk._internal.client as _internal_client
    from claude_agent_sdk._errors import MessageParseError

    _original_parse = _parser.parse_message

    def _safe_parse_message(data):
        try:
            return _original_parse(data)
        except MessageParseError as e:
            if "Unknown message type" in str(e):
                _msg_type = data.get("type", "?") if isinstance(data, dict) else "?"
                logging.getLogger(__name__).debug(
                    f"[SDK] Unrecognized message type from CLI: {_msg_type}"
                )
                # Wrap as SystemMessage so it flows through the pipeline
                # instead of killing the stream
                return SystemMessage(subtype=_msg_type, data=data)
            raise

    _parser.parse_message = _safe_parse_message
    # Also patch the direct reference in _internal/client.py, which imports
    # parse_message at the top level (from .message_parser import parse_message).
    # Without this, _internal/client.py holds a reference to the original function
    # and the query() code path (used by generation.py) still crashes.
    _internal_client.parse_message = _safe_parse_message

_patch_sdk_message_parser()

from app.db import database
from app.core.config import settings
from app.core.session_pool import (
    SessionState, get_session_pool, is_subprocess_alive,
    requires_reconnect, graceful_disconnect
)
from app.core.profiles import get_profile
from app.core.sync_engine import sync_engine
from app.core.permission_handler import permission_handler
from app.core.platform import detect_deployment_mode, DeploymentMode
from app.core.user_question_handler import user_question_handler
from app.core import encryption

from app.core.project_utils import get_project_working_dir, get_worktree_working_dir
from app.core.mcp_service import get_mcp_service, McpServerType
from app.core.live_session_store import live_session_store
from app.core.message_resolver import message_resolver, resolve_working_dir

logger = logging.getLogger(__name__)

# Sentinel for "use default" (distinguishes None from "not provided")
_UNSET = object()

# Use centralized constants from config (can be overridden via env vars)
MAX_TOOL_OUTPUT_SIZE = settings.max_tool_output_chars
MAX_DISPLAY_OUTPUT_SIZE = settings.max_display_output_chars
DEFAULT_SDK_BUFFER_SIZE = settings.sdk_buffer_size


def extract_mcp_image(content: Any) -> Optional[str]:
    """
    Extract base64 image data from MCP-style content block lists.

    MCP tools can return content as a list of blocks:
    [{"type": "text", "text": "..."}, {"type": "image", "data": "base64...", "mimeType": "image/jpeg"}]

    Returns the first image's base64 data, or None if no image found.
    """
    if not isinstance(content, list):
        return None
    for block in content:
        if isinstance(block, dict) and block.get("type") == "image":
            return block.get("data")
    return None


def truncate_large_payload(content: Any, max_size: int = MAX_TOOL_OUTPUT_SIZE) -> str:
    """
    Intelligently truncate large tool output payloads.

    Handles:
    - Base64 encoded images/files (detected by pattern)
    - Very long text outputs
    - List/dict content that serializes to large strings
    - MCP content block lists (extract text, skip images)

    Args:
        content: The raw content from a ToolResultBlock
        max_size: Maximum allowed size in characters

    Returns:
        Truncated string with informative message if content was too large
    """
    if content is None:
        return ""

    # Handle content block lists: [{"type": "text", "text": "..."}, {"type": "image", ...}]
    # Supports both dict-based (MCP) and Pydantic model-based (SDK) content blocks.
    # Extract text content so downstream consumers get usable strings instead of Python repr
    if isinstance(content, list):
        text_parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif hasattr(block, "type") and hasattr(block, "text") and getattr(block, "type") == "text":
                # Pydantic TextBlock objects from the Anthropic SDK
                text_parts.append(getattr(block, "text", ""))
        if text_parts:
            content = "\n".join(text_parts)
        # else: fall through to str() for non-standard lists

    # Convert to string for size checking
    content_str = str(content) if not isinstance(content, str) else content
    content_len = len(content_str)

    # If it's small enough, return as-is (truncated for display)
    if content_len <= max_size:
        return content_str[:MAX_DISPLAY_OUTPUT_SIZE]

    # Check if it looks like base64 image data
    # Common patterns: data:image/..., very long alphanumeric strings, iVBOR (PNG), /9j/ (JPEG)
    is_likely_base64 = (
        'data:image/' in content_str[:100] or
        content_str[:10].startswith('iVBOR') or  # PNG base64 signature
        content_str[:10].startswith('/9j/') or   # JPEG base64 signature
        content_str[:10].startswith('R0lGOD') or # GIF base64 signature
        # Check for very high ratio of alphanumeric chars (typical of base64)
        (content_len > 10000 and sum(c.isalnum() or c in '+/=' for c in content_str[:1000]) > 950)
    )

    if is_likely_base64:
        # Calculate approximate original size
        original_size_kb = int(content_len * 0.75 / 1024)  # base64 is ~33% larger
        return f"[Image/binary content - {original_size_kb}KB - content processed by Claude but truncated for display]"

    # For other large content, truncate with indicator
    size_kb = content_len // 1024
    truncated = content_str[:MAX_DISPLAY_OUTPUT_SIZE]
    return f"{truncated}\n\n[... truncated - total size: {size_kb}KB]"


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
        # Build YAML frontmatter
        frontmatter_lines = [
            "---",
            f"name: {agent_id}",
            f"description: {agent_def.description}",
        ]

        # Add tools if specified
        if agent_def.tools:
            tools_str = ", ".join(agent_def.tools)
            frontmatter_lines.append(f"tools: {tools_str}")

        # Add model if specified
        if agent_def.model:
            frontmatter_lines.append(f"model: {agent_def.model}")

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
    browser_session_id: Optional[str] = None,
) -> Optional[dict]:
    """
    Build the mcp_servers dict for ClaudeAgentOptions from registered MCP servers.

    Reads MCP servers from two sources:
    1. Scope-based .mcp.json files (user, local, project) via McpService
    2. Installed+enabled plugin directories (each plugin can have its own .mcp.json)

    Args:
        project_dir: Optional project directory for project-scoped servers.
        browser_session_id: Optional session ID to inject into browser-tools env.

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
            # Skip browser-tools from .mcp.json — it's now added as an
            # in-process SDK MCP server below (no subprocess needed)
            if server.name == "browser-tools":
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

    # Add browser-tools as an in-process SDK MCP server (replaces the old
    # stdio subprocess).  The server proxies tool calls to the FastAPI
    # browser REST API so agent and user share the same Playwright browser.
    # Skip if user has globally disabled browser-tools via settings.json.
    if browser_session_id:
        disabled_servers = service.get_disabled_servers() if service else []
        if "browser-tools" not in disabled_servers:
            try:
                from app.mcp.browser_sdk_tools import create_browser_tools_server
                from app.core.config import INTERNAL_BROWSER_TOKEN

                browser_server = create_browser_tools_server(
                    base_url=f"http://127.0.0.1:{settings.port}",
                    session_id=browser_session_id,
                    auth_token=INTERNAL_BROWSER_TOKEN,
                )
                mcp_servers["browser-tools"] = browser_server
                logger.debug("Added in-process SDK MCP server for browser-tools")
            except Exception as e:
                logger.warning(f"Failed to create browser-tools SDK MCP server: {e}")
        else:
            logger.info("browser-tools is globally disabled, skipping")

    if mcp_servers:
        logger.info(f"Passing {len(mcp_servers)} MCP server(s) to SDK: {list(mcp_servers.keys())}")
        return mcp_servers

    return None


def _build_mcp_servers_dict_with_ai_tools(
    project_dir: Optional[Path] = None,
    browser_session_id: Optional[str] = None,
    ai_tools_config: Optional[Dict[str, Any]] = None,
    tool_env: Optional[Dict[str, str]] = None,
) -> Optional[dict]:
    """
    Build MCP servers dict including AI content generation tools.

    Wraps _build_mcp_servers_dict() and adds the AI tools SDK MCP server
    when any AI tools are enabled in the profile.

    Args:
        project_dir: Optional project directory for project-scoped servers.
        browser_session_id: Optional session ID to inject into browser-tools env.
        ai_tools_config: Optional dict with individual AI tool toggles.
        tool_env: Environment variables (API keys, provider settings) to inject
            into AI tool Node.js subprocesses.

    Returns:
        Dictionary mapping server names to SDK MCP server configs, or None if empty.
    """
    mcp_servers = _build_mcp_servers_dict(
        project_dir=project_dir,
        browser_session_id=browser_session_id,
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


def get_session_state(session_id: str) -> Optional[SessionState]:
    """Get the active session state for a session ID."""
    return get_session_pool().peek(session_id)


# Default instructions for displaying media and files in chat
# Can be customized via admin settings API
DEFAULT_SECURITY_INSTRUCTIONS = """
Chat and its Capabilities:
## Displaying Media and Files in Chat

The chat UI can render images, videos, and file download cards.

### Media Formats
- **Images:** `![Description](/api/generated-images/filename.png)`
- **Videos:** `[Description](/api/generated-videos/filename.mp4)`
- **Files:** `📎[filename.ext](/api/files/by-path?path=/full/path/to/file.ext)`

---

### Sharing Files for Download

To make any workspace file downloadable, use this markdown format:
```
📎[filename.ext](/api/files/by-path?path=/full/path/to/file.ext)
```

**Example:** If you created `/workspace/my-app/output/report.pdf`:
```
📎[report.pdf](/api/files/by-path?path=/workspace/my-app/output/report.pdf)
```

Provide download links when you create standalone files the user might want (documents, exports, scripts, etc.).

---

## Chat History References

When the user references a chat history file (`.jsonl` file from `~/.claude/projects/`):

1. **Purpose**: This is a previous conversation provided as context - NOT a file to edit
2. **Efficient reading**: Scan for relevant context rather than reading every message in full. The JSONL format contains one message per line with `type`, `message`, and `timestamp` fields
3. **Focus on**: Key decisions made, code discussed, problems solved, user preferences expressed
4. **Don't**: Read the entire file message-by-message unless specifically asked to summarize the whole conversation
5. **Summarize**: Tell the user what relevant context you found from the referenced conversation
6. **Format**: Messages alternate between `"type":"user"` and `"type":"assistant"`. Tool uses are embedded in assistant messages, tool results follow in subsequent user messages.

This helps the model understand past context without overwhelming the current conversation.
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

    # In Docker mode, enable CLI debug output to stderr for diagnosing
    # initialization hangs and other subprocess issues
    if deployment_mode and deployment_mode != DeploymentMode.LOCAL:
        if "debug-to-stderr" not in extra_args:
            extra_args["debug-to-stderr"] = None
            logger.debug("Added --debug-to-stderr for Docker CLI diagnostics")

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
        env["IMAGE_PROVIDER"] = "fal-ai"
        if image_model:
            env["IMAGE_MODEL"] = image_model
            logger.debug(f"Set default IMAGE_MODEL={image_model}")
        image_aspect_ratio = database.get_system_setting("image_aspect_ratio")
        image_resolution = database.get_system_setting("image_resolution")
        if image_aspect_ratio:
            env["IMAGE_ASPECT_RATIO"] = image_aspect_ratio
        if image_resolution:
            env["IMAGE_RESOLUTION"] = image_resolution

    if any_video_tools:
        video_model = database.get_system_setting("video_model")
        env["VIDEO_PROVIDER"] = "fal-ai"
        if video_model:
            env["VIDEO_MODEL"] = video_model
            logger.debug(f"Set default VIDEO_MODEL={video_model}")
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
        env["MODEL3D_PROVIDER"] = "fal-ai"
        if model3d_model:
            env["MODEL3D_MODEL"] = model3d_model
            logger.debug(f"Set default MODEL3D_MODEL={model3d_model}")

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
    today = datetime.now().strftime("%Y-%m-%d")

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
    browser_session_id: Optional[str] = _UNSET,
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
    enabled_agent_ids = config.get("enabled_agents", [])
    agents_dict = None
    logger.debug(f"Building agents from enabled_agent_ids: {enabled_agent_ids}")
    if enabled_agent_ids:
        agents_dict = {}
        for agent_id in enabled_agent_ids:
            # Look up subagent from global database
            subagent = database.get_subagent(agent_id)
            if subagent:
                # Create AgentDefinition dataclass instance
                agent_def = AgentDefinition(
                    description=subagent.get("description", ""),
                    prompt=subagent.get("prompt", ""),
                    tools=subagent.get("tools"),
                    model=subagent.get("model")
                )
                agents_dict[agent_id] = agent_def
                logger.debug(f"Loaded subagent '{agent_id}': description='{agent_def.description[:50]}...', tools={agent_def.tools}, model={agent_def.model}")
            else:
                logger.warning(f"Subagent not found in database: {agent_id}")
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
    def stderr_callback(line: str):
        logger.debug(f"[Claude CLI stderr] {line}")

    # Process model selection - handle special 1M context models
    # Models ending in "-1m" use the base model with 1M context beta header
    raw_model = overrides.get("model") or config.get("model")
    model_to_use = raw_model
    betas_to_use = []

    # Map special model names to base model + beta headers
    MODEL_1M_MAPPING = {
        "sonnet-1m": ("sonnet", ["context-1m-2025-08-07"]),
        "claude-sonnet-4-5-1m": ("claude-sonnet-4-5-20250514", ["context-1m-2025-08-07"]),
        "claude-sonnet-4-1m": ("claude-sonnet-4-20250514", ["context-1m-2025-08-07"]),
    }

    if raw_model in MODEL_1M_MAPPING:
        model_to_use, betas_to_use = MODEL_1M_MAPPING[raw_model]
        logger.debug(f"1M context model selected: {raw_model} -> model={model_to_use}, betas={betas_to_use}")

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
        # This ensures tools from MCP servers (e.g., browser-tools) are available
        # regardless of whether the CLI discovers them from .mcp.json files
        # Note: ai_tool_env is passed so Node.js tool subprocesses get API keys
        # browser_session_id defaults to session_id (WebSocket queries) but can
        # be set to None for connector/API queries that have no browser.
        mcp_servers=_build_mcp_servers_dict_with_ai_tools(
            project_dir=Path(working_dir) if working_dir else None,
            browser_session_id=session_id if browser_session_id is _UNSET else browser_session_id,
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


async def execute_query(
    prompt: str,
    profile_id: str,
    project_id: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
    session_id: Optional[str] = None,
    api_user_id: Optional[str] = None,
    browser_session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute a non-streaming query.

    Args:
        browser_session_id: Optional browser session ID for browser-tools MCP.
            If None, browser-tools is not included (appropriate for connector
            and API queries that have no interactive browser).
    """

    # Get profile
    profile = get_profile(profile_id)
    if not profile:
        raise ValueError(f"Profile not found: {profile_id}")

    # Get project if specified
    project = None
    if project_id:
        project = database.get_project(project_id)
        if not project:
            raise ValueError(f"Project not found: {project_id}")

    # Get or create session
    if session_id:
        session = database.get_session(session_id)
        if not session:
            raise ValueError(f"Session not found: {session_id}")
        resume_id = session.get("sdk_session_id")
    else:
        session_id = str(uuid.uuid4())
        # Generate title from first message (truncate to 50 chars)
        title = prompt[:50].strip()
        if len(prompt) > 50:
            title += "..."
        session = database.create_session(
            session_id=session_id,
            profile_id=profile_id,
            project_id=project_id,
            title=title,
            api_user_id=api_user_id
        )
        resume_id = None

        # Notify all connected frontends so the sidebar updates
        from app.core.sync_engine import sync_engine
        await sync_engine.broadcast_user_session_created(
            user_id="admin",
            session_id=session_id,
            source_device_id=None
        )

    # Store user message with profile_id for per-query tracking
    database.add_session_message(
        session_id=session_id,
        role="user",
        content=prompt,
        profile_id=profile_id
    )

    # Build options with user-scoped credential resolution
    # browser_session_id=None for connector/API queries (no interactive browser)
    options, agents_dict = build_options_from_profile(
        profile=profile,
        project=project,
        overrides=overrides,
        resume_session_id=resume_id,
        api_user_id=api_user_id,
        session_id=session_id,
        browser_session_id=browser_session_id,
    )

    # On Windows local mode, write agents to filesystem (workaround for --agents CLI bug)
    # On Docker/Linux, agents are already passed via SDK's --agents flag
    # agents_dir created for local mode - used implicitly by SDK reading from filesystem
    _agents_dir = None
    if agents_dict and options.cwd and detect_deployment_mode() == DeploymentMode.LOCAL:
        _agents_dir = write_agents_to_filesystem(agents_dict, options.cwd)

    # Resolve virtual @Notes/note-id references to actual content
    prompt = resolve_note_references(prompt)

    # Execute query and collect response
    response_text = []
    tool_messages = []  # Collect tool use/result messages for storage
    metadata = {}
    sdk_session_id = None

    # Wrap string prompt as async iterable to force SDK to use stream_input() path.
    # The SDK's string-prompt path calls end_input() immediately after writing,
    # closing stdin before the CLI finishes processing. If the CLI sends a control
    # request (e.g., mcp_tool_request for SDK MCP servers), the response write fails
    # with "ProcessTransport is not ready for writing". The stream_input() path
    # properly waits for the result before closing stdin.
    async def _prompt_stream():
        yield {
            "type": "user",
            "session_id": "",
            "message": {"role": "user", "content": prompt},
            "parent_tool_use_id": None,
        }

    try:
        async for message in query(prompt=_prompt_stream(), options=options):
            if isinstance(message, SystemMessage):
                # session_id comes in warmup message data after first query
                if message.subtype == "init" and "session_id" in message.data:
                    sdk_session_id = message.data["session_id"]

            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        response_text.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        tool_messages.append({
                            "type": "tool_use",
                            "name": block.name,
                            "tool_id": getattr(block, 'id', None),
                            "input": block.input
                        })
                    elif isinstance(block, ToolResultBlock):
                        output = truncate_large_payload(block.content)
                        tool_messages.append({
                            "type": "tool_result",
                            "name": getattr(block, 'name', 'unknown'),
                            "tool_id": getattr(block, 'tool_use_id', None),
                            "output": output
                        })
                metadata["model"] = message.model

            elif isinstance(message, UserMessage):
                # UserMessage contains tool results from Claude's tool executions
                for block in message.content:
                    if isinstance(block, ToolResultBlock):
                        output = truncate_large_payload(block.content) if block.content else ""
                        tool_messages.append({
                            "type": "tool_result",
                            "name": "unknown",
                            "tool_id": block.tool_use_id,
                            "output": output
                        })

            elif isinstance(message, ResultMessage):
                metadata["duration_ms"] = message.duration_ms
                metadata["num_turns"] = message.num_turns
                metadata["total_cost_usd"] = message.total_cost_usd
                metadata["is_error"] = message.is_error
                # Extract token counts from usage dict
                if message.usage:
                    metadata["tokens_in"] = message.usage.get("input_tokens", 0)
                    metadata["tokens_out"] = message.usage.get("output_tokens", 0)
                    metadata["cache_creation_tokens"] = message.usage.get("cache_creation_input_tokens", 0)
                    metadata["cache_read_tokens"] = message.usage.get("cache_read_input_tokens", 0)

    except (Exception, BaseExceptionGroup) as e:
        # Unwrap ExceptionGroup from SDK's anyio.TaskGroup (Python 3.11+)
        if isinstance(e, BaseExceptionGroup) and e.exceptions:
            actual = e.exceptions[0]
            logger.error(f"Query execution error (unwrapped from ExceptionGroup): {actual}", exc_info=actual)
            raise actual from e
        logger.error(f"Query execution error: {e}")
        raise

    # Update session with SDK session ID for resume
    if sdk_session_id:
        database.update_session(
            session_id=session_id,
            sdk_session_id=sdk_session_id,
            cost_increment=metadata.get("total_cost_usd", 0),
            tokens_in_increment=metadata.get("tokens_in", 0),
            tokens_out_increment=metadata.get("tokens_out", 0),
            turn_increment=metadata.get("num_turns", 0)
        )

    # Store tool messages (tool_use and tool_result)
    for tool_msg in tool_messages:
        if tool_msg["type"] == "tool_use":
            database.add_session_message(
                session_id=session_id,
                role="tool_use",
                content=f"Using tool: {tool_msg['name']}",
                tool_name=tool_msg["name"],
                tool_input=tool_msg.get("input"),
                metadata={"tool_id": tool_msg.get("tool_id")},
                profile_id=profile_id
            )
        elif tool_msg["type"] == "tool_result":
            database.add_session_message(
                session_id=session_id,
                role="tool_result",
                content=tool_msg.get("output", ""),
                tool_name=tool_msg["name"],
                metadata={"tool_id": tool_msg.get("tool_id")},
                profile_id=profile_id
            )

    # Store assistant response
    full_response = "\n".join(response_text)
    database.add_session_message(
        session_id=session_id,
        role="assistant",
        content=full_response,
        metadata=metadata,
        profile_id=profile_id
    )

    # Log usage
    database.log_usage(
        session_id=session_id,
        profile_id=profile_id,
        model=metadata.get("model"),
        tokens_in=metadata.get("tokens_in", 0),
        tokens_out=metadata.get("tokens_out", 0),
        cost_usd=metadata.get("total_cost_usd", 0),
        duration_ms=metadata.get("duration_ms", 0)
    )

    return {
        "response": full_response,
        "session_id": session_id,
        "metadata": metadata
    }


async def stream_query(
    prompt: str,
    profile_id: str,
    project_id: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
    session_id: Optional[str] = None,
    api_user_id: Optional[str] = None,
    device_id: Optional[str] = None  # Source device for sync
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    Execute a streaming query using ClaudeSDKClient.

    Following Anvil's SessionManager pattern:
    - Keep clients connected for session lifetime
    - Don't disconnect after each query (causes async context issues)
    - Reuse existing client if available, create new one with 'resume' if needed
    """

    # Get profile
    profile = get_profile(profile_id)
    if not profile:
        yield {"type": "error", "message": f"Profile not found: {profile_id}"}
        return

    # Get project if specified
    project = None
    if project_id:
        project = database.get_project(project_id)
        if not project:
            yield {"type": "error", "message": f"Project not found: {project_id}"}
            return

    # Get or create session in database
    _is_new_session = False  # Tracks new vs resumed for potential analytics
    resume_id = None

    if session_id:
        session = database.get_session(session_id)
        if not session:
            yield {"type": "error", "message": f"Session not found: {session_id}"}
            return
        resume_id = session.get("sdk_session_id")
        logger.debug(f"Resuming session {session_id} with SDK session {resume_id}")
    else:
        session_id = str(uuid.uuid4())
        # Generate title from first message (truncate to 50 chars)
        title = prompt[:50].strip()
        if len(prompt) > 50:
            title += "..."
        session = database.create_session(
            session_id=session_id,
            profile_id=profile_id,
            project_id=project_id,
            title=title,
            api_user_id=api_user_id
        )
        _is_new_session = True  # Flag available for future analytics/logging
        logger.debug(f"Created new session {session_id} with title: {title}")

    # Store user message and broadcast to other devices
    user_msg = database.add_session_message(
        session_id=session_id,
        role="user",
        content=prompt,
        profile_id=profile_id
    )

    # Broadcast user message to other devices (include profile_id for display)
    await sync_engine.broadcast_message_added(
        session_id=session_id,
        message={**user_msg, "profile_id": profile_id},
        source_device_id=device_id
    )

    # Log to sync log for polling fallback
    database.add_sync_log(
        session_id=session_id,
        event_type="message_added",
        entity_type="message",
        entity_id=str(user_msg["id"]),
        data=user_msg
    )

    # Build options with user-scoped credential resolution
    options, agents_dict = build_options_from_profile(
        profile=profile,
        project=project,
        overrides=overrides,
        resume_session_id=resume_id,
        api_user_id=api_user_id,
        session_id=session_id
    )

    # On Windows local mode, write agents to filesystem (workaround for --agents CLI bug)
    # On Docker/Linux, agents are already passed via SDK's --agents flag
    agents_dir = None
    written_agent_ids = []
    if agents_dict and options.cwd and detect_deployment_mode() == DeploymentMode.LOCAL:
        agents_dir = write_agents_to_filesystem(agents_dict, options.cwd)
        written_agent_ids = list(agents_dict.keys())

    # Yield init event
    yield {"type": "init", "session_id": session_id}

    # --- Session Pool: Acquire or create SDK client ---
    pool = get_session_pool()
    existing_state = pool.get(session_id)
    reusing_client = False

    if existing_state and existing_state.is_connected and is_subprocess_alive(existing_state):
        if not requires_reconnect(existing_state, options):
            state = existing_state
            reusing_client = True
            logger.info(f"Reusing pooled client for session {session_id}")
        else:
            logger.info(f"Options changed for session {session_id}, creating fresh client")
            await pool.remove(session_id)
            existing_state = None
    elif existing_state:
        logger.warning(f"Subprocess dead for pooled session {session_id}, creating fresh client")
        await pool.remove_dead(session_id)
        existing_state = None

    if not reusing_client:
        # Evict LRU if pool is full
        await pool.evict_if_needed()

        logger.debug(f"Creating new ClaudeSDKClient for session {session_id} (resume={resume_id is not None})")

        # Wrap stderr callback to collect lines for error reporting
        cli_stderr_lines: list[str] = []
        original_stderr = options.stderr
        def capturing_stderr(line: str):
            cli_stderr_lines.append(line)
            if original_stderr:
                original_stderr(line)
        options.stderr = capturing_stderr

        client = ClaudeSDKClient(options=options)

        try:
            await client.connect()
            logger.debug(f"Connected to Claude SDK for session {session_id}")
        except Exception as e:
            import traceback
            logger.error(f"Failed to connect to Claude SDK for session {session_id}: {e}")
            logger.error(f"Exception type: {type(e).__name__}")
            logger.error(f"Full traceback:\n{traceback.format_exc()}")
            if cli_stderr_lines:
                stderr_msg = " ".join(line.strip() for line in cli_stderr_lines if line.strip())
                logger.error(f"CLI stderr: {stderr_msg}")
                yield {"type": "error", "message": stderr_msg}
            else:
                yield {"type": "error", "message": f"Connection failed: {e}"}
            return

        state = SessionState(
            client=client,
            sdk_session_id=resume_id,
            is_connected=True,
            written_agent_ids=written_agent_ids,
            agents_dir=agents_dir,
            connect_options=options,
        )
        pool.put(session_id, state)

    # Mark as streaming
    state.is_streaming = True
    state.last_activity = datetime.now()

    # Generate a message ID for streaming sync
    assistant_msg_id = f"streaming-{session_id}-{datetime.now().timestamp()}"

    # Broadcast stream start to other devices
    await sync_engine.broadcast_stream_start(
        session_id=session_id,
        message_id=assistant_msg_id,
        source_device_id=device_id,
        user_id=api_user_id
    )

    # Log stream start for polling fallback
    database.add_sync_log(
        session_id=session_id,
        event_type="stream_start",
        entity_type="message",
        entity_id=assistant_msg_id,
        data={"message_id": assistant_msg_id}
    )

    # Execute query
    response_text = []
    tool_messages = []  # Collect tool use/result messages for storage
    tool_id_to_name = {}  # Track tool_use_id → tool_name for ToolResultBlock lookup
    metadata = {}
    sdk_session_id = resume_id  # Start with existing SDK session ID if resuming
    interrupted = False

    try:
        await state.client.query(prompt)

        async for message in state.client.receive_response():
            if isinstance(message, SystemMessage):
                # session_id comes in init message data after first query
                if message.subtype == "init" and "session_id" in message.data:
                    sdk_session_id = message.data["session_id"]
                    state.sdk_session_id = sdk_session_id
                    logger.debug(f"Captured SDK session ID: {sdk_session_id}")

            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        response_text.append(block.text)
                        yield {"type": "text", "content": block.text}

                        # Broadcast text chunk to other devices
                        await sync_engine.broadcast_stream_chunk(
                            session_id=session_id,
                            message_id=assistant_msg_id,
                            chunk_type="text",
                            chunk_data={"content": block.text},
                            source_device_id=device_id
                        )

                    elif isinstance(block, ToolUseBlock):
                        tool_id = getattr(block, 'id', None)

                        # Track tool_id → name for ToolResultBlock lookup
                        if tool_id:
                            tool_id_to_name[tool_id] = block.name

                        yield {
                            "type": "tool_use",
                            "name": block.name,
                            "input": block.input
                        }

                        # Collect tool use for storage
                        tool_messages.append({
                            "type": "tool_use",
                            "name": block.name,
                            "tool_id": tool_id,
                            "input": block.input
                        })

                        # Broadcast tool use to other devices
                        await sync_engine.broadcast_stream_chunk(
                            session_id=session_id,
                            message_id=assistant_msg_id,
                            chunk_type="tool_use",
                            chunk_data={"name": block.name, "input": block.input},
                            source_device_id=device_id
                        )

                    elif isinstance(block, ToolResultBlock):
                        # Truncate large outputs
                        output = truncate_large_payload(block.content)
                        tool_use_id = getattr(block, 'tool_use_id', None)
                        tool_name = tool_id_to_name.get(tool_use_id, getattr(block, 'name', 'unknown'))
                        yield {
                            "type": "tool_result",
                            "name": tool_name,
                            "output": output
                        }

                        # Collect tool result for storage
                        tool_messages.append({
                            "type": "tool_result",
                            "name": tool_name,
                            "tool_id": tool_use_id,
                            "output": output
                        })

                        # Broadcast tool result to other devices
                        await sync_engine.broadcast_stream_chunk(
                            session_id=session_id,
                            message_id=assistant_msg_id,
                            chunk_type="tool_result",
                            chunk_data={"name": tool_name, "output": output},
                            source_device_id=device_id
                        )

                metadata["model"] = message.model

            elif isinstance(message, UserMessage):
                # UserMessage contains tool results from Claude's tool executions
                for block in message.content:
                    if isinstance(block, ToolResultBlock):
                        output = truncate_large_payload(block.content) if block.content else ""
                        tool_name = tool_id_to_name.get(block.tool_use_id, "unknown")
                        logger.debug(f"UserMessage ToolResultBlock - tool_use_id: {block.tool_use_id}, tool_name: {tool_name}, content length: {len(str(block.content) if block.content else '')}")

                        yield {
                            "type": "tool_result",
                            "name": tool_name,
                            "tool_use_id": block.tool_use_id,
                            "output": output
                        }

                        # Collect tool result for storage
                        tool_messages.append({
                            "type": "tool_result",
                            "name": tool_name,
                            "tool_id": block.tool_use_id,
                            "output": output
                        })

                        # Broadcast tool result to other devices
                        await sync_engine.broadcast_stream_chunk(
                            session_id=session_id,
                            message_id=assistant_msg_id,
                            chunk_type="tool_result",
                            chunk_data={"name": tool_name, "output": output, "tool_use_id": block.tool_use_id},
                            source_device_id=device_id
                        )

            elif isinstance(message, ResultMessage):
                metadata["duration_ms"] = message.duration_ms
                metadata["num_turns"] = message.num_turns
                metadata["total_cost_usd"] = message.total_cost_usd
                metadata["is_error"] = message.is_error
                # Extract token counts from usage dict
                if message.usage:
                    metadata["tokens_in"] = message.usage.get("input_tokens", 0)
                    metadata["tokens_out"] = message.usage.get("output_tokens", 0)
                    metadata["cache_creation_tokens"] = message.usage.get("cache_creation_input_tokens", 0)
                    metadata["cache_read_tokens"] = message.usage.get("cache_read_input_tokens", 0)

    except asyncio.CancelledError:
        interrupted = True
        logger.debug(f"Query interrupted for session {session_id}")
        yield {"type": "interrupted", "message": "Query was interrupted"}

    except (Exception, BaseExceptionGroup) as e:
        # Unwrap ExceptionGroup from SDK's anyio.TaskGroup (Python 3.11+)
        actual = e
        if isinstance(e, BaseExceptionGroup) and e.exceptions:
            actual = e.exceptions[0]
            logger.error(f"Stream query error (unwrapped from ExceptionGroup) for session {session_id}: {actual}", exc_info=actual)
        else:
            logger.error(f"Stream query error for session {session_id}: {e}")
        # Mark client as disconnected on error
        state.is_connected = False
        yield {"type": "error", "message": str(actual)}

    finally:
        # Mark as not streaming — keep in pool for reuse
        state.is_streaming = False
        state.last_activity = datetime.now()

        # Remove dead clients from pool
        if not state.is_connected:
            await pool.remove_dead(session_id)

        # Broadcast stream end to other devices
        await sync_engine.broadcast_stream_end(
            session_id=session_id,
            message_id=assistant_msg_id,
            metadata=metadata,
            interrupted=interrupted,
            source_device_id=device_id
        )

    # Update session in database
    if sdk_session_id:
        database.update_session(
            session_id=session_id,
            sdk_session_id=sdk_session_id,
            cost_increment=metadata.get("total_cost_usd", 0),
            tokens_in_increment=metadata.get("tokens_in", 0),
            tokens_out_increment=metadata.get("tokens_out", 0),
            turn_increment=metadata.get("num_turns", 0)
        )
        logger.debug(f"Updated session {session_id}, sdk_session_id={sdk_session_id}")

    # Store tool messages (tool_use and tool_result)
    for tool_msg in tool_messages:
        if tool_msg["type"] == "tool_use":
            database.add_session_message(
                session_id=session_id,
                role="tool_use",
                content=f"Using tool: {tool_msg['name']}",
                tool_name=tool_msg["name"],
                tool_input=tool_msg.get("input"),
                metadata={"tool_id": tool_msg.get("tool_id")},
                profile_id=profile_id
            )
        elif tool_msg["type"] == "tool_result":
            database.add_session_message(
                session_id=session_id,
                role="tool_result",
                content=tool_msg.get("output", ""),
                tool_name=tool_msg["name"],
                metadata={"tool_id": tool_msg.get("tool_id")},
                profile_id=profile_id
            )

    # Store assistant response
    full_response = "\n".join(response_text)
    if full_response or interrupted or tool_messages:
        assistant_msg = database.add_session_message(
            session_id=session_id,
            role="assistant",
            content=full_response + ("\n[Interrupted]" if interrupted else ""),
            metadata=metadata,
            profile_id=profile_id
        )

        # Log stream end for polling fallback
        database.add_sync_log(
            session_id=session_id,
            event_type="stream_end",
            entity_type="message",
            entity_id=str(assistant_msg["id"]),
            data={
                "message": assistant_msg,
                "metadata": metadata,
                "interrupted": interrupted
            }
        )

    # Log usage
    if metadata:
        database.log_usage(
            session_id=session_id,
            profile_id=profile_id,
            model=metadata.get("model"),
            tokens_in=metadata.get("tokens_in", 0),
            tokens_out=metadata.get("tokens_out", 0),
            cost_usd=metadata.get("total_cost_usd", 0),
            duration_ms=metadata.get("duration_ms", 0)
        )

    # Yield done event (unless already yielded error/interrupted)
    if not interrupted:
        yield {
            "type": "done",
            "session_id": session_id,
            "metadata": metadata
        }


async def _run_background_query(
    session_id: str,
    prompt: str,
    profile: Dict[str, Any],
    project: Optional[Dict[str, Any]],
    overrides: Optional[Dict[str, Any]],
    resume_id: Optional[str],
    device_id: Optional[str],
    api_user_id: Optional[str],
    message_id: Optional[str] = None
):
    """
    Run a streaming query in the background, independent of HTTP connection.

    This allows work to continue even when:
    - User locks their phone
    - Browser tab is backgrounded
    - HTTP connection is interrupted

    All events are broadcast via sync_engine for WebSocket delivery.
    """
    # Extract profile_id for per-query tracking
    profile_id = profile.get("id")

    # Store user message and broadcast to other devices
    user_msg = database.add_session_message(
        session_id=session_id,
        role="user",
        content=prompt,
        profile_id=profile_id
    )

    # Broadcast user message to other devices (include profile_id for display)
    await sync_engine.broadcast_message_added(
        session_id=session_id,
        message={**user_msg, "profile_id": profile_id},
        source_device_id=device_id
    )

    # Log to sync log for polling fallback
    database.add_sync_log(
        session_id=session_id,
        event_type="message_added",
        entity_type="message",
        entity_id=str(user_msg["id"]),
        data=user_msg
    )

    # Build options with user-scoped credential resolution
    options, agents_dict = build_options_from_profile(
        profile=profile,
        project=project,
        overrides=overrides,
        resume_session_id=resume_id,
        api_user_id=api_user_id,
        session_id=session_id
    )

    # On Windows local mode, write agents to filesystem (workaround for --agents CLI bug)
    # On Docker/Linux, agents are already passed via SDK's --agents flag
    agents_dir = None
    written_agent_ids = []
    if agents_dict and options.cwd and detect_deployment_mode() == DeploymentMode.LOCAL:
        agents_dir = write_agents_to_filesystem(agents_dict, options.cwd)
        written_agent_ids = list(agents_dict.keys())

    # --- Session Pool: Acquire or create SDK client ---
    pool = get_session_pool()
    existing_state = pool.get(session_id)
    reusing_client = False

    if existing_state and existing_state.is_connected and is_subprocess_alive(existing_state):
        if not requires_reconnect(existing_state, options):
            state = existing_state
            reusing_client = True
            logger.info(f"[Background] Reusing pooled client for session {session_id}")
        else:
            logger.info(f"[Background] Options changed for session {session_id}, creating fresh client")
            await pool.remove(session_id)
            existing_state = None
    elif existing_state:
        logger.warning(f"[Background] Subprocess dead for pooled session {session_id}, creating fresh client")
        await pool.remove_dead(session_id)
        existing_state = None

    if not reusing_client:
        # Evict LRU if pool is full
        await pool.evict_if_needed()

        logger.debug(f"[Background] Creating new ClaudeSDKClient for session {session_id} (resume={resume_id is not None})")

        # Wrap stderr callback to collect lines for error reporting
        cli_stderr_lines: list[str] = []
        original_stderr = options.stderr
        def capturing_stderr(line: str):
            cli_stderr_lines.append(line)
            if original_stderr:
                original_stderr(line)
        options.stderr = capturing_stderr

        client = ClaudeSDKClient(options=options)

        try:
            await client.connect()
            logger.debug(f"[Background] Connected to Claude SDK for session {session_id}")
        except Exception as e:
            error_msg = str(e)
            if cli_stderr_lines:
                error_msg = " ".join(line.strip() for line in cli_stderr_lines if line.strip())
                logger.error(f"[Background] CLI stderr: {error_msg}")
            logger.error(f"[Background] Failed to connect to Claude SDK for session {session_id}: {error_msg}")
            # Clean up LiveSessionStore on connect failure
            try:
                await live_session_store.cleanup_session(session_id)
            except Exception:
                pass
            await sync_engine.broadcast_stream_end(
                session_id=session_id,
                message_id=f"error-{session_id}",
                metadata={"error": error_msg},
                interrupted=True,
                source_device_id=device_id
            )
            return

        state = SessionState(
            client=client,
            sdk_session_id=resume_id,
            is_connected=True,
            written_agent_ids=written_agent_ids,
            agents_dir=agents_dir,
            connect_options=options,
        )
        pool.put(session_id, state)

    # Mark as streaming
    state.is_streaming = True
    state.last_activity = datetime.now()

    # Use provided message_id or generate a new one
    # If message_id is provided, stream_start was already broadcast by start_background_query
    assistant_msg_id = message_id or f"streaming-{session_id}-{datetime.now().timestamp()}"

    # Only broadcast stream_start if we generated a new message_id
    # (i.e., if message_id wasn't provided by start_background_query)
    if not message_id:
        await sync_engine.broadcast_stream_start(
            session_id=session_id,
            message_id=assistant_msg_id,
            source_device_id=None,  # Don't exclude any device - all should see it
            user_id=api_user_id
        )

    # Log stream start for polling fallback
    database.add_sync_log(
        session_id=session_id,
        event_type="stream_start",
        entity_type="message",
        entity_id=assistant_msg_id,
        data={"message_id": assistant_msg_id}
    )

    # Execute query
    response_text = []
    tool_messages = []  # Collect tool use/result messages for storage
    metadata = {}
    sdk_session_id = resume_id
    interrupted = False

    try:
        await state.client.query(prompt)

        async for message in state.client.receive_response():
            if isinstance(message, SystemMessage):
                if message.subtype == "init" and "session_id" in message.data:
                    sdk_session_id = message.data["session_id"]
                    state.sdk_session_id = sdk_session_id
                    logger.debug(f"[Background] Captured SDK session ID: {sdk_session_id}")

            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        response_text.append(block.text)

                        # Update LiveSessionStore for late-joining devices
                        try:
                            await live_session_store.append_text(session_id, block.text)
                        except Exception as e:
                            logger.warning(f"[Background] Failed to append text to live store for {session_id}: {e}")

                        # Broadcast text chunk to all devices
                        await sync_engine.broadcast_stream_chunk(
                            session_id=session_id,
                            message_id=assistant_msg_id,
                            chunk_type="text",
                            chunk_data={"content": block.text},
                            source_device_id=None
                        )

                    elif isinstance(block, ToolUseBlock):
                        # Collect tool use for storage
                        tool_messages.append({
                            "type": "tool_use",
                            "name": block.name,
                            "tool_id": getattr(block, 'id', None),
                            "input": block.input
                        })

                        # Update LiveSessionStore for late-joining devices
                        try:
                            tool_id = getattr(block, 'id', None) or f"tool-{uuid.uuid4().hex[:12]}"
                            tool_input = block.input if isinstance(block.input, dict) else {}
                            await live_session_store.add_tool_use(session_id, tool_id, block.name, tool_input)
                        except Exception as e:
                            logger.warning(f"[Background] Failed to add tool_use to live store for {session_id}: {e}")

                        await sync_engine.broadcast_stream_chunk(
                            session_id=session_id,
                            message_id=assistant_msg_id,
                            chunk_type="tool_use",
                            chunk_data={
                                "name": block.name,
                                "id": getattr(block, 'id', None),
                                "input": block.input
                            },
                            source_device_id=None
                        )

                    elif isinstance(block, ToolResultBlock):
                        output = truncate_large_payload(block.content)

                        # Collect tool result for storage
                        tool_messages.append({
                            "type": "tool_result",
                            "name": getattr(block, 'name', 'unknown'),
                            "tool_id": getattr(block, 'tool_use_id', None),
                            "output": output
                        })

                        # Update LiveSessionStore for late-joining devices
                        try:
                            result_tool_id = getattr(block, 'tool_use_id', None) or ""
                            output_text = str(output) if output else ""
                            await live_session_store.add_tool_result(
                                session_id, result_tool_id, output_text,
                                is_error=getattr(block, 'is_error', False)
                            )
                        except Exception as e:
                            logger.warning(f"[Background] Failed to add tool_result to live store for {session_id}: {e}")

                        await sync_engine.broadcast_stream_chunk(
                            session_id=session_id,
                            message_id=assistant_msg_id,
                            chunk_type="tool_result",
                            chunk_data={
                                "name": getattr(block, 'name', 'unknown'),
                                "tool_use_id": getattr(block, 'tool_use_id', None),
                                "output": output
                            },
                            source_device_id=None
                        )

                metadata["model"] = message.model

            elif isinstance(message, UserMessage):
                # UserMessage contains tool results from Claude's tool executions
                for block in message.content:
                    if isinstance(block, ToolResultBlock):
                        output = truncate_large_payload(block.content) if block.content else ""
                        logger.debug(f"[Background] UserMessage ToolResultBlock - tool_use_id: {block.tool_use_id}, content length: {len(str(block.content) if block.content else '')}")

                        # Collect tool result for storage
                        tool_messages.append({
                            "type": "tool_result",
                            "name": "unknown",
                            "tool_id": block.tool_use_id,
                            "output": output
                        })

                        # Update LiveSessionStore for late-joining devices
                        try:
                            output_text = str(output) if output else ""
                            await live_session_store.add_tool_result(
                                session_id, block.tool_use_id or "", output_text,
                                is_error=getattr(block, 'is_error', False)
                            )
                        except Exception as e:
                            logger.warning(f"[Background] Failed to add UserMessage tool_result to live store for {session_id}: {e}")

                        await sync_engine.broadcast_stream_chunk(
                            session_id=session_id,
                            message_id=assistant_msg_id,
                            chunk_type="tool_result",
                            chunk_data={
                                "name": "unknown",
                                "tool_use_id": block.tool_use_id,
                                "output": output
                            },
                            source_device_id=None
                        )

            elif isinstance(message, ResultMessage):
                metadata["duration_ms"] = message.duration_ms
                metadata["num_turns"] = message.num_turns
                metadata["total_cost_usd"] = message.total_cost_usd
                metadata["is_error"] = message.is_error
                # Extract token counts from usage dict
                if message.usage:
                    metadata["tokens_in"] = message.usage.get("input_tokens", 0)
                    metadata["tokens_out"] = message.usage.get("output_tokens", 0)
                    metadata["cache_creation_tokens"] = message.usage.get("cache_creation_input_tokens", 0)
                    metadata["cache_read_tokens"] = message.usage.get("cache_read_input_tokens", 0)

                    # Update LiveSessionStore usage for late-joining devices
                    try:
                        await live_session_store.update_usage(
                            session_id,
                            input_tokens=message.usage.get("input_tokens", 0),
                            output_tokens=message.usage.get("output_tokens", 0),
                            cache_creation_tokens=message.usage.get("cache_creation_input_tokens", 0),
                            cache_read_tokens=message.usage.get("cache_read_input_tokens", 0),
                        )
                    except Exception as e:
                        logger.warning(f"[Background] Failed to update usage in live store for {session_id}: {e}")

    except asyncio.CancelledError:
        interrupted = True
        logger.debug(f"[Background] Query interrupted for session {session_id}")

    except Exception as e:
        logger.error(f"[Background] Stream query error for session {session_id}: {e}")
        state.is_connected = False
        metadata["error"] = str(e)

    finally:
        # Mark as not streaming — keep in pool for reuse
        state.is_streaming = False
        state.last_activity = datetime.now()
        state.background_task = None

        # Remove dead clients from pool
        if not state.is_connected:
            await pool.remove_dead(session_id)

        # End LiveSessionStore streaming BEFORE broadcast_stream_end
        try:
            await live_session_store.end_streaming(session_id, metadata=metadata, interrupted=interrupted)
        except Exception as e:
            logger.warning(f"[Background] Failed to end live session store streaming for {session_id}: {e}")

        # Broadcast stream end to all devices
        await sync_engine.broadcast_stream_end(
            session_id=session_id,
            message_id=assistant_msg_id,
            metadata=metadata,
            interrupted=interrupted,
            source_device_id=None
        )

        # Schedule delayed cleanup of live store data (keep for 30s for late joiners)
        async def _delayed_cleanup():
            await asyncio.sleep(30)
            await live_session_store.cleanup_ended_session(session_id)

        asyncio.create_task(_delayed_cleanup())

    # Update session in database
    if sdk_session_id:
        database.update_session(
            session_id=session_id,
            sdk_session_id=sdk_session_id,
            cost_increment=metadata.get("total_cost_usd", 0),
            tokens_in_increment=metadata.get("tokens_in", 0),
            tokens_out_increment=metadata.get("tokens_out", 0),
            turn_increment=metadata.get("num_turns", 0)
        )
        logger.debug(f"[Background] Updated session {session_id}, sdk_session_id={sdk_session_id}")

    # Store tool messages (tool_use and tool_result)
    for tool_msg in tool_messages:
        if tool_msg["type"] == "tool_use":
            database.add_session_message(
                session_id=session_id,
                role="tool_use",
                content=f"Using tool: {tool_msg['name']}",
                tool_name=tool_msg["name"],
                tool_input=tool_msg.get("input"),
                metadata={"tool_id": tool_msg.get("tool_id")},
                profile_id=profile_id
            )
        elif tool_msg["type"] == "tool_result":
            database.add_session_message(
                session_id=session_id,
                role="tool_result",
                content=tool_msg.get("output", ""),
                tool_name=tool_msg["name"],
                metadata={"tool_id": tool_msg.get("tool_id")},
                profile_id=profile_id
            )

    # Store assistant response
    full_response = "\n".join(response_text)
    if full_response or interrupted or tool_messages:
        assistant_msg = database.add_session_message(
            session_id=session_id,
            role="assistant",
            content=full_response + ("\n[Interrupted]" if interrupted else ""),
            metadata=metadata,
            profile_id=profile_id
        )

        # Log stream end for polling fallback
        database.add_sync_log(
            session_id=session_id,
            event_type="stream_end",
            entity_type="message",
            entity_id=str(assistant_msg["id"]),
            data={
                "message": assistant_msg,
                "metadata": metadata,
                "interrupted": interrupted
            }
        )

    # Log usage
    if metadata and not metadata.get("error"):
        database.log_usage(
            session_id=session_id,
            profile_id=profile["id"],
            model=metadata.get("model"),
            tokens_in=metadata.get("tokens_in", 0),
            tokens_out=metadata.get("tokens_out", 0),
            cost_usd=metadata.get("total_cost_usd", 0),
            duration_ms=metadata.get("duration_ms", 0)
        )

    logger.debug(f"[Background] Query completed for session {session_id}")


async def start_background_query(
    prompt: str,
    profile_id: str,
    project_id: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
    session_id: Optional[str] = None,
    api_user_id: Optional[str] = None,
    device_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Start a streaming query in a background task.

    Returns immediately with session info. Work continues in background.
    Use interrupt_session() to stop.
    """
    # Get profile
    profile = get_profile(profile_id)
    if not profile:
        raise ValueError(f"Profile not found: {profile_id}")

    # Get project if specified
    project = None
    if project_id:
        project = database.get_project(project_id)
        if not project:
            raise ValueError(f"Project not found: {project_id}")

    # Get or create session in database
    resume_id = None

    if session_id:
        session = database.get_session(session_id)
        if not session:
            raise ValueError(f"Session not found: {session_id}")
        resume_id = session.get("sdk_session_id")
        logger.debug(f"Resuming session {session_id} with SDK session {resume_id}")
    else:
        session_id = str(uuid.uuid4())
        title = prompt[:50].strip()
        if len(prompt) > 50:
            title += "..."
        session = database.create_session(
            session_id=session_id,
            profile_id=profile_id,
            project_id=project_id,
            title=title,
            api_user_id=api_user_id
        )
        logger.debug(f"Created new session {session_id} with title: {title}")

    # Generate a message ID for streaming
    assistant_msg_id = f"streaming-{session_id}-{datetime.now().timestamp()}"

    # Broadcast stream_start BEFORE returning so the WebSocket can pick it up
    # This ensures is_streaming=true and buffer exists when client connects
    await sync_engine.broadcast_stream_start(
        session_id=session_id,
        message_id=assistant_msg_id,
        source_device_id=None,  # Don't exclude any device - all should see it
        user_id=api_user_id
    )
    logger.debug(f"[Background] Broadcast stream_start for session {session_id}")

    # Initialize LiveSessionStore for late-joining devices
    try:
        working_dir = resolve_working_dir(session)
        sdk_sid = session.get("sdk_session_id") if session else None
        initial_messages = message_resolver.resolve_for_streaming_init(session_id, sdk_sid, working_dir)
    except Exception:
        initial_messages = []

    await live_session_store.start_streaming(session_id, initial_messages)

    # Add user message to live store
    user_msg_id = f"user-{int(datetime.now().timestamp() * 1000)}"
    await live_session_store.add_user_message(session_id, prompt, user_msg_id)

    # Start background task
    task = asyncio.create_task(
        _run_background_query(
            session_id=session_id,
            prompt=prompt,
            profile=profile,
            project=project,
            overrides=overrides,
            resume_id=resume_id,
            device_id=device_id,
            api_user_id=api_user_id,
            message_id=assistant_msg_id
        )
    )

    # Store task reference for interrupt support
    # Note: state may not exist yet, it will be created in the background task
    # We'll store the task reference after the state is created
    # For now, we use a temporary holder
    asyncio.get_event_loop().call_soon(
        lambda: _store_background_task(session_id, task)
    )

    return {
        "session_id": session_id,
        "status": "started",
        "message_id": assistant_msg_id
    }


def _store_background_task(session_id: str, task: asyncio.Task):
    """Store background task reference after state is created"""
    state = get_session_pool().peek(session_id)
    if state:
        state.background_task = task


async def interrupt_session(session_id: str) -> bool:
    """Interrupt an active streaming session.

    This calls the SDK's interrupt() method which signals the Claude API
    to stop processing. This is more reliable than just cancelling the
    asyncio task, which only takes effect at the next await point.
    """
    # Use peek() to avoid promoting to MRU — interrupt is a read-only check
    state = get_session_pool().peek(session_id)
    if not state:
        logger.warning(f"No active session found for {session_id}")
        return False

    if not state.is_connected:
        logger.warning(f"Session {session_id} is not connected")
        return False

    # Set interrupt flag first - this is checked in the streaming loop as a failsafe
    state.interrupt_requested = True

    # Don't check is_streaming too strictly - there might be race conditions
    # and it's better to try to interrupt anyway
    logger.debug(f"Attempting to interrupt session {session_id} (streaming={state.is_streaming})")

    try:
        await state.client.interrupt()
        # Mark as not streaming immediately after interrupt
        state.is_streaming = False
        logger.debug(f"Successfully interrupted session {session_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to interrupt session {session_id}: {e}", exc_info=True)
        # Even if interrupt failed, mark as not streaming to prevent getting stuck
        state.is_streaming = False
        return False


def get_active_sessions() -> list:
    """Get list of active session IDs (connected clients)"""
    return get_session_pool().active_session_ids()


def get_streaming_sessions() -> list:
    """Get list of currently streaming session IDs"""
    return get_session_pool().streaming_session_ids()


async def stream_to_websocket(
    prompt: str,
    session_id: str,
    profile_id: str,
    project_id: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
    broadcast_func: Optional[callable] = None,
    api_user_id: Optional[str] = None
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    Stream Claude response directly to WebSocket.

    This is the simplified streaming function for the WebSocket-first architecture.
    No sync engine, no background tasks - just direct streaming.

    Yields events:
    - {"type": "chunk", "content": "..."}
    - {"type": "tool_use", "name": "...", "input": {...}}
    - {"type": "tool_result", "name": "...", "output": "..."}
    - {"type": "permission_request", ...} - When tool permission is needed
    - {"type": "done", "session_id": "...", "metadata": {...}}
    - {"type": "error", "message": "..."}

    Args:
        prompt: The user's message
        session_id: The chat session ID
        profile_id: The profile to use
        project_id: Optional project ID
        overrides: Optional overrides for model, permission_mode, etc.
        broadcast_func: Async function to broadcast messages to the WebSocket
        api_user_id: Optional API user ID for user-specific credential resolution
    """
    # Get profile
    profile = get_profile(profile_id)
    if not profile:
        yield {"type": "error", "message": f"Profile not found: {profile_id}"}
        return

    # Get project if specified
    project = None
    if project_id:
        project = database.get_project(project_id)
        if not project:
            yield {"type": "error", "message": f"Project not found: {project_id}"}
            return

    # Get session for resume ID
    session = database.get_session(session_id)
    resume_id = session.get("sdk_session_id") if session else None

    # Determine permission mode
    config = profile.get("config", {})
    permission_mode = overrides.get("permission_mode") if overrides else None
    if not permission_mode:
        permission_mode = config.get("permission_mode", "default")

    logger.debug(f"[WS] Permission mode for session {session_id}: {permission_mode} (profile config: {config.get('permission_mode')}, override: {overrides.get('permission_mode') if overrides else None})")
    logger.debug(f"[WS] broadcast_func provided: {broadcast_func is not None}")

    # Get tool restrictions for use in callback
    allowed_tools = config.get("allowed_tools") or []
    disallowed_tools = config.get("disallowed_tools") or []

    # --- Session Pool: Acquire or create SDK client ---
    pool = get_session_pool()
    existing_state = pool.get(session_id)
    reusing_client = False
    state = None

    # Create a queue for plugin stderr messages to be broadcast to frontend
    # Plugin hooks run in the SDK's async loop but need to send messages to our streaming loop
    stderr_queue: asyncio.Queue = asyncio.Queue()

    if existing_state and existing_state.is_connected and is_subprocess_alive(existing_state):
        # Candidate for reuse — check if critical options changed
        # Build options without callback just for comparison
        options, agents_dict = build_options_from_profile(
            profile=profile,
            project=project,
            overrides=overrides,
            resume_session_id=resume_id,
            can_use_tool=None,  # Not needed for comparison
            hooks=None,
            api_user_id=api_user_id,
            session_id=session_id
        )

        if not requires_reconnect(existing_state, options):
            # REUSE: Subprocess is alive and options are compatible
            state = existing_state
            reusing_client = True

            # Update callback refs so the existing can_use_tool closure sees current values
            state.callback_refs.update({
                "broadcast_func": broadcast_func,
                "permission_mode": permission_mode,
                "allowed_tools": allowed_tools,
                "disallowed_tools": disallowed_tools,
                "profile_id": profile_id,
                "config": config,
                "session_id": session_id,
            })

            # Handle runtime-changeable options via SDK methods
            old_opts = state.connect_options
            if old_opts:
                new_model = getattr(options, 'model', None)
                old_model = getattr(old_opts, 'model', None)
                if new_model and new_model != old_model:
                    try:
                        await state.client.set_model(new_model)
                        logger.debug(f"[WS] Updated model to {new_model} for reused session {session_id}")
                    except Exception as e:
                        logger.warning(f"[WS] Failed to set model on reused session {session_id}: {e}")

            logger.info(f"[WS] Reusing pooled client for session {session_id}")
        else:
            # Options changed — need fresh subprocess
            logger.info(f"[WS] Options changed for session {session_id}, creating fresh client")
            await pool.remove(session_id)
            existing_state = None
    elif existing_state:
        # Subprocess died — clean up dead entry
        logger.warning(f"[WS] Subprocess dead for pooled session {session_id}, creating fresh client")
        await pool.remove_dead(session_id)
        existing_state = None

    if not reusing_client:
        # CREATE: Build full options with callback, connect new subprocess

        # Create callback_refs dict — the callback closure captures this dict reference
        # so we can update values between queries without recreating the callback
        callback_refs = {
            "broadcast_func": broadcast_func,
            "permission_mode": permission_mode,
            "allowed_tools": allowed_tools,
            "disallowed_tools": disallowed_tools,
            "profile_id": profile_id,
            "config": config,
            "session_id": session_id,
        }

        # Create canUseTool callback that reads from callback_refs (mutable dict)
        # This allows the callback to use current values even when the client is reused
        can_use_tool_callback = None
        if broadcast_func:
            logger.debug(f"[WS] Creating canUseTool callback for session {session_id}")

            async def can_use_tool_callback(tool_name: str, tool_input: dict, context):
                """Permission callback that reads from mutable callback_refs dict."""
                refs = callback_refs
                _disallowed = refs["disallowed_tools"]
                _allowed = refs["allowed_tools"]
                _perm_mode = refs["permission_mode"]
                _broadcast = refs["broadcast_func"]
                _profile_id = refs["profile_id"]
                _config = refs["config"]
                _sid = refs["session_id"]

                # Check profile-level tool restrictions FIRST
                if _disallowed and tool_name in _disallowed:
                    logger.debug(f"[WS] Tool {tool_name} blocked by profile disallowed_tools")
                    return PermissionResultDeny(message=f"Tool '{tool_name}' is disabled in this profile")
                if _allowed and tool_name not in _allowed:
                    logger.debug(f"[WS] Tool {tool_name} blocked by profile allowed_tools (not in whitelist)")
                    return PermissionResultDeny(message=f"Tool '{tool_name}' is not enabled in this profile")

                # Handle AskUserQuestion specially
                if tool_name == "AskUserQuestion":
                    questions = tool_input.get("questions", [])
                    if not questions:
                        return PermissionResultAllow(updated_input=tool_input)

                    request_id = f"question-{_sid}-{uuid.uuid4().hex[:8]}"
                    logger.debug(f"[WS] AskUserQuestion canUseTool triggered: {request_id} with {len(questions)} questions")

                    answers = await user_question_handler.request_answers(
                        request_id=request_id,
                        tool_use_id=request_id,
                        session_id=_sid,
                        questions=questions,
                        broadcast_func=_broadcast
                    )

                    logger.debug(f"[WS] Got answers for {request_id}: {list(answers.keys())}")
                    return PermissionResultAllow(updated_input={**tool_input, "answers": answers})

                # ExitPlanMode handling — show approval modal in default mode
                # only. In bypass mode, auto-approve so fire-and-forget
                # workflows aren't blocked waiting for user input.
                if tool_name == "ExitPlanMode":
                    if _perm_mode != "bypassPermissions":
                        request_id = f"perm-{_sid}-{uuid.uuid4().hex[:8]}"
                        logger.debug(f"[WS] ExitPlanMode requires approval in {_perm_mode} mode: {request_id}")

                        result = await permission_handler.request_permission(
                            request_id=request_id,
                            session_id=_sid,
                            profile_id=_profile_id,
                            tool_name=tool_name,
                            tool_input=tool_input,
                            broadcast_func=_broadcast
                        )
                        if result.behavior == "deny":
                            return result

                    return PermissionResultAllow()

                # For other tools in default mode, route through permission handler
                if _perm_mode == "default":
                    request_id = f"perm-{_sid}-{uuid.uuid4().hex[:8]}"
                    logger.debug(f"[WS] Permission request for {tool_name}: {request_id}")

                    result = await permission_handler.request_permission(
                        request_id=request_id,
                        session_id=_sid,
                        profile_id=_profile_id,
                        tool_name=tool_name,
                        tool_input=tool_input,
                        broadcast_func=_broadcast
                    )
                    return result
                else:
                    return PermissionResultAllow()

        # Build options with callback
        options, agents_dict = build_options_from_profile(
            profile=profile,
            project=project,
            overrides=overrides,
            resume_session_id=resume_id,
            can_use_tool=can_use_tool_callback,
            hooks=None,  # Will be set after we have working_dir
            api_user_id=api_user_id,
            session_id=session_id
        )

        # Build hooks configuration from all sources (plugins, database)
        from app.core.hook_service import build_hooks_config

        async def stderr_callback(plugin_name: str, stderr_output: str, severity: str):
            """Callback for plugin hooks to broadcast stderr to frontend."""
            await stderr_queue.put({
                "type": "plugin_message",
                "plugin": plugin_name,
                "message": stderr_output,
                "severity": severity
            })

        if options.cwd:
            plugin_paths = []
            if options.plugins:
                for plugin_entry in options.plugins:
                    if isinstance(plugin_entry, dict) and plugin_entry.get("path"):
                        plugin_paths.append(plugin_entry["path"])

            hooks_config = build_hooks_config(
                working_dir=options.cwd,
                project_id=project_id,
                plugin_paths=plugin_paths,
                stderr_callback=stderr_callback
            )
            if hooks_config:
                options.hooks = hooks_config
                logger.debug(f"[WS] Hooks configured: {list(hooks_config.keys())}")

        # On Windows local mode, write agents to filesystem (workaround for --agents CLI bug)
        agents_dir = None
        written_agent_ids = []
        if agents_dict and options.cwd and detect_deployment_mode() == DeploymentMode.LOCAL:
            agents_dir = write_agents_to_filesystem(agents_dict, options.cwd)
            written_agent_ids = list(agents_dict.keys())

        # Evict LRU session if pool is full
        evicted = await pool.evict_if_needed()
        if evicted:
            logger.debug(f"[WS] Evicted LRU session {evicted} to make room for {session_id}")

        # Create and connect new client
        logger.debug(f"[WS] Creating ClaudeSDKClient for session {session_id} (resume={resume_id is not None}, include_partial={options.include_partial_messages})")
        logger.debug(f"[WS] Options cwd: {options.cwd}")

        # Wrap stderr callback to collect lines for error reporting
        cli_stderr_lines: list[str] = []
        original_stderr = options.stderr
        def capturing_stderr(line: str):
            cli_stderr_lines.append(line)
            if original_stderr:
                original_stderr(line)
        options.stderr = capturing_stderr

        client = ClaudeSDKClient(options=options)
        logger.debug("[WS] ClaudeSDKClient created, attempting connect...")

        try:
            await client.connect()
            logger.debug(f"[WS] Connected to Claude SDK for session {session_id}")
        except Exception as e:
            import traceback
            logger.error(f"[WS] Failed to connect for session {session_id}: {e}")
            logger.error(f"[WS] Exception type: {type(e).__name__}")
            logger.error(f"[WS] Full traceback:\n{traceback.format_exc()}")
            # Check if the CLI subprocess is still alive
            if hasattr(client, '_transport') and hasattr(client._transport, '_process') and client._transport._process:
                proc = client._transport._process
                exit_code = proc.returncode
                if exit_code is not None:
                    logger.error(f"[WS] CLI process exited with code {exit_code} during initialization")
                else:
                    logger.error("[WS] CLI process is still running (hung during initialization)")
            if cli_stderr_lines:
                stderr_msg = " ".join(line.strip() for line in cli_stderr_lines if line.strip())
                logger.error(f"[WS] CLI stderr: {stderr_msg}")
                yield {"type": "error", "message": stderr_msg}
            else:
                yield {"type": "error", "message": f"Connection failed: {e}"}
            return

        state = SessionState(
            client=client,
            sdk_session_id=resume_id,
            is_connected=True,
            written_agent_ids=written_agent_ids,
            agents_dir=agents_dir,
            callback_refs=callback_refs,
            connect_options=options,
        )
        pool.put(session_id, state)

    # Mark as streaming (both reuse and create paths)
    state.is_streaming = True
    state.last_activity = datetime.now()

    # Initialize sdk_session_id - use resume_id if resuming, otherwise None until we get it from SDK
    sdk_session_id = resume_id

    # Execute query
    # Note: SDK built-in commands like /context and /compact are passed directly to the SDK
    response_text = []
    tool_messages = []  # Collect tool use/result messages for storage
    task_tool_uses = {}  # Track Task (subagent) tool uses by tool_id
    tool_id_to_name = {}  # Track tool_use_id → tool_name for ToolResultBlock lookup
    metadata = {}
    interrupted = False
    include_partial = options.include_partial_messages  # Track if streaming events are enabled
    has_visible_content = False  # Track if any content was yielded to the frontend
    is_slash_command = prompt.strip().startswith("/")

    # Helper to drain stderr queue and yield plugin messages
    async def drain_stderr_queue():
        """Drain all pending stderr messages from plugin hooks."""
        messages = []
        while True:
            try:
                msg = stderr_queue.get_nowait()
                messages.append(msg)
                logger.debug(f"[WS] Drained plugin message from queue: {msg.get('type')}")
            except asyncio.QueueEmpty:
                break
        if messages:
            logger.debug(f"[WS] Drained {len(messages)} plugin message(s) from stderr queue")
        return messages

    try:
        # Resolve virtual @Notes/note-id references to actual content
        prompt = resolve_note_references(prompt)

        await state.client.query(prompt)

        async for message in state.client.receive_response():
            # Don't break immediately on interrupt_requested — let receive_response()
            # end naturally so remaining in-flight tool calls reach the UI.
            # The SDK's client.interrupt() causes receive_response() to end after
            # completing any in-progress operations.

            # Drain stderr queue and yield any plugin messages
            for stderr_msg in await drain_stderr_queue():
                logger.debug(f"[WS] Yielding plugin message to websocket: {stderr_msg.get('type')}")
                yield stderr_msg

            if isinstance(message, SystemMessage):
                logger.debug(f"[WS] SystemMessage received: subtype={message.subtype}, data_keys={list(message.data.keys()) if message.data else []}, data_preview={str(message.data)[:500] if message.data else 'None'}")
                if message.subtype == "init" and "session_id" in message.data:
                    sdk_session_id = message.data["session_id"]
                    state.sdk_session_id = sdk_session_id
                    logger.debug(f"[WS] Captured SDK session ID: {sdk_session_id}")

                else:
                    # Forward all system messages to frontend except noisy internal ones.
                    # Previous whitelist approach silently dropped SDK errors/warnings
                    # with new subtypes (e.g., skill errors, command output).
                    SUPPRESSED_SUBTYPES = {
                        'init',  # Already handled above
                        'rate_limit_event',  # Internal API rate limit info, not useful in chat
                    }

                    # Suppress system messages from subagent execution.
                    # SystemMessage doesn't have parent_tool_use_id as a direct attribute,
                    # but the raw data dict may contain it. Also suppress if any subagents
                    # are actively running (SDK runs subagents synchronously, so system
                    # messages during active subagent execution are from the subagent context).
                    parent_tool_id = message.data.get("parent_tool_use_id") if message.data else None
                    is_from_subagent = (
                        (parent_tool_id and parent_tool_id in task_tool_uses) or
                        (not parent_tool_id and len(task_tool_uses) > 0)
                    )

                    if is_from_subagent:
                        logger.debug(f"[WS] Suppressed subagent SystemMessage: subtype={message.subtype}")
                    elif message.subtype not in SUPPRESSED_SUBTYPES:
                        has_visible_content = True
                        logger.debug(f"[WS] Forwarding SystemMessage to frontend: subtype={message.subtype}")
                        yield {
                            "type": "system",
                            "subtype": message.subtype,
                            "data": message.data
                        }
                    else:
                        logger.debug(f"[WS] Suppressed SystemMessage subtype: {message.subtype}")

            elif isinstance(message, StreamEvent):
                # StreamEvent is sent when include_partial_messages=True
                # Contains real-time streaming events for character-by-character display
                event = message.event
                event_type = event.get("type") if event else None
                parent_tool_id = getattr(message, 'parent_tool_use_id', None)

                # Route subagent StreamEvents — don't mix into main agent stream
                # The SDK (0.1.30+) sets parent_tool_use_id on subagent events
                if parent_tool_id and parent_tool_id in task_tool_uses:
                    # Only forward text deltas as subagent_chunk for real-time display
                    if event_type == "content_block_delta":
                        delta = event.get("delta", {})
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            yield {
                                "type": "subagent_chunk",
                                "agent_id": parent_tool_id,
                                "content": delta["text"]
                            }
                    # Skip all other subagent stream events (block_start, block_stop,
                    # message_start, message_delta, thinking_delta, input_json_delta)
                    # The AssistantMessage handler already handles final subagent content
                    continue

                if event_type == "content_block_delta":
                    delta = event.get("delta", {})
                    delta_type = delta.get("type")

                    if delta_type == "text_delta" and delta.get("text"):
                        # Real-time text streaming chunk
                        text = delta["text"]
                        has_visible_content = True
                        logger.debug(f"[WS] StreamEvent text_delta: {len(text)} chars")
                        yield {
                            "type": "stream_delta",
                            "delta_type": "text",
                            "content": text,
                            "index": event.get("index", 0)
                        }
                    elif delta_type == "thinking_delta" and delta.get("thinking"):
                        # Thinking block streaming
                        yield {
                            "type": "stream_delta",
                            "delta_type": "thinking",
                            "content": delta["thinking"],
                            "index": event.get("index", 0)
                        }
                    elif delta_type == "input_json_delta" and delta.get("partial_json"):
                        # Tool input streaming
                        yield {
                            "type": "stream_delta",
                            "delta_type": "tool_input",
                            "content": delta["partial_json"],
                            "index": event.get("index", 0)
                        }

                elif event_type == "message_start":
                    # Start of a new streaming message
                    logger.debug("[WS] StreamEvent message_start")
                    yield {
                        "type": "stream_start",
                        "message": event.get("message", {})
                    }

                elif event_type == "content_block_start":
                    # Start of a content block (text, thinking, tool_use)
                    content_block = event.get("content_block", {})
                    block_type = content_block.get("type")
                    logger.debug(f"[WS] StreamEvent content_block_start: {block_type}")
                    yield {
                        "type": "stream_block_start",
                        "block_type": block_type,
                        "index": event.get("index", 0),
                        "content_block": content_block
                    }

                elif event_type == "content_block_stop":
                    # End of a content block
                    yield {
                        "type": "stream_block_stop",
                        "index": event.get("index", 0)
                    }

                elif event_type == "message_delta":
                    # Final message metadata (stop_reason, usage)
                    yield {
                        "type": "stream_message_delta",
                        "delta": event.get("delta", {}),
                        "usage": event.get("usage", {})
                    }

            elif isinstance(message, AssistantMessage):
                # Check for synthetic API error messages (model is "<synthetic>")
                # These are generated by the SDK when an API error occurs
                if message.model == "<synthetic>" or message.error:
                    # Extract error text from content blocks
                    error_text = ""
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            error_text = block.text
                            break
                    if error_text:
                        logger.warning(f"[WS] API error message: {error_text[:100]}...")
                        yield {
                            "type": "system",
                            "subtype": "api_error",
                            "data": {"content": error_text}
                        }
                    continue

                # Check if this message is from a subagent using parent_tool_use_id
                parent_tool_id = message.parent_tool_use_id

                for block in message.content:
                    if isinstance(block, TextBlock):
                        # Check if this text is from a subagent
                        if parent_tool_id and parent_tool_id in task_tool_uses:
                            # Text from subagent - send as subagent_chunk
                            logger.debug(f"[WS] Subagent chunk len={len(block.text)} for agent={parent_tool_id}")
                            yield {
                                "type": "subagent_chunk",
                                "agent_id": parent_tool_id,
                                "content": block.text
                            }
                        else:
                            # When include_partial_messages=True, text was already streamed via stream_delta
                            # Only send chunk when partial messages is disabled
                            response_text.append(block.text)
                            has_visible_content = True
                            if not include_partial:
                                logger.debug(f"[WS] Text chunk len={len(block.text)} for session={session_id}")
                                yield {"type": "chunk", "content": block.text}

                    elif isinstance(block, ToolUseBlock):
                        tool_id = getattr(block, 'id', None)
                        tool_input = block.input or {}

                        # Track tool_id → name for ToolResultBlock lookup
                        if tool_id:
                            tool_id_to_name[tool_id] = block.name

                        # Collect tool use for storage
                        tool_messages.append({
                            "type": "tool_use",
                            "name": block.name,
                            "tool_id": tool_id,
                            "input": tool_input
                        })

                        # Check if this is a Task tool (subagent) invocation
                        if block.name == "Task":
                            # Track this Task tool use
                            task_tool_uses[tool_id] = {
                                "agent_type": tool_input.get("subagent_type", "unknown"),
                                "description": tool_input.get("description", ""),
                                "prompt": tool_input.get("prompt", "")
                            }

                            # Yield subagent_start event instead of regular tool_use
                            yield {
                                "type": "subagent_start",
                                "tool_id": tool_id,
                                "agent_type": tool_input.get("subagent_type", "unknown"),
                                "description": tool_input.get("description", ""),
                                "prompt": tool_input.get("prompt", ""),
                                "agent_id": tool_id  # Use tool_id as agent_id for tracking
                            }
                        # Check if this tool use is from a subagent
                        elif parent_tool_id and parent_tool_id in task_tool_uses:
                            # Tool use from subagent - send as subagent_tool_use
                            yield {
                                "type": "subagent_tool_use",
                                "agent_id": parent_tool_id,
                                "name": block.name,
                                "id": tool_id,
                                "input": tool_input
                            }
                        else:
                            yield {
                                "type": "tool_use",
                                "name": block.name,
                                "id": tool_id,
                                "input": tool_input
                            }

                    elif isinstance(block, ToolResultBlock):
                        # Extract screenshot from MCP content blocks before truncation
                        screenshot_b64 = extract_mcp_image(block.content)
                        output = truncate_large_payload(block.content)
                        tool_use_id = getattr(block, 'tool_use_id', None)
                        tool_name = tool_id_to_name.get(tool_use_id, getattr(block, 'name', 'unknown'))

                        # Diagnostic: log when tool result content is empty/missing
                        if not output:
                            content_type = type(block.content).__name__
                            content_repr = repr(block.content)[:200] if block.content else "None"
                            logger.warning(
                                f"[WS] AssistantMsg ToolResultBlock EMPTY output - "
                                f"tool={tool_name}, tool_use_id={tool_use_id}, "
                                f"content_type={content_type}, content_repr={content_repr}"
                            )

                        # Collect tool result for storage
                        tool_messages.append({
                            "type": "tool_result",
                            "name": tool_name,
                            "tool_id": tool_use_id,
                            "output": output
                        })

                        # Check if this is a result for a Task tool (subagent)
                        if tool_use_id in task_tool_uses:
                            task_info = task_tool_uses[tool_use_id]
                            yield {
                                "type": "subagent_done",
                                "tool_id": tool_use_id,
                                "agent_id": tool_use_id,  # Use tool_id as agent_id
                                "agent_type": task_info.get("agent_type", "unknown"),
                                "result": output,
                                "is_error": getattr(block, 'is_error', False)
                            }
                            # Clean up tracking
                            del task_tool_uses[tool_use_id]
                        # Check if this tool result is from a subagent
                        elif parent_tool_id and parent_tool_id in task_tool_uses:
                            # Tool result from subagent - send as subagent_tool_result
                            yield {
                                "type": "subagent_tool_result",
                                "agent_id": parent_tool_id,
                                "name": tool_name,
                                "tool_use_id": tool_use_id,
                                "output": output,
                                "is_error": getattr(block, 'is_error', False)
                            }
                        else:
                            event = {
                                "type": "tool_result",
                                "name": tool_name,
                                "tool_use_id": tool_use_id,
                                "output": output,
                                "is_error": getattr(block, 'is_error', False)
                            }
                            # Include screenshot for browser tools (MCP content has image blocks)
                            if screenshot_b64:
                                event["screenshot"] = screenshot_b64
                            yield event

                metadata["model"] = message.model

            elif isinstance(message, UserMessage):
                # UserMessage can contain:
                # 1. Tool results from Claude's tool executions (ToolResultBlock)
                # 2. Local command output like /context, /compact (TextBlock with <local-command-stdout>)
                # Check if this message is from a subagent using parent_tool_use_id
                parent_tool_id = message.parent_tool_use_id
                logger.debug(f"[WS] UserMessage received: content_type={type(message.content).__name__}, parent_tool_id={parent_tool_id}")

                # Suppress local command output from subagent UserMessages
                is_subagent_user_msg = parent_tool_id and parent_tool_id in task_tool_uses

                # Handle string content (rare but possible)
                if isinstance(message.content, str):
                    content = message.content
                    logger.debug(f"[WS] UserMessage string content preview: {content[:200] if content else 'empty'}")
                    # Check for local command output
                    if "<local-command-stdout>" in content:
                        match = re.search(r'<local-command-stdout>(.*?)</local-command-stdout>', content, re.DOTALL)
                        if match and not is_subagent_user_msg:
                            has_visible_content = True
                            yield {
                                "type": "system",
                                "subtype": "local_command",
                                "data": {"content": match.group(1).strip()}
                            }
                    elif is_slash_command and content.strip() and content.strip() != "No response requested." and not is_subagent_user_msg:
                        has_visible_content = True
                        logger.debug(f"[WS] Forwarding slash command UserMessage string: {content[:200]}")
                        yield {
                            "type": "system",
                            "subtype": "local_command",
                            "data": {"content": content.strip()}
                        }
                    continue

                for block in message.content:
                    # Handle TextBlock - may contain local command output
                    if isinstance(block, TextBlock):
                        text = block.text
                        # Check for local command output (e.g., /context, /compact)
                        if "<local-command-stdout>" in text:
                            match = re.search(r'<local-command-stdout>(.*?)</local-command-stdout>', text, re.DOTALL)
                            if match and not is_subagent_user_msg:
                                has_visible_content = True
                                yield {
                                    "type": "system",
                                    "subtype": "local_command",
                                    "data": {"content": match.group(1).strip()}
                                }
                        elif is_slash_command and text.strip() and text.strip() != "No response requested." and not is_subagent_user_msg:
                            # For slash commands, forward non-trivial UserMessage text
                            # SDK built-in commands may return output without <local-command-stdout> tags
                            has_visible_content = True
                            logger.debug(f"[WS] Forwarding slash command UserMessage text: {text[:200]}")
                            yield {
                                "type": "system",
                                "subtype": "local_command",
                                "data": {"content": text.strip()}
                            }
                        # Don't yield regular text from UserMessage for non-slash queries

                    elif isinstance(block, ToolResultBlock):
                        # Extract screenshot from MCP content blocks before truncation
                        screenshot_b64 = extract_mcp_image(block.content)
                        output = truncate_large_payload(block.content) if block.content else ""
                        tool_use_id = block.tool_use_id
                        # Look up tool name from preceding ToolUseBlock
                        tool_name = tool_id_to_name.get(tool_use_id, "unknown")
                        content_len = len(str(block.content) if block.content else '')
                        logger.debug(f"[WS] UserMessage ToolResultBlock - tool_use_id: {tool_use_id}, tool_name: {tool_name}, parent_tool_id: {parent_tool_id}, content length: {content_len}, has_screenshot: {screenshot_b64 is not None}, is_error: {block.is_error}")

                        # Diagnostic: log when tool result content is empty/missing
                        if not output:
                            content_type = type(block.content).__name__
                            content_repr = repr(block.content)[:200] if block.content else "None"
                            logger.warning(
                                f"[WS] UserMessage ToolResultBlock EMPTY output - "
                                f"tool={tool_name}, tool_use_id={tool_use_id}, "
                                f"content_type={content_type}, content_repr={content_repr}, "
                                f"block.content falsy={not block.content}"
                            )

                        # Collect tool result for storage
                        tool_messages.append({
                            "type": "tool_result",
                            "name": tool_name,
                            "tool_id": tool_use_id,
                            "output": output
                        })

                        # Check if this is a result for a Task tool (subagent)
                        if tool_use_id in task_tool_uses:
                            task_info = task_tool_uses[tool_use_id]
                            yield {
                                "type": "subagent_done",
                                "tool_id": tool_use_id,
                                "agent_id": tool_use_id,  # Use tool_id as agent_id
                                "agent_type": task_info.get("agent_type", "unknown"),
                                "result": output,
                                "is_error": block.is_error
                            }
                            # Clean up tracking
                            del task_tool_uses[tool_use_id]
                        # Check if this tool result is from a subagent
                        elif parent_tool_id and parent_tool_id in task_tool_uses:
                            # Tool result from subagent - send as subagent_tool_result
                            yield {
                                "type": "subagent_tool_result",
                                "agent_id": parent_tool_id,
                                "name": tool_name,
                                "tool_use_id": tool_use_id,
                                "output": output,
                                "is_error": block.is_error
                            }
                        else:
                            event = {
                                "type": "tool_result",
                                "name": tool_name,
                                "tool_use_id": tool_use_id,
                                "output": output,
                                "is_error": block.is_error
                            }
                            # Include screenshot for browser tools (MCP content has image blocks)
                            if screenshot_b64:
                                event["screenshot"] = screenshot_b64
                            yield event

            elif isinstance(message, ResultMessage):
                metadata["duration_ms"] = message.duration_ms
                metadata["num_turns"] = message.num_turns
                metadata["total_cost_usd"] = message.total_cost_usd
                metadata["is_error"] = message.is_error
                # Extract token counts from usage dict
                if message.usage:
                    metadata["tokens_in"] = message.usage.get("input_tokens", 0)
                    metadata["tokens_out"] = message.usage.get("output_tokens", 0)
                    metadata["cache_creation_tokens"] = message.usage.get("cache_creation_input_tokens", 0)
                    metadata["cache_read_tokens"] = message.usage.get("cache_read_input_tokens", 0)

            else:
                logger.warning(f"[WS] Unhandled message type: {type(message).__name__}")

        # Check if the loop ended due to an interrupt request
        # (SDK's receive_response() ended naturally after client.interrupt())
        if state.interrupt_requested:
            interrupted = True
            logger.debug(f"[WS] Streaming loop ended after interrupt for session {session_id}")

        # Final drain of stderr queue after streaming loop completes
        for stderr_msg in await drain_stderr_queue():
            yield stderr_msg

    except asyncio.CancelledError:
        interrupted = True
        logger.debug(f"[WS] Query cancelled for session {session_id}")
        # Don't re-raise - we handle it gracefully by yielding interrupted message

    except Exception as e:
        logger.error(f"[WS] Query error for session {session_id}: {e}")
        state.is_connected = False
        yield {"type": "error", "message": str(e)}
        return

    finally:
        # Mark as not streaming and reset interrupt flag
        state.is_streaming = False
        state.interrupt_requested = False
        state.last_activity = datetime.now()

        # Clear stale broadcast ref — will be set fresh on next query
        if state.callback_refs:
            state.callback_refs["broadcast_func"] = None

        # Session pool lifecycle: KEEP the client alive for reuse.
        # Only remove from pool if the client died.
        if not state.is_connected:
            # Client died during query — remove dead entry
            await pool.remove_dead(session_id)
            logger.debug(f"[WS] Removed dead session {session_id} from pool")
        else:
            # Normal case: keep in pool for reuse!
            logger.debug(f"[WS] Kept session {session_id} in pool for reuse")

    # Update session in database
    # Only set title for new sessions (when resume_id was None, meaning first message)
    # This preserves the first user message as the session title
    title_to_set = None
    if not resume_id:
        title_to_set = prompt[:50].strip()
        if len(prompt) > 50:
            title_to_set += "..."

    database.update_session(
        session_id=session_id,
        sdk_session_id=sdk_session_id,
        title=title_to_set,
        cost_increment=metadata.get("total_cost_usd", 0),
        tokens_in_increment=metadata.get("tokens_in", 0),
        tokens_out_increment=metadata.get("tokens_out", 0),
        turn_increment=metadata.get("num_turns", 0)
    )
    logger.debug(f"[WS] Updated session {session_id}, sdk_session_id={sdk_session_id}, title={title_to_set}")

    # Store tool messages (tool_use and tool_result)
    for tool_msg in tool_messages:
        if tool_msg["type"] == "tool_use":
            database.add_session_message(
                session_id=session_id,
                role="tool_use",
                content=f"Using tool: {tool_msg['name']}",
                tool_name=tool_msg["name"],
                tool_input=tool_msg.get("input"),
                metadata={"tool_id": tool_msg.get("tool_id")},
                profile_id=profile_id
            )
        elif tool_msg["type"] == "tool_result":
            database.add_session_message(
                session_id=session_id,
                role="tool_result",
                content=tool_msg.get("output", ""),
                tool_name=tool_msg["name"],
                metadata={"tool_id": tool_msg.get("tool_id")},
                profile_id=profile_id
            )

    # Store assistant response
    full_response = "".join(response_text)
    if full_response or tool_messages or interrupted:
        database.add_session_message(
            session_id=session_id,
            role="assistant",
            content=full_response + ("\n\n[Interrupted]" if interrupted else ""),
            metadata=metadata,
            profile_id=profile_id
        )

    # Log usage
    if metadata:
        database.log_usage(
            session_id=session_id,
            profile_id=profile_id,
            model=metadata.get("model"),
            tokens_in=metadata.get("tokens_in", 0),
            tokens_out=metadata.get("tokens_out", 0),
            cost_usd=metadata.get("total_cost_usd", 0),
            duration_ms=metadata.get("duration_ms", 0)
        )

    # For slash commands that produced no visible content, yield a notification
    # SDK built-in commands like /memory, /cost, /model execute inside the subprocess
    # and may not produce any streaming events visible to the frontend
    if is_slash_command and not has_visible_content and not interrupted:
        cmd_name = prompt.strip().split()[0]  # e.g., "/memory"
        logger.info(f"[WS] Slash command {cmd_name} produced no visible output for session {session_id}")
        yield {
            "type": "system",
            "subtype": "local_command",
            "data": {"content": f"Command {cmd_name} completed (no output)."}
        }

    # Yield done or interrupted event
    if interrupted:
        yield {
            "type": "interrupted",
            "session_id": session_id,
            "message": "Query was interrupted"
        }
    else:
        yield {
            "type": "done",
            "session_id": session_id,
            "metadata": metadata
        }
