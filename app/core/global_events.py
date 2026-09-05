"""Authenticated invalidation subscribers, shared by HTTP, tools and workers."""
import asyncio
import json
from uuid import uuid4

from app.core.sync_engine import ConnectionWriter


class GlobalEvents:
    def __init__(self):
        self.connections = {}

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
        if connection:
            await connection[3].aclose()

    async def shutdown(self):
        for socket_id in list(self.connections):
            await self.unregister(socket_id)

    def publish(self, message, *, user_type="admin", user_id=None):
        # Snapshot under the GIL; registry mutations happen on subscriber loops.
        for kind, identity, loop, writer in list(self.connections.copy().values()):
            if kind != user_type or (user_id is not None and identity != user_id):
                continue
            if loop.is_closed():
                continue
            try:
                loop.call_soon_threadsafe(writer.enqueue, json.dumps(message))
            except RuntimeError:
                pass  # The subscriber's event loop has already shut down.

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
