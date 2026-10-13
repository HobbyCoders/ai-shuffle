"""Durable session-busy truth from the ``runs`` table (v2 core, Phase 1).

Re-sources every "is this session busy/streaming?" read to the leased ``runs``
table (`app.db.runs`): a session is busy iff it has an ``active`` run whose
lease has not lapsed (``lease_expires_at >= NOW()`` — the same predicate
``renew_run`` enforces; an expired-lease active row counts
as NOT busy). Because the runs table lives in the shared database, this is one
truth across installs — no in-memory map to wipe on restart or fan out to
peers.

Three consumers:

* Guard sites call :func:`has_active_run` (per-session, small TTL cache so hot
  guard paths don't hammer the DB) or :func:`get_user_streaming_sessions`
  (connect-time snapshot join). Both fail OPEN (not busy / empty) — a status
  read must never take down its caller.
* Run-lifecycle writers (the TurnRecorder's claim/release) call :func:`notify`
  — cheap, synchronous, thread-safe. It schedules an UNCACHED async re-check,
  diffs against the last-broadcast busy state, and on change emits the existing
  ``user_streaming_changed`` WS frame (exact legacy wire shape — the frontend
  needs zero changes). The event gateway's NOTIFY ticks go through
  :func:`notify_gateway_tick` instead, which drops ticks for sessions already
  broadcast as busy and throttles the rest per session — an append batch is not
  a run-lifecycle change, and there are ~7 of them a second per streaming
  session.
* :func:`sweep_loop` (started from ``startup.start_background_tasks``)
  re-checks every session we last broadcast as busy so a lease that expires
  with no terminal event (crashed runner) still turns its board spinner off.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from app.db.base import get_db

logger = logging.getLogger(__name__)

# Guard-path cache: busy reads within this window are served from memory.
CACHE_TTL_SECONDS = 1.5
# Reconciler cadence: catches lease expiry with no terminal event.
SWEEP_INTERVAL_SECONDS = 15.0
# Damping floor for gateway NOTIFY ticks (W5). Append batches arrive ~7/s per
# streaming session; without this every one of them cost an uncached runs+turns
# read that almost always diffed to "no change" — and popped the guard cache
# other busy-guards read through. Mirrors turn_claim's WAKE_COOLDOWN_SECONDS.
# Leading edge fires immediately (no added latency for a real transition), then
# at most one trailing re-check per window, so a burst that ENDS inside the
# window is still evaluated.
GATEWAY_TICK_COOLDOWN_SECONDS = 1.0
# Size at which the per-session throttle bookkeeping sheds its stale entries.
_TICK_STATE_MAX = 512

# The lease-validity predicate, verbatim from app/db/runs.py (renew_run): an
# active run whose lease lapsed is NOT busy.
_LIVE_RUN_WHERE = "state='active' AND lease_expires_at >= NOW()"

# session_id -> (checked_at_monotonic, busy). Guarded by _cache_lock because
# has_active_run is called from sync (thread-pool) and async contexts alike.
_cache: Dict[str, Tuple[float, bool]] = {}
_cache_lock = threading.Lock()

# Sessions last broadcast as busy (spinner ON): session_id -> the broadcast
# routing info captured when the spinner went on (user/project/worktree).
# Only mutated on the event loop (single-threaded between awaits).
_last_broadcast: Dict[str, Dict[str, Optional[str]]] = {}

# Per-session recheck chains: session_id -> the most recently spawned recheck
# task. Each recheck awaits its predecessor so two transitions (turn N's
# release, turn N+1's claim) can never interleave their read→send windows and
# land on the wire out of order — an OFF frame sent AFTER a newer ON leaves
# the board dark with no recovery: the sweep only revisits sessions it still
# believes are busy, and plain gateway-tick rechecks are diff-suppressed.
_chains: Dict[str, "asyncio.Task"] = {}

# Gateway-tick throttle state, both touched only on the event loop:
#   _tick_last_fired: session_id -> loop time of the last tick-driven recheck.
#   _tick_pending:    session_id -> the armed trailing-edge timer, if any.
_tick_last_fired: Dict[str, float] = {}
_tick_pending: Dict[str, "asyncio.TimerHandle"] = {}

# Event loop captured at startup so notify() can be called from any thread
# (the event gateway's LISTEN loop runs in a daemon thread).
_loop: Optional[asyncio.AbstractEventLoop] = None


# ----------------------------------------------------------------------
# DB reads
# ----------------------------------------------------------------------

def _has_live_turn_db(session_id: str) -> bool:
    """True iff a v2 live turn (running|interrupting|draining) exists for the
    session. Fail closed on read errors: an unverified draining turn must not
    authorize Git mutations after its run lease has been released.

    A ``draining`` turn is the reason this matters: an escalated interrupt keeps
    the healthy client while its reader drains, and the run lease may already be
    released — the runs read alone would read idle, but the session is still
    busy until the drain settles.
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM turns WHERE session_id=%s "
                "AND state IN ('running','interrupting','draining') LIMIT 1",
                (session_id,),
            )
            return cursor.fetchone() is not None
    except Exception:
        logger.debug(
            "[streaming_status] live-turn read failed for %s; treating as busy", session_id,
            exc_info=True,
        )
        return True


def _has_active_run_db(session_id: str) -> bool:
    """Uncached busy read (notifier/sweep + guard path). May raise on the runs
    read; the v2 live-turn union is guarded and never masks the runs signal."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT 1 FROM runs WHERE session_id=%s AND {_LIVE_RUN_WHERE} "
            "LIMIT 1",
            (session_id,),
        )
        if cursor.fetchone() is not None:
            return True
    return _has_live_turn_db(session_id)


def has_active_run(session_id: Optional[str]) -> bool:
    """True iff the session has a lease-valid active run. TTL-cached.

    Fails open (False) on any DB error — busy guards degrade to "not busy",
    never to a crashed caller.
    """
    if not session_id:
        return False
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(session_id)
        if hit is not None and now - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]
    try:
        busy = _has_active_run_db(session_id)
    except Exception:
        logger.warning(
            "[streaming_status] has_active_run read failed for %s — "
            "treating as not busy", session_id, exc_info=True,
        )
        return False
    with _cache_lock:
        _cache[session_id] = (now, busy)
    return busy


def has_any_active_run() -> bool:
    """True iff ANY session has a lease-valid active run (uncached).

    Global-granularity busy read for coarse gates (e.g. the periodic VACUUM
    skip). Fails open (False) on DB error.
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"SELECT 1 FROM runs WHERE {_LIVE_RUN_WHERE} LIMIT 1"
            )
            return cursor.fetchone() is not None
    except Exception:
        logger.warning(
            "[streaming_status] has_any_active_run read failed — "
            "treating as not busy", exc_info=True,
        )
        return False


def get_user_streaming_sessions(user_id: str) -> List[Dict[str, Any]]:
    """Busy sessions owned by this user, matching the per-session busy read.

    Connect-time snapshot shape:
    ``[{"session_id", "project_id", "worktree_id", "worktree_branch"}, ...]``
    (``worktree_branch`` = the legacy column, else the v2 ``sessions.branch``).

    Ownership follows the app's user model (see app/api/websocket.py):
    admin sessions have ``api_user_id`` NULL and broadcast under the constant
    ``"admin"`` user id; API-user sessions carry their ``api_user_id``. A
    device paired to the owner (``"paired:<id>"``) sees the owner's sessions.
    Fails open (empty list) on any DB error.
    """
    # Reconnect must use the same runs OR live-turn truth as
    # _has_active_run_db. In particular, draining turns remain busy after their
    # run lease is released. EXISTS also returns each session only once.
    base_sql = (
        "SELECT s.id AS session_id, s.project_id, s.worktree_id, "
        "s.worktree_branch, s.branch FROM sessions s "
        "WHERE (EXISTS (SELECT 1 FROM runs r WHERE r.session_id=s.id "
        "AND r.state='active' AND r.lease_expires_at >= NOW()) "
        "OR EXISTS (SELECT 1 FROM turns t WHERE t.session_id=s.id "
        "AND t.state IN ('running','interrupting','draining')))"
    )
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            # Same owner test as sync_engine._is_owner_user_id, inlined because
            # this module must not import the legacy engine at load time.
            if user_id == "admin" or user_id.startswith("paired:"):
                cursor.execute(base_sql + " AND s.api_user_id IS NULL")
            else:
                cursor.execute(
                    base_sql + " AND s.api_user_id = %s", (user_id,)
                )
            rows = cursor.fetchall() or []
    except Exception:
        logger.warning(
            "[streaming_status] get_user_streaming_sessions failed for %s",
            user_id, exc_info=True,
        )
        return []
    return [
        {
            "session_id": row["session_id"],
            "project_id": row["project_id"],
            "worktree_id": row["worktree_id"],
            # Logical branch: legacy column first, then the v2 ``sessions.branch``
            # a branch-intake session records instead. Consumers use this to tell
            # a branch/worktree session (own checkout — does NOT lock main-
            # checkout git controls) apart from a true main-checkout session.
            "worktree_branch": row["worktree_branch"] or row["branch"],
        }
        for row in rows
    ]


def _broadcast_info(session_id: str) -> Dict[str, Optional[str]]:
    """Routing fields for the ``user_streaming_changed`` frame (best-effort)."""
    row: Dict[str, Any] = {}
    try:
        from app.db import sessions as sessions_db

        row = sessions_db.get_session(session_id) or {}
    except Exception:
        logger.warning(
            "[streaming_status] session row read failed for %s — "
            "broadcasting with defaults", session_id, exc_info=True,
        )
    return {
        "user_id": row.get("api_user_id") or "admin",
        "project_id": row.get("project_id"),
        "worktree_id": row.get("worktree_id"),
        # Same fallback as get_user_streaming_sessions: v2 branch sessions have
        # no legacy worktree row — their logical branch lives in sessions.branch.
        "worktree_branch": row.get("worktree_branch") or row.get("branch"),
    }


# ----------------------------------------------------------------------
# Notify (run lifecycle changes) -> diff -> user_streaming_changed
# ----------------------------------------------------------------------

def notify(session_id: str, force: bool = False) -> None:
    """Run-lifecycle change hook. Cheap, synchronous, thread-safe, never raises.

    Drops the guard cache entry (so guards see the transition promptly) and
    schedules an uncached async re-check + diff + broadcast. Callable from the
    event loop (TurnRecorder) or a foreign thread (event gateway listener).

    ``force=True`` bypasses the last-broadcast diff for the spinner-ON side:
    a run CLAIM must always produce an ON frame even when ``_last_broadcast``
    still holds a stale busy entry from a previous turn whose OFF was only
    just swept (or not yet swept) — otherwise back-to-back turns leave the
    board spinner dark while the session works.
    """
    if not session_id:
        return
    try:
        with _cache_lock:
            _cache.pop(session_id, None)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = _loop
            if loop is None or loop.is_closed():
                return
            loop.call_soon_threadsafe(_schedule_recheck, session_id, force)
            return
        _spawn_recheck(session_id, force)
    except Exception:
        logger.debug(
            "[streaming_status] notify scheduling failed for %s",
            session_id, exc_info=True,
        )


def notify_gateway_tick(session_id: str) -> None:
    """Damped entry point for event-gateway NOTIFY ticks (W5).

    The gateway fires this for EVERY append batch on EVERY session — roughly
    7/s per streaming session. Two dampers, both safe:

    1. **Already-busy sessions are skipped outright.** A tick carries no event
       types, so an ordinary append can only ever confirm "still busy". The
       busy->idle edge does not depend on this path at all: the run-lifecycle
       writer calls :func:`notify` directly on release/abort, and a lease that
       lapses with no terminal event is caught by :func:`sweep_once` within
       ``SWEEP_INTERVAL_SECONDS``. Skipping also leaves the guard TTL cache
       intact, which is correct — the session IS busy — instead of forcing every
       other busy-guard's next read back to the database.
    2. **Idle sessions are throttled per session** at
       ``GATEWAY_TICK_COOLDOWN_SECONDS``: the first tick fires immediately (an
       idle->busy edge keeps its latency) and further ticks inside the window
       collapse into a single trailing re-check, so a burst that ends mid-window
       is still evaluated exactly once.

    Cheap, synchronous, thread-safe, never raises — the listener thread calls it.
    """
    if not session_id:
        return
    try:
        # Fast path, no loop hop: an already-busy session needs no re-check.
        if session_id in _last_broadcast:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = _loop
            if loop is None or loop.is_closed():
                return
            loop.call_soon_threadsafe(_throttled_tick, session_id)
            return
        _throttled_tick(session_id)
    except Exception:
        logger.debug(
            "[streaming_status] gateway tick scheduling failed for %s",
            session_id, exc_info=True,
        )


def _throttled_tick(session_id: str) -> None:
    """Leading-edge-with-trailing throttle. Runs on the event loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if session_id in _last_broadcast:
        return  # became busy between the listener thread and this hop
    now = loop.time()
    last = _tick_last_fired.get(session_id)
    if last is None or now - last >= GATEWAY_TICK_COOLDOWN_SECONDS:
        _tick_last_fired[session_id] = now
        _prune_tick_state(now)
        notify(session_id)
        return
    if session_id in _tick_pending:
        return  # a trailing re-check is already armed for this window
    delay = GATEWAY_TICK_COOLDOWN_SECONDS - (now - last)
    _tick_pending[session_id] = loop.call_later(delay, _fire_trailing_tick, session_id)


def _fire_trailing_tick(session_id: str) -> None:
    """Trailing edge of the throttle window."""
    _tick_pending.pop(session_id, None)
    if session_id in _last_broadcast:
        return  # a claim already turned the spinner on — nothing to re-check
    try:
        _tick_last_fired[session_id] = asyncio.get_running_loop().time()
    except RuntimeError:
        return
    notify(session_id)


def _prune_tick_state(now: float) -> None:
    """Bound the throttle bookkeeping; sessions are unbounded over a process."""
    if len(_tick_last_fired) <= _TICK_STATE_MAX:
        return
    stale = [
        sid for sid, when in _tick_last_fired.items()
        if now - when >= GATEWAY_TICK_COOLDOWN_SECONDS * 10
    ]
    for sid in stale:
        _tick_last_fired.pop(sid, None)


def _schedule_recheck(session_id: str, force: bool = False) -> None:
    """Loop-thread trampoline for cross-thread notify()."""
    try:
        _spawn_recheck(session_id, force)
    except Exception:
        logger.debug(
            "[streaming_status] recheck scheduling failed for %s",
            session_id, exc_info=True,
        )


def _spawn_recheck(session_id: str, force: bool) -> "asyncio.Task":
    """Spawn a recheck chained behind any pending one for this session."""
    prev = _chains.get(session_id)
    task = asyncio.create_task(_recheck(session_id, force, prev))
    # Fire-and-forget broadcast: if the loop dies before it runs (shutdown,
    # test teardown) the sweep reconciles later, so the destroy-while-pending
    # log would only be noise.
    task._log_destroy_pending = False  # type: ignore[attr-defined]
    _chains[session_id] = task
    return task


async def _recheck(
    session_id: str,
    force: bool = False,
    prev: Optional["asyncio.Task"] = None,
) -> bool:
    """Uncached busy re-check; broadcast the diff if the state flipped.

    Awaits ``prev`` (this session's prior recheck) BEFORE reading, so
    evaluations run strictly in notify order and their frames hit the wire in
    that order too. Returns True iff a frame was broadcast.
    """
    try:
        if prev is not None:
            try:
                await prev
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling() > 0:
                    raise
                return False
            except Exception:
                pass  # predecessor failed — evaluate against current state
        try:
            busy = await asyncio.to_thread(_has_active_run_db, session_id)
        except Exception:
            logger.warning(
                "[streaming_status] recheck read failed for %s",
                session_id, exc_info=True,
            )
            return False
        return await _apply_busy_state(session_id, busy, force=force)
    finally:
        # A recheck GC'd after its loop died resumes here with no running
        # loop; current_task() would raise and spam "Exception ignored".
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        if current is not None and _chains.get(session_id) is current:
            _chains.pop(session_id, None)


async def _apply_busy_state(session_id: str, busy: bool, force: bool = False) -> bool:
    """Diff ``busy`` against the last-broadcast state; broadcast on change.

    Dict mutations happen between awaits on the event loop; per-session
    serialization (``_chains``) keeps evaluations ordered. ``force=True``
    (a run claim) re-sends the ON frame even when the last-broadcast map says
    already-busy — the map can hold a stale entry from a PREVIOUS turn whose
    spinner-off the frontend already consumed via the done frame.
    Returns True iff a frame was broadcast.
    """
    if busy:
        existing = _last_broadcast.get(session_id)
        if existing is not None:
            if not force:
                return False  # unchanged — no broadcast
            info = existing
        else:
            info = await asyncio.to_thread(_broadcast_info, session_id)
            if session_id in _last_broadcast and not force:
                return False  # lost a benign race to a concurrent recheck
        _last_broadcast[session_id] = info
    else:
        info = _last_broadcast.pop(session_id, None)
        if info is None:
            return False  # unchanged — no broadcast
        # The OFF transition also invalidates any cached guard read.
        with _cache_lock:
            _cache.pop(session_id, None)
    await _send_streaming_changed(session_id, busy, info)
    return True


async def _send_streaming_changed(
    session_id: str, is_streaming: bool, info: Dict[str, Optional[str]]
) -> None:
    """Emit the EXISTING ``user_streaming_changed`` frame (exact legacy shape).

    Lazy sync_engine import: this module must stay importable from
    turn_recorder without pulling the legacy engine into its import graph.
    """
    try:
        from app.core.sync_engine import sync_engine

        await sync_engine._broadcast_user_message(  # noqa: SLF001 — existing send helper
            user_id=info.get("user_id") or "admin",
            message={
                "type": "user_streaming_changed",
                "session_id": session_id,
                "is_streaming": is_streaming,
                "project_id": info.get("project_id"),
                "worktree_id": info.get("worktree_id"),
                "worktree_branch": info.get("worktree_branch"),
            },
        )
    except Exception:
        logger.warning(
            "[streaming_status] user_streaming_changed broadcast failed "
            "for %s", session_id, exc_info=True,
        )


# ----------------------------------------------------------------------
# Sweep reconciler + startup wiring
# ----------------------------------------------------------------------

async def sweep_once() -> int:
    """Re-check every session last broadcast busy; broadcast spinner-off diffs.

    Catches lease expiry with no terminal event (crashed runner never released
    its run and stopped heartbeating). Returns the number of spinners cleared.

    V2 turns (flag-gated): also reaps dead-lease live turns (``reap_expired``
    requeues user/queue work, aborts the rest) and kicks each affected session
    so a requeued turn restarts — the 15s crash/kill recovery backstop.
    """
    await _reap_and_kick_turns()

    cleared = 0
    for session_id in list(_last_broadcast):
        try:
            # Through the per-session chain so the sweep's evaluation cannot
            # interleave with (and land after) a newer claim's forced ON.
            sent_off = await _spawn_recheck(session_id, False)
        except Exception:
            logger.warning(
                "[streaming_status] sweep read failed for %s",
                session_id, exc_info=True,
            )
            continue
        if sent_off:
            cleared += 1
    return cleared


async def _reap_and_kick_turns() -> int:
    """Reap dead-lease live turns and kick each affected session. Fully guarded
    — a reap/kick failure must never break the spinner sweep.

    Returns the number of turns reaped (0 on error)."""
    try:
        from app.core import session_runtime, turn_scheduler
        from app.db import turns as turns_db

        # A turn still executing in this process is not abandoned even when
        # its lease is dead: it stops itself once its heartbeat notices, and
        # requeueing it would start it a second time (#1444).
        reaped = await asyncio.to_thread(
            turns_db.reap_expired, session_runtime.live_turn_ids(),
        )
        if not reaped:
            return 0
        for session_id in {r["session_id"] for r in reaped}:
            turn_scheduler.kick(session_id)
        logger.info("[streaming_status] sweep reaped %d dead-lease turn(s)", len(reaped))
        return len(reaped)
    except Exception:
        logger.warning("[streaming_status] turn reap sweep failed", exc_info=True)
        return 0


async def sweep_loop() -> None:
    """Periodic sweep task (started by startup.start_background_tasks)."""
    global _loop
    _loop = asyncio.get_running_loop()
    while True:
        try:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            swept = await sweep_once()
            if swept:
                logger.info(
                    "[streaming_status] sweep cleared %d stale spinner(s)",
                    swept,
                )
        except asyncio.CancelledError:
            break
        except Exception:
            logger.error("[streaming_status] sweep failed", exc_info=True)


def install_gateway_listener() -> None:
    """Secondary trigger: re-check busy on session-event flow.

    Registers :func:`notify_gateway_tick` (the DAMPED wrapper around
    :func:`notify` — see its docstring) as an all-sessions listener on the event
    gateway (NOTIFY ticks from ANY install's appends, since the gateway listens
    on the shared database). Also captures the running loop for cross-thread
    notify.
    """
    global _loop
    try:
        _loop = asyncio.get_running_loop()
    except RuntimeError:
        pass
    try:
        from app.core.event_gateway import get_gateway

        get_gateway().add_global_listener(notify_gateway_tick)
    except Exception:
        logger.warning(
            "[streaming_status] gateway listener install failed", exc_info=True
        )


def _reset_state() -> None:
    """Test helper — drop the cache, last-broadcast map, chains and loop."""
    global _loop
    with _cache_lock:
        _cache.clear()
    _last_broadcast.clear()
    for handle in _tick_pending.values():
        try:
            handle.cancel()
        except Exception:  # noqa: BLE001 — loop already gone
            pass
    _tick_pending.clear()
    _tick_last_fired.clear()
    for task in _chains.values():
        try:
            task.cancel()
        except RuntimeError:
            pass  # task's loop already closed — nothing left to deliver to
    _chains.clear()
    _loop = None
