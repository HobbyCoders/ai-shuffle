"""EventBus — seq-numbered, replayable fan-out.

Semantics:
- ``publish`` assigns the next per-session ``seq`` and appends to the session
  log, then fans out to subscribers. ``seq`` is monotonic for the life of the
  session, not the process: an engine that resumes a persisted session calls
  ``seed(session_id, last_seq)`` first so numbering continues after the rows
  already on disk instead of restarting at 1 and colliding with them.
- The in-process log is a ring of the newest ``log_ring_size`` events per
  session. Everything older is the store's job: ``subscribe(since_seq=N)``
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
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace

from fluid.events.model import Event

_CLOSE = object()

# Newest events kept in memory per session. A TUI reconnecting after a longer
# gap replays the rest from the store.
LOG_RING_SIZE = 2048

ReplaySource = Callable[[str, int], Awaitable[list[Event]]]


class _Subscriber:
    def __init__(self, maxsize: int, generation: int = 0) -> None:
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize) if maxsize else asyncio.Queue()
        self.cursor: int = 0  # seq of the last event delivered
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
        replay_source: ReplaySource | None = None,
    ) -> None:
        self._logs: dict[str, deque[Event]] = {}
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

    # ─── configuration ───────────────────────────────────────────────────
    def seed(self, session_id: str, last_seq: int) -> None:
        """Continue numbering after ``last_seq`` (the persisted maximum).

        Never moves backwards: seeding below what this process already
        published is a no-op, so a late or repeated seed cannot produce
        duplicate seqs.
        """
        if int(last_seq) > self._last_seq.get(session_id, 0):
            self._last_seq[session_id] = int(last_seq)

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
            ring = self._logs[sid] = deque(maxlen=self._ring_size)
        ring.append(sequenced)
        for sub in self._subs.get(sid, ()):
            if sub.lagging:
                continue  # will re-sync from the log when it catches up
            try:
                sub.queue.put_nowait(sequenced)
            except asyncio.QueueFull:
                sub.lagging = True  # stop buffering; iterator falls back to the log
        return sequenced

    async def _after(
        self, session_id: str, cursor: int, *, from_store: bool = True
    ) -> list[Event]:
        """Events with ``seq > cursor``, oldest first, from the ring or the store.

        Empty only when nothing after ``cursor`` is reachable anywhere.
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
            return [e for e in ring if e.seq > cursor]
        # Older than the ring: the store has the gap.
        if from_store and self._replay_source is not None:
            try:
                stored = await self._replay_source(session_id, cursor)
            except Exception:  # a store that cannot replay: fall through to the ring
                stored = []
            stored = sorted((e for e in stored if e.seq > cursor), key=lambda e: e.seq)
            if stored:
                return stored
        return list(ring) if ring else []

    async def subscribe(
        self, session_id: str, *, since_seq: int = 0, from_store: bool = True
    ) -> AsyncIterator[Event]:
        """Replay the log from ``since_seq`` (exclusive), then stream live events.

        ``from_store=False`` confines replay to this process's ring: a cursor
        older than the ring starts at its floor. Live renderers use it — the
        cursor they take before a turn starts predates the resume seed, and
        the persisted history is not part of the turn they are watching.
        """
        sub = _Subscriber(self._maxsize, self._close_generation)
        sub.cursor = since_seq
        subs = self._subs.setdefault(session_id, [])
        subs.append(sub)
        try:
            while True:
                # Catch up from the log first (covers replay and lag recovery).
                while sub.cursor < self.last_seq(session_id):
                    batch = await self._after(
                        session_id, sub.cursor, from_store=from_store
                    )
                    if not batch:
                        break
                    for event in batch:
                        if event.seq <= sub.cursor:
                            continue
                        sub.cursor = event.seq
                        yield event
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
                            session_id, sub.cursor, from_store=from_store
                        )
                        if not batch:
                            break
                        for event in batch:
                            if event.seq <= sub.cursor:
                                continue
                            sub.cursor = event.seq
                            yield event
                    return
                event = item
                if event.seq <= sub.cursor:
                    continue  # already delivered via log catch-up
                if event.seq > sub.cursor + 1:
                    continue  # gap: loop back to log catch-up
                sub.cursor = event.seq
                yield event
        finally:
            subs.remove(sub)
            if not subs and self._subs.get(session_id) is subs:
                del self._subs[session_id]

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
