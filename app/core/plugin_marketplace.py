"""
Plugin Marketplace - Marketplace management operations for the plugin system.

Provides standalone functions that operate on a PluginService instance.
"""

import logging
import re
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from app.core.plugin_models import MarketplaceInfo
from app.core.process_tree import child_env
from app.core.scrub import redact_url_credentials

logger = logging.getLogger(__name__)

# Public catalog already used by Claude Code; registering it never enables plugins.
DEFAULT_SOURCE_ID = "claude-plugins-official"
DEFAULT_SOURCE_URL = "https://github.com/anthropics/claude-plugins-official.git"
_DEFAULTS_LOCK = threading.Lock()
_FETCH_LOCK = threading.Lock()


def ensure_default_source(svc) -> None:
    """Apply once to fresh and upgraded installs; removal must survive restarts.

    Register before marking applied, without network I/O. A failed first fetch
    leaves a removable source that discovery or the Sync action can retry.
    """
    marker = svc.PLUGINS_DIR / "source_defaults.json"
    with _DEFAULTS_LOCK:
        defaults = svc._read_json_file(marker, {}, for_update=True)
        if defaults.get("anthropic_source_applied"):
            return
        data = svc._read_json_file(svc.MARKETPLACES_FILE, {}, for_update=True)
        svc._check_files_replaceable(marker, svc.MARKETPLACES_FILE)

        def same_source(entry):
            url = entry.get("source", {}).get("url", "").rstrip("/")
            return url.removesuffix(".git") in (
                DEFAULT_SOURCE_URL.removesuffix(".git"),
                "anthropics/claude-plugins-official",
                "git@github.com:anthropics/claude-plugins-official",
            )

        if DEFAULT_SOURCE_ID not in data and not any(same_source(v) for v in data.values()):
            data[DEFAULT_SOURCE_ID] = {
                "source": {"source": "git", "url": DEFAULT_SOURCE_URL},
                "displayName": "Anthropic official plugins",
                "installLocation": str(svc.MARKETPLACES_DIR / DEFAULT_SOURCE_ID),
                "fetchOnDiscovery": True,
            }
            svc._write_json_file(svc.MARKETPLACES_FILE, data)
        defaults["anthropic_source_applied"] = True
        svc._write_json_file(marker, defaults)


def source_error(svc, mp_id: str, data: dict) -> Optional[str]:
    """A missing/unreadable source is not an empty search result."""
    reason = data.get("syncError")
    location = data.get("installLocation")
    if not reason:
        if not location or not Path(location).is_dir():
            reason = "its catalog has not been downloaded or is missing"
        elif not svc._get_marketplace_manifest(Path(location)):
            if not svc._discover_implicit_components(Path(location))["any"]:
                reason = "no readable plugin catalog or skills were found"
    if reason:
        return f"Plugin source {mp_id} is unavailable: {reason}. Retry Sync in Settings > Plugins > Sources and updates."
    return None


def fetch_pending_sources(svc) -> None:
    # Only the shipped, never-fetched source needs automatic I/O on discovery.
    with _FETCH_LOCK:
        data = svc._read_json_file(svc.MARKETPLACES_FILE, {})
        for mp_id, entry in data.items():
            if entry.get("fetchOnDiscovery") and not entry.get("lastUpdated"):
                try:
                    svc.sync_marketplace(mp_id)
                except (RuntimeError, OSError):
                    logger.warning("Could not fetch plugin source %s", mp_id)


def _git_error(e: subprocess.CalledProcessError) -> str:
    """git's stderr for a client-facing error, with URL credentials removed."""
    stderr = e.stderr or b""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    return redact_url_credentials(stderr)


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
            last_updated=last_updated,
            display_name=mp_data.get("displayName"),
            error=source_error(svc, mp_id, mp_data),
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
        raise ValueError(f"Invalid git URL: {redact_url_credentials(url)[:100]}")

    # Derive name from URL if not provided
    if not name:
        name = url.rstrip("/").split("/")[-1]
        if name.endswith(".git"):
            name = name[:-4]

    # Validate derived name (prevent path traversal)
    if not re.match(r'^[\w\-]+$', name):
        raise ValueError(f"Invalid marketplace name: {name[:50]}")
    # Agent-drafted plugins live under this source name and load only after
    # review, so a marketplace may not share it (in any letter case).
    if name.casefold() == "authored":
        raise ValueError("The marketplace name 'authored' is reserved; pass a different name")

    install_location = str(svc.MARKETPLACES_DIR / name)

    # Refuse (409) before cloning if the registry cannot be read back.
    svc._check_files_replaceable(svc.MARKETPLACES_FILE)

    # Clone the repository
    if not Path(install_location).exists():
        try:
            # "--" so a URL can never be read as a git option (e.g. --upload-pack=...).
            subprocess.run(
                ["git", "clone", "--depth", "1", "--", url, install_location],
                check=True,
                capture_output=True,
                timeout=120, env=child_env(),
            )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to clone marketplace: {_git_error(e)}")
        except subprocess.TimeoutExpired:
            raise RuntimeError("Marketplace clone timed out")

    # Update known_marketplaces.json
    data = svc._read_json_file(svc.MARKETPLACES_FILE, {}, for_update=True)
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
    # Refuse (409) before removing anything if any file this writes cannot be
    # read back; the entry, clone and cache stay for a retry.
    svc._check_files_replaceable(svc.MARKETPLACES_FILE, svc.INSTALLED_FILE, svc.SETTINGS_FILE)
    data = svc._read_json_file(svc.MARKETPLACES_FILE, {}, for_update=True)

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
    installed = svc._read_json_file(svc.INSTALLED_FILE, {"version": 2, "plugins": {}}, for_update=True)
    plugins_to_remove = [
        pid for pid in installed.get("plugins", {})
        if pid.endswith(f"@{marketplace_id}")
    ]
    for pid in plugins_to_remove:
        del installed["plugins"][pid]
    svc._write_json_file(svc.INSTALLED_FILE, installed)

    # Remove those plugins' cached files too (only a direct child of the cache).
    cache_dir = svc.CACHE_DIR / marketplace_id
    if (
        marketplace_id not in ("", ".", "..")
        and "/" not in marketplace_id and "\\" not in marketplace_id
        and cache_dir.is_dir() and not cache_dir.is_symlink()
    ):
        shutil.rmtree(cache_dir, ignore_errors=True)

    # Remove from enabled plugins in settings
    settings = svc._read_json_file(svc.SETTINGS_FILE, {}, for_update=True)
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
    # Refuse (409) before re-cloning or pulling if the registry cannot be read back.
    svc._check_files_replaceable(svc.MARKETPLACES_FILE)
    data = svc._read_json_file(svc.MARKETPLACES_FILE, {}, for_update=True)

    if marketplace_id not in data:
        raise ValueError(f"Marketplace not found: {marketplace_id}")

    install_location = data[marketplace_id].get("installLocation")

    if not install_location:
        raise RuntimeError(f"Marketplace directory not found: {install_location}")

    install_path = Path(install_location)
    git_dir = install_path / ".git"
    gcs_sha_file = install_path / ".gcs-sha"

    def _clone_from_source() -> None:
        source_data = data[marketplace_id].get("source", {})
        source_url = source_data.get("url") or source_data.get("repo", "")
        # Handle GitHub shorthand (org/repo) vs full URL
        if source_url and not source_url.startswith(("http://", "https://", "git@")):
            source_url = f"https://github.com/{source_url}"
        if not source_url:
            if not install_path.exists():
                raise RuntimeError(f"Marketplace directory not found: {install_location}")
            raise RuntimeError(f"Cannot re-clone marketplace: no source URL found for {marketplace_id}")
        shutil.rmtree(install_path, ignore_errors=True)
        try:
            subprocess.run(
                ["git", "clone", "--depth", "1", "--", source_url, install_location],
                check=True,
                capture_output=True,
                timeout=30 if data[marketplace_id].get("fetchOnDiscovery") else 120,
                env={**child_env(), "GIT_TERMINAL_PROMPT": "0"},
            )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to re-clone marketplace: {_git_error(e)}")
        except subprocess.TimeoutExpired:
            raise RuntimeError("Marketplace re-clone timed out")

    if not install_path.exists():
        # The registry entry outlived its folder -- clone it back from source.
        logger.warning(f"Marketplace directory is missing: {install_location}. Cloning...")
        _clone_from_source()
    # GCS-distributed marketplace (installed by Claude Code CLI, not git).
    # These have a .gcs-sha file and no .git directory. The CLI manages
    # updates, so we just verify the content is intact and skip re-clone.
    elif gcs_sha_file.exists():
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
        _clone_from_source()
    else:
        # Git-based marketplace -- pull latest
        try:
            subprocess.run(
                ["git", "-C", install_location, "pull", "--ff-only"],
                check=True,
                capture_output=True,
                timeout=120, env=child_env(),
            )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to sync marketplace: {_git_error(e)}")
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


def _skip_reason(source) -> str:
    """Why a catalog entry's ``source`` cannot be installed here, in plain words."""
    kind = source.get("source") if isinstance(source, dict) else None
    if kind == "npm" or (isinstance(source, dict) and "package" in source):
        return "packages from npm can't be installed here"
    if kind == "pip":
        return "packages from pip can't be installed here"
    return "its source type isn't supported here"


def skipped_entries(mp_path: Path) -> List[Dict[str, str]]:
    """Catalog entries in the marketplace at ``mp_path`` that are not offered,
    each ``{"name", "reason"}``, so Settings can say why instead of dropping
    them. Covers sources the reader cannot install (Agent Plugins npm entries)
    and names that are unsafe as a folder name. Never raises."""
    import json as _json

    from fluid.context.plugin_formats import read_marketplace

    from app.core.plugin_install import _safe_path_component

    try:
        catalog = read_marketplace(mp_path)
    except Exception:  # noqa: BLE001 — an unreadable catalog lists nothing
        return []
    if catalog is None:
        return []
    out: List[Dict[str, str]] = []
    if catalog.skipped:
        raw_sources: Dict[str, object] = {}
        try:
            raw = _json.loads(catalog.path.read_text(encoding="utf-8"))
            for entry in raw.get("plugins") or []:
                if isinstance(entry, dict):
                    raw_sources.setdefault(str(entry.get("name") or "?")[:100], entry.get("source"))
        except (OSError, ValueError, AttributeError):
            pass
        for name in catalog.skipped:
            out.append({"name": name, "reason": _skip_reason(raw_sources.get(name))})
    entries = catalog.data.get("plugins")
    for entry in entries if isinstance(entries, list) else []:
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not name:
            continue
        try:
            _safe_path_component(name, "name")
        except ValueError:
            out.append({"name": name[:100], "reason": "its name can't be used as a folder name"})
    return out
