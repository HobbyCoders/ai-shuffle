"""EventBus — seq-numbered, replayable fan-out.

Semantics:
- ``publish`` assigns the next per-session ``seq`` and appends to the session
  log, then fans out to subscribers. ``seq`` is monotonic for the life of the
  session, not the process: an engine that resumes a persisted session calls
  ``seed(session_id, last_seq)`` first so numbering continues after the rows
  already on disk instead of restarting at 1 and colliding with them.
- The in-process log is a ring of the newest ``log_ring_size`` events per
  session, also bounded by ``log_ring_max_bytes`` estimated bytes (8 MiB by
  default). With a durable source, oversized events are delivered live but not
  kept in the ring. Without a source, active subscribers retain the original
  count-bounded window for lossless lag recovery; the byte bound resumes when
  the last subscriber closes. Everything older is the store's job: ``subscribe(since_seq=N)``
  with N below the ring's floor asks the replay source (the store's
  ``events_since``, attached by the engine) for the gap before streaming from
  the ring. Without a replay source it starts at the floor. A live renderer
  passes ``from_store=False``: it watches this process's events only, so on
  a resumed session it starts at the ring's floor instead of replaying the
  persisted history it never asked to see.
- Subscribers get bounded queues. A slow subscriber never blocks the engine:
  when its queue overflows, the oldest buffered event is dropped and the
  subscriber transparently re-syncs from the log (it owns a cursor, so nothing
  is ever lost — only buffered late).
- ``subscribe(since_seq=N)`` replays the log from N before streaming live —
  reconnect support for WS bridges and TUIs.

Lookups never create state: ``log`` and ``last_seq`` for a session the bus has
not seen return empty/0 without recording the id, and a session's subscriber
list is dropped once its last subscriber leaves. A consumer that stops
iterating early should close the generator (``contextlib.aclosing``) so the
subscriber is removed at once rather than when the garbage collector gets to
it.

Durable replay across restarts belongs to the SessionStore, which is just
another subscriber (with an unbounded queue — it must never drop).
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import fields, is_dataclass, replace
from functools import lru_cache

from fluid.events.model import Event

_CLOSE = object()

# Newest events kept in memory per session. A TUI reconnecting after a longer
# gap replays the rest from the store.
LOG_RING_SIZE = 2048
LOG_RING_MAX_BYTES = 8 * 1024 * 1024
# Publication precedes persistence. Give in-flight commits time to become
# replayable, but do not let a refused/non-persisted event stall a turn forever.
REPLAY_GAP_GRACE_SECONDS = 0.5


@lru_cache(maxsize=128)
def _field_names(event_type):
    return tuple(field.name for field in fields(event_type))


def _retained_size(value, depth=0) -> int:
    """Conservative estimate for JSON-shaped event fields, without copying them."""
    size = sys.getsizeof(value)
    if depth >= 12:
        return size
    if is_dataclass(value) and not isinstance(value, type):
        size += sum(_retained_size(getattr(value, name), depth + 1)
                    for name in _field_names(type(value)))
    elif isinstance(value, dict):
        size += sum(_retained_size(k, depth + 1) + _retained_size(v, depth + 1)
                    for k, v in value.items())
    elif isinstance(value, (list, tuple)):
        size += sum(_retained_size(v, depth + 1) for v in value)
    return size


ReplaySource = Callable[[str, int], Awaitable[list[Event]]]


class _Subscriber:
    def __init__(self, maxsize: int, generation: int = 0) -> None:
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize) if maxsize else asyncio.Queue()
        self.cursor: int = 0  # seq of the last event delivered
        self.replay_history = True
        self.lagging: bool = False
        # The close generation this subscription was opened under. A close
        # ends the subscriptions of its generation; one opened afterwards
        # (a later session on the same bus) runs as normal.
        self.generation: int = generation


class EventBus:
    def __init__(
        self,
        *,
        subscriber_queue_size: int = 1024,
        log_ring_size: int = LOG_RING_SIZE,
        log_ring_max_bytes: int = LOG_RING_MAX_BYTES,
        replay_source: ReplaySource | None = None,
    ) -> None:
        self._logs: dict[str, deque[Event]] = {}
        self._log_sizes: dict[str, deque[int]] = {}
        self._log_bytes: dict[str, int] = {}
        self._ring_max_bytes = max(1, int(log_ring_max_bytes))
        self._last_seq: dict[str, int] = {}
        self._subs: dict[str, list[_Subscriber]] = {}
        self._maxsize = subscriber_queue_size
        self._ring_size = max(1, int(log_ring_size))
        self._replay_source = replay_source
        # Bumped by close(); subscriptions from an older generation end.
        self._close_generation = 0

    # ─── introspection (never creates state) ─────────────────────────────
    def log(self, session_id: str) -> list[Event]:
        """Snapshot of the in-process ring: the newest events, oldest first."""
        ring = self._logs.get(session_id)
        return list(ring) if ring else []

    def last_seq(self, session_id: str) -> int:
        """Seq of the last event published (or seeded) for the session; 0 if none."""
        return self._last_seq.get(session_id, 0)

    def floor_seq(self, session_id: str) -> int:
        """Lowest seq the ring can still replay; ``last_seq + 1`` when it holds nothing."""
        ring = self._logs.get(session_id)
        if ring:
            return ring[0].seq
        return self.last_seq(session_id) + 1

    def subscriber_count(self, session_id: str) -> int:
        return len(self._subs.get(session_id, ()))

    def memory_stats(self) -> dict[str, int]:
        """Cheap counts only; estimates were computed once at publication."""
        return {
            "event_bus_ring_events": sum(map(len, self._logs.values())),
            "event_bus_ring_estimated_bytes": sum(self._log_bytes.values()),
            "event_bus_subscribers": sum(map(len, self._subs.values())),
            "event_bus_queued_events": sum(sub.queue.qsize()
                                           for subs in self._subs.values() for sub in subs),
        }

    # ─── configuration ───────────────────────────────────────────────────
    def seed(self, session_id: str, last_seq: int) -> None:
        """Continue numbering after ``last_seq`` (the persisted maximum).

        Never moves backwards: seeding below what this process already
        published is a no-op, so a late or repeated seed cannot produce
        duplicate seqs.
        """
        if int(last_seq) > self._last_seq.get(session_id, 0):
            self._last_seq[session_id] = int(last_seq)
            for sub in self._subs.get(session_id, ()):
                if not sub.replay_history:
                    sub.cursor = max(sub.cursor, int(last_seq))

    @property
    def replay_source(self) -> ReplaySource | None:
        return self._replay_source

    def set_replay_source(self, source: ReplaySource | None) -> None:
        """Where ``subscribe`` fetches events older than the ring (``store.events_since``)."""
        self._replay_source = source

    # ─── publish / subscribe ─────────────────────────────────────────────
    async def publish(self, event: Event) -> Event:
        """Assign seq and ts, append to the log, fan out. Returns the sequenced event."""
        sid = event.session_id
        seq = self._last_seq.get(sid, 0) + 1
        # Stamp both facts about the event's place in the stream together: its
        # order (seq) and when it happened (ts, epoch seconds). An emitter that
        # already set ts — a replayed or reconstructed event carrying its
        # original time — keeps it; only an unstamped 0.0 is filled in.
        sequenced = replace(event, seq=seq, ts=event.ts or time.time())
        self._last_seq[sid] = seq
        ring = self._logs.get(sid)
        if ring is None:
            ring = self._logs[sid] = deque()
            self._log_sizes[sid] = deque()
            self._log_bytes[sid] = 0
        size = _retained_size(sequenced)
        sizes = self._log_sizes[sid]
        ring.append(sequenced)
        sizes.append(size)
        self._log_bytes[sid] += size
        self._trim_log(sid)
        # Even an event larger than the entire ring budget is delivered live
        # below and persisted by the engine. The ring remains a contiguous
        # suffix (possibly empty), so durable gap replay still works.
        for sub in self._subs.get(sid, ()):
            if sub.lagging:
                continue  # will re-sync from the log when it catches up
            try:
                sub.queue.put_nowait(sequenced)
            except asyncio.QueueFull:
                sub.lagging = True  # stop buffering; iterator falls back to the log
        return sequenced

    def _trim_log(self, sid: str) -> None:
        ring = self._logs.get(sid)
        # Without a durable source, active consumers still need the original
        # count-bounded replay window. Restore the byte bound on unsubscribe.
        live_only = self._replay_source is None and bool(self._subs.get(sid))
        while ring and (len(ring) > self._ring_size or (
            not live_only and self._log_bytes[sid] > self._ring_max_bytes
        )):
            ring.popleft()
            self._log_bytes[sid] -= self._log_sizes[sid].popleft()

    async def _after(
        self, session_id: str, cursor: int, *, from_store: bool = True,
        allow_gap: bool = False,
    ) -> list[Event]:
        """Events with ``seq > cursor``, oldest first, from the ring or the store.

        Normally empty if the next seq is unavailable, even with a newer
        suffix. ``allow_gap`` exposes that suffix after the replay grace period.
        """
        ring = self._logs.get(session_id)
        floor = self.floor_seq(session_id)
        if cursor >= floor - 1:
            if not ring:
                return []
            # Seqs in the ring are contiguous, so the offset is exact; the
            # check guards a seed that jumped numbering mid-ring.
            idx = cursor - floor + 1
            if idx < len(ring) and ring[idx].seq == cursor + 1:
                return [ring[i] for i in range(idx, len(ring))]
            events = [e for e in ring if e.seq > cursor]
            return events if allow_gap or (events and events[0].seq == cursor + 1) else []
        # Older than the ring: the store has the gap.
        if from_store and self._replay_source is not None:
            try:
                stored = await self._replay_source(session_id, cursor)
            except Exception:  # a store that cannot replay: fall through to the ring
                stored = []
            stored = sorted((e for e in stored if e.seq > cursor), key=lambda e: e.seq)
            if stored:
                if allow_gap:
                    available = {e.seq: e for e in stored}
                    available.update((e.seq, e) for e in ring or () if e.seq > cursor)
                    return [available[seq] for seq in sorted(available)]
                return stored if stored[0].seq == cursor + 1 else []
        # A newer ring suffix must never jump over a still-uncommitted event
        # until the subscriber's bounded grace period has expired.
        return list(ring) if ring and (allow_gap or ring[0].seq <= cursor + 1) else []

    async def subscribe(
        self, session_id: str, *, since_seq: int = 0, from_store: bool = True
    ) -> AsyncIterator[Event]:
        """Replay the log from ``since_seq`` (exclusive), then stream live events.

        ``from_store=False`` confines replay to this process's ring: a cursor
        older than the ring starts at its floor. Live renderers use it — the
        cursor they take before a turn starts predates the resume seed, and
        the persisted history is not part of the turn they are watching.
        Subsequent lag recovery can use the store, but never predates that floor.
        Without a store, active consumers retain the count-bounded ring window.
        Missing sequences wait briefly for in-flight commits, then skip to the
        next available event. A queued copy is delivered without waiting; close
        bypasses the grace period and drains all available events.
        """
        sub = _Subscriber(self._maxsize, self._close_generation)
        sub.replay_history = from_store
        sub.cursor = (since_seq if from_store and self._replay_source is not None
                      else max(since_seq, self.floor_seq(session_id) - 1))
        subs = self._subs.setdefault(session_id, [])
        subs.append(sub)
        pending: Event | None = None
        gap_cursor = -1
        gap_started = 0.0
        try:
            while True:
                if self._replay_source is None:
                    sub.cursor = max(sub.cursor, self.floor_seq(session_id) - 1)
                # Catch up from the log first (covers replay and lag recovery).
                while sub.cursor < self.last_seq(session_id):
                    batch = await self._after(
                        session_id, sub.cursor, from_store=True
                    )
                    if not batch:
                        break
                    for event in batch:
                        if event.seq <= sub.cursor:
                            continue
                        if event.seq != sub.cursor + 1:
                            break
                        sub.cursor = event.seq
                        yield event
                if sub.cursor < self.last_seq(session_id) and self._replay_source is not None:
                    # The live queue can have the missing event even if it was
                    # never persisted (or its commit is still in flight).
                    while pending is None or pending.seq <= sub.cursor:
                        pending = None
                        if sub.queue.empty():
                            break
                        item = sub.queue.get_nowait()
                        if item is not _CLOSE:
                            pending = item
                    if pending is not None and pending.seq == sub.cursor + 1:
                        sub.cursor = pending.seq
                        yield pending
                        pending = None
                        continue
                    if gap_cursor != sub.cursor:
                        gap_cursor = sub.cursor
                        gap_started = time.monotonic()
                    closing = sub.generation < self._close_generation
                    if closing or time.monotonic() - gap_started >= REPLAY_GAP_GRACE_SECONDS:
                        # A permanent hole must not hide later durable events or
                        # a queued terminal. Closing drains without another wait.
                        available = await self._after(session_id, sub.cursor, allow_gap=True)
                        next_seq = available[0].seq if available else self.last_seq(session_id) + 1
                        if pending is not None:
                            next_seq = min(next_seq, pending.seq)
                        sub.cursor = next_seq - 1
                        continue
                    # Retry even without another publish: a commit can finish
                    # after its event has already fallen out of the byte ring.
                    await asyncio.sleep(0.05)
                    continue
                if sub.lagging:
                    # Caught up with the log; drain stale buffer and resume live.
                    while not sub.queue.empty():
                        sub.queue.get_nowait()
                    sub.lagging = False
                    continue
                if sub.generation < self._close_generation:
                    return
                item = await sub.queue.get()
                if item is _CLOSE:
                    # Drain anything published after close was signalled.
                    while sub.cursor < self.last_seq(session_id):
                        batch = await self._after(
                            session_id, sub.cursor, from_store=True
                        )
                        if not batch:
                            break
                        for event in batch:
                            if event.seq <= sub.cursor:
                                continue
                            if event.seq != sub.cursor + 1:
                                break
                            sub.cursor = event.seq
                            yield event
                    return
                event = item
                if event.seq <= sub.cursor:
                    continue  # already delivered via log catch-up
                if event.seq > sub.cursor + 1:
                    pending = event  # retain it while replay catches up
                    continue
                sub.cursor = event.seq
                yield event
        finally:
            subs.remove(sub)
            if not subs and self._subs.get(session_id) is subs:
                del self._subs[session_id]
                self._trim_log(session_id)

    async def close(self) -> None:
        """Signal the current subscribers to finish after draining the log.

        Only subscriptions open now are ended: a session that subscribes to
        the same bus later runs as normal, instead of ending as soon as it
        has replayed the log.
        """
        self._close_generation += 1
        for subs in list(self._subs.values()):
            for sub in subs:
                try:
                    sub.queue.put_nowait(_CLOSE)
                except asyncio.QueueFull:
                    sub.lagging = True
