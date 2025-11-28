"""
Real-time synchronization engine for cross-device chat sync.

Manages WebSocket connections and broadcasts events to all connected devices
watching the same session.
"""

import asyncio
import logging
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Set, Callable, Any
from weakref import WeakSet

from fastapi import WebSocket

logger = logging.getLogger(__name__)


@dataclass
class SyncEvent:
    """Represents a synchronization event to broadcast"""
    event_type: str  # 'message_added', 'message_chunk', 'stream_start', 'stream_end', 'session_updated'
    session_id: str
    data: Dict[str, Any]
    timestamp: datetime = field(default_factory=datetime.utcnow)
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
    connected_at: datetime = field(default_factory=datetime.utcnow)
    last_activity: datetime = field(default_factory=datetime.utcnow)

    async def send_event(self, event: SyncEvent) -> bool:
        """Send an event to this device. Returns False if send failed."""
        try:
            await self.websocket.send_json(event.to_dict())
            self.last_activity = datetime.utcnow()
            return True
        except Exception as e:
            logger.warning(f"Failed to send event to device {self.device_id}: {e}")
            return False


class SyncEngine:
    """
    Manages real-time synchronization between devices.

    Key features:
    - Track device connections per session
    - Broadcast events to all devices watching a session
    - Support for streaming events (message chunks)
    - Exclude source device from broadcasts (to avoid echo)
    """

    def __init__(self):
        # session_id -> set of DeviceConnection
        self._connections: Dict[str, Dict[str, DeviceConnection]] = {}
        # Lock for thread-safe connection management
        self._lock = asyncio.Lock()
        # Track streaming state per session
        self._streaming_sessions: Set[str] = set()

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

    async def unregister_device(self, device_id: str, session_id: str):
        """Unregister a device from watching a session"""
        async with self._lock:
            if session_id in self._connections:
                if device_id in self._connections[session_id]:
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

    def is_session_streaming(self, session_id: str) -> bool:
        """Check if a session is currently streaming"""
        return session_id in self._streaming_sessions

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
        session_id = event.session_id

        if session_id not in self._connections:
            return

        # Get connections to broadcast to
        connections = list(self._connections[session_id].values())

        # Send to all devices except the excluded one
        failed_devices = []
        for conn in connections:
            if exclude_device_id and conn.device_id == exclude_device_id:
                continue

            success = await conn.send_event(event)
            if not success:
                failed_devices.append(conn.device_id)

        # Clean up failed connections
        if failed_devices:
            async with self._lock:
                for device_id in failed_devices:
                    if session_id in self._connections:
                        self._connections[session_id].pop(device_id, None)

    async def broadcast_stream_start(
        self,
        session_id: str,
        message_id: str,
        source_device_id: Optional[str] = None
    ):
        """Notify all devices that streaming has started for a session"""
        self._streaming_sessions.add(session_id)

        event = SyncEvent(
            event_type="stream_start",
            session_id=session_id,
            data={"message_id": message_id},
            source_device_id=source_device_id
        )
        await self.broadcast_event(event, exclude_device_id=source_device_id)

    async def broadcast_stream_chunk(
        self,
        session_id: str,
        message_id: str,
        chunk_type: str,  # 'text', 'tool_use', 'tool_result'
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
        self._streaming_sessions.discard(session_id)

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

    async def get_session_state(self, session_id: str) -> Dict[str, Any]:
        """Get current state of a session for new device joining"""
        return {
            "session_id": session_id,
            "is_streaming": self.is_session_streaming(session_id),
            "connected_devices": self.get_device_count(session_id)
        }

    async def cleanup_stale_connections(self, max_age_seconds: int = 300):
        """Remove connections that haven't been active recently"""
        now = datetime.utcnow()
        stale_devices = []

        async with self._lock:
            for session_id, devices in list(self._connections.items()):
                for device_id, conn in list(devices.items()):
                    age = (now - conn.last_activity).total_seconds()
                    if age > max_age_seconds:
                        stale_devices.append((session_id, device_id))

        for session_id, device_id in stale_devices:
            await self.unregister_device(device_id, session_id)
            logger.info(f"Cleaned up stale connection: {device_id} from {session_id}")


# Global sync engine instance
sync_engine = SyncEngine()
