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
from datetime import datetime, timezone
from typing import List, NoReturn, Optional, Dict, Any

from app.core.mcp_service import McpConfigUnreadable

# Re-export all models and helpers for backward compatibility
from app.core.plugin_models import (  # noqa: F401
    PluginScope,
    MarketplaceInfo,
    PluginInfo,
    PluginDetails,
    PluginUpdateInfo,
    AutoUpdateSettings,
    AvailablePlugin,
    _get_fluid_dir,
)

# Import delegate modules
from app.core import plugin_marketplace as _mp_mod
from app.core import plugin_install as _inst_mod
from app.core import plugin_hooks as _hooks_mod
from app.core import plugin_updates as _upd_mod
from app.core import plugin_authored as _authored_mod

logger = logging.getLogger(__name__)


# =============================================================================
# Shared plugin reader (fluid.context.plugin_formats)
# =============================================================================
#
# Every plugin directory this service describes is read by fluid's one parser,
# the same one the runtime loads plugins with, so the components listed here
# are the ones that load (Claude Code and Agent Plugins / OpenAI formats).


def plugin_record(path: Path, name: str, source: str, version: Optional[str] = None):
    """The normalized :class:`~fluid.context.plugin_formats.PluginRecord` for
    the plugin at ``path``, or ``None`` when it is missing or unreadable."""
    from fluid.context.plugin_formats import read_plugin

    try:
        if not path.is_dir():
            return None
        return read_plugin(path, name=name, source=source, version=version)
    except Exception as e:  # noqa: BLE001 — one bad plugin never hides the rest
        logger.warning(f"Could not read plugin at {path}: {e}")
        return None


def record_summary(record, install_path: Path) -> Dict[str, Any]:
    """The ``has_*`` flags, format and components the API reports for a plugin."""
    if record is None:
        return {
            "has_commands": False, "has_agents": False, "has_skills": False,
            "has_hooks": False, "has_mcp": False, "format": None, "components": [],
        }
    # Hook prompt files (hooks/*.md) are listed by plugin_hooks without being
    # a hooks.json component; keep counting a hooks folder as "has hooks".
    legacy_hooks = (install_path / "hooks").is_dir() or (install_path / "hooks.json").exists()
    return {
        "has_commands": bool(record.commands),
        "has_agents": bool(record.agents),
        "has_skills": bool(record.skills),
        "has_hooks": record.has("hook") or legacy_hooks,
        "has_mcp": bool(record.mcp_servers),
        "format": record.format,
        "components": [c.to_dict() for c in record.components] + _service_components(record),
    }


def _service_components(record) -> List[Dict[str, Any]]:
    """The outside services a plugin declares (``extensions["ai.shuffle"].services``),
    one ``service`` component per name, so Settings can show what it will ask
    access to. Names only: a declaration holds no credential and grants nothing."""
    manifest = getattr(record, "manifest", None)
    extensions = manifest.get("extensions") if isinstance(manifest, dict) else None
    shuffle = extensions.get("ai.shuffle") if isinstance(extensions, dict) else None
    services = shuffle.get("services") if isinstance(shuffle, dict) else None
    out: List[Dict[str, Any]] = []
    seen = set()
    for item in services if isinstance(services, list) else []:
        name = item.get("name") if isinstance(item, dict) else None
        if not isinstance(name, str) or not name.strip() or name.strip() in seen:
            continue
        seen.add(name.strip())
        out.append({
            "kind": "service", "name": name.strip()[:100], "support": "loaded",
            "reason": "", "detail": "",
        })
    return out[:20]


def record_description(record, install_path: Path) -> str:
    """The manifest description, else the README's first line."""
    if record is not None and record.description:
        return record.description[:200]
    return PluginService._readme_first_line(install_path) or ""


def _component_names(record) -> tuple:
    """``(commands, agents, skills)`` names as the details view lists them."""
    if record is None:
        return [], [], []

    def local(name: str) -> str:
        return name.split(":", 1)[-1]

    return (
        [local(c.name) for c in record.commands],
        [local(a.name) for a in record.agents],
        [local(s.name) for s in record.skills],
    )


def _read_readme(directory: Path) -> Optional[str]:
    readme_path = directory / "README.md"
    if not readme_path.exists():
        return None
    try:
        return readme_path.read_text(encoding='utf-8')
    except (IOError, UnicodeDecodeError):
        return None


def _pending(install: Any) -> Optional[str]:
    """``"pending"`` while an agent-installed plugin waits for the owner's
    review (``app.core.plugin_install._agent_request``), else None."""
    return "pending" if isinstance(install, dict) and install.get("approval") == "pending" else None


def _trust_for(marketplace: str) -> str:
    return "authored" if marketplace == "authored" else "marketplace"


class PluginConfigUnreadable(McpConfigUnreadable):
    """A plugin settings/registry file exists but cannot be read, so it is not rewritten.

    Same 409 and message as :class:`McpConfigUnreadable` (the plugins API
    lets it through like any HTTPException); when the bytes were readable,
    the detail also names the ``<name>.<timestamp>.bak`` copy.
    """

    def __init__(self, path: Path, backup: Optional[Path] = None):
        super().__init__(path)
        self.backup = backup
        if backup is not None:
            self.detail = f"{self.detail} A copy of it was saved to {backup}."


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

    def _read_json_file(self, path: Path, default: Any = None, *, for_update: bool = False) -> Any:
        """Read a JSON object file.

        A missing file reads as ``default`` (``{}``). An unreadable or corrupt
        one (permission denied, not UTF-8, not JSON, not an object) also reads
        as ``default`` for display, but with ``for_update`` it raises
        :class:`PluginConfigUnreadable`: the caller is about to write the
        result back, and writing what an unreadable file "read as" would
        replace the whole file.
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
                self._refuse_unreadable(path, e)
            return fallback

    # Backups of refused files: <name>.<UTC timestamp>.bak, never overwritten,
    # the newest few kept per file. A hand-made <name>.bak is never touched.
    _BACKUP_KEEP = 5
    _BACKUP_STAMP = re.compile(r"^(\d{8}T\d{12}Z)(?:-(\d+))?$")

    @classmethod
    def _backups_of(cls, path: Path) -> List[Path]:
        """This module's backups of ``path``, oldest first."""
        prefix, suffix = path.name + ".", ".bak"
        found = []
        try:
            siblings = list(path.parent.iterdir())
        except OSError:
            return []
        for sibling in siblings:
            name = sibling.name
            if name.startswith(prefix) and name.endswith(suffix):
                match = cls._BACKUP_STAMP.match(name[len(prefix):-len(suffix)])
                if match:
                    found.append(((match.group(1), int(match.group(2) or 0)), sibling))
        return [backup for _key, backup in sorted(found)]

    @classmethod
    def _keep_backup(cls, path: Path, raw: bytes) -> Optional[Path]:
        """Save ``raw`` (the bytes of a file that failed to parse) beside ``path``.

        Written to ``<name>.<UTC timestamp>.bak`` with exclusive create, so no
        earlier backup (and no ``<name>.bak`` the owner made) is ever
        overwritten. When a kept backup already holds these bytes, that one is
        returned instead of a new copy, so repeated background refusals of the
        same file do not rotate the original out. Only the newest
        ``_BACKUP_KEEP`` backups of each file are kept.
        """
        existing = cls._backups_of(path)
        for backup in reversed(existing):
            try:
                if backup.read_bytes() == raw:
                    return backup
            except OSError:
                continue
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = None
        try:
            for attempt in range(100):
                candidate = path.with_name(
                    f"{path.name}.{stamp}{'' if attempt == 0 else f'-{attempt}'}.bak"
                )
                try:
                    with open(candidate, "xb") as f:
                        f.write(raw)
                except FileExistsError:
                    continue
                backup = candidate
                break
        except OSError as e:
            logger.error(f"Could not back up {path}: {e}")
            return None
        if backup is None:
            logger.error(f"Could not back up {path}: no free backup name")
            return None
        for old in cls._backups_of(path)[:-cls._BACKUP_KEEP]:
            try:
                old.unlink()
            except OSError as e:
                logger.warning(f"Could not remove old backup {old}: {e}")
        return backup

    def _refuse_unreadable(self, path: Path, cause: BaseException) -> NoReturn:
        """Raise :class:`PluginConfigUnreadable` for ``path``, leaving it untouched.

        When the file's bytes can be read (it failed to parse rather than to
        open), a copy is kept at ``<name>.<timestamp>.bak`` (see
        :meth:`_keep_backup`) so the owner can recover it even after deleting
        or hand-editing the original.
        """
        try:
            raw: Optional[bytes] = path.read_bytes()
        except OSError:
            raw = None
        backup = self._keep_backup(path, raw) if raw is not None else None
        raise PluginConfigUnreadable(path, backup) from cause

    def _check_replaceable(self, path: Path) -> None:
        """Refuse to replace a file that exists but is not a readable JSON object.

        Every read that precedes a write uses ``for_update=True``, so a file
        caught mid-write after the up-front check (see
        :meth:`_check_files_replaceable`) is refused rather than read as
        ``{}`` and written back; this check on the write itself is the last
        guard. A missing file is fine: the write creates it.

        The auto-update settings file is the exception: it is always written
        as a whole record (never merged into), so one that fails to parse is
        backed up (see :meth:`_keep_backup`) and replaced; otherwise it could never be fixed
        from the app and every background run would fail. One that cannot be
        opened at all is still refused, since no copy can be kept.
        """
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return
        except OSError as e:
            self._refuse_unreadable(path, e)
        try:
            data = json.loads(raw.decode('utf-8'))
            if isinstance(data, dict):
                return
            cause: Exception = ValueError("top-level JSON is not an object")
        except ValueError as e:
            cause = e
        if path == self.AUTO_UPDATE_FILE:
            logger.error(f"Replacing unreadable {path}: {cause}")
            self._keep_backup(path, raw)
            return
        self._refuse_unreadable(path, cause)

    def _check_files_replaceable(self, *paths: Optional[Path]) -> None:
        """Run :meth:`_check_replaceable` on every file an operation will write.

        Called at the top of each multi-step change (install, uninstall,
        update, marketplace add/sync/remove, project install/uninstall) before
        it deletes, clones or copies anything, so a 409 for one unreadable file
        leaves the whole change unapplied and the request can be retried once
        the file is fixed. ``None`` entries (no project directory) are skipped.
        """
        for path in paths:
            if path is not None:
                self._check_replaceable(path)

    def _write_json_file(self, path: Path, data: Any) -> bool:
        """Atomically write a JSON file.

        Raises :class:`PluginConfigUnreadable` (409) instead of replacing an
        existing file that cannot be read as a JSON object.
        """
        self._check_replaceable(path)
        try:
            temp_path = path.with_suffix('.tmp')
            with open(temp_path, 'w', encoding='utf-8') as f:
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
        """Read a marketplace catalog from a marketplace directory.

        ``.claude-plugin/marketplace.json`` (Claude Code), else
        ``.agents/plugins/marketplace.json`` (Agent Plugins / OpenAI), whose
        entries fluid's reader rewrites into the Claude ``source`` shapes the
        install path resolves.
        """
        from fluid.context.plugin_formats import read_marketplace

        catalog = read_marketplace(mp_path)
        if catalog is None:
            return None
        if catalog.skipped:
            logger.info(
                f"Marketplace {mp_path.name}: skipping entries with unsupported sources: "
                f"{', '.join(catalog.skipped[:10])}"
            )
        return catalog.data

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

            entries = manifest.get("plugins")
            for entry in entries if isinstance(entries, list) else []:
                # One malformed entry must not take down the whole listing.
                if not isinstance(entry, dict):
                    continue
                plugin_name = entry.get("name")
                if not plugin_name or not isinstance(plugin_name, str):
                    continue
                try:
                    _inst_mod._safe_path_component(plugin_name, "name")
                except ValueError:
                    logger.warning(f"Skipping plugin with unsafe name in {mp.id}: {plugin_name[:100]!r}")
                    continue

                plugin_id = f"{plugin_name}@{mp.id}"
                source = entry.get("source", "")
                description = entry.get("description")
                description = description[:200] if isinstance(description, str) else ""

                # Try to resolve to a local directory for component detection
                local_dir = self._resolve_plugin_source(source, mp_path, plugin_root)

                record = None
                if local_dir:
                    # Local source - read the plugin with the shared parser
                    record = plugin_record(local_dir, plugin_name, mp.id)
                    components = record_summary(record, local_dir)
                    mp_path_str = str(local_dir)
                    if not description:
                        description = record_description(record, local_dir)
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
                    enabled=enabled_plugins.get(plugin_id, False),
                    format=record.format if record else None,
                    components=components.get("components", []),
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

            record = plugin_record(install_path, name, marketplace, install.get("version"))
            summary = record_summary(record, install_path)

            plugins.append(PluginInfo(
                id=plugin_id,
                name=name,
                marketplace=marketplace,
                description=record_description(record, install_path),
                version=install.get("version", "unknown"),
                scope=install.get("scope", "user"),
                install_path=str(install_path),
                installed_at=installed_at,
                enabled=enabled_plugins.get(plugin_id, False),
                trust=_trust_for(marketplace),
                approval=_pending(install),
                **summary,
            ))

        from app.core import connector_packages
        plugins.extend(connector_packages.installed())
        plugins.extend(_authored_mod.list_authored(self, enabled_plugins))
        return plugins

    def get_plugin_details(self, plugin_id: str) -> Optional[PluginDetails]:
        """Get detailed information about a specific plugin"""
        from app.core import connector_packages
        kind = connector_packages.kind_for(plugin_id)
        if kind:
            return connector_packages.details(kind)
        if _authored_mod.is_authored(plugin_id):
            return _authored_mod.details(self, plugin_id)
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

        record = plugin_record(install_path, name, marketplace, install.get("version"))
        summary = record_summary(record, install_path)
        commands, agents, skills = _component_names(record)

        return PluginDetails(
            id=plugin_id,
            name=name,
            marketplace=marketplace,
            description=record_description(record, install_path),
            version=install.get("version", "unknown"),
            scope=install.get("scope", "user"),
            install_path=str(install_path),
            installed_at=installed_at,
            enabled=enabled_plugins.get(plugin_id, False),
            trust=_trust_for(marketplace),
            approval=_pending(install),
            **summary,
            commands=commands,
            agents=agents,
            skills=skills,
            readme=_read_readme(install_path),
        )

    def _implicit_component_names(self, root: Path) -> tuple:
        """Component names of a marketplace-less repo added as a source."""
        commands_dir = self._resolve_component_dir(root, "commands")
        agents_dir = self._resolve_component_dir(root, "agents")
        skills_dir = self._resolve_component_dir(root, "skills")
        commands = [f.stem for f in commands_dir.glob("**/*.md")] if commands_dir else []
        agents = [f.stem for f in agents_dir.glob("**/*.md")] if agents_dir else []
        skills = [
            d.name for d in skills_dir.iterdir()
            if d.is_dir() and (d / "SKILL.md").exists()
        ] if skills_dir else []
        return commands, agents, skills

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
                trust=_trust_for(marketplace),
                format=plugin.format,
                components=plugin.components,
                commands=[],
                agents=[],
                skills=[],
                readme=readme
            )

        record = plugin_record(plugin_path, name, marketplace)
        summary = record_summary(record, plugin_path)
        commands, agents, skills = _component_names(record)
        if not (commands or agents or skills):
            # An implicit, marketplace-less plugin keeps its components at the
            # repo root or under .claude/ or .agents/ (see plugin_install).
            commands, agents, skills = self._implicit_component_names(plugin_path)
            summary.update(
                has_commands=bool(commands), has_agents=bool(agents), has_skills=bool(skills),
            )

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
            trust=_trust_for(marketplace),
            **summary,
            commands=commands,
            agents=agents,
            skills=skills,
            readme=_read_readme(plugin_path)
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

    def install_plugin(
        self, plugin_id: str, scope: str = "user", enable: bool = True,
        requested_by: Optional[Dict[str, Any]] = None,
    ) -> PluginInfo:
        """
        Install a plugin from a marketplace.

        Args:
            plugin_id: Plugin ID in format "name@marketplace"
            scope: Installation scope ("user" or "project")
            enable: False installs it switched off, for the owner to enable

        Returns:
            PluginInfo for the installed plugin
        """
        from app.core import connector_packages
        kind = connector_packages.kind_for(plugin_id)
        if kind:
            return connector_packages.install(kind, scope)
        return _inst_mod.install_plugin(self, plugin_id, scope, enable, requested_by)

    def install_plugins_batch(
        self, plugin_ids: List[str], scope: str = "user", enable: bool = True
    ) -> List[PluginInfo]:
        """
        Install multiple plugins in batch.

        Args:
            plugin_ids: List of plugin IDs to install
            scope: Installation scope
            enable: False installs them switched off, for the owner to enable

        Returns:
            List of installed plugin info
        """
        return [self.install_plugin(plugin_id, scope, enable) for plugin_id in plugin_ids]

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
        if _authored_mod.is_authored(plugin_id):
            return _authored_mod.uninstall(self, plugin_id)
        return _inst_mod.uninstall_plugin(self, plugin_id)

    # =========================================================================
    # Plugin Enable/Disable
    # =========================================================================

    def enable_plugin(self, plugin_id: str, content_hash: Optional[str] = None) -> bool:
        """Enable a plugin globally.

        An authored plugin is approved at the same time, pinned to
        ``content_hash`` (the hash of the version the owner reviewed); see
        :mod:`app.core.plugin_authored`.
        """
        from app.core import connector_packages
        kind = connector_packages.kind_for(plugin_id)
        if kind:
            return connector_packages.set_enabled(kind, True)
        if _authored_mod.is_authored(plugin_id):
            return _authored_mod.enable(self, plugin_id, content_hash)
        settings = self._read_json_file(self.SETTINGS_FILE, {}, for_update=True)
        if "enabledPlugins" not in settings:
            settings["enabledPlugins"] = {}
        settings["enabledPlugins"][plugin_id] = True
        written = self._write_json_file(self.SETTINGS_FILE, settings)
        self._clear_pending(plugin_id)
        return written

    def _clear_pending(self, plugin_id: str) -> None:
        """The owner enabled an agent-installed plugin: it no longer waits
        for review. Who asked stays in the record."""
        if not self.INSTALLED_FILE.exists():
            return
        installed = self._read_json_file(self.INSTALLED_FILE, {"version": 2, "plugins": {}}, for_update=True)
        data = installed.get("plugins", {}).get(plugin_id)
        records = data if isinstance(data, list) else [data]
        changed = False
        for record in records:
            if isinstance(record, dict) and record.pop("approval", None) is not None:
                changed = True
        if changed:
            self._write_json_file(self.INSTALLED_FILE, installed)

    def disable_plugin(self, plugin_id: str) -> bool:
        """Disable a plugin globally"""
        from app.core import connector_packages
        kind = connector_packages.kind_for(plugin_id)
        if kind:
            return connector_packages.set_enabled(kind, False)
        if _authored_mod.is_authored(plugin_id):
            return _authored_mod.disable(self, plugin_id)
        settings = self._read_json_file(self.SETTINGS_FILE, {}, for_update=True)
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

    def _read_project_settings(
        self, scope: Optional[PluginScope] = None, *, for_update: bool = False
    ) -> Dict[str, Any]:
        """Read project Fluid settings. No scope merges both enabledPlugins maps.

        ``for_update`` (single scope only) refuses an unreadable file; see
        :meth:`_read_json_file`.
        """
        if scope is None:
            enabled: Dict[str, Any] = {}
            for layer_scope in (PluginScope.PROJECT, PluginScope.LOCAL):
                layer = self._read_project_settings(layer_scope)
                enabled.update(layer.get("enabledPlugins", {}))
            return {"enabledPlugins": enabled}
        settings_file = self._get_project_settings_file(scope)
        if settings_file:
            return self._read_json_file(settings_file, {}, for_update=for_update)
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

                record = plugin_record(plugin_dir, plugin_name, f"project-{scope.value}")
                summary = record_summary(record, plugin_dir)
                version = (record.manifest.get("version") if record else None) or "1.0.0"

                # Get file timestamps
                dir_stat = plugin_dir.stat()
                installed_at = datetime.fromtimestamp(dir_stat.st_ctime)

                plugins.append(PluginInfo(
                    id=plugin_id,
                    name=plugin_name,
                    marketplace="project",
                    description=record_description(record, plugin_dir),
                    version=str(version),
                    scope=scope.value,
                    install_path=str(plugin_dir),
                    installed_at=installed_at,
                    enabled=enabled_plugins.get(plugin_id, True),  # Default enabled
                    trust="marketplace",
                    **summary,
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
        settings = self._read_project_settings(scope, for_update=True)
        if "enabledPlugins" not in settings:
            settings["enabledPlugins"] = {}
        settings["enabledPlugins"][plugin_id] = True
        return self._write_project_settings(settings, scope)

    def disable_project_plugin(self, plugin_id: str) -> bool:
        """Disable a plugin in its Fluid project/local settings file."""
        scope = self._scope_from_project_plugin_id(plugin_id)
        settings = self._read_project_settings(scope, for_update=True)
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
