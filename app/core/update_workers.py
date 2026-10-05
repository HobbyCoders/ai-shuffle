"""One-shot update fanout over the existing authenticated worker network.

No synced command rows, offline queues, URLs, shell commands or recursive fanout.
Each recipient independently checks its own policy, phase and live sessions.
"""
import asyncio
import logging
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
# Why each worker refused the last dispatch: {device_id: reason}, stamped with
# when it was set. A refused worker that is not preparing or staged would be
# left behind, so restart waits until a new request reaches it.
_refusals = {}
_refusals_at = 0.0
# Containers check their feed on their own only once a day, so a worker that has
# not checked since another one found an update is sent a check: at most once
# per DISCOVERY_SECONDS per worker, never while an action or install is running.
DISCOVERY_SECONDS = 600
_discovery = {}
_discovery_tasks = set()
NOT_CHECKED = "Not checked yet; will check and download"
# "Restart when they finish" (#1818): this worker's waiting restart, if any:
# {"id", "expires_at", "task"}. In memory only; a restart of the worker drops it.
_deferred = None
DEFER_SECONDS = 12 * 60 * 60
DEFER_POLL_SECONDS = 5
# "Stop them and restart": how long stopped sessions get to wind down.
STOP_WAIT_SECONDS = 60
_stop_tasks = set()
logger = logging.getLogger(__name__)


def _busy_reason(busy):
    names = ", ".join(row["title"] for row in busy) or "a chat"
    return f"Busy with running work: {names}"


def _cancel_deferred():
    global _deferred
    task, _deferred = (_deferred or {}).get("task"), None
    if task is not None and not task.done() and task is not asyncio.current_task():
        task.cancel()
    return task is not None


async def _restart_when_idle(request_id):
    """Install once no session runs here (#1818). Never outlives DEFER_SECONDS."""
    global _deferred
    try:
        while _deferred and _deferred["id"] == request_id and time.time() < _deferred["expires_at"]:
            await asyncio.sleep(DEFER_POLL_SECONDS)
            from app.core.desktop_lifecycle import active_session_ids
            if active_session_ids():
                continue
            result = local_action("install", f"{request_id}:idle:{uuid.uuid4().hex[:8]}")
            if result.get("busy"):
                continue  # a new turn started between the two reads
            logger.info("Application update: deferred restart %s", result["reason"])
            return
    finally:
        if _deferred and _deferred["id"] == request_id:
            _deferred = None


async def _stop_then_restart(request_id):
    """Stop this worker's running work as its Stop buttons do, then install."""
    from app.core.desktop_lifecycle import active_session_ids, stop_session_work
    sessions = active_session_ids()
    results = await asyncio.gather(*(stop_session_work(sid, source="worker_update") for sid in sessions), return_exceptions=True)
    for sid, result in zip(sessions, results):
        if isinstance(result, BaseException):
            logger.warning("Application update: could not stop %s before restarting", sid, exc_info=result)
    deadline = time.monotonic() + STOP_WAIT_SECONDS
    while active_session_ids() and time.monotonic() < deadline:
        await asyncio.sleep(1)
    result = local_action("install", f"{request_id}:stopped")
    logger.info("Application update after stopping %d session(s): %s", len(sessions), result["reason"])


def local_action(action, request_id, mode=None):
    """One worker's part of an update action. ``mode`` only applies to install.

    A busy install is refused with the running chats named in ``busy``, unless
    ``mode`` is "when_idle" (restart once they finish) or "stop" (stop them,
    then restart). "cancel" drops a waiting "when_idle" restart.
    """
    global _deferred
    if action not in ("check", "download", "install", "cancel"):
        raise ValueError("Unknown update action")
    if mode not in (None, "when_idle", "stop"):
        raise ValueError("Unknown update mode")
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
        raise ValueError("Invalid update request id")
    # Called on the event-loop thread: no awaits between dedupe and enqueue.
    previous = _receipts.get(request_id)
    if previous and previous[0] > time.monotonic():
        return previous[1]
    if action == "cancel":
        cancelled = _cancel_deferred()
        result = {"accepted": cancelled, "reason": "Scheduled restart cancelled" if cancelled
                  else "No restart was scheduled"}
        _receipts[request_id] = (time.monotonic() + 60, result)
        return result
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
    elif action == "download" and state["phase"] not in ("idle", "available", "error"):
        reason = f"Cannot download while the updater is {state['phase']}"
    elif action == "install" and state["phase"] != "ready":
        reason = "No staged update; current version left running"
    busy = None
    if action == "install" and not reason:
        from app.core.desktop_lifecycle import active_session_ids, busy_work
        if active_session_ids():
            busy = busy_work()
            if mode is None:
                reason = _busy_reason(busy)
            else:
                # Restart later: when the work ends, or once it is stopped.
                _cancel_deferred()
                if mode == "when_idle":
                    _deferred = {"id": request_id, "expires_at": time.time() + DEFER_SECONDS}
                    _deferred["task"] = asyncio.ensure_future(_restart_when_idle(request_id))
                    reason, queued = None, "Restarts when running work finishes"
                else:
                    task = asyncio.ensure_future(_stop_then_restart(request_id))
                    _stop_tasks.add(task)
                    task.add_done_callback(_stop_tasks.discard)
                    queued = "Stopping running work, then restarting"
                result = {"accepted": True, "reason": queued, "phase": state["phase"],
                          "version": state.get("version"), "busy": busy,
                          "deferred": mode == "when_idle"}
                _receipts[request_id] = (time.monotonic() + 60, result)
                return result
    if not reason:
        try:
            updates.enqueue(action)
        except ValueError as exc:
            reason = str(exc)
    queued = "Request queued"
    if action == "download" and state["phase"] == "idle":
        # Peers check on their own only once a day, so a fanned-out download
        # usually finds one that has not seen the new revision yet. The updater
        # checks first and downloads only if the check finds an update.
        queued = "Checking for an update to download"
    result = {"accepted": reason is None, "reason": reason or queued, "phase": state["phase"],
              "version": state.get("version")}
    if busy:
        result["busy"] = busy
    _receipts[request_id] = (time.monotonic() + 60, result)
    while len(_receipts) > 256:
        _receipts.popitem(last=False)
    return result


def _build():
    """The source revision and app version this worker runs, for a peer's deploy status."""
    from app.core.config import settings
    from app.core.file_logging import source_revision
    revision = source_revision()
    return {"revision": None if revision == "unknown" else revision, "app_version": settings.version}


async def peer_action(body, from_device):
    context = engine.context()
    member = context.member(from_device) if context else None
    if not member or member.kind != membership.KIND_WORKER or member.status != membership.STATUS_ACTIVE:
        raise PermissionError("Only active account workers may request application updates")
    if body.get("operation") == "status":
        return {**await run_sync(updates.status), **_build(), "deferred": _deferred is not None}
    if body.get("operation") != "action":
        raise ValueError("Unknown update operation")
    if not isinstance(body.get("request_id"), str) or not body["request_id"]:
        raise ValueError("Invalid update request id")
    expiry = body.get("expires_at")
    if type(expiry) not in (int, float) or not time.time() <= expiry <= time.time() + 15:
        raise ValueError("Update request expired; offline commands are not replayed")
    result = local_action(body.get("action"), from_device + ":" + body["request_id"], body.get("mode"))
    logger.info("Application update %s requested by worker %s: %s", body.get("action"), from_device,
                result["reason"])
    return result


async def _remote(row, body):
    try:
        result = await asyncio.wait_for(control.call(row["device_id"], "application-update", body, timeout=3), timeout=5)
        if not isinstance(result, dict):
            raise ValueError("Invalid peer update response")
        fields = ("accepted", "reason", "supported", "allow_updates", "phase", "version", "running_version", "percent", "message", "error",
                  "request_pending", "checked_at", "channel", "release", "notes", "kind",
                  "revision", "app_version", "busy", "deferred")
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
        busy = value.get("phase") in ("checking", "downloading") or value.get("request_pending")
        ttl = PEER_STATUS_BUSY_TTL if busy else PEER_STATUS_TTL
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


def _same(a, b):
    return bool(a and b and (a.startswith(b) or b.startswith(a)))


def _offers(rows, all_workers):
    """The updates included workers have found: [{version, channel, checked_at}]."""
    return [{"version": row["version"], "channel": row.get("channel"), "checked_at": row.get("checked_at")}
            for row in rows
            if (all_workers or row.get("is_self")) and row.get("online", True) and row.get("supported")
            and row.get("allow_updates") and row.get("phase") in ("available", "downloading", "ready")
            and row.get("version") and not _same(row["version"], row.get("running_version"))]


def _not_checked(row, offers):
    """An idle worker that has not read its feed since another worker found ``offers``."""
    for offer in offers:
        if _same(offer["version"], row.get("running_version")):
            continue
        if offer["channel"] and row.get("channel") and offer["channel"] != row["channel"]:
            continue
        checked, found = row.get("checked_at"), offer["checked_at"]
        if not isinstance(checked, (int, float)) or (isinstance(found, (int, float)) and checked < found):
            return True
    return False


def classify(row, all_workers, offers=(), refusals=None):
    """A worker's part in the next update: ``{"role", "label"}``.

    Roles: excluded (not part of this update), preparing, available, staged,
    and unknown (an included worker whose state cannot be trusted; restart
    waits for it). DesktopUpdateButton.svelte keeps a fallback copy of this for
    workers that predate it; keep the two in step.
    """
    phase = row.get("phase")
    if not all_workers and row.get("is_self") is False:
        # A peer read only to verify an earlier all-worker install.
        return {"role": "excluded", "label": "Not in scope; current worker only"}
    if not row.get("online", True):
        return {"role": "excluded", "label": row.get("reason") or "Offline; not included"}
    # A failed status read is not proof the worker sits this update out.
    if phase == "unknown":
        return {"role": "unknown", "label": "Status unavailable"}
    if not row.get("supported"):
        return {"role": "excluded", "label": row.get("reason") or "No application updater; not included"}
    if not row.get("allow_updates"):
        return {"role": "excluded", "label": "Updates disabled on this worker; not included"}
    if _same(row.get("version"), row.get("running_version")):
        return {"role": "excluded", "label": "Already up to date"}
    # Queued but not yet taken (a desktop takes requests on its 30 s report).
    if row.get("request_pending") and phase != "ready":
        return {"role": "preparing", "label": "Request queued; waiting for the updater"}
    refused = (refusals or {}).get(row.get("device_id"))
    if refused and phase not in ("checking", "downloading", "ready", "image-required"):
        return {"role": "unknown", "label": f"Refused: {refused}"}
    if phase == "checking":
        return {"role": "preparing", "label": "Checking for updates…"}
    if phase == "downloading":
        percent = row.get("percent")
        suffix = f" {round(percent)}%" if isinstance(percent, (int, float)) else ""
        return {"role": "preparing", "label": f"{row.get('message') or 'Downloading update…'}{suffix}"}
    if phase == "ready":
        if row.get("deferred"):
            return {"role": "staged", "label": "Restarts when work finishes"}
        error = row.get("error")
        return {"role": "staged", "label": f"Staged, awaiting restart. {error}" if error else "Staged, awaiting restart"}
    if phase == "available":
        return ({"role": "available", "label": "Update available; not downloaded"} if row.get("version")
                else {"role": "excluded", "label": "No update offered"})
    if phase == "error":
        return ({"role": "available", "label": row.get("error") or "Download failed; retry"} if row.get("version")
                else {"role": "unknown", "label": row.get("error") or "Update check failed"})
    if phase == "image-required":
        return {"role": "excluded", "label": row.get("message") or "Needs a new Docker image; not included"}
    if phase == "idle" and _not_checked(row, offers):
        return {"role": "available", "label": NOT_CHECKED}
    message = row.get("message")
    return {"role": "excluded", "label": (message if message and not updates.IN_PROGRESS.search(message) else "")
            or "No update offered"}


def plan(rows, operation=None):
    """What one click does next, from rows already carrying ``role`` and ``label``."""
    targets = [row for row in rows if row["role"] != "excluded"]
    preparing = any(row["role"] == "preparing" for row in targets)
    available = any(row["role"] == "available" for row in targets)
    staged = sum(row["role"] == "staged" for row in targets)
    unknown = [row for row in targets if row["role"] == "unknown"]
    # Restart is offered only once EVERY included worker is staged, never on the first one.
    ready = staged > 0 and not preparing and not available and not unknown
    waits = ", ".join(f"{row['device_name']} ({row['label']})" for row in unknown)
    held = f"Restart waits for {waits}." if staged and not preparing and not available and unknown else ""
    return {"action": "install" if ready else "download" if available else "check",
            "ready": ready, "preparing": preparing, "available": available, "staged": staged,
            "included": len(targets), "held": held,
            "waiting": bool(operation and operation.get("phase") == "waiting")}


def _discover(rows, busy):
    """Send a check to included workers that have not seen an update another worker found."""
    if busy or _dispatch_lock.locked():
        return
    now = time.monotonic()
    for row in rows:
        if row["label"] != NOT_CHECKED or now - _discovery.get(row["device_id"], -DISCOVERY_SECONDS) < DISCOVERY_SECONDS:
            continue
        _discovery[row["device_id"]] = now
        request_id = uuid.uuid4().hex
        logger.info("Application update: asking %s to check its feed; another worker found an update",
                    row["device_name"])
        if row.get("is_self"):
            local_action("check", request_id)
            continue
        task = asyncio.ensure_future(_remote(row, {"operation": "action", "action": "check", "request_id": request_id,
                                                   "expires_at": time.time() + 10}))
        _discovery_tasks.add(task)
        task.add_done_callback(_discovery_tasks.discard)
        # Its next status read must show the check rather than the cached idle.
        _peer_status.pop(row["device_id"], None)


async def snapshot(*, fresh=False):
    local = await run_sync(updates.status)
    listing = await run_sync(runners.directory)
    rows = [{"device_id": listing.get("self_device_id") or "local", "device_name": "Current worker", "is_self": True,
             "online": True, **local, "deferred": _deferred is not None}]
    operation = await run_sync(progress.load)
    # A completed install is terminal (progress.observe returns it as saved), so
    # its cohort no longer needs reading.
    tracked = ({row["device_id"] for row in operation["workers"]}
               if operation and operation["phase"] != "complete" else set())
    verifying = _verifying(operation)
    fresh = fresh or verifying
    if local["update_all_workers"] or tracked:
        async def read(row):
            if not row["online"]:
                return {**row, "supported": False, "phase": "unsupported", "reason": "Offline; not queued"}
            return {**row, **await _peer_status_for(row["device_id"], fresh=fresh)}
        rows.extend(await asyncio.gather(*(read(row) for row in listing["runners"]
                                          if not row["is_self"] and (local["update_all_workers"] or row["device_id"] in tracked))))
    observed = await run_sync(progress.observe, operation, rows)
    global _refusals_at
    if (_refusals and observed and observed.get("phase") == "complete"
            and (observed.get("completed_at") or 0) >= _refusals_at):
        _refusals.clear()  # an install finished after them; they are history
    all_workers = local["update_all_workers"]
    offers = _offers(rows, all_workers)
    for row in rows:
        row.update(classify(row, all_workers, offers, _refusals))
    if all_workers:
        _discover(rows, verifying)
    # Only return display fields, never peer endpoints/certificates.
    fields = ("device_id", "device_name", "is_self", "online", "supported", "allow_updates", "phase", "version", "running_version", "percent", "message", "error", "reason",
              "request_pending", "checked_at", "channel", "release", "notes", "role", "label", "deferred")
    # server_time lets a browser judge how recent a completion is without
    # trusting its own clock (completed_at is stamped by this worker).
    return {"allow_updates": local["allow_updates"], "update_all_workers": all_workers,
            "server_time": time.time(),
            "operation": observed,
            "plan": plan(rows, observed),
            "refusals": dict(_refusals),
            "workers": [{key: row.get(key) for key in fields} for row in rows]}


async def dispatch(action, mode=None):
    async with _dispatch_lock:
        try:
            return await _dispatch(action, mode)
        finally:
            # An action changes peer phases: the next snapshot must read them anew.
            _peer_status.clear()


async def _dispatch(action, mode=None):
    global _refusals_at
    if action not in ("check", "download", "install", "cancel"):
        raise ValueError("Unknown update action")
    if mode not in (None, "when_idle", "stop") or (mode and action != "install"):
        raise ValueError("Unknown update mode")
    if action == "cancel":
        return await _send_cancel()
    if not await run_sync(updates.allowed):
        raise ValueError("Application updates are disabled in Account settings")
    result = await _send(action, mode)
    refused = {row["device_id"]: row.get("reason") or "Refused" for row in result["results"]
               if not row.get("accepted") and row.get("device_id")}
    _refusals.clear()
    _refusals.update(refused)
    _refusals_at = time.time()
    for row in result["results"]:
        logger.info("Application update %s for %s: %s (%s)", result.get("sent", action), row["device_name"],
                    "accepted" if row.get("accepted") else "refused", row.get("reason"))
    return result


async def _send_cancel():
    """Drop a waiting "Restart when they finish" on every worker in scope."""
    listing = await run_sync(runners.directory)
    request_id = uuid.uuid4().hex
    results = []
    all_workers = await run_sync(updates.all_workers)
    if all_workers:
        results = list(await asyncio.gather(*(_act(row, "cancel", request_id) for row in listing["runners"]
                                             if not row["is_self"] and row["online"])))
    self_row = {"device_id": listing.get("self_device_id") or "local", "device_name": "Current worker"}
    results.append({**self_row, **local_action("cancel", request_id)})
    return {"scope": "all" if all_workers else "current", "sent": "cancel", "results": results}


def _later(result):
    """A worker that will not restart now: refused while busy, or waiting for its work to end."""
    return result.get("accepted") is False and bool(result.get("busy")) or bool(result.get("deferred"))


async def _send(action, mode=None):
    operation = await run_sync(progress.load)
    if operation and operation["phase"] != "complete":
        previous = await snapshot()
        if previous.get("operation") and previous["operation"]["phase"] == "waiting":
            raise ValueError("Waiting on workers to finish the previous update")
    listing = await run_sync(runners.directory)
    all_workers = await run_sync(updates.all_workers)
    request_id = uuid.uuid4().hex
    self_row = {"device_id": listing.get("self_device_id") or "local", "device_name": "Current worker"}
    if action == "install" and all_workers:
        # Never restart part of the fleet: a worker that still has to download
        # (often one that has not checked yet) is sent the download instead,
        # and nothing installs until every included worker is staged.
        current = await snapshot(fresh=True)
        if not current["plan"]["ready"]:
            behind = [row for row in current["workers"] if row["role"] == "available"]
            if not behind:
                raise ValueError(current["plan"]["held"] or "Not every worker is staged yet; nothing was installed")
            results = await asyncio.gather(*(_act(row, "download", request_id) for row in behind))
            return {"scope": "all", "sent": "download", "held": "Not every worker is staged yet; nothing was installed",
                    "results": list(results)}
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
                    body = {"operation": "action", "action": action, "request_id": request_id, "expires_at": time.time() + 10}
                    if mode:
                        body["mode"] = mode
                    result = await _remote(row, body)
                    if action == "install":
                        await run_sync(progress.record, request_id, row["device_id"], result=_tracked(result))
            return {"device_id": row["device_id"], "device_name": row["device_name"], **result}
        results = await asyncio.gather(*(send(row) for row in listing["runners"] if not row["is_self"]))
    # Queue the initiating worker LAST so its restart cannot strand the fanout.
    if action == "install":
        state = await run_sync(updates.status)
        await run_sync(progress.record, request_id, self_row["device_id"], target_version=state.get("version"))
    local = local_action(action, request_id, mode)
    if action == "install":
        await run_sync(progress.record, request_id, self_row["device_id"], result=_tracked(local))
    results.append({**self_row, **local})
    if action == "install" and all(_later(row) for row in results if row.get("reason") != "Offline; not queued"):
        # Nothing restarts now: no install to verify, so no "startup could
        # not be confirmed" error hides the busy reason (#1818).
        await run_sync(progress.discard, request_id)
    return {"scope": "all" if all_workers else "current", "sent": action, "results": results}


def _tracked(result):
    """What install verification records: a waiting restart is not one to verify now."""
    return {**result, "accepted": False} if result.get("deferred") else result


async def _act(row, action, request_id):
    """Send one action to one snapshot row, the current worker included."""
    if row.get("is_self"):
        result = local_action(action, request_id)
    else:
        result = await _remote(row, {"operation": "action", "action": action, "request_id": request_id,
                                     "expires_at": time.time() + 10})
    return {"device_id": row["device_id"], "device_name": row["device_name"], **result}
