"""
Plugin Install - Installation and uninstallation operations for plugins.

Provides standalone functions that operate on a PluginService instance.
"""

import json
import logging
import shutil
import stat
import subprocess
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any

from app.core.plugin_models import (
    PluginInfo,
    PluginScope,
    _force_rmtree,
)

logger = logging.getLogger(__name__)


def _find_plugin_in_manifest(svc, marketplace_id: str, plugin_name: str) -> Optional[Dict[str, Any]]:
    """Find a plugin entry in a marketplace's marketplace.json."""
    marketplaces = svc._read_json_file(svc.MARKETPLACES_FILE, {})
    mp_data = marketplaces.get(marketplace_id)
    if not mp_data:
        return None

    mp_path = Path(mp_data.get("installLocation", ""))
    manifest = svc._get_marketplace_manifest(mp_path)
    if not manifest:
        return None

    for entry in manifest.get("plugins", []):
        if entry.get("name") == plugin_name:
            return entry
    return None


def _clone_plugin_source(svc, source: Dict[str, Any], dest: Path) -> None:
    """Clone an external plugin source to a destination directory.

    Supported source types:
    - "github": Clone from GitHub using org/repo shorthand
    - "url": Clone from a full git URL
    - "git-subdir": Clone a repo and extract a specific subdirectory
    """
    source_type = source.get("source", "")
    ref = source.get("ref")

    if source_type == "git-subdir":
        _clone_git_subdir_source(svc, source, dest)
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


def _clone_git_subdir_source(svc, source: Dict[str, Any], dest: Path) -> None:
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

    # Normalize URL: GitHub shorthand (org/repo) -> full URL
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


def _resolve_plugin_source(svc, source, mp_path: Path, plugin_root: Optional[str] = None) -> Optional[Path]:
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


def _get_plugin_manifest_version(svc, local_dir: Path) -> Optional[str]:
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


def _detect_components_from_dir(svc, plugin_dir: Path) -> Dict[str, bool]:
    """Detect plugin components by scanning a local plugin directory."""
    return {
        "has_commands": (plugin_dir / "commands").exists(),
        "has_agents": (plugin_dir / "agents").exists(),
        "has_skills": (plugin_dir / "skills").exists(),
        "has_hooks": (plugin_dir / "hooks").exists() or (plugin_dir / "hooks.json").exists(),
        "has_mcp": (plugin_dir / ".mcp.json").exists(),
    }


def _detect_components_from_entry(svc, entry: Dict[str, Any]) -> Dict[str, bool]:
    """Infer plugin components from marketplace.json entry keys."""
    return {
        "has_commands": "commands" in entry,
        "has_agents": "agents" in entry,
        "has_skills": "skills" in entry,
        "has_hooks": "hooks" in entry,
        "has_mcp": "mcpServers" in entry,
    }


# Passive, user-invoked component types an *implicit* plugin (a git repo added as
# a source but carrying no .claude-plugin/marketplace.json) is allowed to expose.
# Deliberately excludes hooks and MCP servers: those execute automatically, so
# auto-adopting them from a repo merely added as a source would be a footgun.
IMPLICIT_COMPONENTS = ("skills", "commands", "agents")


def _resolve_implicit_component_dir(root: Path, component: str) -> Optional[Path]:
    """Locate a component's source directory in a marketplace-less repo.

    Searches, in priority order:
        <root>/<component>            (plugin-style at the repo root)
        <root>/.claude/<component>   (project-style, e.g. .claude/skills)
        <root>/.agents/<component>   (multi-tool convention, e.g. remotion)

    Returns the first existing directory, or None. is_dir() follows symlinks, so
    a symlinked component dir resolves correctly.
    """
    for candidate in (root / component, root / ".claude" / component, root / ".agents" / component):
        if candidate.is_dir():
            return candidate
    return None


def _skills_dir_has_skill(skills_dir: Path) -> bool:
    """True iff skills_dir contains at least one <name>/SKILL.md."""
    try:
        for child in skills_dir.iterdir():
            if child.is_dir() and (child / "SKILL.md").exists():
                return True
    except OSError:
        pass
    return False


def _dir_has_markdown(md_dir: Path) -> bool:
    """True iff md_dir contains at least one *.md file (recursively)."""
    try:
        return next(md_dir.glob("**/*.md"), None) is not None
    except OSError:
        return False


def discover_implicit_components(svc, root: Path) -> Dict[str, Any]:
    """Detect skills/commands/agents in a marketplace-less repo (implicit plugin).

    A component only counts as present if its directory actually holds usable
    content (skills need <name>/SKILL.md; commands/agents need at least one *.md),
    so an empty or irrelevant directory does not conjure a phantom plugin.

    Returns per-component resolved source Paths plus has_* booleans and an "any"
    convenience flag.
    """
    skills_dir = _resolve_implicit_component_dir(root, "skills")
    commands_dir = _resolve_implicit_component_dir(root, "commands")
    agents_dir = _resolve_implicit_component_dir(root, "agents")

    has_skills = bool(skills_dir and _skills_dir_has_skill(skills_dir))
    has_commands = bool(commands_dir and _dir_has_markdown(commands_dir))
    has_agents = bool(agents_dir and _dir_has_markdown(agents_dir))

    return {
        "skills_dir": skills_dir if has_skills else None,
        "commands_dir": commands_dir if has_commands else None,
        "agents_dir": agents_dir if has_agents else None,
        "has_skills": has_skills,
        "has_commands": has_commands,
        "has_agents": has_agents,
        "any": has_skills or has_commands or has_agents,
    }


def _copy_tree_resolving_symlinks(src: Path, dst: Path) -> None:
    """Copy `src` into `dst`, materializing symlinks as real files.

    Unlike `shutil.copytree(..., symlinks=False, ignore_dangling_symlinks=True)`,
    this resolves relative symlinks correctly: `Path.exists()`/`is_dir()` follow the
    link relative to its own location (kernel semantics), whereas copytree checks the
    raw link target against the current working directory and so wrongly drops valid
    relative symlinks (e.g. remotion's `.claude/skills/x -> ../../.agents/skills/x`).
    Genuinely-broken links are skipped.
    """
    dst.mkdir(parents=True, exist_ok=True)
    for entry in src.iterdir():
        target = dst / entry.name
        if entry.is_symlink() and not entry.exists():
            continue  # genuinely dangling link -- skip
        if entry.is_dir():  # follows symlinks to directories
            _copy_tree_resolving_symlinks(entry, target)
        else:
            shutil.copy2(entry, target)  # follows symlinks to real files


def _install_implicit_plugin(
    svc, plugin_id: str, marketplace_id: str, mp_path: Path, implicit: Dict[str, Any], scope: str
) -> PluginInfo:
    """Install a marketplace-less repo as a single implicit plugin.

    Materializes the discovered skills/commands/agents into the plugin cache under
    a normalized <cache>/<component>/ layout so the ordinary enable + skill-discovery
    path (which reads <install>/skills/<name>/SKILL.md) works unchanged. Symlinks are
    resolved to real files so repos that symlink their skills (e.g. .claude/skills ->
    .agents/skills) install correctly.
    """
    plugin_name = plugin_id.rsplit("@", 1)[0]
    version = svc._get_marketplace_head_version(marketplace_id) or "0.0.0"

    cache_path = svc.CACHE_DIR / marketplace_id / plugin_name / version
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    if cache_path.exists():
        _force_rmtree(cache_path)
    cache_path.mkdir(parents=True)

    for component in IMPLICIT_COMPONENTS:
        src = implicit.get(f"{component}_dir")
        if src:
            _copy_tree_resolving_symlinks(src, cache_path / component)

    # Ensure scripts have execute permissions (mirror install_plugin).
    for script in cache_path.rglob("*.sh"):
        current_mode = script.stat().st_mode
        script.chmod(current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    installed = svc._read_json_file(svc.INSTALLED_FILE, {"version": 2, "plugins": {}})
    if "plugins" not in installed:
        installed["plugins"] = {}
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    installed["plugins"][plugin_id] = [{
        "scope": scope,
        "installPath": str(cache_path),
        "version": version,
        "installedAt": now,
        "lastUpdated": now,
        "isLocal": True,
        # Marks a repo installed as an implicit plugin (no marketplace.json entry).
        # Update checks skip these; see plugin_updates.check_for_updates.
        "implicit": True,
    }]
    svc._write_json_file(svc.INSTALLED_FILE, installed)

    svc.enable_plugin(plugin_id)

    for p in svc.get_installed_plugins():
        if p.id == plugin_id:
            return p
    return svc.get_installed_plugins()[-1]


def install_plugin(svc, plugin_id: str, scope: str = "user") -> PluginInfo:
    """
    Install a plugin from a marketplace.

    Reads marketplace.json to find the plugin source and handles both
    relative-path (local) and external (github/url) sources.

    Args:
        svc: PluginService instance
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
    marketplaces = svc._read_json_file(svc.MARKETPLACES_FILE, {})
    if marketplace_id not in marketplaces:
        raise ValueError(f"Marketplace not found: {marketplace_id}")

    mp_location = marketplaces[marketplace_id].get("installLocation")
    mp_path = Path(mp_location)

    # Look up plugin in marketplace.json
    entry = svc._find_plugin_in_manifest(marketplace_id, plugin_name)
    if not entry:
        # No manifest entry. If the marketplace has no manifest at all and the repo
        # itself carries skills/commands/agents, install it as an implicit
        # single-plugin (canonical id "<mp>@<mp>").
        if plugin_name == marketplace_id and svc._get_marketplace_manifest(mp_path) is None:
            implicit = discover_implicit_components(svc, mp_path)
            if implicit["any"]:
                return _install_implicit_plugin(
                    svc, plugin_id, marketplace_id, mp_path, implicit, scope
                )
        raise ValueError(f"Plugin not found in marketplace: {plugin_name}")

    source = entry.get("source", "")
    # Version priority: plugin manifest > entry.version > source.sha (short) > "1.0.0"
    version = entry.get("version", "1.0.0")
    if version == "1.0.0" and isinstance(source, dict) and source.get("sha"):
        version = source["sha"][:7]
    manifest = svc._get_marketplace_manifest(mp_path)
    plugin_root = manifest.get("metadata", {}).get("pluginRoot") if manifest and isinstance(manifest.get("metadata"), dict) else None

    # Resolve plugin source
    local_dir = svc._resolve_plugin_source(source, mp_path, plugin_root)

    if local_dir:
        # Local source - read version from the plugin's own manifest
        # (matching Claude Code's behavior: manifest.version is source of truth)
        manifest_version = svc._get_plugin_manifest_version(local_dir)
        if manifest_version:
            version = manifest_version
        elif not entry.get("version"):
            # No manifest version and no entry version -- fall back to
            # marketplace HEAD hash for git-based marketplaces
            resolved_version = svc._get_marketplace_head_version(marketplace_id)
            if resolved_version:
                version = resolved_version

        # Create cache directory and copy plugin
        cache_path = svc.CACHE_DIR / marketplace_id / plugin_name / version
        cache_path.parent.mkdir(parents=True, exist_ok=True)

        if cache_path.exists():
            _force_rmtree(cache_path)
        shutil.copytree(local_dir, cache_path)

    elif isinstance(source, dict):
        # External source - clone from remote
        cache_path = svc.CACHE_DIR / marketplace_id / plugin_name / version
        cache_path.parent.mkdir(parents=True, exist_ok=True)

        if cache_path.exists():
            _force_rmtree(cache_path)

        svc._clone_plugin_source(source, cache_path)

        # After cloning, read the plugin's own manifest version -- it's the
        # source of truth (matching Claude Code behaviour).
        cloned_manifest_version = svc._get_plugin_manifest_version(cache_path)
        if cloned_manifest_version and cloned_manifest_version != version:
            # Re-cache under the correct manifest version
            new_cache_path = svc.CACHE_DIR / marketplace_id / plugin_name / cloned_manifest_version
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
    installed = svc._read_json_file(svc.INSTALLED_FILE, {"version": 2, "plugins": {}})
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
    svc._write_json_file(svc.INSTALLED_FILE, installed)

    # Enable by default
    svc.enable_plugin(plugin_id)

    # Return plugin info for the installed/updated plugin
    for p in svc.get_installed_plugins():
        if p.id == plugin_id:
            return p
    # Fallback: shouldn't happen, but return last if not found
    return svc.get_installed_plugins()[-1]


def install_plugins_batch(svc, plugin_ids: List[str], scope: str = "user") -> List[PluginInfo]:
    """
    Install multiple plugins in batch.

    Args:
        svc: PluginService instance
        plugin_ids: List of plugin IDs to install
        scope: Installation scope

    Returns:
        List of installed plugin info
    """
    results = []
    for plugin_id in plugin_ids:
        try:
            result = svc.install_plugin(plugin_id, scope)
            results.append(result)
        except Exception as e:
            logger.error(f"Failed to install plugin {plugin_id}: {e}")
    return results


def uninstall_plugin(svc, plugin_id: str) -> bool:
    """
    Uninstall a plugin.

    Args:
        svc: PluginService instance
        plugin_id: Plugin ID to uninstall

    Returns:
        True if successful
    """
    installed = svc._read_json_file(svc.INSTALLED_FILE, {"version": 2, "plugins": {}})
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
    svc._write_json_file(svc.INSTALLED_FILE, installed)

    # Remove from enabled plugins
    settings = svc._read_json_file(svc.SETTINGS_FILE, {})
    enabled = settings.get("enabledPlugins", {})
    enabled.pop(plugin_id, None)
    settings["enabledPlugins"] = enabled
    svc._write_json_file(svc.SETTINGS_FILE, settings)

    return True


def install_plugin_to_project(svc, plugin_id: str, scope: PluginScope = PluginScope.LOCAL) -> PluginInfo:
    """
    Install a plugin from a marketplace to the project directory.

    Args:
        svc: PluginService instance
        plugin_id: Plugin ID in format "name@marketplace"
        scope: LOCAL (.fluid/plugins/) or PROJECT (plugins/)

    Returns:
        PluginInfo for the installed plugin
    """
    if not svc.project_dir:
        raise ValueError("No project directory configured")

    if scope not in (PluginScope.LOCAL, PluginScope.PROJECT):
        raise ValueError(f"Invalid scope for project plugin: {scope}")

    parts = plugin_id.rsplit("@", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid plugin ID format: {plugin_id}")

    plugin_name, marketplace_id = parts

    # Find the plugin in marketplace
    marketplaces = svc._read_json_file(svc.MARKETPLACES_FILE, {})
    if marketplace_id not in marketplaces:
        raise ValueError(f"Marketplace not found: {marketplace_id}")

    mp_location = marketplaces[marketplace_id].get("installLocation")
    mp_path = Path(mp_location)

    # Look up plugin in marketplace.json
    entry = svc._find_plugin_in_manifest(marketplace_id, plugin_name)
    if not entry:
        raise ValueError(f"Plugin not found in marketplace: {plugin_name}")

    source = entry.get("source", "")
    manifest = svc._get_marketplace_manifest(mp_path)
    plugin_root = manifest.get("metadata", {}).get("pluginRoot") if manifest and isinstance(manifest.get("metadata"), dict) else None

    # Determine target directory
    target_dir = svc.get_plugins_dir_for_scope(scope)
    if not target_dir:
        raise ValueError(f"Cannot get directory for scope: {scope}")

    target_path = target_dir / plugin_name

    # Create target directory
    target_dir.mkdir(parents=True, exist_ok=True)

    # Resolve and copy/clone plugin to target
    local_dir = svc._resolve_plugin_source(source, mp_path, plugin_root)

    if target_path.exists():
        shutil.rmtree(target_path)

    if local_dir:
        shutil.copytree(local_dir, target_path)
    elif isinstance(source, dict):
        svc._clone_plugin_source(source, target_path)
    else:
        raise ValueError(f"Plugin source not found or unsupported: {plugin_name}")

    # Ensure scripts have execute permissions
    for script in target_path.rglob("*.sh"):
        current_mode = script.stat().st_mode
        script.chmod(current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # Enable by default in project settings
    project_plugin_id = f"{plugin_name}@project-{scope.value}"
    svc.enable_project_plugin(project_plugin_id)

    # Return plugin info
    plugins = svc.get_project_installed_plugins()
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


def uninstall_project_plugin(svc, plugin_id: str) -> bool:
    """
    Uninstall a plugin from the project directory.

    Args:
        svc: PluginService instance
        plugin_id: Plugin ID in format "name@project-local" or "name@project-project"

    Returns:
        True if successful
    """
    if not svc.project_dir:
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
    plugins_dir = svc.get_plugins_dir_for_scope(scope)
    if not plugins_dir:
        raise ValueError(f"Cannot get directory for scope: {scope}")

    plugin_path = plugins_dir / plugin_name

    if not plugin_path.exists():
        raise ValueError(f"Plugin not found: {plugin_name}")

    # Remove plugin directory
    shutil.rmtree(plugin_path)

    # Remove from enabled plugins in the matching Fluid settings file
    settings = svc._read_project_settings(scope)
    enabled = settings.get("enabledPlugins", {})
    enabled.pop(plugin_id, None)
    settings["enabledPlugins"] = enabled
    svc._write_project_settings(settings, scope)

    return True
