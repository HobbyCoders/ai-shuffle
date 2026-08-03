"""Cross-runner turn claim + interrupt observer (V2 Phase 4, Q3).

This is the piece that replaces the legacy HTTP relay DISPATCH: when a WS
handler on install A enqueues a turn for a session placed on install B, nothing
on A executes it — B's copy of this loop notices the durable ``queued`` row and
kicks B's own scheduler, which claims the session's run lease and executes
exactly as if the turn had been submitted locally (OD-1).

Two passes per tick, both cheap indexed reads against shared tables:

* **Claim pass** — ``turns.list_claimable_sessions()`` (queued turns with no
  live run lease, distinct sessions) → resolve each session's placement →
  ``turn_scheduler.kick`` ONLY the sessions whose decision is explicitly
  ``("runner", me)``. ``kind == "local"`` is deliberately EXCLUDED: placement
  resolution is install-relative, and the only rows that still resolve "local"
  after the A11 instance_id fallback are OWNERLESS (NULL placement AND blank
  ``instance_id``) — those resolve "local" on EVERY install, so acting on them
  here would let install B steal install A's local-default sessions. Owned
  NULL-placement rows resolve deterministically to ``("runner", <owner>)`` via
  A11 and ARE claimable — but only by their owner. Local sessions are kicked
  by their own submit path (and the reap sweep), never by this loop.

* **Interrupt-observe pass** — ``turns.list_interrupting_for_runner(me)``:
  a foreign install's WS handler flips ``turns`` to ``interrupting`` durably,
  but its in-process interrupt executor cannot reach the runtime living HERE.
  This pass bridges that gap: for each of MY turns durably marked
  interrupting, drive the live runtime's ``interrupt(mode)`` (fire-and-forget —
  it can block up to the 8s escalation window). Idempotent via the runtime's
  per-turn ``_interrupt_driving`` guard (O4), so re-observing the same flip on
  the next tick is a no-op.

Cadence (OD-1): a ≥5s slow poll is the CORRECTNESS mechanism; a NOTIFY wake
off the ``session_events`` gateway channel is the LATENCY mechanism (the
submit/interrupt seams fire ``session_events.notify_session`` after writing).
The gateway listener fires on EVERY event tick of every session — including
each streaming delta batch — so wakes are damped by a cooldown floor: at most
one pass per ``WAKE_COOLDOWN_SECONDS``, and both passes are tiny indexed reads.

Started from ``startup.install_v2_turns_wiring``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional, Set

from app.core.config import settings

logger = logging.getLogger(__name__)

# Correctness cadence (OD-1: "≥5s slow poll, never a hot poll").
CLAIM_POLL_SECONDS = 5.0
# Latency damping: NOTIFY ticks arrive per event batch during streaming; never
# run passes more often than this.
WAKE_COOLDOWN_SECONDS = 1.0

# Loop plumbing — set by claim_loop() on its own event loop; the gateway
# listener thread only ever touches them via call_soon_threadsafe.
_wake: Optional[asyncio.Event] = None
_loop: Optional[asyncio.AbstractEventLoop] = None
_loop_task: Optional[asyncio.Task] = None

# Strong refs to fire-and-forget interrupt-drive tasks (asyncio only holds
# weak refs; a GC'd task silently vanishes mid-escalation).
_interrupt_tasks: Set[asyncio.Task] = set()


def notify_turns_changed(session_id: str) -> None:
    """Best-effort NOTIFY wake for peer claim loops (sync — call via run_sync).

    Fired by the placement submit/interrupt seams right after the durable
    turns write, so the owning runner reacts in NOTIFY time instead of poll
    time. Pure latency optimization: correctness rides the 5s poll (OD-1).
    """
    from app.db import session_events

    session_events.notify_session(session_id)


def _on_gateway_tick(session_id: str) -> None:
    """Gateway global listener — runs on the LISTEN thread; must be cheap and
    never raise. Just sets the wake event on the claim loop's event loop."""
    loop, wake = _loop, _wake
    if loop is None or wake is None:
        return
    try:
        loop.call_soon_threadsafe(wake.set)
    except RuntimeError:
        pass  # loop already closed (shutdown) — poll cadence is gone anyway.


async def claim_once() -> int:
    """Run one observe+claim pass. Returns the number of sessions kicked.

    Never raises: each session is handled independently and a failure on one
    (racing deletion, resolver error) must not starve the rest.
    """
    me = settings.effective_instance_id

    # --- Interrupt-observe pass (first: reacting to a stop beats starting
    # new work, and the list is almost always empty). ---
    try:
        from app.core import session_runtime
        from app.db import turns as turns_db

        interrupting = await asyncio.to_thread(
            turns_db.list_interrupting_for_runner, me
        )
    except Exception:  # noqa: BLE001
        logger.warning("turn_claim: interrupt-observe read failed", exc_info=True)
        interrupting = []
    for row in interrupting:
        sid = row["session_id"]
        runtime = session_runtime.get_live_runtime(sid)
        if runtime is None or runtime.turn_id != row["id"]:
            # Not (or no longer) live in this process — a stale row after a
            # crash is the reap sweep's job, not ours.
            continue
        mode = row.get("interrupt_mode") or "stop"
        # Fire-and-forget: interrupt() can hold for the full escalation window;
        # the loop must keep ticking. The runtime's per-turn guard (O4) makes
        # duplicate observations of the same flip no-ops.
        task = asyncio.create_task(runtime.interrupt(mode))
        _interrupt_tasks.add(task)
        task.add_done_callback(_interrupt_tasks.discard)
        task.add_done_callback(lambda t: t.cancelled() or t.exception())

    # --- Claim pass. ---
    claimed = 0
    try:
        from app.db import turns as turns_db

        session_ids = await asyncio.to_thread(turns_db.list_claimable_sessions)
    except Exception:  # noqa: BLE001
        logger.warning("turn_claim: claimable-session read failed", exc_info=True)
        return claimed
    if not session_ids:
        return claimed

    from app.core import placement as placement_mod
    from app.core import turn_scheduler
    from app.db import database

    for sid in session_ids:
        try:
            session_row = await asyncio.to_thread(database.get_session, sid)
            if session_row is None:
                continue
            decision = await asyncio.to_thread(
                placement_mod.resolve_placement, session_row
            )
        except Exception:  # noqa: BLE001
            logger.debug("turn_claim: resolution failed for %s", sid, exc_info=True)
            continue
        # ONLY an explicit ("runner", me) decision is claimable here — see the
        # module docstring for why "local" must never be acted on by this loop.
        if decision.kind == "runner" and decision.runner_id == me:
            logger.info(
                "turn_claim: claiming session %s (%s)", sid, decision.reason
            )
            turn_scheduler.kick(sid)
            claimed += 1
    return claimed


async def claim_loop() -> None:
    """The background loop: pass every CLAIM_POLL_SECONDS, sooner on NOTIFY."""
    global _loop, _wake
    _loop = asyncio.get_running_loop()
    _wake = asyncio.Event()

    try:
        from app.core.event_gateway import get_gateway

        get_gateway().add_global_listener(_on_gateway_tick)
    except Exception:  # noqa: BLE001 — no gateway ⇒ poll-only cadence, still correct.
        logger.warning(
            "turn_claim: gateway listener unavailable — poll-only cadence",
            exc_info=True,
        )

    logger.info(
        "turn_claim: loop started (poll every %ss + NOTIFY wake, runner=%s)",
        int(CLAIM_POLL_SECONDS), settings.effective_instance_id,
    )
    while True:
        try:
            try:
                await asyncio.wait_for(_wake.wait(), timeout=CLAIM_POLL_SECONDS)
            except asyncio.TimeoutError:
                pass
            _wake.clear()
            await claim_once()
            # Damping floor: NOTIFY ticks fire per event batch during
            # streaming; never pass more than once per cooldown.
            await asyncio.sleep(WAKE_COOLDOWN_SECONDS)
        except asyncio.CancelledError:
            break
        except Exception:  # noqa: BLE001 — the loop must survive anything.
            logger.warning("turn_claim: pass failed", exc_info=True)
            try:
                await asyncio.sleep(WAKE_COOLDOWN_SECONDS)
            except asyncio.CancelledError:
                break
    logger.info("turn_claim: loop stopped")


def start() -> None:
    """Start the claim loop on the running event loop (idempotent).

    Called from ``startup.install_v2_turns_wiring`` (which runs inside the
    lifespan startup, so a loop is running). The task ref is module-held so it
    is never GC'd; ``shutdown_services`` cancels it via :func:`stop` (rF4).
    """
    global _loop_task
    if _loop_task is not None and not _loop_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("turn_claim.start(): no running event loop — not started")
        return
    _loop_task = loop.create_task(claim_loop())


async def stop() -> None:
    """Cancel the claim loop and wait for it to exit (idempotent; rF4).

    Called from ``startup.shutdown_services`` so teardown cancels the loop
    cleanly instead of relying on process death — a pass racing the session-
    pool shutdown could otherwise kick the scheduler into starting new work
    mid-teardown. No-op when the loop was never started (flag OFF, or a test
    process that never called :func:`start`).
    """
    global _loop_task
    task = _loop_task
    _loop_task = None
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001 — teardown must never raise.
        logger.debug("turn_claim: loop raised during shutdown", exc_info=True)
