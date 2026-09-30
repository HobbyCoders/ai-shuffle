"""
Real-time synchronization engine for cross-device chat sync.

Manages WebSocket connections and broadcasts events to all connected devices
watching the same session.
"""

import asyncio
import json
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import (
    Any, Awaitable, Callable, Deque, Dict, List, Optional, Tuple,
)

from fastapi import WebSocket

from app.core.detached_tasks import spawn_detached

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Outbound WebSocket delivery model (W2)
# ---------------------------------------------------------------------------
# Every connection owns a bounded queue plus a dedicated writer task. Producers
# (the turn pump's broadcast leg, notification fan-out, usage snapshots, ...)
# only ever ENQUEUE — they never await a socket. Before this, ``broadcast_event``
# awaited each device's ``send_text`` serially, so one half-open client (phone
# locked, cellular NAT timeout) stalled the producer for up to the send timeout
# PER FRAME PER DEVICE. The producer is the turn pump, so that stall froze SDK
# event consumption and the TurnRecorder — and therefore SSE delivery to EVERY
# device, not just the sick one.
#
# Ordering: each connection has exactly one writer task draining one FIFO deque,
# so frames for a given connection are delivered in enqueue order. There is no
# cross-connection ordering guarantee, and there never was.

# Upper bound on any single outbound send performed by a writer task. A
# half-open connection (client vanished uncleanly) never raises — the send just
# blocks forever once the socket buffer fills. This timeout no longer governs
# any producer's progress (producers only enqueue); it purely bounds how long a
# writer waits before declaring its own socket dead, so it can be aggressive.
# Mirrors app.api.ws_utils.WS_SEND_TIMEOUT_SECONDS; duplicated locally because
# core must not import the api layer.
_SEND_TIMEOUT_SECONDS = 2.5

# Depth of a connection's outbound queue. Sized for a burst of lifecycle/tool
# frames from a fast turn (the render frames ride SSE, not this leg) while still
# capping per-connection memory at a few hundred small JSON strings.
_OUTBOUND_QUEUE_MAXSIZE = 256

# ---------------------------------------------------------------------------
# Overflow policy
# ---------------------------------------------------------------------------
# What still rides this WS leg is lifecycle/coordination, not render content:
# ``done``, ``error``, ``tool_use``/``tool_result`` (they maintain the client's
# pendingTools set), permission and question frames, session/queue updates,
# notifications. Silently dropping any of those leaves the client incoherent —
# a spinner that never stops, a permission modal that never appears, a tool card
# stuck "running". So the policy is two-tier:
#
#   1. COALESCABLE frames — those carrying a COMPLETE replacement snapshot,
#      where the newest one alone is sufficient and the older one is pure waste
#      (``usage_snapshot``, ``tabs_sync``, ``session_attention`` per session,
#      ``queue_updated`` per session). These are collapsed IN PLACE at enqueue
#      time: the queued frame's payload is overwritten with the newer one and
#      keeps its queue position, so ordering relative to other frame types is
#      preserved and the client never sees a stale snapshot. A coalescable
#      frame therefore never contributes more than one slot per key.
#
#   2. EVERYTHING ELSE — no safe drop exists, so the connection is dropped
#      instead: the writer is killed, the socket closed, the device pruned from
#      the registry. The client reconnects and resyncs from durable state (the
#      SSE event feed's cursor resume, the /live snapshot, and the
#      pending_permissions / pending_questions replay), which is exactly the
#      path a network drop already takes. A disconnect the client recovers from
#      beats a silently truncated stream it cannot detect.
_COALESCABLE_SESSION_EVENTS = frozenset({"queue_updated"})
_COALESCABLE_USER_FRAMES = frozenset({"usage_snapshot", "tabs_sync"})

def _session_coalesce_key(event_type: str, session_id: str) -> Optional[Tuple[Any, ...]]:
    """Coalescing key for a session-scoped SyncEvent, or None if it must not drop."""
    if event_type in _COALESCABLE_SESSION_EVENTS:
        return ("session", event_type, session_id)
    return None


def _is_owner_user_id(user_id: str) -> bool:
    """Whether a user-level socket registration belongs to the owner.

    The chat socket registers as ``"admin"`` for the owner, ``"paired:<id>"``
    for a device paired to the owner, and the API key's id for a key
    (app/api/websocket.py). Owner-only broadcasts skip the last kind.
    """
    return user_id == "admin" or user_id.startswith("paired:")


def _user_coalesce_key(message: Dict[str, Any]) -> Optional[Tuple[Any, ...]]:
    """Coalescing key for a user-scoped WS frame, or None if it must not drop."""
    mtype = message.get("type")
    if mtype in _COALESCABLE_USER_FRAMES:
        return ("user", mtype)
    if mtype == "session_attention":
        # Whole-state ("N items pending for this session", 0 clears): the newest
        # count for a session fully supersedes any earlier one.
        return ("user", mtype, message.get("session_id"))
    return None


@dataclass
class _OutboundFrame:
    """One pre-serialized text frame waiting on a connection's writer queue."""
    text: str
    coalesce_key: Optional[Tuple[Any, ...]] = None


class ConnectionWriter:
    """Bounded outbound queue + dedicated writer task for a single WebSocket.

    Producers call :meth:`enqueue`, which is fully synchronous and never awaits.
    The writer task owns the socket: it is the ONLY place an outbound send is
    awaited, so a slow peer costs exactly one stalled task — its own.

    Lifecycle: :meth:`start` on connect (idempotent), :meth:`aclose` on
    disconnect (cancels and awaits the task, so nothing leaks), and
    ``SyncEngine.shutdown`` for process teardown. Any exception inside the
    writer is contained: it kills that one connection, never the loop.
    """

    def __init__(
        self,
        websocket: Any,
        *,
        label: str,
        maxsize: int = _OUTBOUND_QUEUE_MAXSIZE,
        on_sent: Optional[Callable[[], None]] = None,
    ) -> None:
        self._websocket = websocket
        self._label = label
        self._maxsize = maxsize
        self._on_sent = on_sent
        self._on_dead: Optional[Callable[[], Awaitable[None]]] = None
        self._pending: Deque[_OutboundFrame] = deque()
        self._coalescable: Dict[Tuple[Any, ...], _OutboundFrame] = {}
        self._wakeup = asyncio.Event()
        self._task: Optional["asyncio.Task[None]"] = None
        self._dead = False
        self._reap = True
        # Observability: frames superseded by coalescing, and the reason this
        # writer died (None while healthy).
        self.coalesced = 0
        self.death_reason: Optional[str] = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Create the writer task if it doesn't exist yet. Idempotent."""
        if self._task is not None or self._dead:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop yet — frames buffer until a start() with one
        self._task = loop.create_task(self._run(), name=f"ws-writer:{self._label}")

    def set_on_dead(self, hook: Optional[Callable[[], Awaitable[None]]]) -> None:
        """Register the registry-reap callback fired when this writer dies."""
        self._on_dead = hook

    @property
    def alive(self) -> bool:
        return not self._dead

    @property
    def depth(self) -> int:
        """Frames currently queued (test/metrics visibility)."""
        return len(self._pending)

    async def aclose(self, *, close_socket: bool = False) -> None:
        """Stop the writer, awaiting its task so nothing is left pending.

        Deliberate teardown: the reap hook is suppressed (the caller is already
        removing this connection from the registry). Idempotent.
        """
        self._reap = False
        self._dead = True
        task, self._task = self._task, None
        self._wakeup.set()
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 — teardown must never propagate
                logger.debug("ws-writer %s errored during close", self._label, exc_info=True)
        self._pending.clear()
        self._coalescable.clear()
        if close_socket:
            spawn_detached(self._close_socket(), name=f"ws-writer-close:{self._label}")

    async def _close_socket(self) -> None:
        try:
            await asyncio.wait_for(self._websocket.close(), _SEND_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 — the socket is already a zombie
            pass

    def _mark_dead(self, reason: str) -> None:
        """Kill this connection. Synchronous: safe from the enqueue path.

        The socket close and the registry reap run detached so no producer ever
        waits on them.
        """
        if self._dead:
            return
        self._dead = True
        self.death_reason = reason
        self._pending.clear()
        self._coalescable.clear()
        self._wakeup.set()
        spawn_detached(self._teardown(), name=f"ws-writer-teardown:{self._label}")

    async def _teardown(self) -> None:
        await self._close_socket()
        hook = self._on_dead
        if self._reap and hook is not None:
            try:
                await hook()
            except Exception:  # noqa: BLE001
                logger.debug("ws-writer %s reap hook failed", self._label, exc_info=True)

    # -- producer side (never awaits) --------------------------------------

    def enqueue(self, text: str, coalesce_key: Optional[Tuple[Any, ...]] = None) -> bool:
        """Queue one pre-serialized text frame. Never blocks, never awaits.

        Returns False when the frame was not accepted, which always means the
        connection is finished: it has been marked dead and its socket close +
        reap are already in flight. Callers should prune it from their registry.
        """
        if self._dead:
            return False
        if coalesce_key is not None:
            superseded = self._coalescable.get(coalesce_key)
            if superseded is not None:
                # Newest whole-state snapshot wins, in the position the older
                # one already held (see "Overflow policy" above).
                superseded.text = text
                self.coalesced += 1
                self._wakeup.set()
                return True
        if len(self._pending) >= self._maxsize:
            logger.warning(
                "ws-writer %s: outbound queue full (%d frames) — dropping connection; "
                "the client resyncs from the event feed on reconnect",
                self._label, self._maxsize,
            )
            self._mark_dead("queue_overflow")
            return False
        frame = _OutboundFrame(text=text, coalesce_key=coalesce_key)
        self._pending.append(frame)
        if coalesce_key is not None:
            self._coalescable[coalesce_key] = frame
        self._wakeup.set()
        if self._task is None:
            self.start()
        return True

    # -- consumer side -----------------------------------------------------

    async def _run(self) -> None:
        """Drain the queue onto the socket, one frame at a time, in order."""
        try:
            while True:
                if not self._pending:
                    self._wakeup.clear()
                    # Re-check after clear: producers are synchronous and set
                    # the event AFTER appending, so this ordering cannot lose a
                    # wakeup.
                    if not self._pending:
                        await self._wakeup.wait()
                    if self._dead:
                        return
                    continue
                frame = self._pending.popleft()
                key = frame.coalesce_key
                if key is not None and self._coalescable.get(key) is frame:
                    del self._coalescable[key]
                try:
                    await asyncio.wait_for(
                        self._websocket.send_text(frame.text), _SEND_TIMEOUT_SECONDS
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 — includes TimeoutError
                    logger.warning(
                        "ws-writer %s: send failed (%s) — dropping connection",
                        self._label, e,
                    )
                    self._mark_dead("send_failed")
                    return
                if self._on_sent is not None:
                    self._on_sent()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — one bad connection must not kill the loop
            logger.warning("ws-writer %s crashed", self._label, exc_info=True)
            self._mark_dead("writer_crashed")


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
    writer: ConnectionWriter = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.writer = ConnectionWriter(
            self.websocket,
            label=f"device={self.device_id} session={self.session_id}",
            on_sent=self._touch,
        )

    def _touch(self) -> None:
        self.last_activity = datetime.now(timezone.utc)

    def send_text(self, text: str, coalesce_key: Optional[Tuple[Any, ...]] = None) -> bool:
        """Hand one pre-serialized frame to this device's writer (non-blocking).

        Returns False if the connection is finished and must be pruned.
        """
        self.writer.start()
        return self.writer.enqueue(text, coalesce_key)

    async def send_event(self, event: SyncEvent) -> bool:
        """Queue an event for this device. Returns False if the connection is dead.

        Non-blocking despite being a coroutine: it performs no awaits, so the
        caller resumes without ever yielding to the socket. Delivery (and the
        only bounded await) happens on this connection's writer task.
        """
        try:
            text = json.dumps(event.to_dict(), separators=(",", ":"), default=str)
        except (TypeError, ValueError) as e:
            # A non-serializable payload is a bug in that one message, not a
            # sign the socket died — drop the frame, keep the connection.
            logger.error("Failed to serialize event for device %s: %s", self.device_id, e)
            return True
        return self.send_text(text)


@dataclass
class UserDeviceConnection:
    """Represents a connected device for a user (for tab sync)"""
    device_id: str
    user_id: str
    websocket: WebSocket
    connected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_activity: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    writer: ConnectionWriter = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.writer = ConnectionWriter(
            self.websocket,
            label=f"user={self.user_id} device={self.device_id}",
            on_sent=self._touch,
        )

    def _touch(self) -> None:
        self.last_activity = datetime.now(timezone.utc)

    def send_text(self, text: str, coalesce_key: Optional[Tuple[Any, ...]] = None) -> bool:
        """Hand one pre-serialized frame to this device's writer (non-blocking)."""
        self.writer.start()
        return self.writer.enqueue(text, coalesce_key)

    async def send_json(self, data: Dict[str, Any]) -> bool:
        """Queue JSON data for this device. Returns False if the connection is dead.

        Non-blocking despite being a coroutine (no awaits): the send itself
        happens on this connection's writer task.
        """
        try:
            text = json.dumps(data, separators=(",", ":"), default=str)
        except (TypeError, ValueError) as e:
            logger.error("Failed to serialize frame for device %s: %s", self.device_id, e)
            return True
        return self.send_text(text, _user_coalesce_key(data))



def _note_pool_attachment(session_id: str, *, attached: bool) -> None:
    """Signal the SDK session pool that a session gained/lost its last watcher.

    Imported lazily: session_pool imports sync_engine for its self-healing
    attachment probe, so a module-level import here would be circular. Never
    raises — the pool derives the same signal itself within one re-evaluate
    interval, so this is a latency optimisation, not a correctness dependency.
    """
    try:
        from app.core import session_pool

        if attached:
            session_pool.note_session_attached(session_id)
        else:
            session_pool.note_session_detached(session_id)
    except Exception:  # noqa: BLE001 - never let a pool hint break WS lifecycle
        logger.debug("session_pool attachment hint failed for %s", session_id, exc_info=True)


class SyncEngine:
    """
    Manages real-time synchronization between devices.

    Key features:
    - Track device connections per session
    - Broadcast non-render events to all devices watching a session
    - Exclude source device from broadcasts (to avoid echo)

    V2: this engine carries NO streaming state. Render frames ride the
    session event feed (SSE); busy truth is the durable runs table
    (``app.core.streaming_status``).
    """

    def __init__(self):
        # session_id -> {registration_id -> DeviceConnection}
        self._connections: Dict[str, Dict[str, DeviceConnection]] = {}
        # Lock for thread-safe connection management
        self._lock = asyncio.Lock()
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

            devices = self._connections[session_id]
            for existing in devices.values():
                if existing.device_id == device_id and existing.websocket is websocket:
                    return existing
            # Persistent device IDs are shared by tabs, not unique to sockets.
            # Match the user registry: retain the device ID on the connection
            # and use a private registration key for sibling sockets.
            from uuid import uuid4
            registration_id = device_id if device_id not in devices else uuid4().hex
            while registration_id in devices:
                registration_id = uuid4().hex

            connection = DeviceConnection(
                device_id=device_id,
                session_id=session_id,
                websocket=websocket
            )
            # Reap hook: if the writer dies on its own (dead socket / overflow)
            # it prunes itself from this registry instead of lingering as a
            # zombie entry that every later broadcast has to rediscover.
            connection.writer.set_on_dead(
                lambda: self._reap_device(registration_id, session_id, connection)
            )
            connection.writer.start()
            self._connections[session_id][registration_id] = connection

            # Tell the SDK pool this session is being watched, so its tiered
            # idle policy doesn't treat it as abandoned. No-raise / no-op before
            # the pool exists; imported lazily to avoid an import cycle.
            _note_pool_attachment(session_id, attached=True)

            logger.info("Device %s registered to watch session %s", device_id, session_id)
            return connection

    async def _reap_device(
        self, device_id: str, session_id: str, connection: "DeviceConnection"
    ) -> None:
        """Drop a device whose writer died. Identity-checked so a newer
        connection for the same device_id is never collateral damage."""
        async with self._lock:
            devices = self._connections.get(session_id)
            if devices is None or devices.get(device_id) is not connection:
                return
            del devices[device_id]
            if not devices:
                self._connections.pop(session_id, None)
        logger.info(
            "Device %s dropped from session %s (writer died: %s)",
            device_id, session_id, connection.writer.death_reason,
        )

    async def unregister_device(self, device_id: str, session_id: str, websocket: Optional[WebSocket] = None):
        """Unregister a device from watching a session.

        Args:
            device_id: The device ID to unregister
            session_id: The session ID to unregister from
            websocket: Only remove the registration using this exact socket.
                      Without it, only an unambiguous device registration is removed.
                      This keeps stale cleanup from evicting a sibling or reconnect.
        """
        writer: Optional[ConnectionWriter] = None
        async with self._lock:
            devices = self._connections.get(session_id)
            if devices is not None:
                matches = [(key, conn) for key, conn in devices.items()
                           if conn.device_id == device_id
                           and (websocket is None or conn.websocket is websocket)]
                # Without a socket, only an unambiguous registration is safe.
                if len(matches) == 1:
                    key, conn = matches[0]
                    del devices[key]
                    writer = conn.writer
                    logger.info(
                        "Device %s unregistered from session %s", device_id, session_id
                    )

                # Clean up empty session entries
                if not devices:
                    self._connections.pop(session_id, None)
                    # Last watcher gone: let the pool start its detached grace
                    # timer instead of waiting for the idle sweep to notice.
                    _note_pool_attachment(session_id, attached=False)

        # Outside the lock: cancels the writer task and AWAITS it, so no writer
        # outlives its connection.
        if writer is not None:
            await writer.aclose()

    def get_connected_devices(self, session_id: str) -> List[str]:
        """Get list of device IDs watching a session"""
        if session_id not in self._connections:
            return []
        return list(dict.fromkeys(conn.device_id for conn in self._connections[session_id].values()))

    def get_device_count(self, session_id: str) -> int:
        """Get number of devices watching a session"""
        return len(self._connections.get(session_id, {}))

    def is_device_watching_session(self, device_id: str, session_id: str) -> bool:
        """Check if a specific device is already watching a session.

        Multiple sockets can share the same persistent device ID.
        """
        return any(conn.device_id == device_id
                   for conn in self._connections.get(session_id, {}).values())

    async def broadcast_event(
        self,
        event: SyncEvent,
        exclude_device_id: Optional[str] = None
    ):
        """
        Broadcast an event to all devices watching the session.

        This runs inside the turn pump's broadcast leg, so it must never await
        a socket: every device gets a NON-BLOCKING enqueue onto its own writer
        queue (W2). A device that cannot keep up stalls only its own writer.

        Args:
            event: The event to broadcast
            exclude_device_id: Device ID to exclude (usually the source device)
        """
        session_id = event.session_id

        # Snapshot connections under lock to avoid TOCTOU race with
        # register_device/unregister_device which modify _connections under lock.
        async with self._lock:
            devices = self._connections.get(session_id)
            if not devices:
                # Lazy %-args: this is a per-event hot path (W9).
                logger.debug("No connections for session %s, skipping broadcast", session_id)
                return

            # Get connections to broadcast to
            connections = list(devices.items())
        logger.debug(
            "Broadcasting %s to %d devices for session %s",
            event.event_type, len(connections), session_id,
        )

        # Pre-serialize once so a serialization error doesn't disconnect all devices.
        # default=str: event payloads can include PostgreSQL datetime values (e.g. a
        # queued message's created_at). Without it those raise here and the event is
        # dropped for every device — e.g. queue_updated never reaching the client.
        try:
            event_text = json.dumps(event.to_dict(), separators=(",", ":"), default=str)
        except (TypeError, ValueError) as e:
            logger.error(
                "Failed to serialize event %s for session %s: %s",
                event.event_type, session_id, e,
            )
            return

        coalesce_key = _session_coalesce_key(event.event_type, session_id)

        # Send to all devices except the excluded one. No awaits in this loop —
        # a producer stall here is exactly the W2 bug.
        failed_devices = []
        for registration_id, conn in connections:
            if exclude_device_id and conn.device_id == exclude_device_id:
                logger.debug("Skipping source device %s", conn.device_id)
                continue

            logger.debug("Queueing %s for device %s", event.event_type, conn.device_id)
            if not conn.send_text(event_text, coalesce_key):
                failed_devices.append((registration_id, conn))

        # Clean up connections the writer refused (dead / overflowed). Their
        # sockets are already being closed detached by the writer.
        if failed_devices:
            async with self._lock:
                devices = self._connections.get(session_id)
                if devices is not None:
                    for registration_id, conn in failed_devices:
                        if devices.get(registration_id) is conn:
                            del devices[registration_id]
                    if not devices:
                        self._connections.pop(session_id, None)

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
        source_device_id: Optional[str] = None,
        pending_peer_messages: Optional[list] = None,
    ):
        """Broadcast snapshots without yielding before the connection enqueue."""
        data = {"queued_messages": queued_messages}
        if pending_peer_messages is not None:
            data["pending_peer_messages"] = pending_peer_messages
        event = SyncEvent(
            event_type="queue_updated",
            session_id=session_id,
            data=data,
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

            devices = self._user_connections[user_id]
            for existing in devices.values():
                if existing.device_id == device_id and existing.websocket is websocket:
                    return existing
            # A device ID is shared by browser tabs; it is not a socket ID.
            from uuid import uuid4
            registration_id = device_id if device_id not in devices else uuid4().hex
            while registration_id in devices:
                registration_id = uuid4().hex

            connection = UserDeviceConnection(
                device_id=device_id,
                user_id=user_id,
                websocket=websocket
            )
            connection.writer.set_on_dead(
                lambda: self._reap_user_device(user_id, registration_id, connection)
            )
            connection.writer.start()
            self._user_connections[user_id][registration_id] = connection

            logger.debug("User device registered: user=%s device=%s", user_id, device_id)
            return connection

    async def _reap_user_device(
        self, user_id: str, device_id: str, connection: "UserDeviceConnection"
    ) -> None:
        """Drop a user device whose writer died (identity-checked)."""
        async with self._user_lock:
            devices = self._user_connections.get(user_id)
            if devices is None or devices.get(device_id) is not connection:
                return
            del devices[device_id]
            if not devices:
                self._user_connections.pop(user_id, None)
        logger.debug(
            "User device %s dropped for user %s (writer died: %s)",
            device_id, user_id, connection.writer.death_reason,
        )

    async def unregister_user_device(
        self,
        user_id: str,
        device_id: str,
        websocket: Optional[WebSocket] = None
    ):
        """Unregister a device from user connection tracking."""
        writer: Optional[ConnectionWriter] = None
        async with self._user_lock:
            devices = self._user_connections.get(user_id)
            if devices is not None:
                matches = [(key, conn) for key, conn in devices.items()
                           if conn.device_id == device_id
                           and (websocket is None or conn.websocket is websocket)]
                # Without a socket, only an unambiguous registration is safe.
                if len(matches) == 1:
                    key, conn = matches[0]
                    del devices[key]
                    writer = conn.writer

                # Clean up empty user entries
                if not devices:
                    self._user_connections.pop(user_id, None)

        # Outside the lock: cancel + await the writer task so nothing leaks.
        if writer is not None:
            await writer.aclose()

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
                if conn.device_id != source_device_id
            ]

        if not targets:
            return

        message = {
            "type": "tabs_sync",
            "tabs": tabs_data.get("tabs", []),
            "activeTabId": tabs_data.get("activeTabId"),
            "source_device_id": source_device_id
        }

        failed_devices = self._fan_out_to_user_devices(user_id, targets, message)

        sent_count = len(targets) - len(failed_devices)
        if sent_count > 0:
            logger.debug("Broadcast tabs_sync to %d devices for user %s", sent_count, user_id)
        if failed_devices:
            await self._prune_user_devices(user_id, failed_devices)

    async def _broadcast_user_message(
        self,
        user_id: str,
        message: Dict[str, Any],
        source_device_id: Optional[str] = None
    ):
        """Send a message to all devices for a user, excluding source device.

        Private helper used by user-level broadcast methods.

        Takes a snapshot of connections under the lock, releases it, then hands
        the frame to each device's writer queue. The fan-out itself performs NO
        awaits (W2), so a half-open device can never stall the caller — which on
        several of these paths is the turn pump.
        """
        from app.core.global_events import global_events
        if message.get("type") in {
            "user_session_created", "user_session_updated", "user_session_deleted",
            "user_session_cleared", "notification",
        }:
            # Legacy user IDs are not role-qualified. Session ownership is
            # authoritative; NotificationOwner carries explicit caller metadata.
            audience = getattr(user_id, "user_type", None)
            owner_id = user_id
            session_id = message.get("session_id")
            # A notification's referenced session need not belong to its
            # recipient (e.g. an admin alert about an API user's session).
            # Only explicit recipient metadata can scope that payload.
            if audience not in {"admin", "api_user"} and session_id:
                try:
                    from app.db import database
                    from app.db.async_database import run_sync
                    session = await run_sync(database.get_session, session_id)
                    if session is not None and "api_user_id" in session:
                        owner_id = session["api_user_id"]
                        audience = "admin" if owner_id is None else "api_user"
                except Exception:
                    logger.debug("Global lifecycle owner lookup failed", exc_info=True)
            global_events.lifecycle(owner_id, message, user_type=audience)
        # Snapshot connections under lock, then release before fan-out
        async with self._user_lock:
            user_devices = self._user_connections.get(user_id, {})
            if not user_devices:
                return
            targets = [
                (did, conn) for did, conn in user_devices.items()
                if conn.device_id != source_device_id
            ]

        if not targets:
            return

        failed_devices = self._fan_out_to_user_devices(user_id, targets, message)
        if failed_devices:
            await self._prune_user_devices(user_id, failed_devices)

    # -- user fan-out helpers ------------------------------------------------

    def _fan_out_to_user_devices(
        self,
        user_id: str,
        targets: List[tuple],
        message: Dict[str, Any],
    ) -> List[str]:
        """Serialize once and enqueue to every target. Never awaits.

        Returns the device ids whose writer refused the frame (dead socket or
        queue overflow) and must therefore be pruned.
        """
        try:
            text = json.dumps(message, separators=(",", ":"), default=str)
        except (TypeError, ValueError) as e:
            # A bad payload is one message's bug, not a transport failure:
            # drop the frame, keep every connection.
            logger.error(
                "Failed to serialize %s frame for user %s: %s",
                message.get("type"), user_id, e,
            )
            return []

        coalesce_key = _user_coalesce_key(message)
        failed: List[str] = []
        for device_id, conn in targets:
            if not conn.send_text(text, coalesce_key):
                failed.append(device_id)
        return failed

    async def _prune_user_devices(self, user_id: str, device_ids: List[str]) -> None:
        """Remove finished user connections from the registry."""
        async with self._user_lock:
            devices = self._user_connections.get(user_id)
            if devices is None:
                return
            for device_id in device_ids:
                devices.pop(device_id, None)
            if not devices:
                self._user_connections.pop(user_id, None)

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

    async def broadcast_user_permission(
        self,
        user_id: str,
        payload: Dict[str, Any],
        source_device_id: Optional[str] = None
    ):
        """Fan a permission frame out to every one of a user's devices.

        Exists for the same reason as ``broadcast_user_question``: the per-query
        origin socket only reaches the tab that owns the query. A ProviderAgent
        worker's approval is redirected onto its PARENT's session (P3-A3,
        ``app/core/provider_agent_approvals``) whose owning tab may be closed,
        headless, or on another device — so the frame is fanned user-wide and the
        frontend routes it by the ``session_id`` the payload already carries
        (deduping by ``request_id``).

        Ordinary permission prompts do NOT use this path; they keep riding the
        origin socket exactly as before.
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

    async def broadcast_user_session_updated(
        self,
        user_id: str,
        session_id: str,
        updates: Dict[str, Any],
        source_device_id: Optional[str] = None
    ):
        """Broadcast a session metadata change to all of a user's devices.

        Unlike ``broadcast_session_updated``, this does NOT require any device
        to be watching the session — which is exactly the voice command-session
        case: its hidden backfill must reach lists on devices that never opened
        the session (nobody ever watches a wake-word one-shot).
        """
        await self._broadcast_user_message(
            user_id=user_id,
            message={
                "type": "user_session_updated",
                "session_id": session_id,
                "updates": updates,
            },
            source_device_id=source_device_id
        )

    async def broadcast_user_session_cleared(
        self,
        user_id: str,
        session_id: str,
        source_device_id: Optional[str] = None
    ):
        """Broadcast that a session's conversation was cleared IN PLACE.

        User-level fan-out like ``broadcast_user_session_updated``: the clear
        appends NOTHING to the session's event log — it rewrites the log out
        from under any feed resuming at ``from_seq=lastSeq`` — so a device
        watching the session gets no per-session signal to react to, and a
        device merely listing it needs its row counters reset too.

        A dedicated type (not a ``user_session_updated`` field patch) because
        receivers must do more than patch a row: drop the session's cached
        transcript and re-hydrate an open view from seq 0, unambiguously.
        """
        await self._broadcast_user_message(
            user_id=user_id,
            message={"type": "user_session_cleared", "session_id": session_id},
            source_device_id=source_device_id
        )

    async def broadcast_user_session_deleted(
        self,
        user_id: str,
        session_id: str,
        source_device_id: Optional[str] = None
    ):
        """Broadcast that a session row was deleted.

        Counterpart of ``broadcast_user_session_created``: other devices drop
        the row from their lists, drop any cached transcript for it, and leave
        the chat view if they are sitting on the deleted session.
        """
        await self._broadcast_user_message(
            user_id=user_id,
            message={"type": "user_session_deleted", "session_id": session_id},
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
        """Fan a Claude usage-limits snapshot out to every OWNER device.

        Only the owner's sockets receive it: the web app/desktop (``"admin"``)
        and devices paired to the owner (``"paired:<id>"``). An API key's chat
        socket registers under the key's id and gets nothing: the snapshot is
        the owner's Claude windows, install names and instance id, which REST
        already refuses a key (``/usage/limits`` returns it an empty reading).

        Cluster-level (not session-scoped): the 5-hour Claude window is
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
                if _is_owner_user_id(user_id)
                for did, conn in devices.items()
            ]

        if not targets:
            return

        # Serialize once for the whole cluster fan-out, then enqueue without
        # awaiting (W2). ``usage_snapshot`` is coalescable: it is a complete
        # snapshot, so a newer one queued behind an older one simply replaces it.
        try:
            text = json.dumps(message, separators=(",", ":"), default=str)
        except (TypeError, ValueError) as e:
            logger.error("Failed to serialize usage_snapshot: %s", e)
            return

        coalesce_key = _user_coalesce_key(message)
        failed: List[tuple] = []
        for user_id, device_id, conn in targets:
            if not conn.send_text(text, coalesce_key):
                failed.append((user_id, device_id))

        if failed:
            async with self._user_lock:
                for user_id, device_id in failed:
                    devices = self._user_connections.get(user_id)
                    if devices is None:
                        continue
                    devices.pop(device_id, None)
                    if not devices:
                        self._user_connections.pop(user_id, None)

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

    # ============= Process teardown =============

    async def shutdown(self) -> None:
        """Cancel every connection writer and clear the registries.

        Called from the app's shutdown path so no writer task outlives the event
        loop (otherwise uvicorn's reload/exit logs "Task was destroyed but it is
        pending" once per connected device). Safe to call more than once, and
        safe to call with nothing connected.
        """
        from app.core.global_events import global_events
        await global_events.shutdown()
        writers: List[ConnectionWriter] = []
        async with self._lock:
            for devices in self._connections.values():
                writers.extend(conn.writer for conn in devices.values())
            self._connections.clear()
        async with self._user_lock:
            for devices in self._user_connections.values():
                writers.extend(conn.writer for conn in devices.values())
            self._user_connections.clear()

        if not writers:
            return
        # aclose() suppresses each writer's reap hook, so no task races us for
        # the (now empty) registries.
        await asyncio.gather(
            *(w.aclose() for w in writers), return_exceptions=True
        )
        logger.info("SyncEngine: shut down %d connection writer(s)", len(writers))


# Global sync engine instance
sync_engine = SyncEngine()
