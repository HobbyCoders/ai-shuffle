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
import subprocess
import shutil
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
            "has_hooks": self.has_hooks
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
        # Derive name from URL if not provided
        if not name:
            name = url.rstrip("/").split("/")[-1]
            if name.endswith(".git"):
                name = name[:-4]

        install_location = str(self.MARKETPLACES_DIR / name)

        # Clone the repository
        if not Path(install_location).exists():
            try:
                subprocess.run(
                    ["git", "clone", url, install_location],
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
            plugins_dir = mp_path / "plugins"

            if not plugins_dir.exists():
                continue

            for plugin_dir in plugins_dir.iterdir():
                if not plugin_dir.is_dir():
                    continue

                plugin_name = plugin_dir.name
                plugin_id = f"{plugin_name}@{mp.id}"

                # Read plugin README for description
                description = ""
                readme_path = plugin_dir / "README.md"
                if readme_path.exists():
                    try:
                        content = readme_path.read_text()
                        # Extract first paragraph as description
                        lines = content.strip().split("\n")
                        for line in lines:
                            line = line.strip()
                            if line and not line.startswith("#"):
                                description = line[:200]
                                break
                    except IOError:
                        pass

                # Check what components the plugin has
                has_commands = (plugin_dir / "commands").exists()
                has_agents = (plugin_dir / "agents").exists()
                has_skills = (plugin_dir / "skills").exists()
                has_hooks = (plugin_dir / "hooks").exists() or (plugin_dir / "hooks.json").exists()

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

        for plugin_id, installations in installed_data.get("plugins", {}).items():
            if not installations:
                continue

            # Use the first (usually only) installation
            install = installations[0]
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

            # Read description from README
            description = ""
            readme_path = install_path / "README.md"
            if readme_path.exists():
                try:
                    content = readme_path.read_text()
                    lines = content.strip().split("\n")
                    for line in lines:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            description = line[:200]
                            break
                except IOError:
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
                has_hooks=has_hooks
            ))

        return plugins

    def get_plugin_details(self, plugin_id: str) -> Optional[PluginDetails]:
        """Get detailed information about a specific plugin"""
        installed_data = self._read_json_file(self.INSTALLED_FILE, {"version": 2, "plugins": {}})
        settings = self._read_json_file(self.SETTINGS_FILE, {})
        enabled_plugins = settings.get("enabledPlugins", {})

        installations = installed_data.get("plugins", {}).get(plugin_id, [])
        if not installations:
            return None

        install = installations[0]
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

        # Read README
        readme = None
        readme_path = install_path / "README.md"
        if readme_path.exists():
            try:
                readme = readme_path.read_text()
            except IOError:
                pass

        # Extract description from README
        description = ""
        if readme:
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
        plugin_source = Path(mp_location) / "plugins" / plugin_name

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

        if plugin_id not in installed.get("plugins", {}):
            raise ValueError(f"Plugin not installed: {plugin_id}")

        installations = installed["plugins"][plugin_id]

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
                    content = agent_file.read_text()

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

    if cache_key not in _plugin_services:
        _plugin_services[cache_key] = PluginService(project_dir=project_dir)

    return _plugin_services[cache_key]


def clear_plugin_service_cache():
    """Clear the plugin service cache (useful for testing)"""
    global _plugin_services
    _plugin_services = {}
