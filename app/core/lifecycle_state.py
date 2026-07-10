"""Derived session-lifecycle view + drift sensor (Phase 1 of the lifecycle
unification).

A session's lifecycle is currently spread across FIVE independent sources of
truth, each written by a different subsystem:

  1. ``live_session_store``     — in-memory message buffer + ``is_streaming``
  2. ``session_pool``           — pooled SDK client state (``SessionState``)
  3. ``sync_engine``            — cross-device ``_streaming_sessions`` set
  4. ``task_registry``          — background tasks + the ``unpark_pending`` latch
  5. durable ``sessions`` rows  — ``parked_task_kind`` (v51)

plus two adjacent signals: the between-turns drain pump
(``app.api.ws_cleanup._drain_pumps``) and undrained ``queued_messages`` rows.

Phase-1 discipline is DERIVATION BEFORE ENFORCEMENT: this module only READS
those sources and derives one coherent snapshot; it flips no readers and
changes no writers. ``get_live_session`` destructures ``active_tasks`` /
``unpark_pending`` from the snapshot — the values (and the exact side-effect
ordering that produces them) are byte-identical to what it computed inline
before (see tests/api/test_live_lifecycle_differential.py). Disagreements
between the sources are reported as DRIFT (log-only, stable prefix
``lifecycle-drift``); repairs live in ``app.core.lifecycle_reconcile``.

State model (see docs/lifecycle-states.md):

  streaming        — a turn is actively producing output
                     (authority: ``live_session_store.is_streaming`` — the same
                     authority ordering /live uses today)
  waking           — not streaming, but a follow-up turn is imminent: the
                     ``unpark_pending`` latch is set OR undrained queued
                     messages exist (the queued-messages keep-alive absorbed
                     verbatim from sessions.py)
  parked_with_work — not streaming, no follow-up committed, but visible
                     background tasks exist (running work or a retained
                     terminal row inside its display window)
  idle             — none of the above
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.db import database
from app.db.async_database import run_sync

logger = logging.getLogger(__name__)

# The four derived lifecycle states.
STATE_IDLE = "idle"
STATE_STREAMING = "streaming"
STATE_PARKED_WITH_WORK = "parked_with_work"
STATE_WAKING = "waking"


@dataclass
class LifecycleSnapshot:
    """One coherent read of every lifecycle source for a session.

    ``state`` is the derived classification; the remaining fields are the RAW
    inputs it was derived from (kept so the reconciler and the drift log can
    show their work). ``active_tasks`` is the ``get_visible`` DISPLAY view —
    the exact list /live attaches as ``activeTasks``; ``unpark_pending`` is
    the COMPOSED wire value (registry latch OR queued keep-alive), also
    attached verbatim.
    """

    session_id: str
    state: str

    # Raw inputs
    live_store_streaming: bool = False
    pool_present: bool = False
    pool_connected: bool = False
    pool_streaming: bool = False
    sync_streaming: bool = False
    active_tasks: List[Dict[str, Any]] = field(default_factory=list)
    background_work: List[Dict[str, Any]] = field(default_factory=list)
    unpark_pending: bool = False       # composed wire value (latch OR queue)
    unpark_latch: bool = False         # raw registry latch
    queued_count: Optional[int] = None  # None = not queried (latch already set) or query failed
    parked_task_kind: Optional[str] = None
    pump_running: bool = False

    # Drift sensor output: human-readable disagreement descriptions. Empty when
    # every source agrees. Log-only in Phase 1.
    drift: List[str] = field(default_factory=list)


def _pump_running(session_id: str) -> bool:
    """True iff a live between-turns drain pump exists for the session.

    READ-ONLY view of ``ws_cleanup._drain_pumps`` — never touches the pump
    (the single-reader invariant and ``stop_drain_pump`` ownership are not
    this module's business).
    """
    try:
        from app.api import ws_cleanup

        task = ws_cleanup._drain_pumps.get(session_id)  # noqa: SLF001
        return task is not None and not task.done()
    except Exception:
        return False


def _detect_drift(snap: LifecycleSnapshot) -> List[str]:
    """Cross-source disagreement classes (log-only).

    Every entry is a stable, greppable ``class=...`` string. These are
    SENSORS, not judgments: some classes are legal for a short window (e.g.
    ``unpark-no-pump`` during the latch -> start_streaming hand-off), which is
    exactly why Phase 1 only logs and the reconciler age-gates its repairs.
    """
    drift: List[str] = []

    # (1) sync_engine vs live store disagree on streaming.
    if snap.sync_streaming != snap.live_store_streaming:
        drift.append(
            "class=sync-vs-livestore "
            f"sync_streaming={snap.sync_streaming} "
            f"live_store_streaming={snap.live_store_streaming}"
        )

    # (2) durable parked marker set while the derived state is idle.
    if snap.parked_task_kind and snap.state == STATE_IDLE:
        drift.append(
            f"class=parked-marker-while-idle parked_task_kind={snap.parked_task_kind}"
        )

    # (3) unpark latch with no pump and nothing streaming: the committed
    # follow-up has neither a between-turns reader nor a live turn. Legal for
    # the brief latch -> follow-up-start hand-off; stale beyond that.
    if snap.unpark_latch and not snap.pump_running and not snap.live_store_streaming:
        drift.append("class=unpark-no-pump")

    # (4) registry says background work is running but the pooled SDK client
    # that owns its completion notification is gone/dead.
    if snap.background_work and (not snap.pool_present or not snap.pool_connected):
        drift.append(
            "class=bg-work-dead-client "
            f"tasks={[t.get('id') for t in snap.background_work]} "
            f"pool_present={snap.pool_present} pool_connected={snap.pool_connected}"
        )

    return drift


async def compute(
    session_id: str,
    session_row: Optional[Dict[str, Any]] = None,
) -> LifecycleSnapshot:
    """Derive the lifecycle snapshot for a session.

    ``session_row`` is the already-fetched ``sessions`` row when the caller
    has one (``/live`` does — ``require_session`` fetched it); passing it
    avoids a second DB read on the poll hot path. When omitted, the row is
    fetched best-effort (the reconciler's periodic sweep does this).

    ORDERING CONTRACT: the first three reads below — ``get_visible``, the
    ``unpark_pending`` latch, and the queued-messages keep-alive (queried ONLY
    when the latch is unset, failures swallowed to ``debug``) — are absorbed
    VERBATIM from ``get_live_session`` (sessions.py), in the same order and
    with the same side effects (``get_visible`` prunes the terminal-retention
    store), so destructuring this snapshot is byte-identical to the inline
    code it replaced. New raw inputs are read strictly AFTER them.
    """
    from app.core.live_session_store import live_session_store
    from app.core.task_registry import task_registry

    # --- absorbed verbatim from get_live_session (order matters) -----------
    # Active background tasks (SDK agent tasks + Monitor shell subprocesses)
    # PLUS any recently-failed task still inside the retention window.
    # ``get_visible`` (not ``get_active``) is the DISPLAY view.
    active_tasks = task_registry.get_visible(session_id)

    # "Unpark pending": an unpark follow-up turn is committed but has NOT yet
    # flipped ``is_streaming`` true.
    unpark_latch = task_registry.is_unpark_pending(session_id)
    unpark_pending = unpark_latch

    # Keep-polling ALSO stays up while undrained queued messages await the
    # drain pump: a follow-up turn is imminent even though nothing is
    # streaming this instant. Reuses ``unpark_pending`` as the keep-alive
    # channel; scoped narrowly to the exact "a queue row still exists"
    # condition. Only queried when the latch is unset (same short-circuit as
    # the inline code); a failed count degrades to False, never raises.
    queued_count: Optional[int] = None
    if not unpark_pending:
        try:
            queued_count = await run_sync(database.count_queued_messages, session_id)
            if queued_count > 0:
                unpark_pending = True
        except Exception:
            queued_count = None
            logger.debug(
                "count_queued_messages failed for %s", session_id, exc_info=True
            )
    # --- end verbatim block -------------------------------------------------

    # Streaming authority: the live store — the same authority /live serves
    # today (per-path wire values keep reading it directly).
    live_store_streaming = live_session_store.is_streaming(session_id)

    # Remaining raw inputs (read-only; no side effects).
    background_work = task_registry.get_active_background_work(session_id)

    pool_present = False
    pool_connected = False
    pool_streaming = False
    try:
        from app.core.session_pool import get_session_pool

        state = get_session_pool().peek(session_id)
        if state is not None:
            pool_present = True
            pool_connected = bool(getattr(state, "is_connected", False))
            pool_streaming = bool(getattr(state, "is_streaming", False))
    except Exception:
        logger.debug("session pool peek failed for %s", session_id, exc_info=True)

    sync_streaming = False
    try:
        from app.core.sync_engine import sync_engine

        sync_streaming = sync_engine.is_session_streaming(session_id)
    except Exception:
        logger.debug("sync_engine streaming read failed for %s", session_id, exc_info=True)

    parked_task_kind: Optional[str] = None
    try:
        if session_row is None:
            session_row = await run_sync(database.get_session, session_id)
        if session_row:
            parked_task_kind = session_row.get("parked_task_kind")
    except Exception:
        logger.debug("session row read failed for %s", session_id, exc_info=True)

    pump_running = _pump_running(session_id)

    # Derived state. Precedence: streaming > waking > parked_with_work > idle.
    # ``waking`` outranks ``parked_with_work`` because a committed follow-up
    # turn is a stronger claim about what happens next than lingering visible
    # work (both can be true mid-hand-off).
    if live_store_streaming:
        state_name = STATE_STREAMING
    elif unpark_pending:
        state_name = STATE_WAKING
    elif active_tasks:
        state_name = STATE_PARKED_WITH_WORK
    else:
        state_name = STATE_IDLE

    snap = LifecycleSnapshot(
        session_id=session_id,
        state=state_name,
        live_store_streaming=live_store_streaming,
        pool_present=pool_present,
        pool_connected=pool_connected,
        pool_streaming=pool_streaming,
        sync_streaming=sync_streaming,
        active_tasks=active_tasks,
        background_work=background_work,
        unpark_pending=unpark_pending,
        unpark_latch=unpark_latch,
        queued_count=queued_count,
        parked_task_kind=parked_task_kind,
        pump_running=pump_running,
    )
    snap.drift = _detect_drift(snap)

    if snap.drift:
        # Stable prefix + one line per class. Plain INFO (mirrors the
        # _log_unpark_decision verbosity decision): drift fires only while
        # sources disagree — low-frequency, and exactly the trace a live
        # incident needs. Never raises into the caller.
        try:
            for entry in snap.drift:
                logger.info(
                    "lifecycle-drift session=%s state=%s %s",
                    session_id,
                    snap.state,
                    entry,
                )
        except Exception:
            pass

    return snap
