"""Account sync: this worker's side of the hub's per-network feed.

The account hub (a Cloudflare Worker with D1, a Durable Object per network
and R2; see docs/network/hub-api.md) stores ciphertext and cursors only. A
worker signed into the account:

- registers its device in the account's network (``register``); the first
  worker creates the network, later ones wait for approval from an online
  member or unlock with the recovery key
- pushes every operation in its log in ingest order (``push``); the hub
  deduplicates on (origin, incarnation, seq)
- pulls the feed from its cursor (``pull``) and applies it like any peer
  delivery; the same operation over LAN and hub applies once
- approves pending devices (``approve_device``) by handing the hub a join
  response wrapped to that device's keys

Quota errors stop cloud durability, never LAN sync: the backlog is shown
in the status and retried.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from app.core import account_hub
from app.core.config import settings
from app.db import mobile_hub as binding_store
from app.db import network as store
from app.network import crypto, engine, identity as identity_mod, membership

logger = logging.getLogger(__name__)

PUSH_BATCH = 200
PULL_BATCH = 500
FEED_MAX_BYTES = 8 * 1024 * 1024
LOOP_IDLE_SECONDS = 15.0
LOOP_ACTIVE_SECONDS = 1.0
APPROVAL_POLL_SECONDS = 5.0

_tasks: List[asyncio.Task] = []
_wake: Optional[asyncio.Event] = None
_loop: Optional[asyncio.AbstractEventLoop] = None
_state: Dict[str, Any] = {"linked": False}
_recovery_upload_due = False


class HubError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

def _binding() -> Optional[dict]:
    try:
        binding = binding_store.get_binding()
    except Exception:  # noqa: BLE001
        return None
    if not binding or not binding.get("active"):
        return None
    return binding


def _call(method: str, suffix: str, body=None, *, max_bytes: int = 16384, timeout: Optional[float] = None) -> dict:
    binding = _binding()
    if binding is None:
        raise HubError("This worker is not signed in to a Shuffle account")
    try:
        return account_hub.call(binding, method, "network/" + suffix if suffix else "network", body,
                                max_bytes=max_bytes, timeout=timeout)
    except HTTPException as exc:
        raise HubError(str(exc.detail)) from exc


def _device_description(ident: identity_mod.DeviceIdentity, name: Optional[str] = None) -> dict:
    return {
        "device_id": ident.device_id,
        "signing_public": crypto.b64u_encode(ident.signing_public),
        "encryption_public": crypto.b64u_encode(ident.encryption_public),
        "incarnation": ident.incarnation,
        "name": name or engine._default_device_name(),
        "platform": identity_mod.platform_name(),
        "kind": membership.KIND_WORKER,
        "version": settings.version,
    }


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def _hub_state() -> dict:
    state = store.get_state()
    return dict((state or {}).get("hub") or {})


def _save_hub_state(**fields) -> dict:
    if store.get_state() is None:
        return {}
    current = _hub_state()
    current.update(fields)
    store.update_state(hub=current)
    return current


def status() -> dict:
    binding = _binding()
    hub_state = _hub_state() if store.get_state() else {}
    return {
        "linked": binding is not None,
        "server_id": binding.get("server_id") if binding else None,
        "registered": bool(hub_state.get("registered")),
        "status": hub_state.get("status") or _state.get("status"),
        "cursor": hub_state.get("cursor", 0),
        "pushed_ingest_id": hub_state.get("pushed_ingest_id", 0),
        "backlog": _backlog(hub_state),
        "last_push_at": hub_state.get("last_push_at"),
        "last_pull_at": hub_state.get("last_pull_at"),
        "last_error": _state.get("last_error"),
        "quota": hub_state.get("quota"),
        "pending_devices": _state.get("pending_devices") or [],
    }


def _backlog(hub_state: dict) -> int:
    try:
        with store.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) AS n FROM sync_ops WHERE ingest_id > %s",
                           (int(hub_state.get("pushed_ingest_id", 0) or 0),))
            return int(cursor.fetchone()["n"])
    except Exception:  # noqa: BLE001
        return 0


# ---------------------------------------------------------------------------
# Registration, approval, recovery
# ---------------------------------------------------------------------------

def register() -> dict:
    """Tell the hub about this device and learn where the network stands."""
    ident = identity_mod.load()
    ctx = engine.context()
    body = {"device": _device_description(ident, ctx.member(ctx.device_id).name if ctx and ctx.member(ctx.device_id) else None)}
    if ctx is not None:
        body["network_id"] = ctx.network_id
        body["founder_device_id"] = ctx.founder_device_id
        body["generation"] = ctx.generation
        state = store.get_state() or {}
        if state.get("recovery"):
            body["recovery"] = state["recovery"]
    result = _call("POST", "", body)
    _state["status"] = result.get("status")
    if ctx is not None:
        _save_hub_state(registered=True, status=result.get("status"), network_hub_id=result.get("network_hub_id"),
                        quota=result.get("quota"))
    return result


def pending_devices() -> List[dict]:
    result = _call("GET", "pending")
    devices = list(result.get("devices") or [])
    _state["pending_devices"] = devices
    return devices


def approve_device(device_id: str, *, fingerprint_confirmed: bool) -> dict:
    """Admit a device that registered through the account."""
    if not fingerprint_confirmed:
        raise HubError("Confirm the device's fingerprint before approving")
    device = next((d for d in pending_devices() if d.get("device_id") == device_id), None)
    if device is None:
        raise HubError("No such pending device")
    signing_public = crypto.b64u_decode(device["signing_public"])
    encryption_public = crypto.b64u_decode(device["encryption_public"])
    if membership.device_id_for(signing_public) != device_id:
        raise HubError("Pending device id does not match its key")
    response = engine.admit_device(device_id=device_id, signing_public=signing_public,
                                   encryption_public=encryption_public, name=device.get("name") or device_id,
                                   platform=device.get("platform"), legacy_instance_id=device.get("legacy_instance_id"))
    # Only the device can read the wrapped keys; the rest is public membership data.
    _call("POST", f"devices/{device_id}/approve", {"response": response}, max_bytes=65536)
    kick()
    return {"device_id": device_id, "status": "approved"}


def reject_device(device_id: str) -> dict:
    _call("POST", f"devices/{device_id}/reject", {})
    return {"device_id": device_id, "status": "rejected"}


def _await_approval_sync() -> Optional[dict]:
    """A registered-but-pending device asks whether it was admitted."""
    result = _call("GET", "approval", max_bytes=1 << 20)
    if result.get("status") == "approved" and result.get("response"):
        return result["response"]
    _state["status"] = result.get("status")
    return None


def schedule_recovery_upload() -> None:
    global _recovery_upload_due
    _recovery_upload_due = True
    kick()


def _upload_recovery() -> None:
    """Upload a new wrap or durably tombstone an invalidated one."""
    global _recovery_upload_due
    state = store.get_state() or {}
    wrap = state.get("recovery")
    if not wrap:
        _recovery_upload_due = False
        return
    if wrap.get("stale"):
        if not wrap.get("hub_tombstoned"):
            _call("POST", "recovery", {"recovery": None})
            wrap = dict(wrap)
            wrap["hub_tombstoned"] = True
            store.update_state(recovery=wrap)
        _recovery_upload_due = False
        return
    _call("POST", "recovery", {"recovery": wrap})
    _recovery_upload_due = False


async def restore_with_recovery(recovery_key_text: str, *, device_name: Optional[str] = None) -> dict:
    """Fresh worker, every other worker off: unlock with the recovery key and replay the feed."""
    from app.network import recovery as recovery_mod
    info = await asyncio.to_thread(_call, "GET", "", None, max_bytes=1 << 20)
    wrap = info.get("recovery")
    if not wrap:
        raise HubError("This account's network has no recovery key on file")
    response = recovery_mod.response_from_wrap(
        recovery_key_text, wrap, name=info.get("name"), founder_device_id=info["founder_device_id"],
        generation=int(info.get("generation") or 0), membership_records=[], device_name=device_name)
    await asyncio.to_thread(engine.enroll_from_response, response, device_name=device_name or engine._default_device_name())
    await asyncio.to_thread(_replay_feed_from_start)
    await asyncio.to_thread(engine.finish_bootstrap)
    await asyncio.to_thread(register)
    await asyncio.to_thread(engine.publish_presence, version=settings.version)
    kick()
    return {"restored": True, "network": engine.require_context().summary()}


def _replay_feed_from_start() -> None:
    cursor = 0
    ctx = engine.require_context()
    while True:
        page = _call("GET", f"feed?cursor={cursor}&limit={PULL_BATCH}", max_bytes=FEED_MAX_BYTES, timeout=60)
        ops = page.get("ops") or []
        if ops:
            engine.apply_batch(ops, ctx=ctx)
        cursor = int(page.get("cursor", cursor))
        if not page.get("more"):
            break
    _save_hub_state(cursor=cursor, registered=True, status="active")


# ---------------------------------------------------------------------------
# Push and pull
# ---------------------------------------------------------------------------

def push_once() -> int:
    """Send the next batch of our log to the hub. Returns how many went."""
    hub_state = _hub_state()
    pushed = int(hub_state.get("pushed_ingest_id", 0) or 0)
    batch = store.ops_after_ingest(pushed, limit=PUSH_BATCH)
    if not batch:
        return 0
    result = _call("POST", "ops", {"ops": [env for _, env in batch]}, max_bytes=65536, timeout=30)
    if result.get("quota_exceeded"):
        _save_hub_state(quota=result.get("quota"))
        raise HubError("Account storage quota exceeded; cloud backlog kept")
    _save_hub_state(pushed_ingest_id=batch[-1][0], last_push_at=time.time(), quota=result.get("quota"))
    return len(batch)


def pull_once() -> int:
    hub_state = _hub_state()
    cursor = int(hub_state.get("cursor", 0) or 0)
    page = _call("GET", f"feed?cursor={cursor}&limit={PULL_BATCH}", max_bytes=FEED_MAX_BYTES, timeout=60)
    ops = page.get("ops") or []
    applied = 0
    if ops:
        result = engine.apply_batch(ops)
        applied = result.applied
        if applied:
            engine.kick()
    new_cursor = int(page.get("cursor", cursor))
    if new_cursor != cursor or ops:
        _save_hub_state(cursor=new_cursor, last_pull_at=time.time())
    return applied


# ---------------------------------------------------------------------------
# Loop
# ---------------------------------------------------------------------------

async def _loop_body() -> bool:
    """One pass. Returns True when there is more to do soon."""
    global _recovery_upload_due
    if _binding() is None:
        _state["linked"] = False
        return False
    _state["linked"] = True
    ctx = engine.context()
    hub_state = _hub_state() if ctx else {}
    try:
        if ctx is None:
            # Signed in but not enrolled: register and wait for an approval.
            result = await asyncio.to_thread(register)
            if result.get("status") == "approved" or result.get("response"):
                response = result.get("response") or await asyncio.to_thread(_await_approval_sync)
                if response:
                    await asyncio.to_thread(engine.enroll_from_response, response,
                                            device_name=engine._default_device_name())
                    await asyncio.to_thread(_replay_feed_from_start)
                    await asyncio.to_thread(engine.finish_bootstrap)
                    await asyncio.to_thread(engine.publish_presence, version=settings.version)
                    engine.kick()
                    return True
            elif result.get("status") == "pending":
                response = await asyncio.to_thread(_await_approval_sync)
                if response:
                    await asyncio.to_thread(engine.enroll_from_response, response,
                                            device_name=engine._default_device_name())
                    await asyncio.to_thread(_replay_feed_from_start)
                    await asyncio.to_thread(engine.finish_bootstrap)
                    await asyncio.to_thread(engine.publish_presence, version=settings.version)
                    engine.kick()
                    return True
            _state["last_error"] = None
            return False
        if not hub_state.get("registered"):
            await asyncio.to_thread(register)
        recovery_wrap = (store.get_state() or {}).get("recovery") or {}
        if _recovery_upload_due or (recovery_wrap.get("stale") and not recovery_wrap.get("hub_tombstoned")):
            await asyncio.to_thread(_upload_recovery)
        if ctx.bootstrap_state == engine.BOOTSTRAP_READY:
            sent = await asyncio.to_thread(push_once)
            received = await asyncio.to_thread(pull_once)
            try:
                await asyncio.to_thread(pending_devices)
            except HubError:
                pass
            _state["last_error"] = None
            return sent >= PUSH_BATCH or received >= PULL_BATCH
    except HubError as exc:
        _state["last_error"] = str(exc)[:200]
        logger.debug("Hub sync: %s", exc)
    except Exception:  # noqa: BLE001
        _state["last_error"] = "hub sync failed"
        logger.warning("Hub sync failed", exc_info=True)
    return False


async def loop() -> None:
    global _wake
    _wake = asyncio.Event()
    while True:
        try:
            busy = await _loop_body()
        except asyncio.CancelledError:
            raise
        busy = busy or False
        try:
            await asyncio.wait_for(_wake.wait(), timeout=LOOP_ACTIVE_SECONDS if busy else LOOP_IDLE_SECONDS)
        except asyncio.TimeoutError:
            pass
        _wake.clear()


def kick() -> None:
    loop_ref, wake = _loop, _wake
    if loop_ref is None or wake is None:
        return
    try:
        loop_ref.call_soon_threadsafe(wake.set)
    except RuntimeError:
        pass


def start() -> List[asyncio.Task]:
    global _loop
    _loop = asyncio.get_running_loop()
    _tasks[:] = [asyncio.create_task(loop(), name="network-hub")]
    return list(_tasks)


async def stop() -> None:
    global _loop
    for task in _tasks:
        task.cancel()
    for task in _tasks:
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _tasks.clear()
    _loop = None


def reset_for_tests() -> None:
    global _recovery_upload_due
    _state.clear()
    _state["linked"] = False
    _recovery_upload_due = False
