"""Account registration and admission; data travels directly between workers."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from app.core import account_hub
from app.core.config import settings
from app.db import mobile_hub as binding_store
from app.db import network as store
from app.db.async_database import run_sync
from app.network import crypto, engine, membership
from app.network import identity as identity_mod
from app.network.endpoints import validate_endpoints

logger = logging.getLogger(__name__)

LOOP_IDLE_SECONDS = 15.0
LOOP_ACTIVE_SECONDS = 1.0
APPROVAL_POLL_SECONDS = 5.0

_tasks: List[asyncio.Task] = []
_wake: Optional[asyncio.Event] = None
_loop: Optional[asyncio.AbstractEventLoop] = None
_state: Dict[str, Any] = {"linked": False}
_recovery_upload_due = False
_bootstrap_lock: Optional[asyncio.Lock] = None
_pass_lock: Optional[asyncio.Lock] = None
_directory_poll_at: dict[str, float] = {}
DIRECTORY_POLL_SECONDS = 240.0


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
    description = {
        "device_id": ident.device_id,
        "signing_public": crypto.b64u_encode(ident.signing_public),
        "encryption_public": crypto.b64u_encode(ident.encryption_public),
        "incarnation": ident.incarnation,
        "name": name or engine._default_device_name(),
        "platform": identity_mod.platform_name(),
        "kind": membership.KIND_WORKER,
        "version": settings.version,
    }
    # ``proof`` lets the admitting member check that the encryption key the
    # hub relays belongs to the device that owns the signing key; the hub
    # stores and returns the description as given.
    return membership.sign_device_description(description, ident.signing_private)


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
            current[key] = value
        store.update_state(_conn=conn, hub=current)
        return current


def _ensure_hub_scope() -> str:
    """Registration belongs to this account binding and network."""
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
                current = {"registered": False}

            current["binding_scope"] = scope
            store.update_state(_conn=conn, hub=current)
    return scope


def status() -> dict:
    binding = _binding()
    hub_state = _hub_state() if store.get_state() else {}
    return {
        "linked": binding is not None,
        "sync_mode": "peers",
        "server_id": binding.get("server_id") if binding else None,
        "registered": bool(hub_state.get("registered")),
        "status": hub_state.get("status") or _state.get("status"),
        "last_error": _state.get("last_error"),
        "pending_devices": _state.get("pending_devices") or [],
        "account_network": _state.get("account_network"),
        "adoption_required": bool(_state.get("adoption_required")),
        "revocations_due": list(hub_state.get("revocations_due") or []),
    }


def forget_network() -> None:
    """The worker left its network: drop what was learned about the account's."""
    for key in ("status", "pending_devices", "account_network", "last_error", "enrollment_poll", "adoption_required"):
        _state.pop(key, None)


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
                        network_hub_id=result.get("network_hub_id"))
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


def refresh_peer_endpoints() -> None:
    """Account URLs are routing hints, never a grant of network membership."""
    ctx = engine.context()
    if _binding() is None or ctx is None:
        return
    info = _call("GET", "", None, max_bytes=1 << 20)
    if info.get("network_id") != ctx.network_id:
        return
    for device in info.get("devices") or []:
        member = ctx.member(device.get("device_id"))
        if member is None or member.status != membership.STATUS_ACTIVE or member.device_id == ctx.device_id:
            continue
        endpoints = device.get("endpoints")
        if isinstance(endpoints, list) and endpoints and all(isinstance(url, str) for url in endpoints):
            existing = (store.get_member(member.device_id) or {}).get("endpoints") or []
            # Stored addresses follow the rule dialed ones do, and stay bounded.
            merged = validate_endpoints(endpoints + list(existing))
            if merged:
                store.touch_member(member.device_id, endpoints=merged)


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
    # The displayed fingerprint covers only the signing key. Even explicit
    # confirmation cannot authorize a hub-substituted encryption key.
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
    if not membership.verify_device_description(device):
        # Both unattended and manual admission must bind the encryption key
        # to the device's signing key before any network key is wrapped.
        raise HubError("Pending device carries no valid proof of its keys; update that worker and re-register")
    ctx = engine.context()
    existing = ctx.member(device_id) if ctx else None
    if existing is not None and existing.status == membership.STATUS_ACTIVE and (
            existing.signing_public != signing_public or existing.encryption_public != encryption_public):
        raise HubError("Device is already a member with different keys; remove it before admitting it again")
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


def notify_revoked(device_id: str) -> bool:
    """Tell the hub a member was removed so it stops relaying for that device.

    Best effort: when the hub cannot be reached the notice is queued in the
    hub state and retried by the sync loop.
    """
    if _binding() is None or store.get_state() is None:
        return False
    try:
        _call("POST", f"devices/{device_id}/reject", {"reason": "revoked"})
    except HubError as exc:
        logger.warning("Hub revocation notice for %s deferred: %s", device_id, exc)
        due = list(_hub_state().get("revocations_due") or [])
        if device_id not in due:
            due.append(device_id)
            _save_hub_state(revocations_due=due)
        return False
    due = [d for d in (_hub_state().get("revocations_due") or []) if d != device_id]
    if due != list(_hub_state().get("revocations_due") or []):
        _save_hub_state(revocations_due=due)
    return True


def _flush_revocations() -> None:
    for device_id in list(_hub_state().get("revocations_due") or []):
        notify_revoked(device_id)


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
    _state.pop("adoption_required", None)
    result = await asyncio.to_thread(register)
    _state["account_network"] = _account_network(info)
    kick()
    return {"adopted": True, "status": result.get("status")}


async def _adopt_or_wait() -> None:
    """Switch to the account's network on our own only when nothing is at stake.

    A worker that is alone in its network loses no shared state by leaving
    it. One that syncs with other workers would drop their membership, its
    conflicts list and its recovery wrap without anyone asking; that switch
    waits for an explicit *Adopt the account's network* from Settings >
    Devices (``POST /api/v1/network/hub/adopt``).
    """
    ctx = engine.context()
    others = [m for m in ctx.active_members() if m.device_id != ctx.device_id] if ctx else []
    if not others:
        _state.pop("adoption_required", None)
        await adopt_account_network()
        return
    if not _state.get("adoption_required"):
        logger.warning("The account already has a network; this worker syncs with %d other worker(s) and "
                       "keeps its own until the switch is confirmed from Settings > Devices", len(others))
    _state["adoption_required"] = True
    _state["status"] = "conflict"
def _bootstrap_id() -> str:
    return "hub-bootstrap:" + identity_mod.load().device_id


async def _bootstrap_from_hub(response: dict | None = None, *, device_name: str | None = None) -> bool:
    """Persist account admission and resume a verified peer snapshot after restart."""
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
            if not (await run_sync(_hub_state)).get("registered"):
                await asyncio.to_thread(register)
            await _bootstrap_from_peers(admission)
            await asyncio.to_thread(engine.finish_bootstrap)
        elif ctx.bootstrap_state != engine.BOOTSTRAP_READY:
            raise HubError("Settings sync is not ready to resume")
        await asyncio.to_thread(register)
        await asyncio.to_thread(engine.publish_presence, version=settings.version)
        await asyncio.to_thread(store.delete_join, marker["id"])
        engine.kick()
        return True



async def _loop_body() -> bool:
    global _pass_lock
    if _pass_lock is None:
        _pass_lock = asyncio.Lock()
    async with _pass_lock:
        return await _account_pass()


async def _account_pass() -> bool:
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
            binding_key = tuple((_binding() or {}).get(k) for k in ("hub_url", "server_id"))
            pending = _state.get("enrollment_poll")
            if pending and pending["binding"] == binding_key:
                if time.monotonic() < pending["next"]:
                    return False
                result = pending["result"]
                pending["delay"] = min(DIRECTORY_POLL_SECONDS, pending["delay"] * 2)
                pending["next"] = time.monotonic() + pending["delay"]
            else:
                result = await asyncio.to_thread(register)
                _state["enrollment_poll"] = {"binding": binding_key, "result": result,
                                                 "delay": 15, "next": time.monotonic() + 15}
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
        if hub_state.get("revocations_due"):
            await asyncio.to_thread(_flush_revocations)
        if not hub_state.get("registered") or name_changed:
            result = await asyncio.to_thread(register)
            if result.get("status") == "conflict":
                # The account already syncs other workers: the account wins.
                await _adopt_or_wait()
                _state["last_error"] = None
                return True
        elif hub_state.get("status") == "conflict":
            await _adopt_or_wait()
            _state["last_error"] = None
            return True
        recovery_wrap = (await run_sync(store.get_state) or {}).get("recovery") or {}
        if _recovery_upload_due or (recovery_wrap.get("stale") and not recovery_wrap.get("hub_tombstoned")):
            await asyncio.to_thread(_upload_recovery)
        if ctx.bootstrap_state == engine.BOOTSTRAP_READY:
            scope = hub_state.get("binding_scope", "")
            if time.monotonic() >= _directory_poll_at.get(scope, 0):
                _directory_poll_at[scope] = time.monotonic() + DIRECTORY_POLL_SECONDS
                await asyncio.to_thread(admit_pending_devices)
                await asyncio.to_thread(refresh_peer_endpoints)
            _state["last_error"] = None
            return False

    except HubError as exc:
        _state["last_error"] = str(exc)[:200]
        logger.debug("Hub sync: %s", exc)
    except Exception:
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
    global _recovery_upload_due, _bootstrap_lock, _pass_lock
    _state.clear()
    _state["linked"] = False
    _recovery_upload_due = False
    _bootstrap_lock = None
    _pass_lock = None
    _directory_poll_at.clear()


async def _bootstrap_from_peers(admission: dict) -> None:
    import httpx

    from app.network import transport
    ctx = engine.require_context()
    issuer = admission.get("issuer") or {}
    candidates = {issuer.get("device_id"): issuer.get("endpoints") or []}
    for member in await run_sync(store.list_members):
        if member["device_id"] != ctx.device_id and member["status"] == "active":
            candidates.setdefault(member["device_id"], member.get("endpoints") or [])
    async with httpx.AsyncClient(follow_redirects=False) as client:
        for device_id, endpoints in candidates.items():
            if not device_id or not endpoints:
                continue
            try:
                link = transport.PeerLink(device_id, endpoints)
                await link.receive_state(client, ctx)
                return
            except (httpx.HTTPError, transport.PeerError, ValueError, engine.ApplyError):
                continue
    raise HubError("Waiting for another worker to come online to copy your data")
