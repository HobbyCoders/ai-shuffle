"""Shared account update policy and local container updater control.

Only the supervisor selects code. The API can enqueue a fixed action, never a
path, URL or command. No Docker socket or elevated container permissions.
"""
import json
import os
from pathlib import Path
import re
import tempfile
import time
import threading

from app.db import database

KEY = "allow_updates"
ALL_KEY = "update_all_workers"
_bridge_lock = threading.Lock()
_desktop_state = {}
_desktop_seen = 0.0
_desktop_request = None
# A download handed to a desktop shell that has not found the update yet: the
# shell is sent a check and gets the download once it reports "available".
_desktop_followup = None
# The desktop shell reports every 30 s (BRIDGE_INTERVAL_MS in
# desktop/src/main/updater.ts) and at once on a phase change. One missed
# report means about 60 s of silence, so presence spans two intervals plus
# slack; a queued request outlives presence, so an accepted request is never
# dropped while the updater still counts as present.
DESKTOP_PRESENCE_SECONDS = 75
DESKTOP_REQUEST_SECONDS = 90
# The container supervisor, unlike the desktop shell, polls request.json about
# once a second (watch() in docker/container_runtime.py starts a service pass
# each second while the app is ready and no pass is running), so 10 s covers
# idle polling several times over. It is deliberately not widened to the
# desktop's 90 s: a request that lands while a check or download is running,
# or while the readiness probe is failing, should expire rather than replay
# minutes later (docker/application_updates.py service() never replays old
# requests); the UI then shows the finished pass and the owner can retry.
CONTAINER_REQUEST_SECONDS = 10
# How long a desktop check may run before the download chained to it is dropped.
DESKTOP_FOLLOWUP_SECONDS = 120
# An idle or ready updater is not doing anything. Older container supervisors
# leave "Application update is running." on the idle status after an install,
# which reads as work in progress, so a message like that is dropped.
IN_PROGRESS = re.compile(r"\b(?:update is running|checking|downloading|building|preparing|installing)\b", re.I)


def _desktop_present():
    return bool(_desktop_seen) and time.monotonic() - _desktop_seen < DESKTOP_PRESENCE_SECONDS


def all_workers():
    return database.get_system_setting(ALL_KEY) == "true"


def desktop_exchange(state):
    """Only called by the token-authenticated local launcher, never a browser."""
    global _desktop_state, _desktop_seen, _desktop_request, _desktop_followup
    with _bridge_lock:
        _desktop_state = dict(state)
        _desktop_seen = time.monotonic()
        # A phase-change report can race an already accepted action. The shell
        # refuses actions while checking or downloading, so leave the request
        # queued until it can accept it, without extending its original expiry.
        if (_desktop_request and _desktop_request["expires_at"] >= time.time()
                and state.get("phase") in ("checking", "downloading")):
            return {}
        request, _desktop_request = _desktop_request, None
        if request is None and _desktop_followup:
            # The check this download waits on was handed over. Keep the
            # download while the check runs, send it once an update is found,
            # and drop it when the check finds nothing, fails or runs too long.
            phase = state.get("phase")
            if _desktop_followup["expires_at"] < time.time() or phase not in ("checking", "available"):
                _desktop_followup = None
            elif phase == "available":
                _desktop_followup = None
                request = {"action": "download", "expires_at": time.time() + DESKTOP_REQUEST_SECONDS}
    # Most reports carry nothing back: answer those without the settings read.
    if not request or request["expires_at"] < time.time() or not allowed():
        if request:
            with _bridge_lock:
                _desktop_followup = None  # its check was never sent
        return {}
    return request


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".update-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def allowed():
    # No row means enabled; a corrupt value is not permission to execute code.
    value = database.get_system_setting(KEY)
    return value is None or value == "true"


def mirror_policy():
    global _desktop_request, _desktop_followup
    enabled = allowed()
    if not enabled:
        with _bridge_lock:
            _desktop_request = _desktop_followup = None
    path = os.environ.get("SHUFFLE_UPDATE_POLICY_FILE")
    if path:
        atomic_json(path, {"allow_updates": enabled})
    if not enabled and control_root():
        (control_root() / "request.json").unlink(missing_ok=True)
    return enabled


def control_root():
    value = os.environ.get("SHUFFLE_CONTAINER_UPDATES")
    return Path(value) if value else None


def status():
    root = control_root()
    result = {"allow_updates": mirror_policy(), "update_all_workers": all_workers(), "supported": root is not None,
              "phase": "idle", "version": None, "error": None, "request_pending": False,
              "running_version": os.environ.get("GIT_SHA") if root else None}
    if root:
        try:
            saved = json.loads((root / "status.json").read_text())
            result.update({key: saved.get(key) for key in ("phase", "version", "error", "message",
                                                           "release", "notes", "channel")})
        except FileNotFoundError:
            result["message"] = "Starting application updater…"
        except (ValueError, TypeError, AttributeError, OSError):
            result.update(phase="error", error="Cannot read application updater status. Check the container logs.")
        # The supervisor deletes request.json when it takes the request.
        result["request_pending"] = (root / "request.json").exists()
    else:
        with _bridge_lock:
            if _desktop_present():
                result.update(_desktop_state)
                result["supported"] = _desktop_state.get("phase") != "unsupported"
                result["kind"] = "desktop"
                result["request_pending"] = any(queued and queued["expires_at"] >= time.time()
                                                for queued in (_desktop_request, _desktop_followup))
    if result.get("phase") in ("idle", "ready") and IN_PROGRESS.search(str(result.get("message") or "")):
        result["message"] = None
    return result


def enqueue(action):
    global _desktop_request, _desktop_followup
    if action not in ("check", "download", "install"):
        raise ValueError("Unknown update action")
    if not allowed():
        raise ValueError("Application updates are disabled in Account settings")
    root = control_root()
    if root:
        atomic_json(root / "request.json", {"action": action, "expires_at": time.time() + CONTAINER_REQUEST_SECONDS})
        return
    request = {"action": action, "expires_at": time.time() + DESKTOP_REQUEST_SECONDS}
    with _bridge_lock:
        if not _desktop_present() or _desktop_state.get("phase") == "unsupported":
            raise ValueError("This worker's application updater is unavailable")
        if _desktop_request and _desktop_request["expires_at"] >= time.time():
            raise ValueError("An update request is already queued")
        _desktop_followup = None
        if action == "download" and _desktop_state.get("phase") == "idle":
            # The shell only downloads an update it has already found.
            request["action"] = "check"
            _desktop_followup = {"action": "download", "expires_at": request["expires_at"] + DESKTOP_FOLLOWUP_SECONDS}
        _desktop_request = request
