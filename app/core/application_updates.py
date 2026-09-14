"""Shared account update policy and local container updater control.

Only the supervisor selects code. The API can enqueue a fixed action, never a
path, URL or command. No Docker socket or elevated container permissions.
"""
import json
import os
from pathlib import Path
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


def all_workers():
    return database.get_system_setting(ALL_KEY) == "true"


def desktop_exchange(state):
    """Only called by the token-authenticated local launcher, never a browser."""
    global _desktop_state, _desktop_seen, _desktop_request
    with _bridge_lock:
        _desktop_state = dict(state)
        _desktop_seen = time.monotonic()
        request, _desktop_request = _desktop_request, None
    if not allowed() or not request or request["expires_at"] < time.time():
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
    global _desktop_request
    enabled = allowed()
    if not enabled:
        with _bridge_lock:
            _desktop_request = None
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
              "phase": "idle", "version": None, "error": None,
              "running_version": os.environ.get("GIT_SHA") if root else None}
    if root:
        try:
            saved = json.loads((root / "status.json").read_text())
            result.update({key: saved.get(key) for key in ("phase", "version", "error", "message")})
        except FileNotFoundError:
            result["message"] = "Starting application updater…"
        except (ValueError, TypeError, AttributeError, OSError):
            result.update(phase="error", error="Cannot read application updater status. Check the container logs.")
    else:
        with _bridge_lock:
            if _desktop_seen and time.monotonic() - _desktop_seen < 10:
                result.update(_desktop_state)
                result["supported"] = _desktop_state.get("phase") != "unsupported"
                result["kind"] = "desktop"
    return result


def enqueue(action):
    global _desktop_request
    if action not in ("check", "download", "install"):
        raise ValueError("Unknown update action")
    if not allowed():
        raise ValueError("Application updates are disabled in Account settings")
    root = control_root()
    request = {"action": action, "expires_at": time.time() + 10}
    if root:
        atomic_json(root / "request.json", request)
        return
    with _bridge_lock:
        if not _desktop_seen or time.monotonic() - _desktop_seen >= 10 or _desktop_state.get("phase") == "unsupported":
            raise ValueError("This worker's application updater is unavailable")
        if _desktop_request and _desktop_request["expires_at"] >= time.time():
            raise ValueError("An update request is already queued")
        _desktop_request = request
