"""Agent-requested hand-off: "wrap up and continue this on DesktopWorker".

The owner used to ask "find a stop point, commit and push so I can pick up on
another machine", then move the session by hand. ``HandOffSession`` lets the
agent queue that: it validates the target worker and stores the request; when
the turn (and any background work) has finished, the app runs the ordinary
move (``app.api.sessions.move_session_to``): commit and push the branch through
Git, sync the target's checkout and context, then transfer ownership. A
hand-off note and the outcome are recorded in the session itself, never in a
file the owner cannot reach.

Pending requests live in memory on the owning worker: a restart drops them and
the session simply stays where it is.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# How long a queued hand-off waits for the turn and background work to end.
IDLE_TIMEOUT_S = 600
POLL_S = 2.0
SUBTYPE = "handoff"


@dataclass
class PendingHandoff:
    target_id: str
    label: str
    note: str
    requested_at: float


_pending: Dict[str, PendingHandoff] = {}
_running: set[str] = set()


def _workers() -> Dict[str, Any]:
    from app.network import runners

    return runners.directory()


def resolve_worker(worker: str) -> Dict[str, Any]:
    """The online worker named by label or device id. Raises ``ValueError``."""
    from app.network import runners

    wanted = (worker or "").strip()
    if not wanted:
        raise ValueError("Name the worker to hand off to (its name as shown in the app, or its device id).")
    row = runners.resolve_worker(wanted, instead="pick another worker", refused="nothing was queued",
                                 listing=_workers())
    if row.get("is_self"):
        raise ValueError(f"{row.get('device_name')} is this worker; name another worker to hand off to.")
    return row


def request(session_id: str, worker: str, note: str = "") -> Dict[str, Any]:
    """Queue a hand-off of ``session_id`` to ``worker`` for when the turn ends."""
    row = resolve_worker(worker)
    pending = PendingHandoff(row["device_id"], str(row.get("device_name") or row["device_id"]),
                             " ".join((note or "").split())[:2000], time.time())
    _pending[session_id] = pending
    return {"queued": True, "worker": pending.label,
            "message": (f"Hand-off to {pending.label} is queued. Finish this turn now with a short "
                        "summary of where the work stands; once the turn and any background work end, "
                        "the app commits and pushes the branch and moves this session there.")}


def pending_for(session_id: str) -> Optional[PendingHandoff]:
    return _pending.get(session_id)


def cancel(session_id: str) -> bool:
    return _pending.pop(session_id, None) is not None


async def _record(session_id: str, content: str, data: Dict[str, Any]) -> None:
    try:
        from app.core.turn_recorder import record_drained_event

        await record_drained_event(session_id, {"type": "system", "subtype": SUBTYPE,
                                                "content": content, "data": {**data, "content": content}})
    except Exception:  # noqa: BLE001 — the row is a report
        logger.warning("Could not record hand-off row for %s", session_id, exc_info=True)


def _busy(session_id: str) -> bool:
    from app.core import streaming_status
    from app.core.task_registry import task_registry

    return bool(streaming_status.has_active_run(session_id)
                or task_registry.get_active_background_work(session_id))


async def _wait_idle(session_id: str) -> bool:
    deadline = time.monotonic() + IDLE_TIMEOUT_S
    while time.monotonic() < deadline:
        if not await asyncio.to_thread(_busy, session_id):
            return True
        await asyncio.sleep(POLL_S)
    return False


async def run_pending(session_id: str) -> Optional[str]:
    """Run a queued hand-off once ``session_id`` is idle. Returns the outcome text."""
    pending = _pending.pop(session_id, None)
    if pending is None or session_id in _running:
        return None
    _running.add(session_id)
    try:
        if not await _wait_idle(session_id):
            outcome = f"Hand-off to {pending.label} skipped: the session was still busy after {IDLE_TIMEOUT_S // 60} minutes."
            await _record(session_id, outcome, {"status": "timeout", "worker": pending.label})
            return outcome
        note = f" Note: {pending.note}" if pending.note else ""
        await _record(session_id, f"Handing off to {pending.label}.{note}",
                      {"status": "started", "worker": pending.label, "note": pending.note})
        from fastapi import HTTPException

        from app.api.sessions import move_session_to
        from app.db import database

        session = await asyncio.to_thread(database.get_session, session_id)
        try:
            await move_session_to(session_id, session or {}, pending.target_id)
        except HTTPException as exc:
            outcome = f"Hand-off to {pending.label} failed: {exc.detail}"
            await _record(session_id, outcome, {"status": "failed", "worker": pending.label})
            return outcome
        outcome = f"Handed off to {pending.label}. Open this chat there to continue on the same branch."
        await _record(session_id, outcome, {"status": "moved", "worker": pending.label})
        return outcome
    except Exception as exc:  # noqa: BLE001 — never break turn teardown
        logger.warning("Hand-off failed for %s", session_id, exc_info=True)
        outcome = f"Hand-off to {pending.label} failed: {exc}"
        await _record(session_id, outcome, {"status": "failed", "worker": pending.label})
        return outcome
    finally:
        _running.discard(session_id)


def schedule(session_id: str) -> None:
    """Turn-end hook: start a queued hand-off in the background (never blocks)."""
    if session_id in _pending and session_id not in _running:
        asyncio.get_running_loop().create_task(run_pending(session_id))
