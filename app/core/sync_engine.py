"""
Real-time synchronization engine for cross-device chat sync.

Manages WebSocket connections and broadcasts events to all connected devices
watching the same session.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


@dataclass
class SyncEvent:
    """Represents a synchronization event to broadcast"""
    event_type: str  # 'message_added', 'message_chunk', 'stream_start', 'stream_end', 'session_updated'
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
    - Broadcast events to all devices watching a session
    - Support for streaming events (message chunks)
    - Exclude source device from broadcasts (to avoid echo)
    - Enforce single active device per session (session locking)

    Late-joining devices get streaming state from LiveSessionStore, not from
    this engine. SyncEngine only tracks whether a session is streaming and
    broadcasts real-time events to already-connected devices.
    """

    def __init__(self):
        # session_id -> set of DeviceConnection
        self._connections: Dict[str, Dict[str, DeviceConnection]] = {}
        # Lock for thread-safe connection management
        self._lock = asyncio.Lock()
        # Track streaming state per session
        self._streaming_sessions: Set[str] = set()
        # Session locks: session_id -> device_id that owns the lock
        self._session_locks: Dict[str, str] = {}
        # User connections for tab sync: user_id -> {device_id -> UserDeviceConnection}
        self._user_connections: Dict[str, Dict[str, UserDeviceConnection]] = {}
        # Lock for user connection management
        self._user_lock = asyncio.Lock()
        # Map streaming session_id -> user_id for user-level broadcasts
        self._session_user_map: Dict[str, str] = {}
        # Map streaming session_id -> project_id for workspace-level streaming awareness
        self._session_project_map: Dict[str, Optional[str]] = {}
        # Map streaming session_id -> worktree_id for worktree-aware streaming locks
        self._session_worktree_map: Dict[str, Optional[str]] = {}

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

            logger.debug(f"Device {device_id} registered to watch session {session_id}")
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
                    logger.debug(f"Device {device_id} unregistered from session {session_id}")

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

    def is_session_streaming(self, session_id: str) -> bool:
        """Check if a session is currently streaming"""
        return session_id in self._streaming_sessions

    async def clear_streaming_state(self, session_id: str) -> None:
        """Clear streaming state for a session without broadcasting.

        Used during cleanup when no devices are connected to receive broadcasts.
        """
        async with self._lock:
            self._streaming_sessions.discard(session_id)
            self._session_user_map.pop(session_id, None)
            self._session_project_map.pop(session_id, None)
            self._session_worktree_map.pop(session_id, None)

    async def sweep_stale_maps(self) -> int:
        """Remove session map entries that are no longer in _streaming_sessions.

        Handles cases where a stream ended abnormally (SDK crash, forced
        disconnect) and broadcast_stream_end was never called, leaving
        stale entries in _session_user_map, _session_project_map, and
        _session_worktree_map.

        Returns:
            Number of stale entries removed.
        """
        async with self._lock:
            stale_ids = set()
            for sid in self._session_user_map:
                if sid not in self._streaming_sessions:
                    stale_ids.add(sid)
            for sid in self._session_project_map:
                if sid not in self._streaming_sessions:
                    stale_ids.add(sid)
            for sid in self._session_worktree_map:
                if sid not in self._streaming_sessions:
                    stale_ids.add(sid)

            for sid in stale_ids:
                self._session_user_map.pop(sid, None)
                self._session_project_map.pop(sid, None)
                self._session_worktree_map.pop(sid, None)

            # Also clean up session locks for sessions with no connected devices
            # and not currently streaming. Locks can become orphaned when a device
            # disconnects abnormally without releasing its lock.
            stale_lock_ids = []
            for sid in list(self._session_locks.keys()):
                if sid not in self._streaming_sessions and sid not in self._connections:
                    stale_lock_ids.append(sid)
            for sid in stale_lock_ids:
                del self._session_locks[sid]
                stale_ids.add(sid)

            if stale_ids:
                logger.info(
                    f"Swept {len(stale_ids)} stale entries "
                    f"(locks={len(stale_lock_ids)}). "
                    f"Remaining: user_map={len(self._session_user_map)} "
                    f"project_map={len(self._session_project_map)} "
                    f"worktree_map={len(self._session_worktree_map)} "
                    f"session_locks={len(self._session_locks)}"
                )
            return len(stale_ids)

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

    async def broadcast_stream_start(
        self,
        session_id: str,
        message_id: str,
        source_device_id: Optional[str] = None,
        usage: Optional[Dict[str, Any]] = None,
        user_id: Optional[str] = None,
        project_id: Optional[str] = None,
        worktree_id: Optional[str] = None
    ):
        """Notify all devices that streaming has started for a session.

        This method is idempotent - calling it multiple times for the same session
        is safe.

        Args:
            session_id: The session ID
            message_id: The assistant message ID
            source_device_id: Device that originated the event (will be excluded from broadcast)
            usage: Token usage data from message_start event (input_tokens, cache_creation_input_tokens, cache_read_input_tokens)
            user_id: Optional user ID for user-level broadcast (notifies all user devices)
            project_id: Optional project ID for workspace-level streaming awareness
            worktree_id: Optional worktree ID — worktree sessions have independent working
                directories and should not lock git controls for project-mode sessions
        """
        # Mark session as streaming (idempotent) — acquire lock to protect
        # shared streaming maps from concurrent broadcast_stream_start/end calls.
        async with self._lock:
            self._streaming_sessions.add(session_id)

            # Store user_id mapping so broadcast_stream_end can look it up
            if user_id:
                self._session_user_map[session_id] = user_id
            # Store project_id mapping for workspace-level streaming awareness
            if project_id is not None:
                self._session_project_map[session_id] = project_id
            # Store worktree_id mapping for worktree-aware git control locking
            if worktree_id is not None:
                self._session_worktree_map[session_id] = worktree_id

        data = {"message_id": message_id}
        if usage:
            data["usage"] = usage

        event = SyncEvent(
            event_type="stream_start",
            session_id=session_id,
            data=data,
            source_device_id=source_device_id
        )
        await self.broadcast_event(event, exclude_device_id=source_device_id)

        # Broadcast to all user devices so side panels update streaming indicators
        stored_user_id = self._session_user_map.get(session_id)
        if stored_user_id:
            await self._broadcast_user_message(
                user_id=stored_user_id,
                message={
                    "type": "user_streaming_changed",
                    "session_id": session_id,
                    "is_streaming": True,
                    "project_id": self._session_project_map.get(session_id),
                    "worktree_id": self._session_worktree_map.get(session_id),
                },
                source_device_id=source_device_id
            )

    async def broadcast_usage_update(
        self,
        session_id: str,
        usage: Dict[str, Any],
        source_device_id: Optional[str] = None
    ):
        """Broadcast a usage update during streaming (for agentic loops with multiple turns).

        This is called when subsequent stream_start events occur during a single query,
        such as when Claude uses tools and continues responding. Each new assistant turn
        has updated token usage that synced devices need to know about.

        Args:
            session_id: The session ID
            usage: Token usage data (input_tokens, cache_creation_input_tokens, cache_read_input_tokens)
            source_device_id: Device that originated the event (will be excluded from broadcast)
        """
        event = SyncEvent(
            event_type="usage_update",
            session_id=session_id,
            data={"usage": usage},
            source_device_id=source_device_id
        )
        await self.broadcast_event(event, exclude_device_id=source_device_id)

    async def broadcast_stream_chunk(
        self,
        session_id: str,
        message_id: str,
        chunk_type: str,  # 'text', 'tool_use', 'tool_result', 'subagent_*'
        chunk_data: Dict[str, Any],
        source_device_id: Optional[str] = None
    ):
        """Broadcast a streaming chunk to all watching devices"""
        event = SyncEvent(
            event_type="stream_chunk",
            session_id=session_id,
            data={
                "message_id": message_id,
                "chunk_type": chunk_type,
                **chunk_data
            },
            source_device_id=source_device_id
        )
        await self.broadcast_event(event, exclude_device_id=source_device_id)

    async def broadcast_stream_end(
        self,
        session_id: str,
        message_id: str,
        metadata: Optional[Dict[str, Any]] = None,
        interrupted: bool = False,
        source_device_id: Optional[str] = None
    ):
        """Notify all devices that streaming has ended"""
        # Acquire lock to protect shared streaming maps from concurrent access.
        async with self._lock:
            self._streaming_sessions.discard(session_id)
            # Pop user_id mapping (used for user-level broadcast below)
            stored_user_id = self._session_user_map.pop(session_id, None)
            # Pop project_id mapping (used for user-level broadcast below)
            stored_project_id = self._session_project_map.pop(session_id, None)
            # Pop worktree_id mapping
            stored_worktree_id = self._session_worktree_map.pop(session_id, None)

        event = SyncEvent(
            event_type="stream_end",
            session_id=session_id,
            data={
                "message_id": message_id,
                "metadata": metadata or {},
                "interrupted": interrupted
            },
            source_device_id=source_device_id
        )
        await self.broadcast_event(event, exclude_device_id=source_device_id)

        # Broadcast to all user devices so side panels update streaming indicators
        if stored_user_id:
            await self._broadcast_user_message(
                user_id=stored_user_id,
                message={
                    "type": "user_streaming_changed",
                    "session_id": session_id,
                    "is_streaming": False,
                    "project_id": stored_project_id,
                    "worktree_id": stored_worktree_id,
                },
                source_device_id=source_device_id
            )

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
        """Get current state of a session for new device joining"""
        return {
            "session_id": session_id,
            "is_streaming": self.is_session_streaming(session_id),
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

    def get_user_streaming_sessions(self, user_id: str) -> List[Dict[str, Any]]:
        """Get all currently streaming sessions for a user with their project/worktree IDs.

        Used to send an initial snapshot when a device connects.
        Returns list of dicts with session_id, project_id, and worktree_id.
        """
        return [
            {
                "session_id": sid,
                "project_id": self._session_project_map.get(sid),
                "worktree_id": self._session_worktree_map.get(sid),
            }
            for sid in self._streaming_sessions
            if self._session_user_map.get(sid) == user_id
        ]


# Global sync engine instance
sync_engine = SyncEngine()
