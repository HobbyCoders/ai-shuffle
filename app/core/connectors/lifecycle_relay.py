"""Bounded connector-owner delivery over the existing PG notification gateway.

Like local lifecycle delivery this is best effort, not a durable outbox. An
offline listener or process restart can lose events. No peer HTTP address is
required; all peers already share the trusted database and LISTEN connection.
Only connector notifications are relayed, never webhook/automation dispatch.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable

from app.db.base import get_db

logger = logging.getLogger(__name__)
MESSAGE_TYPE = "connector.lifecycle"
EVENT_TYPES = frozenset({"session.started", "session.complete", "session.error", "permission.requested", "input.needed"})
_FIELDS = frozenset({
    "event_id", "session_id", "profile_id", "title", "turn_id", "interaction_id",
    "request_id", "source", "total_cost", "input_tokens", "output_tokens",
    "duration_seconds", "error_message",
})
MAX_PAYLOAD_BYTES = 7000
MAX_SEEN = 4096
SEEN_TTL_SECONDS = 900


def publish(connector_id: str, runner_id: str, event_type: str, data: dict) -> None:
    """Publish a small owner-addressed notification after its transaction commits."""
    if (event_type not in EVENT_TYPES
            or any(not isinstance(value, str) or not value for value in (
                connector_id, runner_id, data.get("event_id"), data.get("session_id")))):
        raise ValueError("Connector relay requires a lifecycle event and stable event/session IDs")
    safe = {}
    for key in _FIELDS:
        value = data.get(key)
        if value is None or isinstance(value, (str, int, float, bool)):
            safe[key] = value
    # Descriptive text is optional; identifiers are never silently truncated.
    for key in ("title", "error_message"):
        if isinstance(safe.get(key), str):
            safe[key] = safe[key][:512]
    message = {"type": MESSAGE_TYPE, "runner_id": runner_id, "connector_id": connector_id,
               "event_type": event_type, "data": safe}
    payload = json.dumps(message, ensure_ascii=False, allow_nan=False)
    if len(payload.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValueError("Connector lifecycle notification exceeds its bounded payload size")
    from app.core.event_gateway import LISTEN_CHANNEL
    with get_db() as conn:
        conn.cursor().execute("SELECT pg_notify(%s, %s)", (LISTEN_CHANNEL, payload))


class LifecycleRelay:
    """Receive on the gateway thread; deliver serially on one bounded worker."""

    def __init__(self, runner_id: str, deliver: Callable[[str, str, dict], Awaitable[None]]):
        self.runner_id = runner_id
        self.deliver = deliver
        self._queue: asyncio.Queue | None = None
        self._worker: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._gateway = None
        self._pending_callbacks = threading.BoundedSemaphore(256)
        self._seen: OrderedDict[tuple[str, str], float] = OrderedDict()

    def start(self) -> None:
        if self._worker is not None and not self._worker.done():
            return
        from app.core.event_gateway import get_gateway
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=256)
        self._gateway = get_gateway()
        self._gateway.add_message_listener(MESSAGE_TYPE, self._on_message)
        self._worker = self._loop.create_task(self._drain(), name="connector-lifecycle-relay")

    async def stop(self) -> None:
        if self._gateway is not None:
            self._gateway.remove_message_listener(MESSAGE_TYPE, self._on_message)
        self._loop = None
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
        self._queue = None

    def _on_message(self, message: dict) -> None:
        # Called on the existing gateway thread: no DB, connector I/O, or waits.
        loop = self._loop
        if message.get("runner_id") != self.runner_id or loop is None:
            return
        # Also bound callbacks waiting for the asyncio loop, before they reach
        # the worker queue. A busy loop must not permit unbounded thread ingress.
        if not self._pending_callbacks.acquire(blocking=False):
            logger.warning("Connector lifecycle relay ingress full; dropped notification")
            return
        try:
            loop.call_soon_threadsafe(self._accept_message, message)
        except RuntimeError:
            self._pending_callbacks.release()  # Loop closed during shutdown.

    def _accept_message(self, message: dict) -> None:
        try:
            self._enqueue(message)
        finally:
            self._pending_callbacks.release()

    def _enqueue(self, message: dict) -> None:
        if self._queue is None:
            return
        data = message.get("data")
        connector_id = message.get("connector_id")
        event_type = message.get("event_type")
        if (not isinstance(data, dict) or not isinstance(connector_id, str)
                or not connector_id or not isinstance(event_type, str)
                or event_type not in EVENT_TYPES or not isinstance(data.get("event_id"), str)
                or not data.get("event_id") or not isinstance(data.get("session_id"), str)
                or not data.get("session_id")):
            logger.warning("Discarded malformed connector lifecycle notification")
            return
        now = time.monotonic()
        while self._seen and next(iter(self._seen.values())) < now - SEEN_TTL_SECONDS:
            self._seen.popitem(last=False)
        key = (connector_id, data["event_id"])
        if key in self._seen:
            return
        try:
            self._queue.put_nowait((connector_id, event_type, data))
        except asyncio.QueueFull:
            logger.warning("Connector lifecycle relay queue full; dropped notification")
            return
        # Mark accepted messages before delivery; an uncertain send is not
        # retried and must not duplicate a notification on a repeated NOTIFY.
        self._seen[key] = now
        if len(self._seen) > MAX_SEEN:
            self._seen.popitem(last=False)

    async def _drain(self) -> None:
        queue = self._queue
        while True:
            connector_id, event_type, data = await queue.get()
            try:
                async with asyncio.timeout(20):
                    await self.deliver(connector_id, event_type, data)
            except Exception:
                logger.warning("Relayed connector lifecycle delivery failed", exc_info=True)
            finally:
                queue.task_done()
