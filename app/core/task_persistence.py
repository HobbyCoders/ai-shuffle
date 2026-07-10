"""Durable background-task snapshot writer (Phase 1, WRITE-ONLY).

``task_registry`` is in-memory and rebuilt empty on every restart — which is
exactly why a whole class of lifecycle incidents (stranded parked sessions,
silent kills, forever-spinning cards) has been invisible after the fact.
This module mirrors every registry transition into the durable
``background_tasks`` table (v55) WITHOUT changing any behavior:

- WRITE-ONLY: no reader flips anywhere in Phase 1. The rows are a durable
  trail (plus boot reconcile's stale-row sweep), nothing more yet.
- FIRE-AND-FORGET: ``enqueue_task_snapshot`` is sync, never raises, never
  blocks — the registry hot path (called inside streaming loops) must not
  gain a DB write. Snapshots land in a COALESCING queue (latest snapshot per
  ``(session_id, task_id)`` wins) drained by a single asyncio consumer.
- OUTAGE-QUIET: a failing write logs ONCE per outage (WARNING), then stays
  silent until a write succeeds again. Failed snapshots are dropped — the
  table is a mirror of in-memory truth, and the next transition re-mirrors.
- KILL SWITCH: ``settings.durable_task_state`` (env AI_SHUFFLE_DURABLE_TASKS,
  default on). Off = every enqueue is a no-op.
- ``flush()`` drains everything synchronously for tests.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional, Tuple

from app.core.config import settings

logger = logging.getLogger(__name__)

# Coalescing queue: latest snapshot per (session_id, task_id) wins. Plain dict
# (insertion-ordered) on the single asyncio loop, mirroring task_registry.
_pending: Dict[Tuple[str, str], Dict[str, Any]] = {}
_wakeup: Optional[asyncio.Event] = None
_consumer: Optional[asyncio.Task] = None
# One-log-per-outage latch: True after the first failed write, reset by the
# next successful one.
_outage_logged = False


def is_enabled() -> bool:
    """Kill switch (settings.durable_task_state). Never raises."""
    try:
        return bool(settings.durable_task_state)
    except Exception:
        return False


def enqueue_task_snapshot(
    session_id: str, task_id: str, snapshot: Dict[str, Any]
) -> None:
    """Queue one task snapshot for durable write. Sync, never raises.

    Latest snapshot per ``(session_id, task_id)`` wins — a task that moves
    running -> completed before the consumer wakes writes only the terminal
    row. Callable with no running loop (a threaded caller): the snapshot is
    queued and picked up when the next loop-side enqueue (or ``flush``) runs.
    """
    if not session_id or not task_id:
        return
    try:
        if not is_enabled():
            return
        _pending[(session_id, task_id)] = snapshot
        _ensure_consumer()
    except Exception:
        logger.debug("task persistence enqueue failed", exc_info=True)


def _ensure_consumer() -> None:
    """Start (or wake) the single consumer task on the running loop."""
    global _consumer, _wakeup
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # no loop here — the queue holds the snapshot for later
    if _wakeup is None:
        _wakeup = asyncio.Event()
    _wakeup.set()
    if _consumer is None or _consumer.done():
        _consumer = loop.create_task(_consume())


async def _consume() -> None:
    """Single consumer: drain the coalescing queue, then sleep on the event."""
    while True:
        event = _wakeup
        if event is None:
            return  # reset() tore us down
        if not _pending:
            event.clear()
            await event.wait()
        await _drain_once()


async def _drain_once() -> None:
    """Write every queued snapshot (latest-wins already applied by the dict)."""
    global _outage_logged
    from app.db import database

    while _pending:
        key = next(iter(_pending))
        snapshot = _pending.pop(key)
        try:
            await asyncio.to_thread(database.upsert_background_task, snapshot)
            if _outage_logged:
                _outage_logged = False
                logger.info("task persistence recovered — durable writes resumed")
        except Exception:
            if not _outage_logged:
                _outage_logged = True
                logger.warning(
                    "task persistence write failed — snapshots will be dropped "
                    "until the database recovers (logged once per outage)",
                    exc_info=True,
                )


async def flush() -> None:
    """Drain the queue NOW and return when every write has been attempted.

    Test hook (and graceful-shutdown convenience): production never awaits
    persistence.
    """
    await _drain_once()


def pending_count() -> int:
    """Snapshots still queued (tests/diagnostics)."""
    return len(_pending)


def reset() -> None:
    """Drop all queued snapshots and stop the consumer (tests/shutdown)."""
    global _consumer, _wakeup, _outage_logged
    if _consumer is not None and not _consumer.done():
        _consumer.cancel()
    _consumer = None
    _wakeup = None
    _pending.clear()
    _outage_logged = False
