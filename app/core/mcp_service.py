"""
MCP Server Management Service

Provides functionality for managing MCP (Model Context Protocol) server configurations.

MCP Scopes:
- USER: ~/.claude.json (global MCP servers in user home, mcpServers key)
- LOCAL: <project>/.claude/.mcp.json (project-local, gitignored)
- PROJECT: <project>/.mcp.json (project MCP config, committed to git)

Enable/disable state is managed via ~/.claude/settings.json (disabledMcpServers key).
Docker mode uses /home/appuser/.claude/ for user scope.
"""

import json
import logging
import threading
from pathlib import Path
from dataclasses import dataclass
from typing import List, Optional, Dict, Any
from enum import Enum

from fastapi import HTTPException

from app.core.platform import is_docker_mode, get_claude_credentials_dir

logger = logging.getLogger(__name__)


# Built-in servers that can no longer be disabled. The browser automation
# suite is always on; a stale ``disabledMcpServers`` entry (including the
# legacy ``claude-in-chrome`` name from before it became a spawned STDIO
# server) is stripped on read so every gate honours the same truth.
_ALWAYS_ON_SERVERS = {"browser"}

_LEGACY_ALWAYS_ON_NAMES = {"claude-in-chrome": "browser"}


def _normalize_disabled_servers(disabled: List[str]) -> List[str]:
    """
    Drop always-on built-ins from a ``disabledMcpServers`` list.

    Legacy built-in names are resolved first so an old disable entry for a
    now-always-on server strips too. Idempotent, order-preserving,
    de-duplicating.
    """
    return [
        name
        for name in dict.fromkeys(disabled)
        if _LEGACY_ALWAYS_ON_NAMES.get(name, name) not in _ALWAYS_ON_SERVERS
    ]


class McpConfigUnreadable(HTTPException):
    """An MCP config file exists but cannot be read, so it must not be rewritten.

    Raised on the read of a read-modify-write: writing back what an
    unreadable file "read as" (an empty object) would wipe it. The API
    answers 409 with this message; the file is left exactly as it was.
    """

    def __init__(self, path: Path):
        self.path = path
        super().__init__(
            status_code=409,
            detail=(
                f"{path} could not be read (it is not a valid JSON object, or another "
                "program was writing it). It was left unchanged; fix or restore it, "
                "then try again."
            ),
        )


class McpScope(str, Enum):
    """Scope for MCP server configuration"""
    USER = "user"       # ~/.claude.json (mcpServers key)
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
    builtin: bool = False
    # When set, this built-in is NOT a spawnable MCP server but a Claude Code
    # CLI integration enabled by passing ``--<cli_flag>`` to the CLI (e.g.
    # ``chrome`` for the "Claude in Chrome" browser integration). Such servers
    # carry no command/url and are skipped by the mcp_servers builder; the flag
    # is applied in build_options_from_profile instead.
    cli_flag: Optional[str] = None

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
            "builtin": self.builtin,
            "cli_flag": self.cli_flag,
        }

    def to_mcp_json_format(self) -> Dict[str, Any]:
        """
        Convert to .mcp.json format for Claude CLI.

        Returns the server config without name/scope/enabled (those are external).
        """
        config: Dict[str, Any] = {}
        if self.description is not None:
            config["description"] = self.description
        if self.type == McpServerType.STDIO or self.type == "stdio":
            if self.command:
                config["command"] = self.command
            if self.args:
                config["args"] = self.args
            if self.env:
                config["env"] = self.env
            return config
        else:
            # SSE or HTTP
            config["type"] = self.type.value if isinstance(self.type, McpServerType) else self.type
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


def _get_user_home() -> Path:
    """
    Get the user home directory based on deployment mode.

    Returns:
        - Docker: /home/appuser
        - Local: ~ (user home)
    """
    if is_docker_mode():
        return Path("/home/appuser")
    return Path.home()


def _get_user_claude_json() -> Path:
    """
    Get the path to ~/.claude.json which stores global MCP server configs.

    Claude Code stores MCP server definitions in ~/.claude.json under the
    'mcpServers' key. This is in the user home directory, NOT inside
    ~/.claude/.

    In Docker mode, falls back to ~/.claude/.mcp.json since the Docker
    volume mount may not include ~/.claude.json from the host.
    """
    if is_docker_mode():
        # Docker: .claude.json may not be mounted. Fall back to .mcp.json
        # inside the .claude directory which IS mounted.
        return _get_claude_dir() / ".mcp.json"
    return _get_user_home() / ".claude.json"


class McpService:
    """
    Service for managing MCP server configurations.

    Reads and writes to configuration files at various scopes:

    MCP Scopes:
        - USER: ~/.claude.json (mcpServers key) - Global MCP servers
        - LOCAL: <project>/.claude/.mcp.json - Project-local (gitignored)
        - PROJECT: <project>/.mcp.json - Project MCP config (committed)

    Enable/disable state is stored in ~/.claude/settings.json
    under the 'disabledMcpServers' key.
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
        self.USER_MCP_FILE = _get_user_claude_json()
        self.SETTINGS_FILE = self.CLAUDE_DIR / "settings.json"

        # Project scope paths. Coerce to Path defensively: callers sometimes
        # hand us a directory as a plain string, and `str / ".claude"` would
        # otherwise blow up with a cryptic "unsupported operand type(s) for /".
        self.project_dir = Path(project_dir) if project_dir else None
        if self.project_dir:
            self.LOCAL_MCP_FILE = self.project_dir / ".claude" / ".mcp.json"
            self.PROJECT_MCP_FILE = self.project_dir / ".mcp.json"
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

    def _read_json_file(self, path: Path, default: Any = None, *, for_update: bool = False) -> Any:
        """Read a JSON object file.

        A missing file reads as ``default`` (``{}``). An unreadable or corrupt
        one (not UTF-8, not JSON, not an object, or caught mid-write by the
        CLI) also reads as ``default`` for display, but with ``for_update``
        it raises :class:`McpConfigUnreadable`: the caller is about to write
        the result back, and writing an empty object would replace the whole
        file (~/.claude.json is the CLI's entire state file).
        """
        fallback = default if default is not None else {}
        try:
            if not path.exists():
                return fallback
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            # Every caller indexes the result as an object; a file holding
            # ``null`` or a list would otherwise raise AttributeError.
            if not isinstance(data, dict):
                raise ValueError("top-level JSON is not an object")
            return data
        except (ValueError, OSError) as e:
            # ValueError covers JSONDecodeError and a non-UTF-8 file.
            logger.error(f"Error reading {path}: {e}")
            if for_update:
                raise McpConfigUnreadable(path) from e
            return fallback

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

    def get_disabled_servers(self) -> List[str]:
        """
        Get the list of globally disabled MCP server names from settings.json.

        Returns:
            List of disabled server names
        """
        data = self._read_json_file(self.SETTINGS_FILE, {})
        return _normalize_disabled_servers(data.get("disabledMcpServers", []))

    def get_project_disabled_servers(self) -> List[str]:
        """
        Get the list of disabled MCP server names for this project.

        Reads from ~/.claude.json → projects[project_path].disabledMcpServers.
        This is the per-project disable list that Claude Code uses.

        Returns:
            List of disabled server names for this project
        """
        if not self.project_dir:
            return []

        data = self._read_json_file(self.USER_MCP_FILE, {})
        projects = data.get("projects", {})

        # Try both forward-slash and original path formats
        project_path_str = str(self.project_dir).replace("\\", "/")
        project_data = projects.get(project_path_str)
        if project_data is None:
            # Try original OS path format
            project_data = projects.get(str(self.project_dir))
        if project_data is None:
            return []

        return project_data.get("disabledMcpServers", [])

    def set_project_server_enabled(self, name: str, enabled: bool) -> bool:
        """
        Enable or disable an MCP server for this project.

        Writes to ~/.claude.json → projects[project_path].disabledMcpServers.

        Args:
            name: Server name
            enabled: True to enable, False to disable

        Returns:
            True if successful
        """
        if not self.project_dir:
            raise ValueError("No project directory set")

        data = self._read_json_file(self.USER_MCP_FILE, {}, for_update=True)
        projects = data.get("projects", {})

        # Find the project entry (try forward-slash then OS path)
        project_path_str = str(self.project_dir).replace("\\", "/")
        project_key = None
        if project_path_str in projects:
            project_key = project_path_str
        elif str(self.project_dir) in projects:
            project_key = str(self.project_dir)

        if project_key is None:
            # Create project entry if it doesn't exist
            project_key = project_path_str
            projects[project_key] = {}

        disabled = projects[project_key].get("disabledMcpServers", [])

        if enabled:
            disabled = [s for s in disabled if s != name]
        else:
            if name not in disabled:
                disabled.append(name)

        projects[project_key]["disabledMcpServers"] = disabled
        data["projects"] = projects
        return self._write_json_file(self.USER_MCP_FILE, data)

    def _find_project_entry(self, data: Dict[str, Any]) -> tuple:
        """
        Find the project entry in ~/.claude.json projects dict.

        Returns:
            (project_key, project_data) or (None, None) if not found.
        """
        projects = data.get("projects", {})
        project_path_str = str(self.project_dir).replace("\\", "/")
        project_data = projects.get(project_path_str)
        if project_data is not None:
            return project_path_str, project_data
        project_data = projects.get(str(self.project_dir))
        if project_data is not None:
            return str(self.project_dir), project_data
        return None, None

    def _ensure_project_entry(self, data: Dict[str, Any]) -> str:
        """
        Find or create the project entry in ~/.claude.json.

        Returns:
            The project key used in the projects dict.
        """
        if "projects" not in data:
            data["projects"] = {}
        project_key, _ = self._find_project_entry(data)
        if project_key is None:
            project_key = str(self.project_dir).replace("\\", "/")
            data["projects"][project_key] = {}
        return project_key

    def get_project_installed_servers(self) -> List[McpServerConfig]:
        """
        Get MCP servers installed for this project.

        Reads from ~/.claude.json → projects[project_path].mcpServers.
        These are per-project installed MCPs managed by Claude Code.

        Returns:
            List of installed MCP server configurations
        """
        if not self.project_dir:
            return []

        data = self._read_json_file(self.USER_MCP_FILE, {})
        _, project_data = self._find_project_entry(data)
        if project_data is None:
            return []

        mcp_servers = project_data.get("mcpServers", {})
        disabled = project_data.get("disabledMcpServers", [])
        servers = []
        for name, config in mcp_servers.items():
            try:
                server = McpServerConfig.from_mcp_json(name, config, "installed")
                if name in disabled:
                    server.enabled = False
                servers.append(server)
            except Exception as e:
                logger.error(f"Error parsing installed MCP server {name}: {e}")

        return servers

    def add_project_installed_server(self, name: str, config: McpServerConfig) -> McpServerConfig:
        """
        Add an MCP server to this project's installed servers.

        Writes to ~/.claude.json → projects[project_path].mcpServers[name].
        """
        if not self.project_dir:
            raise ValueError("No project directory set")

        data = self._read_json_file(self.USER_MCP_FILE, {}, for_update=True)
        project_key = self._ensure_project_entry(data)

        if "mcpServers" not in data["projects"][project_key]:
            data["projects"][project_key]["mcpServers"] = {}

        if name in data["projects"][project_key]["mcpServers"]:
            raise ValueError(f"MCP server '{name}' already exists in installed scope")

        config.name = name
        config.scope = "installed"
        data["projects"][project_key]["mcpServers"][name] = config.to_mcp_json_format()

        if not self._write_json_file(self.USER_MCP_FILE, data):
            raise RuntimeError("Failed to write installed MCP server")

        return config

    def remove_project_installed_server(self, name: str) -> bool:
        """
        Remove an MCP server from this project's installed servers.

        Removes from ~/.claude.json → projects[project_path].mcpServers[name].
        """
        if not self.project_dir:
            raise ValueError("No project directory set")

        data = self._read_json_file(self.USER_MCP_FILE, {}, for_update=True)
        project_key, project_data = self._find_project_entry(data)
        if project_key is None or project_data is None:
            raise ValueError("Project not found in ~/.claude.json")

        mcp_servers = project_data.get("mcpServers", {})
        if name not in mcp_servers:
            raise ValueError(f"MCP server '{name}' not found in installed scope")

        del data["projects"][project_key]["mcpServers"][name]

        if not self._write_json_file(self.USER_MCP_FILE, data):
            raise RuntimeError("Failed to remove installed MCP server")

        return True

    def set_server_enabled(self, name: str, enabled: bool) -> bool:
        """
        Enable or disable a built-in MCP server via settings.json.

        Only used for built-in servers (like browser-tools) that are added
        programmatically rather than from config files. Non-built-in servers
        can only be disabled at the project level, not globally.

        Args:
            name: Server name
            enabled: True to enable, False to disable

        Returns:
            True if successful
        """
        data = self._read_json_file(self.SETTINGS_FILE, {}, for_update=True)
        disabled = _normalize_disabled_servers(data.get("disabledMcpServers", []))

        # Always-on built-ins cannot be disabled (the browser suite has no
        # setting anymore); enabling one is a harmless no-op on the list.
        if not enabled and name in _ALWAYS_ON_SERVERS:
            return True

        if enabled:
            disabled = [s for s in disabled if s != name]
        else:
            if name not in disabled:
                disabled.append(name)

        data["disabledMcpServers"] = disabled
        return self._write_json_file(self.SETTINGS_FILE, data)

    def get_servers_for_scope(self, scope: McpScope) -> List[McpServerConfig]:
        """
        Get all MCP servers configured in a specific scope.

        For USER scope, also merges enabled/disabled state from settings.json.

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

        # Get disabled list for USER scope
        # Per-project disabled list takes priority if we have a project context
        if scope == McpScope.USER and self.project_dir:
            disabled = self.get_project_disabled_servers()
        elif scope == McpScope.USER:
            disabled = self.get_disabled_servers()
        else:
            disabled = []

        servers = []
        for name, config in mcp_servers.items():
            try:
                server = McpServerConfig.from_mcp_json(name, config, scope.value)
                if name in disabled:
                    server.enabled = False
                servers.append(server)
            except Exception as e:
                logger.error(f"Error parsing MCP server {name}: {e}")

        return servers

    def get_builtin_servers(self) -> List[McpServerConfig]:
        """
        Get built-in/managed MCP servers (e.g. the headless browser server).

        These are shipped and managed by AI Shuffle rather than read from
        config files. Their enabled state is resolved from the global
        disable list in settings.json (``disabledMcpServers``) so admins can
        toggle them the same way as other servers.

        Returns:
            List of built-in MCP server configurations
        """
        # Imported lazily to avoid a circular import (builtin_mcp_servers
        # imports McpServerConfig from this module).
        from app.core.builtin_mcp_servers import get_builtin_servers as _builtins

        disabled = self.get_disabled_servers()
        servers = _builtins()
        for server in servers:
            if server.name in disabled:
                server.enabled = False
        return servers

    def get_all_servers(self) -> Dict[str, List[McpServerConfig]]:
        """
        Get all MCP servers from all available scopes.

        Returns:
            Dictionary mapping scope name to list of servers
        """
        result = {}

        # Built-in/managed servers (browser, etc.) — always available
        result["builtin"] = self.get_builtin_servers()

        # Always include USER scope
        result["user"] = list(self.get_servers_for_scope(McpScope.USER))

        # Include project scopes if available
        if self.project_dir:
            result["installed"] = self.get_project_installed_servers()
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

        For USER scope, writes to ~/.claude.json (preserving other keys).
        For LOCAL/PROJECT scopes, writes to .mcp.json files.

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

        # Read existing file (preserve all existing content)
        data = self._read_json_file(mcp_file, {}, for_update=True)
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

        # Read existing file (preserve all existing content)
        data = self._read_json_file(mcp_file, {}, for_update=True)
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

        # Read existing file (preserve all existing content)
        data = self._read_json_file(mcp_file, {}, for_update=True)
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

    def get_all_servers_with_builtins(self) -> List[McpServerConfig]:
        """
        Get all USER scope servers plus built-in/managed servers.

        Built-ins (e.g. the headless browser server) are listed first so the
        admin MCP UI surfaces them alongside user-configured servers.

        Returns:
            List of all globally available MCP servers.
        """
        return self.get_builtin_servers() + list(self.get_servers_for_scope(McpScope.USER))

    def validate_server_config(self, config: McpServerConfig) -> List[str]:
        """
        Validate an MCP server configuration.

        Returns list of validation errors (empty if valid).
        """
        errors = []

        if not config.name:
            errors.append("Server name is required")
        elif not (
            config.name.isascii()
            and config.name.replace("-", "").replace("_", "").isalnum()
        ):
            # ASCII only: str.isalnum() accepts letters and digits from any
            # script, which tool names (mcp__<server>__<tool>) cannot carry.
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


from app.core.resource_sync import install_service_mutations as _install_resource_sync

_install_resource_sync(McpService, 'mcp', 'add_server update_server remove_server set_server_enabled set_project_server_enabled add_project_installed_server remove_project_installed_server')
