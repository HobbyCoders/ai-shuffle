"""EventBus — seq-numbered, replayable fan-out.

Semantics:
- ``publish`` assigns the next per-session ``seq`` and appends to the session
  log, then fans out to subscribers.
- Subscribers get bounded queues. A slow subscriber never blocks the engine:
  when its queue overflows, the oldest buffered event is dropped and the
  subscriber transparently re-syncs from the log (it owns a cursor, so nothing
  is ever lost — only buffered late).
- ``subscribe(since_seq=N)`` replays the log from N before streaming live —
  reconnect support for WS bridges and TUIs.

The in-memory log is the source for replay within a process lifetime; durable
replay across restarts belongs to the SessionStore, which is just another
subscriber (with an unbounded queue — it must never drop).
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from dataclasses import replace

from fluid.events.model import Event

_CLOSE = object()


class _Subscriber:
    def __init__(self, maxsize: int) -> None:
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize) if maxsize else asyncio.Queue()
        self.cursor: int = 0  # seq of the last event delivered
        self.lagging: bool = False


class EventBus:
    def __init__(self, *, subscriber_queue_size: int = 1024) -> None:
        self._logs: dict[str, list[Event]] = defaultdict(list)
        self._subs: dict[str, list[_Subscriber]] = defaultdict(list)
        self._maxsize = subscriber_queue_size
        self._closed = False

    def log(self, session_id: str) -> list[Event]:
        return self._logs[session_id]

    async def publish(self, event: Event) -> Event:
        """Assign seq and ts, append to the log, fan out. Returns the sequenced event."""
        log = self._logs[event.session_id]
        # Stamp both facts about the event's place in the stream together: its
        # order (seq) and when it happened (ts, epoch seconds). An emitter that
        # already set ts — a replayed or reconstructed event carrying its
        # original time — keeps it; only an unstamped 0.0 is filled in.
        sequenced = replace(
            event,
            seq=len(log) + 1,
            ts=event.ts or time.time(),
        )
        log.append(sequenced)
        for sub in self._subs[event.session_id]:
            if sub.lagging:
                continue  # will re-sync from the log when it catches up
            try:
                sub.queue.put_nowait(sequenced)
            except asyncio.QueueFull:
                sub.lagging = True  # stop buffering; iterator falls back to the log
        return sequenced

    async def subscribe(
        self, session_id: str, *, since_seq: int = 0
    ) -> AsyncIterator[Event]:
        """Replay the log from ``since_seq`` (exclusive), then stream live events."""
        sub = _Subscriber(self._maxsize)
        sub.cursor = since_seq
        self._subs[session_id].append(sub)
        log = self._logs[session_id]
        try:
            while True:
                # Catch up from the log first (covers replay and lag recovery).
                while sub.cursor < len(log):
                    event = log[sub.cursor]
                    sub.cursor = event.seq
                    yield event
                if sub.lagging:
                    # Caught up with the log; drain stale buffer and resume live.
                    while not sub.queue.empty():
                        sub.queue.get_nowait()
                    sub.lagging = False
                    continue
                if self._closed:
                    return
                item = await sub.queue.get()
                if item is _CLOSE:
                    # Drain anything published after close was signalled.
                    while sub.cursor < len(log):
                        event = log[sub.cursor]
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
            self._subs[session_id].remove(sub)

    async def close(self) -> None:
        """Signal all subscribers to finish after draining the log."""
        self._closed = True
        for subs in self._subs.values():
            for sub in subs:
                try:
                    sub.queue.put_nowait(_CLOSE)
                except asyncio.QueueFull:
                    sub.lagging = True
