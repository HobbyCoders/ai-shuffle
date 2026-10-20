"""Private access for a session that runs on another device.

Grants, pending requests, one-shot approvals and saved connections live only
in the owning worker's broker (``app.core.chat_access``). A worker showing a
mirrored session forwards each access-panel call to the owner over the
authenticated control channel. Only a bounded, short-lived panel snapshot is
cached on the viewer; no usable grant or credential is copied to it. Private
inputs typed on the viewing device pass through in memory and travel to the
owner only over HTTPS; the owner refuses them on any other transport.

The owner remembers which devices asked about a session and tells them when
its access state changes, so their panels refresh without polling.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, Optional

from app.db.async_database import run_sync
from app.network import control

logger = logging.getLogger(__name__)

OPERATIONS = frozenset({"snapshot", "grant", "decline", "revoke", "decide", "forget", "edit"})
# An SSH grant probes several host-key types before signing in.
GRANT_TIMEOUT = 120.0
INSECURE_INPUTS = ("Private inputs reach the device that runs this conversation only over HTTPS. "
                   "Choose a saved connection, or enter them on that device.")

# What a viewer shows while the owner cannot answer: nothing to act on here.
EMPTY_SNAPSHOT: Dict[str, Any] = {
    "saved_connections": [], "vault_available": False, "grants": [], "actions": [],
    "requests": [], "folder_available": False, "restart_revokes": True,
}


def carries_secret(submission) -> bool:
    from app.core.chat_access import SECRET_FIELDS
    connection = submission.connection
    return connection is not None and any(
        getattr(connection, name).get_secret_value() for name in SECRET_FIELDS)


def edit_carries_secret(edit) -> bool:
    """Whether a saved-connection edit replaces a password, key or token."""
    from app.core.chat_access import SECRET_FIELDS
    return any(getattr(edit, name).get_secret_value() for name in SECRET_FIELDS)


# ---------------------------------------------------------------------------
# Viewer side
# ---------------------------------------------------------------------------

SNAPSHOT_MIN_SECONDS = 2.0
SNAPSHOT_MAX_SECONDS = 30.0
SNAPSHOT_TTL = 300.0
SNAPSHOT_LIMIT = 256


@dataclass
class _Snapshot:
    until: float
    value: dict | None = None
    task: asyncio.Task | None = None
    next_at: float = 0.0
    delay: float = SNAPSHOT_MIN_SECONDS


_snapshots: OrderedDict[tuple[str, str], _Snapshot] = OrderedDict()


def invalidate_snapshot(owner: str, session_id: str, *, saved: bool = False) -> None:
    # Removal also fences an in-flight response: it can finish for its caller,
    # but cannot repopulate the next request's cache after a change notification.
    for key in list(_snapshots):
        if key[0] == owner and (saved or key[1] == session_id):
            _snapshots.pop(key, None)


async def _fetch_snapshot(owner: str, session_id: str, entry: _Snapshot) -> dict:
    try:
        value = await control.call(owner, "chat-access", {"op": "snapshot", "session_id": session_id})
        entry.delay = (min(SNAPSHOT_MAX_SECONDS, entry.delay * 2)
                       if value == entry.value else SNAPSHOT_MIN_SECONDS)
        entry.value = copy.deepcopy(value)
        now = time.monotonic()
        entry.until = now + SNAPSHOT_TTL
        entry.next_at = now + entry.delay
        # Never cache a displayed grant/approval past its advertised expiry.
        for field in ("grants", "actions", "requests"):
            for item in value.get(field, []):
                expiry = item.get("expires_at")
                if isinstance(expiry, (int, float)):
                    entry.next_at = min(entry.next_at, now + max(0, expiry - time.time()))
        return value
    finally:
        entry.task = None


async def forward(owner: str, session_id: str, operation: str, payload: Optional[Dict[str, Any]] = None, *,
                  secure: bool = False, timeout: float | None = None) -> Dict[str, Any]:
    """Run an access-panel operation on the owner, coalescing snapshot polling.

    ChatAccess's safety polling can be continually reset by session events and
    multiple viewers. Identical snapshots back off here too (2 s -> 30 s), so
    those events cannot produce a cross-worker request every two seconds.
    Changes push an invalidation; mutations never use cached authorization.
    """
    if operation != "snapshot":
        invalidate_snapshot(owner, session_id, saved=True)
        try:
            return await control.call(owner, "chat-access", {"op": operation, "session_id": session_id, **(payload or {})},
                                      secure=secure, timeout=timeout)
        finally:
            invalidate_snapshot(owner, session_id, saved=True)
    now = time.monotonic()
    key = (owner, session_id)
    for stale, entry in list(_snapshots.items()):
        if entry.until <= now and entry.task is None:
            _snapshots.pop(stale, None)
    entry = _snapshots.get(key)
    if entry is None:
        entry = _Snapshot(now + SNAPSHOT_TTL)
        _snapshots[key] = entry
        while len(_snapshots) > SNAPSHOT_LIMIT:
            _snapshots.popitem(last=False)
    if entry.value is not None and now < entry.next_at:
        return copy.deepcopy(entry.value)
    if entry.task is None:
        entry.task = asyncio.create_task(_fetch_snapshot(owner, session_id, entry))
    return copy.deepcopy(await asyncio.shield(entry.task))


# ---------------------------------------------------------------------------
# Owner side
# ---------------------------------------------------------------------------

WATCH_SECONDS = 6 * 3600
WATCH_LIMIT = 256
PUSH_DELAY = 0.2
PUSH_TIMEOUT = 5.0
_watchers: Dict[tuple, float] = {}
_scheduled: set = set()
_tasks: set = set()


def watch(session_id: str, device_id: str) -> None:
    """Remember that ``device_id`` shows this session's access panel."""
    now = time.monotonic()
    key = (session_id, device_id)
    if key not in _watchers and len(_watchers) >= WATCH_LIMIT:
        for stale, until in list(_watchers.items()):
            if until <= now:
                del _watchers[stale]
        if len(_watchers) >= WATCH_LIMIT:
            del _watchers[min(_watchers, key=_watchers.__getitem__)]
    _watchers[key] = now + WATCH_SECONDS


def notify_watchers(session_id: str, *, saved: bool = False) -> None:
    """Ask each watching device to refresh. A saved-list change reaches every watched session."""
    if not _watchers:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    now = time.monotonic()
    for key, until in list(_watchers.items()):
        if until <= now:
            _watchers.pop(key, None)
        elif (saved or key[0] == session_id) and key not in _scheduled:
            _scheduled.add(key)
            task = loop.create_task(_push(key, saved))
            _tasks.add(task)
            task.add_done_callback(_tasks.discard)


async def _push(key: tuple, saved: bool) -> None:
    session_id, device_id = key
    try:
        await asyncio.sleep(PUSH_DELAY)  # one push for a burst of changes
        _scheduled.discard(key)
        from app.core.chat_access import broker
        await control.call(device_id, "chat-access-changed",
                           {"session_id": session_id, "saved": saved,
                            "pending": broker.pending_count(session_id)},
                           timeout=PUSH_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 - best effort; the viewer re-registers when it next asks
        logger.debug("Private-access refresh for %s not delivered: %s", device_id, type(exc).__name__)
        _watchers.pop(key, None)
    finally:
        _scheduled.discard(key)


def _identifier(body: Dict[str, Any], name: str) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError(f"{name} is required")
    return value


async def handle(body: Dict[str, Any], from_device: str) -> Dict[str, Any]:
    """Owner side of ``chat-access``: the same operations as the local access API."""
    from app.core.chat_access import AccessDenied, GrantSubmission, SavedConnectionEdit, broker, grant_failure
    operation = body.get("op")
    session_id = body.get("session_id")
    if operation not in OPERATIONS or not isinstance(session_id, str) or not session_id:
        raise ValueError("Unknown private-access operation")
    await run_sync(control._require_owned, session_id)
    watch(session_id, from_device)
    try:
        if operation == "snapshot":
            return await broker.snapshot(session_id)
        if operation == "grant":
            request_id = _identifier(body, "request_id")
            try:
                submission = GrantSubmission.model_validate(body.get("submission"))
            except ValueError:
                # Never echo a validation error: it would quote the private inputs.
                raise ValueError("Invalid access request. Check the fields and duration.") from None
            if carries_secret(submission) and not control.inbound_https.get():
                raise PermissionError(INSECURE_INPUTS)
            try:
                return await broker.fulfill(session_id, request_id, submission)
            except Exception as exc:  # noqa: BLE001 - same sanitized status as the local API
                status, message = grant_failure(exc)
                return {"grant_error": {"status": status, "detail": message}}
        if operation == "decline":
            await broker.decline(session_id, _identifier(body, "request_id"))
            return {"declined": True}
        if operation == "revoke":
            grant_id = _identifier(body, "grant_id")
            await broker.get(session_id, grant_id)
            broker.revoke(session_id, grant_id)
            return {"revoked": True}
        if operation == "decide":
            allow = body.get("allow")
            if type(allow) is not bool:
                raise ValueError("Supply a single boolean approval decision.")
            await broker.decide(session_id, _identifier(body, "action_id"), allow)
            return {"accepted": True}
        if operation == "edit":
            try:
                edit = SavedConnectionEdit.model_validate(body.get("changes"))
            except ValueError:
                # Never echo a validation error: it would quote the private inputs.
                raise ValueError("Invalid saved connection change. Check the fields.") from None
            if edit_carries_secret(edit) and not control.inbound_https.get():
                raise PermissionError(INSECURE_INPUTS)
            return await broker.edit_saved(session_id, _identifier(body, "connection_id"), edit)
        await broker.forget(session_id, _identifier(body, "connection_id"))
        return {"deleted": True}
    except AccessDenied as exc:
        raise PermissionError(str(exc)) from None


async def handle_changed(body: Dict[str, Any], from_device: str) -> Dict[str, Any]:
    """Viewer side: the owner says a mirrored session's access state changed."""
    from app.core.chat_access import publish_change
    from app.db import database
    session_id = _identifier(body, "session_id")
    row = await run_sync(database.get_session, session_id)
    if row is None:
        raise LookupError("Unknown session")
    if await run_sync(control.owner_of, row) != from_device:
        raise PermissionError("That device does not run this session")
    pending = body.get("pending")
    if isinstance(pending, bool) or not isinstance(pending, int) or not 0 <= pending <= 1000:
        pending = None  # an older owner sends none
    invalidate_snapshot(from_device, session_id, saved=body.get("saved") is True)
    await publish_change(session_id, row.get("api_user_id"), saved=body.get("saved") is True,
                         pending=pending)
    return {"refreshed": True}
