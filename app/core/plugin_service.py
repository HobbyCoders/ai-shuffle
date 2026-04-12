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
import re
import stat
import subprocess
import shutil
import threading
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field, asdict
from enum import Enum

import os

from app.core.platform import is_docker_mode, get_claude_credentials_dir


def _force_rmtree(path):
    """Remove directory tree, handling read-only files on Windows (e.g. .git pack files)."""
    def _on_error(_func, _path, _exc_info):
        os.chmod(_path, stat.S_IWRITE)
        os.unlink(_path)
    shutil.rmtree(path, onerror=_on_error)

logger = logging.getLogger(__name__)


class DictMixin:
    """Mixin providing generic to_dict() for dataclasses.

    Converts datetime fields to ISO format strings (or None).
    All other fields are passed through via dataclasses.asdict().
    """

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        for key, value in result.items():
            if isinstance(value, datetime):
                result[key] = value.isoformat()
        return result


class PluginScope(str, Enum):
    USER = "user"       # ~/.claude/plugins/
    LOCAL = "local"     # <project>/.claude/plugins/
    PROJECT = "project" # <project>/plugins/


@dataclass
class MarketplaceInfo(DictMixin):
    """Information about a plugin marketplace"""
    id: str
    source: str  # "git"
    url: str
    install_location: str
    last_updated: Optional[datetime] = None


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
class PluginInfo(DictMixin):
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


@dataclass
class PluginDetails(PluginInfo):
    """Detailed information about a plugin including contents"""
    commands: List[str] = field(default_factory=list)
    agents: List[str] = field(default_factory=list)
    skills: List[str] = field(default_factory=list)
    readme: Optional[str] = None


@dataclass
class PluginUpdateInfo(DictMixin):
    """Information about an available plugin update"""
    plugin_id: str
    name: str
    marketplace: str
    installed_version: str
    available_version: str
    is_local: bool = True


@dataclass
class AutoUpdateSettings(DictMixin):
    """Settings for plugin auto-update"""
    enabled: bool = True
    interval_hours: int = 6
    auto_sync_marketplaces: bool = True
    auto_update_plugins: bool = True
    last_check: Optional[str] = None  # ISO timestamp


@dataclass
class AvailablePlugin(DictMixin):
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
        self.AUTO_UPDATE_FILE = self.PLUGINS_DIR / "auto_update_settings.json"

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

        # Accept GitHub shorthand (owner/repo) and expand to full URL
        if re.match(r'^[\w\-]+/[\w\-.]+$', url):
            url = f"https://github.com/{url}"

        # Ensure GitHub URLs end with .git
        if re.match(r'^https?://github\.com/', url) and not url.endswith('.git'):
            url = url.rstrip('/') + '.git'

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
            "lastUpdated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        }
        self._write_json_file(self.MARKETPLACES_FILE, data)

        return MarketplaceInfo(
            id=name,
            source="git",
            url=url,
            install_location=install_location,
            last_updated=datetime.now(timezone.utc)
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

        if not install_location:
            raise RuntimeError(f"Marketplace directory not found: {install_location}")

        install_path = Path(install_location)
        git_dir = install_path / ".git"
        gcs_sha_file = install_path / ".gcs-sha"

        if not install_path.exists():
            raise RuntimeError(f"Marketplace directory not found: {install_location}")

        # GCS-distributed marketplace (installed by Claude Code CLI, not git).
        # These have a .gcs-sha file and no .git directory. The CLI manages
        # updates, so we just verify the content is intact and skip re-clone.
        if gcs_sha_file.exists():
            manifest = self._get_marketplace_manifest(install_path)
            if manifest:
                logger.info(
                    f"Marketplace {marketplace_id} is GCS-distributed (managed by Claude CLI). "
                    f"Skipping git sync."
                )
            else:
                logger.warning(
                    f"GCS marketplace {marketplace_id} exists but has no valid manifest. "
                    f"It may need to be reinstalled via Claude CLI."
                )
        elif not git_dir.exists():
            # Non-git, non-GCS directory — attempt re-clone from source
            logger.warning(f"Marketplace directory exists but is not a git repo: {install_location}. Re-cloning...")
            source_data = data[marketplace_id].get("source", {})
            source_url = source_data.get("url") or source_data.get("repo", "")
            # Handle GitHub shorthand (org/repo) vs full URL
            if source_url and not source_url.startswith(("http://", "https://", "git@")):
                source_url = f"https://github.com/{source_url}"
            if not source_url:
                raise RuntimeError(f"Cannot re-clone marketplace: no source URL found for {marketplace_id}")
            shutil.rmtree(install_path, ignore_errors=True)
            try:
                subprocess.run(
                    ["git", "clone", "--depth", "1", source_url, install_location],
                    check=True,
                    capture_output=True,
                    timeout=120
                )
            except subprocess.CalledProcessError as e:
                raise RuntimeError(f"Failed to re-clone marketplace: {e.stderr.decode()}")
            except subprocess.TimeoutExpired:
                raise RuntimeError("Marketplace re-clone timed out")
        else:
            # Git-based marketplace — pull latest
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
        data[marketplace_id]["lastUpdated"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self._write_json_file(self.MARKETPLACES_FILE, data)

        return True

    # =========================================================================
    # Available Plugins (from marketplaces)
    # =========================================================================

    def _get_marketplace_manifest(self, mp_path: Path) -> Optional[Dict[str, Any]]:
        """Read and parse .claude-plugin/marketplace.json from a marketplace directory."""
        manifest_path = mp_path / ".claude-plugin" / "marketplace.json"
        if not manifest_path.exists():
            return None
        try:
            return json.loads(manifest_path.read_text(encoding='utf-8'))
        except (IOError, json.JSONDecodeError, UnicodeDecodeError):
            logger.warning(f"Failed to parse marketplace.json at {manifest_path}")
            return None

    def _resolve_plugin_source(self, source, mp_path: Path, plugin_root: Optional[str] = None) -> Optional[Path]:
        """
        Resolve a plugin source to a local directory path.
        Only works for relative-path sources within the marketplace repo.
        Returns None for external sources (github, url, npm, pip).
        """
        if isinstance(source, str) and (source.startswith("./") or source.startswith(".\\")):
            # Relative path - resolve against marketplace root or pluginRoot
            if plugin_root:
                resolved = mp_path / plugin_root / source.lstrip("./").lstrip(".\\")
            else:
                resolved = mp_path / source.lstrip("./").lstrip(".\\")
            if resolved.exists() and resolved.is_dir():
                return resolved
        return None

    def _detect_components_from_dir(self, plugin_dir: Path) -> Dict[str, bool]:
        """Detect plugin components by scanning a local plugin directory."""
        return {
            "has_commands": (plugin_dir / "commands").exists(),
            "has_agents": (plugin_dir / "agents").exists(),
            "has_skills": (plugin_dir / "skills").exists(),
            "has_hooks": (plugin_dir / "hooks").exists() or (plugin_dir / "hooks.json").exists(),
            "has_mcp": (plugin_dir / ".mcp.json").exists(),
        }

    def _detect_components_from_entry(self, entry: Dict[str, Any]) -> Dict[str, bool]:
        """Infer plugin components from marketplace.json entry keys."""
        return {
            "has_commands": "commands" in entry,
            "has_agents": "agents" in entry,
            "has_skills": "skills" in entry,
            "has_hooks": "hooks" in entry,
            "has_mcp": "mcpServers" in entry,
        }

    @staticmethod
    def _fetch_github_readme(marketplace_path: str) -> Optional[str]:
        """Try to fetch README.md from a GitHub repo URL stored in marketplace_path.

        marketplace_path for external sources is a JSON-serialized source dict like:
            {"source": "url", "url": "https://github.com/user/repo.git"}
        """
        import httpx

        try:
            source = json.loads(marketplace_path)
        except (json.JSONDecodeError, TypeError):
            return None

        url = source.get("url", "") if isinstance(source, dict) else ""
        if not url:
            return None

        # Extract GitHub owner/repo from URL, stripping .git suffix
        match = re.match(r'https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$', url)
        if not match:
            return None

        owner, repo = match.group(1), match.group(2)

        # Try fetching raw README from common default branches
        for branch in ("main", "master"):
            raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/README.md"
            try:
                resp = httpx.get(raw_url, timeout=5, follow_redirects=True)
                if resp.status_code == 200:
                    return resp.text
            except httpx.HTTPError:
                continue

        return None

    def get_available_plugins(self, marketplace: Optional[str] = None) -> List[AvailablePlugin]:
        """
        Get all available plugins from marketplaces.

        Reads .claude-plugin/marketplace.json from each marketplace to discover
        plugins. Supports relative-path, github, url, npm, and pip source types.

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
            manifest = self._get_marketplace_manifest(mp_path)

            if not manifest:
                logger.warning(f"No marketplace.json found for {mp.id} at {mp_path}")
                continue

            plugin_root = manifest.get("metadata", {}).get("pluginRoot") if isinstance(manifest.get("metadata"), dict) else None

            for entry in manifest.get("plugins", []):
                plugin_name = entry.get("name")
                if not plugin_name:
                    continue

                plugin_id = f"{plugin_name}@{mp.id}"
                source = entry.get("source", "")
                description = entry.get("description", "")[:200]

                # Try to resolve to a local directory for component detection
                local_dir = self._resolve_plugin_source(source, mp_path, plugin_root)

                if local_dir:
                    # Local source - scan directory for components
                    components = self._detect_components_from_dir(local_dir)
                    mp_path_str = str(local_dir)

                    # If description is empty, try reading from local plugin files
                    if not description:
                        # Try .claude-plugin/plugin.json
                        pj = local_dir / ".claude-plugin" / "plugin.json"
                        if pj.exists():
                            try:
                                pj_data = json.loads(pj.read_text(encoding='utf-8'))
                                description = pj_data.get("description", "")[:200]
                            except (IOError, json.JSONDecodeError, UnicodeDecodeError):
                                pass
                        # Try README.md
                        if not description:
                            readme = local_dir / "README.md"
                            if readme.exists():
                                try:
                                    content = readme.read_text(encoding='utf-8')
                                    for line in content.strip().split("\n"):
                                        line = line.strip()
                                        if line and not line.startswith("#"):
                                            description = line[:200]
                                            break
                                except (IOError, UnicodeDecodeError):
                                    pass
                else:
                    # External source - infer components from entry keys
                    components = self._detect_components_from_entry(entry)
                    # Store serialized source for install_plugin to use
                    mp_path_str = json.dumps(source) if isinstance(source, dict) else str(source)

                # Check for LSP servers (show as has_mcp since they're server-like)
                if "lspServers" in entry and not components.get("has_mcp"):
                    components["has_mcp"] = True

                available.append(AvailablePlugin(
                    id=plugin_id,
                    name=plugin_name,
                    marketplace=mp.id,
                    marketplace_path=mp_path_str,
                    description=description,
                    has_commands=components.get("has_commands", False),
                    has_agents=components.get("has_agents", False),
                    has_skills=components.get("has_skills", False),
                    has_hooks=components.get("has_hooks", False),
                    has_mcp=components.get("has_mcp", False),
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

        parts = plugin_id.rsplit("@", 1)
        name = parts[0]
        marketplace = parts[1] if len(parts) > 1 else "unknown"

        plugin_path = Path(plugin.marketplace_path)
        if not plugin_path.exists():
            # External source (github/url/npm/pip) — no local directory to scan.
            # Try to fetch README from GitHub if the source is a GitHub URL.
            readme = self._fetch_github_readme(plugin.marketplace_path)

            return PluginDetails(
                id=plugin_id,
                name=name,
                marketplace=marketplace,
                description=plugin.description,
                version="available",
                scope="marketplace",
                install_path=plugin.marketplace_path,
                installed_at=None,
                enabled=False,
                has_commands=plugin.has_commands,
                has_agents=plugin.has_agents,
                has_skills=plugin.has_skills,
                has_hooks=plugin.has_hooks,
                has_mcp=plugin.has_mcp,
                commands=[],
                agents=[],
                skills=[],
                readme=readme
            )

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

    def _find_plugin_in_manifest(self, marketplace_id: str, plugin_name: str) -> Optional[Dict[str, Any]]:
        """Find a plugin entry in a marketplace's marketplace.json."""
        marketplaces = self._read_json_file(self.MARKETPLACES_FILE, {})
        mp_data = marketplaces.get(marketplace_id)
        if not mp_data:
            return None

        mp_path = Path(mp_data.get("installLocation", ""))
        manifest = self._get_marketplace_manifest(mp_path)
        if not manifest:
            return None

        for entry in manifest.get("plugins", []):
            if entry.get("name") == plugin_name:
                return entry
        return None

    def _clone_plugin_source(self, source: Dict[str, Any], dest: Path) -> None:
        """Clone an external plugin source to a destination directory.

        Supported source types:
        - "github": Clone from GitHub using org/repo shorthand
        - "url": Clone from a full git URL
        - "git-subdir": Clone a repo and extract a specific subdirectory
        """
        source_type = source.get("source", "")
        ref = source.get("ref")

        if source_type == "git-subdir":
            self._clone_git_subdir_source(source, dest)
            return

        if source_type == "github":
            repo = source.get("repo", "")
            clone_url = f"https://github.com/{repo}.git"
        elif source_type == "url":
            clone_url = source.get("url", "")
        else:
            raise ValueError(f"Unsupported plugin source type: {source_type}")

        if not clone_url:
            raise ValueError("Plugin source URL is empty")

        cmd = ["git", "clone", "--depth", "1"]
        if ref:
            cmd.extend(["--branch", ref])
        cmd.extend([clone_url, str(dest)])

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to clone plugin: {result.stderr.strip()}")

    def _clone_git_subdir_source(self, source: Dict[str, Any], dest: Path) -> None:
        """Clone a git repo and extract a specific subdirectory.

        Handles the "git-subdir" source type from marketplace.json:
            {"source": "git-subdir", "url": "org/repo", "path": "plugins/foo", "ref": "main"}

        Uses git sparse-checkout to avoid downloading the entire repo when possible,
        falling back to full shallow clone + subdir extraction.
        """
        url = source.get("url", "")
        subdir_path = source.get("path", "")
        ref = source.get("ref")

        if not url:
            raise ValueError("git-subdir source: URL is empty")
        if not subdir_path:
            raise ValueError("git-subdir source: path is empty")

        # Normalize URL: GitHub shorthand (org/repo) → full URL
        if not url.startswith(("http://", "https://", "git@")):
            url = f"https://github.com/{url}.git"
        elif not url.endswith(".git"):
            url = f"{url}.git"

        import tempfile
        tmp_dir = Path(tempfile.mkdtemp(prefix="plugin-clone-"))
        try:
            # Shallow clone the repo
            cmd = ["git", "clone", "--depth", "1", "--filter=blob:none"]
            if ref:
                cmd.extend(["--branch", ref])
            cmd.extend([url, str(tmp_dir)])

            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"Failed to clone git-subdir repo: {result.stderr.strip()}"
                )

            # Extract the target subdirectory
            extracted = tmp_dir / subdir_path.strip("/")
            if not extracted.exists() or not extracted.is_dir():
                raise ValueError(
                    f"Subdirectory '{subdir_path}' not found in cloned repo"
                )

            # Copy subdirectory to destination
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(extracted, dest)

        finally:
            # Clean up the full clone
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def install_plugin(self, plugin_id: str, scope: str = "user") -> PluginInfo:
        """
        Install a plugin from a marketplace.

        Reads marketplace.json to find the plugin source and handles both
        relative-path (local) and external (github/url) sources.

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
        mp_path = Path(mp_location)

        # Look up plugin in marketplace.json
        entry = self._find_plugin_in_manifest(marketplace_id, plugin_name)
        if not entry:
            raise ValueError(f"Plugin not found in marketplace: {plugin_name}")

        source = entry.get("source", "")
        # Version priority: plugin manifest > entry.version > source.sha (short) > "1.0.0"
        version = entry.get("version", "1.0.0")
        if version == "1.0.0" and isinstance(source, dict) and source.get("sha"):
            version = source["sha"][:7]
        manifest = self._get_marketplace_manifest(mp_path)
        plugin_root = manifest.get("metadata", {}).get("pluginRoot") if manifest and isinstance(manifest.get("metadata"), dict) else None

        # Resolve plugin source
        local_dir = self._resolve_plugin_source(source, mp_path, plugin_root)

        if local_dir:
            # Local source - read version from the plugin's own manifest
            # (matching Claude Code's behavior: manifest.version is source of truth)
            manifest_version = self._get_plugin_manifest_version(local_dir)
            if manifest_version:
                version = manifest_version
            elif not entry.get("version"):
                # No manifest version and no entry version — fall back to
                # marketplace HEAD hash for git-based marketplaces
                resolved_version = self._get_marketplace_head_version(marketplace_id)
                if resolved_version:
                    version = resolved_version

            # Create cache directory and copy plugin
            cache_path = self.CACHE_DIR / marketplace_id / plugin_name / version
            cache_path.parent.mkdir(parents=True, exist_ok=True)

            if cache_path.exists():
                _force_rmtree(cache_path)
            shutil.copytree(local_dir, cache_path)

        elif isinstance(source, dict):
            # External source - clone from remote
            cache_path = self.CACHE_DIR / marketplace_id / plugin_name / version
            cache_path.parent.mkdir(parents=True, exist_ok=True)

            if cache_path.exists():
                _force_rmtree(cache_path)

            self._clone_plugin_source(source, cache_path)

            # After cloning, read the plugin's own manifest version — it's the
            # source of truth (matching Claude Code behaviour).
            cloned_manifest_version = self._get_plugin_manifest_version(cache_path)
            if cloned_manifest_version and cloned_manifest_version != version:
                # Re-cache under the correct manifest version
                new_cache_path = self.CACHE_DIR / marketplace_id / plugin_name / cloned_manifest_version
                if new_cache_path != cache_path:
                    new_cache_path.parent.mkdir(parents=True, exist_ok=True)
                    if new_cache_path.exists():
                        _force_rmtree(new_cache_path)
                    shutil.move(str(cache_path), str(new_cache_path))
                    cache_path = new_cache_path
                version = cloned_manifest_version

        else:
            # Better error: include resolved path for relative sources
            if isinstance(source, str) and (source.startswith("./") or source.startswith(".\\")):
                if plugin_root:
                    tried_path = mp_path / plugin_root / source.lstrip("./").lstrip(".\\")
                else:
                    tried_path = mp_path / source.lstrip("./").lstrip(".\\")
                raise ValueError(
                    f"Plugin source directory not found for '{plugin_name}': "
                    f"{tried_path} does not exist. The marketplace manifest references "
                    f"this plugin but its files are missing from the local marketplace repo."
                )
            raise ValueError(f"Plugin source not found or unsupported: {plugin_name}")

        # Ensure scripts have execute permissions
        for script in cache_path.rglob("*.sh"):
            current_mode = script.stat().st_mode
            script.chmod(current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        # Update installed_plugins.json
        installed = self._read_json_file(self.INSTALLED_FILE, {"version": 2, "plugins": {}})
        if "plugins" not in installed:
            installed["plugins"] = {}

        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        install_record = {
            "scope": scope,
            "installPath": str(cache_path),
            "version": version,
            "installedAt": now,
            "lastUpdated": now,
            "isLocal": isinstance(source, str)
        }
        # Store source SHA for external plugins so update checks can compare
        # against the marketplace entry's SHA without cloning.
        if isinstance(source, dict) and source.get("sha"):
            install_record["sourceSha"] = source["sha"][:7]
        installed["plugins"][plugin_id] = [install_record]
        self._write_json_file(self.INSTALLED_FILE, installed)

        # Enable by default
        self.enable_plugin(plugin_id)

        # Return plugin info for the installed/updated plugin
        for p in self.get_installed_plugins():
            if p.id == plugin_id:
                return p
        # Fallback: shouldn't happen, but return last if not found
        return self.get_installed_plugins()[-1]

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
        mp_path = Path(mp_location)

        # Look up plugin in marketplace.json
        entry = self._find_plugin_in_manifest(marketplace_id, plugin_name)
        if not entry:
            raise ValueError(f"Plugin not found in marketplace: {plugin_name}")

        source = entry.get("source", "")
        manifest = self._get_marketplace_manifest(mp_path)
        plugin_root = manifest.get("metadata", {}).get("pluginRoot") if manifest and isinstance(manifest.get("metadata"), dict) else None

        # Determine target directory
        target_dir = self.get_plugins_dir_for_scope(scope)
        if not target_dir:
            raise ValueError(f"Cannot get directory for scope: {scope}")

        target_path = target_dir / plugin_name

        # Create target directory
        target_dir.mkdir(parents=True, exist_ok=True)

        # Resolve and copy/clone plugin to target
        local_dir = self._resolve_plugin_source(source, mp_path, plugin_root)

        if target_path.exists():
            shutil.rmtree(target_path)

        if local_dir:
            shutil.copytree(local_dir, target_path)
        elif isinstance(source, dict):
            self._clone_plugin_source(source, target_path)
        else:
            raise ValueError(f"Plugin source not found or unsupported: {plugin_name}")

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

    # =========================================================================
    # Auto-Update Settings & Update Methods
    # =========================================================================

    def get_auto_update_settings(self) -> AutoUpdateSettings:
        """Get auto-update settings"""
        data = self._read_json_file(self.AUTO_UPDATE_FILE, {})
        return AutoUpdateSettings(
            enabled=data.get("enabled", True),
            interval_hours=data.get("intervalHours", 6),
            auto_sync_marketplaces=data.get("autoSyncMarketplaces", True),
            auto_update_plugins=data.get("autoUpdatePlugins", True),
            last_check=data.get("lastCheck")
        )

    def save_auto_update_settings(self, settings: AutoUpdateSettings) -> bool:
        """Save auto-update settings"""
        data = {
            "enabled": settings.enabled,
            "intervalHours": settings.interval_hours,
            "autoSyncMarketplaces": settings.auto_sync_marketplaces,
            "autoUpdatePlugins": settings.auto_update_plugins,
            "lastCheck": settings.last_check
        }
        return self._write_json_file(self.AUTO_UPDATE_FILE, data)

    def _get_marketplace_head_version(self, marketplace_id: str) -> Optional[str]:
        """Get the current version identifier for a marketplace.

        Checks in order:
        1. Git HEAD short hash (git-based marketplaces)
        2. .gcs-sha file content (GCS-distributed marketplaces)
        3. None if neither is available
        """
        marketplaces = self._read_json_file(self.MARKETPLACES_FILE, {})
        mp_data = marketplaces.get(marketplace_id, {})
        install_location = mp_data.get("installLocation")
        if not install_location:
            return None

        install_path = Path(install_location)

        # Try git first
        try:
            result = subprocess.run(
                ["git", "-C", install_location, "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass

        # Fall back to .gcs-sha for GCS-distributed marketplaces
        gcs_sha_file = install_path / ".gcs-sha"
        if gcs_sha_file.exists():
            try:
                sha = gcs_sha_file.read_text(encoding="utf-8").strip()
                if sha:
                    return sha[:7]  # Short hash, consistent with git --short
            except (IOError, UnicodeDecodeError):
                pass

        return None

    def _get_plugin_manifest_version(self, local_dir: Path) -> Optional[str]:
        """Read a plugin's version from its .claude-plugin/plugin.json manifest.

        This matches Claude Code's behavior: the plugin's own manifest is the
        source of truth for versioning, not the marketplace repo hash.

        Args:
            local_dir: Path to the plugin's source directory in the marketplace.

        Returns:
            The version string from plugin.json, or None if not found.
        """
        manifest_path = local_dir / ".claude-plugin" / "plugin.json"
        if not manifest_path.exists():
            # Also check for plugin.json at root (older format)
            manifest_path = local_dir / "plugin.json"
            if not manifest_path.exists():
                return None
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            return manifest.get("version") or None
        except (IOError, json.JSONDecodeError, UnicodeDecodeError):
            return None

    def check_for_updates(self) -> List[PluginUpdateInfo]:
        """
        Check all installed plugins for available updates.

        Compares the installed version (git hash or semver) against the
        current marketplace HEAD. For local-source plugins, a version
        mismatch means the marketplace has been updated since install.

        Returns:
            List of plugins with available updates
        """
        installed_data = self._read_json_file(self.INSTALLED_FILE, {"version": 2, "plugins": {}})
        file_version = installed_data.get("version", 1)
        updates: List[PluginUpdateInfo] = []

        # Cache marketplace HEAD versions to avoid repeated git calls
        mp_head_cache: Dict[str, Optional[str]] = {}
        marketplaces = self._read_json_file(self.MARKETPLACES_FILE, {})

        for plugin_id, installation_data in installed_data.get("plugins", {}).items():
            if not installation_data:
                continue

            # Handle both version 1 (single object) and version 2 (array) formats
            if file_version == 1 or isinstance(installation_data, dict):
                install = installation_data
            else:
                install = installation_data[0] if installation_data else None
                if not install:
                    continue

            parts = plugin_id.rsplit("@", 1)
            if len(parts) != 2:
                continue

            plugin_name, marketplace_id = parts
            installed_version = install.get("version", "unknown")

            # Determine locality from marketplace entry, not just installed data.
            # Old installs may lack the isLocal field entirely.
            entry = self._find_plugin_in_manifest(marketplace_id, plugin_name)
            if entry:
                source = entry.get("source", "")
                is_local = isinstance(source, str)
            else:
                is_local = install.get("isLocal", True)

            # For local-source plugins: read the plugin's own manifest version
            # from the marketplace source directory.  This matches Claude Code's
            # behaviour — manifest.version is the canonical version, NOT the
            # marketplace repo's git/gcs hash.
            if is_local:
                # Resolve the local source directory for this plugin
                mp_data = marketplaces.get(marketplace_id, {})
                mp_location = mp_data.get("installLocation")

                available_version = None
                if mp_location and entry:
                    mp_path = Path(mp_location)
                    mp_manifest = self._get_marketplace_manifest(mp_path)
                    mp_plugin_root = (
                        mp_manifest.get("metadata", {}).get("pluginRoot")
                        if mp_manifest and isinstance(mp_manifest.get("metadata"), dict)
                        else None
                    )
                    local_dir = self._resolve_plugin_source(
                        entry.get("source", ""), mp_path, mp_plugin_root
                    )
                    if local_dir:
                        available_version = self._get_plugin_manifest_version(local_dir)

                # Fall back chain: entry.version → marketplace HEAD hash.
                # Track whether the version is from a "real" source (manifest/entry)
                # vs a hash fallback, to avoid comparing apples to oranges.
                version_from_manifest = available_version is not None
                if not available_version:
                    available_version = entry.get("version") if entry else None
                    if available_version:
                        version_from_manifest = True
                if not available_version:
                    if marketplace_id not in mp_head_cache:
                        mp_head_cache[marketplace_id] = self._get_marketplace_head_version(marketplace_id)
                    available_version = mp_head_cache[marketplace_id]
                    # This is a repo-level hash, not a plugin manifest version

                if not available_version:
                    continue

                # Only compare versions from the same system.  If installed
                # version is "unknown" (plugin has no manifest version) and
                # available came from a repo hash, skip — they're incomparable.
                if not version_from_manifest and installed_version in ("unknown", "1.0.0"):
                    continue

                if installed_version != available_version:
                    updates.append(PluginUpdateInfo(
                        plugin_id=plugin_id,
                        name=plugin_name,
                        marketplace=marketplace_id,
                        installed_version=installed_version,
                        available_version=available_version,
                        is_local=True
                    ))
            else:
                # For external plugins: compare source SHAs when available.
                # The marketplace entry's source.sha represents the latest
                # pinned commit.  We stored the SHA at install time in
                # install_record["sourceSha"].  If they differ, the plugin
                # has been updated upstream.
                if entry:
                    source = entry.get("source", {})
                    entry_version = entry.get("version", "")

                    if entry_version:
                        # Explicit version in marketplace entry — compare directly
                        if entry_version != installed_version:
                            updates.append(PluginUpdateInfo(
                                plugin_id=plugin_id,
                                name=plugin_name,
                                marketplace=marketplace_id,
                                installed_version=installed_version,
                                available_version=entry_version,
                                is_local=False
                            ))
                    elif isinstance(source, dict) and source.get("sha"):
                        # No explicit version — compare source SHAs
                        available_sha = source["sha"][:7]
                        installed_sha = install.get("sourceSha", "")
                        if installed_sha:
                            # We stored SHA at install time — compare directly
                            if available_sha != installed_sha:
                                cache_dir = self.CACHE_DIR / marketplace_id / plugin_name / available_sha
                                cached_ver = self._get_plugin_manifest_version(cache_dir)
                                updates.append(PluginUpdateInfo(
                                    plugin_id=plugin_id,
                                    name=plugin_name,
                                    marketplace=marketplace_id,
                                    installed_version=installed_version,
                                    available_version=cached_ver or available_sha,
                                    is_local=False
                                ))
                        else:
                            # Legacy install without sourceSha — check cached
                            # copy at new SHA for its manifest version
                            cache_dir = self.CACHE_DIR / marketplace_id / plugin_name / available_sha
                            cached_ver = self._get_plugin_manifest_version(cache_dir)
                            if cached_ver and cached_ver != installed_version:
                                updates.append(PluginUpdateInfo(
                                    plugin_id=plugin_id,
                                    name=plugin_name,
                                    marketplace=marketplace_id,
                                    installed_version=installed_version,
                                    available_version=cached_ver,
                                    is_local=False
                                ))
                            elif not cached_ver:
                                # No cached copy — only compare SHA-to-SHA
                                _is_sha = (
                                    len(installed_version) == 7
                                    and all(c in "0123456789abcdef" for c in installed_version)
                                )
                                if _is_sha and installed_version != available_sha:
                                    updates.append(PluginUpdateInfo(
                                        plugin_id=plugin_id,
                                        name=plugin_name,
                                        marketplace=marketplace_id,
                                        installed_version=installed_version,
                                        available_version=available_sha,
                                        is_local=False
                                    ))

        return updates

    def update_plugin(self, plugin_id: str) -> PluginInfo:
        """
        Update a single installed plugin to the latest version.

        Preserves the plugin's enabled state across the re-install.

        Args:
            plugin_id: Plugin ID in format "name@marketplace"

        Returns:
            Updated PluginInfo
        """
        installed_data = self._read_json_file(self.INSTALLED_FILE, {"version": 2, "plugins": {}})
        if plugin_id not in installed_data.get("plugins", {}):
            raise ValueError(f"Plugin not installed: {plugin_id}")

        # Get current scope and enabled state
        installation_data = installed_data["plugins"][plugin_id]
        if isinstance(installation_data, list):
            install = installation_data[0] if installation_data else {}
        else:
            install = installation_data
        scope = install.get("scope", "user")

        settings = self._read_json_file(self.SETTINGS_FILE, {})
        was_enabled = settings.get("enabledPlugins", {}).get(plugin_id, False)

        # Re-install (this overwrites the cache and updates installed_plugins.json)
        result = self.install_plugin(plugin_id, scope)

        # Restore enabled state
        if was_enabled:
            self.enable_plugin(plugin_id)
        else:
            self.disable_plugin(plugin_id)

        logger.info(f"Updated plugin: {plugin_id} -> {result.version}")
        return result

    def update_all_plugins(self) -> List[PluginInfo]:
        """
        Update all installed plugins that have available updates.

        Returns:
            List of updated PluginInfo objects
        """
        updates = self.check_for_updates()
        results: List[PluginInfo] = []

        for update in updates:
            try:
                result = self.update_plugin(update.plugin_id)
                results.append(result)
            except Exception as e:
                logger.error(f"Failed to update plugin {update.plugin_id}: {e}")

        return results

    def sync_all_marketplaces(self) -> Dict[str, bool]:
        """
        Sync all registered marketplaces.

        Returns:
            Dict of marketplace_id -> success status
        """
        results: Dict[str, bool] = {}
        for mp in self.get_marketplaces():
            try:
                self.sync_marketplace(mp.id)
                results[mp.id] = True
            except Exception as e:
                logger.error(f"Failed to sync marketplace {mp.id}: {e}")
                results[mp.id] = False
        return results

    def run_auto_update(self) -> Dict[str, Any]:
        """
        Run the full auto-update cycle:
        1. Sync all marketplaces (if enabled)
        2. Check for updates
        3. Update plugins (if enabled)
        4. Update last_check timestamp

        Returns:
            Summary of actions taken
        """
        au_settings = self.get_auto_update_settings()
        summary: Dict[str, Any] = {
            "synced_marketplaces": {},
            "updates_available": [],
            "updated_plugins": [],
            "errors": []
        }

        # 1. Sync marketplaces
        if au_settings.auto_sync_marketplaces:
            summary["synced_marketplaces"] = self.sync_all_marketplaces()

        # 2. Check for updates
        try:
            updates = self.check_for_updates()
            summary["updates_available"] = [u.to_dict() for u in updates]
        except Exception as e:
            logger.error(f"Failed to check for updates: {e}")
            summary["errors"].append(f"Update check failed: {e}")
            updates = []

        # 3. Auto-update plugins
        if au_settings.auto_update_plugins and updates:
            for update in updates:
                try:
                    result = self.update_plugin(update.plugin_id)
                    summary["updated_plugins"].append(result.to_dict())
                except Exception as e:
                    error_msg = f"Failed to update {update.plugin_id}: {e}"
                    logger.error(error_msg)
                    summary["errors"].append(error_msg)

        # 4. Update last check timestamp
        au_settings.last_check = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self.save_auto_update_settings(au_settings)

        return summary


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
