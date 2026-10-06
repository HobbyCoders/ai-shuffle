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
calls ``queue_move`` on the owning worker. "Stop and move now" stops the work
as the Stop button does, then moves. The rules (#1819):

* Busy means a live turn, a queued turn, background work, or a wake-up about
  to fire (``busy_state``). Queued messages run first: "Moving after the
  queued messages".
* Every queued move, the owner's and the agent's, waits at most
  IDLE_TIMEOUT_S. Then it is cancelled and the chat says what still ran.
* The owner's messages sent while a move waits are HELD (``hold``) and sent
  on the new worker after the move, in order (``deliver_held``). Each stays
  in the ``session_moves`` row until the new worker has it, and travels with
  its id as the turn's ``command_id``, so a resend after a lost reply is
  dropped there. When the move is cancelled, fails, expires or is refused,
  they are sent here instead. A message that cannot be sent is named in the
  chat; none is dropped silently.
* An open approval, question or private-access card refuses the move: those
  cards cannot follow the chat (``app.network.handoff.OPEN_PROMPT``).
* Pending wake-ups move with the chat (``app.network.handoff``).
* The outcome and any message the new worker did not take are recorded in the
  chat on the worker that owns it after the move (the ``note`` operation).
* A queued move is a ``session_moves`` row (``app.db.session_moves``), so a
  restart of this worker resumes it (``resume_queued``) with its held
  messages instead of dropping it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# How long a queued hand-off waits for the turn and background work to end.
IDLE_TIMEOUT_S = 600
POLL_S = 2.0
SUBTYPE = "handoff"
# A pending wake-up due this soon counts as work that is about to run.
WAKE_SOON_S = 60
# Held messages carry a queue id with this prefix (never a turn id).
HELD_PREFIX = "held-"
# The owner's own messages; only these are held while a move waits.
HOLD_SOURCES = ("user", "queue")
# Held messages are retried to the new worker for this long after the move
# (or after a restart), then the chat names the ones it did not take.
FORWARD_WINDOW_S = 300
FORWARD_RETRY_S = 2.0
FORWARD_RETRY_MAX_S = 30.0
# What the new worker's "note" operation accepts in a hand-off row's data.
NOTE_STATUSES = ("moved", "held_failed")
NOTE_KEYS = ("status", "worker", "count", "unconfirmed", "messages", "reason")
# A peer's note is kept to this: the first NOTE_MESSAGES texts, each cut to
# NOTE_MESSAGE_CHARS, and at most NOTE_DATA_CHARS of JSON in all.
NOTE_MESSAGES = 50
NOTE_MESSAGE_CHARS = 2000
NOTE_DATA_CHARS = 120_000


@dataclass
class PendingHandoff:
    target_id: str
    label: str
    note: str
    requested_at: float
    # "agent" (HandOffSession) or "owner" (worker selector / Resume here).
    by: str = "agent"
    cancelled: bool = False
    # Epoch deadline; 0 means requested_at + IDLE_TIMEOUT_S.
    expires_at: float = 0.0
    # While True, the owner's new messages wait for the move (``hold``).
    holding: bool = field(default=True)

    def __post_init__(self) -> None:
        if not self.expires_at:
            self.expires_at = self.requested_at + IDLE_TIMEOUT_S


_pending: Dict[str, PendingHandoff] = {}
_running: set[str] = set()
# Hand-offs taken from _pending that are waiting for the session to go idle.
_waiting: Dict[str, PendingHandoff] = {}
# Hand-offs whose move is under way.
_moving: Dict[str, PendingHandoff] = {}
# Sessions whose held messages are being sent to their new worker.
_delivering: set[str] = set()
_tasks: set[asyncio.Task] = set()
# Serialises a move starting (``_claim``) against a cancel, so a cancel either
# stops the move or is refused as already started; never both.
_state_lock = threading.Lock()
# Serialises holding a message against releasing the held list, so a message
# is either held (and later released with the rest) or submitted after them.
_hold_lock = threading.RLock()
_store_lock = threading.Lock()


def _store_path():
    """Legacy (#1826) note of queued hand-offs; read once by ``announce_dropped``."""
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


def _save(session_id: str, pending: PendingHandoff) -> None:
    from app.db import session_moves

    session_moves.save(session_id, target_id=pending.target_id, label=pending.label, note=pending.note,
                       requested_by=pending.by, requested_at=pending.requested_at,
                       expires_at=pending.expires_at)


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
    current = _active(session_id)
    if current is not None and current.target_id != row["device_id"]:
        raise ValueError(f"A move to {current.label} is already queued. Cancel it first.")
    message = ("Finish this turn now with a short summary of where the work stands; once the turn "
               "and any background work end, the app commits and pushes the branch and moves this "
               "session there.")
    if current is not None:
        # The same move again: the queued one stands (with its time limit).
        return {"queued": True, "worker": current.label,
                "message": f"Hand-off to {current.label} is already queued. {message}"}
    pending = PendingHandoff(row["device_id"], str(row.get("device_name") or row["device_id"]),
                             " ".join((note or "").split())[:2000], time.time())
    _save(session_id, pending)
    _pending[session_id] = pending
    return {"queued": True, "worker": pending.label,
            "message": f"Hand-off to {pending.label} is queued. {message}"}


def pending_for(session_id: str) -> Optional[PendingHandoff]:
    return _pending.get(session_id) or _waiting.get(session_id) or _moving.get(session_id)


def _active(session_id: str) -> Optional[PendingHandoff]:
    pending = pending_for(session_id)
    return pending if pending is not None and not pending.cancelled else None


# ---------------------------------------------------------------------------
# Held messages
# ---------------------------------------------------------------------------

def holds(session_id: str) -> bool:
    """Whether the owner's new messages to ``session_id`` wait for a queued move.

    A cancelled move holds too until its held messages are released here
    (``_release_here_sync`` ends it), so a newer message never runs before them.
    """
    pending = pending_for(session_id)
    return bool(pending and pending.holding)


def moving(session_id: str) -> bool:
    """Whether the move of ``session_id`` is under way (begin may have sent its wakes)."""
    return session_id in _moving


def hold(session_id: str, source: str, submit: Dict[str, Any]) -> Optional[str]:
    """Hold an owner's message while a move waits. Returns its queue id, or None.

    ``submit`` is the keyword arguments of ``turn_manager._submit_blocking``;
    they are replayed as they are, here or on the new worker.
    """
    if source not in HOLD_SOURCES:
        return None
    from app.core.turn_manager import VOICE_DELEGATION

    if (submit.get("payload") or {}).get(VOICE_DELEGATION):
        return None
    from app.db import session_moves

    # Checked only under the lock: a cancel releasing the held messages holds
    # it, so this message is held before the release or sent after it.
    with _hold_lock:
        if not holds(session_id):
            return None
        held_id = HELD_PREFIX + uuid.uuid4().hex
        entry = {"id": held_id, "source": source, "held_at": time.time(),
                 **{key: value for key, value in submit.items() if value is not None}}
        try:
            if not session_moves.append_held(session_id, json.loads(json.dumps(entry, default=str))):
                return None
        except Exception:  # noqa: BLE001 — never lose the message: send it normally
            logger.warning("Could not hold a message for the queued move of %s", session_id, exc_info=True)
            return None
    return held_id


def held_view(session_id: str) -> List[Dict[str, Any]]:
    """The held messages of ``session_id``, oldest first (queue chips)."""
    if pending_for(session_id) is None:
        return []
    from app.db import session_moves

    row = session_moves.get(session_id)
    return list((row or {}).get("held") or [])


def drop_held(session_id: str, held_id: str) -> bool:
    """Remove one held message (its queue chip's x)."""
    from app.db import session_moves

    with _hold_lock:
        return session_moves.remove_held(session_id, held_id)


_SUBMIT_KEYS = ("prompt", "profile_id", "project_id", "worktree_id", "overrides",
                "explicit_permission", "payload", "command_id")


Released = Tuple[int, List[Dict[str, Any]]]


def _submit_here(session_id: str, held: List[Dict[str, Any]]) -> Released:
    """Queue ``held`` here. Returns how many were sent and the ones refused."""
    from app.core import turn_manager

    sent = 0
    unsent: List[Dict[str, Any]] = []
    for entry in held:
        try:
            turn_manager._submit_blocking(session_id, str(entry.get("source") or "user"),
                                          **{key: entry[key] for key in _SUBMIT_KEYS if key in entry})
            sent += 1
        except Exception:  # noqa: BLE001 — one refused message must not stop the rest
            logger.warning("Could not send a held message of %s here", session_id, exc_info=True)
            unsent.append(entry)
    return sent, unsent


def _release_here_sync(session_id: str, pending: Optional[PendingHandoff]) -> Released:
    """Stop holding and send the held messages here, in order."""
    from app.db import session_moves

    with _hold_lock:
        if pending is not None:
            pending.holding = False
        row = session_moves.take(session_id)
        return _submit_here(session_id, list((row or {}).get("held") or []))


def _quoted(entries: List[Dict[str, Any]]) -> str:
    """The messages' texts for the chat: “first”, “second”."""
    texts = []
    for entry in entries:
        text = " ".join(str(entry.get("prompt") or "").split())
        texts.append("“" + (text[:200] + "…" if len(text) > 200 else text) + "”")
    return ", ".join(texts)


def _messages(count: int) -> str:
    return "1 of your messages" if count == 1 else f"{count} of your messages"


async def _notify(session_id: str, entries: List[Dict[str, Any]], message: str) -> None:
    """Tell the owner outside the chat too: a chat on another worker or an archived one."""
    try:
        from app.services.notification_service import emit_notification

        user = next((str((entry.get("payload") or {}).get("api_user_id"))
                     for entry in entries if (entry.get("payload") or {}).get("api_user_id")), "admin")
        await emit_notification(user, severity="warning", title="Messages not sent", message=message,
                                category="handoff_held_failed", session_id=session_id)
    except Exception:  # noqa: BLE001 — the chat row is the main report
        logger.warning("Could not notify about unsent messages of %s", session_id, exc_info=True)


async def _report_unsent_here(session_id: str, unsent: List[Dict[str, Any]]) -> None:
    """Name the held messages this worker refused too (an archived chat)."""
    if not unsent:
        return
    from app.core import archive_guard

    archived = await asyncio.to_thread(archive_guard.is_archived, session_id)
    reason = " because this chat is archived" if archived else ""
    content = f"Could not send {_messages(len(unsent))} here{reason}: {_quoted(unsent)}. Send them again."
    await _record(session_id, content, {"status": "held_failed", "count": len(unsent),
                                        "messages": [str(entry.get("prompt") or "") for entry in unsent],
                                        "reason": "archived" if reason else "refused"})
    await _notify(session_id, unsent, content)


async def _queue_changed(session_id: str) -> None:
    from app.core.session_runtime import _broadcast_queue_after_start

    await _broadcast_queue_after_start(session_id)


async def _release_here(session_id: str, pending: Optional[PendingHandoff]) -> Released:
    """Send the held messages here. The caller records its outcome, then
    ``_report_unsent_here`` for the ones refused."""
    sent, unsent = await asyncio.to_thread(_release_here_sync, session_id, pending)
    if sent:
        from app.core import turn_manager

        turn_manager._kick(session_id)
    await _queue_changed(session_id)
    return sent, unsent


def _resend_safe(target_id: str) -> bool:
    """Whether ``target_id`` drops a repeated held message (``move_wakes`` 1+)."""
    from app.network.handoff import carries_wakes

    try:
        return carries_wakes(target_id)
    except Exception:  # noqa: BLE001 — unknown: never send twice
        return False


def _held_command_id(entry: Dict[str, Any]) -> str:
    """The id a held message travels with; the new worker drops a repeat of it."""
    return str(entry.get("command_id") or entry.get("id") or "")


def _first_held(session_id: str) -> Optional[Dict[str, Any]]:
    from app.db import session_moves

    held = (session_moves.get(session_id) or {}).get("held") or []
    return held[0] if held else None


def _delivered(session_id: str, entry: Dict[str, Any]) -> None:
    from app.db import session_moves

    with _hold_lock:
        session_moves.remove_held(session_id, str(entry.get("id") or ""))


def _take_rest(session_id: str) -> List[Dict[str, Any]]:
    from app.db import session_moves

    with _hold_lock:
        row = session_moves.take(session_id)
    return list((row or {}).get("held") or [])


def _label(device_id: str, fallback: str = "") -> str:
    from app.network import runners

    try:
        row = runners.find(device_id) or {}
    except Exception:  # noqa: BLE001 — a label only
        row = {}
    return str(row.get("device_name") or fallback or "the other worker")


async def _tell(session_id: str, owner: str, content: str, data: Dict[str, Any]) -> bool:
    """Record a hand-off row in the chat on ``owner``, which now runs it.

    Rows recorded here would not reach that chat. When the owner cannot take
    the row (offline, or a version without the ``note`` operation) it is
    recorded here. Returns whether the owner took it.
    """
    from app.network import control

    try:
        await control.call(owner, "session-move",
                           {"session_id": session_id, "op": "note", "content": content, "data": data})
        return True
    except Exception:  # noqa: BLE001 — recorded here instead
        logger.info("Could not record the hand-off row of %s on %s; recording it here",
                    session_id, owner, exc_info=True)
    await _record(session_id, content, data)
    return False


async def deliver_held(session_id: str, target_id: str, label: str) -> None:
    """Send the held messages of ``session_id``, oldest first, to ``target_id``.

    ``target_id`` owns the chat now. Each message stays in the
    ``session_moves`` row until the new worker has it, so a crash or restart
    here resends the rest (``resume_queued``). It travels with a stable
    ``command_id``: a worker that has the move protocol (``move_wakes``)
    drops a repeat, so a send whose reply was lost is retried. An older worker
    gets each message once; one whose reply was lost is named in the chat as
    unconfirmed instead of being sent twice. A refusal, or no answer within
    FORWARD_WINDOW_S, ends the delivery and the chat names what was not sent.
    """
    if session_id in _delivering:
        return
    _delivering.add(session_id)
    try:
        await _deliver(session_id, target_id, label)
    except Exception:  # noqa: BLE001 — the rest stay stored; the next boot resends them
        logger.warning("Could not send the held messages of %s to %s", session_id, target_id, exc_info=True)
    finally:
        _delivering.discard(session_id)


async def _deliver(session_id: str, target_id: str, label: str) -> None:
    from app.network import control

    resend_safe = await asyncio.to_thread(_resend_safe, target_id)
    deadline = time.time() + FORWARD_WINDOW_S
    delay = FORWARD_RETRY_S
    unconfirmed: List[Dict[str, Any]] = []
    reason = ""
    while True:
        entry = await asyncio.to_thread(_first_held, session_id)
        if entry is None:
            break
        try:
            await control.submit_turn(
                target_id, session_id, prompt=str(entry.get("prompt") or ""),
                source=str(entry.get("source") or "user"), profile_id=entry.get("profile_id"),
                project_id=entry.get("project_id"), overrides=entry.get("overrides"),
                payload=entry.get("payload"), owner_send=bool(entry.get("explicit_permission")),
                command_id=_held_command_id(entry))
        except control.ControlRefused as exc:
            reason = f" ({exc})" if str(exc) else ""
            break
        except control.MaybeApplied:
            logger.warning("No answer to held message of %s from %s", session_id, target_id, exc_info=True)
            if not resend_safe:
                # It may have arrived and an older worker cannot drop a repeat.
                unconfirmed.append(entry)
                await asyncio.to_thread(_delivered, session_id, entry)
                continue
        except Exception:  # noqa: BLE001 — nothing reached the new worker: retried
            logger.warning("Could not send held message of %s to %s", session_id, target_id, exc_info=True)
        else:
            await asyncio.to_thread(_delivered, session_id, entry)
            delay = FORWARD_RETRY_S
            continue
        if time.time() + delay > deadline:
            minutes = max(1, round(FORWARD_WINDOW_S / 60))
            reason = f" (it did not answer for {minutes} minute{'' if minutes == 1 else 's'})"
            break
        await asyncio.sleep(delay)
        delay = min(delay * 2, FORWARD_RETRY_MAX_S)
    unsent = await asyncio.to_thread(_take_rest, session_id)
    parts = []
    if unsent:
        parts.append(f"Could not send {_messages(len(unsent))} to {label}{reason}: {_quoted(unsent)}. "
                     "Send them again there.")
    if unconfirmed:
        parts.append(f"{label} did not confirm {_messages(len(unconfirmed))}: {_quoted(unconfirmed)}. "
                     "Check there before sending them again.")
    if not parts:
        return
    content = " ".join(parts)
    told = await _tell(session_id, target_id, content, {
        "status": "held_failed", "worker": label, "count": len(unsent), "unconfirmed": len(unconfirmed),
        "messages": [str(entry.get("prompt") or "") for entry in unsent + unconfirmed]})
    if not told:
        await _notify(session_id, unsent + unconfirmed, content)


def _spawn(coro) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


# ---------------------------------------------------------------------------
# Busy and waiting
# ---------------------------------------------------------------------------

def cancel(session_id: str) -> bool:
    """Cancel a queued hand-off; held messages are sent here. Sync.

    A move already under way is not cancelled. The caller kicks the turn
    scheduler (``cancel_async`` does) so released messages start.
    """
    return _cancel_counting(session_id) is not None


async def cancel_async(session_id: str) -> bool:
    released = await asyncio.to_thread(_cancel_counting, session_id)
    if released is not None:
        from app.core import turn_manager

        turn_manager._kick(session_id)
        await _queue_changed(session_id)
        await _report_unsent_here(session_id, released[1])
    return released is not None


async def _record(session_id: str, content: str, data: Dict[str, Any]) -> None:
    try:
        from app.core.turn_recorder import record_drained_event

        await record_drained_event(session_id, {"type": "system", "subtype": SUBTYPE,
                                                "content": content, "data": {**data, "content": content}})
    except Exception:  # noqa: BLE001 — the row is a report
        logger.warning("Could not record hand-off row for %s", session_id, exc_info=True)


def busy_state(session_id: str) -> Dict[str, Any]:
    """What keeps ``session_id`` from moving now.

    ``{"running", "tasks", "queued", "wake"}``: ``running`` is a live turn or
    one waiting in the turn queue; ``tasks`` names its running background
    tasks (subagents, Monitors, background shells); ``queued`` counts the
    turns waiting in the queue (they run before the move); ``wake`` is True
    when a scheduled wake-up is due within WAKE_SOON_S. The handoff's own idle
    gate refuses a move while any turn runs or is queued, so all of them must
    hold the move too.
    """
    from app.core import streaming_status
    from app.core.task_registry import task_registry
    from app.db import turns as turns_db
    from app.db import wakeups

    tasks = [str(task.get("subject") or task.get("agent") or "Background task")
             for task in task_registry.get_active(session_id)]
    try:
        # One SELECT over queued/running/interrupting/draining, so a turn
        # flipping from queued to running is never missed between two reads.
        unsettled = bool(turns_db.session_has_unsettled_turns(session_id))
        queued = len(turns_db.get_queued_turns(session_id)) if unsettled else 0
    except Exception:
        logger.warning("Could not read the turn queue for %s", session_id, exc_info=True)
        unsettled, queued = False, 0
    try:
        soon = time.time() + WAKE_SOON_S
        wake = any(float(row.get("fire_at") or 0) <= soon for row in wakeups.get_session_wakeups(session_id))
    except Exception:
        logger.warning("Could not read the wake-ups of %s", session_id, exc_info=True)
        wake = False
    running = unsettled or bool(streaming_status.has_active_run(session_id))
    return {"running": running, "tasks": tasks, "queued": queued, "wake": wake}


def _is_busy(state: Dict[str, Any]) -> bool:
    return bool(state["running"] or state["tasks"] or state.get("wake"))


def _busy(session_id: str) -> bool:
    return _is_busy(busy_state(session_id))


def _busy_words(state: Dict[str, Any]) -> str:
    """``state`` in plain words: "the turn, 2 queued messages and Build"."""
    parts: List[str] = []
    queued = int(state.get("queued") or 0)
    if state.get("running") and not queued:
        parts.append("the turn")
    elif queued:
        parts.append("the turn and 1 queued message" if queued == 1 else f"the turn and {queued} queued messages")
    parts.extend(state.get("tasks") or [])
    if state.get("wake"):
        parts.append("a scheduled wake-up")
    if not parts:
        return ""
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


async def _wait_idle(session_id: str, pending: PendingHandoff) -> bool:
    while time.time() < pending.expires_at:
        if pending.cancelled:
            return False
        if not await asyncio.to_thread(_busy, session_id):
            return True
        await asyncio.sleep(POLL_S)
    return False


def _open_prompt(session_id: str) -> bool:
    from app.db.base import get_db
    from app.network.handoff import open_prompt

    with get_db() as conn:
        return open_prompt(conn.cursor(), session_id)


def _sent_here(sent: int) -> str:
    if not sent:
        return ""
    return " Your message was sent here." if sent == 1 else f" Your {sent} messages were sent here."


def _noun(pending: PendingHandoff) -> str:
    return "Move" if pending.by == "owner" else "Hand-off"


async def _expire(session_id: str, pending: PendingHandoff) -> str:
    """The move waited IDLE_TIMEOUT_S: cancel it, say what still ran, release here."""
    state = await asyncio.to_thread(busy_state, session_id)
    still = _busy_words(state)
    still_text = f" Still running: {still}." if still else ""
    sent, unsent = await _release_here(session_id, pending)
    minutes = max(1, round(IDLE_TIMEOUT_S / 60))
    outcome = (f"{_noun(pending)} to {pending.label} cancelled: this chat was still busy after "
               f"{minutes} minutes.{still_text}{_sent_here(sent)}")
    await _record(session_id, outcome, {"status": "timeout", "worker": pending.label, "by": pending.by,
                                        "still": still})
    await _report_unsent_here(session_id, unsent)
    return outcome


def _claim(session_id: str, pending: PendingHandoff) -> bool:
    """Start the move (or its expiry) unless a cancel came first.

    From here a cancel is refused and a second move cannot be queued, so the
    end of this one never deletes another's row. No await comes between the
    wait ending and this.
    """
    with _state_lock:
        _waiting.pop(session_id, None)
        if pending.cancelled:
            return False
        _moving[session_id] = pending
        return True


def _owner_now(session_id: str) -> Optional[str]:
    """The worker that owns ``session_id`` now, when it is not this one."""
    from app.db import database
    from app.network import control

    session = database.get_session(session_id)
    return control.owner_of(session) if session else None


async def _moved(session_id: str, pending: PendingHandoff, owner: str) -> str:
    """The chat now runs on ``owner``: say so there, then send it the held messages."""
    label = pending.label if owner == pending.target_id else _label(owner)
    outcome = f"Handed off to {label}. Open this chat there to continue on the same branch."
    await _tell(session_id, owner, outcome, {"status": "moved", "worker": label})
    with _hold_lock:
        pending.holding = False
    await deliver_held(session_id, owner, label)
    return outcome


async def _failed(session_id: str, pending: PendingHandoff, error: Any) -> str:
    """The move raised. Ownership decides what happened, not the error: a move
    whose reply was lost may have finished on the new worker."""
    try:
        owner = await asyncio.to_thread(_owner_now, session_id)
    except Exception:  # noqa: BLE001 — unknown: treated as still here
        logger.warning("Could not read the owner of %s after a failed move", session_id, exc_info=True)
        owner = None
    if owner is not None:
        logger.warning("Move of %s raised (%s) but it now runs on %s", session_id, error, owner)
        return await _moved(session_id, pending, owner)
    sent, unsent = 0, []
    if pending.holding:
        try:
            sent, unsent = await _release_here(session_id, pending)
        except Exception:  # noqa: BLE001
            logger.warning("Could not send held messages of %s here", session_id, exc_info=True)
    outcome = f"{_noun(pending)} to {pending.label} failed: {error}{_sent_here(sent)}"
    await _record(session_id, outcome, {"status": "failed", "worker": pending.label})
    await _report_unsent_here(session_id, unsent)
    return outcome


async def run_pending(session_id: str) -> Optional[str]:
    """Run a queued hand-off once ``session_id`` is idle. Returns the outcome text."""
    with _state_lock:
        if session_id in _running:
            return None
        pending = _pending.pop(session_id, None)
        if pending is None:
            return None
        _running.add(session_id)
        _waiting[session_id] = pending
    try:
        idle = await _wait_idle(session_id, pending)
        if not _claim(session_id, pending):
            return None
        if not idle:
            return await _expire(session_id, pending)
        if await asyncio.to_thread(_open_prompt, session_id):
            sent, unsent = await _release_here(session_id, pending)
            from app.network.handoff import OPEN_PROMPT

            outcome = f"{_noun(pending)} to {pending.label} cancelled. {OPEN_PROMPT}{_sent_here(sent)}"
            await _record(session_id, outcome, {"status": "failed", "worker": pending.label,
                                                "by": pending.by, "reason": "open_prompt"})
            await _report_unsent_here(session_id, unsent)
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
            return await _failed(session_id, pending, exc.detail)
        return await _moved(session_id, pending, pending.target_id)
    except Exception as exc:  # noqa: BLE001 — never break turn teardown
        logger.warning("Hand-off failed for %s", session_id, exc_info=True)
        return await _failed(session_id, pending, exc)
    finally:
        with _state_lock:
            _waiting.pop(session_id, None)
            _moving.pop(session_id, None)
            _running.discard(session_id)
        # A move queued while a cancelled one was still winding down.
        schedule(session_id)


def _owner_state(session_id: str) -> Dict[str, Any]:
    pending = _active(session_id)
    return {"busy": busy_state(session_id),
            "pending": ({"worker": pending.label, "target_id": pending.target_id, "by": pending.by,
                         "moving": session_id in _moving}
                        if pending else None)}


async def queue_move(session_id: str, target: str, *, stop: bool = False) -> Dict[str, Any]:
    """The owner's "Move when this turn ends" (#1819), on the owning worker.

    Queues the same hand-off HandOffSession does and starts waiting at once:
    the move runs when the turn, the queued messages and background work end.
    ``stop`` stops that work first, as the Stop button does ("Stop and move
    now"). An open approval, question or access card refuses it, unless the
    Stop dismisses it.
    """
    from app.network import runners
    from app.network.handoff import OPEN_PROMPT

    current = _active(session_id)
    if current is not None:
        raise ValueError(f"A move to {current.label} is already queued. Cancel it first.")
    if not stop and await asyncio.to_thread(_open_prompt, session_id):
        raise ValueError(OPEN_PROMPT)
    row = await asyncio.to_thread(resolve_worker, runners.resolve_runner_id(target) or target)
    label = str(row.get("device_name") or row["device_id"])
    pending = PendingHandoff(row["device_id"], label, "", time.time(), by="owner")
    await asyncio.to_thread(_save, session_id, pending)
    _pending[session_id] = pending
    state = await asyncio.to_thread(busy_state, session_id)
    after = "queued" if state.get("queued") and not stop else "turn"
    text = (f"Move to {label} queued for after the queued messages." if after == "queued"
            else f"Move to {label} queued for when this turn ends.")
    await _record(session_id, text, {"status": "queued", "worker": label, "by": "owner", "after": after})
    if stop:
        from app.core.desktop_lifecycle import stop_session_work

        await stop_session_work(session_id, source="session_handoff")
    # Start waiting now: a parked chat has no turn end left to start it.
    schedule(session_id)
    return {"queued": True, "worker": label, "after": after}


async def cancel_move(session_id: str) -> Dict[str, Any]:
    """Drop a queued move, the owner's or the agent's, and say so in the chat.

    Held messages are sent here. A move already under way is not cancelled.
    """
    pending = pending_for(session_id)
    released = await asyncio.to_thread(_cancel_counting, session_id)
    if released is None:
        moving = _moving.get(session_id)
        if moving is not None:
            raise ValueError(f"The move to {moving.label} has already started.")
        if pending is not None or session_id in _running:
            # A move ending right now records its own outcome.
            return {"cancelled": False}
        # Nothing is queued: a row left "queued" (an earlier version, a lost
        # restart note) would keep showing a move. This row ends it.
        await _record(session_id, "No move is queued for this chat.",
                      {"status": "cancelled", "worker": "", "by": "owner", "reason": "nothing_queued"})
        return {"cancelled": False}
    sent, unsent = released
    from app.core import turn_manager

    turn_manager._kick(session_id)
    await _queue_changed(session_id)
    label = pending.label if pending else ""
    target = f" to {label}" if label else ""
    await _record(session_id, f"Queued move{target} cancelled.{_sent_here(sent)}",
                  {"status": "cancelled", "worker": label, "by": "owner"})
    await _report_unsent_here(session_id, unsent)
    return {"cancelled": True}


def _cancel_counting(session_id: str) -> Optional[Released]:
    """``cancel`` that returns the held messages it sent here (None: nothing cancelled).

    A move that has started (``_claim``) is not cancelled. The release holds
    ``_hold_lock`` from the cancel on, so a message sent meanwhile is held and
    released after the older ones, never queued ahead of them.
    """
    with _hold_lock:
        with _state_lock:
            if session_id in _moving:
                return None
            pending = _pending.pop(session_id, None)
            if pending is None:
                waiting = _waiting.get(session_id)
                if waiting is None or waiting.cancelled:
                    return None
                pending = waiting
            pending.cancelled = True
        return _release_here_sync(session_id, pending)


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
    if op == "note":
        return await _note(session_id, body.get("content"), body.get("data"))
    if op == "queue":
        from app.core.provider_agent import is_subagent_row
        from app.api.git_owner import SUBAGENT_GIT_REFUSED
        if is_subagent_row(session):
            raise PermissionError(SUBAGENT_GIT_REFUSED)
    return await owner_op(session_id, op, body.get("target"), stop=bool(body.get("stop")))


async def _note(session_id: str, content: Any, data: Any) -> Dict[str, Any]:
    """Record the old owner's hand-off row (the outcome, unsent messages) here,
    on the worker that now runs the chat, so its open chats show it."""
    if not isinstance(content, str) or not content.strip() or len(content) > 8000 or not isinstance(data, dict):
        raise ValueError("Invalid hand-off note")
    if data.get("status") not in NOTE_STATUSES:
        raise ValueError("Unknown hand-off note")
    kept = {key: data[key] for key in NOTE_KEYS if key in data}
    if "messages" in kept:
        messages = kept["messages"] if isinstance(kept["messages"], list) else []
        kept["messages"] = [str(text)[:NOTE_MESSAGE_CHARS] for text in messages[:NOTE_MESSAGES]]
    if len(json.dumps(kept, default=str)) > NOTE_DATA_CHARS:
        raise ValueError("Hand-off note too large")
    await _record(session_id, content, kept)
    return {"recorded": True}


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


async def resume_queued() -> int:
    """Boot: pick up the queued moves this worker held before it restarted.

    A move that has not expired waits again; an expired one is cancelled as
    any expired move is. Either way its held messages are kept: sent after
    the move, or here. A session that now runs on another worker gets the
    held messages it did not have yet there (``deliver_held``, retried until
    the link to it is up); a deleted one is forgotten. Returns how many moves
    wait again.
    """
    from app.db import database, session_moves
    from app.network import control

    resumed = 0
    for row in await asyncio.to_thread(session_moves.list_all):
        session_id = str(row["session_id"])
        if pending_for(session_id) is not None:
            continue
        try:
            session = await asyncio.to_thread(database.get_session, session_id)
            if not session:
                await asyncio.to_thread(session_moves.take, session_id)
                continue
            owner = control.owner_of(session)
            pending = PendingHandoff(str(row["target_id"]), str(row["label"]), str(row.get("note") or ""),
                                     float(row["requested_at"]), by=str(row.get("requested_by") or "agent"),
                                     expires_at=float(row["expires_at"]))
            if owner is not None:
                # The move finished before the restart: its held messages go there.
                pending.holding = False
                label = pending.label if owner == pending.target_id else _label(owner)
                _spawn(deliver_held(session_id, owner, label))
                continue
            if time.time() >= pending.expires_at:
                await _expire(session_id, pending)
                continue
            _pending[session_id] = pending
            schedule(session_id)
            resumed += 1
        except Exception:  # noqa: BLE001 — one unreadable row must not stop the rest
            logger.warning("Could not resume the queued move of %s", session_id, exc_info=True)
    return resumed


async def announce_dropped() -> int:
    """Boot: say in each chat that the restart dropped its queued hand-off.

    Moves queued before ``session_moves`` existed (#1826) lived only in memory
    and a file in the data dir; nothing will run them. A ``cancelled`` row
    ends the chat's queued state the same way the owner's Cancel does. A
    session that now runs on another worker, no longer exists, or waits again
    (``resume_queued``) gets no row. Returns how many chats were told.
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


async def on_boot() -> None:
    """Startup: resume moves kept in the database, then the legacy note."""
    try:
        await resume_queued()
    except Exception:  # noqa: BLE001 — boot continues
        logger.warning("Could not resume queued moves", exc_info=True)
    await announce_dropped()


def schedule(session_id: str) -> None:
    """Turn-end hook: start a queued hand-off in the background (never blocks)."""
    if session_id in _pending and session_id not in _running:
        asyncio.get_running_loop().create_task(run_pending(session_id))
