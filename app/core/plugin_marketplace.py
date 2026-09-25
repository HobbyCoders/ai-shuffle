"""
Plugin Marketplace - Marketplace management operations for the plugin system.

Provides standalone functions that operate on a PluginService instance.
"""

import logging
import re
import shutil
import subprocess
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Optional, Dict

from app.core.plugin_models import MarketplaceInfo

logger = logging.getLogger(__name__)


def get_marketplaces(svc) -> List[MarketplaceInfo]:
    """Get all registered marketplaces."""
    data = svc._read_json_file(svc.MARKETPLACES_FILE, {})
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


def add_marketplace(svc, url: str, name: Optional[str] = None) -> MarketplaceInfo:
    """
    Add a new marketplace from a git URL.

    Args:
        svc: PluginService instance
        url: Git repository URL
        name: Optional marketplace name (derived from URL if not provided)

    Returns:
        MarketplaceInfo for the new marketplace
    """
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

    install_location = str(svc.MARKETPLACES_DIR / name)

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
    data = svc._read_json_file(svc.MARKETPLACES_FILE, {})
    data[name] = {
        "source": {
            "source": "git",
            "url": url
        },
        "installLocation": install_location,
        "lastUpdated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    }
    svc._write_json_file(svc.MARKETPLACES_FILE, data)

    return MarketplaceInfo(
        id=name,
        source="git",
        url=url,
        install_location=install_location,
        last_updated=datetime.now(timezone.utc)
    )


def remove_marketplace(svc, marketplace_id: str) -> bool:
    """
    Remove a marketplace and all its installed plugins.

    Args:
        svc: PluginService instance
        marketplace_id: The marketplace identifier

    Returns:
        True if successful
    """
    data = svc._read_json_file(svc.MARKETPLACES_FILE, {})

    if marketplace_id not in data:
        raise ValueError(f"Marketplace not found: {marketplace_id}")

    install_location = data[marketplace_id].get("installLocation")

    # Remove from known_marketplaces.json
    del data[marketplace_id]
    svc._write_json_file(svc.MARKETPLACES_FILE, data)

    # Remove marketplace directory
    if install_location and Path(install_location).exists():
        shutil.rmtree(install_location, ignore_errors=True)

    # Remove installed plugins from this marketplace
    installed = svc._read_json_file(svc.INSTALLED_FILE, {"version": 2, "plugins": {}})
    plugins_to_remove = [
        pid for pid in installed.get("plugins", {})
        if pid.endswith(f"@{marketplace_id}")
    ]
    for pid in plugins_to_remove:
        del installed["plugins"][pid]
    svc._write_json_file(svc.INSTALLED_FILE, installed)

    # Remove from enabled plugins in settings
    settings = svc._read_json_file(svc.SETTINGS_FILE, {})
    enabled = settings.get("enabledPlugins", {})
    for pid in plugins_to_remove:
        enabled.pop(pid, None)
    settings["enabledPlugins"] = enabled
    svc._write_json_file(svc.SETTINGS_FILE, settings)

    return True


def sync_marketplace(svc, marketplace_id: str) -> bool:
    """
    Sync a marketplace with its remote repository.

    Args:
        svc: PluginService instance
        marketplace_id: The marketplace identifier

    Returns:
        True if successful
    """
    data = svc._read_json_file(svc.MARKETPLACES_FILE, {})

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
        manifest = svc._get_marketplace_manifest(install_path)
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
        # Non-git, non-GCS directory -- attempt re-clone from source
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
        # Git-based marketplace -- pull latest
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
    svc._write_json_file(svc.MARKETPLACES_FILE, data)

    return True


def sync_all_marketplaces(svc) -> Dict[str, bool]:
    """
    Sync all registered marketplaces.

    Returns:
        Dict of marketplace_id -> success status
    """
    results: Dict[str, bool] = {}
    for mp in svc.get_marketplaces():
        try:
            svc.sync_marketplace(mp.id)
            results[mp.id] = True
        except Exception as e:
            logger.error(f"Failed to sync marketplace {mp.id}: {e}")
            results[mp.id] = False
    return results
