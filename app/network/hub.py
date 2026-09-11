"""Account sync: this worker's side of the hub's per-network feed.

The account hub (a Cloudflare Worker with D1; see docs/network/hub-api.md)
stores ciphertext and cursors only. A
worker signed into the account:

- registers its device in the account's network (``register``); the first
  worker to sign in founds the account's network, later ones are admitted
  by whichever member is online (``admit_pending_devices``) or unlock with
  the recovery key when every other worker is off
- pushes every operation in its log in ingest order (``push``); the hub
  deduplicates on (origin, incarnation, seq)
- pulls the feed from its cursor (``pull``) and applies it like any peer
  delivery; the same operation over LAN and hub applies once
- admits devices that registered through the account (``approve_device``)
  by handing the hub a join response wrapped to that device's keys. Signing
  in to the account is the authorization; no one compares fingerprints.

A worker that already has its own network and signs in to an account whose
network was founded elsewhere is answered ``conflict`` by the hub; the
account always wins, so the loop adopts the account's network at once
(``adopt_account_network``): the local network bookkeeping is replaced and
local-only rows are merged into the account's. A worker signed in to an
account is therefore always in that account's network; there is no second
state to manage.

Quota errors stop cloud durability, never LAN sync: the backlog is shown
in the status and retried.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sys
import time
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from app.core import account_hub
from app.core.config import settings
from app.db import mobile_hub as binding_store
from app.db import network as store
from app.db.async_database import run_sync
from app.network import checkpoints, cloud_blobs, crypto, engine, identity as identity_mod, membership, object_sync

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
_bootstrap_lock: Optional[asyncio.Lock] = None
_object_poll_at: dict[str, float] = {}
_object_work_at: dict[str, float] = {}


class HubError(RuntimeError):
    pass


class UploadBlocked(HubError):
    """Durable upload cooldown; reading and admitting devices may continue."""


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


def _save_hub_state(*, _expected_scope: str | None = None, **fields) -> dict:
    with store.get_db() as conn:
        state = store.lock_state(conn.cursor())
        if _expected_scope is not None and ((state or {}).get("hub") or {}).get("binding_scope") != _expected_scope:
            raise HubError("Account network changed during sync")
        if state is None:
            return {}
        current = dict(state.get("hub") or {})
        for key, value in fields.items():
            current[key] = max(int(current.get(key) or 0), int(value)) if key in ("cursor", "pushed_ingest_id", "archive_cursor") else value
        store.update_state(_conn=conn, hub=current)
        return current


def _ensure_hub_scope() -> str:
    """Receipts belong to this binding and network, never a different account."""
    binding = _binding()
    if binding is None:
        raise HubError("This worker is not signed in to a Shuffle account")
    ctx = engine.require_context()
    scope = hashlib.sha256(json.dumps([binding.get("hub_url"), binding.get("server_id"), ctx.network_id]).encode()).hexdigest()
    with store.get_db() as conn:
        state = store.lock_state(conn.cursor())
        if state is None or state["network_id"] != ctx.network_id:
            raise HubError("Account network changed during sync")
        current = dict((state or {}).get("hub") or {})
        previous = current.get("binding_scope")
        if previous != scope:
            if previous is not None:
                current.update(cursor=0, pushed_ingest_id=0, upload_blocked=None, quota=None,
                               registered=False, registered_name=None, object_sync=None, object_batch_due=None,
                               checkpoint_id=None, checkpoint_cursor=0, checkpoint_last_at=0, archive_cursor=0, restored_checkpoint=None)
            current["binding_scope"] = scope
            store.update_state(_conn=conn, hub=current)
    return scope


def status() -> dict:
    binding = _binding()
    hub_state = _hub_state() if store.get_state() else {}
    backlog = _backlog(hub_state)
    file_backlog = cloud_blobs.pending_count(hub_state.get("binding_scope", "")) if hub_state else 0
    incoming = object_sync.inbox_count(hub_state.get("binding_scope", "")) if hub_state.get("object_sync") else 0
    return {
        "linked": binding is not None,
        "server_id": binding.get("server_id") if binding else None,
        "registered": bool(hub_state.get("registered")),
        "status": hub_state.get("status") or _state.get("status"),
        "cursor": hub_state.get("cursor", 0),
        "pushed_ingest_id": hub_state.get("pushed_ingest_id", 0),
        "backlog": backlog,
        "files_pending_backup": file_backlog,
        "incoming_pending": incoming,
        "last_push_at": hub_state.get("last_push_at"),
        "last_pull_at": hub_state.get("last_pull_at"),
        "last_error": _state.get("last_error") or ("Account upload quota exceeded; cloud backlog kept" if hub_state.get("upload_blocked") else None),
        "quota": _current_quota(hub_state.get("quota")),
        "upload_blocked": hub_state.get("upload_blocked"),
        "cloud_storage": "objects" if (hub_state.get("object_sync") or {}).get("writes") else "feed",
        "remote_durability": "local_only" if binding is None else "pending" if not hub_state.get("registered") or backlog or file_backlog or incoming else "committed",
        "batch_due_at": hub_state.get("object_batch_due"),
        "recovery_checkpoint": hub_state.get("checkpoint_id"),
        "recovery_checkpoint_at": hub_state.get("checkpoint_last_at"),
        "pending_devices": _state.get("pending_devices") or [],
        "account_network": _state.get("account_network"),
    }


def forget_network() -> None:
    """The worker left its network: drop what was learned about the account's."""
    for key in ("status", "pending_devices", "account_network", "last_error"):
        _state.pop(key, None)


def _backlog(hub_state: dict) -> int:
    try:
        with store.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""SELECT COUNT(*) AS n FROM sync_ops o WHERE ingest_id > %s
                              AND NOT EXISTS (SELECT 1 FROM sync_hub_receipts r WHERE r.hub_scope=%s
                                AND r.origin_device_id=o.origin_device_id AND r.incarnation=o.incarnation AND r.seq=o.seq)""",
                           (int(hub_state.get("pushed_ingest_id", 0) or 0), hub_state.get("binding_scope", "")))
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
    if ctx is not None:
        _ensure_hub_scope()
    member = store.get_member(ctx.device_id) if ctx else None
    body = {"device": _device_description(ident, member["name"] if member else None)}
    if ctx is not None:
        body["network_id"] = ctx.network_id
        body["founder_device_id"] = ctx.founder_device_id
        body["generation"] = ctx.generation
        state = store.get_state() or {}
        if state.get("recovery"):
            body["recovery"] = state["recovery"]
    result = _call("POST", "", body)
    _state["status"] = result.get("status")
    if result.get("status") == "conflict":
        # The account's network was founded elsewhere; remember whose so the
        # UI can say which workers will admit this one once it switched.
        _state["account_network"] = _account_network(_summary_or_none())
    if ctx is not None:
        _save_hub_state(registered=result.get("status") != "conflict", status=result.get("status"),
                        registered_name=body["device"]["name"],
                        network_hub_id=result.get("network_hub_id"), quota=result.get("quota"))
    return result


def _summary_or_none() -> Optional[dict]:
    try:
        return _call("GET", "", None, max_bytes=1 << 20)
    except HubError:
        return None


def _account_network(info: Optional[dict]) -> Optional[dict]:
    """What the UI needs to explain who can admit this worker."""
    if not info:
        return None
    devices = []
    for device in info.get("devices") or []:
        devices.append({k: device.get(k) for k in ("device_id", "name", "platform", "kind", "status", "version")})
    return {"network_id": info.get("network_id"), "name": info.get("name"),
            "founder_device_id": info.get("founder_device_id"), "devices": devices,
            "recovery_available": bool(info.get("recovery"))}


def pending_devices() -> List[dict]:
    result = _call("GET", "pending")
    devices = list(result.get("devices") or [])
    _state["pending_devices"] = devices
    return devices


def approve_device(device_id: str, *, fingerprint_confirmed: bool = True) -> dict:
    """Admit a device that registered through the account.

    The account sign-in is the authorization: the hub only lists devices
    registered with a server secret bound to this same account.
    """
    device = next((d for d in pending_devices() if d.get("device_id") == device_id), None)
    if device is None:
        raise HubError("No such pending device")
    return _admit(device)


def _admit(device: dict) -> dict:
    device_id = device["device_id"]
    try:
        signing_public = crypto.b64u_decode(device["signing_public"])
        encryption_public = crypto.b64u_decode(device["encryption_public"])
    except (KeyError, TypeError, crypto.CryptoError) as exc:
        raise HubError("Pending device has malformed keys") from exc
    if membership.device_id_for(signing_public) != device_id:
        raise HubError("Pending device id does not match its key")
    response = engine.admit_device(device_id=device_id, signing_public=signing_public,
                                   encryption_public=encryption_public, name=device.get("name") or device_id,
                                   platform=device.get("platform"), legacy_instance_id=device.get("legacy_instance_id"))
    # Only the device can read the wrapped keys; the rest is public membership data.
    _call("POST", f"devices/{device_id}/approve", {"response": response}, max_bytes=65536)
    kick()
    return {"device_id": device_id, "status": "approved"}


def admit_pending_devices(devices: list[dict] | None = None) -> int:
    """Admit every device waiting through the account. Returns how many were admitted."""
    admitted = []
    for device in pending_devices() if devices is None else devices:
        try:
            _admit(device)
            admitted.append(device.get("device_id"))
        except (HubError, engine.NotEnrolled) as exc:
            logger.warning("Could not admit account device %s: %s", device.get("device_id"), exc)
        except Exception:  # noqa: BLE001
            logger.warning("Could not admit account device %s", device.get("device_id"), exc_info=True)
    if admitted:
        _state["pending_devices"] = [d for d in (_state.get("pending_devices") or [])
                                     if d.get("device_id") not in admitted]
    return len(admitted)


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
    _save_hub_state(checkpoint_last_at=0)
    _recovery_upload_due = False


def found_account_network(*, device_name: Optional[str] = None) -> dict:
    """First worker on this account: create the network here and register it."""
    if engine.context() is not None:
        raise HubError("This worker already belongs to a network")
    engine.create_network(None, device_name=device_name, legacy_instance_id=settings.effective_instance_id)
    result = register()
    if result.get("status") != "active":
        raise HubError("The account did not accept this worker's network")
    try:
        engine.publish_presence(version=settings.version)
    except Exception:  # noqa: BLE001
        logger.debug("Presence after founding failed", exc_info=True)
    kick()
    return result


async def adopt_account_network(*, device_name: Optional[str] = None) -> dict:
    """Leave this worker's own network and join the account's instead.

    The account always takes precedence: records already in the account's
    network win over local copies with the same id; rows only this worker
    holds are published once it is admitted. The sync loop calls this on its
    own when the hub reports a conflict; the API exposes it for a retry.
    """
    if _binding() is None:
        raise HubError("This worker is not signed in to a Shuffle account")
    info = await asyncio.to_thread(_call, "GET", "", None, max_bytes=1 << 20)
    if not info.get("network_id"):
        raise HubError("The account has no network to adopt")
    ctx = engine.context()
    if ctx is None or ctx.network_id == info["network_id"]:
        # Already switched (waiting to be admitted) or already in the account's network.
        return {"adopted": False, "status": _state.get("status")}
    try:
        await asyncio.to_thread(engine.leave_network)
    except RuntimeError as exc:
        raise HubError(str(exc)) from exc
    result = await asyncio.to_thread(register)
    _state["account_network"] = _account_network(info)
    kick()
    return {"adopted": True, "status": result.get("status")}


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
    await _bootstrap_from_hub(response, device_name=device_name)
    kick()
    return {"restored": True, "network": engine.require_context().summary()}


def _bootstrap_id() -> str:
    return "hub-bootstrap:" + identity_mod.load().device_id


async def _bootstrap_from_hub(response: Optional[dict] = None, *, device_name: Optional[str] = None) -> bool:
    """Persist hub provenance before enrollment, then safely retry the initial feed.

    Direct-invite snapshots have their own joiner records and must never be
    completed from a possibly partial hub feed. This separate local record also
    closes the crash window between installing network state and starting replay.
    Its admission response contains device-wrapped keys, never the recovery key.
    """
    global _bootstrap_lock
    if _bootstrap_lock is None:
        _bootstrap_lock = asyncio.Lock()
    async with _bootstrap_lock:
        binding = _binding()
        if binding is None:
            raise HubError("Sign in to the same Shuffle account to resume settings sync")
        binding_identity = {key: binding.get(key) for key in ("hub_url", "server_id")}
        marker = await asyncio.to_thread(store.get_join, _bootstrap_id())
        if marker is None:
            if response is None:
                return False
            if engine.context() is not None:
                raise HubError("This worker already belongs to a network")
            marker = await asyncio.to_thread(store.create_join, {
                "id": _bootstrap_id(), "role": "hub_bootstrap", "status": "syncing",
                "device_id": identity_mod.load().device_id,
                "name": device_name or engine._default_device_name(),
                "response": {"admission": response, "binding": binding_identity},
            })
        saved = marker.get("response") or {}
        if (marker.get("role") != "hub_bootstrap" or marker.get("device_id") != identity_mod.load().device_id
                or saved.get("binding") != binding_identity):
            raise HubError("Settings sync belongs to a different worker or account connection")
        admission = saved.get("admission") or {}
        ctx = engine.context()
        if ctx is None:
            ctx = await asyncio.to_thread(engine.enroll_from_response, admission, device_name=marker["name"])
        if ctx.network_id != admission.get("network_id") or ctx.device_id != marker["device_id"]:
            raise HubError("Settings sync does not match this worker's network")
        if ctx.bootstrap_state == engine.BOOTSTRAP_SYNCING:
            await asyncio.to_thread(register)
            probe = await asyncio.to_thread(_call, "GET", "feed?cursor=0&limit=1&protocol=2", max_bytes=FEED_MAX_BYTES)
            if (probe.get("object_sync") or {}).get("direct_read") and not (await asyncio.to_thread(_hub_state)).get("restored_checkpoint"):
                await asyncio.to_thread(checkpoints.restore, sys.modules[__name__], await asyncio.to_thread(_ensure_hub_scope))
            await asyncio.to_thread(_replay_feed_from_start)
            await asyncio.to_thread(engine.finish_bootstrap)
        elif ctx.bootstrap_state != engine.BOOTSTRAP_READY:
            raise HubError("Settings sync is not ready to resume")
        await asyncio.to_thread(register)
        await asyncio.to_thread(engine.publish_presence, version=settings.version)
        await asyncio.to_thread(store.delete_join, marker["id"])
        engine.kick()
        return True


def _replay_feed_from_start() -> None:
    scope = _ensure_hub_scope()
    while True:
        poll = {}
        pull_once(poll)
        if not poll.get("more"):
            break
    if object_sync.inbox_count(scope) or store.pending_holes():
        raise HubError("Cloud restoration is waiting for missing signed operations or network keys")
    _save_hub_state(_expected_scope=scope, registered=True, status="active")


# ---------------------------------------------------------------------------
# Push and pull
# ---------------------------------------------------------------------------

def _current_quota(value: dict | None) -> dict | None:
    if value is None:
        return None
    quota = dict(value)
    reset = quota.get("daily_upload_reset_at")
    if reset and time.time() * 1000 >= reset:
        quota["daily_upload_bytes"] = 0
    return quota


def _save_quota(quota: dict, scope: str) -> None:
    blocked = _hub_state().get("upload_blocked")
    fields = {"quota": quota}
    if blocked:
        previous = blocked.get("quota") or {}
        used, limit = {"ops": ("ops", "limit"), "stored_bytes": ("bytes", "bytes_limit"),
                       "daily_upload": ("daily_upload_bytes", "daily_upload_limit")}.get(blocked["reason"], (None, None))
        if (used and quota.get(limit) is not None and previous.get(limit) is not None
                and quota[limit] - quota.get(used, 0) > previous[limit] - previous.get(used, 0)):
            fields["upload_blocked"] = None
    _save_hub_state(_expected_scope=scope, **fields)


def _block_upload(result: dict, scope: str) -> None:
    quota = result.get("quota") or {}
    reason = result.get("quota_reason")
    if not reason:
        # Older hubs return only quota_exceeded. Retry conservatively without
        # assuming a failed response acknowledged any prefix of the batch.
        reason = "ops" if quota.get("limit") is not None and quota.get("ops", 0) >= quota["limit"] else "quota"
        if reason == "quota" and quota.get("bytes_limit") and quota.get("bytes", 0) >= quota["bytes_limit"]:
            reason = "stored_bytes"
        if reason == "quota" and quota.get("daily_upload_limit") and quota.get("daily_upload_bytes", 0) >= quota["daily_upload_limit"]:
            reason = "daily_upload"
    now = time.time()
    retry_at = now + 3600  # Re-probe permanent/legacy limits hourly, never every wake.
    if reason == "daily_upload":
        retry_at = (quota.get("daily_upload_reset_at") or (int(now // 86400) + 1) * 86400000) / 1000
    if reason == "concurrent_upload":
        retry_at = now + LOOP_IDLE_SECONDS
    _save_hub_state(_expected_scope=scope, quota=quota, upload_blocked={"reason": reason, "retry_at": retry_at, "quota": quota})
    raise UploadBlocked("Account upload quota exceeded; cloud backlog kept")


def push_once() -> int:
    """Send the next batch of our log to the hub. Returns how many went."""
    scope = _ensure_hub_scope()
    hub_state = _hub_state()
    blocked = hub_state.get("upload_blocked")
    if (hub_state.get("object_sync") or {}).get("writes") or object_sync._outbox(scope):
        if blocked and time.time() < blocked["retry_at"]:
            raise UploadBlocked("Account upload quota exceeded; cloud backlog kept")
        try:
            return object_sync.push(sys.modules[__name__], scope, hub_state)
        except object_sync.ObjectError as exc:
            raise HubError(str(exc)) from exc
    pushed = int(hub_state.get("pushed_ingest_id", 0) or 0)
    batch = store.hub_ops_after_ingest(pushed, scope, limit=PUSH_BATCH)
    if not batch:
        if hub_state.get("upload_blocked"):
            _save_hub_state(_expected_scope=scope, upload_blocked=None)
        return 0
    ops = [env for _, env in batch if env is not None]
    if ops:
        blocked = hub_state.get("upload_blocked")
        if blocked and time.time() < blocked["retry_at"]:
            raise UploadBlocked("Account upload quota exceeded; cloud backlog kept")
        result = _call("POST", "ops", {"ops": ops}, max_bytes=65536, timeout=30)
        if result.get("quota_exceeded"):
            _block_upload(result, scope)
        _save_hub_state(_expected_scope=scope, last_push_at=time.time(), quota=result.get("quota"), upload_blocked=None)
    _save_hub_state(_expected_scope=scope, pushed_ingest_id=batch[-1][0])
    return len(batch)


def pull_once(poll: dict | None = None) -> int:
    scope = _ensure_hub_scope()
    hub_state = _hub_state()
    cursor = int(hub_state.get("cursor", 0) or 0)
    suffix = f"feed?cursor={cursor}&limit={PULL_BATCH}&protocol=2" + ("&include_pending=1" if poll is not None else "")
    page = _call("GET", suffix, max_bytes=FEED_MAX_BYTES, timeout=60)
    if poll is not None:
        poll["more"] = bool(page.get("more"))
    if isinstance(page.get("object_sync"), dict):
        _state["object_mode"] = True
        if page["object_sync"] != hub_state.get("object_sync"):
            _save_hub_state(_expected_scope=scope, object_sync=page["object_sync"])
    if poll is not None and isinstance(page.get("pending_devices"), list):
        poll["devices"] = page["pending_devices"]
        _state["pending_devices"] = page["pending_devices"]
    if isinstance(page.get("quota"), dict):
        _save_quota(page["quota"], scope)
    ops = page.get("ops") or []
    applied = 0
    if "entries" in page:
        try:
            for entry in page["entries"]:
                applied += object_sync.pull_entry(sys.modules[__name__], scope, entry)
                _save_hub_state(_expected_scope=scope, cursor=int(entry["cursor"]), last_pull_at=time.time())
            applied += object_sync.drain_inbox(scope)
        except Exception as exc:
            raise HubError("Could not verify or persist cloud bundle; cursor retained") from exc
    elif ops and isinstance(page.get("object_sync"), dict):
        # The compatibility bridge still needs durable dependency staging when
        # direct signing is temporarily unavailable during a rollout/rollback.
        try:
            applied += object_sync.pull_entry(sys.modules[__name__], scope, {"cursor": page["cursor"], "ops": ops})
        except Exception as exc:
            raise HubError("Could not persist cloud operations; feed cursor kept") from exc
    elif ops:
        try:
            result = engine.apply_batch(ops, hub_scope=scope)
        except Exception as exc:
            raise HubError("Could not persist cloud operations; feed cursor kept") from exc
        applied = result.applied
        if applied:
            engine.kick()
    new_cursor = int(page.get("cursor", cursor))
    if new_cursor != cursor or ops:
        _save_hub_state(_expected_scope=scope, cursor=new_cursor, last_pull_at=time.time())
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
    try:
        if ctx is not None:
            await asyncio.to_thread(_ensure_hub_scope)
        hub_state = await asyncio.to_thread(_hub_state) if ctx else {}
        if await asyncio.to_thread(store.get_join, _bootstrap_id()):
            resumed = await _bootstrap_from_hub()
            _state["last_error"] = None
            return resumed
        if ctx is None:
            # Signed in but not enrolled: register, then either found the
            # account's network (nobody has yet) or wait to be admitted by an
            # online member.
            result = await asyncio.to_thread(register)
            if result.get("status") == "approved" or result.get("response"):
                response = result.get("response") or await asyncio.to_thread(_await_approval_sync)
                if response:
                    await _bootstrap_from_hub(response)
                    return True
            elif result.get("status") == "pending":
                info = await asyncio.to_thread(_summary_or_none)
                _state["account_network"] = _account_network(info)
                if info is not None and not info.get("network_id"):
                    await asyncio.to_thread(found_account_network)
                    _state["last_error"] = None
                    return True
                response = await asyncio.to_thread(_await_approval_sync)
                if response:
                    await _bootstrap_from_hub(response)
                    return True
            _state["last_error"] = None
            return False
        # Read the durable name, including renames made before this process
        # started. Remember it only after registration succeeds so outages retry.
        member = await asyncio.to_thread(store.get_member, ctx.device_id)
        name_changed = member is not None and hub_state.get("registered_name") != member["name"]
        if not hub_state.get("registered") or name_changed:
            result = await asyncio.to_thread(register)
            if result.get("status") == "conflict":
                # The account already syncs other workers: the account wins.
                await adopt_account_network()
                _state["last_error"] = None
                return True
        elif hub_state.get("status") == "conflict":
            await adopt_account_network()
            _state["last_error"] = None
            return True
        recovery_wrap = (await run_sync(store.get_state) or {}).get("recovery") or {}
        if _recovery_upload_due or (recovery_wrap.get("stale") and not recovery_wrap.get("hub_tombstoned")):
            await asyncio.to_thread(_upload_recovery)
        if ctx.bootstrap_state == engine.BOOTSTRAP_READY:
            sent = received = 0
            checkpoint_work = False
            errors = []
            poll = {}
            try:
                sent = await asyncio.to_thread(push_once)
            except HubError as exc:
                errors.append(str(exc))
            try:
                scope = hub_state.get("binding_scope", "")
                if hub_state.get("object_sync") and time.monotonic() < _object_poll_at.get(scope, 0):
                    poll["devices"] = []
                else:
                    received = await asyncio.to_thread(pull_once, poll)
                    _object_poll_at[scope] = time.monotonic() + (0 if poll.get("more") else 60)
            except HubError as exc:
                errors.append(str(exc))
            try:
                # Devices that signed in to this account are admitted here.
                admitted = await asyncio.to_thread(admit_pending_devices, poll.get("devices"))
                if admitted:
                    sent += await asyncio.to_thread(push_once)
            except HubError as exc:
                errors.append(str(exc))
            current = await asyncio.to_thread(_hub_state)
            scope = current.get("binding_scope", "")
            if ((current.get("object_sync") or {}).get("writes") and not current.get("upload_blocked")
                    and time.monotonic() >= _object_work_at.get(scope, 0)):
                _object_work_at[scope] = time.monotonic() + 60
                try:
                    checkpoint_work = await asyncio.to_thread(cloud_blobs.publish_once, sys.modules[__name__],
                                                              await asyncio.to_thread(_ensure_hub_scope))
                    if not checkpoint_work:
                        checkpoint_work = await asyncio.to_thread(checkpoints.publish_once, sys.modules[__name__],
                                                                  await asyncio.to_thread(_ensure_hub_scope))
                    if not checkpoint_work and current.get("checkpoint_id"):
                        checkpoint_work = await asyncio.to_thread(object_sync.archive_once, sys.modules[__name__],
                                                                  await asyncio.to_thread(_ensure_hub_scope))
                    if checkpoint_work:
                        _object_work_at[scope] = time.monotonic() + 5
                except (HubError, object_sync.ObjectError) as exc:
                    errors.append(str(exc))
            _state["last_error"] = "; ".join(dict.fromkeys(errors))[:200] or None
            return checkpoint_work or sent >= PUSH_BATCH or received >= PULL_BATCH
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
            active_wait = 5.0 if _state.get("object_mode") else LOOP_ACTIVE_SECONDS
            await asyncio.wait_for(_wake.wait(), timeout=active_wait if busy else LOOP_IDLE_SECONDS)
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
    global _recovery_upload_due, _bootstrap_lock
    _state.clear()
    _state["linked"] = False
    _recovery_upload_due = False
    _bootstrap_lock = None
    _object_poll_at.clear()
    _object_work_at.clear()
