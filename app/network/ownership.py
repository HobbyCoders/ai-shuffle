"""Who runs a synced definition: automations and connectors have one owner.

A definition (an automation, a connector) is shared data; the work it does
is not. Only the device named in ``owner_device_id`` fires it or holds its
platform session. A definition that arrives on a worker with no owner, or
with an owner this worker is not, stays inactive here. Reassigning an owner
is an explicit action that rewrites the definition; the previous owner stops
on its next tick after seeing the new revision.

Outside a network (a worker that never joined one) these helpers fall back
to the legacy install-affinity rules the callers already had.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping, Optional, Tuple

from app.db.base import get_db
from app.network import engine

logger = logging.getLogger(__name__)

OWNED_TABLES = {"automations", "connectors"}


def enrolled() -> bool:
    return engine.context() is not None


def my_device_id() -> Optional[str]:
    ctx = engine.context()
    return ctx.device_id if ctx else None


def execution_eligible() -> bool:
    """Whether this enrolled worker may execute or claim network-owned work.

    Presence of a context is deliberately not enough: a worker that has been
    revoked retains it to decrypt history, but it must not run new work.
    """
    ctx = engine.context()
    if ctx is None or ctx.bootstrap_state != engine.BOOTSTRAP_READY:
        return False
    me = ctx.member(ctx.device_id)
    from app.network import crypto, keyring
    return bool(me and me.status == engine.membership.STATUS_ACTIVE and ctx.key_version in ctx.keyring
                and keyring.safe(ctx, crypto.sha256_hex(ctx.network_key)))


# Public alias for execution-boundary callers in services owned elsewhere.
can_execute_local = execution_eligible


def owner_of(row: Mapping[str, Any]) -> Optional[str]:
    return (row or {}).get("owner_device_id") or None


def owns(row: Mapping[str, Any]) -> bool:
    """True when this eligible device is the definition's owner."""
    me = my_device_id()
    return execution_eligible() and bool(me) and owner_of(row) == me


def is_unowned(row: Mapping[str, Any]) -> bool:
    return owner_of(row) is None


def _claimant(ctx: engine.NetworkContext) -> Optional[str]:
    """The one online, eligible worker that may claim unowned work: the lowest device id."""
    from app.network import transport
    online = set(transport.online_devices()) | {ctx.device_id}
    candidates = []
    for device_id in online:
        member = ctx.member(device_id)
        if member is not None and member.status == engine.membership.STATUS_ACTIVE \
                and member.kind == engine.membership.KIND_WORKER:
            candidates.append(device_id)
    return min(candidates) if candidates else None


def claim(table: str, row_id: str) -> bool:
    """Take ownership of an unowned definition.

    Deterministic across devices: only the online eligible worker with the
    lowest device id claims; the others wait for its revision to arrive.
    Inside one database only the first UPDATE wins.
    """
    me = my_device_id()
    if table not in OWNED_TABLES or not me or not execution_eligible():
        return False
    if _claimant(engine.require_context()) != me:
        return False
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(f"UPDATE {table} SET owner_device_id = %s, updated_at = NOW() "
                       f"WHERE id = %s AND owner_device_id IS NULL", (me, row_id))
        won = bool(cursor.rowcount)
    if won:
        engine.record_after_commit(table, row_id)
    return won


def assign(table: str, row_id: str, device_id: Optional[str]) -> dict:
    """Explicitly hand a definition to ``device_id`` (None = leave it unassigned/inactive)."""
    if table not in OWNED_TABLES:
        raise ValueError("Not an owned definition table")
    ctx = engine.require_context()
    if device_id is not None:
        member = ctx.member(device_id)
        if member is None or member.status != engine.membership.STATUS_ACTIVE:
            raise KeyError("Owner must be an active device")
        if member.kind != engine.membership.KIND_WORKER:
            raise ValueError("Only a worker device can own a definition")
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(f"UPDATE {table} SET owner_device_id = %s, updated_at = NOW() WHERE id = %s", (device_id, row_id))
        if not cursor.rowcount:
            raise LookupError("Unknown definition")
    engine.record_after_commit(table, row_id)
    return {"table": table, "id": row_id, "owner_device_id": device_id}


def decide_automation(automation: Mapping[str, Any]) -> Optional[Tuple[bool, str]]:
    """Network rule for a scheduled fire, or None to let the legacy election decide."""
    if not enrolled():
        return None
    if not execution_eligible():
        return False, "this device is not eligible to execute network work"
    owner = owner_of(automation)
    me = my_device_id()
    if owner == me:
        return True, "this device owns the automation"
    if owner is None:
        if claim("automations", automation["id"]):
            return True, "claimed the unowned automation"
        return False, "automation has no owner yet"
    return False, f"device {owner} owns the automation"


def decide_connector(row: Mapping[str, Any]) -> Optional[str]:
    """'start' | 'claim' | 'skip' for a connector row, or None for the legacy rule."""
    if not enrolled():
        return None
    if not execution_eligible():
        return "skip"
    owner = owner_of(row)
    if owner == my_device_id():
        return "start"
    if owner is None:
        return "claim"
    return "skip"
