"""Owner-only peer terminal transport over the authenticated control channel.

An enrolled worker is not itself owner authority. The recipient challenges an
opaque, target-bound lease back at the source, where the original UI credential
is rechecked. Neither credentials nor leases are returned to the UI or tools.
"""
from __future__ import annotations

import asyncio
import base64
import secrets
import time

from app.db.async_database import run_sync
from app.network import control, engine, membership

LEASE_SECONDS = 30
_owners = {}
_sessions = {}
_opening = set()


def require_peer(device_id):
    ctx = engine.context()
    member = ctx.member(device_id) if ctx else None
    if not member or member.kind != membership.KIND_WORKER or member.status != membership.STATUS_ACTIVE:
        raise PermissionError("Only active account workers may relay terminals")


def authorize(request, who, target):
    token = secrets.token_urlsafe(32)
    _owners[token] = (request, who, target)
    return token


def forget(token):
    _owners.pop(token, None)


async def handle_owner(body, from_device):
    from app.api.terminals import require_terminal_owner

    require_peer(from_device)
    entry = _owners.get(body.get("lease"))
    if not entry or entry[2] != from_device:
        return {"authorized": False}
    request, who, _ = entry
    try:
        allowed = await run_sync(require_terminal_owner, request) == who
    except Exception:
        allowed = False
    return {"authorized": allowed}


async def _expire(key, registry):
    try:
        while key in _sessions:
            await asyncio.sleep(LEASE_SECONDS)
            entry = _sessions.get(key)
            if entry and time.monotonic() - entry[2] >= LEASE_SECONDS:
                break
    finally:
        entry = _sessions.pop(key, None)
        if entry:
            terminal, viewer, _, _ = entry
            terminal.detach(viewer)
            from app.core.terminals import TerminalError
            try:
                await registry.close(terminal.id, by="peer-disconnected")
            except TerminalError:
                pass  # Already closed by the owner on the destination worker.


async def handle(body, from_device):
    from app.core import terminals as service

    require_peer(from_device)
    lease = body.get("lease")
    if not isinstance(lease, str) or not 20 <= len(lease) <= 100:
        raise PermissionError("Owner authorization required")
    proof = await control.call(from_device, "terminal-owner", {"lease": lease}, timeout=10)
    if proof.get("authorized") is not True:
        raise PermissionError("Terminal owner authorization expired")
    key = (from_device, lease)
    op = body.get("op")
    registry = service.registry
    if op == "open":
        if key in _sessions or key in _opening:
            raise ValueError("Terminal already opened")
        options = body.get("options") or {}
        # Session ids are local UI associations, never a remote cwd authority.
        _opening.add(key)
        try:
            terminal = await registry.create(created_by="peer-owner:" + from_device,
                cwd=options.get("cwd"), shell=options.get("shell"),
                cols=options.get("cols"), rows=options.get("rows"))
        except service.TerminalError as exc:
            return {"error": {"code": exc.code, "message": exc.message, "status": exc.status}}
        finally:
            _opening.discard(key)
        viewer, replay = terminal.attach()
        viewer.push_output(replay)
        task = asyncio.create_task(_expire(key, registry))
        _sessions[key] = [terminal, viewer, time.monotonic(), task]
        return {"terminal": terminal.to_dict()}
    entry = _sessions.get(key)
    if not entry:
        raise LookupError("Terminal relay expired")
    terminal, viewer, _, task = entry
    entry[2] = time.monotonic()
    if op == "close":
        _sessions.pop(key)
        task.cancel()
        terminal.detach(viewer)
        await registry.close(terminal.id, by="peer-owner:" + from_device)
        return {"closed": True}
    if op == "read":
        try:
            await asyncio.wait_for(viewer.event.wait(), 1)
        except asyncio.TimeoutError:
            pass
        if viewer.overflowed:
            raise ValueError("Terminal relay fell behind")
        output = b"".join(value for kind, value in viewer.drain() if kind == "output")
        return {"data": base64.b64encode(output).decode("ascii"), "terminal": terminal.to_dict()}
    if op == "write":
        encoded = body.get("data")
        if not isinstance(encoded, str) or len(encoded) > 400000:
            raise ValueError("Terminal input too large")
        await terminal.write(base64.b64decode(encoded, validate=True))
    elif op == "resize":
        terminal.resize(body.get("cols"), body.get("rows"))
    else:
        raise ValueError("Unknown terminal operation")
    return {"ok": True}
