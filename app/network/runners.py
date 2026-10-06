"""The directory of network runners: what replaces the ``installs`` table.

A runner is an active worker member. Its reachability comes from the
presence record it published and from the peers that recently answered us;
its capabilities come from the same presence record. The shape mirrors the
old ``installs`` rows so the placement resolver and the composer's runner
picker keep working unchanged.
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

from app.core.config import settings
from app.db import network as store
from app.db.base import utc_now_iso as _now_iso
from app.network import engine, membership, transport
from app.network.endpoints import peer_endpoints

logger = logging.getLogger(__name__)


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
        # The addresses this worker dials it at (the tunnel alone when it has one).
        endpoints = peer_endpoints(member, seen)
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
    resolves to the right device. This install's own instance id always maps
    to its own row, even when its member row records no alias or an older
    one (#1716), as ``device_for_instance`` does.
    """
    out: Dict[str, dict] = {}
    listing = directory()
    for row in listing["runners"]:
        out[row["id"]] = row
        legacy = row.get("legacy_instance_id")
        if legacy and legacy not in out:
            out[legacy] = dict(row, id=legacy)
    own_instance = settings.effective_instance_id
    own = next((row for row in listing["runners"] if row.get("is_self")), None)
    if own is not None and own_instance and own_instance != own["id"]:
        out[own_instance] = dict(own, id=own_instance)
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


def _active_worker(ctx, device_id: str) -> bool:
    member = ctx.member(device_id)
    return (member is not None and member.kind == membership.KIND_WORKER
            and member.status == membership.STATUS_ACTIVE)


def resolve_worker(value, *, instead: str, refused: str, listing: Optional[dict] = None) -> dict:
    """The directory row of the worker ``value`` names. Raises ``ValueError``.

    One resolver for every tool that names a worker (StartSession,
    HandOffSession, Task and Swarm ``worker``). ``value`` is an exact device
    id, else a case-insensitive worker name. The row of THIS worker is
    returned too (``is_self``); each caller decides what naming itself means.
    Any other pick must be an online, active worker member of this account.
    Everything else is refused by name with the choices listed; a bad pick is
    never replaced by this worker.

    ``instead`` and ``refused`` finish the refusals, e.g. "omit worker to
    start here" and "nothing was started". ``listing`` reuses a directory the
    caller already read.
    """
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError("worker must be a worker name or device id of at most 200 characters")
    wanted = value.strip()
    ctx = engine.context()
    if listing is None:
        if ctx is None:
            raise ValueError(f"This worker is not part of a worker network; {instead}.")
        listing = directory()
    rows = listing.get("runners") or []
    matches = [row for row in rows if row["device_id"] == wanted]
    if not matches:
        matches = [row for row in rows if (row.get("device_name") or "").casefold() == wanted.casefold()]
    names = ", ".join(f"{row['device_name']} ({'this worker' if row.get('is_self') else 'online' if row.get('online') else 'offline'})"
                      for row in rows) or "none"
    if not matches:
        raise ValueError(f"Unknown worker {wanted!r}. Workers on this account: {names}.")
    if len(matches) > 1:
        raise ValueError(f"More than one worker is named {wanted!r}; pass its device id instead.")
    row = matches[0]
    if row.get("is_self"):
        return row
    if ctx is not None and not _active_worker(ctx, row["device_id"]):
        raise ValueError(f"{row['device_name']} is not an active worker of this account.")
    if not row.get("online"):
        raise ValueError(f"{row['device_name']} is offline; {refused}. Ask the user to bring it "
                         f"online, or {instead}.")
    return row


def other_workers() -> list:
    """The directory rows of this account's other active workers, online or not.

    Empty when this worker is not networked. Never raises: a tool schema asks
    this on every build and must not break over a directory read.
    """
    try:
        ctx = engine.context()
        if ctx is None:
            return []
        return [row for row in directory()["runners"]
                if not row["is_self"] and _active_worker(ctx, row["device_id"])]
    except Exception:  # noqa: BLE001 — no peers is the safe answer
        logger.debug("Could not list the other workers", exc_info=True)
        return []


def find(device_id: str) -> Optional[dict]:
    for row in directory()["runners"]:
        if row["id"] == device_id:
            return row
    return None
