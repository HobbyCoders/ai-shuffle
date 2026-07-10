"""Runtime lifecycle reconciler (Phase 1 of the lifecycle unification).

``app.core.lifecycle_state`` derives one coherent snapshot from the lifecycle
sources and reports disagreements as drift; THIS module repairs the subset of
drift that is safe to repair at runtime. It is modeled on
``app.core.startup_reconcile``'s guard structure: never raises, instance-
scoped, every repair recorded via ``record_error`` (visible in the error UI /
``GetAppErrors``).

Phase-1 discipline — REPAIRS ARE LATCHES/MARKERS ONLY. Nothing here touches a
message buffer, a pooled client, a pump, or the wire contract. The repair
classes:

  R1 parked-marker-while-idle  — durable ``sessions.parked_task_kind`` set but
                                 the derived state is idle → clear the marker
                                 (the runtime twin of boot reconcile's
                                 ``_clear_stale_parked_markers``).
  R2 stale-unpark-latch        — ``unpark_pending`` older than its grace with
                                 no drain pump and nothing streaming → clear
                                 the latch (frees the poller from a follow-up
                                 that will never stream).
  R3 stale-sync-streaming      — ``sync_engine`` says streaming but the live
                                 store says idle → ``clear_streaming_state``
                                 (the stale git-lock / remote-pill leak).
  R4 bg-work-dead-client       — registry background work "running" while the
                                 pooled SDK client that owns its completion is
                                 gone/dead → ``complete_task(killed)``, which
                                 routes through the guaranteed terminal-
                                 notification callback (the #514 hook), so the
                                 kill surfaces a durable chip instead of a
                                 forever-spinning task.
  R5 livestore-streaming-no-client — live store streaming with NO pooled
                                 client at all. WARN-ONLY (severity=high, no
                                 auto-repair): a turn can legitimately stream
                                 outside the pool briefly, and force-ending a
                                 real turn is exactly the ResourceMonitor
                                 incident Phase 1 must not repeat.

SAFETY GATES. Every repair is age-gated and confirm-twice: a condition must be
observed on at least two separate evaluations spanning the check's grace
window before it is repaired, so a legal transient (a fresh waking session, a
mid-hand-off latch) is never touched. The whole reconciler stands down while
the server is shutting down. Each check has an enable flag.

ENTRY POINTS. A periodic sweep (every ``RECONCILE_INTERVAL_SECONDS``,
registered in ``startup.start_background_tasks``) covers every session any
lifecycle source knows about, and ``request_reconcile`` gives /live's drift
sensor a debounced on-demand path for the session it just observed drifting.
"""

from __future__ import annotations

import asyncio
import logging
import time
import traceback
from typing import Any, Dict, Optional, Set, Tuple

from app.core.config import settings
from app.db import database
from app.db.async_database import run_sync
from app.db.base import get_db
from app.db.compat import ph
from app.db.error_logs import record_error

logger = logging.getLogger(__name__)

# Periodic sweep interval. The confirm-twice + grace gates are calibrated to
# this: a condition must persist across at least two sweeps.
RECONCILE_INTERVAL_SECONDS = 60.0

# General repair grace: >= 2 observation cycles of the periodic sweep.
GRACE_SECONDS = 120.0

# Explicit grace for the stale unpark latch (R2): the legal latch window is the
# commit -> start_streaming hand-off, which is sub-second; 60s of latch age is
# unambiguous strand territory.
UNPARK_LATCH_GRACE_SECONDS = 60.0

# On-demand (/live drift trigger) debounce per session.
ON_DEMAND_DEBOUNCE_SECONDS = 15.0

# Per-check enable flags. Operators can disable a repair class at runtime via
# a debugger/console without redeploying; tests use them for negative
# controls. R5 is detection-only by design (no repair to disable).
CHECKS_ENABLED: Dict[str, bool] = {
    "parked_marker": True,      # R1
    "unpark_latch": True,       # R2
    "sync_streaming": True,     # R3
    "dead_client_reap": True,   # R4
    "livestore_no_client": True,  # R5 (WARN-only)
}

# Confirm-twice bookkeeping: (check, session_id) -> (first_seen_monotonic,
# observation_count). An entry is dropped the moment its condition is no
# longer observed, so a condition that heals itself never accumulates credit.
_observations: Dict[Tuple[str, str], Tuple[float, int]] = {}

# Per-session last on-demand run (monotonic), for the /live drift debounce.
_last_on_demand: Dict[str, float] = {}

# In-flight on-demand tasks so request_reconcile never stacks duplicates.
_inflight: Dict[str, asyncio.Task] = {}


def reset() -> None:
    """Drop all reconciler bookkeeping (tests / shutdown)."""
    _observations.clear()
    _last_on_demand.clear()
    _inflight.clear()


def _observe(check: str, session_id: str, grace: float) -> bool:
    """Record one observation of a drift condition; True when repair-ripe.

    Repair-ripe = seen on >= 2 separate evaluations AND the first sighting is
    at least ``grace`` seconds old. Callers must call ``_clear_observation``
    whenever the condition is NOT present so healed transients reset.
    """
    now = time.monotonic()
    first_seen, count = _observations.get((check, session_id), (now, 0))
    count += 1
    _observations[(check, session_id)] = (first_seen, count)
    return count >= 2 and (now - first_seen) >= grace


def _clear_observation(check: str, session_id: str) -> None:
    _observations.pop((check, session_id), None)


def _log_repair(action: str, session_id: str, detail: str) -> None:
    """INFO trace + durable record for one repair. Never raises."""
    try:
        logger.info(
            "lifecycle-reconcile action=%s session=%s %s", action, session_id, detail
        )
    except Exception:
        pass


async def _check_parked_marker(session_id: str, snap) -> bool:
    """R1: durable parked marker set while the derived state is idle."""
    if not CHECKS_ENABLED.get("parked_marker"):
        return False
    if not (snap.parked_task_kind and snap.state == "idle"):
        _clear_observation("parked_marker", session_id)
        return False
    if not _observe("parked_marker", session_id, GRACE_SECONDS):
        return False

    # Instance scope: several installs share one Postgres; another install's
    # marker can be legitimately live. Only repair rows THIS install owns.
    row = await run_sync(database.get_session, session_id)
    if not row or row.get("instance_id") != settings.effective_instance_id:
        _clear_observation("parked_marker", session_id)
        return False

    await run_sync(database.set_session_parked_task_kind, session_id, None)
    _clear_observation("parked_marker", session_id)
    _log_repair(
        "clear-parked-marker",
        session_id,
        f"kind={snap.parked_task_kind}",
    )
    record_error(
        "backend",
        f"Lifecycle reconcile: session {session_id} carried a durable parked "
        f"marker (kind={snap.parked_task_kind}) while derived-idle for over "
        f"{int(GRACE_SECONDS)}s; cleared the stale marker.",
        level="WARNING",
        session_id=session_id,
        context={"reconcile": "lifecycle.parked_task_kind", "kind": snap.parked_task_kind},
    )
    return True


async def _check_unpark_latch(session_id: str, snap) -> bool:
    """R2: unpark-pending latch stranded (old, no pump, nothing streaming)."""
    from app.core.task_registry import task_registry

    if not CHECKS_ENABLED.get("unpark_latch"):
        return False
    stranded = (
        snap.unpark_latch and not snap.pump_running and not snap.live_store_streaming
    )
    if not stranded:
        _clear_observation("unpark_latch", session_id)
        return False
    age = task_registry.unpark_pending_age(session_id)
    ripe = _observe("unpark_latch", session_id, 0.0)
    # The latch's own monotonic age is the explicit grace (commit 2); the
    # confirm-twice observation still applies on top of it.
    if age is None or age < UNPARK_LATCH_GRACE_SECONDS or not ripe:
        return False

    task_registry.clear_unpark_pending(session_id)
    _clear_observation("unpark_latch", session_id)
    _log_repair("clear-unpark-latch", session_id, f"age={age:.1f}s")
    record_error(
        "backend",
        f"Lifecycle reconcile: session {session_id} had an unpark-pending "
        f"latch aged {age:.0f}s with no drain pump and nothing streaming — "
        "the committed follow-up turn never started; cleared the latch so the "
        "poller stops waiting on it.",
        level="WARNING",
        session_id=session_id,
        context={"reconcile": "lifecycle.unpark_pending", "age_seconds": round(age, 1)},
    )
    return True


async def _check_sync_streaming(session_id: str, snap) -> bool:
    """R3: sync_engine still says streaming while the live store says idle."""
    if not CHECKS_ENABLED.get("sync_streaming"):
        return False
    if not (snap.sync_streaming and not snap.live_store_streaming):
        _clear_observation("sync_streaming", session_id)
        return False
    if not _observe("sync_streaming", session_id, GRACE_SECONDS):
        return False

    from app.core.sync_engine import sync_engine

    await sync_engine.clear_streaming_state(session_id)
    _clear_observation("sync_streaming", session_id)
    _log_repair("clear-sync-streaming", session_id, "")
    record_error(
        "backend",
        f"Lifecycle reconcile: session {session_id} was marked streaming in "
        f"the sync engine while the live store was idle for over "
        f"{int(GRACE_SECONDS)}s; cleared the stale cross-device streaming "
        "state.",
        level="WARNING",
        session_id=session_id,
        context={"reconcile": "lifecycle.sync_streaming"},
    )
    return True


async def _check_dead_client_reap(session_id: str, snap) -> bool:
    """R4: registry background work running with no live pooled SDK client.

    Only PARKING work (``get_active_background_work``: background sub-agents +
    SDK-backgrounded shells) is eligible — Monitor shells are app-managed and
    independent of the pool. Streaming sessions are exempt: a mid-turn task's
    lifecycle belongs to the turn. The ``complete_task(killed)`` transition
    routes through the registry's guaranteed terminal-notification callback
    (the #514 hook), so the kill surfaces a durable chip.
    """
    if not CHECKS_ENABLED.get("dead_client_reap"):
        return False
    dead = (
        bool(snap.background_work)
        and not snap.live_store_streaming
        and (not snap.pool_present or not snap.pool_connected)
    )
    if not dead:
        _clear_observation("dead_client_reap", session_id)
        return False
    if not _observe("dead_client_reap", session_id, GRACE_SECONDS):
        return False

    from app.core.task_registry import task_registry

    reaped = []
    for task in snap.background_work:
        task_id = task.get("id")
        if not task_id:
            continue
        task_registry.complete_task(
            session_id,
            task_id,
            "killed",
            summary="Background task lost its SDK client — marked killed by the lifecycle reconciler",
            reason="dead_client",
        )
        reaped.append(task_id)
    _clear_observation("dead_client_reap", session_id)
    if not reaped:
        return False
    _log_repair("reap-dead-client-tasks", session_id, f"tasks={reaped}")
    record_error(
        "backend",
        f"Lifecycle reconcile: session {session_id} had background task(s) "
        f"{reaped} registered as running while the pooled SDK client that "
        f"owns their completion was gone/dead for over {int(GRACE_SECONDS)}s; "
        "marked them killed (terminal notification fired).",
        level="WARNING",
        session_id=session_id,
        context={"reconcile": "lifecycle.dead_client_reap", "task_ids": reaped},
    )
    return True


async def _check_livestore_no_client(session_id: str, snap) -> bool:
    """R5: live store streaming with no pooled client — WARN-only, no repair.

    Force-ending a possibly-real turn is the ResourceMonitor incident; Phase 1
    only makes the condition loudly visible.
    """
    if not CHECKS_ENABLED.get("livestore_no_client"):
        return False
    if not (snap.live_store_streaming and not snap.pool_present):
        _clear_observation("livestore_no_client", session_id)
        return False
    if not _observe("livestore_no_client", session_id, GRACE_SECONDS):
        return False

    _clear_observation("livestore_no_client", session_id)
    logger.warning(
        "lifecycle-drift severity=high class=livestore-streaming-no-client "
        "session=%s — live store reports streaming but no pooled SDK client "
        "exists; NOT auto-repaired (Phase 1 policy)",
        session_id,
    )
    record_error(
        "backend",
        f"Lifecycle drift (high): session {session_id} reports streaming in "
        "the live store but has no pooled SDK client. Not auto-repaired — "
        "investigate (possible orphaned turn).",
        level="WARNING",
        session_id=session_id,
        context={"reconcile": "lifecycle.livestore_no_client", "repaired": False},
    )
    return False  # detection only — never counts as a repair


async def reconcile_session(session_id: str, *, on_demand: bool = False) -> int:
    """Run every repair check against one session. Returns the repair count.

    Never raises; every check is guarded independently (startup_reconcile's
    structure). Skips entirely while the server is shutting down, and — for
    the on-demand path — inside the per-session debounce window.
    """
    from app.core import startup as _startup

    if _startup.is_shutting_down:
        return 0

    if on_demand:
        now = time.monotonic()
        last = _last_on_demand.get(session_id)
        if last is not None and (now - last) < ON_DEMAND_DEBOUNCE_SECONDS:
            return 0
        _last_on_demand[session_id] = now

    try:
        from app.core.lifecycle_state import compute

        snap = await compute(session_id)
    except Exception:
        logger.debug(
            "lifecycle-reconcile snapshot failed for %s", session_id, exc_info=True
        )
        return 0

    repairs = 0
    for check in (
        _check_parked_marker,
        _check_unpark_latch,
        _check_sync_streaming,
        _check_dead_client_reap,
        _check_livestore_no_client,
    ):
        try:
            if await check(session_id, snap):
                repairs += 1
        except Exception as exc:  # noqa: BLE001 — one check must not block the rest
            logger.exception(
                "lifecycle-reconcile check %s failed for %s",
                getattr(check, "__name__", check),
                session_id,
            )
            record_error(
                "backend",
                f"Lifecycle reconcile check {getattr(check, '__name__', check)} "
                f"failed for session {session_id}: {exc}",
                detail=traceback.format_exc(),
                session_id=session_id,
                context={"reconcile": "lifecycle.check_failure"},
            )
    return repairs


def request_reconcile(session_id: str) -> None:
    """Debounced fire-and-forget on-demand reconcile (the /live drift trigger).

    Sync and never raises — safe on the poll hot path. No running loop, a
    reconcile already in flight for the session, or a session inside the
    debounce window all degrade to a no-op; the periodic sweep is the backstop.
    """
    try:
        from app.core import startup as _startup

        if _startup.is_shutting_down:
            return
        existing = _inflight.get(session_id)
        if existing is not None and not existing.done():
            return
        loop = asyncio.get_running_loop()

        async def _run() -> None:
            try:
                await reconcile_session(session_id, on_demand=True)
            finally:
                _inflight.pop(session_id, None)

        _inflight[session_id] = loop.create_task(_run())
    except Exception:
        logger.debug("request_reconcile failed for %s", session_id, exc_info=True)


def _sessions_with_parked_markers() -> Set[str]:
    """This install's session ids with a durable parked marker set."""
    _ph = ph()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"""SELECT id FROM sessions
                 WHERE instance_id = {_ph} AND parked_task_kind IS NOT NULL""",
            (settings.effective_instance_id,),
        )
        rows = cursor.fetchall()
    return {row["id"] if isinstance(row, dict) else row[0] for row in rows}


async def _candidate_sessions() -> Set[str]:
    """Every session any lifecycle source currently knows about.

    In-memory sources are inherently this-install; the durable parked-marker
    query is instance-scoped. Read-only.
    """
    candidates: Set[str] = set()
    try:
        from app.core.live_session_store import live_session_store

        candidates.update(live_session_store._sessions.keys())  # noqa: SLF001
    except Exception:
        pass
    try:
        from app.core.task_registry import task_registry

        candidates.update(task_registry._tasks.keys())  # noqa: SLF001
        candidates.update(task_registry._unpark_pending)  # noqa: SLF001
    except Exception:
        pass
    try:
        from app.core.sync_engine import sync_engine

        candidates.update(sync_engine._streaming_sessions)  # noqa: SLF001
    except Exception:
        pass
    try:
        candidates.update(await run_sync(_sessions_with_parked_markers))
    except Exception:
        logger.debug("parked-marker candidate query failed", exc_info=True)
    return candidates


async def reconcile_all_once() -> int:
    """One periodic sweep over every candidate session. Never raises."""
    from app.core import startup as _startup

    if _startup.is_shutting_down:
        return 0
    total = 0
    try:
        candidates = await _candidate_sessions()
    except Exception:
        logger.debug("lifecycle-reconcile candidate scan failed", exc_info=True)
        return 0
    for session_id in candidates:
        if _startup.is_shutting_down:
            break
        try:
            total += await reconcile_session(session_id)
        except Exception:
            # reconcile_session already guards; belt-and-braces.
            logger.debug(
                "lifecycle-reconcile failed for %s", session_id, exc_info=True
            )
    if total:
        logger.info("lifecycle-reconcile sweep repaired %d item(s)", total)
    return total


async def periodic_reconcile_loop() -> None:
    """Background task: run one reconcile sweep every interval.

    Registered in ``startup.start_background_tasks``; cancelled on shutdown.
    """
    while True:
        try:
            await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)
            await reconcile_all_once()
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("lifecycle-reconcile periodic sweep failed")
