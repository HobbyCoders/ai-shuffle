"""
Plugin Service - Core business logic for plugin management.

User-global plugins live in Fluid's home so native legs pick them up
(fluid.context.plugins, CE5). Package format is still Claude's.

Plugin Scopes:
- USER: ~/.fluid/plugins/ (user-global plugins)
- LOCAL: <project>/.fluid/plugins/ (project-local, gitignored)
- PROJECT: <project>/plugins/ (project plugins, committed to git)

Docker mode uses /home/appuser/.fluid/ for user scope.

It provides a Python API for managing marketplaces, installing/uninstalling plugins,
and enabling/disabling plugins.
"""

import json
import logging
import re
import threading
from pathlib import Path
from datetime import datetime
from typing import List, Optional, Dict, Any


# Re-export all models and helpers for backward compatibility
from app.core.plugin_models import (  # noqa: F401
    DictMixin,
    PluginScope,
    MarketplaceInfo,
    PluginInstallation,
    PluginInfo,
    PluginDetails,
    PluginUpdateInfo,
    AutoUpdateSettings,
    AvailablePlugin,
    _get_claude_dir,
    _get_fluid_dir,
    _force_rmtree,
)

# Import delegate modules
from app.core import plugin_marketplace as _mp_mod
from app.core import plugin_install as _inst_mod
from app.core import plugin_hooks as _hooks_mod
from app.core import plugin_updates as _upd_mod

logger = logging.getLogger(__name__)


class PluginService:
    """
    Service for managing plugins.

    User-global installs and enablement write to Fluid's home
    (~/.fluid/plugins, ~/.fluid/settings.json).

    Plugin Scopes:
        - USER: ~/.fluid/plugins/ - User-global plugins
        - LOCAL: <project>/.fluid/plugins/ - Project-local (gitignored)
        - PROJECT: <project>/plugins/ - Project plugins (committed)
    """

    def __init__(self, project_dir: Optional[Path] = None):
        """
        Initialize the plugin service.

        Args:
            project_dir: Optional project directory for LOCAL/PROJECT scope plugins.
                        If not provided, only USER scope is available.
        """
        # User scope paths (always available) — Fluid home, not ~/.claude
        self.FLUID_DIR = _get_fluid_dir()
        self.PLUGINS_DIR = self.FLUID_DIR / "plugins"
        self.SETTINGS_FILE = self.FLUID_DIR / "settings.json"
        self.MARKETPLACES_FILE = self.PLUGINS_DIR / "known_marketplaces.json"
        self.INSTALLED_FILE = self.PLUGINS_DIR / "installed_plugins.json"
        self.CACHE_DIR = self.PLUGINS_DIR / "cache"
        self.MARKETPLACES_DIR = self.PLUGINS_DIR / "marketplaces"
        self.AUTO_UPDATE_FILE = self.PLUGINS_DIR / "auto_update_settings.json"

        # Project scope paths (optional)
        self.project_dir = project_dir
        if project_dir:
            self.LOCAL_PLUGINS_DIR = project_dir / ".fluid" / "plugins"
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
    # Marketplace Management (delegated to plugin_marketplace module)
    # =========================================================================

    def get_marketplaces(self) -> List[MarketplaceInfo]:
        """Get all registered marketplaces"""
        return _mp_mod.get_marketplaces(self)

    def add_marketplace(self, url: str, name: Optional[str] = None) -> MarketplaceInfo:
        """
        Add a new marketplace from a git URL.

        Args:
            url: Git repository URL
            name: Optional marketplace name (derived from URL if not provided)

        Returns:
            MarketplaceInfo for the new marketplace
        """
        return _mp_mod.add_marketplace(self, url, name)

    def remove_marketplace(self, marketplace_id: str) -> bool:
        """
        Remove a marketplace and all its installed plugins.

        Args:
            marketplace_id: The marketplace identifier

        Returns:
            True if successful
        """
        return _mp_mod.remove_marketplace(self, marketplace_id)

    def sync_marketplace(self, marketplace_id: str) -> bool:
        """
        Sync a marketplace with its remote repository.

        Args:
            marketplace_id: The marketplace identifier

        Returns:
            True if successful
        """
        return _mp_mod.sync_marketplace(self, marketplace_id)

    def sync_all_marketplaces(self) -> Dict[str, bool]:
        """
        Sync all registered marketplaces.

        Returns:
            Dict of marketplace_id -> success status
        """
        return _mp_mod.sync_all_marketplaces(self)

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
        return _inst_mod._resolve_plugin_source(self, source, mp_path, plugin_root)

    def _detect_components_from_dir(self, plugin_dir: Path) -> Dict[str, bool]:
        """Detect plugin components by scanning a local plugin directory."""
        return _inst_mod._detect_components_from_dir(self, plugin_dir)

    def _detect_components_from_entry(self, entry: Dict[str, Any]) -> Dict[str, bool]:
        """Infer plugin components from marketplace.json entry keys."""
        return _inst_mod._detect_components_from_entry(self, entry)

    def _discover_implicit_components(self, root: Path) -> Dict[str, Any]:
        """Detect skills/commands/agents in a marketplace-less repo (implicit plugin)."""
        return _inst_mod.discover_implicit_components(self, root)

    def _resolve_component_dir(self, root: Path, component: str) -> Optional[Path]:
        """Resolve a component dir at the repo root or under .claude/ or .agents/."""
        return _inst_mod._resolve_implicit_component_dir(root, component)

    @staticmethod
    def _readme_first_line(directory: Path) -> Optional[str]:
        """First non-empty, non-heading line of README.md in `directory`, truncated."""
        readme = directory / "README.md"
        if not readme.exists():
            return None
        try:
            for line in readme.read_text(encoding="utf-8").strip().split("\n"):
                line = line.strip()
                if line and not line.startswith("#"):
                    return line[:200]
        except (IOError, UnicodeDecodeError):
            pass
        return None

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
                # No marketplace.json -- treat the repo itself as a single implicit
                # plugin if it carries skills/commands/agents (e.g. a repo whose
                # skills live in .claude/skills or .agents/skills). Passive,
                # user-invoked components only; never hooks/MCP.
                implicit = self._discover_implicit_components(mp_path)
                if not implicit["any"]:
                    logger.warning(f"No marketplace.json or skills found for {mp.id} at {mp_path}")
                    continue

                plugin_id = f"{mp.id}@{mp.id}"
                description = self._readme_first_line(mp_path) or "Skills discovered in this repository"
                available.append(AvailablePlugin(
                    id=plugin_id,
                    name=mp.id,
                    marketplace=mp.id,
                    marketplace_path=str(mp_path),
                    description=description,
                    has_commands=implicit["has_commands"],
                    has_agents=implicit["has_agents"],
                    has_skills=implicit["has_skills"],
                    has_hooks=False,
                    has_mcp=False,
                    installed=plugin_id in installed_plugins,
                    enabled=enabled_plugins.get(plugin_id, False),
                ))
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

        from app.core import connector_packages
        if marketplace in (None, connector_packages.MARKETPLACE):
            available.extend(connector_packages.available())
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

        from app.core import connector_packages
        plugins.extend(connector_packages.installed())
        return plugins

    def get_plugin_details(self, plugin_id: str) -> Optional[PluginDetails]:
        """Get detailed information about a specific plugin"""
        from app.core import connector_packages
        kind = connector_packages.kind_for(plugin_id)
        if kind:
            return connector_packages.details(kind)
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
            # External source (github/url/npm/pip) -- no local directory to scan.
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

        # Collect commands / agents / skills. Component dirs may live at the repo
        # root or under .claude/ or .agents/ (implicit, marketplace-less plugins).
        commands = []
        commands_dir = self._resolve_component_dir(plugin_path, "commands")
        if commands_dir:
            for cmd_file in commands_dir.glob("**/*.md"):
                commands.append(cmd_file.stem)

        agents = []
        agents_dir = self._resolve_component_dir(plugin_path, "agents")
        if agents_dir:
            for agent_file in agents_dir.glob("**/*.md"):
                agents.append(agent_file.stem)

        skills = []
        skills_dir = self._resolve_component_dir(plugin_path, "skills")
        if skills_dir:
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
    # Plugin Installation (delegated to plugin_install module)
    # =========================================================================

    def _find_plugin_in_manifest(self, marketplace_id: str, plugin_name: str) -> Optional[Dict[str, Any]]:
        """Find a plugin entry in a marketplace's marketplace.json."""
        return _inst_mod._find_plugin_in_manifest(self, marketplace_id, plugin_name)

    def _clone_plugin_source(self, source: Dict[str, Any], dest: Path) -> None:
        """Clone an external plugin source to a destination directory."""
        return _inst_mod._clone_plugin_source(self, source, dest)

    def _clone_git_subdir_source(self, source: Dict[str, Any], dest: Path) -> None:
        """Clone a git repo and extract a specific subdirectory."""
        return _inst_mod._clone_git_subdir_source(self, source, dest)

    def install_plugin(self, plugin_id: str, scope: str = "user") -> PluginInfo:
        """
        Install a plugin from a marketplace.

        Args:
            plugin_id: Plugin ID in format "name@marketplace"
            scope: Installation scope ("user" or "project")

        Returns:
            PluginInfo for the installed plugin
        """
        from app.core import connector_packages
        kind = connector_packages.kind_for(plugin_id)
        if kind:
            return connector_packages.install(kind, scope)
        return _inst_mod.install_plugin(self, plugin_id, scope)

    def install_plugins_batch(self, plugin_ids: List[str], scope: str = "user") -> List[PluginInfo]:
        """
        Install multiple plugins in batch.

        Args:
            plugin_ids: List of plugin IDs to install
            scope: Installation scope

        Returns:
            List of installed plugin info
        """
        return [self.install_plugin(plugin_id, scope) for plugin_id in plugin_ids]

    def uninstall_plugin(self, plugin_id: str) -> bool:
        """
        Uninstall a plugin.

        Args:
            plugin_id: Plugin ID to uninstall

        Returns:
            True if successful
        """
        from app.core import connector_packages
        kind = connector_packages.kind_for(plugin_id)
        if kind:
            return connector_packages.uninstall(kind)
        return _inst_mod.uninstall_plugin(self, plugin_id)

    # =========================================================================
    # Plugin Enable/Disable
    # =========================================================================

    def enable_plugin(self, plugin_id: str) -> bool:
        """Enable a plugin globally"""
        from app.core import connector_packages
        kind = connector_packages.kind_for(plugin_id)
        if kind:
            return connector_packages.set_enabled(kind, True)
        settings = self._read_json_file(self.SETTINGS_FILE, {})
        if "enabledPlugins" not in settings:
            settings["enabledPlugins"] = {}
        settings["enabledPlugins"][plugin_id] = True
        return self._write_json_file(self.SETTINGS_FILE, settings)

    def disable_plugin(self, plugin_id: str) -> bool:
        """Disable a plugin globally"""
        from app.core import connector_packages
        kind = connector_packages.kind_for(plugin_id)
        if kind:
            return connector_packages.set_enabled(kind, False)
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

    def _scope_from_project_plugin_id(self, plugin_id: str) -> PluginScope:
        """Map name@project-local / name@project-project onto a PluginScope."""
        marker = plugin_id.rsplit("@", 1)[-1]
        if marker == "project-local":
            return PluginScope.LOCAL
        return PluginScope.PROJECT

    def _get_project_settings_file(self, scope: PluginScope = PluginScope.PROJECT) -> Optional[Path]:
        """Fluid settings file for a project plugin scope.

        LOCAL (gitignored) → <project>/.fluid/settings.local.json
        PROJECT (committed) → <project>/.fluid/settings.json
        """
        if not self.project_dir:
            return None
        if scope == PluginScope.LOCAL:
            return self.project_dir / ".fluid" / "settings.local.json"
        return self.project_dir / ".fluid" / "settings.json"

    def _read_project_settings(self, scope: Optional[PluginScope] = None) -> Dict[str, Any]:
        """Read project Fluid settings. No scope merges both enabledPlugins maps."""
        if scope is None:
            enabled: Dict[str, Any] = {}
            for layer_scope in (PluginScope.PROJECT, PluginScope.LOCAL):
                layer = self._read_project_settings(layer_scope)
                enabled.update(layer.get("enabledPlugins", {}))
            return {"enabledPlugins": enabled}
        settings_file = self._get_project_settings_file(scope)
        if settings_file:
            return self._read_json_file(settings_file, {})
        return {}

    def _write_project_settings(self, data: Dict[str, Any], scope: PluginScope = PluginScope.PROJECT) -> bool:
        """Write one Fluid project-scope settings file."""
        settings_file = self._get_project_settings_file(scope)
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
            scope: LOCAL (.fluid/plugins/) or PROJECT (plugins/)

        Returns:
            PluginInfo for the installed plugin
        """
        return _inst_mod.install_plugin_to_project(self, plugin_id, scope)

    def uninstall_project_plugin(self, plugin_id: str) -> bool:
        """
        Uninstall a plugin from the project directory.

        Args:
            plugin_id: Plugin ID in format "name@project-local" or "name@project-project"

        Returns:
            True if successful
        """
        return _inst_mod.uninstall_project_plugin(self, plugin_id)

    def enable_project_plugin(self, plugin_id: str) -> bool:
        """Enable a plugin in its Fluid project/local settings file."""
        scope = self._scope_from_project_plugin_id(plugin_id)
        settings = self._read_project_settings(scope)
        if "enabledPlugins" not in settings:
            settings["enabledPlugins"] = {}
        settings["enabledPlugins"][plugin_id] = True
        return self._write_project_settings(settings, scope)

    def disable_project_plugin(self, plugin_id: str) -> bool:
        """Disable a plugin in its Fluid project/local settings file."""
        scope = self._scope_from_project_plugin_id(plugin_id)
        settings = self._read_project_settings(scope)
        if "enabledPlugins" not in settings:
            settings["enabledPlugins"] = {}
        settings["enabledPlugins"][plugin_id] = False
        return self._write_project_settings(settings, scope)

    def get_project_enabled_plugins(self) -> Dict[str, bool]:
        """Get all project plugin enable/disable states"""
        settings = self._read_project_settings()
        return settings.get("enabledPlugins", {})

    # =========================================================================
    # File-based Agents (delegated to plugin_hooks module)
    # =========================================================================

    def get_file_based_agents(self) -> List[Dict[str, Any]]:
        """
        Get all file-based agents from installed and enabled plugins.

        Returns:
            List of agent definitions with their source plugin info
        """
        return _hooks_mod.get_file_based_agents(self)

    def _parse_agent_frontmatter(self, content: str) -> Dict[str, Any]:
        """Parse YAML frontmatter from an agent markdown file"""
        return _hooks_mod._parse_agent_frontmatter(content)

    # =========================================================================
    # Auto-Update Settings & Update Methods (delegated to plugin_updates module)
    # =========================================================================

    def get_auto_update_settings(self) -> AutoUpdateSettings:
        """Get auto-update settings"""
        return _upd_mod.get_auto_update_settings(self)

    def save_auto_update_settings(self, settings: AutoUpdateSettings) -> bool:
        """Save auto-update settings"""
        return _upd_mod.save_auto_update_settings(self, settings)

    def _get_marketplace_head_version(self, marketplace_id: str) -> Optional[str]:
        """Get the current version identifier for a marketplace."""
        return _upd_mod._get_marketplace_head_version(self, marketplace_id)

    def _get_plugin_manifest_version(self, local_dir: Path) -> Optional[str]:
        """Read a plugin's version from its .claude-plugin/plugin.json manifest."""
        return _inst_mod._get_plugin_manifest_version(self, local_dir)

    def check_for_updates(self) -> List[PluginUpdateInfo]:
        """
        Check all installed plugins for available updates.

        Returns:
            List of plugins with available updates
        """
        return _upd_mod.check_for_updates(self)

    def update_plugin(self, plugin_id: str) -> PluginInfo:
        """
        Update a single installed plugin to the latest version.

        Args:
            plugin_id: Plugin ID in format "name@marketplace"

        Returns:
            Updated PluginInfo
        """
        from app.core import connector_packages
        kind = connector_packages.kind_for(plugin_id)
        if kind:
            return connector_packages.install(kind)
        return _upd_mod.update_plugin(self, plugin_id)

    def update_all_plugins(self) -> List[PluginInfo]:
        """
        Update all installed plugins that have available updates.

        Returns:
            List of updated PluginInfo objects
        """
        return _upd_mod.update_all_plugins(self)

    def run_auto_update(self) -> Dict[str, Any]:
        """
        Run the full auto-update cycle.

        Returns:
            Summary of actions taken
        """
        return _upd_mod.run_auto_update(self)


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
    return _hooks_mod.get_all_plugin_hooks(get_plugin_service)


def get_hooks_from_plugins(plugins: List["PluginInfo"]) -> List[Dict[str, Any]]:
    """
    Get all hooks from a list of plugins.

    This is a generalized version that works with any list of plugins,
    whether global or project-scoped.

    Returns a list of hook info dicts.
    """
    return _hooks_mod.get_hooks_from_plugins(plugins)


def _parse_plugin_hook_file(hook_file: Path, plugin: "PluginInfo") -> Optional[Dict[str, Any]]:
    """
    Parse a plugin hook markdown file and extract metadata.
    """
    return _hooks_mod._parse_plugin_hook_file(hook_file, plugin)


from app.core.resource_sync import install_service_mutations as _install_resource_sync

_install_resource_sync(PluginService, 'plugins', 'add_marketplace remove_marketplace sync_marketplace sync_all_marketplaces install_plugin install_plugins_batch uninstall_plugin enable_plugin disable_plugin install_plugin_to_project uninstall_project_plugin enable_project_plugin disable_project_plugin update_plugin update_all_plugins save_auto_update_settings')
