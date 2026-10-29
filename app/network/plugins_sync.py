"""Plugins: shared desired state, installed by each worker.

The network carries what a plugin *should* be on every worker (id and
enabled state) in ``network_plugins_desired``; binaries are never a payload.
The table's ``config`` column is written as ``{}`` for a new row, never
updated, and not applied to any worker. A worker
publishes only a plugin new to the table or a change to its enabled state;
explicit removals produce tombstones that inventory scans cannot resurrect. An
explicit install can replace a tombstone. The
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
from app.db import network as store
from app.network import engine

logger = logging.getLogger(__name__)
_REMOVAL_PREFIX = "network_plugin_uninstall:"


def _pending_removals(cursor):
    # Local intent survives held outboxes and bootstrap. The network_* setting
    # policy excludes these markers; only signed collection tombstones travel.
    cursor.execute("SELECT key FROM system_settings WHERE key LIKE %s", (_REMOVAL_PREFIX + "%",))
    return {row["key"][len(_REMOVAL_PREFIX):] for row in cursor.fetchall()}


def request_uninstall(plugin_id: str) -> None:
    """Persist an explicit removal, even before this plugin's first publish."""
    if _authored(plugin_id):
        return
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM network_plugins_desired WHERE id = %s", (plugin_id,))
        cursor.execute("""INSERT INTO system_settings (key, value, updated_at) VALUES (%s, %s, NOW())
                          ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()""",
                       (_REMOVAL_PREFIX + plugin_id, "1"))


def install_plugin(service, plugin_id: str, scope: str, enable: bool):
    """An explicit install may replace a tombstone; an inventory scan may not."""
    plugin = service.install_plugin(plugin_id, scope, enable)
    if scope not in ("user", "global", None) or _authored(plugin_id):
        return plugin
    info = _installed(service).get(plugin_id)
    if info is None:
        return plugin  # pending approval or a registry write that did not stick
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM system_settings WHERE key = %s", (_REMOVAL_PREFIX + plugin_id,))
        cursor.execute("""INSERT INTO network_plugins_desired (id, version, enabled, config, updated_at)
                          VALUES (%s, %s, %s, %s, NOW()) ON CONFLICT (id) DO UPDATE SET
                          version = EXCLUDED.version, enabled = EXCLUDED.enabled, updated_at = NOW()""",
                       (plugin_id, info["version"], info["enabled"], json.dumps({})))
    return plugin


def _service():
    from app.core.plugin_service import get_plugin_service
    return get_plugin_service()


def _authored(plugin_id: str) -> bool:
    """Any spelling of the authored source, as the loader matches it on
    case-insensitive file systems (``Authored``, ``authored.``)."""
    source = plugin_id.rpartition("@")[2]
    return "@" in plugin_id and source.strip().rstrip(". ").lower() == "authored"


def _installed(service=None) -> Dict[str, dict]:
    try:
        service = service or _service()
        enabled = service.get_enabled_plugins()
        out = {}
        for plugin in service.get_installed_plugins():
            if getattr(plugin, "scope", "user") not in ("user", "global", None):
                continue
            if _authored(plugin.id):
                continue
            if getattr(plugin, "approval", None) == "pending":
                # An agent asked for it here; it waits for the owner's review
                # on this worker and is not shared until they enable it.
                continue
            out[plugin.id] ={"version": plugin.version, "enabled": bool(enabled.get(plugin.id, plugin.enabled))}
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
        removed = _pending_removals(cursor) | {
            pid for pid, row in store.records_for_collection(cursor, "plugins_desired").items() if row["deleted"]}
        for plugin_id, info in installed.items():
            if plugin_id in removed:
                continue  # it may still be installed while reconciliation is queued or failing
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
    removed = 0
    with get_db() as conn:
        cursor = conn.cursor()
        for plugin_id in _pending_removals(cursor):
            cursor.execute("DELETE FROM network_plugins_desired WHERE id = %s", (plugin_id,))
            known = store.get_record(cursor, "plugins_desired", plugin_id)
            if known and known["deleted"]:
                published = True
            else:
                published = engine.record(cursor, "plugins_desired", plugin_id, None, ctx=ctx, force=True) is not None
                removed += int(published)
            if published:
                cursor.execute("DELETE FROM system_settings WHERE key = %s", (_REMOVAL_PREFIX + plugin_id,))
    return removed + engine.scan_collection("plugins_desired", ctx=ctx)


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
        pending = _pending_removals(cursor)
    for plugin_id in ids:
        if _authored(plugin_id):
            continue  # never installed or enabled from this table
        row = None if plugin_id in pending else rows.get(plugin_id)
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


from app.core.resource_sync import install_database_mutations as _install_resource_sync

_install_resource_sync(globals(), 'plugins', 'request_uninstall install_plugin', scope='shared')
