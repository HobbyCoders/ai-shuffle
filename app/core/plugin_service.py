"""
Plugin Service - Core business logic for Claude Code plugin management.

This service reads and writes to Claude's native plugin configuration files.

Plugin Scopes:
- USER: ~/.claude/plugins/ (user-global plugins)
- LOCAL: <project>/.claude/plugins/ (project-local, gitignored)
- PROJECT: <project>/plugins/ (project plugins, committed to git)

Docker mode uses /home/appuser/.claude/ for user scope.

It provides a Python API for managing marketplaces, installing/uninstalling plugins,
and enabling/disabling plugins.
"""

import json
import logging
import stat
import subprocess
import shutil
import threading
from pathlib import Path
from datetime import datetime
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field, asdict
from enum import Enum

from app.core.platform import is_docker_mode, get_claude_credentials_dir

logger = logging.getLogger(__name__)


class PluginScope(str, Enum):
    USER = "user"       # ~/.claude/plugins/
    LOCAL = "local"     # <project>/.claude/plugins/
    PROJECT = "project" # <project>/plugins/


@dataclass
class MarketplaceInfo:
    """Information about a plugin marketplace"""
    id: str
    source: str  # "git"
    url: str
    install_location: str
    last_updated: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "url": self.url,
            "install_location": self.install_location,
            "last_updated": self.last_updated.isoformat() if self.last_updated else None
        }


@dataclass
class PluginInstallation:
    """Information about an installed plugin instance"""
    scope: str
    install_path: str
    version: str
    installed_at: datetime
    last_updated: Optional[datetime] = None
    is_local: bool = True


@dataclass
class PluginInfo:
    """Information about a plugin"""
    id: str  # e.g., "frontend-design@claude-plugins-official"
    name: str
    marketplace: str
    description: str
    version: str
    scope: str
    install_path: str
    installed_at: Optional[datetime] = None
    enabled: bool = False
    has_commands: bool = False
    has_agents: bool = False
    has_skills: bool = False
    has_hooks: bool = False
    has_mcp: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "marketplace": self.marketplace,
            "description": self.description,
            "version": self.version,
            "scope": self.scope,
            "install_path": self.install_path,
            "installed_at": self.installed_at.isoformat() if self.installed_at else None,
            "enabled": self.enabled,
            "has_commands": self.has_commands,
            "has_agents": self.has_agents,
            "has_skills": self.has_skills,
            "has_hooks": self.has_hooks,
            "has_mcp": self.has_mcp
        }


@dataclass
class PluginDetails(PluginInfo):
    """Detailed information about a plugin including contents"""
    commands: List[str] = field(default_factory=list)
    agents: List[str] = field(default_factory=list)
    skills: List[str] = field(default_factory=list)
    readme: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        base = super().to_dict()
        base.update({
            "commands": self.commands,
            "agents": self.agents,
            "skills": self.skills,
            "readme": self.readme
        })
        return base


@dataclass
class AvailablePlugin:
    """A plugin available in a marketplace (not necessarily installed)"""
    id: str
    name: str
    marketplace: str
    marketplace_path: str  # Path to plugin in marketplace repo
    description: str
    has_commands: bool = False
    has_agents: bool = False
    has_skills: bool = False
    has_hooks: bool = False
    has_mcp: bool = False
    installed: bool = False
    enabled: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _get_claude_dir() -> Path:
    """
    Get the Claude configuration directory based on deployment mode.

    Returns:
        - Docker: /home/appuser/.claude
        - Local (Windows/macOS/Linux): ~/.claude
    """
    if is_docker_mode():
        return Path("/home/appuser/.claude")
    return get_claude_credentials_dir()


class PluginService:
    """
    Service for managing Claude Code plugins.

    Reads and writes to Claude's native configuration files to maintain
    compatibility with the Claude CLI.

    Plugin Scopes:
        - USER: ~/.claude/plugins/ - User-global plugins
        - LOCAL: <project>/.claude/plugins/ - Project-local (gitignored)
        - PROJECT: <project>/plugins/ - Project plugins (committed)
    """

    def __init__(self, project_dir: Optional[Path] = None):
        """
        Initialize the plugin service.

        Args:
            project_dir: Optional project directory for LOCAL/PROJECT scope plugins.
                        If not provided, only USER scope is available.
        """
        # User scope paths (always available)
        self.CLAUDE_DIR = _get_claude_dir()
        self.PLUGINS_DIR = self.CLAUDE_DIR / "plugins"
        self.SETTINGS_FILE = self.CLAUDE_DIR / "settings.json"
        self.MARKETPLACES_FILE = self.PLUGINS_DIR / "known_marketplaces.json"
        self.INSTALLED_FILE = self.PLUGINS_DIR / "installed_plugins.json"
        self.CACHE_DIR = self.PLUGINS_DIR / "cache"
        self.MARKETPLACES_DIR = self.PLUGINS_DIR / "marketplaces"

        # Project scope paths (optional)
        self.project_dir = project_dir
        if project_dir:
            self.LOCAL_PLUGINS_DIR = project_dir / ".claude" / "plugins"
            self.PROJECT_PLUGINS_DIR = project_dir / "plugins"
        else:
            self.LOCAL_PLUGINS_DIR = None
            self.PROJECT_PLUGINS_DIR = None

        self._ensure_directories()

    def _ensure_directories(self):
        """Ensure required directories exist for USER scope"""
        self.PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
        self.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self.MARKETPLACES_DIR.mkdir(parents=True, exist_ok=True)

    def get_plugins_dir_for_scope(self, scope: PluginScope) -> Optional[Path]:
        """
        Get the plugins directory for a given scope.

        Args:
            scope: The plugin scope (USER, LOCAL, or PROJECT)

        Returns:
            Path to the plugins directory, or None if scope is not available
        """
        if scope == PluginScope.USER:
            return self.PLUGINS_DIR
        elif scope == PluginScope.LOCAL:
            return self.LOCAL_PLUGINS_DIR
        elif scope == PluginScope.PROJECT:
            return self.PROJECT_PLUGINS_DIR
        return None

    def ensure_scope_directory(self, scope: PluginScope) -> Optional[Path]:
        """
        Ensure the plugins directory exists for a given scope.

        Args:
            scope: The plugin scope

        Returns:
            Path to the created/existing directory, or None if scope unavailable
        """
        plugins_dir = self.get_plugins_dir_for_scope(scope)
        if plugins_dir:
            plugins_dir.mkdir(parents=True, exist_ok=True)
        return plugins_dir

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
            temp_path = path.with_suffix('.tmp')
            with open(temp_path, 'w') as f:
                json.dump(data, f, indent=2)
            temp_path.replace(path)
            return True
        except IOError as e:
            logger.error(f"Error writing {path}: {e}")
            return False

    # =========================================================================
    # Marketplace Management
    # =========================================================================

    def get_marketplaces(self) -> List[MarketplaceInfo]:
        """Get all registered marketplaces"""
        data = self._read_json_file(self.MARKETPLACES_FILE, {})
        marketplaces = []

        for mp_id, mp_data in data.items():
            source_info = mp_data.get("source", {})
            last_updated = None
            if mp_data.get("lastUpdated"):
                try:
                    last_updated = datetime.fromisoformat(
                        mp_data["lastUpdated"].replace("Z", "+00:00")
                    )
                except (ValueError, TypeError):
                    pass

            marketplaces.append(MarketplaceInfo(
                id=mp_id,
                source=source_info.get("source", "git"),
                url=source_info.get("url", ""),
                install_location=mp_data.get("installLocation", ""),
                last_updated=last_updated
            ))

        return marketplaces

    def add_marketplace(self, url: str, name: Optional[str] = None) -> MarketplaceInfo:
        """
        Add a new marketplace from a git URL.

        Args:
            url: Git repository URL
            name: Optional marketplace name (derived from URL if not provided)

        Returns:
            MarketplaceInfo for the new marketplace
        """
        # Validate URL before passing to subprocess
        import re
        if not re.match(r'^https?://[\w\-.]+(:\d+)?(/[\w\-./%+@]*)*$', url) and \
           not re.match(r'^git@[\w\-.]+:[\w\-./]+$', url):
            raise ValueError(f"Invalid git URL: {url[:100]}")

        # Derive name from URL if not provided
        if not name:
            name = url.rstrip("/").split("/")[-1]
            if name.endswith(".git"):
                name = name[:-4]

        # Validate derived name (prevent path traversal)
        if not re.match(r'^[\w\-]+$', name):
            raise ValueError(f"Invalid marketplace name: {name[:50]}")

        install_location = str(self.MARKETPLACES_DIR / name)

        # Clone the repository
        if not Path(install_location).exists():
            try:
                subprocess.run(
                    ["git", "clone", "--depth", "1", url, install_location],
                    check=True,
                    capture_output=True,
                    timeout=120
                )
            except subprocess.CalledProcessError as e:
                raise RuntimeError(f"Failed to clone marketplace: {e.stderr.decode()}")
            except subprocess.TimeoutExpired:
                raise RuntimeError("Marketplace clone timed out")

        # Update known_marketplaces.json
        data = self._read_json_file(self.MARKETPLACES_FILE, {})
        data[name] = {
            "source": {
                "source": "git",
                "url": url
            },
            "installLocation": install_location,
            "lastUpdated": datetime.utcnow().isoformat() + "Z"
        }
        self._write_json_file(self.MARKETPLACES_FILE, data)

        return MarketplaceInfo(
            id=name,
            source="git",
            url=url,
            install_location=install_location,
            last_updated=datetime.utcnow()
        )

    def remove_marketplace(self, marketplace_id: str) -> bool:
        """
        Remove a marketplace and all its installed plugins.

        Args:
            marketplace_id: The marketplace identifier

        Returns:
            True if successful
        """
        data = self._read_json_file(self.MARKETPLACES_FILE, {})

        if marketplace_id not in data:
            raise ValueError(f"Marketplace not found: {marketplace_id}")

        install_location = data[marketplace_id].get("installLocation")

        # Remove from known_marketplaces.json
        del data[marketplace_id]
        self._write_json_file(self.MARKETPLACES_FILE, data)

        # Remove marketplace directory
        if install_location and Path(install_location).exists():
            shutil.rmtree(install_location, ignore_errors=True)

        # Remove installed plugins from this marketplace
        installed = self._read_json_file(self.INSTALLED_FILE, {"version": 2, "plugins": {}})
        plugins_to_remove = [
            pid for pid in installed.get("plugins", {})
            if pid.endswith(f"@{marketplace_id}")
        ]
        for pid in plugins_to_remove:
            del installed["plugins"][pid]
        self._write_json_file(self.INSTALLED_FILE, installed)

        # Remove from enabled plugins in settings
        settings = self._read_json_file(self.SETTINGS_FILE, {})
        enabled = settings.get("enabledPlugins", {})
        for pid in plugins_to_remove:
            enabled.pop(pid, None)
        settings["enabledPlugins"] = enabled
        self._write_json_file(self.SETTINGS_FILE, settings)

        return True

    def sync_marketplace(self, marketplace_id: str) -> bool:
        """
        Sync a marketplace with its remote repository.

        Args:
            marketplace_id: The marketplace identifier

        Returns:
            True if successful
        """
        data = self._read_json_file(self.MARKETPLACES_FILE, {})

        if marketplace_id not in data:
            raise ValueError(f"Marketplace not found: {marketplace_id}")

        install_location = data[marketplace_id].get("installLocation")

        if not install_location or not Path(install_location).exists():
            raise RuntimeError(f"Marketplace directory not found: {install_location}")

        # Git pull
        try:
            subprocess.run(
                ["git", "-C", install_location, "pull", "--ff-only"],
                check=True,
                capture_output=True,
                timeout=120
            )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to sync marketplace: {e.stderr.decode()}")
        except subprocess.TimeoutExpired:
            raise RuntimeError("Marketplace sync timed out")

        # Update lastUpdated
        data[marketplace_id]["lastUpdated"] = datetime.utcnow().isoformat() + "Z"
        self._write_json_file(self.MARKETPLACES_FILE, data)

        return True

    # =========================================================================
    # Available Plugins (from marketplaces)
    # =========================================================================

    def get_available_plugins(self, marketplace: Optional[str] = None) -> List[AvailablePlugin]:
        """
        Get all available plugins from marketplaces.

        Scans both 'plugins/' (first-party) and 'external_plugins/' (third-party/MCP)
        directories within each marketplace.

        Args:
            marketplace: Optional filter by marketplace ID

        Returns:
            List of available plugins
        """
        marketplaces = self.get_marketplaces()
        installed_data = self._read_json_file(self.INSTALLED_FILE, {"version": 2, "plugins": {}})
        installed_plugins = set(installed_data.get("plugins", {}).keys())

        settings = self._read_json_file(self.SETTINGS_FILE, {})
        enabled_plugins = settings.get("enabledPlugins", {})

        available = []

        for mp in marketplaces:
            if marketplace and mp.id != marketplace:
                continue

            mp_path = Path(mp.install_location)

            # Scan both plugins/ and external_plugins/ directories
            plugin_dirs_to_scan = [
                mp_path / "plugins",
                mp_path / "external_plugins",
            ]

            for plugins_dir in plugin_dirs_to_scan:
                if not plugins_dir.exists():
                    continue

                for plugin_dir in plugins_dir.iterdir():
                    if not plugin_dir.is_dir():
                        continue

                    plugin_name = plugin_dir.name
                    plugin_id = f"{plugin_name}@{mp.id}"

                    # Check if this is an external plugin (has .claude-plugin/plugin.json)
                    is_external = (plugin_dir / ".claude-plugin" / "plugin.json").exists()

                    # Get description based on plugin type
                    description = ""
                    if is_external:
                        # External plugins: read from .claude-plugin/plugin.json
                        manifest_path = plugin_dir / ".claude-plugin" / "plugin.json"
                        try:
                            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
                            description = manifest.get("description", "")[:200]
                        except (IOError, json.JSONDecodeError, UnicodeDecodeError):
                            pass
                    else:
                        # Regular plugins: read from README.md
                        readme_path = plugin_dir / "README.md"
                        if readme_path.exists():
                            try:
                                content = readme_path.read_text(encoding='utf-8')
                                # Extract first paragraph as description
                                lines = content.strip().split("\n")
                                for line in lines:
                                    line = line.strip()
                                    if line and not line.startswith("#"):
                                        description = line[:200]
                                        break
                            except (IOError, UnicodeDecodeError):
                                pass

                    # Check what components the plugin has
                    has_commands = (plugin_dir / "commands").exists()
                    has_agents = (plugin_dir / "agents").exists()
                    has_skills = (plugin_dir / "skills").exists()
                    has_hooks = (plugin_dir / "hooks").exists() or (plugin_dir / "hooks.json").exists()
                    has_mcp = (plugin_dir / ".mcp.json").exists()

                    available.append(AvailablePlugin(
                        id=plugin_id,
                        name=plugin_name,
                        marketplace=mp.id,
                        marketplace_path=str(plugin_dir),
                        description=description,
                        has_commands=has_commands,
                        has_agents=has_agents,
                        has_skills=has_skills,
                        has_hooks=has_hooks,
                        has_mcp=has_mcp,
                        installed=plugin_id in installed_plugins,
                        enabled=enabled_plugins.get(plugin_id, False)
                    ))

        return available

    # =========================================================================
    # Installed Plugins
    # =========================================================================

    def get_installed_plugins(self) -> List[PluginInfo]:
        """Get all installed plugins"""
        installed_data = self._read_json_file(self.INSTALLED_FILE, {"version": 2, "plugins": {}})
        settings = self._read_json_file(self.SETTINGS_FILE, {})
        enabled_plugins = settings.get("enabledPlugins", {})

        plugins = []
        file_version = installed_data.get("version", 1)

        for plugin_id, installation_data in installed_data.get("plugins", {}).items():
            if not installation_data:
                continue

            # Handle both version 1 (single object) and version 2 (array) formats
            if file_version == 1 or isinstance(installation_data, dict):
                # Version 1: plugins are single objects
                install = installation_data
            else:
                # Version 2: plugins are arrays
                install = installation_data[0] if installation_data else None
                if not install:
                    continue
            parts = plugin_id.rsplit("@", 1)
            name = parts[0]
            marketplace = parts[1] if len(parts) > 1 else "local"

            install_path = Path(install.get("installPath", ""))

            # Parse dates
            installed_at = None
            if install.get("installedAt"):
                try:
                    installed_at = datetime.fromisoformat(
                        install["installedAt"].replace("Z", "+00:00")
                    )
                except (ValueError, TypeError):
                    pass

            # Check components
            has_commands = (install_path / "commands").exists() if install_path.exists() else False
            has_agents = (install_path / "agents").exists() if install_path.exists() else False
            has_skills = (install_path / "skills").exists() if install_path.exists() else False
            has_hooks = ((install_path / "hooks").exists() or
                        (install_path / "hooks.json").exists()) if install_path.exists() else False
            has_mcp = (install_path / ".mcp.json").exists() if install_path.exists() else False

            # Check if this is an external plugin (has .claude-plugin/plugin.json)
            is_external = (install_path / ".claude-plugin" / "plugin.json").exists() if install_path.exists() else False

            # Get description based on plugin type
            description = ""
            if is_external:
                # External plugins: read from .claude-plugin/plugin.json
                manifest_path = install_path / ".claude-plugin" / "plugin.json"
                try:
                    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
                    description = manifest.get("description", "")[:200]
                except (IOError, json.JSONDecodeError, UnicodeDecodeError):
                    pass
            else:
                # Regular plugins: read from README.md
                readme_path = install_path / "README.md"
                if readme_path.exists():
                    try:
                        content = readme_path.read_text(encoding='utf-8')
                        lines = content.strip().split("\n")
                        for line in lines:
                            line = line.strip()
                            if line and not line.startswith("#"):
                                description = line[:200]
                                break
                    except (IOError, UnicodeDecodeError):
                        pass

            plugins.append(PluginInfo(
                id=plugin_id,
                name=name,
                marketplace=marketplace,
                description=description,
                version=install.get("version", "unknown"),
                scope=install.get("scope", "user"),
                install_path=str(install_path),
                installed_at=installed_at,
                enabled=enabled_plugins.get(plugin_id, False),
                has_commands=has_commands,
                has_agents=has_agents,
                has_skills=has_skills,
                has_hooks=has_hooks,
                has_mcp=has_mcp
            ))

        return plugins

    def get_plugin_details(self, plugin_id: str) -> Optional[PluginDetails]:
        """Get detailed information about a specific plugin"""
        installed_data = self._read_json_file(self.INSTALLED_FILE, {"version": 2, "plugins": {}})
        settings = self._read_json_file(self.SETTINGS_FILE, {})
        enabled_plugins = settings.get("enabledPlugins", {})
        file_version = installed_data.get("version", 1)

        installation_data = installed_data.get("plugins", {}).get(plugin_id)
        if not installation_data:
            return None

        # Handle both version 1 (single object) and version 2 (array) formats
        if file_version == 1 or isinstance(installation_data, dict):
            install = installation_data
        else:
            install = installation_data[0] if installation_data else None
            if not install:
                return None
        parts = plugin_id.rsplit("@", 1)
        name = parts[0]
        marketplace = parts[1] if len(parts) > 1 else "local"

        install_path = Path(install.get("installPath", ""))

        if not install_path.exists():
            return None

        # Parse dates
        installed_at = None
        if install.get("installedAt"):
            try:
                installed_at = datetime.fromisoformat(
                    install["installedAt"].replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                pass

        # Collect commands
        commands = []
        commands_dir = install_path / "commands"
        if commands_dir.exists():
            for cmd_file in commands_dir.glob("**/*.md"):
                cmd_name = cmd_file.stem
                commands.append(cmd_name)

        # Collect agents
        agents = []
        agents_dir = install_path / "agents"
        if agents_dir.exists():
            for agent_file in agents_dir.glob("**/*.md"):
                agent_name = agent_file.stem
                agents.append(agent_name)

        # Collect skills
        skills = []
        skills_dir = install_path / "skills"
        if skills_dir.exists():
            for skill_dir in skills_dir.iterdir():
                if skill_dir.is_dir() and (skill_dir / "SKILL.md").exists():
                    skills.append(skill_dir.name)

        # Check if this is an external plugin (has .claude-plugin/plugin.json)
        is_external = (install_path / ".claude-plugin" / "plugin.json").exists()
        has_mcp = (install_path / ".mcp.json").exists()

        # Read README
        readme = None
        readme_path = install_path / "README.md"
        if readme_path.exists():
            try:
                readme = readme_path.read_text(encoding='utf-8')
            except (IOError, UnicodeDecodeError):
                pass

        # Get description based on plugin type
        description = ""
        if is_external:
            # External plugins: read from .claude-plugin/plugin.json
            manifest_path = install_path / ".claude-plugin" / "plugin.json"
            try:
                manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
                description = manifest.get("description", "")[:200]
            except (IOError, json.JSONDecodeError, UnicodeDecodeError):
                pass
        elif readme:
            # Regular plugins: extract from README
            lines = readme.strip().split("\n")
            for line in lines:
                line = line.strip()
                if line and not line.startswith("#"):
                    description = line[:200]
                    break

        return PluginDetails(
            id=plugin_id,
            name=name,
            marketplace=marketplace,
            description=description,
            version=install.get("version", "unknown"),
            scope=install.get("scope", "user"),
            install_path=str(install_path),
            installed_at=installed_at,
            enabled=enabled_plugins.get(plugin_id, False),
            has_commands=bool(commands),
            has_agents=bool(agents),
            has_skills=bool(skills),
            has_hooks=(install_path / "hooks").exists() or (install_path / "hooks.json").exists(),
            has_mcp=has_mcp,
            commands=commands,
            agents=agents,
            skills=skills,
            readme=readme
        )

    def get_available_plugin_details(self, plugin_id: str) -> Optional[PluginDetails]:
        """Get detailed information about an available (not-yet-installed) plugin from its marketplace path."""
        available = self.get_available_plugins()
        plugin = next((p for p in available if p.id == plugin_id), None)
        if not plugin:
            return None

        plugin_path = Path(plugin.marketplace_path)
        if not plugin_path.exists():
            return None

        parts = plugin_id.rsplit("@", 1)
        name = parts[0]
        marketplace = parts[1] if len(parts) > 1 else "unknown"

        # Collect commands
        commands = []
        commands_dir = plugin_path / "commands"
        if commands_dir.exists():
            for cmd_file in commands_dir.glob("**/*.md"):
                commands.append(cmd_file.stem)

        # Collect agents
        agents = []
        agents_dir = plugin_path / "agents"
        if agents_dir.exists():
            for agent_file in agents_dir.glob("**/*.md"):
                agents.append(agent_file.stem)

        # Collect skills
        skills = []
        skills_dir = plugin_path / "skills"
        if skills_dir.exists():
            for skill_dir in skills_dir.iterdir():
                if skill_dir.is_dir() and (skill_dir / "SKILL.md").exists():
                    skills.append(skill_dir.name)

        has_hooks = (plugin_path / "hooks").exists() or (plugin_path / "hooks.json").exists()
        has_mcp = (plugin_path / ".mcp.json").exists()

        # Read README
        readme = None
        readme_path = plugin_path / "README.md"
        if readme_path.exists():
            try:
                readme = readme_path.read_text(encoding='utf-8')
            except (IOError, UnicodeDecodeError):
                pass

        return PluginDetails(
            id=plugin_id,
            name=name,
            marketplace=marketplace,
            description=plugin.description,
            version="available",
            scope="marketplace",
            install_path=str(plugin_path),
            installed_at=None,
            enabled=False,
            has_commands=bool(commands),
            has_agents=bool(agents),
            has_skills=bool(skills),
            has_hooks=has_hooks,
            has_mcp=has_mcp,
            commands=commands,
            agents=agents,
            skills=skills,
            readme=readme
        )

    # =========================================================================
    # Plugin Installation
    # =========================================================================

    def install_plugin(self, plugin_id: str, scope: str = "user") -> PluginInfo:
        """
        Install a plugin from a marketplace.

        Args:
            plugin_id: Plugin ID in format "name@marketplace"
            scope: Installation scope ("user" or "project")

        Returns:
            PluginInfo for the installed plugin
        """
        parts = plugin_id.rsplit("@", 1)
        if len(parts) != 2:
            raise ValueError(f"Invalid plugin ID format: {plugin_id}")

        plugin_name, marketplace_id = parts

        # Find the plugin in marketplace
        marketplaces = self._read_json_file(self.MARKETPLACES_FILE, {})
        if marketplace_id not in marketplaces:
            raise ValueError(f"Marketplace not found: {marketplace_id}")

        mp_location = marketplaces[marketplace_id].get("installLocation")

        # Check both plugins/ and external_plugins/ directories
        plugin_source = Path(mp_location) / "plugins" / plugin_name
        if not plugin_source.exists():
            plugin_source = Path(mp_location) / "external_plugins" / plugin_name

        if not plugin_source.exists():
            raise ValueError(f"Plugin not found in marketplace: {plugin_name}")

        # Get version (git commit hash or version from plugin.json)
        version = "1.0.0"
        try:
            result = subprocess.run(
                ["git", "-C", mp_location, "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0:
                version = result.stdout.strip()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass

        # Create cache directory for this plugin version
        cache_path = self.CACHE_DIR / marketplace_id / plugin_name / version
        cache_path.parent.mkdir(parents=True, exist_ok=True)

        # Copy plugin to cache
        if cache_path.exists():
            shutil.rmtree(cache_path)
        shutil.copytree(plugin_source, cache_path)

        # Ensure scripts have execute permissions
        # This is needed because git may not preserve executable bits
        for script in cache_path.rglob("*.sh"):
            current_mode = script.stat().st_mode
            script.chmod(current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        # Update installed_plugins.json
        installed = self._read_json_file(self.INSTALLED_FILE, {"version": 2, "plugins": {}})
        if "plugins" not in installed:
            installed["plugins"] = {}

        now = datetime.utcnow().isoformat() + "Z"
        installed["plugins"][plugin_id] = [{
            "scope": scope,
            "installPath": str(cache_path),
            "version": version,
            "installedAt": now,
            "lastUpdated": now,
            "isLocal": True
        }]
        self._write_json_file(self.INSTALLED_FILE, installed)

        # Enable by default
        self.enable_plugin(plugin_id)

        # Return plugin info
        return self.get_installed_plugins()[-1]  # Get the one we just added

    def install_plugins_batch(self, plugin_ids: List[str], scope: str = "user") -> List[PluginInfo]:
        """
        Install multiple plugins in batch.

        Args:
            plugin_ids: List of plugin IDs to install
            scope: Installation scope

        Returns:
            List of installed plugin info
        """
        results = []
        for plugin_id in plugin_ids:
            try:
                result = self.install_plugin(plugin_id, scope)
                results.append(result)
            except Exception as e:
                logger.error(f"Failed to install plugin {plugin_id}: {e}")
        return results

    def uninstall_plugin(self, plugin_id: str) -> bool:
        """
        Uninstall a plugin.

        Args:
            plugin_id: Plugin ID to uninstall

        Returns:
            True if successful
        """
        installed = self._read_json_file(self.INSTALLED_FILE, {"version": 2, "plugins": {}})
        file_version = installed.get("version", 1)

        if plugin_id not in installed.get("plugins", {}):
            raise ValueError(f"Plugin not installed: {plugin_id}")

        installation_data = installed["plugins"][plugin_id]

        # Handle both version 1 (single object) and version 2 (array) formats
        if file_version == 1 or isinstance(installation_data, dict):
            installations = [installation_data]
        else:
            installations = installation_data if installation_data else []

        # Remove from filesystem
        for install in installations:
            install_path = Path(install.get("installPath", ""))
            if install_path.exists():
                shutil.rmtree(install_path, ignore_errors=True)

        # Remove from installed_plugins.json
        del installed["plugins"][plugin_id]
        self._write_json_file(self.INSTALLED_FILE, installed)

        # Remove from enabled plugins
        settings = self._read_json_file(self.SETTINGS_FILE, {})
        enabled = settings.get("enabledPlugins", {})
        enabled.pop(plugin_id, None)
        settings["enabledPlugins"] = enabled
        self._write_json_file(self.SETTINGS_FILE, settings)

        return True

    # =========================================================================
    # Plugin Enable/Disable
    # =========================================================================

    def enable_plugin(self, plugin_id: str) -> bool:
        """Enable a plugin globally"""
        settings = self._read_json_file(self.SETTINGS_FILE, {})
        if "enabledPlugins" not in settings:
            settings["enabledPlugins"] = {}
        settings["enabledPlugins"][plugin_id] = True
        return self._write_json_file(self.SETTINGS_FILE, settings)

    def disable_plugin(self, plugin_id: str) -> bool:
        """Disable a plugin globally"""
        settings = self._read_json_file(self.SETTINGS_FILE, {})
        if "enabledPlugins" not in settings:
            settings["enabledPlugins"] = {}
        settings["enabledPlugins"][plugin_id] = False
        return self._write_json_file(self.SETTINGS_FILE, settings)

    def get_enabled_plugins(self) -> Dict[str, bool]:
        """Get all plugin enable/disable states"""
        settings = self._read_json_file(self.SETTINGS_FILE, {})
        return settings.get("enabledPlugins", {})

    # =========================================================================
    # Project-Scoped Plugins
    # =========================================================================

    def _get_project_settings_file(self) -> Optional[Path]:
        """Get the project-local settings file path"""
        if self.project_dir:
            return self.project_dir / ".claude" / "settings.json"
        return None

    def _read_project_settings(self) -> Dict[str, Any]:
        """Read project-local settings"""
        settings_file = self._get_project_settings_file()
        if settings_file:
            return self._read_json_file(settings_file, {})
        return {}

    def _write_project_settings(self, data: Dict[str, Any]) -> bool:
        """Write project-local settings"""
        settings_file = self._get_project_settings_file()
        if settings_file:
            settings_file.parent.mkdir(parents=True, exist_ok=True)
            return self._write_json_file(settings_file, data)
        return False

    def get_project_installed_plugins(self) -> List[PluginInfo]:
        """
        Get all plugins installed in the project's LOCAL and PROJECT scope directories.

        This discovers plugins by scanning the directories, not from a manifest.
        """
        plugins = []

        if not self.project_dir:
            return plugins

        project_settings = self._read_project_settings()
        enabled_plugins = project_settings.get("enabledPlugins", {})

        # Scan both LOCAL and PROJECT scope directories
        scope_dirs = [
            (PluginScope.LOCAL, self.LOCAL_PLUGINS_DIR),
            (PluginScope.PROJECT, self.PROJECT_PLUGINS_DIR),
        ]

        for scope, plugins_dir in scope_dirs:
            logger.debug(f"[get_project_installed_plugins] Checking scope={scope.value}, dir={plugins_dir}, exists={plugins_dir.exists() if plugins_dir else False}")
            if not plugins_dir or not plugins_dir.exists():
                continue

            for plugin_dir in plugins_dir.iterdir():
                if not plugin_dir.is_dir():
                    continue

                # Skip hidden directories
                if plugin_dir.name.startswith('.'):
                    continue

                plugin_name = plugin_dir.name
                plugin_id = f"{plugin_name}@project-{scope.value}"

                # Read plugin.json for metadata if exists
                # Check both root and .claude-plugin/ directory
                plugin_json = plugin_dir / "plugin.json"
                if not plugin_json.exists():
                    plugin_json = plugin_dir / ".claude-plugin" / "plugin.json"
                version = "1.0.0"
                if plugin_json.exists():
                    try:
                        with open(plugin_json) as f:
                            data = json.load(f)
                        version = data.get("version", version)
                    except (json.JSONDecodeError, IOError):
                        pass

                # Read description from README
                # Check if this is an external plugin (has .claude-plugin/plugin.json)
                is_external = (plugin_dir / ".claude-plugin" / "plugin.json").exists()

                # Get description based on plugin type
                description = ""
                if is_external:
                    # External plugins: read from .claude-plugin/plugin.json
                    manifest_path = plugin_dir / ".claude-plugin" / "plugin.json"
                    try:
                        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
                        description = manifest.get("description", "")[:200]
                    except (IOError, json.JSONDecodeError, UnicodeDecodeError):
                        pass
                else:
                    # Regular plugins: read from README.md
                    readme_path = plugin_dir / "README.md"
                    if readme_path.exists():
                        try:
                            content = readme_path.read_text(encoding='utf-8')
                            lines = content.strip().split("\n")
                            for line in lines:
                                line = line.strip()
                                if line and not line.startswith("#"):
                                    description = line[:200]
                                    break
                        except (IOError, UnicodeDecodeError):
                            pass

                # Check components
                has_commands = (plugin_dir / "commands").exists()
                has_agents = (plugin_dir / "agents").exists()
                has_skills = (plugin_dir / "skills").exists()
                has_hooks = (plugin_dir / "hooks").exists() or (plugin_dir / "hooks.json").exists()
                has_mcp = (plugin_dir / ".mcp.json").exists()

                # Get file timestamps
                dir_stat = plugin_dir.stat()
                installed_at = datetime.fromtimestamp(dir_stat.st_ctime)

                plugins.append(PluginInfo(
                    id=plugin_id,
                    name=plugin_name,
                    marketplace="project",
                    description=description,
                    version=version,
                    scope=scope.value,
                    install_path=str(plugin_dir),
                    installed_at=installed_at,
                    enabled=enabled_plugins.get(plugin_id, True),  # Default enabled
                    has_commands=has_commands,
                    has_agents=has_agents,
                    has_skills=has_skills,
                    has_hooks=has_hooks,
                    has_mcp=has_mcp
                ))

        return plugins

    def install_plugin_to_project(
        self,
        plugin_id: str,
        scope: PluginScope = PluginScope.LOCAL
    ) -> PluginInfo:
        """
        Install a plugin from a marketplace to the project directory.

        Args:
            plugin_id: Plugin ID in format "name@marketplace"
            scope: LOCAL (.claude/plugins/) or PROJECT (plugins/)

        Returns:
            PluginInfo for the installed plugin
        """
        if not self.project_dir:
            raise ValueError("No project directory configured")

        if scope not in (PluginScope.LOCAL, PluginScope.PROJECT):
            raise ValueError(f"Invalid scope for project plugin: {scope}")

        parts = plugin_id.rsplit("@", 1)
        if len(parts) != 2:
            raise ValueError(f"Invalid plugin ID format: {plugin_id}")

        plugin_name, marketplace_id = parts

        # Find the plugin in marketplace
        marketplaces = self._read_json_file(self.MARKETPLACES_FILE, {})
        if marketplace_id not in marketplaces:
            raise ValueError(f"Marketplace not found: {marketplace_id}")

        mp_location = marketplaces[marketplace_id].get("installLocation")

        # Check both plugins/ and external_plugins/ directories
        plugin_source = Path(mp_location) / "plugins" / plugin_name
        if not plugin_source.exists():
            plugin_source = Path(mp_location) / "external_plugins" / plugin_name

        if not plugin_source.exists():
            raise ValueError(f"Plugin not found in marketplace: {plugin_name}")

        # Determine target directory
        target_dir = self.get_plugins_dir_for_scope(scope)
        if not target_dir:
            raise ValueError(f"Cannot get directory for scope: {scope}")

        target_path = target_dir / plugin_name

        # Create target directory
        target_dir.mkdir(parents=True, exist_ok=True)

        # Copy plugin to target
        if target_path.exists():
            shutil.rmtree(target_path)
        shutil.copytree(plugin_source, target_path)

        # Ensure scripts have execute permissions
        for script in target_path.rglob("*.sh"):
            current_mode = script.stat().st_mode
            script.chmod(current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        # Enable by default in project settings
        project_plugin_id = f"{plugin_name}@project-{scope.value}"
        self.enable_project_plugin(project_plugin_id)

        # Return plugin info
        plugins = self.get_project_installed_plugins()
        for p in plugins:
            if p.id == project_plugin_id:
                return p

        # Fallback: construct plugin info
        stat_info = target_path.stat()
        return PluginInfo(
            id=project_plugin_id,
            name=plugin_name,
            marketplace="project",
            description="",
            version="1.0.0",
            scope=scope.value,
            install_path=str(target_path),
            installed_at=datetime.fromtimestamp(stat_info.st_ctime),
            enabled=True,
            has_commands=(target_path / "commands").exists(),
            has_agents=(target_path / "agents").exists(),
            has_skills=(target_path / "skills").exists(),
            has_hooks=(target_path / "hooks").exists() or (target_path / "hooks.json").exists(),
            has_mcp=(target_path / ".mcp.json").exists()
        )

    def uninstall_project_plugin(self, plugin_id: str) -> bool:
        """
        Uninstall a plugin from the project directory.

        Args:
            plugin_id: Plugin ID in format "name@project-local" or "name@project-project"

        Returns:
            True if successful
        """
        if not self.project_dir:
            raise ValueError("No project directory configured")

        # Parse plugin_id to get name and scope
        parts = plugin_id.rsplit("@", 1)
        if len(parts) != 2:
            raise ValueError(f"Invalid plugin ID format: {plugin_id}")

        plugin_name, scope_marker = parts

        # Validate plugin_name to prevent path traversal
        if '/' in plugin_name or '\\' in plugin_name or '..' in plugin_name:
            raise ValueError(f"Invalid plugin name: {plugin_name}")

        # Determine scope from marker
        if scope_marker == "project-local":
            scope = PluginScope.LOCAL
        elif scope_marker == "project-project":
            scope = PluginScope.PROJECT
        else:
            raise ValueError(f"Invalid project plugin ID: {plugin_id}")

        # Get plugin directory
        plugins_dir = self.get_plugins_dir_for_scope(scope)
        if not plugins_dir:
            raise ValueError(f"Cannot get directory for scope: {scope}")

        plugin_path = plugins_dir / plugin_name

        if not plugin_path.exists():
            raise ValueError(f"Plugin not found: {plugin_name}")

        # Remove plugin directory
        shutil.rmtree(plugin_path)

        # Remove from enabled plugins in project settings
        settings = self._read_project_settings()
        enabled = settings.get("enabledPlugins", {})
        enabled.pop(plugin_id, None)
        settings["enabledPlugins"] = enabled
        self._write_project_settings(settings)

        return True

    def enable_project_plugin(self, plugin_id: str) -> bool:
        """Enable a plugin in project scope"""
        settings = self._read_project_settings()
        if "enabledPlugins" not in settings:
            settings["enabledPlugins"] = {}
        settings["enabledPlugins"][plugin_id] = True
        return self._write_project_settings(settings)

    def disable_project_plugin(self, plugin_id: str) -> bool:
        """Disable a plugin in project scope"""
        settings = self._read_project_settings()
        if "enabledPlugins" not in settings:
            settings["enabledPlugins"] = {}
        settings["enabledPlugins"][plugin_id] = False
        return self._write_project_settings(settings)

    def get_project_enabled_plugins(self) -> Dict[str, bool]:
        """Get all project plugin enable/disable states"""
        settings = self._read_project_settings()
        return settings.get("enabledPlugins", {})

    # =========================================================================
    # File-based Agents (from plugins)
    # =========================================================================

    def get_file_based_agents(self) -> List[Dict[str, Any]]:
        """
        Get all file-based agents from installed and enabled plugins.

        Returns:
            List of agent definitions with their source plugin info
        """
        installed = self.get_installed_plugins()
        enabled = self.get_enabled_plugins()
        agents = []

        for plugin in installed:
            if not enabled.get(plugin.id, False):
                continue

            install_path = Path(plugin.install_path)
            agents_dir = install_path / "agents"

            if not agents_dir.exists():
                continue

            for agent_file in agents_dir.glob("**/*.md"):
                try:
                    content = agent_file.read_text(encoding='utf-8')

                    # Parse YAML frontmatter
                    agent_data = self._parse_agent_frontmatter(content)
                    agent_data["id"] = f"{plugin.id}:{agent_file.stem}"
                    agent_data["name"] = agent_file.stem
                    agent_data["source_plugin"] = plugin.id
                    agent_data["file_path"] = str(agent_file)

                    agents.append(agent_data)
                except IOError as e:
                    logger.error(f"Failed to read agent file {agent_file}: {e}")

        return agents

    def _parse_agent_frontmatter(self, content: str) -> Dict[str, Any]:
        """Parse YAML frontmatter from an agent markdown file"""
        result = {
            "description": "",
            "model": None,
            "tools": None,
            "prompt": ""
        }

        lines = content.split("\n")
        if not lines or lines[0].strip() != "---":
            result["prompt"] = content
            return result

        # Find end of frontmatter
        end_idx = -1
        for i, line in enumerate(lines[1:], 1):
            if line.strip() == "---":
                end_idx = i
                break

        if end_idx == -1:
            result["prompt"] = content
            return result

        # Parse frontmatter (simple YAML parsing)
        frontmatter_lines = lines[1:end_idx]
        for line in frontmatter_lines:
            if ":" in line:
                key, value = line.split(":", 1)
                key = key.strip().lower()
                value = value.strip()

                if key == "description":
                    result["description"] = value
                elif key == "model":
                    result["model"] = value
                elif key == "tools":
                    # Parse comma-separated tools
                    result["tools"] = [t.strip() for t in value.split(",") if t.strip()]

        # Rest is the prompt
        result["prompt"] = "\n".join(lines[end_idx + 1:]).strip()

        return result


# Global service instance cache (keyed by project_dir)
_plugin_services: Dict[Optional[str], PluginService] = {}
_plugin_services_lock = threading.Lock()


def get_plugin_service(project_dir: Optional[Path] = None) -> PluginService:
    """
    Get a plugin service instance, optionally scoped to a project directory.

    Args:
        project_dir: Optional project directory for LOCAL/PROJECT scope support.
                    If None, only USER scope plugins are accessible.

    Returns:
        PluginService instance (cached per project_dir)
    """
    cache_key = str(project_dir) if project_dir else None

    with _plugin_services_lock:
        if cache_key not in _plugin_services:
            _plugin_services[cache_key] = PluginService(project_dir=project_dir)
        return _plugin_services[cache_key]


def clear_plugin_service_cache():
    """Clear the plugin service cache (useful for testing)"""
    global _plugin_services
    with _plugin_services_lock:
        _plugin_services = {}


def get_all_plugin_hooks() -> List[Dict[str, Any]]:
    """
    Get all hooks contributed by globally installed plugins.

    Returns a list of hook info dicts with:
    - plugin_id: Plugin ID
    - plugin_name: Plugin display name
    - event: Hook event type
    - hook_type: 'prompt' or 'command'
    - description: Optional description
    - tool_matcher: Optional tool name pattern
    """
    service = get_plugin_service()
    plugins = service.get_installed_plugins()
    return get_hooks_from_plugins(plugins)


def get_hooks_from_plugins(plugins: List["PluginInfo"]) -> List[Dict[str, Any]]:
    """
    Get all hooks from a list of plugins.

    This is a generalized version that works with any list of plugins,
    whether global or project-scoped.

    Returns a list of hook info dicts.
    """
    result = []

    logger.debug(f"[get_hooks_from_plugins] Processing {len(plugins)} plugins")

    for plugin in plugins:
        plugin_dir = Path(plugin.install_path) if plugin.install_path else None
        logger.debug(f"[get_hooks_from_plugins] Plugin {plugin.id}: install_path={plugin.install_path}, dir_exists={plugin_dir.exists() if plugin_dir else False}")
        if not plugin_dir or not plugin_dir.exists():
            continue

        # Check for hooks directory
        hooks_dir = plugin_dir / "hooks"
        if hooks_dir.exists() and hooks_dir.is_dir():
            for hook_file in hooks_dir.glob("*.md"):
                # Parse hook file to extract metadata
                hook_info = _parse_plugin_hook_file(hook_file, plugin)
                if hook_info:
                    result.append(hook_info)

        # Check hooks/hooks.json for hook definitions
        hooks_json = hooks_dir / "hooks.json" if hooks_dir and hooks_dir.exists() else None
        logger.debug(f"[get_hooks_from_plugins] Plugin {plugin.id}: hooks_json={hooks_json}, exists={hooks_json.exists() if hooks_json else False}")
        if hooks_json and hooks_json.exists():
            try:
                import json
                with open(hooks_json) as f:
                    hooks_data = json.load(f)

                logger.debug(f"[get_hooks_from_plugins] Plugin {plugin.id}: hooks_data type={type(hooks_data)}, content={hooks_data}")

                # hooks.json can be in multiple formats:
                # 1. List of event names: ["PreToolUse", "PostToolUse"]
                # 2. Dict mapping events to files: {"PreToolUse": "pretooluse.py"}
                # 3. List of hook objects: [{"event": "PreToolUse", "type": "prompt"}]
                # 4. Object with "hooks" key: {"hooks": [...]}

                hooks_list = []
                if isinstance(hooks_data, list):
                    hooks_list = hooks_data
                elif isinstance(hooks_data, dict):
                    if "hooks" in hooks_data:
                        hooks_list = hooks_data["hooks"]
                    else:
                        # Dict mapping event names to something (file paths, configs, etc)
                        hooks_list = list(hooks_data.keys())

                logger.debug(f"[get_hooks_from_plugins] Plugin {plugin.id}: found {len(hooks_list)} hooks in hooks.json")
                for hook in hooks_list:
                    if isinstance(hook, str):
                        # Simple event name string
                        result.append({
                            "plugin_id": plugin.id,
                            "plugin_name": plugin.name,
                            "event": hook,
                            "hook_type": "prompt",
                            "description": None,
                            "tool_matcher": None,
                        })
                    elif isinstance(hook, dict):
                        # Full hook object
                        result.append({
                            "plugin_id": plugin.id,
                            "plugin_name": plugin.name,
                            "event": hook.get("event", "Unknown"),
                            "hook_type": hook.get("type", "prompt"),
                            "description": hook.get("description"),
                            "tool_matcher": hook.get("toolMatcher"),
                        })
            except Exception as e:
                logger.error(f"[get_hooks_from_plugins] Error parsing hooks.json for {plugin.id}: {e}")

        # Also check plugin.json for hooks section
        # Check both root and .claude-plugin/ directory
        plugin_json = plugin_dir / "plugin.json"
        if not plugin_json.exists():
            plugin_json = plugin_dir / ".claude-plugin" / "plugin.json"
        if plugin_json.exists():
            try:
                import json
                with open(plugin_json) as f:
                    data = json.load(f)

                hooks = data.get("hooks", [])
                for hook in hooks:
                    result.append({
                        "plugin_id": plugin.id,
                        "plugin_name": plugin.name,
                        "event": hook.get("event", "Unknown"),
                        "hook_type": hook.get("type", "prompt"),
                        "description": hook.get("description"),
                        "tool_matcher": hook.get("toolMatcher"),
                    })
            except Exception:
                pass

    return result


def _parse_plugin_hook_file(hook_file: Path, plugin: "PluginInfo") -> Optional[Dict[str, Any]]:
    """
    Parse a plugin hook markdown file and extract metadata.

    Hook files use YAML frontmatter like:
    ---
    event: PreToolUse
    toolMatcher: Bash|Edit
    description: Validates tool usage
    ---
    Hook prompt content...
    """
    try:
        content = hook_file.read_text()

        # Check for frontmatter
        if not content.startswith("---"):
            return None

        # Find end of frontmatter
        end_marker = content.find("---", 3)
        if end_marker == -1:
            return None

        frontmatter = content[3:end_marker].strip()

        # Parse frontmatter
        event = None
        tool_matcher = None
        description = None

        for line in frontmatter.split("\n"):
            if ":" in line:
                key, value = line.split(":", 1)
                key = key.strip().lower()
                value = value.strip().strip('"').strip("'")

                if key == "event":
                    event = value
                elif key in ("toolmatcher", "tool_matcher"):
                    tool_matcher = value
                elif key == "description":
                    description = value

        if not event:
            return None

        return {
            "plugin_id": plugin.id,
            "plugin_name": plugin.name,
            "event": event,
            "hook_type": "prompt",
            "description": description,
            "tool_matcher": tool_matcher,
        }

    except Exception:
        return None
