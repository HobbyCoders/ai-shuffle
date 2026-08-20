"""app/core/turn_manager.py — the command layer over the ``turns`` table
(v2 core, Phase 2).

This is the thin, transport-agnostic API that callers (WebSocket chat handlers,
automation, cluster/cloud intake, …) use to *command* a session's turn queue.
It owns none of the state machine — every real transition lives in
:mod:`app.db.turns` and is resolved at the database. Two responsibilities:

* **submit** a new turn (enqueue) and **kick** the session so the scheduler
  starts it, and **interrupt** the live turn — both delegating straight to the
  DB layer.
* **wait** helpers (``wait_settled`` / ``wait_session_idle``) that poll the
  turns table, used by automation (which must block until a headless turn
  finishes) and by tests.

The *kick* is an injected seam: this module does not know how a queued turn gets
started — the runtime/scheduler (Phase 2 A5/A6) registers a kick callback via
:func:`register_kick`. Until something registers one the kick is a no-op, so
``submit`` still safely enqueues (the reap/boot sweep or a later kick will pick
the turn up). Callers of this module are wired in later commits (A6/A7); nothing
here imports a caller.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional

from app.db import turns

logger = logging.getLogger(__name__)

# Injected kick seam. A kick callback takes a ``session_id`` and nudges the
# scheduler to try to start that session's next queued turn. It MUST be
# non-blocking / fire-and-forget (e.g. schedule an asyncio task); ``submit`` runs
# it synchronously right after enqueue. Default: no-op.
_kick_hook: Optional[Callable[[str], Any]] = None


def register_kick(hook: Optional[Callable[[str], Any]]) -> None:
    """Register (or clear, with ``None``) the kick callback the scheduler uses
    to start queued turns. Later commits register the real scheduler kick; tests
    inject a spy."""
    global _kick_hook
    _kick_hook = hook


# Injected interrupt-executor seam (mirrors ``_kick_hook``). Takes
# ``(session_id, mode)`` and reacts to the live turn having just been flipped to
# ``interrupting`` — the boot wiring routes it to
# ``session_runtime.get_live_runtime(sid).interrupt(mode)`` (the SDK interrupt +
# evict/drain escalation). Fire-and-forget; ``interrupt`` runs it synchronously
# right after the durable row flip. Default: no-op — with no executor (or no
# live runtime) the durable ``interrupting`` row stands and ``reap_expired`` is
# the backstop. ``turn_manager`` never imports ``session_runtime``; the wiring
# lives at boot (``startup``).
_interrupt_executor: Optional[Callable[..., Any]] = None


def register_interrupt_executor(hook: Optional[Callable[..., Any]]) -> None:
    """Register (or clear, with ``None``) the interrupt executor the boot wiring
    uses to drive the live runtime's SDK interrupt/escalation. Tests inject a
    spy.

    The hook is called ``hook(session_id, mode, turn_id)`` where ``turn_id`` is
    the optional turn identity a user stop targets (``None`` for the legacy
    session-scoped internal callers)."""
    global _interrupt_executor
    _interrupt_executor = hook


def _fire_interrupt_executor(
    session_id: str, mode: str, turn_id: Optional[str] = None
) -> None:
    """Fire the registered interrupt executor, if any. Swallows errors — a
    failed executor must never break the interrupt: the durable ``interrupting``
    row already stands and the reap sweep is the backstop."""
    hook = _interrupt_executor
    if hook is None:
        return
    try:
        result = hook(session_id, mode, turn_id)
        # Tolerate an async executor (the runtime's ``interrupt`` is a
        # coroutine): schedule it on the running loop rather than dropping it.
        if asyncio.iscoroutine(result):
            try:
                asyncio.get_running_loop().create_task(result)
            except RuntimeError:
                result.close()
    except Exception:  # noqa: BLE001 — an interrupt must never raise here.
        logger.exception("turn interrupt executor failed for session %s", session_id)


def _kick(session_id: str) -> None:
    """Fire the registered kick, if any. Swallows kick errors — a failed kick
    must never lose the just-enqueued turn (it stays queued for the next kick or
    the periodic sweep)."""
    hook = _kick_hook
    if hook is None:
        return
    try:
        result = hook(session_id)
        # Tolerate an async kick registered by an async runtime: schedule it on
        # the running loop rather than dropping the coroutine. No running loop
        # (sync caller) → best-effort no-op, same as a fire-and-forget kick.
        if asyncio.iscoroutine(result):
            try:
                asyncio.get_running_loop().create_task(result)
            except RuntimeError:
                result.close()
    except Exception:  # noqa: BLE001 — a kick must never break enqueue.
        logger.exception("turn kick failed for session %s", session_id)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

# Turn sources that count as "a person just spoke" and therefore cancel every
# wakeup the session had armed. Server-originated sources (automation, unpark,
# failover, auto_continue, wakeup itself) deliberately leave wakes alone.
_WAKEUP_SUPERSEDING_SOURCES = ("user", "queue")


def submit(
    session_id: str,
    source: str,
    *,
    prompt: Optional[str] = None,
    profile_id: Optional[str] = None,
    project_id: Optional[str] = None,
    worktree_id: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
    payload: Optional[Dict[str, Any]] = None,
    command_id: Optional[str] = None,
    parent_turn_id: Optional[str] = None,
    instance_id: Optional[str] = None,
    no_kick: bool = False,
) -> str:
    """Enqueue a new turn for ``session_id`` and kick the scheduler.

    Returns the new turn's id. The turn is durably ``queued`` before the kick
    fires, so even if the kick is a no-op (nothing registered yet) or fails, the
    work is never lost — a later kick or the periodic reap sweep starts it.

    ``no_kick=True`` skips the in-process kick entirely — used by the V2
    placement submit seam (Phase 4) when the session is placed on ANOTHER
    runner: the durable queued row is the whole hand-off (that runner's
    turn_claim loop picks it up via NOTIFY wake or its 5s poll), and a local
    kick would only lose the placement-gate race in session_runtime.execute.
    The skip is an optimization, not a correctness gate — the execute-side
    placement check is the backstop either way.

    A genuine user turn (``source`` in ``user``/``queue``) SUPERSEDES the
    session's pending wakeups: whatever an agent armed for later, the person
    who just typed is the newer instruction, and the agent can re-arm on its
    next turn. Without this a parked session with an armed wake would take the
    user's turn and then a second, stale one on top of it.
    """
    if source in _WAKEUP_SUPERSEDING_SOURCES:
        try:
            from app.core import wakeup_service

            wakeup_service.cancel_session_wakeups(session_id)
        except Exception:  # noqa: BLE001 — never let this break an enqueue.
            logger.exception(
                "wakeup supersede failed for session %s", session_id
            )
    turn = turns.enqueue_turn(
        session_id,
        source,
        prompt=prompt,
        profile_id=profile_id,
        project_id=project_id,
        worktree_id=worktree_id,
        overrides=overrides,
        payload=payload,
        command_id=command_id,
        parent_turn_id=parent_turn_id,
        instance_id=instance_id,
    )
    if not no_kick:
        _kick(session_id)
    return turn["id"]


def remove_queued(turn_id: str) -> Optional[Dict[str, Any]]:
    """Remove a still-``queued`` turn (a queue chip the user dropped).

    Thin wrapper over the guarded terminal transition: settle the turn to
    ``aborted`` with ``stop_reason='removed_from_queue'``. The history row is
    KEPT (the log is the record) and NO scheduler kick fires — removing a queued
    item never starts anything.

    The ``state == 'queued'`` guard is atomic inside the UPDATE, so a turn that
    raced to live between the user's click and this call is left alone (``None``
    returned) rather than aborted mid-flight. Returns the aborted turn row on
    success, else ``None``.
    """
    return turns.remove_queued_turn(turn_id)


def interrupt(
    session_id: str,
    mode: str,
    stop_background: bool = False,
    turn_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Interrupt a session's live turn — the ONE interrupt implementation.

    ``mode`` is ``'stop'`` or ``'send_now'``; ``stop_background`` records whether
    background children should be torn down. Flips the durable row FIRST (the
    guarded ``running -> interrupting`` transition — the durable intent), THEN
    fires the interrupt executor so the live runtime reacts (SDK interrupt +
    escalation). Returns the now-interrupting turn row, or ``None`` when there
    is no running turn to interrupt.

    ``turn_id`` (optional) binds the interrupt to a specific turn identity: it
    is threaded to BOTH the DB transition (so a stale stop whose target turn
    already settled matches zero rows → ``None`` → no executor fire) and the
    runtime signal (so the live runtime is only signalled when it is executing
    that turn). ``None`` preserves the legacy session-scoped behavior used by
    the internal auto/resource/send-now callers (GitHub #585).
    """
    row = turns.interrupt_turn(
        session_id, mode, stop_background=stop_background, turn_id=turn_id
    )
    if row is not None:
        _fire_interrupt_executor(session_id, mode, turn_id)
    return row


# ---------------------------------------------------------------------------
# Wait helpers (poll the turns table)
# ---------------------------------------------------------------------------

async def wait_settled(
    turn_id: str,
    timeout: float = 300.0,
    poll_interval: float = 0.05,
) -> Optional[Dict[str, Any]]:
    """Poll until ``turn_id`` reaches a terminal state
    (completed/aborted/parked) and return its row, or ``None`` on timeout /
    unknown turn.

    Used by automation (block until a headless turn finishes) and tests.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while True:
        turn = turns.get_turn(turn_id)
        if turn is not None and turn["state"] in turns.TERMINAL_STATES:
            return turn
        if loop.time() >= deadline:
            return None
        await asyncio.sleep(poll_interval)


async def wait_session_idle(
    session_id: str,
    timeout: float = 300.0,
    poll_interval: float = 0.05,
) -> bool:
    """Poll until ``session_id`` is idle — no live turn AND no queued turns —
    and return ``True``, or ``False`` on timeout.

    Used by the non-WS intake callers (automation / cluster / cloud) that submit
    then block until the session has fully drained.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while True:
        if turns.get_live_turn(session_id) is None and not turns.get_queued_turns(session_id):
            return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(poll_interval)
