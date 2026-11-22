"""Plugins: shared desired state, installed by each worker.

The network carries what a plugin *should* be on every worker (id and
enabled state) in ``network_plugins_desired``; binaries are never a payload.
The table's ``config`` column carries one thing: ``marketplace``, the git
source (``{"id", "url"}``) of the plugin's marketplace, so a worker that has
never added that marketplace can add it before installing. Only a plain
http(s) or ``git@`` URL with no credentials in it is shared; a local or
credentialed source stays on its worker. A worker
publishes only a plugin new to the table, a change to its enabled state, or
a marketplace source the row lacks;
explicit removals produce tombstones that inventory scans cannot resurrect. An
explicit install can replace a tombstone. The
stored version is informational (the last publisher's) and is never enforced.
Each worker reconciles its user-scope installation against that table:
install what is desired and missing, enable or disable to match, uninstall
what a tombstone removed. Project-scope plugins live with the checkout and
stay local.

A received change (``apply_desired``) uninstalls, enables or disables at
once; a plugin to install is handed to ``reconcile_missing`` on the plugin
I/O thread, because installing can clone from git. ``publish`` schedules the
same pass, so whatever a worker missed (marketplace not added yet, offline,
joined later) is installed eventually, retried with a growing delay while it
keeps failing. That pass never uninstalls and never changes an installed
plugin's enabled state; those follow received changes only, so a local
toggle that has not been published yet is never reverted. Removing a
marketplace records an uninstall for each of its plugins, otherwise the next
pass would add the marketplace back and reinstall them.

Agent-authored plugins (``<name>@authored``) are not part of this table:
their drafts and the owner's content-pinned approvals travel as memory
records instead (see :mod:`app.network.authored_plugins`). An enabled flag
alone cannot carry an approval, and no worker can install one from a
marketplace.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, Iterable, Optional

from app.db.base import get_db
from app.db import network as store
from app.network import engine

logger = logging.getLogger(__name__)
_REMOVAL_PREFIX = "network_plugin_uninstall:"
# The same URL shapes ``add_marketplace`` accepts, minus the GitHub shorthand.
# The host part admits no ``user:secret@``, so a credentialed URL never matches.
_SHAREABLE_URL = re.compile(r"^(https?://[\w\-.]+(:\d+)?(/[\w\-./%+@]*)*|git@[\w\-.]+:[\w\-./]+)$")
_RETRY_FIRST_SECONDS = 300.0
_RETRY_MAX_SECONDS = 6 * 3600.0
_retry_lock = threading.Lock()
_retry: Dict[str, tuple] = {}  # plugin id -> (failures, next attempt on time.monotonic())
_reconcile_lock = threading.Lock()
_reconcile_running = False
_reconcile_again = False
# A received change not yet applied here (local only, like every network_*
# setting). The value is a token: a job clears only the marker it read.
_RECEIVED_PREFIX = "network_plugin_received:"
_received_resumed = False


def _received(cursor) -> Dict[str, str]:
    cursor.execute("SELECT key, value FROM system_settings WHERE key LIKE %s", (_RECEIVED_PREFIX + "%",))
    return {row["key"][len(_RECEIVED_PREFIX):]: row["value"] for row in cursor.fetchall()}


def _mark_received(ids) -> Dict[str, str]:
    tokens = {plugin_id: uuid.uuid4().hex for plugin_id in ids if not _authored(plugin_id)}
    with get_db() as conn:
        cursor = conn.cursor()
        for plugin_id, token in tokens.items():
            cursor.execute("""INSERT INTO system_settings (key, value, updated_at) VALUES (%s, %s, NOW())
                              ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()""",
                           (_RECEIVED_PREFIX + plugin_id, token))
    return tokens


def note_local_change(plugin_id: str) -> None:
    """The owner changed this plugin here: that outranks a received change not yet applied."""
    try:
        with get_db() as conn:
            conn.cursor().execute("DELETE FROM system_settings WHERE key = %s", (_RECEIVED_PREFIX + plugin_id,))
    except Exception:  # noqa: BLE001 — the owner's change itself goes ahead
        logger.warning("Clearing the received plugin change for %s failed", plugin_id, exc_info=True)


def _pending_removals(cursor):
    # Local intent survives held outboxes and bootstrap. The network_* setting
    # policy excludes these markers; only signed collection tombstones travel.
    cursor.execute("SELECT key FROM system_settings WHERE key LIKE %s", (_REMOVAL_PREFIX + "%",))
    return {row["key"][len(_REMOVAL_PREFIX):] for row in cursor.fetchall()}


def request_uninstall(plugin_id: str) -> None:
    """Persist an explicit removal, even before this plugin's first publish."""
    if _authored(plugin_id):
        return
    note_local_change(plugin_id)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM network_plugins_desired WHERE id = %s", (plugin_id,))
        cursor.execute("""INSERT INTO system_settings (key, value, updated_at) VALUES (%s, %s, NOW())
                          ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()""",
                       (_REMOVAL_PREFIX + plugin_id, "1"))


def request_install(plugin_id: str, *, version: str, enabled: bool) -> None:
    """An explicit install or owner approval may replace a tombstone."""
    if _authored(plugin_id):
        return
    note_local_change(plugin_id)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM system_settings WHERE key = %s", (_REMOVAL_PREFIX + plugin_id,))
        cursor.execute("""INSERT INTO network_plugins_desired (id, version, enabled, config, updated_at)
                          VALUES (%s, %s, %s, %s, NOW()) ON CONFLICT (id) DO UPDATE SET
                          version = EXCLUDED.version, enabled = EXCLUDED.enabled, updated_at = NOW()""",
                       (plugin_id, version, enabled, json.dumps({})))


def install_plugin(service, plugin_id: str, scope: str, enable: bool):
    """An explicit install may replace a tombstone; an inventory scan may not."""
    plugin = service.install_plugin(plugin_id, scope, enable)
    if scope in ("user", "global", None) and not _authored(plugin_id):
        info = _installed(service).get(plugin_id)
        if info is not None:  # pending approval is not an owner's install
            request_install(plugin_id, **info)
    return plugin


def remove_marketplace(service, marketplace_id: str) -> bool:
    """Remove a marketplace here and uninstall its plugins on every worker.

    Its plugins are removed with it; without a tombstone for each, the shared
    rows would still list them and the next reconcile would add the
    marketplace back from the row's source and reinstall them. Desired rows
    for plugins that never installed here are covered too, for the same reason.
    """
    suffix = "@" + marketplace_id
    ids = {pid for pid in _user_scope_ids(service) if pid.endswith(suffix)}
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM network_plugins_desired")
        ids |= {r["id"] for r in cursor.fetchall() if r["id"].endswith(suffix)}
    removed = service.remove_marketplace(marketplace_id)
    if removed:
        for plugin_id in sorted(ids):
            request_uninstall(plugin_id)
    return removed


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


def _user_scope_ids(service) -> set:
    """Every user-scope install here, an agent's install awaiting review included."""
    try:
        return {p.id for p in service.get_installed_plugins()
                if getattr(p, "scope", "user") in ("user", "global", None)}
    except Exception:  # noqa: BLE001
        logger.debug("Plugin inventory unavailable", exc_info=True)
        return set()


def _config(row) -> dict:
    value = (row or {}).get("config") or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _marketplace_sources(service) -> Dict[str, dict]:
    """``{marketplace id: {"id", "url"}}`` for this worker's shareable git sources."""
    try:
        marketplaces = service.get_marketplaces()
    except Exception:  # noqa: BLE001 — a test double or an unreadable registry shares nothing
        return {}
    out = {}
    for mp in marketplaces:
        url = getattr(mp, "url", "") or ""
        if getattr(mp, "source", "git") == "git" and _SHAREABLE_URL.match(url):
            out[mp.id] = {"id": mp.id, "url": url}
    return out


def refresh_desired_from_installed() -> int:
    """Publish this worker's installed plugins that are new to the desired state
    or whose enabled state changed, and a marketplace source a row lacks; a
    differing version alone is not published."""
    service = None
    try:
        service = _service()
    except Exception:  # noqa: BLE001
        pass
    installed = _installed(service)
    sources = _marketplace_sources(service) if service is not None else {}
    changed = 0
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, version, enabled, config FROM network_plugins_desired")
        desired = {r["id"]: dict(r) for r in cursor.fetchall()}
        removed = _pending_removals(cursor) | {
            pid for pid, row in store.records_for_collection(cursor, "plugins_desired").items() if row["deleted"]}
        # A received change waiting for the plugin thread: the state here is
        # older than the row, not an edit to publish over it.
        waiting = set(_received(cursor))
        for plugin_id, info in installed.items():
            if plugin_id in removed or plugin_id in waiting:
                continue  # it may still be installed while reconciliation is queued or failing
            current = desired.get(plugin_id)
            config = _config(current)
            source = sources.get(plugin_id.rpartition("@")[2])
            # Only fill in a missing source: two workers with different URLs
            # for one marketplace must not overwrite each other on every scan.
            add_source = source is not None and not config.get("marketplace")
            # ``apply_desired`` never reinstalls to match a version, so workers
            # holding different versions would overwrite each other's row on
            # every scan. Only a new plugin or an enabled change is published.
            if current and bool(current.get("enabled")) == info["enabled"] and not add_source:
                continue
            if add_source:
                config = {**config, "marketplace": source}
            cursor.execute(
                """INSERT INTO network_plugins_desired (id, version, enabled, config, updated_at)
                   VALUES (%s, %s, %s, %s, NOW())
                   ON CONFLICT (id) DO UPDATE SET version = EXCLUDED.version, enabled = EXCLUDED.enabled,
                       config = EXCLUDED.config, updated_at = NOW()""",
                (plugin_id, info["version"], info["enabled"], json.dumps(config)))
            changed += 1
    return changed


def publish(*, ctx: Optional[engine.NetworkContext] = None) -> int:
    ctx = ctx or engine.context()
    if ctx is None or ctx.bootstrap_state != engine.BOOTSTRAP_READY:
        return 0
    _resume_received()
    refresh_desired_from_installed()
    # After the refresh, so a change made here is in the table first.
    schedule_reconcile()
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
    """Make the local installation match desired rows that just changed.

    Runs on the plugin I/O thread: an uninstall or toggle can wait for a
    plugin lock a running clone holds, and the sync thread must not. The one
    thread also orders a change received during an install after it. Without
    an app loop (tests, scripts) it runs here and returns how many it applied.
    """
    ids = list(record_ids)
    if not ids:
        return 0
    tokens: Dict[str, str] = {}
    try:
        tokens = _mark_received(ids)  # until applied, the inventory scan leaves these alone
    except Exception:  # noqa: BLE001
        logger.warning("Recording received plugin changes failed", exc_info=True)
    if _on_plugin_thread(_apply_now, ids, tokens):
        return 0
    return _apply_now(ids, tokens)


def _resume_received() -> None:
    """Once per process: apply received changes a restart cut off."""
    global _received_resumed
    if _received_resumed:
        return
    _received_resumed = True
    with get_db() as conn:
        waiting = sorted(_received(conn.cursor()))
    if waiting:
        logger.info("Applying %d plugin change(s) received before the restart", len(waiting))
        apply_desired(waiting)


def _on_plugin_thread(fn, *args) -> bool:
    """Queue ``fn`` on the plugin I/O thread without waiting; False without an app loop."""
    from app.core.global_events import global_events
    loop = getattr(global_events, "_loop", None)
    if not loop or loop.is_closed():
        return False

    async def run():
        from app.db.async_database import run_plugin_io
        try:
            await run_plugin_io(fn, *args)
        except Exception:  # noqa: BLE001
            logger.warning("Plugin sync job %s failed", getattr(fn, "__name__", fn), exc_info=True)

    try:
        asyncio.run_coroutine_threadsafe(run(), loop)
    except RuntimeError:  # the loop closed in between
        return False
    return True


def _apply_now(ids, tokens=None) -> int:
    tokens = tokens or {}
    try:
        service = _service()
    except Exception:  # noqa: BLE001
        return 0
    installed = _installed(service)
    present = _user_scope_ids(service)
    applied = 0
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, version, enabled, config FROM network_plugins_desired WHERE id IN (%s)"
                       % ",".join(["%s"] * len(ids)), tuple(ids))
        rows = {r["id"]: dict(r) for r in cursor.fetchall()}
        pending = _pending_removals(cursor)
        waiting = _received(cursor)
    to_install = []
    try:
        for plugin_id in ids:
            if _authored(plugin_id):
                continue  # never installed or enabled from this table
            if plugin_id in tokens and plugin_id not in waiting:
                continue  # the owner changed it here since it arrived; theirs stands
            row = None if plugin_id in pending else rows.get(plugin_id)
            applied += _apply_one(service, plugin_id, row, installed, present, to_install)
    finally:
        _clear_received(tokens)
    return applied + _request_installs(to_install)


def _clear_received(tokens: Dict[str, str]) -> None:
    """Clear the markers this job wrote; a newer change for the same plugin keeps its own."""
    if not tokens:
        return
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            for plugin_id, token in tokens.items():
                cursor.execute("DELETE FROM system_settings WHERE key = %s AND value = %s",
                               (_RECEIVED_PREFIX + plugin_id, token))
    except Exception:  # noqa: BLE001 — a leftover marker is applied again after a restart
        logger.warning("Clearing received plugin changes failed", exc_info=True)


def _apply_one(service, plugin_id, row, installed, present, to_install) -> int:
    """Uninstall, toggle, or queue the install of one plugin; 1 if it uninstalled."""
    try:
        if row is None:
            if plugin_id in installed:
                service.uninstall_plugin(plugin_id)
                return 1
            return 0
        if plugin_id not in present:
            to_install.append(plugin_id)  # cloning goes to the reconcile pass
            return 0
        if bool(row.get("enabled")):
            service.enable_plugin(plugin_id)
        else:
            service.disable_plugin(plugin_id)
    except Exception:  # noqa: BLE001 — a failed change shows in the plugin UI; sync goes on
        logger.warning("Applying desired plugin state for %s failed", plugin_id, exc_info=True)
    return 0


def _request_installs(plugin_ids) -> int:
    """Install received plugins on the plugin I/O thread, tried at once."""
    if not plugin_ids:
        return 0
    with _retry_lock:
        for plugin_id in plugin_ids:
            _retry.pop(plugin_id, None)
    if schedule_reconcile():
        return len(plugin_ids)
    return reconcile_missing(only=plugin_ids)  # no app loop (tests, scripts): here


def _connector(plugin_id: str) -> bool:
    try:
        from app.core import connector_packages
        return connector_packages.kind_for(plugin_id) is not None
    except Exception:  # noqa: BLE001 — an unknown connector kind is still a connector id
        return True


def _ensure_marketplace(service, plugin_id: str, row) -> None:
    """Add the plugin's marketplace from the row's shared source if it is missing here."""
    marketplace_id = plugin_id.rpartition("@")[2]
    if _connector(plugin_id):
        return  # connector packages install from the app's own catalog
    try:
        known = {mp.id: mp for mp in service.get_marketplaces()}
    except Exception:  # noqa: BLE001 — let the install report what is wrong
        return
    if marketplace_id in known:
        location = getattr(known[marketplace_id], "install_location", None)
        if location is not None and (not location or not Path(location).is_dir()):
            # Registered but never downloaded (the shipped default source
            # is fetched only when plugins are browsed).
            logger.info("Fetching plugin marketplace %s to install %s", marketplace_id, plugin_id)
            service.sync_marketplace(marketplace_id)
        return
    from app.core.plugin_marketplace import DEFAULT_SOURCE_ID
    if marketplace_id == DEFAULT_SOURCE_ID:
        return  # the app registers it on every worker once; missing means removed here
    source = _config(row).get("marketplace")
    if not isinstance(source, dict) or source.get("id") != marketplace_id:
        return  # the install fails with "Marketplace not found" and is retried later
    url = source.get("url")
    if not isinstance(url, str) or not _SHAREABLE_URL.match(url):
        return
    logger.info("Adding plugin marketplace %s to install %s from another worker", marketplace_id, plugin_id)
    service.add_marketplace(url, marketplace_id)


def _install_desired(service, plugin_id: str, row) -> None:
    """Install a desired plugin the way the row says (not an explicit install: no publish)."""
    _ensure_marketplace(service, plugin_id, row)
    service.install_plugin(plugin_id, "user", enable=bool(row.get("enabled", True)))
    with _retry_lock:
        _retry.pop(plugin_id, None)


def _note_failure(plugin_id: str) -> None:
    with _retry_lock:
        failures = _retry.get(plugin_id, (0, 0.0))[0] + 1
        delay = min(_RETRY_FIRST_SECONDS * 2 ** (failures - 1), _RETRY_MAX_SECONDS)
        _retry[plugin_id] = (failures, time.monotonic() + delay)


def _due(plugin_id: str) -> bool:
    with _retry_lock:
        entry = _retry.get(plugin_id)
    return entry is None or time.monotonic() >= entry[1]


def _current_row(plugin_id: str) -> Optional[dict]:
    """The desired row as it is now; ``None`` once removed (tombstone or local marker)."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM system_settings WHERE key = %s", (_REMOVAL_PREFIX + plugin_id,))
        if cursor.fetchone():
            return None
        cursor.execute("SELECT id, version, enabled, config FROM network_plugins_desired WHERE id = %s",
                       (plugin_id,))
        row = cursor.fetchone()
    return dict(row) if row else None


def reconcile_missing(only: Optional[Iterable[str]] = None) -> int:
    """Install every desired plugin (or each of ``only``) not installed here; return how many.

    Catches up what was missed: a worker that joined later, was offline, or
    lacked the marketplace. A plugin that keeps failing waits longer between
    tries (5 minutes, doubling to 6 hours; a restart or a newly received
    change tries again at once). Never uninstalls a plugin it did not just
    install and never changes the enabled state of an installed plugin.
    """
    try:
        service = _service()
    except Exception:  # noqa: BLE001
        return 0
    present = _user_scope_ids(service)
    if only is None:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM network_plugins_desired")
            only = [r["id"] for r in cursor.fetchall()]
    installed = 0
    for plugin_id in sorted(set(only)):
        if plugin_id in present or _authored(plugin_id) or not _due(plugin_id):
            continue
        # Read each row just before its install: an earlier install can take
        # minutes, and a removal may land meanwhile.
        row = _current_row(plugin_id)
        if row is None:
            continue
        try:
            _install_desired(service, plugin_id, row)
        except Exception as exc:  # noqa: BLE001 — one failure must not stop the others
            first = plugin_id not in _retry
            _note_failure(plugin_id)
            # One warning per plugin per process; retries log at debug.
            (logger.warning if first else logger.debug)(
                "Could not install %s to match the owner's other workers (retrying later): %s", plugin_id, exc)
            continue
        after = _current_row(plugin_id)
        if after is None:
            # Removed while it installed: the removal found nothing to
            # uninstall then, and is never applied again.
            logger.info("%s was removed on another worker while it installed; uninstalling", plugin_id)
            try:
                service.uninstall_plugin(plugin_id)
            except Exception:  # noqa: BLE001
                logger.warning("Uninstalling %s after its removal failed", plugin_id, exc_info=True)
            continue
        if bool(after.get("enabled", True)) != bool(row.get("enabled", True)):
            # Turned on or off elsewhere while it installed: match that, or
            # the next scan would publish the old state back.
            try:
                (service.enable_plugin if after.get("enabled") else service.disable_plugin)(plugin_id)
            except Exception:  # noqa: BLE001
                logger.warning("Matching the enabled state of %s failed", plugin_id, exc_info=True)
        installed += 1
        logger.info("Installed %s to match the plugins on the owner's other workers", plugin_id)
    return installed


def schedule_reconcile() -> bool:
    """Run ``reconcile_missing`` on the plugin I/O thread, one pass at a time.

    Installs can clone from git, so they never run on the sync thread. A
    request while a pass runs queues one more pass, so a row that arrived
    after the running pass listed the table is not missed. Returns False when
    there is no app loop (tests, scripts) and nothing was scheduled.
    """
    global _reconcile_running, _reconcile_again
    from app.core.global_events import global_events
    loop = getattr(global_events, "_loop", None)
    if not loop or loop.is_closed():
        return False
    with _reconcile_lock:
        if _reconcile_running:
            _reconcile_again = True
            return True
        _reconcile_running = True

    async def run():
        global _reconcile_running, _reconcile_again
        from app.db.async_database import run_plugin_io
        while True:
            try:
                await run_plugin_io(reconcile_missing)
            except Exception:  # noqa: BLE001
                logger.warning("Plugin reconcile failed", exc_info=True)
            with _reconcile_lock:
                if not _reconcile_again:
                    _reconcile_running = False
                    return
                _reconcile_again = False

    try:
        asyncio.run_coroutine_threadsafe(run(), loop)
    except RuntimeError:  # the loop closed in between
        with _reconcile_lock:
            _reconcile_running = False
        return False
    return True


from app.core.resource_sync import install_database_mutations as _install_resource_sync

_install_resource_sync(globals(), 'plugins', 'request_uninstall install_plugin remove_marketplace', scope='shared')
