"""Plugins: shared desired state, installed by each worker.

The network carries what a plugin *should* be on every worker (id and
enabled state) in ``network_plugins_desired``; binaries are never a payload.
The table's ``config`` column is written as ``{}`` for a new row, never
updated, and not applied to any worker. A worker
publishes only a plugin new to the table or a change to its enabled state; the
stored version is informational (the last publisher's) and is never enforced.
Each worker reconciles its user-scope installation against that table:
install what is desired and missing, enable or disable to match, uninstall
what a tombstone removed. Project-scope plugins live with the checkout and
stay local.

Agent-authored plugins (``<name>@authored``) are not part of this table:
their drafts and the owner's content-pinned approvals travel as memory
records instead (see :mod:`app.network.authored_plugins`). An enabled flag
alone cannot carry an approval, and no worker can install one from a
marketplace.
"""
from __future__ import annotations

import json
import logging
from typing import Dict, Iterable, Optional

from app.db.base import get_db
from app.network import engine

logger = logging.getLogger(__name__)


def _service():
    from app.core.plugin_service import get_plugin_service
    return get_plugin_service()


def _authored(plugin_id: str) -> bool:
    """Any spelling of the authored source, as the loader matches it on
    case-insensitive file systems (``Authored``, ``authored.``)."""
    source = plugin_id.rpartition("@")[2]
    return "@" in plugin_id and source.strip().rstrip(". ").lower() == "authored"


def _installed() -> Dict[str, dict]:
    try:
        service = _service()
        enabled = service.get_enabled_plugins()
        out = {}
        for plugin in service.get_installed_plugins():
            if getattr(plugin, "scope", "user") not in ("user", "global", None):
                continue
            if _authored(plugin.id):
                continue
            out[plugin.id] = {"version": plugin.version, "enabled": bool(enabled.get(plugin.id, plugin.enabled))}
        return out
    except Exception:  # noqa: BLE001
        logger.debug("Plugin inventory unavailable", exc_info=True)
        return {}


def refresh_desired_from_installed() -> int:
    """Publish this worker's installed plugins that are new to the desired state
    or whose enabled state changed; a differing version alone is not published."""
    installed = _installed()
    changed = 0
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, version, enabled, config FROM network_plugins_desired")
        desired = {r["id"]: dict(r) for r in cursor.fetchall()}
        for plugin_id, info in installed.items():
            current = desired.get(plugin_id)
            # ``apply_desired`` never reinstalls to match a version, so workers
            # holding different versions would overwrite each other's row on
            # every scan. Only a new plugin or an enabled change is published.
            if current and bool(current.get("enabled")) == info["enabled"]:
                continue
            cursor.execute(
                """INSERT INTO network_plugins_desired (id, version, enabled, config, updated_at)
                   VALUES (%s, %s, %s, %s, NOW())
                   ON CONFLICT (id) DO UPDATE SET version = EXCLUDED.version, enabled = EXCLUDED.enabled,
                       updated_at = NOW()""",
                (plugin_id, info["version"], info["enabled"], json.dumps({})))
            changed += 1
    return changed


def publish(*, ctx: Optional[engine.NetworkContext] = None) -> int:
    ctx = ctx or engine.context()
    if ctx is None or ctx.bootstrap_state != engine.BOOTSTRAP_READY:
        return 0
    refresh_desired_from_installed()
    return engine.scan_collection("plugins_desired", ctx=ctx)


def apply_desired(record_ids: Iterable[str]) -> int:
    """Make the local installation match desired rows that just changed."""
    ids = list(record_ids)
    if not ids:
        return 0
    try:
        service = _service()
    except Exception:  # noqa: BLE001
        return 0
    installed = _installed()
    applied = 0
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, version, enabled FROM network_plugins_desired WHERE id IN (%s)" % ",".join(["%s"] * len(ids)),
                       tuple(ids))
        rows = {r["id"]: dict(r) for r in cursor.fetchall()}
    for plugin_id in ids:
        if _authored(plugin_id):
            continue  # never installed or enabled from this table
        row = rows.get(plugin_id)
        try:
            if row is None:
                if plugin_id in installed:
                    service.uninstall_plugin(plugin_id)
                    applied += 1
                continue
            if plugin_id not in installed:
                service.install_plugin(plugin_id, "user")
                applied += 1
            if bool(row.get("enabled")):
                service.enable_plugin(plugin_id)
            else:
                service.disable_plugin(plugin_id)
        except Exception:  # noqa: BLE001 — a failed install shows in the plugin UI; sync goes on
            logger.warning("Applying desired plugin state for %s failed", plugin_id, exc_info=True)
    return applied
