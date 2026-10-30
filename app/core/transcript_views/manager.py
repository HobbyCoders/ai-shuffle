"""One low-priority projection actor, finite queues and best-effort wake hints.

No append/mirror hook awaits this work. Dropped/lost hints are repaired by a
bounded keyset scan. Mirror receivers use the same local source table and fold;
none of the installation-local rows enter the replication protocol.
"""
from __future__ import annotations

import asyncio
from collections import deque
import logging
import os
import threading
import time

from app.core.event_gateway import get_gateway
from app.db import transcript_projection_work as work
from app.db import transcript_projections as store
from .bridge import ReducerBridge, ReducerUnavailable, verified_manifest
from .producer import ProjectionActor, database


logger = logging.getLogger(__name__)
LIVE_QUEUE = 8
BACKFILL_QUEUE = 24
SCAN_SECONDS = 15.0
RETRY_SECONDS = 30.0


def enabled() -> bool:
    return os.environ.get('AI_SHUFFLE_TRANSCRIPT_PRODUCER', '1').strip().lower() not in {'0', 'off', 'false', 'no'}


class ProjectionProducer:
    def __init__(self, *, bridge=None, gateway=None, actor=None, scan_seconds=SCAN_SECONDS):
        self.bridge = bridge if bridge is not None else ReducerBridge()
        self.gateway = gateway
        self.actor = actor
        self.scan_seconds = scan_seconds
        self._live: asyncio.Queue[str] = asyncio.Queue(maxsize=LIVE_QUEUE)
        self._backfill: asyncio.Queue[str] = asyncio.Queue(maxsize=BACKFILL_QUEUE)
        self._pending: dict[str, bool] = {}
        self._counts = {False: 0, True: 0}
        self._lock = threading.Lock()
        self._available = asyncio.Event()
        self._loop = None
        self._task = None
        self._closed = True
        self._live_burst = 0
        self._cursor = ''
        self._durations = deque(maxlen=128)
        self.completed = self.dropped_hints = self.max_pending = 0
        self.last_error: str | None = None

    def start(self) -> None:
        if not enabled() or self._task is not None:
            return
        self._loop = asyncio.get_running_loop()
        with self._lock:
            self._closed = False
        self._task = self._loop.create_task(self._run(), name='transcript-projection-producer')

    def wake(self, session_id: str, *, backfill: bool = False) -> bool:
        """Called from the PG listener: no DB, awaits, payloads or waiter queue."""
        if not isinstance(session_id, str) or not session_id or len(session_id) > 512:
            return False
        with self._lock:
            if self._closed or self._loop is None:
                return False
            if session_id in self._pending:
                return True
            capacity = BACKFILL_QUEUE if backfill else LIVE_QUEUE
            if self._counts[backfill] >= capacity:
                self.dropped_hints += 1
                return False
            self._pending[session_id] = backfill
            self._counts[backfill] += 1
            self.max_pending = max(self.max_pending, len(self._pending))
            # Admission precedes scheduling: a stalled loop can never build an
            # unbounded call_soon_threadsafe backlog under a NOTIFY storm.
            self._loop.call_soon_threadsafe(self._enqueue, session_id, backfill)
            return True

    def _enqueue(self, session_id: str, backfill: bool) -> None:
        with self._lock:
            if self._closed or session_id not in self._pending:
                return
        queue = self._backfill if backfill else self._live
        try:
            queue.put_nowait(session_id)
        except asyncio.QueueFull:
            with self._lock:
                if self._pending.pop(session_id, None) is not None:
                    self._counts[backfill] -= 1
                self.dropped_hints += 1
            return
        self._available.set()

    def _next(self) -> str | None:
        if not self._backfill.empty() and (self._live.empty() or self._live_burst >= LIVE_QUEUE):
            queue = self._backfill
            self._live_burst = 0
        elif not self._live.empty():
            queue = self._live
            self._live_burst += 1
        else:
            return None
        session_id = queue.get_nowait()
        queue.task_done()
        with self._lock:
            backfill = self._pending.pop(session_id)
            self._counts[backfill] -= 1
        return session_id

    def stats(self) -> dict:
        with self._lock:
            pending = len(self._pending)
        durations = sorted(self._durations)
        p95 = durations[min(len(durations) - 1, int(len(durations) * .95))] if durations else 0
        return {'pending': pending, 'max_pending': self.max_pending, 'live_capacity': LIVE_QUEUE,
                'backfill_capacity': BACKFILL_QUEUE, 'completed_quanta': self.completed,
                'dropped_hints': self.dropped_hints, 'quantum_p95_ms': round(p95 * 1000, 3),
                'last_error': self.last_error}

    async def _run(self) -> None:
        listening = False
        try:
            if not enabled():
                return
            if self.actor is None:
                manifest = await database(verified_manifest)
                self.actor = ProjectionActor(self.bridge, fingerprint=manifest['fingerprint'])
            # Missing/old Node or stale assets are optional: do not even create
            # derived tables until the real bundled process answers correctly.
            await self.bridge.request('plan', carry=None, frames=[])
            await database(store.initialize)
            self.gateway = self.gateway or get_gateway()
            self.gateway.add_global_listener(self.wake)
            listening = True
            next_scan = 0.0
            while enabled():
                now = asyncio.get_running_loop().time()
                if now >= next_scan:
                    sessions = await database(work.source_sessions, self._cursor)
                    if not sessions:
                        self._cursor = ''
                    for session_id in sessions:
                        if not self.wake(session_id, backfill=True):
                            break  # Never advance past a dropped discovery hint.
                        self._cursor = session_id
                    next_scan = now + self.scan_seconds
                session_id = self._next()
                if session_id is None:
                    if await database(work.collect_garbage):
                        # Bound cleanup rate too; never spin at idle over a
                        # large retired generation and starve live ingestion.
                        await asyncio.sleep(.01)
                        continue
                    self._available.clear()
                    try:
                        await asyncio.wait_for(self._available.wait(), max(.001, next_scan - now))
                    except asyncio.TimeoutError:
                        pass
                    continue
                started = time.perf_counter()
                try:
                    more = await self.actor.process(session_id)
                    self.completed += 1
                    self.last_error = None
                    self._durations.append(time.perf_counter() - started)
                    if more:
                        self.wake(session_id, backfill=True)
                except ReducerUnavailable as error:
                    # Fixed codes only. A native/provider/SQL exception may
                    # contain credentials or private text; never log it here.
                    self.last_error = error.code
                    logger.warning('Transcript producer unavailable; full replay retained (%s)', error.code)
                    self.wake(session_id, backfill=True)
                    await asyncio.sleep(RETRY_SECONDS)
                except Exception:
                    self.last_error = 'projection_unavailable'
                    logger.warning('Transcript producer unavailable; full replay retained')
                    self.wake(session_id, backfill=True)
                    await asyncio.sleep(RETRY_SECONDS)
                await asyncio.sleep(0)  # one finite actor quantum per loop turn
        except ReducerUnavailable as error:
            self.last_error = error.code
            logger.info('Transcript producer disabled; full replay retained (%s)', error.code)
        except Exception:
            self.last_error = 'projection_unavailable'
            logger.warning('Transcript producer disabled; full replay retained')
        finally:
            with self._lock:
                self._closed = True
                self._pending.clear()
                self._counts = {False: 0, True: 0}
            try:
                if listening:
                    self.gateway.remove_global_listener(self.wake)
            finally:
                await self.bridge.close()

    async def stop(self) -> None:
        with self._lock:
            self._closed = True
        task = self._task
        try:
            if task is not None:
                if not task.done():
                    task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        finally:
            # Cancellation before _run's first instruction never enters its
            # finally block. The lifecycle owner still closes its own child.
            await self.bridge.close()
