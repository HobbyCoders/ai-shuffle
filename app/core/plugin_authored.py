"""The ``authored`` plugin source: plugins an agent drafted with ``save_plugin``.

Drafts live at ``~/.fluid/plugins/cache/authored/<name>/<version>/`` (written
by fluid, see ``fluid.context.plugin_authoring``) and are listed here without
an installed-registry entry, so a new draft shows up in the plugin list at
once. Each one stays disabled until the owner enables it: enabling records an
approval pinned to the content hash of the version the owner reviewed, and
fluid's loader refuses any other content. Disabling drops the approval, so the
next enable is a fresh review.
"""

import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.plugin_models import PluginDetails, PluginInfo

logger = logging.getLogger(__name__)

AUTHORED_SOURCE = "authored"
APPROVALS_FILE = "authored_approvals.json"


class AuthoredReviewRequired(ValueError):
    """The content the owner reviewed is not the content on disk now."""


def is_authored(plugin_id: str) -> bool:
    return plugin_id.rpartition("@")[2] == AUTHORED_SOURCE and "@" in plugin_id


def _base(svc) -> Path:
    return svc.CACHE_DIR / AUTHORED_SOURCE


def _approvals_file(svc) -> Path:
    return svc.PLUGINS_DIR / APPROVALS_FILE


def _plain(name: str) -> bool:
    from fluid.context.plugins import _plain_component

    return _plain_component(name) and not name.startswith(".")


def _latest(svc, name: str) -> Optional[Path]:
    from fluid.context.plugins import _latest_version_dir

    if not _plain(name):
        return None
    folder = _base(svc) / name
    if not folder.is_dir() or folder.is_symlink():
        return None
    return _latest_version_dir(folder)


def _approvals(svc, *, for_update: bool = False) -> Dict[str, Any]:
    data = svc._read_json_file(_approvals_file(svc), {}, for_update=for_update)
    approvals = data.get("approvals")
    return approvals if isinstance(approvals, dict) else {}


def _write_approvals(svc, approvals: Dict[str, Any]) -> bool:
    return svc._write_json_file(_approvals_file(svc), {"version": 1, "approvals": approvals})


def _info(svc, name: str, enabled_plugins: Dict[str, Any], approvals: Dict[str, Any]) -> Optional[PluginInfo]:
    from fluid.context.plugin_formats import content_hash
    from app.core.plugin_service import plugin_record, record_description, record_summary

    root = _latest(svc, name)
    if root is None:
        return None
    plugin_id = f"{name}@{AUTHORED_SOURCE}"
    digest = content_hash(root)
    approval = approvals.get(plugin_id) if isinstance(approvals.get(plugin_id), dict) else None
    approved = bool(
        digest and approval
        and approval.get("hash") == digest
        and str(approval.get("version") or "") == root.name
    )
    record = plugin_record(root, name, AUTHORED_SOURCE, root.name)
    try:
        installed_at = datetime.fromtimestamp(root.stat().st_mtime, tz=timezone.utc)
    except OSError:
        installed_at = None
    return PluginInfo(
        id=plugin_id,
        name=name,
        marketplace=AUTHORED_SOURCE,
        description=record_description(record, root),
        version=root.name,
        scope="user",
        install_path=str(root),
        installed_at=installed_at,
        # Reported enabled only while the approval matches what is on disk —
        # the same rule fluid's loader applies — so the app's own consumers
        # (hooks, file agents) never treat unreviewed content as on.
        enabled=approved and bool(enabled_plugins.get(plugin_id, False)),
        trust="authored",
        approval="approved" if approved else "pending",
        content_hash=digest,
        **record_summary(record, root),
    )


def list_authored(svc, enabled_plugins: Dict[str, Any]) -> List[PluginInfo]:
    base = _base(svc)
    if not base.is_dir():
        return []
    approvals = _approvals(svc)
    out = []
    try:
        names = sorted(d.name for d in base.iterdir() if d.is_dir())
    except OSError as e:
        logger.warning(f"Cannot list authored plugins: {e}")
        return []
    for name in names:
        info = _info(svc, name, enabled_plugins, approvals)
        if info is not None:
            out.append(info)
    return out


def details(svc, plugin_id: str) -> Optional[PluginDetails]:
    from dataclasses import asdict
    from app.core.plugin_service import _component_names, _read_readme, plugin_record

    name = plugin_id.rpartition("@")[0]
    settings = svc._read_json_file(svc.SETTINGS_FILE, {})
    info = _info(svc, name, settings.get("enabledPlugins", {}), _approvals(svc))
    if info is None:
        return None
    root = Path(info.install_path)
    commands, agents, skills = _component_names(plugin_record(root, name, AUTHORED_SOURCE, root.name))
    return PluginDetails(
        **asdict(info), commands=commands, agents=agents, skills=skills, readme=_read_readme(root),
    )


def enable(svc, plugin_id: str, content_hash: Optional[str]) -> bool:
    """Approve the reviewed content and enable the plugin.

    ``content_hash`` is the hash the owner was shown; a mismatch (a newer
    version or an edit since) is refused, so approval always covers exactly
    what was reviewed.
    """
    from fluid.context.plugin_formats import content_hash as hash_tree

    name = plugin_id.rpartition("@")[0]
    root = _latest(svc, name)
    if root is None:
        raise ValueError(f"Plugin not found: {plugin_id}")
    digest = hash_tree(root)
    if not digest or not content_hash or content_hash != digest:
        raise AuthoredReviewRequired(
            "This plugin changed since it was reviewed. Review the current version before enabling it."
        )
    svc._check_files_replaceable(_approvals_file(svc), svc.SETTINGS_FILE)
    approvals = _approvals(svc, for_update=True)
    approvals[plugin_id] = {
        "version": root.name,
        "hash": digest,
        "approvedAt": datetime.now(timezone.utc).isoformat(),
    }
    if not _write_approvals(svc, approvals):
        return False
    return _set_enabled(svc, plugin_id, True)


def disable(svc, plugin_id: str) -> bool:
    svc._check_files_replaceable(_approvals_file(svc), svc.SETTINGS_FILE)
    approvals = _approvals(svc, for_update=True)
    if approvals.pop(plugin_id, None) is not None and not _write_approvals(svc, approvals):
        return False
    return _set_enabled(svc, plugin_id, False)


def uninstall(svc, plugin_id: str) -> bool:
    name = plugin_id.rpartition("@")[0]
    folder = _base(svc) / name
    if not _plain(name) or not folder.is_dir() or folder.is_symlink():
        raise ValueError(f"Plugin not installed: {plugin_id}")
    svc._check_files_replaceable(_approvals_file(svc), svc.SETTINGS_FILE)
    approvals = _approvals(svc, for_update=True)
    if approvals.pop(plugin_id, None) is not None:
        _write_approvals(svc, approvals)
    settings = svc._read_json_file(svc.SETTINGS_FILE, {}, for_update=True)
    enabled = settings.get("enabledPlugins")
    if isinstance(enabled, dict) and plugin_id in enabled:
        del enabled[plugin_id]
        svc._write_json_file(svc.SETTINGS_FILE, settings)
    shutil.rmtree(folder, ignore_errors=True)
    return not folder.exists()


def _set_enabled(svc, plugin_id: str, value: bool) -> bool:
    settings = svc._read_json_file(svc.SETTINGS_FILE, {}, for_update=True)
    settings.setdefault("enabledPlugins", {})[plugin_id] = value
    return svc._write_json_file(svc.SETTINGS_FILE, settings)
