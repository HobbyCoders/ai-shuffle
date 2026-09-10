"""
Plugin Models - Data classes, enums, and helpers for the plugin system.
"""

import os
import stat
import shutil
from pathlib import Path
from datetime import datetime
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field, asdict
from enum import Enum

from app.core.platform import is_docker_mode, get_claude_credentials_dir


def _force_rmtree(path):
    """Remove directory tree, handling read-only files on Windows (e.g. .git pack files)."""
    def _on_error(_func, _path, _exc_info):
        os.chmod(_path, stat.S_IWRITE)
        os.unlink(_path)
    shutil.rmtree(path, onerror=_on_error)


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


def _get_fluid_dir() -> Path:
    """Fluid home — user-global plugins and enabledPlugins live here.

    Matches fluid.context.plugins.plugins_home() / the settings stack:
        - Docker: /home/appuser/.fluid
        - Local: ~/.fluid
    """
    if is_docker_mode():
        return Path("/home/appuser/.fluid")
    return Path.home() / ".fluid"


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
    USER = "user"       # ~/.fluid/plugins/
    LOCAL = "local"     # <project>/.fluid/plugins/
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
    connector_type: Optional[str] = None


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
    connector_type: Optional[str] = None
    installed: bool = False
    enabled: bool = False
