"""Local, durable observation of an install, never a queue of restart commands."""
import json
import re
import threading
import time

from app.core.config import settings
from app.core.application_updates import atomic_json

_lock = threading.RLock()
STARTUP_TIMEOUT = 10 * 60


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
    value = {"id": request_id, "started_at": time.time(), "phase": "waiting", "workers": [
        {"device_id": row["device_id"], "device_name": row["device_name"],
         "target_version": None, "accepted": None} for row in rows
    ]}
    with _lock:
        atomic_json(_path(), value)


def record(request_id, device_id, *, target_version=None, result=None):
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
            if result is not None:
                row["accepted"] = result.get("accepted") if isinstance(result.get("accepted"), bool) else None
                row["reason"] = result.get("reason", "")
                if result.get("version"):
                    row["target_version"] = result["version"]
        atomic_json(_path(), value)


def same_version(running, target):
    # Desktop displays a 12-character commit, while containers use the full SHA.
    return (isinstance(running, str) and isinstance(target, str)
            and re.fullmatch(r"[a-f0-9]{7,64}", running) is not None
            and re.fullmatch(r"[a-f0-9]{7,64}", target) is not None
            and (running.startswith(target) or target.startswith(running)))


def observe(operation, rows):
    if not operation:
        return None
    # Once complete, a later disconnect cannot restart the celebration.
    if operation["phase"] == "complete":
        return operation
    live = {row["device_id"]: row for row in rows}
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
        elif target["accepted"] is False:
            worker.update(status="skipped", message=target.get("reason") or "Not queued")
        elif current.get("error"):
            worker.update(status="error", message=current["error"])
        else:
            worker.update(status="waiting", message=("Verifying updated version…" if current.get("supported")
                                                      else "Waiting for startup…"))
        workers.append(worker)
    included = [worker for worker in workers if worker["status"] != "skipped"]
    if included and all(worker["status"] == "verified" for worker in included):
        phase = "complete"
    elif not included or any(worker["status"] == "error" for worker in included):
        phase = "error"
    elif time.time() - operation["started_at"] >= STARTUP_TIMEOUT:
        phase = "error"
        for worker in included:
            if worker["status"] == "waiting":
                worker.update(status="error", message="Startup not confirmed. Check this worker before retrying.")
    else:
        phase = "waiting"
    result = {**operation, "phase": phase, "workers": workers}
    if phase == "complete":
        # The UI only celebrates a recent completion, never one replayed from
        # history into a fresh browser.
        result["completed_at"] = time.time()
        with _lock:
            current = load()
            if current and current["id"] == operation["id"]:
                atomic_json(_path(), result)
    return result
