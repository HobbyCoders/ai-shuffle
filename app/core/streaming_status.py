"""Durable session-busy truth from the ``runs`` table (v2 core, Phase 1).

Re-sources every "is this session busy/streaming?" read to the leased ``runs``
table (`app.db.runs`): a session is busy iff it has an ``active`` run whose
lease has not lapsed (``lease_expires_at >= NOW()`` — the same predicate
``renew_run``/``expire_due_runs`` enforce; an expired-lease active row counts
as NOT busy). Because the runs table lives in the shared database, this is one
truth across installs — no in-memory map to wipe on restart or fan out to
peers.

Three consumers:

* Guard sites call :func:`has_active_run` (per-session, small TTL cache so hot
  guard paths don't hammer the DB) or :func:`get_user_streaming_sessions`
  (connect-time snapshot join). Both fail OPEN (not busy / empty) — a status
  read must never take down its caller.
* Run-lifecycle writers (the TurnRecorder's claim/release, the event gateway's
  NOTIFY ticks) call :func:`notify` — cheap, synchronous, thread-safe. It
  schedules an UNCACHED async re-check, diffs against the last-broadcast busy
  state, and on change emits the existing ``user_streaming_changed`` WS frame
  (exact legacy wire shape — the frontend needs zero changes).
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

# The lease-validity predicate, verbatim from app/db/runs.py (renew_run /
# expire_due_runs): an active run whose lease lapsed is NOT busy.
_LIVE_RUN_WHERE = "state='active' AND lease_expires_at >= NOW()"

# session_id -> (checked_at_monotonic, busy). Guarded by _cache_lock because
# has_active_run is called from sync (thread-pool) and async contexts alike.
_cache: Dict[str, Tuple[float, bool]] = {}
_cache_lock = threading.Lock()

# Sessions last broadcast as busy (spinner ON): session_id -> the broadcast
# routing info captured when the spinner went on (user/project/worktree).
# Only mutated on the event loop (single-threaded between awaits).
_last_broadcast: Dict[str, Dict[str, Optional[str]]] = {}

# Event loop captured at startup so notify() can be called from any thread
# (the event gateway's LISTEN loop runs in a daemon thread).
_loop: Optional[asyncio.AbstractEventLoop] = None


# ----------------------------------------------------------------------
# DB reads
# ----------------------------------------------------------------------

def _has_live_turn_db(session_id: str) -> bool:
    """True iff a v2 live turn (running|interrupting|draining) exists for the
    session. Fully guarded: returns False on ANY error (missing table, etc.) —
    so this union can NEVER lose the authoritative runs-busy signal it augments.

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
            "[streaming_status] live-turn read failed for %s", session_id,
            exc_info=True,
        )
        return False


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
    """Lease-valid active runs joined to their owner's sessions (one query).

    Connect-time snapshot shape:
    ``[{"session_id", "project_id", "worktree_id"}, ...]``.

    Ownership follows the app's user model (see app/api/websocket.py):
    admin sessions have ``api_user_id`` NULL and broadcast under the constant
    ``"admin"`` user id; API-user sessions carry their ``api_user_id``.
    Fails open (empty list) on any DB error.
    """
    # Same lease-validity predicate as _LIVE_RUN_WHERE, columns qualified for
    # the join.
    base_sql = (
        "SELECT r.session_id, s.project_id, s.worktree_id "
        "FROM runs r JOIN sessions s ON s.id = r.session_id "
        "WHERE r.state='active' AND r.lease_expires_at >= NOW()"
    )
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            if user_id == "admin":
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
    }


# ----------------------------------------------------------------------
# Notify (run lifecycle changes) -> diff -> user_streaming_changed
# ----------------------------------------------------------------------

def notify(session_id: str) -> None:
    """Run-lifecycle change hook. Cheap, synchronous, thread-safe, never raises.

    Drops the guard cache entry (so guards see the transition promptly) and
    schedules an uncached async re-check + diff + broadcast. Callable from the
    event loop (TurnRecorder) or a foreign thread (event gateway listener).
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
            loop.call_soon_threadsafe(_schedule_recheck, session_id)
            return
        loop.create_task(_recheck(session_id))
    except Exception:
        logger.debug(
            "[streaming_status] notify scheduling failed for %s",
            session_id, exc_info=True,
        )


def _schedule_recheck(session_id: str) -> None:
    """Loop-thread trampoline for cross-thread notify()."""
    try:
        asyncio.get_running_loop().create_task(_recheck(session_id))
    except Exception:
        logger.debug(
            "[streaming_status] recheck scheduling failed for %s",
            session_id, exc_info=True,
        )


async def _recheck(session_id: str) -> None:
    """Uncached busy re-check; broadcast the diff if the state flipped."""
    try:
        busy = await asyncio.to_thread(_has_active_run_db, session_id)
    except Exception:
        logger.warning(
            "[streaming_status] recheck read failed for %s",
            session_id, exc_info=True,
        )
        return
    await _apply_busy_state(session_id, busy)


async def _apply_busy_state(session_id: str, busy: bool) -> None:
    """Diff ``busy`` against the last-broadcast state; broadcast on change.

    Dict mutations happen between awaits on the event loop, so concurrent
    rechecks resolve to at most one broadcast per transition.
    """
    if busy:
        if session_id in _last_broadcast:
            return  # unchanged — no broadcast
        info = await asyncio.to_thread(_broadcast_info, session_id)
        if session_id in _last_broadcast:
            return  # lost a benign race to a concurrent recheck
        _last_broadcast[session_id] = info
    else:
        info = _last_broadcast.pop(session_id, None)
        if info is None:
            return  # unchanged — no broadcast
    await _send_streaming_changed(session_id, busy, info)


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
            busy = await asyncio.to_thread(_has_active_run_db, session_id)
        except Exception:
            logger.warning(
                "[streaming_status] sweep read failed for %s",
                session_id, exc_info=True,
            )
            continue
        if not busy:
            with _cache_lock:
                _cache.pop(session_id, None)
            await _apply_busy_state(session_id, False)
            cleared += 1
    return cleared


async def _reap_and_kick_turns() -> int:
    """Reap dead-lease live turns and kick each affected session. Fully guarded
    — a reap/kick failure must never break the spinner sweep.

    Returns the number of turns reaped (0 on error)."""
    try:
        from app.core import turn_scheduler
        from app.db import turns as turns_db

        reaped = await asyncio.to_thread(turns_db.reap_expired)
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

    Registers :func:`notify` as an all-sessions listener on the event gateway
    (NOTIFY ticks from ANY install's appends, since the gateway listens on the
    shared database). Also captures the running loop for cross-thread notify.
    """
    global _loop
    try:
        _loop = asyncio.get_running_loop()
    except RuntimeError:
        pass
    try:
        from app.core.event_gateway import get_gateway

        get_gateway().add_global_listener(notify)
    except Exception:
        logger.warning(
            "[streaming_status] gateway listener install failed", exc_info=True
        )


def _reset_state() -> None:
    """Test helper — drop the cache, last-broadcast map and captured loop."""
    global _loop
    with _cache_lock:
        _cache.clear()
    _last_broadcast.clear()
    _loop = None
