"""Local, durable observation of an install, never a queue of restart commands."""
import json
import re
import threading
import time

from app.core.config import settings
from app.core.application_updates import atomic_json

_lock = threading.RLock()
STARTUP_TIMEOUT = 10 * 60
# Records saved before ``from_version`` existed cannot tell a failed start from
# a worker that moved on. After this long, a worker running another version
# than the target has moved on as far as this install is concerned.
LEGACY_SETTLE = 24 * 60 * 60


def _path():
    return settings.data_dir / "application-update-progress.json"


def load():
    with _lock:
        try:
            value = json.loads(_path().read_text(encoding="utf-8"))
            if (isinstance(value, dict) and isinstance(value.get("id"), str)
                    and isinstance(value.get("started_at"), (int, float))
                    and isinstance(value.get("workers"), list)):
                return value
        except (OSError, ValueError):
            pass
    return None


def begin(request_id, rows):
    # from_version is present from the start (None until recorded), so a record
    # without the key is one saved before it existed (moved_on).
    value = {"id": request_id, "started_at": time.time(), "phase": "waiting", "workers": [
        {"device_id": row["device_id"], "device_name": row["device_name"],
         "target_version": None, "from_version": None, "accepted": None} for row in rows
    ]}
    with _lock:
        atomic_json(_path(), value)


def discard(request_id):
    """Forget an operation that never started any install (every worker busy),
    or a failed one the owner moved past by asking for a new check."""
    with _lock:
        value = load()
        if value and value["id"] == request_id:
            _path().unlink(missing_ok=True)


def record(request_id, device_id, *, target_version=None, from_version=None, result=None):
    # Persist the target BEFORE enqueueing. A lost reply must not lose the cohort.
    with _lock:
        value = load()
        if not value or value["id"] != request_id:
            return
        for row in value["workers"]:
            if row["device_id"] != device_id:
                continue
            if target_version:
                row["target_version"] = target_version
            if from_version:
                row["from_version"] = from_version
            if result is not None:
                row["accepted"] = result.get("accepted") if isinstance(result.get("accepted"), bool) else None
                row["reason"] = result.get("reason", "")
                if result.get("version"):
                    row["target_version"] = result["version"]
        atomic_json(_path(), value)


def _commit(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{7,64}", value) is not None


def same_version(running, target):
    # Desktop displays a 12-character commit, while containers use the full SHA.
    return (_commit(running) and _commit(target)
            and (running.startswith(target) or target.startswith(running)))


def replaced(current, target):
    """The worker now runs a third version: installed another way (an installer,
    a new image, a later update), so this install has nothing left to do.

    Needs the version it ran when the install began; without it a failed start
    (still on the old version) cannot be told apart, so the error stands.
    """
    running, before = current.get("running_version"), target.get("from_version")
    return (bool(current.get("online") and current.get("supported")) and _commit(running) and _commit(before)
            and not same_version(running, before) and not same_version(running, target.get("target_version")))


def superseded(current, target):
    """The worker offers, downloads or has staged a different update: a failed
    install of the old target has nothing left to do, and its error must not
    hide the newer update's own button state."""
    offered = current.get("version")
    return (bool(current.get("online") and current.get("supported"))
            and current.get("phase") in ("available", "downloading", "ready")
            and _commit(offered) and _commit(target.get("target_version"))
            and not same_version(offered, target.get("target_version")))


def moved_on(current, target, age):
    """A record saved before ``from_version`` existed, settled long ago, on a
    worker that runs another version than its target. A current record whose
    starting version was unknown keeps its error: that may be a failed start."""
    running = current.get("running_version")
    return (age >= LEGACY_SETTLE and "from_version" not in target
            and bool(current.get("online") and current.get("supported")) and _commit(running)
            and _commit(target.get("target_version")) and not same_version(running, target["target_version"]))


def put_back(current, target):
    """A desktop back on its previous version, offering this install's target
    again because its first start did not finish (``retry_start``). The
    install is over; waiting longer would only hold the next request back."""
    return (bool(current.get("online") and current.get("retry_start") is True)
            and same_version(current.get("version"), target.get("target_version"))
            and not same_version(current.get("running_version"), target.get("target_version")))


def observe(operation, rows):
    if not operation:
        return None
    # Once complete, a later disconnect cannot restart the celebration.
    if operation["phase"] == "complete":
        return operation
    live = {row["device_id"]: row for row in rows}
    age = time.time() - operation["started_at"]
    workers = []
    for target in operation["workers"]:
        current = live.get(target["device_id"], {})
        worker = dict(target)
        # A duplicate request can be refused while an earlier install succeeds.
        # Verify the running commit before considering that request's receipt;
        # accepting a command is not required to observe its target running.
        if (current.get("online") and current.get("supported")
                and same_version(current.get("running_version"), target.get("target_version"))):
            worker.update(status="verified", message="Updated and verified")
        elif replaced(current, target) or moved_on(current, target, age):
            worker.update(status="replaced", message=f"Now runs {current['running_version'][:12]}; nothing left to install")
        elif put_back(current, target):
            worker.update(status="error", message="The update did not finish starting, so this worker went back to "
                                                  "its previous version with its data as it was. Download it to try again.")
        elif target["accepted"] is False:
            worker.update(status="skipped", message=target.get("reason") or "Not queued")
        elif (current.get("error") or age >= STARTUP_TIMEOUT) and superseded(current, target):
            # Only once this install has failed or timed out: while it runs, a
            # newer offer does not end the wait for its restart.
            worker.update(status="replaced", message=f"Update {current['version'][:12]} replaces this one")
        elif current.get("error"):
            worker.update(status="error", message=current["error"])
        else:
            worker.update(status="waiting", message=("Verifying updated version…" if current.get("supported")
                                                      else "Waiting for startup…"))
        workers.append(worker)
    included = [worker for worker in workers if worker["status"] != "skipped"]
    if included and all(worker["status"] in ("verified", "replaced") for worker in included):
        phase = "complete"
    elif not included or any(worker["status"] == "error" for worker in included):
        phase = "error"
    elif age >= STARTUP_TIMEOUT:
        phase = "error"
        for worker in included:
            if worker["status"] == "waiting":
                worker.update(status="error", message="Startup not confirmed. Check this worker before retrying.")
    else:
        phase = "waiting"
    result = {**operation, "phase": phase, "workers": workers}
    if phase == "complete":
        # The UI only celebrates a recent completion, never one replayed from
        # history into a fresh browser, and never one this install did not make.
        if any(worker["status"] == "verified" for worker in included):
            result["completed_at"] = time.time()
        with _lock:
            current = load()
            if current and current["id"] == operation["id"]:
                atomic_json(_path(), result)
    return result
