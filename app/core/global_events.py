"""Authenticated invalidation subscribers, shared by HTTP, tools and workers."""
import asyncio
import json
import logging
import threading
from collections import OrderedDict, deque
from uuid import uuid4

from app.core.sync_engine import ConnectionWriter

logger = logging.getLogger(__name__)
_RELAY_TYPE = "user.invalidation"
RECONCILE_SECONDS = 60


class GlobalEvents:
    def __init__(self):
        self.connections = {}
        self._source = uuid4().hex
        self._loop = None
        self._queue = None
        self._worker = None
        self._reconciler = None
        self._gateway = None
        self._ingress = threading.BoundedSemaphore(256)
        self._seen = OrderedDict()
        self._delivery_lock = threading.Lock()
        self._deliveries = {}

    def start(self):
        """Relay invalidations through the shared DB; snapshots remain authoritative."""
        if self._worker is not None and not self._worker.done():
            return
        from app.core.event_gateway import get_gateway
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=256)
        self._gateway = get_gateway()
        self._gateway.add_message_listener(_RELAY_TYPE, self._receive)
        self._worker = self._loop.create_task(self._send(), name="user-invalidation-relay")
        self._reconciler = self._loop.create_task(self._reconcile(), name="user-invalidation-reconcile")

    @staticmethod
    def _invalidation(message):
        if not isinstance(message, dict):
            return None
        kind = message.get("type")
        if kind == 'sync_reconcile':
            return {'type': kind}
        field = {"preferences_changed": "keys", "resources_changed": "resources"}.get(kind)
        if field:
            values = message.get(field)
            if isinstance(values, list) and 0 < len(values) <= 64 and all(
                isinstance(value, str) and 0 < len(value) <= 256 for value in values
            ):
                return {"type": kind, field: values}
        if kind in {"user_session_created", "user_session_updated", "user_session_deleted", "user_session_cleared", "notification"}:
            # Never put session/notification content on the cross-install bus.
            return {"type": "resources_changed", "resources": ["notifications" if kind == "notification" else "sessions"]}
        return None

    def _receive(self, envelope):
        if not isinstance(envelope, dict):
            return
        if envelope.get("source") == self._source:
            return
        event_id, kind, identity = (envelope.get(key) for key in ("id", "user_type", "user_id"))
        message = envelope.get("message")
        if (not isinstance(event_id, str) or len(event_id) != 32
                or any(c not in '0123456789abcdef' for c in event_id)
                or kind not in {"admin", "api_user", "account", "paired"}
                or (identity is not None and (not isinstance(identity, str) or not 0 < len(identity) <= 2048))
                or not isinstance(message, dict)):
            return
        message = self._invalidation(message)
        if message is None or (message['type'] == 'preferences_changed' and identity is None):
            return
        with self._delivery_lock:
            if event_id in self._seen:
                return
            self._seen[event_id] = None
            if len(self._seen) > 4096:
                self._seen.popitem(last=False)
        self._deliver_local(message, user_type=kind, user_id=identity)

    @staticmethod
    def _notify(envelope):
        from app.core.event_gateway import LISTEN_CHANNEL
        from app.db.base import get_db
        payload = json.dumps(envelope)
        if len(payload.encode("utf-8")) > 7000:
            raise ValueError("Invalidation payload exceeds notification budget")
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SET LOCAL statement_timeout = '10s'")
            cursor.execute("SELECT pg_notify(%s, %s)", (LISTEN_CHANNEL, payload))

    async def _send(self):
        from app.db.async_database import run_sync
        queue = self._queue
        while True:
            envelope = await queue.get()
            try:
                # A cancelled to_thread call does not cancel its blocking DB
                # thread. Await each write before submitting another, so an
                # outage cannot accumulate detached connection acquisitions.
                await run_sync(self._notify, envelope)
            except Exception:
                logger.warning("Cross-install invalidation delivery failed; periodic reconciliation will refresh", exc_info=True)
            finally:
                queue.task_done()

    async def _reconcile(self):
        # LISTEN/NOTIFY has no replay. Periodic authorized snapshot reads repair
        # lost notifications, listener gaps and overflow without reconnecting
        # healthy sockets or restarting the active session event stream.
        while True:
            await asyncio.sleep(RECONCILE_SECONDS)
            self._reconcile_local()

    def _reconcile_local(self):
        for kind in {connection[0] for connection in self.connections.copy().values()}:
            self._deliver_local({'type': 'sync_reconcile'}, user_type=kind)

    def _enqueue(self, envelope):
        try:
            if self._queue is not None:
                try:
                    self._queue.put_nowait(envelope)
                except asyncio.QueueFull:
                    logger.warning("Invalidation queue full; periodic reconciliation will refresh")
        finally:
            self._ingress.release()

    def register(self, websocket, user_type, user_id):
        socket_id = uuid4().hex
        loop = asyncio.get_running_loop()
        writer = ConnectionWriter(websocket, label=f"global:{socket_id}")
        self.connections[socket_id] = (user_type, user_id, loop, writer)

        async def reap():
            self.connections.pop(socket_id, None)

        writer.set_on_dead(reap)
        writer.start()
        writer.enqueue(json.dumps({"type": "sync_ready"}))
        return socket_id

    async def unregister(self, socket_id):
        connection = self.connections.pop(socket_id, None)
        with self._delivery_lock:
            self._deliveries.pop(socket_id, None)
        if connection:
            await connection[3].aclose()

    async def shutdown(self):
        if self._gateway is not None:
            self._gateway.remove_message_listener(_RELAY_TYPE, self._receive)
        self._loop = None
        tasks = [task for task in (self._worker, self._reconciler) if task is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._worker = self._queue = self._reconciler = self._gateway = None
        for socket_id in list(self.connections):
            await self.unregister(socket_id)

    def publish(self, message, *, user_type="admin", user_id=None):
        if message.get('type') == 'preferences_changed' and not user_id:
            return  # A preference invalidation always has an exact principal.
        self._deliver_local(message, user_type=user_type, user_id=user_id)
        invalidation = self._invalidation(message)
        loop = self._loop
        if invalidation is None or loop is None or not self._ingress.acquire(blocking=False):
            return
        envelope = {"type": _RELAY_TYPE, "id": uuid4().hex, "source": self._source,
                    "user_type": user_type, "user_id": user_id, "message": invalidation}
        if len(json.dumps(envelope).encode('utf-8')) > 7000:
            envelope['message'] = {'type': 'sync_reconcile'}
        try:
            loop.call_soon_threadsafe(self._enqueue, envelope)
        except RuntimeError:
            self._ingress.release()

    def _deliver_local(self, message, *, user_type="admin", user_id=None):
        # Snapshot under the GIL; registry mutations happen on subscriber loops.
        text = json.dumps(message)
        for socket_id, (kind, identity, loop, writer) in self.connections.copy().items():
            if kind != user_type or (user_id is not None and identity != user_id):
                continue
            if loop.is_closed():
                continue
            with self._delivery_lock:
                queued = self._deliveries.get(socket_id)
                if queued is not None:
                    if len(queued) >= 256:
                        queued.clear()
                        queued.append('{"type":"sync_reconcile"}')
                    elif not queued or queued[-1] != text:
                        queued.append(text)
                    continue
                self._deliveries[socket_id] = deque([text])
            try:
                # At most one pending loop callback per connection, even when
                # synchronous writers flood the loop from multiple threads.
                loop.call_soon_threadsafe(self._flush_local, socket_id)
            except RuntimeError:
                with self._delivery_lock:
                    self._deliveries.pop(socket_id, None)

    def _flush_local(self, socket_id):
        with self._delivery_lock:
            queued = self._deliveries.pop(socket_id, ())
        connection = self.connections.get(socket_id)
        if connection:
            for text in queued:
                connection[3].enqueue(text)

    def preference_changed(self, user_type, user_id, key):
        if not key.startswith("active_session:"):
            self.publish({"type": "preferences_changed", "keys": [key]},
                         user_type=user_type, user_id=user_id)

    def resources_changed(self, *resources, user_type="admin", user_id=None):
        self.publish({"type": "resources_changed", "resources": list(resources)},
                     user_type=user_type, user_id=user_id)

    def lifecycle(self, user_id, message, *, user_type=None):
        if message.get("type") not in {
            "user_session_created", "user_session_updated", "user_session_deleted",
            "user_session_cleared", "notification",
        }:
            return
        # Managed accounts and direct-paired clients never receive legacy admin
        # payloads; they still need a signal to refetch their authorized lists.
        resource = "notifications" if message["type"] == "notification" else "sessions"
        for kind in ('account', 'paired'):
            self.resources_changed(resource, user_type=kind)
        if user_type in {"admin", "api_user"}:
            self.publish(message, user_type=user_type,
                         user_id=None if user_type == "admin" else user_id)
        else:
            # Legacy callers provide an untyped ID (and deleted sessions have
            # no owner row). Never guess a role or disclose their payload.
            resource = "notifications" if message["type"] == "notification" else "sessions"
            for kind in ("admin", "api_user"):
                self.resources_changed(resource, user_type=kind)


global_events = GlobalEvents()
