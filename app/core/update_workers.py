"""One-shot update fanout over the existing authenticated worker network.

No synced command rows, offline queues, URLs, shell commands or recursive fanout.
Each recipient independently checks its own policy, phase and live sessions.
"""
import asyncio
import logging
import re
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
_stop_result = None
logger = logging.getLogger(__name__)


class UpdateNotSent(ValueError):
    """A preflight refusal, before any update action was delivered."""


def waiting_status(state, *, busy_rows=None):
    """Owner-only restart state; staging and a busy install are independent.

    Call on the event loop, not in run_sync: runtime/registry liveness belongs
    to that loop. Do not attach session titles to the ordinary authenticated
    policy endpoint. Old producers only prove staging in the ready phase.
    """
    from app.core.desktop_lifecycle import active_session_ids, busy_work
    from app.core.session_runtime import active_executions, get_live_runtime
    from app.core.task_registry import task_registry
    from app.core.session_pool import get_session_pool
    from app.core.provider_agent_supervisor import live_workers
    from app.mcp.builtin_tools import is_background_shell_running
    from app.core.monitor_spawn import serving_idle_budget_seconds

    staged = state.get("staged_version") or (state.get("version") if state.get("phase") == "ready" else None)
    blocked = []
    executing = set(active_executions().values())
    rows = busy_work() if busy_rows is None else _fresh_busy_rows(busy_rows, set(active_session_ids()))
    for row in rows:
        sid = row["session_id"]
        base = {"session_id": sid, "title": row["title"],
                "running_turn": row.get("running_turn", sid in executing),
                "turn_ended_at": row.get("turn_ended_at"), "turn_state": row.get("turn_state")}
        details = {task["task_id"]: task for task in row.get("task_details", [])}
        tasks = task_registry.get_active(sid)
        if sid in executing:
            runtime = get_live_runtime(sid)
            turn = getattr(runtime, "turn", {})
            since = turn.get("started_at")
            if hasattr(since, "isoformat"):
                since = since.isoformat()
            blocked.append({**base, "kind": "turn", "since": since,
                            "live": True, "liveness": "live"})
        state_in_pool = get_session_pool().peek(sid)
        adapter = getattr(state_in_pool, "adapter", None)
        pending = getattr(getattr(adapter, "_engine", None), "_background_pending", None)
        workers = {worker.task_id for worker in live_workers(sid)}
        for task in tasks:
            tid = task["id"]
            detail = details.get(tid, {})
            kind = "remote" if task.get("worker") else detail.get("kind") or task.get("kind") or "registry"
            if kind == "remote":
                live, liveness = False, "unknown"
            elif kind in ("shell", "service"):
                live = is_background_shell_running(tid)
                liveness = "live" if live else "stale" if task_registry.is_user_stopped(sid) else "unknown"
            else:
                live = tid in workers or (isinstance(pending, dict) and tid in pending)
                liveness = "live" if live else "unknown"
            blocked.append({**base, **detail, "task_id": tid,
                            "kind": kind, "since": task.get("startedAt"),
                            "live": live, "liveness": liveness})
        if sid not in executing and not tasks:
            blocked.append({**base, "kind": "registry", "since": None,
                            "live": False, "liveness": "unknown"})
    if _stop_tasks:
        install, reason = "stopping", "stopping_work"
    elif _deferred:
        install, reason = "waiting_for_idle", "running_work"
    elif state.get("install_pending") and state.get("phase") == "ready":
        install, reason = "queued", "updater_acknowledgement"
    elif staged and state.get("phase") != "ready":
        install = "unavailable"
        reason = ("newer_offer" if state.get("phase") == "available" else
                  "preparing_update" if state.get("phase") in ("checking", "downloading") else
                  "updater_idle" if state.get("phase") == "idle" else "updater_error")
    elif staged:
        install, reason = ("blocked", "running_work") if blocked else ("ready", None)
        if blocked and _recent_stop_refusal(blocked):
            reason = "still_busy_after_stop"
    else:
        install, reason = "idle", None
    return {"staged_version": staged, "install_state": install,
            "blocked_by": blocked, "wait_reason": reason,
            "restart_expires_at": (_deferred or {}).get("expires_at"),
            "monitor_serving_idle_seconds": serving_idle_budget_seconds(), "stop_result": _stop_result}


def _recent_stop_refusal(blocked):
    """Do not attribute new work to an old Stop, even in the same chat."""
    from datetime import datetime, timezone

    result = _stop_result or {}
    completed = result.get("completed_at")
    if result.get("accepted") is not False or not isinstance(completed, (int, float)):
        return False
    if not 0 <= time.time() - completed <= 60:
        return False
    sessions = {row["session_id"] for row in result.get("busy", [])}
    for blocker in blocked:
        if blocker["session_id"] not in sessions:
            return False
        since = blocker.get("started_at") or blocker.get("since")
        if since:
            try:
                started = datetime.fromisoformat(since.replace("Z", "+00:00"))
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                if started.timestamp() > completed:
                    return False
            except (ValueError, TypeError, AttributeError):
                return False  # Unknown incarnation: plain running work is safer.
    return True


def _waiting_unavailable():
    return {key: None for key in ("staged_version", "install_state", "blocked_by", "wait_reason",
                                  "restart_expires_at", "monitor_serving_idle_seconds", "stop_result")}


def _safe_waiting_status(state, busy_rows):
    try:
        return waiting_status(state, busy_rows=busy_rows)
    except Exception as exc:  # Display metadata must never undo a queued action.
        logger.warning("Application update: waiting state unavailable (%s)", type(exc).__name__)
        return _waiting_unavailable()


def _fresh_busy_rows(rows, active):
    """Use fresh membership, retaining bounded names from the earlier DB read."""
    from app.core.desktop_lifecycle import BUSY_LIMIT

    busy = [row for row in rows if row["session_id"] in active][:BUSY_LIMIT]
    named = {row["session_id"] for row in busy}
    for sid in sorted(active - named)[:BUSY_LIMIT - len(busy)]:
        busy.append({"session_id": sid, "title": "Running chat", "tasks": []})
    return busy


async def _async_waiting_status(state):
    from app.core.desktop_lifecycle import busy_work
    try:
        rows = await run_sync(busy_work)
    except Exception as exc:
        logger.warning("Application update: waiting state unavailable (%s)", type(exc).__name__)
        return _waiting_unavailable()
    return _safe_waiting_status(state, rows)


async def _local_action(action, request_id, mode=None):
    # Keep registry/engine decisions and atomic enqueue on the loop; only the
    # optional owner-display DB reads run in a thread. Recheck busy after await.
    from app.core.desktop_lifecycle import busy_work
    rows, unavailable = [], False
    if action == "install":
        try:
            rows = await run_sync(busy_work)
        except Exception as exc:
            logger.warning("Application update: waiting state unavailable (%s)", type(exc).__name__)
            unavailable = True
    result = local_action(action, request_id, mode, busy_rows=rows)
    if unavailable:
        result.update(_waiting_unavailable())
    return result


def _busy_reason(busy):
    names = []
    for row in busy:
        services = [task for task in row.get("task_details", []) if task.get("kind") == "service"]
        if not services:
            names.append(row["title"])
        for task in services:
            details = [f"port {task['port']}"] if task.get("port") else []
            if task.get("started_at"):
                details.append(f"started {task['started_at']}")
            if not row.get("running_turn"):
                details.append(f"chat turn ended {row['turn_ended_at']}" if row.get("turn_ended_at") else "chat has no running turn")
            name = "Preview server" if "preview" in task.get("label", "").lower() else "Service"
            status = "still running" if task.get("live") else "still registered (process not confirmed)"
            names.append(f"{name} {status} for {row['title']} ({'; '.join(details)})")
    return "Busy with running work: " + (", ".join(names) or "a chat")


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
            result = await _local_action("install", f"{request_id[:64]}:idle:{uuid.uuid4().hex[:8]}")
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
    while time.monotonic() < deadline:
        if not active_session_ids():
            break
        await asyncio.sleep(1)
    # Do not bypass the normal guard: both runtimes check again before exit.
    # A failed stop must remain a named refusal, never a misleading queue ack.
    result = await _local_action("install", f"{request_id[:64]}:stopped:{uuid.uuid4().hex[:8]}")
    global _stop_result
    _stop_result = {"request_id": request_id, "accepted": result.get("accepted", False), "reason": result["reason"],
                    "completed_at": time.time(), "busy": result.get("busy", [])}
    logger.info("Application update after stopping %d session(s): %s", len(sessions), result["reason"])
    if not result.get("accepted"):
        # Publish after dispatch releases its lock, and only to the same install
        # operation. A newer dispatch must not inherit this old stop's refusal.
        global _refusals_at
        try:
            listing = await run_sync(runners.directory)
            async with _dispatch_lock:
                operation = await run_sync(progress.load)
                if not operation or operation["id"] != request_id:
                    return
                self_id = listing.get("self_device_id") or "local"
                await run_sync(progress.record, request_id, self_id, result=result)
                _refusals[self_id] = result["reason"]
                _refusals_at = time.time()
        except Exception:  # noqa: BLE001 — a reporting failure must still leave the refusal logged.
            logger.warning("Application update: could not publish stop refusal for %s", request_id, exc_info=True)


def local_action(action, request_id, mode=None, *, busy_rows=None):
    """One worker's part of an update action. ``mode`` only applies to install.

    A busy install is refused with the running chats named in ``busy``, unless
    ``mode`` is "when_idle" (restart once they finish) or "stop" (stop them,
    then restart). "cancel" drops a waiting "when_idle" restart.
    """
    global _deferred, _stop_result
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
    elif action == "install" and mode in ("stop", "when_idle") and _stop_tasks:
        reason = "A stop and restart request is already in progress"
    elif action == "install" and state["phase"] != "ready":
        reason = ("A previous download is retained; finish checking or downloading the offered update before restarting"
                  if state.get("staged_version") else "No staged update; current version left running")
    busy = None
    if action == "install" and not reason:
        from app.core.desktop_lifecycle import active_session_ids, busy_work
        active = set(active_session_ids())
        if active:
            busy = _fresh_busy_rows(busy_work() if busy_rows is None else busy_rows, active)
            if mode is None:
                reason = _busy_reason(busy)
                omitted = len(active) - len(busy)
                if omitted:
                    reason += f", and {omitted} more"
            else:
                # Restart later: when the work ends, or once it is stopped.
                _cancel_deferred()
                _stop_result = None
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
                          "deferred": mode == "when_idle", **_safe_waiting_status(state, busy)}
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
    if action == "install":
        result.update(_safe_waiting_status({**state, "install_pending": reason is None or state.get("install_pending", False)}, busy or []))
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
        state = await run_sync(updates.status)
        return {**state, **await _async_waiting_status(state), **_build(), "deferred": _deferred is not None}
    if body.get("operation") != "action":
        raise ValueError("Unknown update operation")
    if not isinstance(body.get("request_id"), str) or not body["request_id"]:
        raise ValueError("Invalid update request id")
    expiry = body.get("expires_at")
    if type(expiry) not in (int, float) or not time.time() <= expiry <= time.time() + 15:
        raise ValueError("Update request expired; offline commands are not replayed")
    result = await _local_action(body.get("action"), from_device + ":" + body["request_id"], body.get("mode"))
    logger.info("Application update action=%s mode=%s scope=current requested by worker %s: %s",
                body.get("action"), body.get("mode"), from_device, result["reason"])
    return result


async def _remote(row, body):
    try:
        result = await asyncio.wait_for(control.call(row["device_id"], "application-update", body, timeout=3), timeout=5)
        if not isinstance(result, dict):
            raise ValueError("Invalid peer update response")
        fields = ("accepted", "reason", "supported", "allow_updates", "phase", "version", "running_version", "percent", "message", "error",
                  "request_pending", "checked_at", "channel", "release", "notes", "kind",
                  "revision", "app_version", "busy", "deferred", "notice", "installer_newer", "retry_start",
                  "staged_version", "install_state", "blocked_by", "wait_reason", "restart_expires_at", "monitor_serving_idle_seconds", "stop_result")
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


def _check_first(row):
    """A retained idle download is not an offer to download or install yet."""
    return row.get("phase") == "idle" and bool(row.get("staged_version"))


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
    # Before the same-version test: the container updater fills ``version``
    # with the RUNNING revision on this phase, so it would read as current (#1901).
    if phase == "image-required":
        return {"role": "excluded", "label": row.get("message") or "Needs a new Docker image; not included"}
    if _check_first(row):
        if row.get("request_pending"):
            return {"role": "preparing", "label": "Request queued; waiting for the updater"}
        return {"role": "available", "label": "Check for updates before restarting"}
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
        # A desktop says why it offers an update again: its last first start did not finish.
        label = (row.get("retry_start") is True and row.get("message")) or "Update available; not downloaded"
        return ({"role": "available", "label": label} if row.get("version")
                else {"role": "excluded", "label": "No update offered"})
    if phase == "error":
        return ({"role": "available", "label": row.get("error") or "Download failed; retry"} if row.get("version")
                else {"role": "unknown", "label": row.get("error") or "Update check failed"})
    if phase == "idle" and _not_checked(row, offers):
        return {"role": "available", "label": NOT_CHECKED}
    message = row.get("message")
    return {"role": "excluded", "label": (message if message and not updates.IN_PROGRESS.search(message) else "")
            or "No update offered"}


# The desktop launcher's own words when only a newer installer can finish the
# update (desktop/src/main/source-updates.ts and installed-entry.ts). The shell
# reports them as free text, so they are read here, on the worker that builds
# the snapshot: peers on older builds get the same answer.
_INSTALLER = re.compile(r"install the latest (?:ai shuffle installer|desktop launcher)"
                        r"|install a verified newer (?:build|release)", re.I)
# A revision whose first start failed is refused until a newer update. The
# installer helps only when it is a newer release than the one running, which
# the desktop reports as ``installer_newer``; otherwise its link is a dead end
# (it would reinstall the same version), so the owner checks again instead.
_FAILED_START = re.compile(r"this revision failed to start", re.I)
# Said only when the launcher kept the previous files and database: "Previous
# files and data have been retained", "Your files and data have been kept", or
# a missing restore point, which requireRestorePoint refuses before the update's
# first database start, so the database was never opened by the new build.
_KEPT = re.compile(r"(?:previous files and|your files and) data have been (?:retained|kept)"
                   r"|no verified database restore point exists", re.I)


def remedy(row):
    """What only the owner can do for a worker: ``{"remedy", "data_kept"}``.

    ``remedy`` is "installer" when the in-app updater cannot finish and the
    latest installer must be run (a desktop that cannot update itself, or one
    whose update needs a newer installer or launcher), else None. ``data_kept``
    is True only when the launcher said the previous data was kept.
    """
    if row.get("kind") != "desktop" or not row.get("online", True):
        return {"remedy": None, "data_kept": False}
    text = " ".join(str(row.get(key) or "") for key in ("error", "message"))
    needed = (row.get("phase") == "unsupported" or bool(_INSTALLER.search(text))
              or (bool(_FAILED_START.search(text)) and row.get("installer_newer") is True))
    return {"remedy": "installer" if needed else None, "data_kept": needed and bool(_KEPT.search(text))}


def plan(rows, operation=None):
    """What one click does next, from rows already carrying ``role`` and ``label``."""
    targets = [row for row in rows if row["role"] != "excluded"]
    preparing = any(row["role"] == "preparing" for row in targets)
    check_first = [row for row in targets if row["role"] == "available" and _check_first(row)]
    available = any(row["role"] == "available" and not _check_first(row) for row in targets)
    staged = sum(row["role"] == "staged" for row in targets)
    unknown = [row for row in targets if row["role"] == "unknown"]
    # Restart is offered only once EVERY included worker is staged, never on the first one.
    ready = staged > 0 and not preparing and not available and not unknown and not check_first
    waits = ", ".join(f"{row['device_name']} ({row['label']})" for row in unknown)
    held = f"Restart waits for {waits}." if staged and not preparing and not available and unknown else ""
    if check_first:
        held = f"Check for updates needed on {', '.join(row['device_name'] for row in check_first)}; nothing was installed"
    return {"action": "check" if check_first else "install" if ready else "download" if available else "check",
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
             "online": True, **local, **await _async_waiting_status(local), "deferred": _deferred is not None}]
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
    # A peer's stop finishes after its action reply. Correlate the later status
    # receipt with this operation, never a historical failure or another sender.
    # record() fences the persisted write if a new dispatch races this snapshot.
    if operation:
        by_device = {row["device_id"]: row for row in rows}
        for target in operation["workers"]:
            row = by_device.get(target["device_id"], {})
            outcome = row.get("stop_result") or {}
            expected = operation["id"] if row.get("is_self") else f"{listing.get('self_device_id') or 'local'}:{operation['id']}"
            if outcome.get("request_id") == expected and outcome.get("accepted") is False and target.get("accepted") is not False:
                await run_sync(progress.record, operation["id"], target["device_id"], result=outcome)
                target.update(accepted=False, reason=outcome.get("reason") or "Restart refused after stopping work")
    observed = await run_sync(progress.observe, operation, rows)
    global _refusals_at
    if (_refusals and observed and observed.get("phase") == "complete"
            and (observed.get("completed_at") or 0) >= _refusals_at):
        _refusals.clear()  # an install finished after them; they are history
    all_workers = local["update_all_workers"]
    offers = _offers(rows, all_workers)
    for row in rows:
        row.update(classify(row, all_workers, offers, _refusals), **remedy(row))
    if all_workers:
        _discover(rows, verifying)
    # Only return display fields, never peer endpoints/certificates.
    fields = ("device_id", "device_name", "is_self", "online", "supported", "allow_updates", "phase", "version", "running_version", "percent", "message", "error", "reason",
              "request_pending", "checked_at", "channel", "release", "notes", "role", "label", "deferred", "notice",
              "remedy", "data_kept", "staged_version", "install_state", "blocked_by", "wait_reason", "restart_expires_at", "monitor_serving_idle_seconds", "stop_result")
    # server_time lets a browser judge how recent a completion is without
    # trusting its own clock (completed_at is stamped by this worker).
    return {"allow_updates": local["allow_updates"], "update_all_workers": all_workers,
            "server_time": time.time(),
            "operation": observed,
            "plan": plan(rows, observed),
            "refusals": dict(_refusals),
            "workers": [{key: row.get(key) for key in fields} for row in rows]}


async def dispatch(action, mode=None, scope=None):
    """``scope`` "current" acts on this worker only, even when "Update all
    workers" is on (an agent updating its own worker must not restart peers)."""
    async with _dispatch_lock:
        try:
            return await _dispatch(action, mode, scope)
        except ValueError as exc:
            resolved_scope = "all" if await _in_scope(scope) else "current"
            logger.info("Application update refusal action=%s mode=%s scope=%s delivery=%s: %s",
                        action, mode, resolved_scope, "not_sent" if isinstance(exc, UpdateNotSent) else "unknown", exc)
            if not isinstance(exc, UpdateNotSent) or action != "install" or mode not in ("stop", "when_idle") or scope not in (None, "current"):
                raise
            listing = await run_sync(runners.directory)
            targets = [row for row in listing["runners"] if resolved_scope == "all" and not row["is_self"]]
            targets.append({"device_id": listing.get("self_device_id") or "local", "device_name": "Current worker"})
            results = [{"device_id": row["device_id"], "device_name": row["device_name"], "accepted": False,
                        "request_state": "not_sent", "mode": mode,
                        "reason": str(exc)} for row in targets]
            # No delivery happened: preserve actual refusals from the active
            # operation. These request-local rows already explain non-delivery.
            return {"scope": resolved_scope, "sent": None, "action": action, "mode": mode,
                    "request_state": "not_sent", "results": results}
        finally:
            # An action changes peer phases: the next snapshot must read them anew.
            _peer_status.clear()


async def _dispatch(action, mode=None, scope=None):
    global _refusals_at
    if action not in ("check", "download", "install", "cancel"):
        raise ValueError("Unknown update action")
    if mode not in (None, "when_idle", "stop") or (mode and action != "install"):
        raise ValueError("Unknown update mode")
    if scope not in (None, "current"):
        raise ValueError("Unknown update scope")
    if action == "cancel":
        return await _send_cancel(scope)
    if not await run_sync(updates.allowed):
        raise UpdateNotSent("Application updates are disabled in Account settings")
    result = await _send(action, mode, scope)
    refused = {row["device_id"]: row.get("reason") or "Refused" for row in result["results"]
               if not row.get("accepted") and row.get("device_id")}
    _refusals.clear()
    _refusals.update(refused)
    _refusals_at = time.time()
    for row in result["results"]:
        logger.info("Application update action=%s mode=%s scope=%s for %s: %s (%s)",
                    result.get("sent", action), mode, result.get("scope"), row["device_name"],
                    "accepted" if row.get("accepted") else "refused", row.get("reason"))
    return result


async def _in_scope(scope):
    """Whether an action fans out to every worker: the policy, unless the caller asked for this one."""
    return scope != "current" and await run_sync(updates.all_workers)


async def _send_cancel(scope=None):
    """Drop a waiting "Restart when they finish" on every worker in scope."""
    listing = await run_sync(runners.directory)
    request_id = uuid.uuid4().hex
    results = []
    all_workers = await _in_scope(scope)
    if all_workers:
        results = list(await asyncio.gather(*(_act(row, "cancel", request_id) for row in listing["runners"]
                                             if not row["is_self"] and row["online"])))
    self_row = {"device_id": listing.get("self_device_id") or "local", "device_name": "Current worker"}
    results.append({**self_row, **local_action("cancel", request_id)})
    return {"scope": "all" if all_workers else "current", "sent": "cancel", "results": results}


def _later(result):
    """A worker that will not restart now: refused while busy, or waiting for its work to end."""
    return result.get("accepted") is False and bool(result.get("busy")) or bool(result.get("deferred"))


async def _send(action, mode=None, scope=None):
    operation = await run_sync(progress.load)
    if operation and operation["phase"] != "complete":
        previous = await snapshot()
        if previous.get("operation") and previous["operation"]["phase"] == "waiting":
            raise UpdateNotSent("Waiting on workers to finish the previous update")
        if previous.get("operation") and previous["operation"]["phase"] == "error" and action != "install":
            # A new check or download moves past a failed install. Live worker
            # errors still show on their rows; the old error could otherwise
            # stay on the button for good when its target can never run (the
            # worker was reinstalled).
            await run_sync(progress.discard, operation["id"])
    listing = await run_sync(runners.directory)
    all_workers = await _in_scope(scope)
    request_id = uuid.uuid4().hex
    self_row = {"device_id": listing.get("self_device_id") or "local", "device_name": "Current worker"}
    if action == "install" and all_workers:
        # Never restart part of the fleet: a worker that still has to download
        # (often one that has not checked yet) is sent the download instead,
        # and nothing installs until every included worker is staged.
        current = await snapshot(fresh=True)
        if not current["plan"]["ready"]:
            if mode in ("stop", "when_idle"):
                raise UpdateNotSent(current["plan"]["held"] or "Not every worker is staged yet; nothing was installed")
            check_first = [row for row in current["workers"] if row["role"] == "available" and _check_first(row)]
            behind = check_first or [row for row in current["workers"] if row["role"] == "available"]
            if not behind:
                raise UpdateNotSent(current["plan"]["held"] or "Not every worker is staged yet; nothing was installed")
            action = "check" if check_first else "download"
            results = await asyncio.gather(*(_act(row, action, request_id) for row in behind))
            return {"scope": "all", "sent": action,
                    "held": current["plan"]["held"] or "Not every worker is staged yet; nothing was installed",
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
                        await run_sync(progress.record, request_id, row["device_id"], target_version=state.get("version"),
                                       from_version=state.get("running_version"))
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
        await run_sync(progress.record, request_id, self_row["device_id"], target_version=state.get("version"),
                       from_version=state.get("running_version"))
    local = await _local_action(action, request_id, mode)
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
        result = await _local_action(action, request_id)
    else:
        result = await _remote(row, {"operation": "action", "action": action, "request_id": request_id,
                                     "expires_at": time.time() + 10})
    return {"device_id": row["device_id"], "device_name": row["device_name"], **result}
