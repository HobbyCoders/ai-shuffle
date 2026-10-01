"""One-shot update fanout over the existing authenticated worker network.

No synced command rows, offline queues, URLs, shell commands or recursive fanout.
Each recipient independently checks its own policy, phase and live sessions.
"""
import asyncio
import time
import uuid
from collections import OrderedDict

from app.core import application_updates as updates
from app.core import update_progress as progress
from app.db.async_database import run_sync
from app.network import control, engine, membership, runners

_receipts = OrderedDict()
_dispatch_lock = asyncio.Lock()
# Peer status is a network round trip (often through the peer's tunnel), and the
# sidebar polls the snapshot. While no install is being verified, a reading is
# reused for PEER_STATUS_TTL seconds and then refreshed in the background, so a
# slow peer never holds up the poll. A peer that is checking or downloading is
# re-read sooner. Concurrent snapshots share one in-flight read per peer.
PEER_STATUS_TTL = 30
PEER_STATUS_BUSY_TTL = 3
_peer_status = {}


def local_action(action, request_id):
    if action not in ("check", "download", "install"):
        raise ValueError("Unknown update action")
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
        raise ValueError("Invalid update request id")
    # Called on the event-loop thread: no awaits between dedupe and enqueue.
    previous = _receipts.get(request_id)
    if previous and previous[0] > time.monotonic():
        return previous[1]
    state = updates.status()
    reason = None
    if not state["allow_updates"]:
        reason = "Updates disabled"
    elif not state["supported"]:
        reason = "Updater unavailable; install a release with update support"
    elif state["phase"] in ("checking", "downloading"):
        reason = "An update is already in progress"
    elif state["phase"] == "image-required":
        reason = "A newer runtime release is required"
    elif action == "download" and state["phase"] == "ready":
        reason = "Already staged; ready to restart"
    elif action == "download" and state["phase"] not in ("available", "error"):
        reason = "No application update available; current version left running"
    elif action == "install" and state["phase"] != "ready":
        reason = "No staged update; current version left running"
    if action == "install" and not reason:
        from app.core.desktop_lifecycle import active_session_ids
        if active_session_ids():
            reason = "Busy with active sessions; retry when finished"
    if not reason:
        try:
            updates.enqueue(action)
        except ValueError as exc:
            reason = str(exc)
    result = {"accepted": reason is None, "reason": reason or "Request queued", "phase": state["phase"],
              "version": state.get("version")}
    _receipts[request_id] = (time.monotonic() + 60, result)
    while len(_receipts) > 256:
        _receipts.popitem(last=False)
    return result


async def peer_action(body, from_device):
    context = engine.context()
    member = context.member(from_device) if context else None
    if not member or member.kind != membership.KIND_WORKER or member.status != membership.STATUS_ACTIVE:
        raise PermissionError("Only active account workers may request application updates")
    if body.get("operation") == "status":
        return await run_sync(updates.status)
    if body.get("operation") != "action":
        raise ValueError("Unknown update operation")
    if not isinstance(body.get("request_id"), str) or not body["request_id"]:
        raise ValueError("Invalid update request id")
    expiry = body.get("expires_at")
    if type(expiry) not in (int, float) or not time.time() <= expiry <= time.time() + 15:
        raise ValueError("Update request expired; offline commands are not replayed")
    return local_action(body.get("action"), from_device + ":" + body["request_id"])


async def _remote(row, body):
    try:
        result = await asyncio.wait_for(control.call(row["device_id"], "application-update", body, timeout=3), timeout=5)
        if not isinstance(result, dict):
            raise ValueError("Invalid peer update response")
        fields = ("accepted", "reason", "supported", "allow_updates", "phase", "version", "running_version", "percent", "message", "error")
        return {key: result[key] for key in fields if key in result}
    except control.ControlRefused:
        # The peer answered and declined (an older build without the updater
        # endpoint answers 404): it is definitively outside this update.
        return {"accepted": None, "reason": "Worker declined application updates; not included",
                "supported": False, "phase": "unsupported"}
    except Exception:
        # "unknown", not "unsupported": an online worker whose status cannot be
        # read may still be preparing, so it must never count as excluded.
        return {"accepted": None, "reason": "Worker unavailable or does not support application updates",
                "supported": False, "phase": "unknown"}


def _peer_entry(device_id):
    return _peer_status.setdefault(device_id, {"at": 0.0, "value": None, "task": None})


async def _read_peer(device_id, entry):
    try:
        value = await _remote({"device_id": device_id}, {"operation": "status"})
        entry.update(at=time.monotonic(), value=value)
        return value
    finally:
        entry["task"] = None


async def _peer_status_for(device_id, *, fresh):
    """A peer's status: awaited when ``fresh``, else cached and refreshed behind the reply."""
    entry = _peer_entry(device_id)
    value = entry["value"]
    if not fresh and value is not None:
        ttl = PEER_STATUS_BUSY_TTL if value.get("phase") in ("checking", "downloading") else PEER_STATUS_TTL
        if time.monotonic() - entry["at"] < ttl:
            return value
    if entry["task"] is None:
        entry["task"] = asyncio.ensure_future(_read_peer(device_id, entry))
    if fresh or value is None:
        return await asyncio.shield(entry["task"])
    return value


def _verifying(operation):
    # An install being verified needs live readings; a timed-out one is settled.
    return (operation is not None and operation["phase"] != "complete"
            and time.time() - operation["started_at"] < progress.STARTUP_TIMEOUT)


async def snapshot():
    local = await run_sync(updates.status)
    listing = await run_sync(runners.directory)
    rows = [{"device_id": listing.get("self_device_id") or "local", "device_name": "Current worker", "is_self": True,
             "online": True, **local}]
    operation = await run_sync(progress.load)
    # A completed install is terminal (progress.observe returns it as saved), so
    # its cohort no longer needs reading.
    tracked = ({row["device_id"] for row in operation["workers"]}
               if operation and operation["phase"] != "complete" else set())
    fresh = _verifying(operation)
    if local["update_all_workers"] or tracked:
        async def read(row):
            if not row["online"]:
                return {**row, "supported": False, "phase": "unsupported", "reason": "Offline; not queued"}
            return {**row, **await _peer_status_for(row["device_id"], fresh=fresh)}
        rows.extend(await asyncio.gather(*(read(row) for row in listing["runners"]
                                          if not row["is_self"] and (local["update_all_workers"] or row["device_id"] in tracked))))
    # Only return display fields, never peer endpoints/certificates.
    fields = ("device_id", "device_name", "is_self", "online", "supported", "allow_updates", "phase", "version", "running_version", "percent", "message", "error", "reason")
    # server_time lets a browser judge how recent a completion is without
    # trusting its own clock (completed_at is stamped by this worker).
    return {"allow_updates": local["allow_updates"], "update_all_workers": local["update_all_workers"],
            "server_time": time.time(),
            "operation": await run_sync(progress.observe, operation, rows),
            "workers": [{key: row.get(key) for key in fields} for row in rows]}


async def dispatch(action):
    async with _dispatch_lock:
        try:
            return await _dispatch(action)
        finally:
            # An action changes peer phases: the next snapshot must read them anew.
            _peer_status.clear()


async def _dispatch(action):
    if action not in ("check", "download", "install"):
        raise ValueError("Unknown update action")
    if not await run_sync(updates.allowed):
        raise ValueError("Application updates are disabled in Account settings")
    operation = await run_sync(progress.load)
    if operation and operation["phase"] != "complete":
        previous = await snapshot()
        if previous.get("operation") and previous["operation"]["phase"] == "waiting":
            raise ValueError("Waiting on workers to finish the previous update")
    listing = await run_sync(runners.directory)
    all_workers = await run_sync(updates.all_workers)
    request_id = uuid.uuid4().hex
    self_row = {"device_id": listing.get("self_device_id") or "local", "device_name": "Current worker"}
    if action == "install":
        targets = [row for row in listing["runners"] if all_workers and not row["is_self"] and row["online"]]
        await run_sync(progress.begin, request_id, [*targets, self_row])
    results = []
    if all_workers:
        semaphore = asyncio.Semaphore(8)
        async def send(row):
            if not row["online"]:
                result = {"accepted": False, "reason": "Offline; not queued"}
            else:
                async with semaphore:
                    if action == "install":
                        state = await _remote(row, {"operation": "status"})
                        await run_sync(progress.record, request_id, row["device_id"], target_version=state.get("version"))
                    result = await _remote(row, {"operation": "action", "action": action, "request_id": request_id, "expires_at": time.time() + 10})
                    if action == "install":
                        await run_sync(progress.record, request_id, row["device_id"], result=result)
            return {"device_id": row["device_id"], "device_name": row["device_name"], **result}
        results = await asyncio.gather(*(send(row) for row in listing["runners"] if not row["is_self"]))
    # Queue the initiating worker LAST so its restart cannot strand the fanout.
    if action == "install":
        state = await run_sync(updates.status)
        await run_sync(progress.record, request_id, self_row["device_id"], target_version=state.get("version"))
    local = local_action(action, request_id)
    if action == "install":
        await run_sync(progress.record, request_id, self_row["device_id"], result=local)
    results.append({**self_row, **local})
    return {"scope": "all" if all_workers else "current", "results": results}
