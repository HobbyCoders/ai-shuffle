"""Owner-only peer terminals: HTTPS control plus a signature from a linked device.

Membership, the worker's network key and the source callback are not owner
proof. A PTY opens only for a signature from a key the owner linked on this
worker (``terminal_device_links``). The source worker relays and holds no key.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import secrets
import time

from app.db.async_database import run_sync
from app.db import terminal_device_links as link_store
from app.network import control, engine, membership, terminal_device_links as device_links

logger = logging.getLogger(__name__)
LEASE_SECONDS = 30
MAX_LIFETIME_SECONDS = 600
AUTH_RECHECK_SECONDS = 15
MAX_LEASES = 64
_owners = {}
_sessions = {}
_opening = set()


def require_peer(device_id):
    ctx = engine.context()
    member = ctx.member(device_id) if ctx else None
    if not member or member.kind != membership.KIND_WORKER or member.status != membership.STATUS_ACTIVE:
        raise PermissionError("Only active account workers may relay terminals")


def peer_targets_enabled():
    from app.core.config import settings
    return settings.network_peer_terminals


def availability():
    from app.core import terminals
    if not peer_targets_enabled():
        return {"available": False, "reason_code": "peer_terminals_disabled",
                "reason": "Turn on Terminals on other workers in Settings."}
    if not terminals.supported():
        return {"available": False, "reason_code": "not_supported", "reason": "No usable PTY or shell on this worker."}
    return {"available": True, "reason_code": None, "reason": None}


def require_encrypted_transport():
    if not control.inbound_https.get():
        raise PermissionError("Terminal control requires authenticated HTTPS")


def authorize(request, who, target):
    # Expired owner requests must not retain credentials in a long-lived process.
    now = time.monotonic()
    for token, entry in list(_owners.items()):
        if entry[3] <= now:
            forget(token)
    if len(_owners) >= MAX_LEASES:
        raise PermissionError("Too many terminal authorization requests")
    token = secrets.token_urlsafe(32)
    _owners[token] = (request, who, target, now + MAX_LIFETIME_SECONDS)
    return token


def forget(token):
    _owners.pop(token, None)


async def handle_owner(body, from_device):
    from app.api.terminals import require_terminal_owner
    require_encrypted_transport()
    require_peer(from_device)
    lease = body.get("lease")
    entry = _owners.get(lease)
    if not entry or entry[2] != from_device or time.monotonic() >= entry[3]:
        return {"authorized": False}
    if not peer_targets_enabled():
        forget(lease)
        return {"authorized": False}
    request, who, _, _ = entry
    try:
        allowed = await run_sync(require_terminal_owner, request) == who
    except Exception:
        allowed = False
    return {"authorized": allowed}


async def _close(key, reason):
    from app.core import terminals as service
    entry = _sessions.pop(key, None)
    if entry:
        terminal, viewer, _, task, _, _ = entry
        if task is not asyncio.current_task():
            task.cancel()
        terminal.detach(viewer)
        try:
            await service.registry.close(terminal.id, by=reason)
        except service.TerminalError:
            pass
        logger.info("peer terminal closed source=%s terminal=%s reason=%s", key[0], terminal.id, reason)


async def revoke_device(key_id):
    """Revoke the link, and close what it opened: authority ends now, not at the next check."""
    result = await device_links.revoke(key_id)
    for key, entry in list(_sessions.items()):
        if entry[5] == key_id:
            await _close(key, "device-link-revoked")
    return result


async def _require_source_owner(from_device, lease):
    """A second revocation check only. A malicious member saying yes grants nothing."""
    proof = await control.call(from_device, "terminal-owner", {"lease": lease}, timeout=10, secure=True)
    if proof.get("authorized") is not True:
        raise PermissionError("Source terminal owner authorization expired")


async def _expire(key):
    try:
        while key in _sessions:
            await asyncio.sleep(min(LEASE_SECONDS, AUTH_RECHECK_SECONDS))
            entry = _sessions.get(key)
            if not entry or time.monotonic() - entry[2] >= LEASE_SECONDS or time.monotonic() >= entry[4]:
                break
            try:
                require_peer(key[0])
                if not availability()["available"] or not await run_sync(link_store.get, entry[5]):
                    break
                await _require_source_owner(key[0], key[1])
            except Exception:
                break
    finally:
        await _close(key, "peer-expired-or-disconnected")


def _open_error(code, message, status, **extra):
    return {"error": {"code": code, "message": message, "status": status, **extra}}


async def _open(key, body, from_device):
    from fastapi import HTTPException
    from app.api.terminals import _validated_terminal_cwd
    from app.core import terminals as service
    if key in _sessions or key in _opening:
        raise PermissionError("Terminal lease already used; open a new terminal")
    state = availability()
    if not state["available"]:
        return _open_error(state["reason_code"], state["reason"], 409)
    registry = service.registry
    options = body.get("options") or {}
    _opening.add(key)
    terminal = None
    try:
        try:
            signed = await device_links.verify_open(body.get("proof"), from_device, engine.context().device_id)
        except device_links.Refused as exc:
            logger.info("peer terminal refused source=%s code=%s", from_device, exc.code)
            return _open_error(exc.code, exc.message, 403)
        target = signed["target"]
        try:
            cwd, cwd_root = await run_sync(_validated_terminal_cwd, target["cwd"])
        except HTTPException as exc:
            return _open_error("bad_folder", str(exc.detail), 400)
        except service.TerminalError as exc:
            return _open_error(exc.code, exc.message, exc.status)
        await _require_source_owner(from_device, key[1])
        terminal = await registry.create(created_by="peer-owner:" + from_device, session_id=target["session_id"],
                                         cwd=cwd, cwd_root=cwd_root, shell=target["shell"],
                                         cols=options.get("cols"), rows=options.get("rows"))
        # A revocation that landed while the shell spawned wins over the spawn.
        if not await run_sync(link_store.get, signed["key_id"]):
            raise PermissionError("Device link revoked during open")
    except BaseException as exc:
        if terminal:
            await registry.close(terminal.id, by="authorization-lost-during-open")
        if isinstance(exc, service.TerminalError):
            return _open_error(exc.code, exc.message, exc.status)
        raise
    finally:
        _opening.discard(key)
    await run_sync(link_store.touch, signed["key_id"])
    viewer, replay = terminal.attach()
    viewer.push_output(replay)
    task = asyncio.create_task(_expire(key))
    _sessions[key] = [terminal, viewer, time.monotonic(), task, time.monotonic() + MAX_LIFETIME_SECONDS, signed["key_id"]]
    logger.info("peer terminal opened source=%s terminal=%s action=%s key=%s", from_device, terminal.id,
                signed["action"], signed["key_id"][:12])
    return {"terminal": terminal.to_dict()}


async def _device_op(op, body, from_device):
    state = availability()
    if not state["available"]:
        return {"status": "unavailable", **state}
    if op == "challenge":
        return await device_links.issue_challenge(from_device, body.get("key_id"))
    member = engine.context().member(from_device)
    return await device_links.request_link(from_device, getattr(member, "name", from_device),
                                           body.get("public_key"), body.get("label"))


async def handle(body, from_device):
    from app.core import terminals as service
    require_encrypted_transport()
    require_peer(from_device)
    op = body.get("op")
    if op == "probe":
        return availability()
    if op in ("challenge", "link-request"):
        return await _device_op(op, body, from_device)
    lease = body.get("lease")
    if not isinstance(lease, str) or not 20 <= len(lease) <= 100:
        raise PermissionError("Owner authorization required")
    key = (from_device, lease)
    if op == "revoke":
        await _close(key, "source-owner-revoked")
        return {"revoked": True}
    if op == "open":
        return await _open(key, body, from_device)
    entry = _sessions.get(key)
    if not entry:
        raise LookupError("Terminal relay expired")
    if not availability()["available"]:
        await _close(key, "peer-terminals-disabled")
        raise PermissionError("Peer terminals are turned off")
    terminal, viewer, _, task, expires_at, _ = entry
    if time.monotonic() >= expires_at:
        await _close(key, "authorization-expired")
        raise PermissionError("Terminal lease expired")
    entry[2] = time.monotonic()
    if op == "close":
        await _close(key, "peer-owner-closed")
        return {"closed": True}
    if op == "read":
        try:
            await asyncio.wait_for(viewer.event.wait(), 1)
        except asyncio.TimeoutError:
            pass
        if _sessions.get(key) is not entry:  # Never return output after losing authority mid-poll.
            raise PermissionError("Terminal closed during read")
        if viewer.overflowed:
            raise ValueError("Terminal relay fell behind")
        output = b"".join(value for kind, value in viewer.drain() if kind == "output")
        return {"data": base64.b64encode(output).decode("ascii"), "terminal": terminal.to_dict()}
    if op == "write":
        encoded = body.get("data")
        if not isinstance(encoded, str) or len(encoded) > 400000:
            raise ValueError("Terminal input too large")
        await terminal.write(base64.b64decode(encoded, validate=True))
    elif op == "paste":
        # A paste is input to a shell this source already holds, with the same
        # authority as a write. It runs here because the rule for what may sit
        # at the prompt unrun needs the real PTY.
        if not isinstance(body.get("text"), str):
            raise ValueError("Paste text required")
        try:
            return {"mode": await service.registry.paste(terminal.id, body["text"])}
        except service.TerminalError as exc:
            return _open_error(exc.code, exc.message, exc.status)
    elif op == "resize":
        terminal.resize(body.get("cols"), body.get("rows"))
    else:
        raise ValueError("Unknown terminal operation")
    return {"ok": True}
