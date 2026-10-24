"""FIFO, cancellation-aware admission for NEW turns; never a run/lease limiter.

The durable turns queue is authoritative (and remains visible as Queued). Only
128 callers wait in memory by default, one per session. Overflow or the bounded
wait deadline defers a kick, NOT the accepted turn. Callers retry without dropping
work. FIFO applies to this worker's waiting set; durable overflow retries join its
tail. Remote dispatch is admitted by the executing worker, never by the sender.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from app.core.config import settings
from app.db import base

logger = logging.getLogger(__name__)
Result = Literal["ready", "deferred", "cancelled"]


def snapshot() -> dict[str, float]:
    """Read counters only: no checkout, SQL, libpq lock or lease slot."""
    pool = base.pool_stats() or {}
    pressure = base.pressure_stats() or {}
    events = base.event_pressure_stats()
    capacity = max(1, pressure.get("ordinary_capacity", pool.get("max", 1)))
    return {
        "utilization": pool.get("in_use", 0) / capacity,
        "waiting": pressure.get("waiting", 0) + events["waiting"],
        "oldest": max(pressure.get("oldest_hold_seconds", 0),
                      events["oldest_wait_seconds"], events["oldest_write_seconds"]),
        "latency": pressure.get("hold_seconds_total", 0),
        "holds": pressure.get("holds", 0),
        "acquisition": pressure.get("acquisition_seconds_total", 0),
        "acquisitions": pressure.get("acquisitions", 0),
    }


@dataclass
class _Waiter:
    session_id: str
    cancelled: threading.Event = field(default_factory=threading.Event)


class StartAdmission:
    def __init__(self) -> None:
        self._queue: OrderedDict[str, _Waiter] = OrderedDict()
        self._lock = threading.Lock()  # remove_queued may run in a DB thread
        self._high_since: float | None = None
        self._low_since: float | None = None
        self._blocked = False
        self._next_start = 0.0
        self._previous: dict[str, float] = {}
        self._slow_until = 0.0

    @property
    def queued(self) -> int:
        with self._lock:
            return len(self._queue)

    def cancel(self, turn_id: str) -> None:
        with self._lock:
            waiter = self._queue.pop(turn_id, None)
            if waiter is not None:
                waiter.cancelled.set()

    def _pressured(self, now: float) -> bool:
        stats = snapshot()
        # Recent interval means, not lifetime maxima: a past stall must recover.
        for duration, count in (("latency", "holds"), ("acquisition", "acquisitions")):
            n = stats.get(count, 0) - self._previous.get(count, 0)
            seconds = stats.get(duration, 0) - self._previous.get(duration, 0)
            if n > 0 and seconds / n >= settings.db_start_latency_seconds:
                self._slow_until = now + settings.db_start_recovery_seconds
        self._previous = stats
        high = (stats["utilization"] >= settings.db_start_utilization
                or stats["waiting"] > 0
                or stats["oldest"] >= max(settings.db_start_sustain_seconds, 0.001)
                or now < self._slow_until)
        low = (stats["utilization"] < settings.db_start_utilization * 0.75
               and stats["waiting"] == 0
               and stats["oldest"] < max(settings.db_start_sustain_seconds, 0.001)
               and now >= self._slow_until)
        if high:
            self._low_since = None
            if self._high_since is None:
                self._high_since = now
            if (now - self._high_since >= settings.db_start_sustain_seconds
                    or stats["oldest"] >= max(settings.db_start_sustain_seconds, 0.001)):
                self._blocked = True
        else:
            self._high_since = None
            if low:
                if self._low_since is None:
                    self._low_since = now
                if now - self._low_since >= settings.db_start_recovery_seconds:
                    self._blocked = False
            else:
                self._low_since = None
        return self._blocked

    async def wait(
        self, turn_id: str, session_id: str, cancelled: Callable[[], bool],
    ) -> Result:
        if cancelled():
            return "cancelled"
        now = time.monotonic()
        blocked = self._pressured(now)
        with self._lock:
            # Duplicate scheduler/claim kicks must not occupy queue capacity.
            if any(w.session_id == session_id for w in self._queue.values()):
                return "cancelled"
            if not blocked and not self._queue and now >= self._next_start:
                return "ready"
            if len(self._queue) >= settings.db_start_max_waiters:
                return "deferred"
            waiter = _Waiter(session_id)
            self._queue[turn_id] = waiter
        logger.info("New turn %s for %s: Queued (database busy)", turn_id, session_id)
        deadline = now + settings.db_start_wait_seconds
        try:
            while True:
                if waiter.cancelled.is_set() or cancelled():
                    return "cancelled"
                now = time.monotonic()
                blocked = self._pressured(now)
                with self._lock:
                    first = next(iter(self._queue), None) == turn_id
                if first and not blocked and now >= self._next_start:
                    self._next_start = now + settings.db_start_interval_seconds
                    return "ready"
                if now >= deadline:
                    logger.info("New turn %s still Queued (database busy); retrying", turn_id)
                    return "deferred"
                await asyncio.sleep(settings.db_start_poll_seconds)
        finally:
            with self._lock:
                if self._queue.get(turn_id) is waiter:
                    self._queue.pop(turn_id)


admission = StartAdmission()
