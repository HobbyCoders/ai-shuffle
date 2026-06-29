"""
MCP server management tools for the app-config SDK MCP server.

Provides list/add/remove/toggle operations for MCP server configurations
across user, local, and project scopes.
"""

import logging
from typing import Optional

from app.mcp.config_helpers import _tool_result, _error

logger = logging.getLogger(__name__)


def _create_mcp_tools(is_admin: bool, project_dir: Optional[str] = None) -> list:
    """Create MCP server management tools.

    Args:
        is_admin: whether write tools are permitted.
        project_dir: the current project's *directory* (path), used for
            LOCAL/PROJECT scope .mcp.json files. This must be a filesystem
            path, not a project identifier — see McpService.__init__.
    """
    from claude_agent_sdk import tool
    from app.core.mcp_service import get_mcp_service, McpScope, McpServerType

    _is_admin = is_admin
    _project_dir = project_dir
    tools = []

    def _server_to_dict(server) -> dict:
        """Convert McpServerConfig dataclass to serializable dict."""
        return {
            "name": server.name,
            "type": server.type.value if hasattr(server.type, "value") else str(server.type),
            "command": server.command,
            "args": server.args,
            "url": server.url,
            "scope": server.scope if isinstance(server.scope, str) else str(server.scope),
            "enabled": server.enabled,
            "description": server.description,
        }

    # -- listMcpServers ---------------------------------------------------

    @tool(
        "listMcpServers",
        "List all configured MCP servers across all scopes (user, local, project).",
        {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["user", "local", "project", "all"],
                    "description": "Filter by scope. Defaults to 'all'.",
                },
            },
        },
    )
    async def list_mcp_servers(args: dict) -> dict:
        service = get_mcp_service(project_dir=_project_dir)
        scope_filter = args.get("scope", "all")

        if scope_filter == "all":
            all_servers = service.get_all_servers()
            result = {}
            for scope_name, servers in all_servers.items():
                result[scope_name] = [_server_to_dict(s) for s in servers]
            total = sum(len(v) for v in result.values())
            return _tool_result({"servers": result, "total": total})
        else:
            scope_map = {"user": McpScope.USER, "local": McpScope.LOCAL, "project": McpScope.PROJECT}
            scope = scope_map.get(scope_filter)
            if not scope:
                return _error(f"Invalid scope: {scope_filter}")
            servers = service.get_servers_for_scope(scope)
            return _tool_result({
                "scope": scope_filter,
                "servers": [_server_to_dict(s) for s in servers],
                "count": len(servers),
            })

    tools.append(list_mcp_servers)

    # -- addMcpServer -----------------------------------------------------

    @tool(
        "addMcpServer",
        "Add a new MCP server configuration. Requires a session restart to take effect.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Unique server name."},
                "server_type": {
                    "type": "string",
                    "enum": ["stdio", "sse", "http"],
                    "description": "Server type.",
                },
                "command": {
                    "type": "string",
                    "description": "Command to run (stdio type). E.g., 'npx', 'python', 'node'.",
                },
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Command arguments (stdio type).",
                },
                "url": {
                    "type": "string",
                    "description": "Server URL (sse/http type).",
                },
                "env": {
                    "type": "object",
                    "description": "Environment variables for the server process.",
                },
                "scope": {
                    "type": "string",
                    "enum": ["user", "local", "project"],
                    "description": "Where to save: 'user' (global), 'local' (project, gitignored), 'project' (committed). Default 'user'.",
                },
                "description": {
                    "type": "string",
                    "description": "Optional description.",
                },
            },
            "required": ["name", "server_type"],
        },
    )
    async def add_mcp_server(args: dict) -> dict:
        if not _is_admin:
            return _error("App configuration tools require admin access.")

        server_type = args["server_type"]
        if server_type == "stdio" and not args.get("command"):
            return _error("stdio servers require a 'command'.")
        if server_type in ("sse", "http") and not args.get("url"):
            return _error(f"{server_type} servers require a 'url'.")

        scope_str = args.get("scope", "user")
        scope_map = {"user": McpScope.USER, "local": McpScope.LOCAL, "project": McpScope.PROJECT}
        scope = scope_map.get(scope_str)
        if not scope:
            return _error(f"Invalid scope: {scope_str}")

        from app.core.mcp_service import McpServerConfig
        type_map = {"stdio": McpServerType.STDIO, "sse": McpServerType.SSE, "http": McpServerType.HTTP}

        config = McpServerConfig(
            name=args["name"],
            type=type_map[server_type],
            command=args.get("command"),
            args=args.get("args"),
            env=args.get("env"),
            url=args.get("url"),
            scope=scope_str,
            enabled=True,
            description=args.get("description"),
        )

        try:
            service = get_mcp_service(project_dir=_project_dir)
            service.add_server(args["name"], config, scope)
            return _tool_result({
                "success": True,
                "name": args["name"],
                "server_type": server_type,
                "scope": scope_str,
                "message": (
                    f"MCP server '{args['name']}' added to {scope_str} scope. "
                    "A session restart is needed for the agent to use it."
                ),
            })
        except Exception as e:
            return _error(f"Failed to add MCP server: {str(e)}")

    tools.append(add_mcp_server)

    # -- removeMcpServer --------------------------------------------------

    @tool(
        "removeMcpServer",
        "Remove an MCP server configuration. This cannot be undone.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Server name."},
                "scope": {
                    "type": "string",
                    "enum": ["user", "local", "project"],
                    "description": "The scope to remove from.",
                },
                "confirm": {
                    "type": "boolean",
                    "description": "Must be true to confirm removal.",
                },
            },
            "required": ["name", "scope", "confirm"],
        },
    )
    async def remove_mcp_server(args: dict) -> dict:
        if not _is_admin:
            return _error("App configuration tools require admin access.")
        if not args.get("confirm"):
            return _error("Set confirm=true to remove. Ask the user for confirmation first.")

        scope_map = {"user": McpScope.USER, "local": McpScope.LOCAL, "project": McpScope.PROJECT}
        scope = scope_map.get(args["scope"])
        if not scope:
            return _error(f"Invalid scope: {args['scope']}")

        try:
            service = get_mcp_service(project_dir=_project_dir)
            service.remove_server(args["name"], scope)
            return _tool_result({
                "success": True,
                "message": f"MCP server '{args['name']}' removed from {args['scope']} scope.",
            })
        except Exception as e:
            return _error(f"Failed to remove MCP server: {str(e)}")

    tools.append(remove_mcp_server)

    # -- toggleMcpServer --------------------------------------------------

    @tool(
        "toggleMcpServer",
        "Enable or disable an MCP server in the user scope.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Server name."},
                "enabled": {"type": "boolean"},
            },
            "required": ["name", "enabled"],
        },
    )
    async def toggle_mcp_server(args: dict) -> dict:
        if not _is_admin:
            return _error("App configuration tools require admin access.")

        try:
            service = get_mcp_service(project_dir=_project_dir)
            service.set_server_enabled(args["name"], args["enabled"])
            state = "enabled" if args["enabled"] else "disabled"
            return _tool_result({
                "success": True,
                "name": args["name"],
                "enabled": args["enabled"],
                "message": f"MCP server '{args['name']}' {state}.",
            })
        except Exception as e:
            return _error(f"Failed to toggle MCP server: {str(e)}")

    tools.append(toggle_mcp_server)

    return tools
