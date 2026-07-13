"""
Real-time synchronization engine for cross-device chat sync.

Manages WebSocket connections and broadcasts events to all connected devices
watching the same session.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


@dataclass
class SyncEvent:
    """Represents a synchronization event to broadcast"""
    event_type: str  # 'message_added', 'queue_updated', 'session_updated', 'session_rewound', ...
    session_id: str
    data: Dict[str, Any]
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    source_device_id: Optional[str] = None  # Device that originated the event

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_type": self.event_type,
            "session_id": self.session_id,
            "data": self.data,
            "timestamp": self.timestamp.isoformat(),
            "source_device_id": self.source_device_id
        }


@dataclass
class DeviceConnection:
    """Represents a connected device watching a session"""
    device_id: str
    session_id: str
    websocket: WebSocket
    connected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_activity: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    async def send_event(self, event: SyncEvent) -> bool:
        """Send an event to this device. Returns False if send failed."""
        try:
            await self.websocket.send_json(event.to_dict())
            self.last_activity = datetime.now(timezone.utc)
            return True
        except Exception as e:
            logger.warning(f"Failed to send event to device {self.device_id}: {e}")
            return False


@dataclass
class UserDeviceConnection:
    """Represents a connected device for a user (for tab sync)"""
    device_id: str
    user_id: str
    websocket: WebSocket
    connected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_activity: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    async def send_json(self, data: Dict[str, Any]) -> bool:
        """Send JSON data to this device. Returns False if send failed."""
        try:
            await self.websocket.send_json(data)
            self.last_activity = datetime.now(timezone.utc)
            return True
        except Exception as e:
            logger.warning(f"Failed to send to device {self.device_id}: {e}")
            return False


class SyncEngine:
    """
    Manages real-time synchronization between devices.

    Key features:
    - Track device connections per session
    - Broadcast non-render events to all devices watching a session
    - Exclude source device from broadcasts (to avoid echo)
    - Enforce single active device per session (session locking)

    V2: this engine carries NO streaming state. Render frames ride the
    session event feed (SSE); busy truth is the durable runs table
    (``app.core.streaming_status``).
    """

    def __init__(self):
        # session_id -> set of DeviceConnection
        self._connections: Dict[str, Dict[str, DeviceConnection]] = {}
        # Lock for thread-safe connection management
        self._lock = asyncio.Lock()
        # Session locks: session_id -> device_id that owns the lock
        self._session_locks: Dict[str, str] = {}
        # User connections for tab sync: user_id -> {device_id -> UserDeviceConnection}
        self._user_connections: Dict[str, Dict[str, UserDeviceConnection]] = {}
        # Lock for user connection management
        self._user_lock = asyncio.Lock()

    async def register_device(
        self,
        device_id: str,
        session_id: str,
        websocket: WebSocket
    ) -> DeviceConnection:
        """Register a device to watch a session"""
        async with self._lock:
            if session_id not in self._connections:
                self._connections[session_id] = {}

            # Close existing connection for this device if any
            if device_id in self._connections[session_id]:
                old_conn = self._connections[session_id][device_id]
                try:
                    await old_conn.websocket.close()
                except Exception:
                    pass

            connection = DeviceConnection(
                device_id=device_id,
                session_id=session_id,
                websocket=websocket
            )
            self._connections[session_id][device_id] = connection

            logger.info(f"Device {device_id} registered to watch session {session_id}")
            return connection

    async def unregister_device(self, device_id: str, session_id: str, websocket: Optional[WebSocket] = None):
        """Unregister a device from watching a session.

        Args:
            device_id: The device ID to unregister
            session_id: The session ID to unregister from
            websocket: Optional - if provided, only unregister if the current connection
                      uses this exact websocket. This prevents race conditions where
                      a new connection is established before cleanup of the old one.
        """
        async with self._lock:
            if session_id in self._connections:
                if device_id in self._connections[session_id]:
                    # If websocket is provided, only unregister if it matches
                    if websocket is not None:
                        current_conn = self._connections[session_id][device_id]
                        if current_conn.websocket is not websocket:
                            logger.debug(f"Device {device_id} has new connection, skipping unregister for old websocket")
                            return

                    del self._connections[session_id][device_id]
                    logger.info(f"Device {device_id} unregistered from session {session_id}")

                # Clean up empty session entries
                if not self._connections[session_id]:
                    del self._connections[session_id]

    def get_connected_devices(self, session_id: str) -> List[str]:
        """Get list of device IDs watching a session"""
        if session_id not in self._connections:
            return []
        return list(self._connections[session_id].keys())

    def get_device_count(self, session_id: str) -> int:
        """Get number of devices watching a session"""
        return len(self._connections.get(session_id, {}))

    def is_device_watching_session(self, device_id: str, session_id: str) -> bool:
        """Check if a specific device is already watching a session.

        Used to prevent the same device from opening the same session in multiple tabs.
        """
        return device_id in self._connections.get(session_id, {})

    async def sweep_stale_maps(self) -> int:
        """Sweep orphaned session locks.

        A lock becomes orphaned when its owning device disconnects abnormally
        without releasing it: with no device left connected, nothing can ever
        release the lock. Sessions with an ACTIVE run (durable runs table) are
        spared — their device may reconnect mid-turn.

        Returns:
            Number of orphaned locks removed.
        """
        from app.core import streaming_status

        async with self._lock:
            candidates = [
                sid for sid in self._session_locks
                if sid not in self._connections
            ]
        stale_lock_ids = [
            sid for sid in candidates
            if not await asyncio.to_thread(streaming_status.has_active_run, sid)
        ]

        removed = 0
        async with self._lock:
            for sid in stale_lock_ids:
                # Re-check: a device may have (re)connected while we were
                # checking the runs table off-lock.
                if sid in self._session_locks and sid not in self._connections:
                    del self._session_locks[sid]
                    removed += 1
        if removed:
            logger.info(
                f"Swept {removed} orphaned session lock(s). "
                f"Remaining: session_locks={len(self._session_locks)}"
            )
        return removed

    async def acquire_session_lock(self, session_id: str, device_id: str) -> tuple[bool, Optional[str]]:
        """Acquire exclusive lock on a session for a device.

        Only one device can hold the lock for a session at a time.
        If the same device already holds the lock, it succeeds (idempotent).

        Args:
            session_id: The session to lock
            device_id: The device requesting the lock

        Returns:
            Tuple of (success, current_owner_device_id)
            - (True, None) if lock acquired successfully
            - (False, owner_device_id) if another device holds the lock
        """
        async with self._lock:
            if session_id in self._session_locks:
                current_owner = self._session_locks[session_id]
                if current_owner != device_id:
                    logger.debug(f"Session {session_id[:8]} lock denied for device {device_id[:8]}, owned by {current_owner[:8]}")
                    return False, current_owner
                # Same device already owns it
                return True, None

            self._session_locks[session_id] = device_id
            logger.debug(f"Session {session_id[:8]} lock acquired by device {device_id[:8]}")
            return True, None

    async def release_session_lock(self, session_id: str, device_id: str) -> bool:
        """Release the lock on a session.

        Only the device that holds the lock can release it.

        Args:
            session_id: The session to unlock
            device_id: The device releasing the lock

        Returns:
            True if lock was released, False if device didn't hold the lock
        """
        async with self._lock:
            if session_id not in self._session_locks:
                return True  # No lock exists, consider it released

            if self._session_locks[session_id] != device_id:
                logger.warning(f"Device {device_id[:8]} tried to release lock on session {session_id[:8]} owned by {self._session_locks[session_id][:8]}")
                return False

            del self._session_locks[session_id]
            logger.debug(f"Session {session_id[:8]} lock released by device {device_id[:8]}")
            return True

    def get_session_lock_owner(self, session_id: str) -> Optional[str]:
        """Get the device ID that currently holds the lock on a session."""
        return self._session_locks.get(session_id)

    def is_session_locked(self, session_id: str) -> bool:
        """Check if a session is currently locked by any device."""
        return session_id in self._session_locks

    async def broadcast_event(
        self,
        event: SyncEvent,
        exclude_device_id: Optional[str] = None
    ):
        """
        Broadcast an event to all devices watching the session.

        Args:
            event: The event to broadcast
            exclude_device_id: Device ID to exclude (usually the source device)
        """
        import json

        session_id = event.session_id

        # Snapshot connections under lock to avoid TOCTOU race with
        # register_device/unregister_device which modify _connections under lock.
        async with self._lock:
            if session_id not in self._connections:
                logger.debug(f"No connections for session {session_id}, skipping broadcast")
                return

            # Get connections to broadcast to
            connections = list(self._connections[session_id].values())
        logger.debug(f"Broadcasting {event.event_type} to {len(connections)} devices for session {session_id[:8]}...")

        # Pre-serialize once so a serialization error doesn't disconnect all devices.
        # default=str: event payloads can include PostgreSQL datetime values (e.g. a
        # queued message's created_at). Without it those raise here and the event is
        # dropped for every device — e.g. queue_updated never reaching the client.
        try:
            event_text = json.dumps(event.to_dict(), default=str)
        except (TypeError, ValueError) as e:
            logger.error(f"Failed to serialize event {event.event_type} for session {session_id[:8]}: {e}")
            return

        # Send to all devices except the excluded one
        failed_devices = []
        for conn in connections:
            if exclude_device_id and conn.device_id == exclude_device_id:
                logger.debug(f"Skipping source device {conn.device_id[:8]}")
                continue

            logger.debug(f"Sending {event.event_type} to device {conn.device_id[:8]}")
            try:
                await conn.websocket.send_text(event_text)
                conn.last_activity = datetime.now(timezone.utc)
            except Exception as e:
                logger.warning(f"Failed to send event to device {conn.device_id}: {e}")
                failed_devices.append(conn.device_id)

        # Clean up failed connections (only actual send failures, not serialization)
        if failed_devices:
            async with self._lock:
                for device_id in failed_devices:
                    if session_id in self._connections:
                        self._connections[session_id].pop(device_id, None)

    async def broadcast_message_added(
        self,
        session_id: str,
        message: Dict[str, Any],
        source_device_id: Optional[str] = None
    ):
        """Broadcast that a new message was added (used for user messages)"""
        event = SyncEvent(
            event_type="message_added",
            session_id=session_id,
            data={"message": message},
            source_device_id=source_device_id
        )
        await self.broadcast_event(event, exclude_device_id=source_device_id)

    async def broadcast_queue_updated(
        self,
        session_id: str,
        queued_messages: list,
        source_device_id: Optional[str] = None
    ):
        """Broadcast updated queue state to all devices watching this session."""
        event = SyncEvent(
            event_type="queue_updated",
            session_id=session_id,
            data={"queued_messages": queued_messages},
            source_device_id=source_device_id
        )
        # Send to ALL devices (including source) so everyone is in sync
        await self.broadcast_event(event, exclude_device_id=None)

    async def broadcast_session_updated(
        self,
        session_id: str,
        updates: Dict[str, Any],
        source_device_id: Optional[str] = None
    ):
        """Broadcast session metadata updates"""
        event = SyncEvent(
            event_type="session_updated",
            session_id=session_id,
            data={"updates": updates},
            source_device_id=source_device_id
        )
        await self.broadcast_event(event, exclude_device_id=source_device_id)

    async def broadcast_session_rewound(
        self,
        session_id: str,
        target_uuid: str,
        messages_removed: int,
        source_device_id: Optional[str] = None
    ):
        """Broadcast that a session was rewound (messages removed)"""
        event = SyncEvent(
            event_type="session_rewound",
            session_id=session_id,
            data={
                "target_uuid": target_uuid,
                "messages_removed": messages_removed
            },
            source_device_id=source_device_id
        )
        # Don't exclude any device - all devices need to know about rewind
        await self.broadcast_event(event, exclude_device_id=None)

    async def broadcast_session_opened(
        self,
        session_id: str,
        device_id: str,
        is_new: bool = False
    ):
        """Broadcast that a device opened/resumed a session"""
        event = SyncEvent(
            event_type="session_opened",
            session_id=session_id,
            data={
                "device_id": device_id,
                "is_new": is_new
            },
            source_device_id=device_id
        )
        # Notify all devices in session except the one that opened it
        await self.broadcast_event(event, exclude_device_id=device_id)

    async def broadcast_session_closed(
        self,
        session_id: str,
        device_id: str
    ):
        """Broadcast that a device closed a session"""
        event = SyncEvent(
            event_type="session_closed",
            session_id=session_id,
            data={
                "device_id": device_id
            },
            source_device_id=device_id
        )
        # Notify all devices in session except the one that closed it
        await self.broadcast_event(event, exclude_device_id=device_id)

    async def get_session_state(self, session_id: str) -> Dict[str, Any]:
        """Get current state of a session for new device joining.

        ``is_streaming`` comes from the durable runs table (V2 busy truth).
        """
        from app.core import streaming_status
        return {
            "session_id": session_id,
            "is_streaming": await asyncio.to_thread(
                streaming_status.has_active_run, session_id
            ),
            "connected_devices": self.get_device_count(session_id)
        }

    # ============= User-level connection management for tab sync =============

    async def register_user_device(
        self,
        user_id: str,
        device_id: str,
        websocket: WebSocket
    ) -> UserDeviceConnection:
        """Register a device for a user (for tab sync across devices)."""
        async with self._user_lock:
            if user_id not in self._user_connections:
                self._user_connections[user_id] = {}

            # Note: If device already registered, it's the same websocket being re-registered
            # Just overwrite the entry - no need to close

            connection = UserDeviceConnection(
                device_id=device_id,
                user_id=user_id,
                websocket=websocket
            )
            self._user_connections[user_id][device_id] = connection

            logger.debug(f"User device registered: user={user_id[:8]}... device={device_id[:8]}...")
            return connection

    async def unregister_user_device(
        self,
        user_id: str,
        device_id: str,
        websocket: Optional[WebSocket] = None
    ):
        """Unregister a device from user connection tracking."""
        async with self._user_lock:
            if user_id in self._user_connections:
                if device_id in self._user_connections[user_id]:
                    # If websocket is provided, only unregister if it matches
                    if websocket is not None:
                        current_conn = self._user_connections[user_id][device_id]
                        if current_conn.websocket is not websocket:
                            logger.debug(f"User device {device_id[:8]} has new connection, skipping unregister")
                            return

                    del self._user_connections[user_id][device_id]
                    logger.debug(f"User device unregistered: user={user_id[:8]}... device={device_id[:8]}...")

                # Clean up empty user entries
                if not self._user_connections[user_id]:
                    del self._user_connections[user_id]

    def get_user_device_count(self, user_id: str) -> int:
        """Get number of devices connected for a user."""
        return len(self._user_connections.get(user_id, {}))

    async def broadcast_tabs_updated(
        self,
        user_id: str,
        tabs_data: Dict[str, Any],
        source_device_id: Optional[str] = None
    ):
        """Broadcast tab state update to all user's devices except the source.

        Args:
            user_id: The user whose tabs changed
            tabs_data: The tab state to broadcast (matches PersistedTabsState)
            source_device_id: Device that made the change (excluded from broadcast)
        """
        # Take a snapshot of connections under the lock, then release before
        # performing potentially-slow WebSocket sends.
        async with self._user_lock:
            user_devices = self._user_connections.get(user_id, {})
            if not user_devices:
                return
            # Snapshot: list of (device_id, connection) pairs
            targets = [
                (did, conn) for did, conn in user_devices.items()
                if did != source_device_id
            ]

        if not targets:
            return

        message = {
            "type": "tabs_sync",
            "tabs": tabs_data.get("tabs", []),
            "activeTabId": tabs_data.get("activeTabId"),
            "source_device_id": source_device_id
        }

        failed_devices = []
        for device_id, conn in targets:
            success = await conn.send_json(message)
            if not success:
                failed_devices.append(device_id)

        # Clean up failed connections under the lock
        if failed_devices:
            async with self._user_lock:
                for device_id in failed_devices:
                    if device_id in self._user_connections.get(user_id, {}):
                        del self._user_connections[user_id][device_id]

                if user_id in self._user_connections and not self._user_connections[user_id]:
                    del self._user_connections[user_id]

        sent_count = len(targets) - len(failed_devices)
        if sent_count > 0:
            logger.debug(f"Broadcast tabs_sync to {sent_count} devices for user {user_id[:8]}...")

    async def _broadcast_user_message(
        self,
        user_id: str,
        message: Dict[str, Any],
        source_device_id: Optional[str] = None
    ):
        """Send a message to all devices for a user, excluding source device.

        Private helper used by user-level broadcast methods.

        Takes a snapshot of connections under the lock, then releases before
        performing potentially-slow WebSocket sends (same pattern as
        broadcast_tabs_updated).
        """
        # Snapshot connections under lock, then release before I/O
        async with self._user_lock:
            user_devices = self._user_connections.get(user_id, {})
            if not user_devices:
                return
            targets = [
                (did, conn) for did, conn in user_devices.items()
                if did != source_device_id
            ]

        if not targets:
            return

        failed_devices = []
        for device_id, conn in targets:
            success = await conn.send_json(message)
            if not success:
                failed_devices.append(device_id)

        # Clean up failed connections under the lock
        if failed_devices:
            async with self._user_lock:
                for device_id in failed_devices:
                    if device_id in self._user_connections.get(user_id, {}):
                        del self._user_connections[user_id][device_id]
                if user_id in self._user_connections and not self._user_connections[user_id]:
                    del self._user_connections[user_id]

    async def broadcast_session_attention(
        self,
        user_id: str,
        session_id: str,
        count: int,
        kind: str = "question",
        source_device_id: Optional[str] = None
    ):
        """Notify all of a user's devices that a session is (or is no longer)
        awaiting user input.

        A running session that is parked on an AskUserQuestion / permission
        prompt is indistinguishable from a busy one on every device except the
        tab that owns the query (only that socket receives the per-request
        event). Fanning a lightweight ``session_attention`` event out to every
        device lets the sessions board surface a "needs you" indicator no matter
        which tab the user is looking at.

        Args:
            user_id: The user whose session needs (or no longer needs) input.
            session_id: The session awaiting input.
            count: Number of pending items; 0 clears the indicator.
            kind: "question" or "permission" (for future per-kind styling).
            source_device_id: Optional device to exclude (e.g. when that device
                already tracks the prompt through its per-socket event).
        """
        await self._broadcast_user_message(
            user_id,
            {
                "type": "session_attention",
                "session_id": session_id,
                "kind": kind,
                "count": count,
            },
            source_device_id=source_device_id,
        )

    async def broadcast_user_question(
        self,
        user_id: str,
        payload: Dict[str, Any],
        source_device_id: Optional[str] = None
    ):
        """Fan an AskUserQuestion payload out to every one of a user's devices.

        The per-query origin socket only reaches the tab that owns the query —
        which is nobody on a headless turn, a socket that died on reconnect, or a
        query driven from another device. Fanning the SAME ``user_question``
        payload to every device (the frontend dedupes by request_id) makes the
        modal reach a live viewer in those cases. Mirrors
        ``broadcast_session_attention``; the payload already carries its own
        ``type``/``session_id`` so it routes correctly on the client.
        """
        await self._broadcast_user_message(
            user_id,
            payload,
            source_device_id=source_device_id,
        )

    async def broadcast_user_session_created(
        self,
        user_id: str,
        session_id: str,
        source_device_id: Optional[str] = None
    ):
        """Broadcast that a new session was created to all user's devices.

        This allows other devices to update their session list without polling.
        """
        await self._broadcast_user_message(
            user_id=user_id,
            message={"type": "user_session_created", "session_id": session_id},
            source_device_id=source_device_id
        )

    async def broadcast_notification(
        self,
        user_id: str,
        notification: Dict[str, Any],
        source_device_id: Optional[str] = None,
    ):
        """Push a unified notification to all of a user's connected devices.

        The single live-delivery channel for the notification system: server code
        funnels through ``app/services/notification_service.emit_notification``,
        which persists the row then calls this to fan it out as a ``notification``
        WS event. User-scoped (not session/device-scoped) so a notification
        reaches every device the user has open, even ones not watching the
        originating session.
        """
        await self._broadcast_user_message(
            user_id=user_id,
            message={"type": "notification", "notification": notification},
            source_device_id=source_device_id,
        )

    async def broadcast_usage_snapshot(
        self,
        snapshot: Dict[str, Any],
    ):
        """Fan a Claude usage-limits snapshot out to EVERY connected device.

        Cluster-level (not session- or user-scoped): the 5-hour Claude window is
        PER-ACCOUNT, and on a shared database two installs may run different
        Claude accounts, so the numbers are NOT identical everywhere. The
        broadcast carries the enriched cluster shape — the local install's legacy
        top-level ``windows``/``fetched_at``/``available``/``generation`` plus a
        ``snapshots`` list of every install's per-account snapshot and
        ``self_instance_id`` — as a ``usage_snapshot`` WS event, so each client
        can render the correct reset time for offloaded sessions.
        """
        message = {"type": "usage_snapshot", **snapshot}

        # Snapshot every user's device list under the lock, then release before I/O.
        async with self._user_lock:
            targets = [
                (user_id, did, conn)
                for user_id, devices in self._user_connections.items()
                for did, conn in devices.items()
            ]

        if not targets:
            return

        failed: List[tuple] = []
        for user_id, device_id, conn in targets:
            success = await conn.send_json(message)
            if not success:
                failed.append((user_id, device_id))

        if failed:
            async with self._user_lock:
                for user_id, device_id in failed:
                    if device_id in self._user_connections.get(user_id, {}):
                        del self._user_connections[user_id][device_id]
                    if user_id in self._user_connections and not self._user_connections[user_id]:
                        del self._user_connections[user_id]

    async def broadcast_branch_changed(
        self,
        user_id: str,
        project_id: str,
        branch_name: str
    ):
        """Broadcast that a branch was checked out to all user's devices.

        Allows other devices viewing the same project to refresh their git state.
        """
        await self._broadcast_user_message(
            user_id=user_id,
            message={
                "type": "user_branch_changed",
                "project_id": project_id,
                "branch_name": branch_name,
            }
        )


# Global sync engine instance
sync_engine = SyncEngine()
