"""Cross-runner turn claim + interrupt observer.

A turn enqueued on install A for a session placed on install B is noticed and
executed by B's copy of this loop: nothing on A executes it — B sees the
durable ``queued`` row and kicks B's own scheduler, which claims the session's
run lease and executes exactly as if the turn had been submitted locally.

Two passes per tick, both cheap indexed reads against shared tables:

* **Claim pass** — ``turns.list_claimable_sessions()`` (queued turns with no
  live run lease, distinct sessions) → resolve each session's placement →
  ``turn_scheduler.kick`` ONLY the sessions whose decision is explicitly
  ``("runner", me)``. ``kind == "local"`` is deliberately EXCLUDED: placement
  resolution is install-relative, and the only rows that still resolve "local"
  after the ``instance_id`` fallback are OWNERLESS (NULL placement AND blank
  ``instance_id``) — those resolve "local" on EVERY install, so acting on them
  here would let install B steal install A's local-default sessions. Owned
  NULL-placement rows resolve deterministically to ``("runner", <owner>)`` via
  that fallback and ARE claimable — but only by their owner. Local sessions are kicked
  by their own submit path (and the reap sweep), never by this loop.

* **Interrupt-observe pass** — ``turns.list_interrupting_for_runner(me)``:
  a foreign install's WS handler flips ``turns`` to ``interrupting`` durably,
  but its in-process interrupt executor cannot reach the runtime living HERE.
  This pass bridges that gap: for each of MY turns durably marked
  interrupting, drive the live runtime's ``interrupt(mode)`` (fire-and-forget —
  it can block up to the 8s escalation window). Idempotent via the runtime's
  per-turn ``_interrupt_driving`` guard (``SessionRuntime``), so re-observing the same flip on
  the next tick is a no-op.

Cadence: a ≥5s slow poll is the CORRECTNESS mechanism; a NOTIFY wake
off the ``session_events`` gateway channel is the LATENCY mechanism (the
submit/interrupt seams fire ``session_events.notify_session`` after writing).
The gateway listener fires on EVERY event tick of every session — including
each streaming delta batch — so wakes are damped by a cooldown floor: at most
one pass per ``WAKE_COOLDOWN_SECONDS``, and both passes are tiny indexed reads.

Started from ``startup.install_v2_turns_wiring``.


THE UNIFIED BACKGROUND SWEEP
----------------------------
This module also hosts the driver that the cross-install correctness loops
share. Every cross-install correctness loop registers a :class:`SweepPass`
here (via :func:`register_pass`) and shares one timer; the driver runs each
pass when it comes due, batching everything that falls in the same tick.

Two things make the idle cost collapse:

* **One timer instead of N.** Passes that fall due together are executed in one
  wake-up. The driver sleeps exactly until the next due pass and not at all in
  between.

* **Single-install detection.** Every registered pass exists to notice work
  that a PEER INSTALL wrote into a shared table. When the install registry
  contains no peer, nothing but this process can write those rows, so the tight
  cadence buys nothing — the driver stretches every pass to
  ``settings.single_install_sweep_interval_seconds`` (60s). The moment a peer
  row appears the tight per-pass cadences come back, and the answer is cached
  for only ``settings.single_install_peer_check_seconds`` (and invalidated
  immediately whenever a pass finds real work).

  SAFETY. The stretch changes LATENCY, never correctness, and each pass keeps a
  latency path that does not depend on the poll at all:
    - turn claim: the submit/interrupt seams fire ``notify_session`` after the
      durable write, and this driver runs the turn-claim pass on the resulting
      NOTIFY tick regardless of its schedule. The poll is the fallback for a
      LOST notify, and a lost notify on an install with no peers can only
      concern a row this same process wrote.
    - git-ops claim: the one latency-critical case (first turn of a freshly
      created remote worktree session) is claimed INLINE by the spawn path in
      ``app.core.query_options``, never by this loop; :func:`kick` is available
      for an enqueue seam that wants immediate execution.
    - interaction expiry: the ``interaction_wait`` adapter honours its own local
      ``expires_at`` deadline, so a late sweep can never wedge a turn; the sweep
      only makes the lapse durable for peers and reconnecting clients — and on
      a single install "peers" is the empty set.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

from app.core.config import settings

logger = logging.getLogger(__name__)

# Correctness cadence: a ≥5s slow poll, never a hot poll.
CLAIM_POLL_SECONDS = 5.0
# Latency damping: NOTIFY ticks arrive per event batch during streaming; never
# run passes more often than this.
WAKE_COOLDOWN_SECONDS = 1.0

# Loop plumbing — set by sweep_loop() on its own event loop; the gateway
# listener thread only ever touches them via call_soon_threadsafe.
_wake: Optional[asyncio.Event] = None
_loop: Optional[asyncio.AbstractEventLoop] = None
_loop_task: Optional[asyncio.Task] = None

# Set by the gateway listener; consumed by the driver to decide whether the
# NOTIFY-driven passes should run on this wake. Distinguishing it from a plain
# wake keeps an unrelated kick() from paying for the turn-claim reads.
_notify_pending: bool = False

# Peer-install cache: (answer, monotonic timestamp). None = never resolved.
_peer_answer: Optional[bool] = None
_peer_checked_at: float = 0.0

# Strong refs to fire-and-forget interrupt-drive tasks (asyncio only holds
# weak refs; a GC'd task silently vanishes mid-escalation).
_interrupt_tasks: Set[asyncio.Task] = set()


def notify_turns_changed(session_id: str) -> None:
    """Best-effort NOTIFY wake for peer claim loops (sync — call via run_sync).

    Fired by the placement submit/interrupt seams right after the durable
    turns write, so the owning runner reacts in NOTIFY time instead of poll
    time. Pure latency optimization: correctness rides the 5s poll.
    """
    from app.db import session_events

    session_events.notify_session(session_id)


def _mark_notify() -> None:
    """Flag a pending NOTIFY and wake the driver (runs ON the driver's loop)."""
    global _notify_pending
    _notify_pending = True
    if _wake is not None:
        _wake.set()


def _on_gateway_tick(session_id: str) -> None:
    """Gateway global listener — runs on the LISTEN thread; must be cheap and
    never raise. Hands the driver's event loop a flag-and-wake callback; the
    flag is what tells the driver to run the NOTIFY-driven passes (the turn
    claim + interrupt-observe reads) irrespective of their poll schedule."""
    loop, wake = _loop, _wake
    if loop is None or wake is None:
        return
    try:
        loop.call_soon_threadsafe(_mark_notify)
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
        # the loop must keep ticking. The runtime's per-turn guard makes
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


# ---------------------------------------------------------------------------
# The unified sweep driver
# ---------------------------------------------------------------------------


@dataclass
class SweepPass:
    """One registered periodic read, multiplexed onto the shared driver.

    * ``tight_interval`` is the pass's own correctness cadence — what it polled
      at when it owned a private timer. It is used verbatim whenever peer
      installs exist.
    * ``wake_on_notify`` marks a pass whose latency contract is "run me on the
      gateway NOTIFY tick". Only the turn-claim pass sets it: NOTIFY ticks
      arrive per event batch during streaming, so waking anything else on them
      would trade an idle-cost win for a streaming-cost regression.
    * ``next_due`` is a monotonic timestamp.
    """

    name: str
    run: Callable[[], Awaitable[Any]]
    tight_interval: float
    wake_on_notify: bool = False
    next_due: float = field(default=0.0)
    # The pass's currently-executing task, if any. Passes run as INDEPENDENT
    # tasks, never inline on the driver: a git-ops claim can spend half a minute
    # inside a worktree checkout, and before the multiplexing that could not
    # delay a turn claim because they were separate loops. Sharing a timer must
    # not quietly reintroduce head-of-line blocking between unrelated passes.
    task: Optional[asyncio.Task] = field(default=None, repr=False)

    def in_flight(self) -> bool:
        return self.task is not None and not self.task.done()


_passes: Dict[str, SweepPass] = {}

# Strong refs to in-flight pass tasks (the loop only holds weak ones).
_pass_tasks: Set[asyncio.Task] = set()


def register_pass(
    name: str,
    run: Callable[[], Awaitable[Any]],
    tight_interval: float,
    *,
    wake_on_notify: bool = False,
    first_delay: Optional[float] = None,
) -> None:
    """Register (or replace) a periodic pass and make sure the driver is up.

    ``first_delay`` defaults to the pass's effective interval, i.e. a pass does
    NOT fire the instant it registers — matching the ``sleep(interval)``-first
    shape every one of these loops had when it owned its own task.

    Registration is order-independent: a module may register before or after
    :func:`start`, and the driver is spawned on demand either way. Re-registering
    an existing name UPDATES that pass in place (keeping its schedule and any
    in-flight execution) rather than replacing the object — a second ``start()``
    must not orphan a running pass and let a duplicate run alongside it.
    """
    delay = tight_interval if first_delay is None else first_delay
    existing = _passes.get(name)
    if existing is not None:
        existing.run = run
        existing.tight_interval = tight_interval
        existing.wake_on_notify = wake_on_notify
    else:
        _passes[name] = SweepPass(
            name=name,
            run=run,
            tight_interval=tight_interval,
            wake_on_notify=wake_on_notify,
            next_due=time.monotonic() + delay,
        )
    _ensure_driver()
    if _wake is not None:
        _wake.set()


def unregister_pass(name: str) -> None:
    """Remove a pass and cancel it if it is mid-flight. Never raises.

    Cancelling stops a pass that is mid-flight at shutdown — e.g. a git op
    that is running when the server tears down is stopped rather than left to
    race the pool shutdown.
    """
    sweep = _passes.pop(name, None)
    if sweep is not None and sweep.in_flight():
        sweep.task.cancel()


def kick(name: Optional[str] = None) -> None:
    """Make ``name`` (or every pass) due immediately and wake the driver.

    The in-process latency escape hatch for a seam that has just written a row
    one of these passes is responsible for — it turns "up to one cadence" into
    "next event-loop tick" without anybody having to shorten a poll. Safe to
    call from any coroutine on the driver's loop; a no-op when the driver is
    not running.
    """
    now = time.monotonic()
    for sweep in _passes.values():
        if name is None or sweep.name == name:
            sweep.next_due = now
    if _wake is not None:
        _wake.set()


def invalidate_peer_cache() -> None:
    """Force the next tick to re-read the install registry."""
    global _peer_answer, _peer_checked_at
    _peer_answer = None
    _peer_checked_at = 0.0


async def _peers_present() -> bool:
    """Cached "does this database carry any install other than me?".

    Fails SAFE: any error answers True (assume a cluster, keep the tight
    cadences) and is cached like any other answer so a broken registry cannot
    turn into a read per tick.

    Deliberately counts EVERY registered peer, not just the currently-online
    ones: an offline peer can come back and enqueue work at any time, and
    flapping the cadence with peer liveness would make the deployment's cost
    profile depend on someone else's uptime.
    """
    global _peer_answer, _peer_checked_at
    try:
        ttl = float(getattr(settings, "single_install_peer_check_seconds", 300))
    except (TypeError, ValueError):
        ttl = 300.0
    now = time.monotonic()
    if _peer_answer is not None and (now - _peer_checked_at) < ttl:
        return _peer_answer
    try:
        from app.db import installs as installs_db

        rows = await asyncio.to_thread(installs_db.get_installs)
        me = settings.effective_instance_id
        answer = any((row.get("id") or "") != me for row in rows)
    except Exception:  # noqa: BLE001 — never let a registry hiccup stop the sweep
        logger.debug("turn_claim: peer-install check failed", exc_info=True)
        answer = True
    _peer_answer = answer
    _peer_checked_at = now
    return answer


def _stretch_interval() -> float:
    """Single-install cadence, or 0 when the stretch is disabled."""
    try:
        return max(0.0, float(
            getattr(settings, "single_install_sweep_interval_seconds", 60)
        ))
    except (TypeError, ValueError):
        return 60.0


def _effective_interval(sweep: SweepPass, peers: bool) -> float:
    """The cadence to schedule ``sweep`` at right now.

    Never shorter than the pass's own tight interval — the stretch can only
    lengthen a cadence, so a pass that already polls slowly (e.g. the daily
    git-ops retention purge) is untouched by it.
    """
    if peers:
        return sweep.tight_interval
    return max(sweep.tight_interval, _stretch_interval())


async def _execute_pass(sweep: SweepPass, peers: bool) -> None:
    """Run one pass to completion, then reschedule it from the finish time.

    Scheduling from COMPLETION (not from the start) reproduces the
    ``sleep(interval); await work()`` shape each of these loops had when it
    owned its own task, so a slow pass cannot pile up on itself.
    """
    result = None
    try:
        result = await sweep.run()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — one bad pass must not starve the rest
        logger.warning("turn_claim: sweep pass %r failed", sweep.name,
                       exc_info=True)
    finally:
        sweep.next_due = time.monotonic() + _effective_interval(sweep, peers)
    # Real work on a supposedly single-install deployment is the strongest
    # possible hint that the peer answer is stale — re-derive it next tick.
    if result and not peers:
        invalidate_peer_cache()
        if _wake is not None:
            _wake.set()


def _dispatch_passes(due: List[SweepPass], peers: bool) -> None:
    """Launch every due pass that is not already running, each as its own task.

    A pass still in flight is simply skipped and re-anchored — the same
    "one execution at a time" property its private loop had — while the other
    passes, and the driver's own timing, are untouched by how long it takes.
    """
    now = time.monotonic()
    for sweep in due:
        if sweep.in_flight():
            sweep.next_due = max(
                sweep.next_due, now + _effective_interval(sweep, peers)
            )
            continue
        # Provisional deadline so the driver does not re-dispatch on the next
        # tick; _execute_pass re-anchors it from the real completion time.
        sweep.next_due = now + _effective_interval(sweep, peers)
        task = asyncio.create_task(_execute_pass(sweep, peers))
        sweep.task = task
        _pass_tasks.add(task)
        task.add_done_callback(_pass_tasks.discard)


async def sweep_loop() -> None:
    """The shared driver: run every registered pass at its effective cadence.

    Sleeps until the earliest due pass (and parks on the wake event with no
    timeout at all when nothing is registered), so an idle install pays one
    wake-up per cadence instead of one per loop.
    """
    global _loop, _wake, _notify_pending
    _loop = asyncio.get_running_loop()
    _wake = asyncio.Event()

    logger.info(
        "turn_claim: unified sweep driver started (passes=%s, runner=%s)",
        sorted(_passes), settings.effective_instance_id,
    )
    last_peers: Optional[bool] = None
    while True:
        try:
            peers = await _peers_present()
            if last_peers is not None and peers != last_peers:
                # The cadence just changed under every pass; re-anchor their
                # deadlines so a tightening takes effect on THIS tick instead of
                # waiting out a schedule computed under the old regime.
                now = time.monotonic()
                for sweep in _passes.values():
                    sweep.next_due = min(
                        sweep.next_due, now + _effective_interval(sweep, peers)
                    )
                logger.info(
                    "turn_claim: cadence switched to %s-install mode",
                    "multi" if peers else "single",
                )
            last_peers = peers

            now = time.monotonic()
            if _passes:
                delay = max(0.0, min(p.next_due for p in _passes.values()) - now)
            else:
                delay = None

            if delay is None:
                await _wake.wait()
            elif delay > 0:
                try:
                    await asyncio.wait_for(_wake.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
            _wake.clear()

            notify = _notify_pending
            _notify_pending = False

            now = time.monotonic()
            due = [p for p in _passes.values() if p.next_due <= now]
            if notify:
                for sweep in _passes.values():
                    if sweep.wake_on_notify and not any(d is sweep for d in due):
                        due.append(sweep)
            if not due:
                # A bare wake (registration, kick of an already-scheduled pass,
                # cadence change) — just recompute the sleep.
                continue

            _dispatch_passes(due, peers)
            # Damping floor: NOTIFY ticks fire per event batch during
            # streaming; never run passes more than once per cooldown.
            await asyncio.sleep(WAKE_COOLDOWN_SECONDS)
        except asyncio.CancelledError:
            break
        except Exception:  # noqa: BLE001 — the loop must survive anything.
            logger.warning("turn_claim: sweep tick failed", exc_info=True)
            try:
                await asyncio.sleep(WAKE_COOLDOWN_SECONDS)
            except asyncio.CancelledError:
                break
    logger.info("turn_claim: unified sweep driver stopped")


# Back-compat alias — this loop's only caller is :func:`start`, but the name is
# referenced in docs/commit history.
claim_loop = sweep_loop


def _ensure_driver() -> None:
    """Spawn the sweep driver task if it is not already running."""
    global _loop_task
    if _loop_task is not None and not _loop_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("turn_claim: no running event loop — sweep driver not started")
        return
    _loop_task = loop.create_task(sweep_loop())


def start() -> None:
    """Register the turn-claim pass and start the shared driver (idempotent).

    Called from ``startup.install_v2_turns_wiring`` (which runs inside the
    lifespan startup, so a loop is running). The task ref is module-held so it
    is never GC'd; ``shutdown_services`` cancels it via :func:`stop` (rF4).
    """
    try:
        from app.core.event_gateway import get_gateway

        get_gateway().add_global_listener(_on_gateway_tick)
    except Exception:  # noqa: BLE001 — no gateway ⇒ poll-only cadence, still correct.
        logger.warning(
            "turn_claim: gateway listener unavailable — poll-only cadence",
            exc_info=True,
        )

    register_pass(
        "turn_claim",
        claim_once,
        CLAIM_POLL_SECONDS,
        wake_on_notify=True,
    )
    logger.info(
        "turn_claim: registered (poll every %ss + NOTIFY wake, runner=%s)",
        int(CLAIM_POLL_SECONDS), settings.effective_instance_id,
    )


async def stop() -> None:
    """Cancel the sweep driver and wait for it to exit (idempotent; rF4).

    Called from ``startup.shutdown_services`` so teardown cancels the loop
    cleanly instead of relying on process death — a pass racing the session-
    pool shutdown could otherwise kick the scheduler into starting new work
    mid-teardown. Stopping the driver stops EVERY registered pass, which is the
    intended shutdown behaviour; the other owners' ``stop()`` functions then
    unregister their passes so a restart in the same process is clean.

    No-op when the driver was never started (flag OFF, or a test process that
    never called :func:`start`).
    """
    global _loop_task, _wake, _loop
    unregister_pass("turn_claim")
    task = _loop_task
    _loop_task = None
    # Cancel any pass still executing: a pass racing the session-pool shutdown
    # is exactly what this teardown exists to prevent.
    in_flight = [t for t in list(_pass_tasks) if not t.done()]
    for t in in_flight:
        t.cancel()
    for t in in_flight:
        try:
            await t
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    if task is None or task.done():
        _wake = None
        _loop = None
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001 — teardown must never raise.
        logger.debug("turn_claim: loop raised during shutdown", exc_info=True)
    finally:
        _wake = None
        _loop = None
