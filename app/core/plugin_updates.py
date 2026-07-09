"""
Plugin Updates - Update checking and auto-update logic for the plugin system.

Provides standalone functions that operate on a PluginService instance.
"""

import logging
import subprocess
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any

from app.core.plugin_models import (
    PluginInfo,
    PluginUpdateInfo,
    AutoUpdateSettings,
)

logger = logging.getLogger(__name__)


def get_auto_update_settings(svc) -> AutoUpdateSettings:
    """Get auto-update settings."""
    data = svc._read_json_file(svc.AUTO_UPDATE_FILE, {})
    return AutoUpdateSettings(
        enabled=data.get("enabled", True),
        interval_hours=data.get("intervalHours", 6),
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


def check_for_updates(svc) -> List[PluginUpdateInfo]:
    """
    Check all installed plugins for available updates.

    Compares the installed version (git hash or semver) against the
    current marketplace HEAD. For local-source plugins, a version
    mismatch means the marketplace has been updated since install.

    Args:
        svc: PluginService instance

    Returns:
        List of plugins with available updates
    """
    installed_data = svc._read_json_file(svc.INSTALLED_FILE, {"version": 2, "plugins": {}})
    file_version = installed_data.get("version", 1)
    updates: List[PluginUpdateInfo] = []

    # Cache marketplace HEAD versions to avoid repeated git calls
    mp_head_cache: Dict[str, Optional[str]] = {}
    marketplaces = svc._read_json_file(svc.MARKETPLACES_FILE, {})

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

        # Implicit plugins (marketplace-less repos installed as a single plugin)
        # have no manifest entry to diff against -- skip update checks entirely,
        # else they would report a spurious update every cycle.
        if isinstance(install, dict) and install.get("implicit"):
            continue

        parts = plugin_id.rsplit("@", 1)
        if len(parts) != 2:
            continue

        plugin_name, marketplace_id = parts
        installed_version = install.get("version", "unknown")

        # Determine locality from marketplace entry, not just installed data.
        # Old installs may lack the isLocal field entirely.
        entry = svc._find_plugin_in_manifest(marketplace_id, plugin_name)
        if entry:
            source = entry.get("source", "")
            is_local = isinstance(source, str)
        else:
            is_local = install.get("isLocal", True)

        # For local-source plugins: read the plugin's own manifest version
        # from the marketplace source directory.  This matches Claude Code's
        # behaviour -- manifest.version is the canonical version, NOT the
        # marketplace repo's git/gcs hash.
        if is_local:
            # Resolve the local source directory for this plugin
            mp_data = marketplaces.get(marketplace_id, {})
            mp_location = mp_data.get("installLocation")

            available_version = None
            if mp_location and entry:
                mp_path = Path(mp_location)
                mp_manifest = svc._get_marketplace_manifest(mp_path)
                mp_plugin_root = (
                    mp_manifest.get("metadata", {}).get("pluginRoot")
                    if mp_manifest and isinstance(mp_manifest.get("metadata"), dict)
                    else None
                )
                local_dir = svc._resolve_plugin_source(
                    entry.get("source", ""), mp_path, mp_plugin_root
                )
                if local_dir:
                    available_version = svc._get_plugin_manifest_version(local_dir)

            # Fall back chain: entry.version -> marketplace HEAD hash.
            # Track whether the version is from a "real" source (manifest/entry)
            # vs a hash fallback, to avoid comparing apples to oranges.
            version_from_manifest = available_version is not None
            if not available_version:
                available_version = entry.get("version") if entry else None
                if available_version:
                    version_from_manifest = True
            if not available_version:
                if marketplace_id not in mp_head_cache:
                    mp_head_cache[marketplace_id] = svc._get_marketplace_head_version(marketplace_id)
                available_version = mp_head_cache[marketplace_id]
                # This is a repo-level hash, not a plugin manifest version

            if not available_version:
                continue

            # Only compare versions from the same system.  If installed
            # version is "unknown" (plugin has no manifest version) and
            # available came from a repo hash, skip -- they're incomparable.
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
                    # Explicit version in marketplace entry -- compare directly
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
                    # No explicit version -- compare source SHAs
                    available_sha = source["sha"][:7]
                    installed_sha = install.get("sourceSha", "")
                    if installed_sha:
                        # We stored SHA at install time -- compare directly
                        if available_sha != installed_sha:
                            cache_dir = svc.CACHE_DIR / marketplace_id / plugin_name / available_sha
                            cached_ver = svc._get_plugin_manifest_version(cache_dir)
                            updates.append(PluginUpdateInfo(
                                plugin_id=plugin_id,
                                name=plugin_name,
                                marketplace=marketplace_id,
                                installed_version=installed_version,
                                available_version=cached_ver or available_sha,
                                is_local=False
                            ))
                    else:
                        # Legacy install without sourceSha -- check cached
                        # copy at new SHA for its manifest version
                        cache_dir = svc.CACHE_DIR / marketplace_id / plugin_name / available_sha
                        cached_ver = svc._get_plugin_manifest_version(cache_dir)
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
                            # No cached copy -- only compare SHA-to-SHA
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


def update_plugin(svc, plugin_id: str) -> PluginInfo:
    """
    Update a single installed plugin to the latest version.

    Preserves the plugin's enabled state across the re-install.

    Args:
        svc: PluginService instance
        plugin_id: Plugin ID in format "name@marketplace"

    Returns:
        Updated PluginInfo
    """
    installed_data = svc._read_json_file(svc.INSTALLED_FILE, {"version": 2, "plugins": {}})
    if plugin_id not in installed_data.get("plugins", {}):
        raise ValueError(f"Plugin not installed: {plugin_id}")

    # Get current scope and enabled state
    installation_data = installed_data["plugins"][plugin_id]
    if isinstance(installation_data, list):
        install = installation_data[0] if installation_data else {}
    else:
        install = installation_data
    scope = install.get("scope", "user")

    settings = svc._read_json_file(svc.SETTINGS_FILE, {})
    was_enabled = settings.get("enabledPlugins", {}).get(plugin_id, False)

    # Re-install (this overwrites the cache and updates installed_plugins.json)
    result = svc.install_plugin(plugin_id, scope)

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
    updates = svc.check_for_updates()
    results: List[PluginInfo] = []

    for update in updates:
        try:
            result = svc.update_plugin(update.plugin_id)
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
        updates = svc.check_for_updates()
        summary["updates_available"] = [u.to_dict() for u in updates]
    except Exception as e:
        logger.error(f"Failed to check for updates: {e}")
        summary["errors"].append(f"Update check failed: {e}")
        updates = []

    # 3. Auto-update plugins
    if au_settings.auto_update_plugins and updates:
        for update in updates:
            try:
                result = svc.update_plugin(update.plugin_id)
                summary["updated_plugins"].append(result.to_dict())
            except Exception as e:
                error_msg = f"Failed to update {update.plugin_id}: {e}"
                logger.error(error_msg)
                summary["errors"].append(error_msg)

    # 4. Update last check timestamp
    au_settings.last_check = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    svc.save_auto_update_settings(au_settings)

    return summary
