"""
MCP server and plugin configuration for Claude queries.

Contains functions that build MCP server dictionaries and plugin lists
for ClaudeAgentOptions.
"""

import logging
from pathlib import Path
from typing import Optional, Dict, Any

from app.core.config import settings
from app.core.mcp_service import get_mcp_service, McpServerType

logger = logging.getLogger(__name__)


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
