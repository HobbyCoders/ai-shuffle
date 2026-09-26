"""Derived session-lifecycle view + drift sensor.

A session's lifecycle is read from several independent sources, each written
by a different subsystem:

  1. durable ``runs`` rows      — lease-valid active run = a turn in flight
                                  (``app.core.streaming_status``), unioned with
                                  the pooled adapter's ``is_streaming`` peek
  2. ``task_registry``          — visible / active background work
  3. durable ``sessions`` rows  — ``parked_task_kind``
  4. the ``turns`` table        — a queued ``source=unpark`` wake and a
                                  ``parked`` turn (``_v2_turn_signals``)

``unpark_latch``, ``queued_count`` and ``pump_running`` stay on the snapshot
as inert fields so the shape does not change.

This module only READS those sources and derives one coherent snapshot;
``get_live_session`` destructures ``active_tasks`` / ``unpark_pending`` from
it. Disagreements between the sources are reported as DRIFT (log-only,
stable prefix ``lifecycle-drift``).

State model:

  streaming        — a turn is actively producing output
                     (authority: a lease-valid active run in the durable
                     ``runs`` table — ``app.core.streaming_status`` — unioned
                     with the pooled adapter's ``is_streaming`` peek)
  waking           — not streaming, but a follow-up turn is imminent: a
                     queued ``source=unpark`` turn exists
  parked_with_work — not streaming, no follow-up committed, but visible
                     background tasks exist (running work or a retained
                     terminal row inside its display window) or a turn is
                     ``parked``
  idle             — none of the above
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

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
    the composed wire value (a queued unpark turn), also attached verbatim.
    """

    session_id: str
    state: str

    # Raw inputs. ``live_store_streaming`` is the busy AUTHORITY: a lease-valid
    # active run in the durable runs table, unioned with the pooled client's
    # is_streaming peek (the name is historical — there is no live store).
    live_store_streaming: bool = False
    pool_present: bool = False
    pool_connected: bool = False
    pool_streaming: bool = False
    active_tasks: List[Dict[str, Any]] = field(default_factory=list)
    background_work: List[Dict[str, Any]] = field(default_factory=list)
    unpark_pending: bool = False       # composed wire value (queued unpark turn)
    unpark_latch: bool = False         # inert, always False
    queued_count: Optional[int] = None  # inert, always None
    parked_task_kind: Optional[str] = None
    pump_running: bool = False

    # Drift sensor output: human-readable disagreement descriptions. Empty when
    # every source agrees. Log-only.
    drift: List[str] = field(default_factory=list)


# --- transition-only drift logging -----------------------------------------
# Drift is a PERSISTENT condition sampled on every /live poll (per-second while
# a session is watched), so logging each observation buried live incidents under
# hundreds of identical lines. Instead we log on TRANSITION: the first poll a
# ``(session, class)`` drift newly appears, then once an hour while it still
# stands, and again after it clears and reappears.
_DRIFT_RELOG_SECONDS = 3600.0

# (session_id, class_token) -> monotonic timestamp of the last emitted line.
_drift_last_log: Dict[Tuple[str, str], float] = {}


def reset_drift_log_state() -> None:
    """Clear the transition-log memo (test hook / restart hygiene)."""
    _drift_last_log.clear()


def _class_token(entry: str) -> str:
    """The stable ``class=<name>`` prefix that keys a drift entry."""
    return entry.split(" ", 1)[0]


def _drift_entries_to_log(session_id: str, drift: List[str]) -> List[str]:
    """Transition gate over a session's current drift entries.

    Returns the subset to actually emit this poll: a class newly appeared for
    the session, or its last emission is older than the hourly re-log window.
    Classes absent this poll (the condition cleared) are forgotten so their next
    appearance counts as a fresh transition. Keyed per ``(session, class)`` —
    the entry's trailing detail (e.g. changing task ids) does not re-trigger.
    """
    now = time.monotonic()
    current = {_class_token(e): e for e in drift}

    # Forget cleared classes for this session (present-before, absent-now).
    for key in [
        k for k in _drift_last_log if k[0] == session_id and k[1] not in current
    ]:
        _drift_last_log.pop(key, None)

    to_log: List[str] = []
    for token, entry in current.items():
        key = (session_id, token)
        last = _drift_last_log.get(key)
        if last is None or (now - last) >= _DRIFT_RELOG_SECONDS:
            _drift_last_log[key] = now
            to_log.append(entry)
    return to_log


def _v2_turn_signals(session_id: str) -> Tuple[bool, bool]:
    """Turns-table lifecycle inputs (fully guarded).

    Returns ``(has_queued_unpark, has_parked_turn)``:

    * ``has_queued_unpark`` — a ``queued`` ``source=unpark``/``unpark_pump`` turn
      exists (a wake is imminent) → contributes to ``waking``;
    * ``has_parked_turn`` — a ``parked`` turn exists (foreground done, background
      work survives) → contributes to ``parked_with_work``.

    Returns ``(False, False)`` on ANY error, so a failed read leaves the other
    signals to decide.
    """
    try:
        from app.db import turns as turns_db
        from app.db.base import get_db

        has_queued_unpark = any(
            t.get("source") in ("unpark", "unpark_pump")
            for t in turns_db.get_queued_turns(session_id)
        )
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM turns WHERE session_id=%s AND state='parked' LIMIT 1",
                (session_id,),
            )
            has_parked_turn = cursor.fetchone() is not None
        return (has_queued_unpark, has_parked_turn)
    except Exception:
        logger.debug("v2 turn-signal read failed for %s", session_id, exc_info=True)
        return (False, False)


def _detect_drift(snap: LifecycleSnapshot) -> List[str]:
    """Cross-source disagreement classes (log-only).

    Every entry is a stable, greppable ``class=...`` string. These are
    SENSORS, not judgments: some classes are legal for a short window, which
    is why they are only logged.
    """
    drift: List[str] = []

    # (1) durable parked marker set while the derived state is idle.
    if snap.parked_task_kind and snap.state == STATE_IDLE:
        drift.append(
            f"class=parked-marker-while-idle parked_task_kind={snap.parked_task_kind}"
        )

    # (2) unpark latch with no between-turns reader and nothing streaming.
    # Both the latch and the pump flag are inert (see ``compute``), so this
    # class cannot fire.
    if snap.unpark_latch and not snap.pump_running and not snap.live_store_streaming:
        drift.append("class=unpark-no-pump")

    # (3) registry says background work is running but the pooled adapter
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
    fetched best-effort.

    ORDERING CONTRACT: ``get_visible`` runs first (it prunes the
    terminal-retention store); every other input is read after it.
    """
    from app.core.task_registry import task_registry

    # Active background tasks (agent tasks + Monitor shell subprocesses)
    # PLUS any recently-failed task still inside the retention window.
    # ``get_visible`` (not ``get_active``) is the DISPLAY view.
    active_tasks = task_registry.get_visible(session_id)

    # The composed ``unpark_pending`` wire value is driven solely by the
    # ``has_queued_unpark`` signal below (a queued ``source=unpark`` turn).
    # ``unpark_latch`` / ``queued_count`` stay on the snapshot for shape
    # parity, hard-set to their inert values.
    unpark_latch = False
    unpark_pending = unpark_latch
    queued_count: Optional[int] = None

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

    # Streaming authority: the durable runs table (a lease-valid
    # active run means a turn is in flight), unioned with the pooled client's
    # is_streaming peek (belt-and-braces: a turn whose run claim failed records
    # UNFENCED and reads idle from runs, but the pooled client still knows).
    runs_streaming = False
    try:
        from app.core import streaming_status

        runs_streaming = streaming_status.has_active_run(session_id)
    except Exception:
        logger.debug("runs busy read failed for %s", session_id, exc_info=True)
    live_store_streaming = runs_streaming or pool_streaming

    parked_task_kind: Optional[str] = None
    try:
        if session_row is None:
            session_row = await run_sync(database.get_session, session_id)
        if session_row:
            parked_task_kind = session_row.get("parked_task_kind")
    except Exception:
        logger.debug("session row read failed for %s", session_id, exc_info=True)

    # Nothing registers a between-turns pump, so this signal is inert.
    # Retained on the snapshot for shape parity, hard-set False.
    pump_running = False

    # Turns-table signals ((False, False) on a read error).
    # A queued unpark turn folds into the composed ``unpark_pending`` wire value
    # (so the /live wire agrees with the derived state); a parked turn is an
    # additional trigger for ``parked_with_work``.
    has_queued_unpark, has_parked_turn = await run_sync(
        _v2_turn_signals, session_id
    )
    if has_queued_unpark:
        unpark_pending = True

    # Derived state. Precedence: streaming > waking > parked_with_work > idle.
    # ``waking`` outranks ``parked_with_work`` because a committed follow-up
    # turn is a stronger claim about what happens next than lingering visible
    # work (both can be true mid-hand-off).
    if live_store_streaming:
        state_name = STATE_STREAMING
    elif unpark_pending:
        state_name = STATE_WAKING
    elif active_tasks or has_parked_turn:
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
        active_tasks=active_tasks,
        background_work=background_work,
        unpark_pending=unpark_pending,
        unpark_latch=unpark_latch,
        queued_count=queued_count,
        parked_task_kind=parked_task_kind,
        pump_running=pump_running,
    )
    snap.drift = _detect_drift(snap)

    # Stable prefix + one line per class, gated to TRANSITIONS (see
    # _drift_entries_to_log): a persistent drift logs once when it appears and
    # hourly thereafter, not on every poll. Plain INFO: exactly the trace a
    # live incident needs. The gate itself runs even when drift is empty so a
    # just-cleared condition is forgotten. Never raises into the caller.
    try:
        for entry in _drift_entries_to_log(session_id, snap.drift):
            logger.info(
                "lifecycle-drift session=%s state=%s %s",
                session_id,
                snap.state,
                entry,
            )
    except Exception:
        pass

    return snap
