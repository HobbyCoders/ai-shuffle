"""Compare the installable content, not just a plugin's release label.

Snapshots use the installer's symlink confinement and implicit-component rules.
They never enable plugins, change the registry, or execute plugin code.
"""

import re
import subprocess
import tempfile
from pathlib import Path

from app.core.plugin_install import (
    IMPLICIT_COMPONENTS, _check_clone_url, _copy_tree_resolving_symlinks, _git_error_text,
    discover_implicit_components,
)
from app.core.plugin_models import PluginUpdateInfo
from app.core.process_tree import child_env


def _remote_revision(source):
    kind = source.get("source")
    if kind == "github":
        url = f"https://github.com/{source.get('repo', '')}.git"
    elif kind in ("url", "git-subdir"):
        url = source.get("url", "")
        if kind == "git-subdir" and "://" not in url and not url.startswith("git@"):
            url = f"https://github.com/{url}.git"
    else:
        return None
    _check_clone_url(url)
    ref = source.get("ref")
    patterns = [f"refs/heads/{ref}", f"refs/tags/{ref}", f"refs/tags/{ref}^{{}}"] if ref else ["HEAD"]
    result = subprocess.run(
        ["git", "ls-remote", "--", url, *patterns],
        capture_output=True, text=True, timeout=30,
        env={**child_env(), "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode:
        raise RuntimeError(f"Failed to check plugin source: {_git_error_text(result.stderr)}")
    refs = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and re.fullmatch(r"[0-9a-fA-F]{40,64}", parts[0]):
            refs[parts[1]] = parts[0]
    # Match clone --branch: prefer a branch, otherwise the peeled tag's commit.
    revision = (refs.get(patterns[0]) or refs.get(patterns[-1]) or refs.get(patterns[1])) if ref else refs.get("HEAD")
    if not revision:
        raise RuntimeError("Plugin source ref is unavailable")
    return revision


def content_update(svc, plugin_id, install, marketplace):
    """Fallback for equal/missing versions and marketplace-less installations."""
    from fluid.context.plugin_formats import content_hash
    from app.core.plugin_updates import _is_update

    name, mp_id = plugin_id.rsplit("@", 1)
    location = marketplace.get("installLocation")
    installed_path = install.get("installPath")
    if not location or not installed_path or not Path(installed_path).is_dir():
        return None
    mp_path = Path(location)
    entry = svc._find_plugin_in_manifest(mp_id, name)
    implicit = install.get("implicit") and not entry
    if not entry and not implicit:
        return None  # Authored/project folders have no tracked upstream.
    source = entry.get("source", "") if entry else ""
    installed_version = install.get("version", "unknown")
    entry_version = entry.get("version") if entry else None
    if entry_version and entry_version != installed_version and not _is_update(installed_version, entry_version):
        return None  # Preserve the release downgrade guard.
    if isinstance(source, dict):
        if source.get("sha") and install.get("sourceSha") == source["sha"][:7]:
            return None  # An exact commit pin never follows the branch tip.
        if not source.get("sha"):
            revision = _remote_revision(source)
            if revision and revision == install.get("sourceRevision"):
                return None  # Do not re-clone unchanged external repositories.

    with tempfile.TemporaryDirectory(prefix="plugin-update-") as tmp:
        tmp = Path(tmp)
        available = tmp / "available"
        if implicit:
            components = discover_implicit_components(svc, mp_path)
            available.mkdir()
            for component in IMPLICIT_COMPONENTS:
                src = components.get(f"{component}_dir")
                if src:
                    _copy_tree_resolving_symlinks(src, available / component, root=mp_path, skip_git=True)
            version = svc._get_marketplace_head_version(mp_id) or installed_version
        elif isinstance(source, dict):
            cloned = tmp / "cloned"
            svc._clone_plugin_source(source, cloned)  # Respects ref, sha and subdirectory.
            _copy_tree_resolving_symlinks(cloned, available, skip_git=True)
            version = svc._get_plugin_manifest_version(available) or entry_version or installed_version
        else:
            manifest = svc._get_marketplace_manifest(mp_path) or {}
            metadata = manifest.get("metadata") or {}
            local = svc._resolve_plugin_source(source, mp_path, metadata.get("pluginRoot"))
            if not local:
                return None
            _copy_tree_resolving_symlinks(local, available, root=mp_path, skip_git=True)
            version = svc._get_plugin_manifest_version(available) or entry_version or installed_version
        if version != installed_version and not _is_update(installed_version, version):
            return None
        current = tmp / "current"
        _copy_tree_resolving_symlinks(Path(installed_path), current, skip_git=True)
        current_hash, available_hash = content_hash(current), content_hash(available)
        if not current_hash or not available_hash:
            raise RuntimeError(f"Cannot compare plugin content: {plugin_id}")
        if current_hash == available_hash:
            return None
        # The API's version label also identifies same-version source revisions.
        label = version if version != installed_version else f"{version}+{available_hash[-7:]}"
        return PluginUpdateInfo(
            plugin_id=plugin_id, name=name, marketplace=mp_id,
            installed_version=installed_version, available_version=label,
            is_local=not isinstance(source, dict),
        )
