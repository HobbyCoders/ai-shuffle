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


def owner_of(row: Mapping[str, Any]) -> Optional[str]:
    return (row or {}).get("owner_device_id") or None


def owns(row: Mapping[str, Any]) -> bool:
    """True when this device is the definition's owner."""
    me = my_device_id()
    return bool(me) and owner_of(row) == me


def is_unowned(row: Mapping[str, Any]) -> bool:
    return owner_of(row) is None


def claim(table: str, row_id: str) -> bool:
    """Take ownership of an unowned definition (race-safe: only the first UPDATE wins)."""
    me = my_device_id()
    if table not in OWNED_TABLES or not me:
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
    if device_id is not None and ctx.member(device_id) is None:
        raise KeyError("Unknown device")
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
    owner = owner_of(row)
    if owner == my_device_id():
        return "start"
    if owner is None:
        return "claim"
    return "skip"
