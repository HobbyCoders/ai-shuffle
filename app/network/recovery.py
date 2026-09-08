"""The recovery key: unlock a network when every worker is off.

At creation (or later, from the Devices page) the network key is wrapped
under a key derived from a 128-bit recovery key. The wrap is public data:
it is stored locally in ``network_state.recovery``, synced to the hub with
the network, and useless without the recovery key itself, which is shown
once and never stored anywhere.

Unlock on a fresh worker: fetch the recovery wrap from the hub, unwrap the
network key, self-issue a recovery certificate (its proof is an AES-GCM tag
under the network key, so only a key holder could have made it), restore
the hub replica and start syncing. Members that come online later accept
the new device from that certificate.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.db import network as store
from app.network import crypto, engine, identity as identity_mod, membership

logger = logging.getLogger(__name__)


class RecoveryError(RuntimeError):
    pass


def status() -> dict:
    state = store.get_state()
    wrap = (state or {}).get("recovery") if state else None
    return {"configured": bool(wrap), "created_at": (wrap or {}).get("created_at"),
            "key_version": (wrap or {}).get("key_version")}


def create() -> dict:
    """Generate a recovery key, wrap the current network key under it, return the key once."""
    ctx = engine.require_context()
    raw = crypto.generate_recovery_key()
    text = crypto.format_recovery_key(raw)
    wrap = {
        "v": 1,
        "network_id": ctx.network_id,
        "key_version": ctx.key_version,
        "wrapped": crypto.wrap_key_with_recovery(raw, ctx.network_id, ctx.network_key),
        "check": crypto.recovery_key_check(raw, ctx.network_id),
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "created_by": ctx.device_id,
    }
    store.update_state(recovery=wrap)
    try:
        from app.network import hub
        hub.schedule_recovery_upload()
    except Exception:  # noqa: BLE001
        logger.debug("Hub recovery upload not scheduled", exc_info=True)
    return {"recovery_key": text, "created_at": wrap["created_at"], "key_version": ctx.key_version}


def refresh_wrap_after_rotation() -> None:
    """A rotated network key invalidates the wrap; a new recovery key must be created."""
    state = store.get_state()
    if state and state.get("recovery"):
        wrap = dict(state["recovery"])
        ctx = engine.context()
        if ctx and int(wrap.get("key_version", 0)) < ctx.key_version:
            wrap["stale"] = True
            store.update_state(recovery=wrap)


def unwrap(recovery_key_text: str, wrap: Dict[str, Any]) -> bytes:
    raw = crypto.parse_recovery_key(recovery_key_text)
    network_id = wrap.get("network_id") or ""
    if wrap.get("check") and crypto.recovery_key_check(raw, network_id) != wrap["check"]:
        raise RecoveryError("That recovery key does not belong to this network")
    try:
        return crypto.unwrap_key_with_recovery(raw, network_id, wrap["wrapped"])
    except (crypto.CryptoError, KeyError, TypeError) as exc:
        raise RecoveryError("Recovery key does not open this network") from exc


def response_from_wrap(recovery_key_text: str, wrap: Dict[str, Any], *, name: Optional[str],
                       founder_device_id: str, generation: int, membership_records: list,
                       keyring_wraps: Optional[Dict[str, Any]] = None, device_name: Optional[str] = None) -> dict:
    """Build a join-style response for a device that proved the recovery key."""
    ident = identity_mod.load()
    network_key = unwrap(recovery_key_text, wrap)
    network_id = wrap["network_id"]
    key_version = int(wrap.get("key_version", 1))
    certificate = membership.issue_recovery_certificate(
        network_id=network_id, network_key=network_key, signing_private=ident.signing_private,
        encryption_public=ident.encryption_public, name=device_name or engine._default_device_name(),
        kind=membership.KIND_WORKER, generation=generation + 1)
    keyring = {str(key_version): crypto.wrap_key(ident.encryption_public, network_key)}
    return {
        "network_id": network_id, "name": name, "founder_device_id": founder_device_id,
        "generation": generation + 1, "key_version": key_version, "keyring": keyring,
        "certificate": certificate, "membership_records": list(membership_records or []),
        "members": [], "legacy_instance_map": {}, "issuer": {},
    }


async def unlock_from_hub(recovery_key_text: str, *, device_name: Optional[str] = None) -> dict:
    """Restore this worker into the account's network using the hub replica."""
    from app.network import hub
    try:
        crypto.parse_recovery_key(recovery_key_text)
    except crypto.CryptoError as exc:
        raise RecoveryError(str(exc)) from exc
    try:
        return await hub.restore_with_recovery(recovery_key_text, device_name=device_name)
    except hub.HubError as exc:
        raise RecoveryError(str(exc)) from exc
