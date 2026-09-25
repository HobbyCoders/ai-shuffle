"""The directory of network runners: what replaces the ``installs`` table.

A runner is an active worker member. Its reachability comes from the
presence record it published and from the peers that recently answered us;
its capabilities come from the same presence record. The shape mirrors the
old ``installs`` rows so the placement resolver and the composer's runner
picker keep working unchanged.
"""
from __future__ import annotations

from typing import Dict, Optional

from app.core.config import settings
from app.db import network as store
from app.db.base import utc_now_iso as _now_iso
from app.network import engine, membership, transport


def directory() -> dict:
    """``{runners: [...], self_device_id, network_id}`` for UIs and placement."""
    ctx = engine.context()
    if ctx is None:
        return {"runners": [], "self_device_id": None, "network_id": None, "enrolled": False}
    presence = engine.all_presence()
    online = transport.online_devices()
    rows = []
    for member in store.list_members(include_revoked=False):
        if member["kind"] != membership.KIND_WORKER:
            continue
        device_id = member["device_id"]
        seen = presence.get(device_id) or {}
        is_self = device_id == ctx.device_id
        endpoints = list(seen.get("endpoints") or member.get("endpoints") or [])
        rows.append({
            "id": device_id,
            "device_id": device_id,
            "device_name": member["name"],
            "base_url": endpoints[0] if endpoints else None,
            "endpoints": endpoints,
            "platform": seen.get("platform") or member.get("platform"),
            "version": seen.get("version") or member.get("version"),
            "capabilities": seen.get("capabilities") or member.get("capabilities") or {},
            "approved": True,
            "approved_at": member.get("created_at"),
            "revoked_at": None,
            "last_seen": _now_iso() if is_self else member.get("last_seen"),
            "online": is_self or device_id in online,
            "is_self": is_self,
            "legacy_instance_id": member.get("legacy_instance_id"),
        })
    rows.sort(key=lambda r: (not r["is_self"], r["device_name"].lower(), r["id"]))
    return {"runners": rows, "self_device_id": ctx.device_id, "network_id": ctx.network_id, "enrolled": True}


def installs_map() -> Dict[str, dict]:
    """The ``installs_map`` shape ``placement.resolve_placement`` consumes.

    Keys are device ids. The legacy instance id of a member is also mapped
    so a ``runner:<instance_id>`` placement written before migration still
    resolves to the right device.
    """
    out: Dict[str, dict] = {}
    listing = directory()
    for row in listing["runners"]:
        out[row["id"]] = row
        legacy = row.get("legacy_instance_id")
        if legacy and legacy not in out:
            out[legacy] = dict(row, id=legacy)
    return out


def self_device_id() -> Optional[str]:
    ctx = engine.context()
    return ctx.device_id if ctx else None


def device_for_instance(instance_id: Optional[str]) -> Optional[str]:
    """Map a legacy instance id to a device id when the network knows it."""
    if not instance_id:
        return None
    ctx = engine.context()
    if ctx is None:
        return None
    if instance_id == settings.effective_instance_id:
        return ctx.device_id
    return ctx.legacy_instance_map.get(instance_id)


def resolve_runner_id(runner_id: Optional[str]) -> Optional[str]:
    """Accept either a device id or a legacy instance id; return the device id."""
    if not runner_id:
        return None
    ctx = engine.context()
    if ctx is None:
        return runner_id
    if runner_id == ctx.device_id or ctx.member(runner_id) is not None:
        return runner_id
    return device_for_instance(runner_id) or runner_id


def find(device_id: str) -> Optional[dict]:
    for row in directory()["runners"]:
        if row["id"] == device_id:
            return row
    return None
