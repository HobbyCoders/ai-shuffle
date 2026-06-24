"""
In-process fan-out of notification events to connected mobile SSE streams.

Each open ``GET /api/v1/mobile/notifications/stream`` connection subscribes a
bounded queue here; the server's notification hooks (session finished, question,
permission) publish events that are delivered to every connected stream.

This is what lets the Android app receive push notifications without Firebase
*and* without a separate UnifiedPush distributor app: the app's foreground
service simply holds the SSE connection open and raises a local notification for
each event.

Single-owner, self-hosted model: every event goes to every connected stream.
State is per-process and intentionally ephemeral — if the process restarts,
clients reconnect and resume receiving new events.
"""

import asyncio
import logging
from typing import Any, Dict, Set

logger = logging.getLogger(__name__)


class MobilePushHub:
    """Fan-out hub mapping notification events to subscriber queues."""

    def __init__(self, max_queue: int = 100) -> None:
        self._subscribers: Set[asyncio.Queue] = set()
        self._max_queue = max_queue

    def subscribe(self) -> asyncio.Queue:
        """Register a new stream and return its bounded event queue."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._max_queue)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Remove a stream's queue (safe to call more than once)."""
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def publish(self, event: Dict[str, Any]) -> None:
        """Deliver ``event`` to every connected stream.

        Never blocks on a slow consumer: if a queue is full the oldest event is
        dropped to make room for the newest, so a stalled client cannot back up
        the publisher.
        """
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except Exception:  # pragma: no cover - defensive
                    pass


# Process-wide singleton used by the SSE endpoint and the notification hooks.
mobile_push_hub = MobilePushHub()
