"""
Plugin Install - Installation and uninstallation operations for plugins.

Provides standalone functions that operate on a PluginService instance.
"""

import logging
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from app.core.plugin_models import (
    PluginInfo,
    PluginScope,
    _force_rmtree,
)
from app.core.scrub import redact_url_credentials

logger = logging.getLogger(__name__)

# How much of git's own error output an install error carries.
_GIT_ERROR_MAX_CHARS = 500


def _git_error_text(stderr: Any) -> str:
    """git's error output for an install error: URL credentials redacted, capped.

    The clone URL comes from a marketplace entry and may carry a token
    (``https://ghp_xxx@host/r.git``); git echoes it back in its errors.
    """
    text = redact_url_credentials(str(stderr or "").strip())
    if len(text) > _GIT_ERROR_MAX_CHARS:
        text = text[:_GIT_ERROR_MAX_CHARS] + "..."
    return text


def _safe_path_component(value: Any, what: str) -> str:
    """Return ``value`` if it is safe as ONE path segment, else raise ValueError.

    Plugin names and versions come from marketplace.json / plugin.json, which a
    third-party repo controls, and are joined onto cache and project paths that
    are then deleted and written. A separator or a ``.``/``..`` segment would
    point those operations outside the plugin directory.
    """
    if not isinstance(value, str) or value in ("", ".", "..") or any(
        c in value for c in ("/", "\\", "\0")
    ):
        raise ValueError(f"Invalid plugin {what}: {str(value)[:100]!r}")
    return value


def _is_within(path: Path, root: Path) -> bool:
    """True iff ``path`` resolves to ``root`` or somewhere beneath it."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _prune_escaping_symlinks(root: Path) -> None:
    """Delete symlinks under ``root`` whose target resolves outside ``root``.

    A cloned repo may carry links such as ``skills/x/SKILL.md -> ~/.ssh/id_rsa``;
    left in place, skill discovery and the plugin APIs would read the target.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            entry = Path(dirpath) / name
            if entry.is_symlink() and not _is_within(entry, root):
                logger.warning("Removing plugin symlink that points outside the plugin: %s", entry)
                entry.unlink()


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

    plugins = manifest.get("plugins") if isinstance(manifest, dict) else None
    for entry in plugins if isinstance(plugins, list) else []:
        if isinstance(entry, dict) and entry.get("name") == plugin_name:
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
    _check_clone_url(clone_url)

    cmd = ["git", "clone", "--depth", "1"]
    if ref:
        cmd.extend(["--branch", ref])
    # "--" so a URL can never be read as a git option (e.g. --upload-pack=...).
    cmd.extend(["--", clone_url, str(dest)])

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=120
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to clone plugin: {_git_error_text(result.stderr)}")
    _checkout_pinned_sha(dest, source.get("sha"))
    _prune_escaping_symlinks(dest)


# Transports a marketplace entry may clone from. Anything else (file://, a bare
# local path, ext::, an option-looking string) is refused: the URL comes from a
# third-party marketplace.json.
_ALLOWED_CLONE_URL_PREFIXES = ("https://", "http://", "ssh://", "git@")


def _check_clone_url(url: Any) -> None:
    if not isinstance(url, str) or not url.startswith(_ALLOWED_CLONE_URL_PREFIXES):
        raise ValueError(f"Unsupported plugin source URL: {redact_url_credentials(str(url))[:100]!r}")


def _checkout_pinned_sha(repo_dir: Path, sha: Any) -> None:
    """Make ``repo_dir`` hold the commit a marketplace entry pins, or raise.

    ``git clone --depth 1`` fetches the tip of the branch, which has usually
    moved past the entry's ``sha``. Fetch and check out the pinned commit, then
    verify HEAD, so the installed files are the ones the marketplace pinned.
    """
    if not sha:
        return
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{7,64}", sha):
        raise ValueError(f"Invalid pinned plugin sha: {str(sha)[:100]!r}")
    sha = sha.lower()

    def _head() -> str:
        r = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30,
        )
        return r.stdout.strip().lower() if r.returncode == 0 else ""

    if _head().startswith(sha):
        return
    for cmd in (
        ["git", "-C", str(repo_dir), "fetch", "--depth", "1", "origin", sha],
        ["git", "-C", str(repo_dir), "checkout", "--detach", "FETCH_HEAD"],
    ):
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            break
    if not _head().startswith(sha):
        raise RuntimeError(f"Plugin source does not match its pinned sha {sha[:12]}")


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

    # Normalize URL: GitHub shorthand (org/repo) -> full URL. Every URL form
    # the clone guard accepts (https, http, ssh://, git@) is kept as given.
    if not url.startswith(_ALLOWED_CLONE_URL_PREFIXES):
        url = f"https://github.com/{url}.git"
    elif not url.endswith(".git"):
        url = f"{url}.git"
    _check_clone_url(url)

    import tempfile
    tmp_dir = Path(tempfile.mkdtemp(prefix="plugin-clone-"))
    try:
        # Shallow clone the repo
        cmd = ["git", "clone", "--depth", "1", "--filter=blob:none"]
        if ref:
            cmd.extend(["--branch", ref])
        cmd.extend(["--", url, str(tmp_dir)])

        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to clone git-subdir repo: {_git_error_text(result.stderr)}"
            )
        _checkout_pinned_sha(tmp_dir, source.get("sha"))

        # Extract the target subdirectory (never one outside the clone)
        extracted = tmp_dir / subdir_path.strip("/")
        if not extracted.is_dir() or not _is_within(extracted, tmp_dir):
            raise ValueError(
                f"Subdirectory '{subdir_path}' not found in cloned repo"
            )

        # Copy subdirectory to destination
        if dest.exists():
            shutil.rmtree(dest)
        _copy_tree_resolving_symlinks(extracted, dest, root=tmp_dir)

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
        # The entry and pluginRoot come from marketplace.json: never resolve to
        # a directory outside the marketplace repo ("./a/../../x", "../..").
        if resolved.exists() and resolved.is_dir() and _is_within(resolved, mp_path):
            return resolved
    return None


def _get_plugin_manifest_version(svc, local_dir: Path) -> Optional[str]:
    """Read a plugin's version from its own manifest, in any supported format.

    This matches Claude Code's behavior: the plugin's own manifest is the
    source of truth for versioning, not the marketplace repo hash. The
    manifest is found by fluid's shared reader (``.claude-plugin/plugin.json``,
    a root ``plugin.json`` or ``.codex-plugin/plugin.json``).

    Returns:
        The version string from the manifest, or None if not found.
    """
    from fluid.context.plugin_formats import read_manifest

    version = read_manifest(local_dir).get("version")
    return str(version) if version else None


def _detect_components_from_dir(svc, plugin_dir: Path) -> Dict[str, bool]:
    """Detect plugin components by reading a local plugin directory."""
    from app.core.plugin_service import plugin_record, record_summary

    summary = record_summary(plugin_record(plugin_dir, plugin_dir.name, ""), plugin_dir)
    return {key: summary[key] for key in (
        "has_commands", "has_agents", "has_skills", "has_hooks", "has_mcp",
    )}


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


def _copy_tree_resolving_symlinks(
    src: Path, dst: Path, root: Optional[Path] = None, _active: Optional[set] = None
) -> None:
    """Copy `src` into `dst`, materializing symlinks as real files.

    Unlike `shutil.copytree(..., symlinks=False, ignore_dangling_symlinks=True)`,
    this resolves relative symlinks correctly: `Path.exists()`/`is_dir()` follow the
    link relative to its own location (kernel semantics), whereas copytree checks the
    raw link target against the current working directory and so wrongly drops valid
    relative symlinks (e.g. remotion's `.claude/skills/x -> ../../.agents/skills/x`).
    Genuinely-broken links are skipped.

    `root` bounds what a symlink may pull in (default: `src`): a link resolving
    outside it (e.g. `-> ~/.ssh`) is skipped, and a directory link back to one
    of its own ancestors is skipped instead of recursing forever.
    """
    root = root or src
    active = set() if _active is None else _active
    real_src = src.resolve()
    active.add(real_src)
    try:
        dst.mkdir(parents=True, exist_ok=True)
        for entry in src.iterdir():
            target = dst / entry.name
            if entry.is_symlink():
                if not entry.exists():
                    continue  # genuinely dangling link -- skip
                if not _is_within(entry, root):
                    logger.warning("Skipping plugin symlink that points outside the plugin: %s", entry)
                    continue
                if entry.is_dir() and entry.resolve() in active:
                    continue  # link to an ancestor -- would recurse forever
            if entry.is_dir():  # follows symlinks to directories
                _copy_tree_resolving_symlinks(entry, target, root, active)
            else:
                shutil.copy2(entry, target)  # follows symlinks to real files
    finally:
        active.discard(real_src)


def _agent_request(
    previous: Any, requested_by: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """The review fields an install record carries.

    An agent's install (``requested_by``) waits for the owner: ``approval``
    is ``"pending"`` until the owner enables it in Settings
    (:meth:`PluginService.enable_plugin` clears it). A re-install or update
    of a plugin still waiting keeps the marker and who asked.
    """
    if requested_by:
        return {"approval": "pending", "requestedBy": dict(requested_by)}
    prev = previous[0] if isinstance(previous, list) and previous else previous
    if isinstance(prev, dict) and prev.get("approval") == "pending":
        out: Dict[str, Any] = {"approval": "pending"}
        if isinstance(prev.get("requestedBy"), dict):
            out["requestedBy"] = prev["requestedBy"]
        return out
    return {}


def _install_implicit_plugin(
    svc, plugin_id: str, marketplace_id: str, mp_path: Path, implicit: Dict[str, Any], scope: str,
    enable: bool = True, requested_by: Optional[Dict[str, Any]] = None,
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
            _copy_tree_resolving_symlinks(src, cache_path / component, root=mp_path)

    # Ensure scripts have execute permissions (mirror install_plugin).
    for script in cache_path.rglob("*.sh"):
        current_mode = script.stat().st_mode
        script.chmod(current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    installed = svc._read_json_file(svc.INSTALLED_FILE, {"version": 2, "plugins": {}}, for_update=True)
    if "plugins" not in installed:
        installed["plugins"] = {}
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    review = _agent_request(installed["plugins"].get(plugin_id), requested_by)
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
        **review,
    }]
    svc._write_json_file(svc.INSTALLED_FILE, installed)

    # Still waiting for the owner (an agent's install, or an update of one):
    # stay off, so enabling remains the owner's own action.
    if enable and not review:
        svc.enable_plugin(plugin_id)
    else:
        svc.disable_plugin(plugin_id)

    for p in svc.get_installed_plugins():
        if p.id == plugin_id:
            return p
    return svc.get_installed_plugins()[-1]


def install_plugin(
    svc, plugin_id: str, scope: str = "user", enable: bool = True,
    requested_by: Optional[Dict[str, Any]] = None,
) -> PluginInfo:
    """
    Install a plugin from a marketplace.

    Reads marketplace.json to find the plugin source and handles both
    relative-path (local) and external (github/url) sources.

    Args:
        svc: PluginService instance
        plugin_id: Plugin ID in format "name@marketplace"
        scope: Installation scope ("user" or "project")
        enable: Turn the plugin on (the owner's own install). An agent's
            install passes False: the files land, nothing loads until the
            owner enables it in Settings.
        requested_by: Set for an agent's install (session and agent that
            asked). The plugin is installed off and marked ``approval:
            "pending"`` whatever ``enable`` says.

    Returns:
        PluginInfo for the installed plugin
    """
    parts = plugin_id.rsplit("@", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid plugin ID format: {plugin_id}")

    plugin_name, marketplace_id = parts
    _safe_path_component(plugin_name, "name")
    _safe_path_component(marketplace_id, "marketplace")
    if requested_by:
        enable = False

    # Refuse (409) before copying anything if a file this install writes
    # cannot be read back; otherwise the files land and the request fails.
    svc._check_files_replaceable(svc.INSTALLED_FILE, svc.SETTINGS_FILE)

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
                    svc, plugin_id, marketplace_id, mp_path, implicit, scope, enable, requested_by
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
        _safe_path_component(version, "version")
        cache_path = svc.CACHE_DIR / marketplace_id / plugin_name / version
        cache_path.parent.mkdir(parents=True, exist_ok=True)

        if cache_path.exists():
            _force_rmtree(cache_path)
        _copy_tree_resolving_symlinks(local_dir, cache_path, root=mp_path)

    elif isinstance(source, dict):
        # External source - clone from remote
        _safe_path_component(version, "version")
        cache_path = svc.CACHE_DIR / marketplace_id / plugin_name / version
        cache_path.parent.mkdir(parents=True, exist_ok=True)

        if cache_path.exists():
            _force_rmtree(cache_path)

        try:
            svc._clone_plugin_source(source, cache_path)
        except Exception:
            # Leave no half-fetched (or unverified) files in the cache.
            if cache_path.exists():
                _force_rmtree(cache_path)
            raise

        # After cloning, read the plugin's own manifest version -- it's the
        # source of truth (matching Claude Code behaviour).
        cloned_manifest_version = svc._get_plugin_manifest_version(cache_path)
        if cloned_manifest_version and cloned_manifest_version != version:
            try:
                _safe_path_component(cloned_manifest_version, "version")
            except ValueError:
                _force_rmtree(cache_path)
                raise
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
    installed = svc._read_json_file(svc.INSTALLED_FILE, {"version": 2, "plugins": {}}, for_update=True)
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
    review = _agent_request(installed["plugins"].get(plugin_id), requested_by)
    install_record.update(review)
    installed["plugins"][plugin_id] = [install_record]
    svc._write_json_file(svc.INSTALLED_FILE, installed)

    # Enable by default; an agent-requested install (or an update of one
    # still waiting) stays off until the owner enables it.
    if enable and not review:
        svc.enable_plugin(plugin_id)
    else:
        svc.disable_plugin(plugin_id)

    # Return plugin info for the installed/updated plugin
    for p in svc.get_installed_plugins():
        if p.id == plugin_id:
            return p
    # Fallback: shouldn't happen, but return last if not found
    return svc.get_installed_plugins()[-1]


def uninstall_plugin(svc, plugin_id: str) -> bool:
    """
    Uninstall a plugin.

    Args:
        svc: PluginService instance
        plugin_id: Plugin ID to uninstall

    Returns:
        True if successful
    """
    # Refuse (409) before deleting anything, so a retry after fixing the
    # file finds the plugin still installed.
    svc._check_files_replaceable(svc.INSTALLED_FILE, svc.SETTINGS_FILE)
    installed = svc._read_json_file(svc.INSTALLED_FILE, {"version": 2, "plugins": {}}, for_update=True)
    file_version = installed.get("version", 1)

    if plugin_id not in installed.get("plugins", {}):
        raise ValueError(f"Plugin not installed: {plugin_id}")

    installation_data = installed["plugins"][plugin_id]

    # Handle both version 1 (single object) and version 2 (array) formats
    if file_version == 1 or isinstance(installation_data, dict):
        installations = [installation_data]
    else:
        installations = installation_data if installation_data else []

    # Remove from filesystem -- only ever inside the plugin cache. A record with
    # no installPath would otherwise be Path("") == the working directory.
    for install in installations:
        raw_path = install.get("installPath") if isinstance(install, dict) else None
        if not raw_path:
            continue
        install_path = Path(raw_path)
        if not _is_within(install_path, svc.CACHE_DIR) or _is_within(svc.CACHE_DIR, install_path):
            logger.warning("Not deleting plugin files outside the plugin cache: %s", install_path)
            continue
        if install_path.exists():
            shutil.rmtree(install_path, ignore_errors=True)

    # Remove from installed_plugins.json
    del installed["plugins"][plugin_id]
    svc._write_json_file(svc.INSTALLED_FILE, installed)

    # Remove from enabled plugins
    settings = svc._read_json_file(svc.SETTINGS_FILE, {}, for_update=True)
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
    _safe_path_component(plugin_name, "name")

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

    # Refuse (409) before replacing the plugin's files if the settings file
    # that enables it cannot be read back.
    svc._check_files_replaceable(svc._get_project_settings_file(scope))

    # Create target directory
    target_dir.mkdir(parents=True, exist_ok=True)

    # Resolve and copy/clone plugin to target
    local_dir = svc._resolve_plugin_source(source, mp_path, plugin_root)

    if target_path.exists():
        shutil.rmtree(target_path)

    if local_dir:
        _copy_tree_resolving_symlinks(local_dir, target_path, root=mp_path)
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

    # Validate plugin_name to prevent path traversal ("" or "." would name the
    # whole scope directory).
    if '..' in plugin_name:
        raise ValueError(f"Invalid plugin name: {plugin_name}")
    _safe_path_component(plugin_name, "name")

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

    # Refuse (409) before deleting the files if the settings file cannot be
    # read back, so a retry after fixing it still finds the plugin.
    svc._check_files_replaceable(svc._get_project_settings_file(scope))

    # Remove plugin directory
    shutil.rmtree(plugin_path)

    # Remove from enabled plugins in the matching Fluid settings file
    settings = svc._read_project_settings(scope, for_update=True)
    enabled = settings.get("enabledPlugins", {})
    enabled.pop(plugin_id, None)
    settings["enabledPlugins"] = enabled
    svc._write_project_settings(settings, scope)

    return True
