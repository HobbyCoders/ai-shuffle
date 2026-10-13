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
from typing import Any, Dict

from app.db import network as store
from app.db.base import get_db
from app.network import crypto, engine

logger = logging.getLogger(__name__)


class RecoveryError(RuntimeError):
    pass


def status() -> dict:
    state = store.get_state()
    wrap = (state or {}).get("recovery") if state else None
    stale = bool((wrap or {}).get("stale"))
    return {"configured": bool(wrap) and not stale, "stale": stale,
            "created_at": (wrap or {}).get("created_at"),
            "key_version": (wrap or {}).get("key_version")}


def create() -> dict:
    """Generate a recovery key from the current locked security frontier.

    The state row lock prevents a recovery certificate from racing a local
    rotation and accidentally wrapping a predecessor key after it was revoked.
    """
    raw = crypto.generate_recovery_key()
    text = crypto.format_recovery_key(raw)
    with get_db() as conn:
        state = store.lock_state(conn.cursor())
        if state is None:
            raise RecoveryError("This worker has not joined a network")
        ctx = engine.context()
        if ctx is None or ctx.network_id != state["network_id"] or ctx.key_version != int(state["key_version"]):
            ctx = engine.load_context(conn)
        if ctx is None or ctx.key_version not in ctx.keyring:
            raise RecoveryError("The current network key is unavailable on this device")
        wrap = {
            "v": 1,
            "network_id": ctx.network_id,
            "key_version": ctx.key_version,
            "wrapped": crypto.wrap_key_with_recovery(raw, ctx.network_id, ctx.network_key),
            "check": crypto.recovery_key_check(raw, ctx.network_id),
            "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "created_by": ctx.device_id,
        }
        store.update_state(_conn=conn, recovery=wrap)
    try:
        from app.network import hub
        hub.schedule_recovery_upload()
    except Exception:  # noqa: BLE001
        logger.debug("Hub recovery upload not scheduled", exc_info=True)
    return {"recovery_key": text, "created_at": wrap["created_at"], "key_version": ctx.key_version}


def refresh_wrap_after_rotation(*, _conn=None) -> bool:
    """Durably invalidate a recovery wrap after any key-security frontier change.

    A retained old key is useful only for decrypting historical data; it must
    never remain an authority for a new recovery certificate.  ``_conn`` lets
    callers mark the wrap stale in the same state transaction as a rotation.
    """
    state = store.get_state(_conn)
    if not state or not state.get("recovery"):
        return False
    wrap = dict(state["recovery"])
    if wrap.get("stale"):
        return False
    wrap["stale"] = True
    store.update_state(_conn=_conn, recovery=wrap)
    try:
        from app.network import hub
        hub.schedule_recovery_upload()
    except Exception:  # noqa: BLE001
        logger.debug("Hub recovery tombstone not scheduled", exc_info=True)
    return True


def unwrap(recovery_key_text: str, wrap: Dict[str, Any]) -> bytes:
    if not isinstance(wrap, dict) or wrap.get("stale"):
        raise RecoveryError("This recovery key is stale after a network rotation")
    raw = crypto.parse_recovery_key(recovery_key_text)
    network_id = wrap.get("network_id") or ""
    if wrap.get("check") and crypto.recovery_key_check(raw, network_id) != wrap["check"]:
        raise RecoveryError("That recovery key does not belong to this network")
    try:
        return crypto.unwrap_key_with_recovery(raw, network_id, wrap["wrapped"])
    except (crypto.CryptoError, KeyError, TypeError) as exc:
        raise RecoveryError("Recovery key does not open this network") from exc
