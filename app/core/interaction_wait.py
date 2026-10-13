"""NOTIFY-awaited pending_interactions waiter.

The v2 permission/question adapters (``permission_handler`` /
``user_question_handler``) persist each prompt as a durable
``pending_interactions`` row and then block the adapter callback in
:func:`await_interaction` until the row leaves ``pending``. The wake plumbing
mirrors ``turn_claim``:

* **Correctness** rides a ≥5s slow poll re-reading the row, plus a local
  ``expires_at`` deadline (belt + braces with the sweeper — see
  ``app/core/interaction_sweeper.py``).
* **Latency** rides two wake paths onto a per-session :class:`asyncio.Event`:
  the event-gateway global listener (a peer install's answer fires
  ``session_events.notify_session`` after its guarded UPDATE, the LISTEN
  thread ticks us) and a same-process fast path (:func:`notify_changed` ticks
  local waiters directly, so a local answer never waits on a PG round-trip).

Flag OFF ⇒ nothing imports this module's registration path and the legacy
in-memory ``asyncio.Event``-per-request flow runs untouched.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Dict, Optional, Set, Tuple

from app.core.detached_tasks import spawn_detached

logger = logging.getLogger(__name__)

# Slow-poll cadence (correctness floor; NOTIFY wakes are the latency path).
POLL_SECONDS = 5.0

# session_id -> set of (loop, event) waiter pairs. Touched from the gateway
# LISTEN thread, so guarded by a threading lock (not an asyncio one).
_waiters: Dict[str, Set[Tuple[object, object]]] = {}
_lock = threading.Lock()
_listener_registered = False

# session_id -> number of await_interaction calls blocked on the user right now.
# The first one pauses the session's working-time clock, the last one out
# resumes it (sessions.work_ms, #1619).
_blocked: Dict[str, int] = {}


def _on_gateway_tick(session_id: str) -> None:
    """Gateway global listener — runs on the LISTEN thread; must be cheap and
    never raise. Sets every registered waiter event for the session."""
    with _lock:
        pairs = list(_waiters.get(session_id, ()))
    for loop, event in pairs:
        try:
            loop.call_soon_threadsafe(event.set)  # type: ignore[attr-defined]
        except RuntimeError:
            pass  # loop already closed — waiter is gone anyway.


def _ensure_listener() -> None:
    """Register the gateway global listener exactly once (best-effort).

    No gateway (PG unreachable, test shim) ⇒ poll-only cadence, still correct.
    Tests monkeypatch this to a no-op so the sqlite suite never spins up a
    LISTEN thread.
    """
    global _listener_registered
    if _listener_registered:
        return
    try:
        from app.core.event_gateway import get_gateway

        get_gateway().add_global_listener(_on_gateway_tick)
        _listener_registered = True
    except Exception:  # noqa: BLE001
        logger.warning(
            "interaction_wait: gateway listener unavailable — poll-only cadence",
            exc_info=True,
        )


def register_waiter(session_id: str) -> asyncio.Event:
    """Register and return a fresh wake event for ``session_id``."""
    _ensure_listener()
    loop = asyncio.get_running_loop()
    event = asyncio.Event()
    with _lock:
        _waiters.setdefault(session_id, set()).add((loop, event))
    return event


def has_waiters(session_id: str) -> bool:
    """True while something in THIS process blocks on an interaction for
    ``session_id``.

    The session pool reads this to keep a turn that is parked on a user
    question (or a permission / plan gate) out of idle eviction: the turn is
    live but silent for as long as the human takes, and reclaiming its client
    mid-question cancels the engine's round-trip — fluid then answers the tool
    with ``input_required round-trip interrupted`` and the device is left
    showing a modal nobody is waiting on. Cheap: one dict lookup under the
    registry lock, no DB.
    """
    with _lock:
        return bool(_waiters.get(session_id))


def unregister_waiter(session_id: str, event: asyncio.Event) -> None:
    """Remove a waiter; drop the session key once empty."""
    with _lock:
        pairs = _waiters.get(session_id)
        if not pairs:
            return
        for pair in list(pairs):
            if pair[1] is event:
                pairs.discard(pair)
        if not pairs:
            _waiters.pop(session_id, None)


def _notify_session_blocking(session_id: str) -> None:
    """Fire the cross-process ``session_events`` NOTIFY. Blocking (a libpq
    round-trip), so it must run OFF the event loop. Any failure (SQLite shim,
    transient conn error) is swallowed — correctness rides the poll cadence."""
    try:
        from app.db import session_events

        session_events.notify_session(session_id)
    except Exception:  # noqa: BLE001
        logger.debug(
            "interaction_wait: notify_session failed (non-fatal)", exc_info=True
        )


def notify_changed(session_id: str) -> None:
    """Wake waiters after an interaction write.

    Two legs, both best-effort:
    * same-process fast path: tick local waiters directly (the answering
      handler usually lives in the same process as the awaiting adapter callback);
    * cross-process: ``pg_notify`` on the ``session_events`` channel so the
      OWNING runner's gateway listener ticks its waiters.

    The cross-process leg is a blocking libpq call, so on the event loop it is
    scheduled off-loop via :func:`asyncio.to_thread` in a fire-and-forget task
    (never run synchronously on the loop). Called from a worker thread already
    (no running loop — e.g. the sweeper's ``to_thread``) it runs inline. Either
    way a NOTIFY failure is swallowed and never raised to the caller.
    """
    _on_gateway_tick(session_id)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No running loop (called from a worker thread): already off-loop, so
        # run the blocking NOTIFY inline (failure-isolated inside the helper).
        _notify_session_blocking(session_id)
        return
    spawn_detached(asyncio.to_thread(_notify_session_blocking, session_id))


def _blocked_delta(session_id: str, delta: int) -> int:
    """Adjust the session's blocked-waiter count; returns the new count."""
    with _lock:
        count = max(0, _blocked.get(session_id, 0) + delta)
        if count:
            _blocked[session_id] = count
        else:
            _blocked.pop(session_id, None)
        return count


async def _set_work_paused(session_id: str, paused: bool) -> None:
    """Pause or resume the session's working-time clock and push the change.

    Time a turn spends blocked on the user is not working time (owner
    decision, 2026-10-01). Best-effort: bookkeeping must never break the
    prompt round-trip, and the device push is fire-and-forget.
    """
    try:
        from app.db import turns

        fn = turns.pause_session_work if paused else turns.resume_session_work
        if not await asyncio.to_thread(fn, session_id):
            return
        from app.core.session_runtime import _broadcast_work_time

        spawn_detached(_broadcast_work_time(session_id))
    except Exception:  # noqa: BLE001
        logger.debug(
            "interaction_wait: work-time %s failed for %s",
            "pause" if paused else "resume", session_id, exc_info=True,
        )


async def await_interaction(
    interaction_id: str,
    session_id: str,
    ttl_seconds: float,
    poll_seconds: float = POLL_SECONDS,
) -> Optional[Dict[str, Any]]:
    """Block until the interaction leaves ``pending`` (or the local TTL lapses).

    Returns the freshest row: state ``answered``/``cancelled``/``expired`` on a
    real transition, or a still-``pending`` row once the LOCAL deadline is
    reached (the caller treats that as expired; the sweeper flips the durable
    row within its cadence). Returns ``None`` if the row vanished.
    """
    from app.db import interactions

    event = register_waiter(session_id)
    first_blocked = _blocked_delta(session_id, +1) == 1
    try:
        # The turn is now blocked on the user: stop its working-time clock
        # until the last pending prompt resolves.
        if first_blocked:
            await _set_work_paused(session_id, True)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + ttl_seconds
        while True:
            row = await asyncio.to_thread(interactions.get_interaction, interaction_id)
            if row is None or row.get("state") != "pending":
                return row
            remaining = deadline - loop.time()
            if remaining <= 0:
                return row  # still pending, past the local deadline.
            try:
                await asyncio.wait_for(
                    event.wait(), timeout=min(poll_seconds, remaining)
                )
            except asyncio.TimeoutError:
                pass
            event.clear()
    finally:
        unregister_waiter(session_id, event)
        if _blocked_delta(session_id, -1) == 0:
            # Shielded so a cancelled waiter still reopens the clock; the
            # resume is a no-op once the turn has left the live slot.
            await asyncio.shield(_set_work_paused(session_id, False))
