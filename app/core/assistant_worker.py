"""Where the assistant's work runs: one owner preference, a worker or this one.

The preference is the device id of another worker of this account, stored in
``user_preferences`` (a synced collection) under the owner's identity, or
absent for "this worker". It applies only to the assistant home and the jobs
it started (``assistant_limits.role``): a Task or Swarm item from those that
does not name ``worker`` is sent to that worker. Every other chat runs here
unless its model names a worker.

Every worker keeps its own assistant home, so the key carries this worker's
device id: the choice made on one worker's Assistant page steers that
worker's home only. A device id that is no longer an active worker of the
account reads as "this worker".
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

PREFERENCE_KEY = "assistant.work_worker"


def _identity() -> tuple[str, str]:
    from app.core.preference_identity import principal_preference_identity

    return principal_preference_identity({"kind": "admin"})


def _key() -> Optional[str]:
    """This worker's preference key, or None when it is not networked."""
    from app.network import engine

    ctx = engine.context()
    return f"{PREFERENCE_KEY}:{ctx.device_id}" if ctx is not None else None


def _stored() -> Optional[str]:
    from app.db import settings as prefs

    key = _key()
    row = prefs.get_user_preference(*_identity(), key) if key else None
    value = row.get("value") if row else None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _can_run_remote(row: dict) -> bool:
    try:
        return int((row.get("capabilities") or {}).get("remote_task") or 0) >= 1
    except (TypeError, ValueError):
        return False


def _choice(rows: list[dict]) -> Optional[dict]:
    stored = _stored()
    return next((row for row in rows if row["device_id"] == stored), None) if stored else None


def read() -> dict[str, Any]:
    """The current choice and the account's other active workers.

    ``device_id`` is None for "this worker", including when the stored id
    names a worker that is gone or is this worker itself.
    """
    from app.network import runners

    rows = runners.other_workers()
    chosen = _choice(rows)
    return {
        "device_id": chosen["device_id"] if chosen else None,
        "workers": [
            {
                "device_id": row["device_id"],
                "name": str(row.get("device_name") or row["device_id"]),
                "online": bool(row.get("online")),
                "remote_task": _can_run_remote(row),
            }
            for row in sorted(rows, key=lambda r: (str(r.get("device_name") or "").casefold(), r["device_id"]))
        ],
    }


def write(device_id: Optional[str]) -> dict[str, Any]:
    """Save the choice; empty or this worker's own id means "this worker".

    The id goes through the shared worker resolver, so an unknown, inactive
    or offline worker is refused (``ValueError``) and nothing is saved. A
    worker whose release cannot run remote subagents is refused as well.
    """
    from app.db import settings as prefs
    from app.network import runners

    key = _key()
    if device_id is None or not str(device_id).strip():
        if key:
            prefs.delete_user_preference(*_identity(), key)
        return read()
    row = runners.resolve_worker(
        str(device_id).strip(), instead="choose this worker", refused="it was not saved"
    )
    if row.get("is_self"):
        if key:
            prefs.delete_user_preference(*_identity(), key)
        return read()
    if not _can_run_remote(row):
        raise ValueError(f"{row['device_name']} needs an update before it can run the assistant's work.")
    if key is None:  # Unreachable: resolve_worker refuses without a network.
        raise ValueError("This worker is not part of a worker network.")
    prefs.set_user_preference(*_identity(), key, row["device_id"])
    return read()


def default_for(session_id: str) -> Optional[dict]:
    """The directory row of the worker ``session_id``'s Task calls default to.

    None unless ``session_id`` is the assistant home or a job it started and
    the owner chose another worker that is still an active member. Never
    raises: a lookup failure means the work runs here, as it always did.
    """
    try:
        from app.core import assistant_limits
        from app.network import runners

        if assistant_limits.role(session_id) is None:
            return None
        return _choice(runners.other_workers())
    except Exception:  # noqa: BLE001 - the default must never break dispatch
        logger.warning("Assistant work worker: lookup failed for %s", session_id, exc_info=True)
        return None
