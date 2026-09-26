"""Boot-time wiring for the worker network.

``initialize()`` runs synchronously right after the database is ready:

- makes sure this worker has a device identity
- turns an existing single install into a network of one (a fresh install
  waits for the first-run choice instead), stamping ``owner_device_id`` on
  the rows that still only carry ``instance_id`` that one time
- hooks resource invalidation so a domain write triggers a scan

``start()`` launches the sync, scan and presence loops once credentials are
unlocked; ``stop()`` cancels them at shutdown.
"""
from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

from app.core.config import settings
from app.db import database
from app.db import network as store
from app.network import engine, identity as identity_mod, policy

logger = logging.getLogger(__name__)

_tasks: List[asyncio.Task] = []


class PrivateDatabaseRequiredError(RuntimeError):
    """Starting work against another device's network database is unsafe."""


def initialize() -> Optional[engine.NetworkContext]:
    state = store.get_state()
    try:
        ident = identity_mod.load(create=state is None)
    except identity_mod.IdentityError as exc:
        # The application treats this as fatal before starting hub registration
        # or schedulers. A missing key is recovery, not a brand-new worker.
        raise PrivateDatabaseRequiredError(str(exc)) from exc
    logger.info("Device identity %s (%s)", ident.device_id, ident.fingerprint)
    if state is not None and not any(
        row["device_id"] == ident.device_id for row in store.list_members()
    ):
        raise PrivateDatabaseRequiredError(
            "A worker network requires a private database and its matching device keys. "
            "Do not point another worker at this database; migrate it to a fresh database "
            "or restore the matching .credential-keys backup."
        )
    ctx = engine.load_context()
    if ctx is None and _is_existing_install():
        ctx = _migrate_single_install()
        if ctx is not None:
            # Once, when the private database becomes a network: later boots
            # leave ownership alone so an explicit ``assign(None)`` holds.
            backfill_owner_device_ids(ctx.device_id)
    if ctx is not None:
        try:
            from app.network import blobs
            blobs.backfill_attachment_hashes()
        except Exception:  # noqa: BLE001
            logger.debug("Attachment hash backfill failed", exc_info=True)
    try:
        # Older builds kept a losing remote write to an encrypted column in
        # plaintext; scrub those rows (a no-op once they are redacted).
        redacted = engine.redact_stored_conflicts()
        if redacted:
            logger.info("Redacted secrets in %d stored sync conflicts", redacted)
    except Exception:  # noqa: BLE001
        logger.warning("Could not redact stored sync conflicts", exc_info=True)
    _install_resource_hook()
    return ctx


def _is_existing_install() -> bool:
    """A database that already served a user gets a network of one silently."""
    try:
        with store.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) AS n FROM installs")
            if int(cursor.fetchone()["n"]) > 1:
                logger.warning("Shared database detected; automatic network enrollment is disabled. Follow the migration runbook.")
                return False
        if database.get_system_setting("onboarding_complete") in ("true", "1", "True"):
            return True
        with store.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) AS n FROM sessions")
            row = cursor.fetchone()
            return bool(row and int(row["n"]) > 0)
    except Exception:  # noqa: BLE001
        return False


def _migrate_single_install() -> Optional[engine.NetworkContext]:
    """Milestone 6 (single install): this database becomes a network of one."""
    try:
        ctx = engine.create_network(None, legacy_instance_id=settings.effective_instance_id)
        logger.info("Existing install migrated to network %s (device %s)", ctx.network_id, ctx.device_id)
        return ctx
    except Exception:  # noqa: BLE001
        logger.exception("Could not create the network for this install; the worker keeps running standalone")
        return None


def backfill_owner_device_ids(device_id: str, *, include_unscoped: bool = True) -> dict:
    """Rows owned by this install (by instance_id or NULL) get this device id.

    Rows that name another install's instance id are left for that install's
    device to claim through the legacy map when it joins. Runs once, when an
    install migrates or admits a legacy install — not at every boot.

    Automations carry no install column, so every unowned one is claimed
    only with ``include_unscoped`` (the single-install migration). A later
    legacy admission leaves them alone: an unowned automation by then is one
    somebody unassigned on purpose.
    """
    counts = {}
    me = settings.effective_instance_id
    ctx = engine.context()
    legacy_map = dict(ctx.legacy_instance_map) if ctx else {}
    legacy_map[me] = device_id
    with store.get_db() as conn:
        cursor = conn.cursor()
        for table, column in (("sessions", "instance_id"), ("automations", None), ("connectors", "runner_id"),
                              ("artifacts", "runner_id")):
            if column:
                cursor.execute(
                    f"UPDATE {table} SET owner_device_id = %s WHERE owner_device_id IS NULL AND ({column} IS NULL OR {column} = '' OR {column} = %s)",
                    (device_id, me))
            elif include_unscoped:
                cursor.execute(f"UPDATE {table} SET owner_device_id = %s WHERE owner_device_id IS NULL", (device_id,))
            else:
                counts[table] = 0
                continue
            counts[table] = cursor.rowcount or 0
            if column:
                for instance_id, mapped in legacy_map.items():
                    if instance_id == me:
                        continue
                    cursor.execute(
                        f"UPDATE {table} SET owner_device_id = %s WHERE owner_device_id IS NULL AND {column} = %s",
                        (mapped, instance_id))
                    counts[table] += cursor.rowcount or 0
    if any(counts.values()):
        logger.info("Stamped owner_device_id: %s", counts)
    return counts


def _install_resource_hook() -> None:
    """Resource invalidations (post-commit) ask the scanner to look now."""
    from app.core import resource_sync
    if getattr(resource_sync, "_network_hooked", False):
        return
    original = resource_sync._emit

    def emit(resource, scope, arguments, before, result):
        original(resource, scope, arguments, before, result)
        collections = policy.collections_for_resource(resource)
        if collections:
            # Every registered session mutation names one ``session_id`` and
            # writes only that session's rows, except ``delete_session``, which
            # also unlinks forks; a missing session widens the pass to the
            # whole catalog (``catalog.publish_owned``). Otherwise leave the
            # ids out so the next catalog pass covers every session.
            session_id = arguments.get("session_id") if resource == "sessions" and scope == "session" else None
            try:
                from app.network import transport
                transport.request_scan([collection.name for collection in collections],
                                       session_ids=[session_id] if isinstance(session_id, str) and session_id else None)
            except Exception:  # noqa: BLE001
                pass

    resource_sync._emit = emit
    resource_sync._network_hooked = True


# A connector start already past its eligibility check when a fence lands is
# still CONNECTING: it is not registered yet, and it registers (and runs) once
# its connect() returns. The fenced reconcile keeps sweeping this long for such
# late registrations, until the worker is re-admitted.
FENCE_SETTLE_SECONDS = 30.0
FENCE_SWEEP_INTERVAL = 0.5

_reconcile_lock: Optional[asyncio.Lock] = None
_reconcile_lock_loop: Optional[asyncio.AbstractEventLoop] = None


def _connector_reconcile_lock() -> asyncio.Lock:
    """One lock per event loop: reconciles run one at a time, in order."""
    global _reconcile_lock, _reconcile_lock_loop
    loop = asyncio.get_running_loop()
    if _reconcile_lock is None or _reconcile_lock_loop is not loop:
        _reconcile_lock, _reconcile_lock_loop = asyncio.Lock(), loop
    return _reconcile_lock


async def _stop_running_connectors(connector_manager) -> None:
    for connector_id, _instance in list(connector_manager.get_running_connectors()):
        try:
            await connector_manager.stop_connector(connector_id)
        except Exception:  # noqa: BLE001 - one failure must not keep the rest connected
            logger.warning("Stopping fenced connector %s failed", connector_id, exc_info=True)


async def _reconcile_owned_connectors() -> None:
    """Close connections when committed membership changes fence this worker.

    Serialised: a fence and the re-admission right after it are applied in
    the order they were scheduled, so the later decision is the one that holds.
    """
    from app.core.connectors.manager import connector_manager
    from app.network import ownership
    async with _connector_reconcile_lock():
        if ownership.can_execute_local():
            await connector_manager.start_all()
            return
        # Stop the running connectors but keep the lifecycle relay:
        # ``start_all`` does not restart it when execution is re-admitted.
        # The manager's registry holds CONNECTING clients too; a start whose
        # connect() is still in flight registers later, so keep sweeping.
        await _stop_running_connectors(connector_manager)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + FENCE_SETTLE_SECONDS
        while loop.time() < deadline:
            await asyncio.sleep(FENCE_SWEEP_INTERVAL)
            if ownership.can_execute_local():
                break  # re-admitted: the reconcile queued behind this one starts them
            await _stop_running_connectors(connector_manager)


def _adopt_presence_endpoints(device_ids) -> None:
    """A peer's presence record is its latest word on its own addresses.

    Keep the member row in step with it, so an address the peer withdrew (its
    tunnel, after signing out of the account) stops being dialed even when
    the record reached us through another worker. Peers dial the tunnel
    alone while it is listed, so a stale listing would keep them offline.
    """
    from app.db import network as store
    from app.network import engine
    from app.network.endpoints import validate_endpoints
    ctx = engine.context()
    if ctx is None:
        return
    for device_id in set(device_ids):
        if device_id == ctx.device_id or store.get_member(device_id) is None:
            continue
        endpoints = validate_endpoints((engine.presence_of(device_id) or {}).get("endpoints"))
        if endpoints:
            store.touch_member(device_id, endpoints=endpoints)


def _on_applied(touched) -> None:
    """Remote records landed: tell connected UIs so they refresh, and materialize side effects."""
    from app.core.global_events import global_events
    resources = set()
    if any(collection == "membership" for collection, _record, _op in touched):
        loop = getattr(global_events, "_loop", None)
        if loop and not loop.is_closed():
            asyncio.run_coroutine_threadsafe(_reconcile_owned_connectors(), loop)
    memory_ids = [r for c, r, _ in touched if c == "memory"]
    plugin_ids = [r for c, r, _ in touched if c == "plugins_desired"]
    if memory_ids:
        try:
            from app.network import memory_records
            memory_records.apply_to_files(memory_ids)
        except Exception:  # noqa: BLE001
            logger.warning("Applying memory records failed", exc_info=True)
    if plugin_ids:
        try:
            from app.network import plugins_sync
            plugins_sync.apply_desired(plugin_ids)
        except Exception:  # noqa: BLE001
            logger.warning("Applying plugin state failed", exc_info=True)
    presence_ids = [r for c, r, _ in touched if c == "presence"]
    if presence_ids:
        try:
            _adopt_presence_endpoints(presence_ids)
        except Exception:  # noqa: BLE001
            logger.warning("Applying peer addresses failed", exc_info=True)
    for collection, record_id, _op in touched:
        coll = policy.COLLECTIONS.get(collection)
        if collection == "user_preferences":
            try:
                user_type, user_id, key = coll.key_values(record_id)
                if user_id == policy.OWNER_PLACEHOLDER:
                    from app.core import owner_auth
                    user_id = owner_auth.preference_identity()
                global_events.preference_changed(user_type, user_id, key)
            except Exception:  # noqa: BLE001
                pass
            continue
        if collection == "system_settings":
            database.invalidate_system_setting_memo(record_id)
            if record_id == "allow_updates":
                from app.core.application_updates import mirror_policy
                mirror_policy()
        if coll is not None and coll.resource:
            resources.add(coll.resource)
        if collection in ("membership", "presence"):
            resources.update(("network", "sessions"))
    for resource in resources:
        try:
            # Remote applies bypass the local mutation decorators. Account and
            # paired browsers need the same content-free refetch signal.
            for kind in ("admin", "api_user", "account", "paired"):
                global_events.resources_changed(resource, user_type=kind)
        except Exception:  # noqa: BLE001
            pass


async def start() -> List[asyncio.Task]:
    from app.network import transport, enrollment, hub
    from app.core import managed_connection
    engine.on_applied(_on_applied)
    _tasks[:] = transport.start()
    enrollment.resume_enrollment()
    try:
        _tasks.extend(hub.start())
    except Exception:  # noqa: BLE001
        logger.debug("Hub feed not started", exc_info=True)
    # Account connectivity is enrollment infrastructure, not workload execution.
    # It must run before approval, including when approval arrives after boot.
    _tasks.append(asyncio.create_task(managed_connection.supervise(), name="managed-account-connection"))
    return list(_tasks)


async def stop() -> None:
    from app.network import transport, hub, startup_sync
    await startup_sync.stop()
    await transport.stop()
    try:
        await hub.stop()
    except Exception:  # noqa: BLE001
        pass
    for task in _tasks:
        task.cancel()
    await asyncio.gather(*_tasks, return_exceptions=True)
    _tasks.clear()
