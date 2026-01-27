"""
MCP Server Management Service

Provides functionality for managing MCP (Model Context Protocol) server configurations.

MCP Scopes:
- USER: ~/.claude/.mcp.json (global MCP servers for all projects)
- LOCAL: <project>/.claude/.mcp.json (project-local, gitignored)
- PROJECT: <project>/.mcp.json (project MCP config, committed to git)

Docker mode uses /home/appuser/.claude/ for user scope.
"""

import json
import logging
import threading
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from enum import Enum

from app.core.platform import is_docker_mode, get_claude_credentials_dir

logger = logging.getLogger(__name__)


class McpScope(str, Enum):
    """Scope for MCP server configuration"""
    USER = "user"       # ~/.claude/.mcp.json
    LOCAL = "local"     # <project>/.claude/.mcp.json
    PROJECT = "project" # <project>/.mcp.json


class McpServerType(str, Enum):
    """Type of MCP server"""
    STDIO = "stdio"
    SSE = "sse"
    HTTP = "http"


@dataclass
class McpServerConfig:
    """
    Configuration for an MCP server.

    For STDIO servers: command, args, env
    For SSE/HTTP servers: url, headers
    """
    name: str
    type: McpServerType = McpServerType.STDIO
    # STDIO fields
    command: Optional[str] = None
    args: Optional[List[str]] = None
    env: Optional[Dict[str, str]] = None
    # SSE/HTTP fields
    url: Optional[str] = None
    headers: Optional[Dict[str, str]] = None
    # Metadata
    scope: str = "user"
    enabled: bool = True
    description: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization"""
        return {
            "name": self.name,
            "type": self.type.value if isinstance(self.type, McpServerType) else self.type,
            "command": self.command,
            "args": self.args,
            "env": self.env,
            "url": self.url,
            "headers": self.headers,
            "scope": self.scope,
            "enabled": self.enabled,
            "description": self.description,
        }

    def to_mcp_json_format(self) -> Dict[str, Any]:
        """
        Convert to .mcp.json format for Claude CLI.

        Returns the server config without name/scope/enabled (those are external).
        """
        if self.type == McpServerType.STDIO or self.type == "stdio":
            config: Dict[str, Any] = {}
            if self.command:
                config["command"] = self.command
            if self.args:
                config["args"] = self.args
            if self.env:
                config["env"] = self.env
            return config
        else:
            # SSE or HTTP
            config = {
                "type": self.type.value if isinstance(self.type, McpServerType) else self.type,
            }
            if self.url:
                config["url"] = self.url
            if self.headers:
                config["headers"] = self.headers
            return config

    @classmethod
    def from_mcp_json(cls, name: str, data: Dict[str, Any], scope: str = "user") -> "McpServerConfig":
        """
        Create from .mcp.json format.

        Args:
            name: Server name (key in mcpServers dict)
            data: Server configuration from JSON
            scope: The scope this server belongs to
        """
        server_type = data.get("type", "stdio")
        if server_type not in ["stdio", "sse", "http"]:
            server_type = "stdio"

        return cls(
            name=name,
            type=McpServerType(server_type),
            command=data.get("command"),
            args=data.get("args"),
            env=data.get("env"),
            url=data.get("url"),
            headers=data.get("headers"),
            scope=scope,
            enabled=True,  # Enabled by default if in config
            description=data.get("description"),
        )


@dataclass
class McpConfig:
    """Full MCP configuration from a file"""
    servers: Dict[str, McpServerConfig] = field(default_factory=dict)

    def to_mcp_json(self) -> Dict[str, Any]:
        """Convert to .mcp.json format"""
        return {
            "mcpServers": {
                name: server.to_mcp_json_format()
                for name, server in self.servers.items()
            }
        }


def _get_claude_dir() -> Path:
    """
    Get the Claude configuration directory based on deployment mode.

    Returns:
        - Docker: /home/appuser/.claude
        - Local: ~/.claude
    """
    if is_docker_mode():
        return Path("/home/appuser/.claude")
    return get_claude_credentials_dir()


class McpService:
    """
    Service for managing MCP server configurations.

    Reads and writes to .mcp.json files at various scopes to maintain
    compatibility with the Claude CLI.

    MCP Scopes:
        - USER: ~/.claude/.mcp.json - Global MCP servers
        - LOCAL: <project>/.claude/.mcp.json - Project-local (gitignored)
        - PROJECT: <project>/.mcp.json - Project MCP config (committed)
    """

    def __init__(self, project_dir: Optional[Path] = None):
        """
        Initialize the MCP service.

        Args:
            project_dir: Optional project directory for LOCAL/PROJECT scope.
                        If not provided, only USER scope is available.
        """
        # User scope paths
        self.CLAUDE_DIR = _get_claude_dir()
        self.USER_MCP_FILE = self.CLAUDE_DIR / ".mcp.json"

        # Project scope paths
        self.project_dir = project_dir
        if project_dir:
            self.LOCAL_MCP_FILE = project_dir / ".claude" / ".mcp.json"
            self.PROJECT_MCP_FILE = project_dir / ".mcp.json"
        else:
            self.LOCAL_MCP_FILE = None
            self.PROJECT_MCP_FILE = None

    def get_mcp_file_for_scope(self, scope: McpScope) -> Optional[Path]:
        """Get the .mcp.json file path for a given scope"""
        if scope == McpScope.USER:
            return self.USER_MCP_FILE
        elif scope == McpScope.LOCAL:
            return self.LOCAL_MCP_FILE
        elif scope == McpScope.PROJECT:
            return self.PROJECT_MCP_FILE
        return None

    def _read_json_file(self, path: Path, default: Any = None) -> Any:
        """Safely read a JSON file"""
        try:
            if path.exists():
                with open(path, 'r') as f:
                    return json.load(f)
            return default if default is not None else {}
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Error reading {path}: {e}")
            return default if default is not None else {}

    def _write_json_file(self, path: Path, data: Any) -> bool:
        """Safely write a JSON file with atomic operation"""
        try:
            # Ensure parent directory exists
            path.parent.mkdir(parents=True, exist_ok=True)

            temp_path = path.with_suffix('.tmp')
            with open(temp_path, 'w') as f:
                json.dump(data, f, indent=2)
            temp_path.replace(path)
            return True
        except IOError as e:
            logger.error(f"Error writing {path}: {e}")
            return False

    # =========================================================================
    # Read Operations
    # =========================================================================

    def get_servers_for_scope(self, scope: McpScope) -> List[McpServerConfig]:
        """
        Get all MCP servers configured in a specific scope.

        Args:
            scope: The scope to read from

        Returns:
            List of MCP server configurations
        """
        mcp_file = self.get_mcp_file_for_scope(scope)
        if not mcp_file:
            return []

        data = self._read_json_file(mcp_file, {})
        mcp_servers = data.get("mcpServers", {})

        servers = []
        for name, config in mcp_servers.items():
            try:
                server = McpServerConfig.from_mcp_json(name, config, scope.value)
                servers.append(server)
            except Exception as e:
                logger.error(f"Error parsing MCP server {name}: {e}")

        return servers

    def get_all_servers(self) -> Dict[str, List[McpServerConfig]]:
        """
        Get all MCP servers from all available scopes.

        Returns:
            Dictionary mapping scope name to list of servers
        """
        result = {}

        # Always include USER scope
        result["user"] = self.get_servers_for_scope(McpScope.USER)

        # Include project scopes if available
        if self.project_dir:
            result["local"] = self.get_servers_for_scope(McpScope.LOCAL)
            result["project"] = self.get_servers_for_scope(McpScope.PROJECT)

        return result

    def get_server(self, name: str, scope: McpScope) -> Optional[McpServerConfig]:
        """
        Get a specific MCP server by name and scope.

        Args:
            name: Server name
            scope: Scope to search in

        Returns:
            Server configuration or None if not found
        """
        servers = self.get_servers_for_scope(scope)
        for server in servers:
            if server.name == name:
                return server
        return None

    # =========================================================================
    # Write Operations
    # =========================================================================

    def add_server(
        self,
        name: str,
        config: McpServerConfig,
        scope: McpScope = McpScope.USER
    ) -> McpServerConfig:
        """
        Add a new MCP server to a specific scope.

        Args:
            name: Server name (must be unique within scope)
            config: Server configuration
            scope: Target scope

        Returns:
            The added server configuration
        """
        mcp_file = self.get_mcp_file_for_scope(scope)
        if not mcp_file:
            raise ValueError(f"Scope {scope} is not available (no project directory)")

        # Read existing config
        data = self._read_json_file(mcp_file, {"mcpServers": {}})
        if "mcpServers" not in data:
            data["mcpServers"] = {}

        # Check if already exists
        if name in data["mcpServers"]:
            raise ValueError(f"MCP server '{name}' already exists in {scope.value} scope")

        # Add server
        config.name = name
        config.scope = scope.value
        data["mcpServers"][name] = config.to_mcp_json_format()

        # Write back
        if not self._write_json_file(mcp_file, data):
            raise RuntimeError(f"Failed to write MCP config to {mcp_file}")

        return config

    def update_server(
        self,
        name: str,
        config: McpServerConfig,
        scope: McpScope = McpScope.USER
    ) -> McpServerConfig:
        """
        Update an existing MCP server configuration.

        Args:
            name: Server name
            config: New configuration
            scope: Target scope

        Returns:
            The updated server configuration
        """
        mcp_file = self.get_mcp_file_for_scope(scope)
        if not mcp_file:
            raise ValueError(f"Scope {scope} is not available")

        # Read existing config
        data = self._read_json_file(mcp_file, {"mcpServers": {}})
        if "mcpServers" not in data:
            data["mcpServers"] = {}

        # Check if exists
        if name not in data["mcpServers"]:
            raise ValueError(f"MCP server '{name}' not found in {scope.value} scope")

        # Update server
        config.name = name
        config.scope = scope.value
        data["mcpServers"][name] = config.to_mcp_json_format()

        # Write back
        if not self._write_json_file(mcp_file, data):
            raise RuntimeError(f"Failed to write MCP config to {mcp_file}")

        return config

    def remove_server(self, name: str, scope: McpScope = McpScope.USER) -> bool:
        """
        Remove an MCP server from a specific scope.

        Args:
            name: Server name
            scope: Target scope

        Returns:
            True if successful
        """
        mcp_file = self.get_mcp_file_for_scope(scope)
        if not mcp_file:
            raise ValueError(f"Scope {scope} is not available")

        # Read existing config
        data = self._read_json_file(mcp_file, {"mcpServers": {}})
        if "mcpServers" not in data:
            return False

        # Check if exists
        if name not in data["mcpServers"]:
            raise ValueError(f"MCP server '{name}' not found in {scope.value} scope")

        # Remove server
        del data["mcpServers"][name]

        # Write back
        if not self._write_json_file(mcp_file, data):
            raise RuntimeError(f"Failed to write MCP config to {mcp_file}")

        return True

    # =========================================================================
    # Utility Methods
    # =========================================================================

    def get_server_count(self) -> int:
        """Get total count of MCP servers across all scopes"""
        all_servers = self.get_all_servers()
        return sum(len(servers) for servers in all_servers.values())

    def validate_server_config(self, config: McpServerConfig) -> List[str]:
        """
        Validate an MCP server configuration.

        Returns list of validation errors (empty if valid).
        """
        errors = []

        if not config.name:
            errors.append("Server name is required")
        elif not config.name.replace("-", "").replace("_", "").isalnum():
            errors.append("Server name must be alphanumeric with hyphens/underscores only")

        if config.type in [McpServerType.STDIO, "stdio"]:
            if not config.command:
                errors.append("Command is required for stdio servers")
        elif config.type in [McpServerType.SSE, McpServerType.HTTP, "sse", "http"]:
            if not config.url:
                errors.append("URL is required for SSE/HTTP servers")

        return errors


# Global service instances (keyed by project_dir)
_mcp_services: Dict[Optional[str], McpService] = {}
_mcp_services_lock = threading.Lock()


def get_mcp_service(project_dir: Optional[Path] = None) -> McpService:
    """
    Get an MCP service instance, optionally scoped to a project directory.

    Args:
        project_dir: Optional project directory for LOCAL/PROJECT scope support.
                    If None, only USER scope is accessible.

    Returns:
        McpService instance (cached per project_dir)
    """
    cache_key = str(project_dir) if project_dir else None

    with _mcp_services_lock:
        if cache_key not in _mcp_services:
            _mcp_services[cache_key] = McpService(project_dir=project_dir)
        return _mcp_services[cache_key]


def clear_mcp_service_cache():
    """Clear the MCP service cache (useful for testing)"""
    global _mcp_services
    with _mcp_services_lock:
        _mcp_services = {}
