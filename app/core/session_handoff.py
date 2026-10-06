"""Agent-requested hand-off: "wrap up and continue this on DesktopWorker".

The owner used to ask "find a stop point, commit and push so I can pick up on
another machine", then move the session by hand. ``HandOffSession`` lets the
agent queue that: it validates the target worker and stores the request; when
the turn (and any background work) has finished, the app runs the ordinary
move (``app.api.sessions.move_session_to``): commit and push the branch through
Git, sync the target's checkout and context, then transfer ownership. A
hand-off note and the outcome are recorded in the session itself, never in a
file the owner cannot reach.

The owner queues the same hand-off when a chat is running or parked (#1819):
the worker selector and "Resume here" offer "Move when this turn ends", which
calls ``queue_move`` on the owning worker. An owner's move waits for as long as
the work runs, until it moves or is cancelled; the agent's waits IDLE_TIMEOUT_S.
"Stop and move now" stops the work as the Stop button does, then moves.

Pending requests live in memory on the owning worker: a restart drops them and
the session simply stays where it is. Each one is also noted in a small file in
the data dir, so the next boot records a "cancelled" row in every chat whose
queued move the restart dropped (``announce_dropped``); otherwise the chat kept
showing a queued move that nothing would ever run.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
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
    # "agent" (HandOffSession) or "owner" (worker selector / Resume here).
    by: str = "agent"
    cancelled: bool = False


_pending: Dict[str, PendingHandoff] = {}
_running: set[str] = set()
# Hand-offs taken from _pending that are waiting for the session to go idle.
_waiting: Dict[str, PendingHandoff] = {}
_store_lock = threading.Lock()


def _store_path():
    from app.core.config import settings

    return settings.data_dir / "pending-handoffs.json"


def _load_store() -> Dict[str, Dict[str, str]]:
    """``{session_id: {"worker", "by"}}`` for the queued hand-offs on disk."""
    try:
        value = json.loads(_store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {str(sid): {"worker": str(entry.get("worker") or ""), "by": str(entry.get("by") or "agent")}
            for sid, entry in value.items() if isinstance(entry, dict)}


def _update_store(session_id: str, pending: Optional[PendingHandoff]) -> None:
    """Note (or forget, when ``pending`` is None) a queued hand-off on disk."""
    try:
        from app.core.application_updates import atomic_json

        with _store_lock:
            stored = _load_store()
            if pending is None:
                if stored.pop(session_id, None) is None:
                    return
            else:
                stored[session_id] = {"worker": pending.label, "by": pending.by}
            path = _store_path()
            if stored:
                atomic_json(path, stored)
            else:
                path.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 — the note only improves the post-restart chat
        logger.warning("Could not note the queued hand-off for %s", session_id, exc_info=True)


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
    from app.core import archive_guard

    if archive_guard.is_archived(session_id):
        raise ValueError(archive_guard.refusal("This chat", "be handed off to another worker"))
    row = resolve_worker(worker)
    pending = PendingHandoff(row["device_id"], str(row.get("device_name") or row["device_id"]),
                             " ".join((note or "").split())[:2000], time.time())
    _pending[session_id] = pending
    _update_store(session_id, pending)
    return {"queued": True, "worker": pending.label,
            "message": (f"Hand-off to {pending.label} is queued. Finish this turn now with a short "
                        "summary of where the work stands; once the turn and any background work end, "
                        "the app commits and pushes the branch and moves this session there.")}


def pending_for(session_id: str) -> Optional[PendingHandoff]:
    return _pending.get(session_id) or _waiting.get(session_id)


def cancel(session_id: str) -> bool:
    if _pending.pop(session_id, None) is not None:
        _update_store(session_id, None)
        return True
    waiting = _waiting.get(session_id)
    if waiting is None or waiting.cancelled:
        return False
    waiting.cancelled = True
    _update_store(session_id, None)
    return True


async def _record(session_id: str, content: str, data: Dict[str, Any]) -> None:
    try:
        from app.core.turn_recorder import record_drained_event

        await record_drained_event(session_id, {"type": "system", "subtype": SUBTYPE,
                                                "content": content, "data": {**data, "content": content}})
    except Exception:  # noqa: BLE001 — the row is a report
        logger.warning("Could not record hand-off row for %s", session_id, exc_info=True)


def busy_state(session_id: str) -> Dict[str, Any]:
    """What keeps ``session_id`` from moving now: ``{"running", "tasks"}``.

    ``running`` is a live turn or one waiting in the turn queue; ``tasks``
    names its running background tasks (subagents, Monitors, background
    shells). The handoff's own idle gate refuses a move while any of them runs
    or a turn is queued, so a queued turn must hold the hand-off too: treated
    as idle, the move started and was refused instead of waiting.
    """
    from app.core import streaming_status
    from app.core.task_registry import task_registry
    from app.db import turns as turns_db

    tasks = [str(task.get("subject") or task.get("agent") or "Background task")
             for task in task_registry.get_active(session_id)]
    try:
        # One SELECT over queued/running/interrupting/draining, so a turn
        # flipping from queued to running is never missed between two reads.
        unsettled = bool(turns_db.session_has_unsettled_turns(session_id))
    except Exception:
        logger.warning("Could not read the turn queue for %s", session_id, exc_info=True)
        unsettled = False
    running = unsettled or bool(streaming_status.has_active_run(session_id))
    return {"running": running, "tasks": tasks}


def _busy(session_id: str) -> bool:
    state = busy_state(session_id)
    return bool(state["running"] or state["tasks"])


async def _wait_idle(session_id: str, pending: PendingHandoff) -> bool:
    # The owner chose to wait for the work, however long it runs.
    deadline = None if pending.by == "owner" else time.monotonic() + IDLE_TIMEOUT_S
    while deadline is None or time.monotonic() < deadline:
        if pending.cancelled:
            return False
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
    _waiting[session_id] = pending
    try:
        idle = await _wait_idle(session_id, pending)
        _waiting.pop(session_id, None)
        if pending.cancelled:
            return None
        if not idle:
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
        _waiting.pop(session_id, None)
        _running.discard(session_id)
        if session_id not in _pending:
            _update_store(session_id, None)
        # A move queued while a cancelled one was still winding down.
        schedule(session_id)


def _owner_state(session_id: str) -> Dict[str, Any]:
    pending = pending_for(session_id)
    return {"busy": busy_state(session_id),
            "pending": ({"worker": pending.label, "target_id": pending.target_id, "by": pending.by}
                        if pending and not pending.cancelled else None)}


async def queue_move(session_id: str, target: str, *, stop: bool = False) -> Dict[str, Any]:
    """The owner's "Move when this turn ends" (#1819), on the owning worker.

    Queues the same hand-off HandOffSession does and starts waiting at once:
    the move runs when the turn and its background work end. ``stop`` stops
    that work first, as the Stop button does ("Stop and move now").
    """
    from app.network import runners

    current = pending_for(session_id)
    if current is not None and not current.cancelled:
        raise ValueError(f"A move to {current.label} is already queued. Cancel it first.")
    row = await asyncio.to_thread(resolve_worker, runners.resolve_runner_id(target) or target)
    label = str(row.get("device_name") or row["device_id"])
    pending = PendingHandoff(row["device_id"], label, "", time.time(), by="owner")
    _pending[session_id] = pending
    await asyncio.to_thread(_update_store, session_id, pending)
    await _record(session_id, f"Move to {label} queued for when this turn ends.",
                  {"status": "queued", "worker": label, "by": "owner"})
    if stop:
        from app.core.desktop_lifecycle import stop_session_work

        await stop_session_work(session_id, source="session_handoff")
    # Start waiting now: a parked chat has no turn end left to start it.
    schedule(session_id)
    return {"queued": True, "worker": label}


async def cancel_move(session_id: str) -> Dict[str, Any]:
    """Drop a queued move, the owner's or the agent's, and say so in the chat.

    The row is recorded even when nothing was queued here (a worker restart
    drops requests), so a chat that still shows a queued move clears it.
    """
    pending = pending_for(session_id)
    cancelled = cancel(session_id)
    label = pending.label if pending else ""
    target = f" to {label}" if label else ""
    await _record(session_id, f"Queued move{target} cancelled.",
                  {"status": "cancelled", "worker": label, "by": "owner"})
    return {"cancelled": cancelled}


async def handle_control(body: Dict[str, Any], from_device: str) -> Dict[str, Any]:
    """The ``session-move`` control action: another worker's UI asks the owner."""
    from app.db.async_database import run_sync
    from app.network import control, engine

    member = engine.require_context().member(from_device)
    if not member or member.kind != "worker" or member.status != "active":
        raise PermissionError("Only active account workers may queue a move.")
    session_id = str(body.get("session_id") or "")
    session = await run_sync(control._require_owned, session_id)
    op = str(body.get("op") or "")
    if op == "queue":
        from app.core.provider_agent import is_subagent_row
        from app.api.git_owner import SUBAGENT_GIT_REFUSED
        if is_subagent_row(session):
            raise PermissionError(SUBAGENT_GIT_REFUSED)
    return await owner_op(session_id, op, body.get("target"), stop=bool(body.get("stop")))


async def owner_op(session_id: str, op: str, target: Optional[str] = None, *, stop: bool = False) -> Dict[str, Any]:
    """Run ``op`` (status, queue, cancel) here, on the session's owning worker."""
    if op == "status":
        return await asyncio.to_thread(_owner_state, session_id)
    if op == "queue":
        if not isinstance(target, str) or not target.strip():
            raise ValueError("Name the worker to move to.")
        return await queue_move(session_id, target, stop=stop)
    if op == "cancel":
        return await cancel_move(session_id)
    raise ValueError("Unknown move operation")


async def route(session: Dict[str, Any], op: str, target: Optional[str] = None, *, stop: bool = False) -> Dict[str, Any]:
    """``owner_op`` on whichever worker runs ``session``: here, or by control call."""
    from app.network import control

    owner = control.owner_of(session)
    if owner is None:
        return await owner_op(session["id"], op, target, stop=stop)
    body: Dict[str, Any] = {"session_id": session["id"], "op": op}
    if target:
        body["target"] = target
    if stop:
        body["stop"] = True
    return await control.call(owner, "session-move", body)


async def announce_dropped() -> int:
    """Boot: say in each chat that the restart dropped its queued hand-off.

    The request lived only in this worker's memory, so nothing will run it; a
    ``cancelled`` row ends the chat's queued state the same way the owner's
    Cancel does. A session that now runs on another worker, or no longer
    exists, gets no row. Returns how many chats were told.
    """
    from app.db import database
    from app.network import control

    def _take() -> Dict[str, Dict[str, str]]:
        with _store_lock:
            stored = _load_store()
            try:
                _store_path().unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not clear the queued hand-off notes", exc_info=True)
            return stored

    told = 0
    for session_id, entry in (await asyncio.to_thread(_take)).items():
        if pending_for(session_id) is not None:
            continue  # queued again since boot: still live
        try:
            session = await asyncio.to_thread(database.get_session, session_id)
            if not session or control.owner_of(session) is not None:
                continue
        except Exception:  # noqa: BLE001 — one unreadable row must not stop the rest
            logger.warning("Could not check dropped hand-off for %s", session_id, exc_info=True)
            continue
        label = entry["worker"]
        target = f" to {label}" if label else ""
        await _record(session_id,
                      f"Queued move{target} cancelled: the app restarted before the move ran. "
                      "Queue it again if you still want to move this chat.",
                      {"status": "cancelled", "worker": label, "by": entry["by"], "reason": "restart"})
        told += 1
    return told


def schedule(session_id: str) -> None:
    """Turn-end hook: start a queued hand-off in the background (never blocks)."""
    if session_id in _pending and session_id not in _running:
        asyncio.get_running_loop().create_task(run_pending(session_id))
