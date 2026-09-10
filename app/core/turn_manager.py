"""app/core/turn_manager.py — the command layer over the ``turns`` table
(v2 core, Phase 2).

This is the thin, transport-agnostic API that callers (WebSocket chat handlers,
automation, cluster/cloud intake, …) use to *command* a session's turn queue.
It owns none of the state machine — every real transition lives in
:mod:`app.db.turns` and is resolved at the database. Two responsibilities:

* **submit** a new turn (enqueue) and **kick** the session so the scheduler
  starts it, and **interrupt** the live turn — both delegating straight to the
  DB layer.
* **wait** helpers (``wait_settled`` / ``wait_session_idle``) used by automation
  (which must block until a headless turn finishes) and by tests. They are
  event-driven first (the PostgreSQL ``session_events`` NOTIFY gateway wakes
  them the moment a turn settles) with an exponentially backing-off poll as the
  correctness backstop.

Every DB touch here is synchronous ``psycopg2``, so each command has an ``_async``
twin (:func:`submit_async` / :func:`interrupt_async`) that runs the blocking part
in a worker thread and keeps the loop-bound part (the kick / the interrupt
executor, both of which need a running loop) on the caller's event loop. **Async
callers must use the ``_async`` twins** — calling the sync form from a coroutine
blocks every other connection on the worker.

The *kick* is an injected seam: this module does not know how a queued turn gets
started — the runtime/scheduler (Phase 2 A5/A6) registers a kick callback via
:func:`register_kick`. Until something registers one the kick is a no-op, so
``submit`` still safely enqueues (the reap/boot sweep or a later kick will pick
the turn up). Callers of this module are wired in later commits (A6/A7); nothing
here imports a caller.
"""

from __future__ import annotations

import asyncio
import time
import logging
import threading
from typing import Any, Callable, Dict, Optional, Set, Tuple

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
# "resume" is a person pressing Retry on a failed turn — a newer instruction
# than an armed wake for the same reason a typed message is, even though it
# carries no prompt of its own.
_WAKEUP_SUPERSEDING_SOURCES = ("user", "queue", "resume")

# Sources whose ``overrides`` carry a PERSON's current pick of model / effort /
# permission mode, so the session's stored binding (v79) follows them. An
# automation, a wakeup replay, and a Task worker all run with overrides that
# were decided elsewhere — writing those back would silently re-bind a session
# the user never touched (a wakeup replays the model the SCHEDULING turn used,
# which may be older than what the user has since chosen).
_BINDING_SOURCES = ("user", "queue", "api", "worktree", "connector")


def _submit_blocking(
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
) -> str:
    """Every BLOCKING part of :func:`submit` — up to three DB round trips (run
    binding, wakeup supersede, enqueue) — and nothing else.

    Split out so :func:`submit_async` can pay ONE thread hop for the whole
    logical operation instead of one per write, while the kick (which needs a
    running event loop) stays with the caller. Never fires the kick.

    Returns the new turn's id. The turn is durably ``queued`` before the kick
    fires, so even if the kick is a no-op (nothing registered yet) or fails, the
    work is never lost — a later kick or the periodic reap sweep starts it.

    A turn from a human-facing source also PERSISTS its model / effort /
    permission overrides onto the session row (v79). Agents no longer pin a
    model, so the composer's current pick is the session's binding, and writing
    it here is what makes a mid-session switch survive a reload, reach another
    device, and govern the next server-initiated turn.

    A genuine user turn (``source`` in ``user``/``queue``) SUPERSEDES the
    session's pending wakeups: whatever an agent armed for later, the person
    who just typed is the newer instruction, and the agent can re-arm on its
    next turn. Without this a parked session with an armed wake would take the
    user's turn and then a second, stale one on top of it.
    """
    if source in _BINDING_SOURCES and overrides:
        try:
            from app.db import database

            database.set_session_run_binding(
                session_id,
                model=overrides.get("model"),
                effort=overrides.get("effort"),
                permission_mode=overrides.get("permission_mode"),
                context_window=overrides.get("context_window"),
                fast=overrides.get("fast"),
                workspace_project_ids=overrides.get("workspace_project_ids"),
            )
        except Exception:  # noqa: BLE001 — never let this break an enqueue.
            logger.exception(
                "run-binding persist failed for session %s", session_id
            )
    if source in _WAKEUP_SUPERSEDING_SOURCES:
        try:
            from app.core import wakeup_service

            wakeup_service.cancel_session_wakeups(session_id)
        except Exception:  # noqa: BLE001 — never let this break an enqueue.
            logger.exception(
                "wakeup supersede failed for session %s", session_id
            )
        # ...and release the Stop latch. Stop means "nothing wakes this session
        # until the user sends a new message" (round 7); this IS that message,
        # so every wake producer is live again from here on. The same source set
        # as the wakeup supersede above, for the same reason — these are the
        # turns a PERSON authors. A server-originated turn must never lift a
        # stop the person asked for.
        try:
            from app.core.task_registry import task_registry

            if task_registry.clear_user_stop(session_id):
                logger.info(
                    "turn_manager: %s turn released the user-stop latch on %s",
                    source, session_id,
                )
        except Exception:  # noqa: BLE001 — never let this break an enqueue.
            logger.exception(
                "stop-latch release failed for session %s", session_id
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
    return turn["id"]


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
    """Enqueue a new turn and kick the scheduler — the SYNCHRONOUS form.

    See :func:`_submit_blocking` for the full contract. This does the DB work on
    the CALLING thread, so it is for sync callers only (the wakeup service, the
    session-delete path, worker threads). **From a coroutine use
    :func:`submit_async`** — three sync DB round trips on the event loop stall
    every WebSocket, SSE stream and HTTP request on the worker.

    ``no_kick=True`` skips the in-process kick entirely — used by the V2
    placement submit seam (Phase 4) when the session is placed on ANOTHER
    runner: the durable queued row is the whole hand-off (that runner's
    turn_claim loop picks it up via NOTIFY wake or its 5s poll), and a local
    kick would only lose the placement-gate race in session_runtime.execute.
    The skip is an optimization, not a correctness gate — the execute-side
    placement check is the backstop either way.
    """
    turn_id = _submit_blocking(
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
    return turn_id


async def submit_async(
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
    """:func:`submit` for async callers — identical semantics, off the loop.

    ONE thread hop covers all three DB writes; the kick then fires HERE, on the
    caller's event loop, because the registered kick
    (``turn_scheduler.kick``) needs ``asyncio.get_running_loop()`` to schedule
    the start and silently drops the nudge from a worker thread. Ordering is
    unchanged: the turn is durably ``queued`` before the kick.
    """
    turn_id = await asyncio.to_thread(
        _submit_blocking,
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
    return turn_id


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


def _interrupt_turn(session_id, mode, stop_background=False, turn_id=None):
    # A dead runner cannot observe interrupt intent. Settle its turn and append
    # the terminal event so connected clients also leave their working state.
    if mode == "stop":
        recovered = turns.stop_orphaned_turns(session_id, turn_id)
        if recovered:
            from app.db.session_events import append_events

            append_events(session_id, [
                {"type": "turn.aborted", "turn_id": row["id"],
                 "payload": {"reason": "interrupt", "message": "Session stopped"}}
                for row in recovered
            ])
            return recovered[0]
    return turns.interrupt_turn(
        session_id, mode, stop_background=stop_background, turn_id=turn_id,
    )


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
    escalation). A Stop with no valid lease settles the orphaned turn and
    records its terminal event instead. Returns the changed turn row, or
    ``None`` when there is no matching live turn.

    ``turn_id`` (optional) binds the interrupt to a specific turn identity: it
    is threaded to BOTH the DB transition (so a stale stop whose target turn
    already settled matches zero rows → ``None`` → no executor fire) and the
    runtime signal (so the live runtime is only signalled when it is executing
    that turn). ``None`` preserves the legacy session-scoped behavior used by
    the internal auto/resource/send-now callers (GitHub #585).

    SYNCHRONOUS: the guarded UPDATE runs on the calling thread. **From a
    coroutine use :func:`interrupt_async`** — this is the stop button's hot
    path, and a sync DB write there freezes every other connection.
    """
    row = _interrupt_turn(
        session_id, mode, stop_background=stop_background, turn_id=turn_id
    )
    if row is not None and row["state"] == "interrupting":
        _fire_interrupt_executor(session_id, mode, turn_id)
    if mode == "send_now":
        # Parking releases the live slot. There may be nothing to interrupt,
        # but the user's already-queued messages still need a scheduling tick.
        # A live turn keeps the early kick out; its settlement drains as usual.
        _kick(session_id)
    return row


async def interrupt_async(
    session_id: str,
    mode: str,
    stop_background: bool = False,
    turn_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """:func:`interrupt` for async callers — identical semantics, off the loop.

    The durable ``running -> interrupting`` flip runs in a worker thread; the
    executor then fires HERE, on the caller's event loop, because the registered
    executor is a coroutine function that needs a running loop to be scheduled
    (from a worker thread ``_fire_interrupt_executor`` would have to drop it).
    Ordering is unchanged: durable row first, runtime signal second.
    """
    row = await asyncio.to_thread(
        _interrupt_turn,
        session_id,
        mode,
        stop_background=stop_background,
        turn_id=turn_id,
    )
    if row is not None and row["state"] == "interrupting":
        _fire_interrupt_executor(session_id, mode, turn_id)
    if mode == "send_now":
        # Parking releases the live slot. There may be nothing to interrupt,
        # but the user's already-queued messages still need a scheduling tick.
        # A live turn keeps the early kick out; its settlement drains as usual.
        _kick(session_id)
    return row

# ---------------------------------------------------------------------------
# Wait helpers (event-driven, with a backing-off poll as the safety net)
# ---------------------------------------------------------------------------
#
# These block a caller (automation, the cluster/cloud intake, the voice bridge,
# tests) for as long as five minutes. The original implementation re-read the
# turns table every 50 ms with SYNCHRONOUS psycopg2 **on the event loop** — one
# waiting automation cost 20–60 blocking queries/second and stalled every other
# coroutine on the worker for the whole wait (perf review 2026-08, finding F2).
#
# The replacement has two legs:
#
# * **Latency** rides the PostgreSQL ``session_events`` NOTIFY gateway. Every
#   terminal turn event (``turn.completed`` / ``turn.aborted`` / ``turn.parked``)
#   is appended to ``session_events``, and that append fires ``pg_notify`` on the
#   ``session_events`` channel (see :mod:`app.db.session_events`). The gateway's
#   LISTEN thread hands us the tick, we set the waiter's event and re-check at
#   once — so the common case settles in one extra query, within a millisecond
#   of the turn actually settling.
# * **Correctness** rides a poll that starts at the caller's ``poll_interval``
#   and doubles up to :data:`MAX_POLL_INTERVAL`. With PostgreSQL unreachable, on
#   the SQLite test shim, or for a settle that somehow appends no event, the poll
#   alone still returns the right answer, just later: a full 300 s wait costs
#   ~155 reads at the 2 s ceiling instead of the ~6000 the 50 ms cadence issued
#   (~0.5/s instead of 20/s), and with the gateway healthy it is two.
#
# Both legs run their DB reads through :func:`asyncio.to_thread`, so nothing in
# here ever touches psycopg2 from the loop thread.

# Ceiling for the exponential poll backoff. A caller that explicitly asks for a
# SLOWER cadence than this keeps its own interval (the ceiling is never allowed
# to speed a poll up).
MAX_POLL_INTERVAL = 2.0
# asyncio treats a timer as due when it is within one clock resolution of
# firing, so a wait can return that much before the deadline it was given.
_CLOCK_SLACK = time.get_clock_info("monotonic").resolution

# Growth factor per un-woken poll.
POLL_BACKOFF_FACTOR = 2.0

# session_id -> set of (loop, asyncio.Event) waiter pairs. Mutated from the
# gateway's LISTEN thread as well as from the loop, so it is guarded by a
# THREADING lock (never an asyncio one).
_waiters: Dict[str, Set[Tuple[Any, Any]]] = {}
_waiters_lock = threading.Lock()
_gateway_listener_registered = False


def _on_gateway_tick(session_id: str) -> None:
    """Gateway global listener — runs on the LISTEN thread, so it must be cheap
    and must never raise. Sets every waiter event registered for the session."""
    with _waiters_lock:
        pairs = list(_waiters.get(session_id, ()))
    for loop, event in pairs:
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:
            pass  # Loop already closed — that waiter is gone anyway.


def _ensure_gateway_listener() -> None:
    """Register the gateway global listener exactly once (best-effort).

    No gateway (PostgreSQL unreachable, test shim) ⇒ the waits degrade to the
    backing-off poll, which is still correct — only slower to notice. Mirrors
    :func:`app.core.interaction_wait._ensure_listener`.
    """
    global _gateway_listener_registered
    if _gateway_listener_registered:
        return
    try:
        from app.core.event_gateway import get_gateway

        get_gateway().add_global_listener(_on_gateway_tick)
        _gateway_listener_registered = True
    except Exception:  # noqa: BLE001
        logger.warning(
            "turn_manager: gateway listener unavailable — wait helpers fall "
            "back to poll-only cadence",
            exc_info=True,
        )


class _SessionWake:
    """A wake handle for one session's NOTIFY ticks.

    ``session_id`` may be unknown at construction time (``wait_settled`` only
    learns it from the turn row it is waiting on), so the binding is lazy and
    idempotent — :meth:`bind` registers on the first real id and does nothing
    thereafter. Always used from a single coroutine; :meth:`close` MUST run in
    that coroutine's ``finally`` so a cancelled wait leaves no registration
    behind.
    """

    __slots__ = ("_event", "_loop", "_session_id")

    def __init__(self, session_id: Optional[str] = None) -> None:
        self._session_id: Optional[str] = None
        self._event = asyncio.Event()
        self._loop = asyncio.get_running_loop()
        self.bind(session_id)

    def bind(self, session_id: Optional[str]) -> None:
        """Register for ``session_id``'s ticks (first non-empty id wins)."""
        if not session_id or self._session_id is not None:
            return
        _ensure_gateway_listener()
        with _waiters_lock:
            _waiters.setdefault(session_id, set()).add((self._loop, self._event))
        self._session_id = session_id

    async def wait(self, timeout: float) -> bool:
        """Sleep until a NOTIFY tick arrives or ``timeout`` elapses.

        Returns ``True`` when a tick woke us (the caller resets its backoff:
        the session is moving), ``False`` on the plain timeout.
        """
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        finally:
            self._event.clear()
        return True

    def close(self) -> None:
        """Drop the registration; the session key goes with the last waiter."""
        session_id = self._session_id
        if session_id is None:
            return
        self._session_id = None
        with _waiters_lock:
            pairs = _waiters.get(session_id)
            if not pairs:
                return
            pairs.discard((self._loop, self._event))
            if not pairs:
                _waiters.pop(session_id, None)


def _next_interval(interval: float, poll_interval: float) -> float:
    """Back off one step, never past the ceiling (or the caller's own, slower,
    cadence)."""
    return min(interval * POLL_BACKOFF_FACTOR, max(MAX_POLL_INTERVAL, poll_interval))


def _session_is_idle(session_id: str) -> bool:
    """No turn row in ANY non-terminal state — one atomic read.

    This must be a SINGLE statement (``turns.session_has_unsettled_turns``),
    never separate live/queued reads: the queued→running handoff moves a turn
    from the second-checked set into the first-checked set, so a two-read
    predicate that straddled ``start_turn``'s commit saw neither and declared
    a session idle while its turn was running (the automation
    empty-"completed" run race, 2026-08-28). Blocking — call off the loop.
    """
    return not turns.session_has_unsettled_turns(session_id)


async def wait_settled(
    turn_id: str,
    timeout: float = 300.0,
    poll_interval: float = 0.05,
) -> Optional[Dict[str, Any]]:
    """Wait until ``turn_id`` reaches a terminal state (completed/aborted/parked)
    and return its row, or ``None`` on timeout / unknown turn.

    Used by automation (block until a headless turn finishes) and tests.

    Wakes on the session's NOTIFY ticks; ``poll_interval`` is the FIRST re-check
    delay of the backstop poll, which then backs off exponentially to
    :data:`MAX_POLL_INTERVAL`. Return values and timeout semantics are exactly
    those of the original 50 ms poll: the state is checked once before any sleep,
    an unknown turn keeps being retried until the deadline, and a lapsed deadline
    yields ``None``.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    interval = poll_interval
    wake = _SessionWake()
    try:
        while True:
            turn = await asyncio.to_thread(turns.get_turn, turn_id)
            if turn is not None:
                if turn["state"] in turns.TERMINAL_STATES:
                    return turn
                # Only now do we know which session's ticks to listen for.
                wake.bind(turn.get("session_id"))
            remaining = deadline - loop.time()
            # asyncio may fire a timer up to one clock resolution early, so
            # a deadline within that slack has lapsed: without this the final
            # backstop wait returns with microseconds left and the loop reads
            # the row one extra time before giving up.
            if remaining <= _CLOCK_SLACK:
                return None
            if await wake.wait(min(interval, remaining)):
                interval = poll_interval  # Session is moving — stay responsive.
            else:
                interval = _next_interval(interval, poll_interval)
    finally:
        wake.close()


async def wait_session_idle(
    session_id: str,
    timeout: float = 300.0,
    poll_interval: float = 0.05,
) -> bool:
    """Wait until ``session_id`` is idle — no live turn AND no queued turns —
    and return ``True``, or ``False`` on timeout.

    Used by the non-WS intake callers (automation / cluster / cloud) that submit
    then block until the session has fully drained. Same wake/backoff design and
    the same timeout semantics as :func:`wait_settled`.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    interval = poll_interval
    wake = _SessionWake(session_id)
    try:
        while True:
            if await asyncio.to_thread(_session_is_idle, session_id):
                return True
            remaining = deadline - loop.time()
            # asyncio may fire a timer up to one clock resolution early, so
            # a deadline within that slack has lapsed: without this the final
            # backstop wait returns with microseconds left and the loop reads
            # the row one extra time before giving up.
            if remaining <= _CLOCK_SLACK:
                return False
            if await wake.wait(min(interval, remaining)):
                interval = poll_interval
            else:
                interval = _next_interval(interval, poll_interval)
    finally:
        wake.close()
