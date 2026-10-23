"""Owner-only peer terminals: HTTPS control plus per-use approval on the recipient.

Membership and the source callback are not owner proof. Only a locally
owner-authenticated approval on the receiving worker can authorize a PTY.
Approvals are memory-only, single-use, target-bound and independently rechecked.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import secrets
import time

from app.db.async_database import run_sync
from app.network import control, engine, membership, transport

logger = logging.getLogger(__name__)
LEASE_SECONDS = 30
MAX_LIFETIME_SECONDS = 600
PENDING_SECONDS = 120
REQUEST_CALLBACK_SECONDS = 3
AUTH_RECHECK_SECONDS = 15
MAX_APPROVALS = 64
_owners = {}
_sessions = {}
_opening = set()
_approvals = {}


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
                "reason": "Turn on peer terminals in owner policy on both workers."}
    if not terminals.supported():
        return {"available": False, "reason_code": "not_supported", "reason": "No usable PTY or shell on this worker."}
    return {"available": False, "reason_code": "recipient_approval_required",
            "reason": "The owner must approve this terminal directly on the receiving worker."}


async def require_recipient_owner(key):
    from app.api.terminals import require_terminal_owner
    approval = _approvals.get(key)
    if (availability()["reason_code"] != "recipient_approval_required" or not approval
            or approval["request"] is None or time.monotonic() >= approval["expires"]):
        raise PermissionError("Independent recipient owner approval required or expired")
    try:
        allowed = await run_sync(require_terminal_owner, approval["request"]) == approval["who"]
    except Exception:
        allowed = False
    if not allowed or _approvals.get(key) is not approval:
        raise PermissionError("Independent recipient owner approval expired or revoked")
    return approval


def require_encrypted_transport():
    if not control.inbound_https.get():
        raise PermissionError("Terminal control requires authenticated HTTPS")


def authorize(request, who, target):
    # Bound the source store as well as recipient requests. Expired owner
    # requests must not retain credentials indefinitely in a long-lived process.
    now = time.monotonic()
    for token, entry in list(_owners.items()):
        if entry[3] <= now:
            forget(token)
    if len(_owners) >= MAX_APPROVALS:
        raise PermissionError("Too many terminal authorization requests")
    token = secrets.token_urlsafe(32)
    _owners[token] = (request, who, target, now + MAX_LIFETIME_SECONDS)
    return token


def forget(token):
    _owners.pop(token, None)


def source_authorization(token, who, target=None):
    entry = _owners.get(token)
    if not entry or entry[1] != who or time.monotonic() >= entry[3] or (target and entry[2] != target):
        raise PermissionError("Source owner terminal authorization expired or invalid")
    return entry


async def handle_owner(body, from_device):
    from app.api.terminals import require_terminal_owner
    require_encrypted_transport()
    require_peer(from_device)
    entry = _owners.get(body.get("lease"))
    if not entry or entry[2] != from_device or time.monotonic() >= entry[3]:
        return {"authorized": False}
    request, who, _, _ = entry
    try:
        allowed = await run_sync(require_terminal_owner, request) == who
    except Exception:
        allowed = False
    return {"authorized": allowed}


def _whole_seconds(seconds):
    """Whole seconds, after dropping float noise: ``(t + 120) - t`` is
    119.99999999999997 for some monotonic ``t`` (199.476812733 on a fresh CI
    runner), and a bare ``int`` read that as 119."""
    return max(0, int(round(seconds, 6)))


def _public_approval(key, approval):
    return {"request_id": approval["id"], "worker_id": key[0],
            "confirmation_code": approval["confirmation_code"], "source_name": approval["source_name"],
            "age_seconds": _whole_seconds(time.monotonic() - approval["created"]),
            "status": "approved" if approval["request"] is not None else "pending", "used": approval["used"],
            "expires_in": _whole_seconds(approval["expires"] - time.monotonic())}


def recipient_requests():
    # With the owner policy off nothing here can be approved, so list nothing.
    if not peer_targets_enabled():
        return []
    now = time.monotonic()
    return [_public_approval(key, value) for key, value in _approvals.items() if value["expires"] > now]


def _request_key(request_id):
    for key, approval in _approvals.items():
        if approval["id"] == request_id:
            return key
    raise PermissionError("Recipient terminal request expired or unavailable")


async def approve_request(request_id, request, who):
    from app.api.terminals import require_terminal_owner
    # Defence in depth: do not rely solely on a route dependency for this gate.
    if await run_sync(require_terminal_owner, request) != who:
        raise PermissionError("Recipient owner authentication required")
    key = _request_key(request_id)
    require_peer(key[0])
    approval = _approvals[key]
    if availability()["reason_code"] != "recipient_approval_required" or approval["expires"] <= time.monotonic():
        raise PermissionError("Recipient terminal request expired or disabled")
    if approval["request"] is not None:
        raise PermissionError("Recipient terminal request already approved; request a new terminal")
    approval.update(request=request, who=who, expires=time.monotonic() + MAX_LIFETIME_SECONDS)
    logger.info("peer terminal approved source=%s request=%s by=%s", key[0], request_id, who)
    return _public_approval(key, approval)


async def _close(key, reason):
    from app.core import terminals as service
    approval = _approvals.pop(key, None)
    entry = _sessions.pop(key, None)
    if entry:
        terminal, viewer, _, task, _ = entry
        if task is not asyncio.current_task():
            task.cancel()
        terminal.detach(viewer)
        try:
            await service.registry.close(terminal.id, by=reason)
        except service.TerminalError:
            pass
        logger.info("peer terminal closed source=%s terminal=%s reason=%s", key[0], terminal.id, reason)
    elif approval:
        logger.info("peer terminal authorization ended source=%s request=%s reason=%s", key[0], approval["id"], reason)


async def revoke_request(request_id):
    await _close(_request_key(request_id), "recipient-owner-revoked")
    return {"revoked": True}


async def _expire(key):
    try:
        while key in _sessions:
            await asyncio.sleep(min(LEASE_SECONDS, AUTH_RECHECK_SECONDS))
            entry = _sessions.get(key)
            if not entry or time.monotonic() - entry[2] >= LEASE_SECONDS or time.monotonic() >= entry[4]:
                break
            try:
                require_peer(key[0])
                await require_recipient_owner(key)
                proof = await control.call(key[0], "terminal-owner", {"lease": key[1]}, timeout=10, secure=True)
                if proof.get("authorized") is not True:
                    break
            except Exception:
                break
    finally:
        await _close(key, "peer-expired-or-disconnected")


async def handle(body, from_device):
    from app.core import terminals as service
    require_encrypted_transport()
    require_peer(from_device)
    op = body.get("op")
    if op == "probe":
        return availability()
    lease = body.get("lease")
    if not isinstance(lease, str) or not 20 <= len(lease) <= 100:
        raise PermissionError("Owner authorization required")
    key = (from_device, lease)
    if op == "revoke":
        await _close(key, "source-owner-revoked")
        return {"revoked": True}
    if op == "request":
        state = availability()
        if state["reason_code"] != "recipient_approval_required":
            return {"status": "unavailable", **state}
        capped = transport.connect_timeout.set(1)
        try:
            proof = await asyncio.wait_for(control.call(from_device, "terminal-owner", {"lease": lease},
                                                       timeout=REQUEST_CALLBACK_SECONDS, secure=True), REQUEST_CALLBACK_SECONDS)
        finally:
            transport.connect_timeout.reset(capped)
        if proof.get("authorized") is not True:
            logger.info("peer terminal request refused source=%s", from_device)
            raise PermissionError("Source terminal owner authorization required")
        require_peer(from_device)
        # Expired requests cannot accumulate or preserve a revoked credential.
        for stale, value in list(_approvals.items()):
            if value["expires"] <= time.monotonic():
                await _close(stale, "authorization-expired")
        if key not in _approvals:
            if len(_approvals) >= MAX_APPROVALS or sum(k[0] == from_device for k in _approvals) >= 4:
                raise PermissionError("Too many pending recipient terminal requests")
            created = time.monotonic()
            member = engine.context().member(from_device)
            _approvals[key] = {"id": secrets.token_urlsafe(16), "request": None, "who": None,
                               "created": created, "expires": created + PENDING_SECONDS, "used": False,
                               "source_name": getattr(member, "name", from_device),
                               "confirmation_code": "".join(secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567") for _ in range(6))}
            logger.info("peer terminal requested source=%s request=%s", from_device, _approvals[key]["id"])
        return _public_approval(key, _approvals[key])
    if op == "status":
        approval = _approvals.get(key)
        if not approval or approval["expires"] <= time.monotonic():
            await _close(key, "authorization-expired")
            return {"status": "expired", "reason_code": "authorization_expired"}
        if approval["request"] is not None:
            try:
                await require_recipient_owner(key)
            except PermissionError:
                await _close(key, "recipient-owner-revoked")
                return {"status": "expired", "reason_code": "authorization_expired"}
        return _public_approval(key, approval)
    # The source's own callback is only a second revocation check. It cannot
    # create recipient authority, including when a malicious member says yes.
    try:
        approval = await require_recipient_owner(key)
        proof = await control.call(from_device, "terminal-owner", {"lease": lease}, timeout=10, secure=True)
        if proof.get("authorized") is not True:
            raise PermissionError("Source terminal owner authorization expired")
    except Exception:
        logger.info("peer terminal refused source=%s operation=%s", from_device,
                    op if op in {"open", "read", "write", "resize", "close"} else "unknown")
        current = _approvals.get(key)
        if key in _sessions or (current and current["request"] is not None):
            await _close(key, "owner-authorization-refused")
        raise
    registry = service.registry
    if op == "open":
        if key in _sessions or key in _opening or approval["used"]:
            raise PermissionError("Recipient approval already used; request a new terminal")
        options = body.get("options") or {}
        _opening.add(key)
        terminal = None
        try:
            from fastapi import HTTPException
            from app.api.terminals import _validated_terminal_cwd
            try:
                cwd, cwd_root = await run_sync(_validated_terminal_cwd, options.get("cwd"))
            except HTTPException as exc:
                return {"error": {"code": "bad_folder", "message": str(exc.detail), "status": 400}}
            except service.TerminalError as exc:
                return {"error": {"code": exc.code, "message": exc.message, "status": exc.status}}
            if await require_recipient_owner(key) is not approval:
                raise PermissionError("Recipient approval replaced during folder validation")
            approval["used"] = True
            terminal = await registry.create(created_by="peer-owner:" + from_device,
                cwd=cwd, cwd_root=cwd_root, shell=options.get("shell"), cols=options.get("cols"), rows=options.get("rows"))
            # A replacement approval for the same source lease is new consent,
            # not permission to finish a spawn authorized by a revoked request.
            if await require_recipient_owner(key) is not approval:
                raise PermissionError("Recipient approval replaced during open")
        except BaseException as exc:
            if terminal:
                await registry.close(terminal.id, by="authorization-lost-during-open")
            await _close(key, "open-failed")
            if isinstance(exc, service.TerminalError):
                return {"error": {"code": exc.code, "message": exc.message, "status": exc.status}}
            raise
        finally:
            _opening.discard(key)
        viewer, replay = terminal.attach()
        viewer.push_output(replay)
        task = asyncio.create_task(_expire(key))
        _sessions[key] = [terminal, viewer, time.monotonic(), task, approval["expires"]]
        logger.info("peer terminal opened source=%s terminal=%s", from_device, terminal.id)
        return {"terminal": terminal.to_dict()}
    entry = _sessions.get(key)
    if not entry:
        raise LookupError("Terminal relay expired")
    terminal, viewer, _, task, expires_at = entry
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
        await require_recipient_owner(key)  # Never return output after a long-poll authorization loss.
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
