"""
Plugin Updates - Update checking and auto-update logic for the plugin system.

Provides standalone functions that operate on a PluginService instance.
"""

import logging
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.plugin_models import (
    AutoUpdateSettings,
    PluginInfo,
    PluginUpdateInfo,
)
from app.core.process_tree import child_env

logger = logging.getLogger(__name__)


# No version-parsing library is a declared runtime dependency. Keep the small
# SemVer precedence rule here (including our existing 2/4-part release labels).
# Require a dot so an all-digit git hash is never mistaken for a release.
_RELEASE_VERSION = re.compile(
    r"v?(\d+(?:\.\d+){1,3})(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)


def _release_key(value):
    match = _RELEASE_VERSION.fullmatch(str(value or ""))
    if not match:
        return None
    release = tuple(int(part) for part in match[1].split("."))
    prerelease = match[2]
    identifiers = tuple((0, int(part)) if part.isdigit() else (1, part)
                        for part in prerelease.split(".")) if prerelease else ()
    return release + (0,) * (4 - len(release)), not prerelease, identifiers


def _compare_versions(installed: str, available: str) -> Optional[int]:
    """Candidate precedence: -1 lower, 0 equivalent, 1 higher; None for hashes.

    Ignore build metadata; numeric prerelease identifiers precede text, and a
    release follows all its prereleases. Prefixes and trailing zeros are aliases.
    """
    if installed == available:
        return 0
    current, candidate = _release_key(installed), _release_key(available)
    if current is None or candidate is None:
        return None
    return (candidate > current) - (candidate < current)


def _is_update(installed: str, available: str) -> bool:
    order = _compare_versions(installed, available)
    return available != installed if order is None else order > 0


# ─── Auto-update defaults ─────────────────────────────────────────────────
#
# DELIBERATE DEFAULT CHANGE: auto-update is OFF unless the user turns it on.
#
# The old default ran, on every install, an unrequested background burst every
# six hours: a ``git fetch``-class subprocess per registered marketplace, network
# I/O for each, and a re-install of every plugin found to be behind. On a weak
# machine that is a visible stall, it can change what the agent is running
# underneath a live session, and nobody asked for it. Nothing about the product
# needs plugins to update by themselves.
#
# It stays fully configurable — the settings file and the
# ``PUT /api/v1/plugins/auto-update/settings`` endpoint are unchanged, and an
# install that has ALREADY opted in has ``enabled`` written in its settings file,
# so this default does not turn anything off that a user turned on. The cadence
# default also moves 6h -> 24h: for a mechanism whose whole job is picking up
# occasional upstream releases, four checks a day was never buying anything over
# one, and each check is the expensive part.
DEFAULT_AUTO_UPDATE_ENABLED = False
DEFAULT_AUTO_UPDATE_INTERVAL_HOURS = 24


def get_auto_update_settings(svc) -> AutoUpdateSettings:
    """Get auto-update settings (defaults: see the block above)."""
    data = svc._read_json_file(svc.AUTO_UPDATE_FILE, {})
    return AutoUpdateSettings(
        enabled=data.get("enabled", DEFAULT_AUTO_UPDATE_ENABLED),
        interval_hours=data.get("intervalHours", DEFAULT_AUTO_UPDATE_INTERVAL_HOURS),
        auto_sync_marketplaces=data.get("autoSyncMarketplaces", True),
        auto_update_plugins=data.get("autoUpdatePlugins", True),
        last_check=data.get("lastCheck")
    )


def save_auto_update_settings(svc, settings: AutoUpdateSettings) -> bool:
    """Save auto-update settings."""
    data = {
        "enabled": settings.enabled,
        "intervalHours": settings.interval_hours,
        "autoSyncMarketplaces": settings.auto_sync_marketplaces,
        "autoUpdatePlugins": settings.auto_update_plugins,
        "lastCheck": settings.last_check
    }
    return svc._write_json_file(svc.AUTO_UPDATE_FILE, data)


def _get_marketplace_head_version(svc, marketplace_id: str) -> Optional[str]:
    """Get the current version identifier for a marketplace.

    Checks in order:
    1. Git HEAD short hash (git-based marketplaces)
    2. .gcs-sha file content (GCS-distributed marketplaces)
    3. None if neither is available
    """
    marketplaces = svc._read_json_file(svc.MARKETPLACES_FILE, {})
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
            timeout=10, env=child_env(),
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


def check_for_updates(svc, *, allow_remote: bool = False) -> List[PluginUpdateInfo]:
    """Compare local candidates; passive reads use cached external checks.

    A catalog label or SHA is not the candidate's canonical manifest version.
    Every source goes through the same content/version guard before an update
    is offered. A broken plugin is isolated from the rest of the list.
    """
    import hashlib
    import json
    from app.core.plugin_update_content import content_update

    installed = svc._read_json_file(svc.INSTALLED_FILE, {"plugins": {}})
    marketplaces = svc._read_json_file(svc.MARKETPLACES_FILE, {})
    cache_path = svc.PLUGINS_DIR / "update_checks.json"
    cached = svc._read_json_file(cache_path, {})
    refreshed = {}
    updates = []
    for plugin_id, records in installed.get("plugins", {}).items():
        if "@" not in plugin_id or not records:
            continue
        install = records[0] if isinstance(records, list) else records
        if not isinstance(install, dict):
            continue
        name, marketplace_id = plugin_id.rsplit("@", 1)
        marketplace = marketplaces.get(marketplace_id, {})
        entry = svc._find_plugin_in_manifest(marketplace_id, name) or {}
        external = not install.get("implicit") and isinstance(entry.get("source"), dict)
        key = hashlib.sha256(json.dumps(
            [install, entry, marketplace], sort_keys=True,
        ).encode()).hexdigest()
        try:
            if external and not allow_remote:
                previous = cached.get(plugin_id, {})
                data = previous.get("update") if previous.get("key") == key else None
                update = PluginUpdateInfo(**data) if data else None
            else:
                update = content_update(svc, plugin_id, install, marketplace)
            if update:
                updates.append(update)
            if external and allow_remote:
                refreshed[plugin_id] = {"key": key, "update": update.to_dict() if update else None}
        except Exception as exc:
            logger.warning("Cannot compare plugin %s: %s", plugin_id, exc)
    if allow_remote:
        svc._write_json_file(cache_path, refreshed)
    return updates


def refresh_update_sources(svc) -> None:
    """Refresh only sources with installed plugins, for an explicit owner check."""
    installed = svc._read_json_file(svc.INSTALLED_FILE, {"plugins": {}})
    marketplaces = svc._read_json_file(svc.MARKETPLACES_FILE, {})
    ids = {p.rsplit("@", 1)[1] for p in installed.get("plugins", {}) if "@" in p}
    for marketplace_id in sorted(ids & marketplaces.keys()):
        try:
            svc.sync_marketplace(marketplace_id)
        except Exception as exc:
            logger.warning("Cannot refresh plugin source %s: %s", marketplace_id, exc)


def update_plugin(svc, plugin_id: str, refresh_source: bool = True) -> PluginInfo:
    """
    Update a single installed plugin to the latest version.

    Preserves the plugin's enabled state across the re-install.

    Args:
        svc: PluginService instance
        plugin_id: Plugin ID in format "name@marketplace"

    Returns:
        Updated PluginInfo
    """
    # Refuse (409) before re-installing if a file this writes cannot be read
    # back (a corrupt registry would otherwise read as "not installed").
    svc._check_files_replaceable(svc.INSTALLED_FILE, svc.SETTINGS_FILE)
    installed_data = svc._read_json_file(svc.INSTALLED_FILE, {"version": 2, "plugins": {}}, for_update=True)
    if plugin_id not in installed_data.get("plugins", {}):
        raise ValueError(f"Plugin not installed: {plugin_id}")

    # Get current scope and enabled state
    installation_data = installed_data["plugins"][plugin_id]
    if isinstance(installation_data, list):
        install = installation_data[0] if installation_data else {}
    else:
        install = installation_data
    scope = install.get("scope", "user")

    settings = svc._read_json_file(svc.SETTINGS_FILE, {}, for_update=True)
    was_enabled = settings.get("enabledPlugins", {}).get(plugin_id, False)

    # A manual update must not reinstall yesterday's marketplace checkout.
    # Scheduled updates control source syncing through their own setting.
    if refresh_source:
        svc.sync_marketplace(plugin_id.rsplit("@", 1)[1])
    from app.core.plugin_install import _install_implicit_plugin, discover_implicit_components, install_plugin

    if install.get("implicit"):
        # Updates cannot promote a passive repo to a catalog's hooks/MCP.
        # A fresh owner install is required to change package type.
        marketplace_id = plugin_id.rsplit("@", 1)[1]
        marketplace = svc._read_json_file(svc.MARKETPLACES_FILE, {})[marketplace_id]
        root = Path(marketplace["installLocation"])
        components = discover_implicit_components(svc, root)
        if not components["any"]:
            raise ValueError("Implicit plugin has no confined components")
        result = _install_implicit_plugin(
            svc, plugin_id, marketplace_id, root, components, scope, was_enabled,
        )
    else:
        result = install_plugin(
            svc, plugin_id, scope, enable=was_enabled, minimum_version=install.get("version"),
        )

    # Restore enabled state
    if was_enabled:
        svc.enable_plugin(plugin_id)
    else:
        svc.disable_plugin(plugin_id)

    logger.info(f"Updated plugin: {plugin_id} -> {result.version}")
    return result


def update_all_plugins(svc) -> List[PluginInfo]:
    """
    Update all installed plugins that have available updates.

    Args:
        svc: PluginService instance

    Returns:
        List of updated PluginInfo objects
    """
    updates = svc.check_for_updates(refresh_sources=True)
    results: List[PluginInfo] = []

    for update in updates:
        try:
            result = svc.update_plugin(update.plugin_id, refresh_source=False)
            results.append(result)
        except Exception as e:
            logger.error(f"Failed to update plugin {update.plugin_id}: {e}")

    return results


def run_auto_update(svc) -> Dict[str, Any]:
    """
    Run the full auto-update cycle:
    1. Sync all marketplaces (if enabled)
    2. Check for updates
    3. Update plugins (if enabled)
    4. Update last_check timestamp

    Args:
        svc: PluginService instance

    Returns:
        Summary of actions taken
    """
    au_settings = svc.get_auto_update_settings()
    summary: Dict[str, Any] = {
        "synced_marketplaces": {},
        "updates_available": [],
        "updated_plugins": [],
        "errors": []
    }

    # 1. Sync marketplaces
    if au_settings.auto_sync_marketplaces:
        summary["synced_marketplaces"] = svc.sync_all_marketplaces()

    # 2. Check for updates
    try:
        updates = check_for_updates(svc, allow_remote=au_settings.auto_sync_marketplaces)
        summary["updates_available"] = [u.to_dict() for u in updates]
    except Exception as e:
        logger.error(f"Failed to check for updates: {e}")
        summary["errors"].append(f"Update check failed: {e}")
        updates = []

    # 3. Auto-update plugins
    if au_settings.auto_update_plugins and updates:
        for update in updates:
            try:
                result = svc.update_plugin(update.plugin_id, refresh_source=False)
                summary["updated_plugins"].append(result.to_dict())
            except Exception as e:
                error_msg = f"Failed to update {update.plugin_id}: {e}"
                logger.error(error_msg)
                summary["errors"].append(error_msg)

    # 4. Update last check timestamp
    au_settings.last_check = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    svc.save_auto_update_settings(au_settings)

    return summary
