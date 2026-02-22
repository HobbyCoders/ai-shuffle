"""
LRU Session Pool for Claude SDK clients.

Keeps SDK clients alive between queries to avoid spawning a new Node.js
subprocess for every message. Bounded by max_size to limit memory usage.
Eviction happens lazily: only when the pool is full and a new session
needs to enter.

The SDK lifecycle is: connect() -> [query() -> receive_response()]* -> disconnect()
This module keeps clients in the [query -> receive_response]* phase between user messages,
avoiding repeated subprocess spawns and JSONL history replays.
"""

import asyncio
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any

from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

logger = logging.getLogger(__name__)

# Grace period (seconds) for the SDK subprocess to flush JSONL writes before SIGTERM.
# The Claude Code subprocess writes to JSONL and stdout independently. After sending
# the ResultMessage to us, it may still be flushing the final JSONL entry. If we
# terminate immediately, that write is lost and the session history is incomplete.
_SUBPROCESS_EXIT_GRACE_SECONDS = 5.0


@dataclass
class SessionState:
    """Track state for an active SDK session.

    Extended from the original query_engine.SessionState with:
    - callback_refs: Mutable dict for indirect callback references (broadcast_func, etc.)
      These are updated at the start of each query so the can_use_tool callback
      (bound at connect() time) always references the current WebSocket.
    - connect_options: The ClaudeAgentOptions used at connect() time, for detecting
      whether a reconnect is needed when options change between queries.
    """
    client: ClaudeSDKClient
    sdk_session_id: Optional[str] = None
    is_connected: bool = False
    is_streaming: bool = False
    interrupt_requested: bool = False
    last_activity: datetime = field(default_factory=datetime.now)
    background_task: Optional[asyncio.Task] = None
    written_agent_ids: list = field(default_factory=list)
    agents_dir: Optional[Path] = None

    # Mutable dict for callback indirection — updated each query, read by can_use_tool closure
    callback_refs: Dict[str, Any] = field(default_factory=lambda: {
        "broadcast_func": None,
        "permission_mode": "default",
        "allowed_tools": [],
        "disallowed_tools": [],
        "profile_id": None,
        "config": {},
        "session_id": None,
    })

    # Options snapshot from connect() time — for reconnect detection
    connect_options: Optional[ClaudeAgentOptions] = None


def is_subprocess_alive(state: SessionState) -> bool:
    """Check if the subprocess for a session state is still running."""
    if not state.client:
        return False
    try:
        query_obj = getattr(state.client, '_query', None)
        transport = getattr(query_obj, 'transport', None) if query_obj else None
        process = getattr(transport, '_process', None) if transport else None
        return process is not None and process.returncode is None
    except Exception:
        return False


async def graceful_disconnect(client: ClaudeSDKClient, session_id: str) -> None:
    """Disconnect the SDK client, giving the subprocess time to flush JSONL writes.

    The SDK's disconnect() immediately sends SIGTERM to the subprocess. But the
    Claude Code subprocess writes to stdout (messages to us) and to JSONL (persistent
    history) independently. After sending the result message, the subprocess may still
    be flushing the final assistant message to JSONL. Terminating immediately causes
    that write to be lost, resulting in sessions with missing final messages.

    This function waits for the subprocess to exit naturally (with a timeout) before
    calling disconnect(). If the process has already exited, disconnect() is a no-op.
    """
    try:
        # Access the underlying anyio Process via the SDK's internal structure.
        # This is necessary because the SDK's disconnect() offers no grace period.
        query = getattr(client, '_query', None)
        transport = getattr(query, 'transport', None) if query else None
        process = getattr(transport, '_process', None) if transport else None

        if process and process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=_SUBPROCESS_EXIT_GRACE_SECONDS)
                logger.debug(f"[Pool] Subprocess exited naturally for session {session_id} (rc={process.returncode})")
            except asyncio.TimeoutError:
                logger.warning(
                    f"[Pool] Subprocess for session {session_id} didn't exit in "
                    f"{_SUBPROCESS_EXIT_GRACE_SECONDS}s, forcing disconnect"
                )
    except Exception as e:
        # Don't let introspection errors prevent disconnect
        logger.debug(f"[Pool] Could not wait for subprocess for session {session_id}: {e}")

    # Now disconnect (terminates the process if still running, no-op if already exited).
    # NOTE: When called from asyncio.shield() during task cancellation, the shield
    # creates a new internal task. client.disconnect() will fail because anyio's
    # cancel scope was entered in the original task. In that case, fall back to
    # directly terminating the subprocess.
    try:
        await client.disconnect()
        logger.debug(f"[Pool] Disconnected SDK client for session {session_id}")
    except RuntimeError as e:
        if "cancel scope" in str(e):
            logger.debug(f"[Pool] Cannot disconnect from shielded task for session {session_id}, terminating subprocess directly")
            # Fall back to direct process termination
            try:
                query_obj = getattr(client, '_query', None)
                transport = getattr(query_obj, 'transport', None) if query_obj else None
                proc = getattr(transport, '_process', None) if transport else None
                if proc and proc.returncode is None:
                    proc.terminate()
            except Exception:
                pass
        else:
            raise


class SessionPool:
    """
    Bounded LRU pool of SDK client sessions.

    - OrderedDict for O(1) LRU tracking (move_to_end on access)
    - Configurable max_size (default 3)
    - Eviction only on insertion when full (lazy — no background timer)
    - Thread-safe within a single asyncio event loop
    """

    def __init__(self, max_size: int = 3):
        self._pool: OrderedDict[str, SessionState] = OrderedDict()
        self._max_size = max(1, max_size)
        self._eviction_lock = asyncio.Lock()

    @property
    def max_size(self) -> int:
        return self._max_size

    @max_size.setter
    def max_size(self, value: int):
        self._max_size = max(1, value)

    def get(self, session_id: str) -> Optional[SessionState]:
        """Get session state, promoting to MRU position. Returns None if not found."""
        state = self._pool.get(session_id)
        if state is not None:
            self._pool.move_to_end(session_id)
        return state

    def peek(self, session_id: str) -> Optional[SessionState]:
        """Get session state WITHOUT promoting (for read-only checks like interrupt)."""
        return self._pool.get(session_id)

    def put(self, session_id: str, state: SessionState) -> None:
        """Insert or update a session, promoting to MRU position.

        Call evict_if_needed() before put() for new sessions to maintain the size bound.
        """
        self._pool[session_id] = state
        self._pool.move_to_end(session_id)

    async def evict_if_needed(self) -> Optional[str]:
        """If pool is at capacity, evict the LRU non-streaming session.

        Returns the evicted session_id, or None if no eviction was needed/possible.

        IMPORTANT: Never evicts a session that is currently streaming.
        If all sessions are streaming, returns None (pool temporarily over capacity).
        """
        async with self._eviction_lock:
            if len(self._pool) < self._max_size:
                return None

            # Find the LRU session that is NOT streaming
            for session_id, state in self._pool.items():
                if not state.is_streaming:
                    return await self._do_evict(session_id, state)

            # All sessions are streaming — allow temporary over-capacity
            logger.warning(
                f"[Pool] At capacity ({self._max_size}) with all sessions streaming. "
                f"Allowing temporary over-capacity ({len(self._pool)} sessions)."
            )
            return None

    async def _do_evict(self, session_id: str, state: SessionState) -> str:
        """Evict a specific session. Returns the evicted session_id."""
        del self._pool[session_id]
        if state.is_connected and is_subprocess_alive(state):
            try:
                await graceful_disconnect(state.client, session_id)
            except Exception as e:
                logger.warning(f"[Pool] Error during eviction disconnect for {session_id}: {e}")
        # Cleanup agent files
        if state.agents_dir and state.written_agent_ids:
            from app.core.query_engine import cleanup_agents_directory
            cleanup_agents_directory(state.agents_dir, state.written_agent_ids)
        logger.info(f"[Pool] Evicted session {session_id} (pool size: {len(self._pool)})")
        return session_id

    async def remove(self, session_id: str) -> Optional[SessionState]:
        """Explicitly remove a session (for close_session, clear_context, delete).

        Returns the removed state, or None if not found.
        """
        state = self._pool.pop(session_id, None)
        if state is None:
            return None
        if state.is_connected and is_subprocess_alive(state):
            try:
                await graceful_disconnect(state.client, session_id)
            except Exception as e:
                logger.warning(f"[Pool] Error disconnecting session {session_id}: {e}")
        if state.agents_dir and state.written_agent_ids:
            from app.core.query_engine import cleanup_agents_directory
            cleanup_agents_directory(state.agents_dir, state.written_agent_ids)
        logger.info(f"[Pool] Removed session {session_id} (pool size: {len(self._pool)})")
        return state

    async def remove_dead(self, session_id: str) -> Optional[SessionState]:
        """Remove a dead session entry without attempting disconnect.

        Used when the subprocess has already died and we just need to clean up the entry.
        """
        state = self._pool.pop(session_id, None)
        if state is None:
            return None
        if state.agents_dir and state.written_agent_ids:
            from app.core.query_engine import cleanup_agents_directory
            cleanup_agents_directory(state.agents_dir, state.written_agent_ids)
        logger.info(f"[Pool] Removed dead session {session_id} (pool size: {len(self._pool)})")
        return state

    async def shutdown(self) -> None:
        """Disconnect all sessions. Called during server shutdown."""
        session_ids = list(self._pool.keys())
        for session_id in session_ids:
            try:
                await self.remove(session_id)
            except Exception as e:
                logger.warning(f"[Pool] Error during shutdown for session {session_id}: {e}")
        logger.info("[Pool] Shutdown complete")

    def contains(self, session_id: str) -> bool:
        return session_id in self._pool

    def __len__(self) -> int:
        return len(self._pool)

    def __contains__(self, session_id: str) -> bool:
        return session_id in self._pool

    def items(self):
        return self._pool.items()

    def active_session_ids(self) -> list:
        """Get list of session IDs with connected clients."""
        return [sid for sid, s in self._pool.items() if s.is_connected]

    def streaming_session_ids(self) -> list:
        """Get list of session IDs currently streaming."""
        return [sid for sid, s in self._pool.items() if s.is_streaming]


def requires_reconnect(state: SessionState, new_options: ClaudeAgentOptions) -> bool:
    """Check if option changes between queries require a fresh subprocess.

    Some options (model, permission_mode) can be changed at runtime via SDK methods.
    Others (system_prompt, cwd, mcp_servers, plugins) are CLI args set at spawn time
    and require a new subprocess.
    """
    old = state.connect_options
    if old is None:
        return True  # No previous options to compare — reconnect to be safe

    # These are immutable per-subprocess (CLI args at spawn time)
    if old.system_prompt != new_options.system_prompt:
        return True
    if old.cwd != new_options.cwd:
        return True
    if str(old.mcp_servers or {}) != str(new_options.mcp_servers or {}):
        return True
    if str(old.plugins or []) != str(new_options.plugins or []):
        return True
    if str(old.tools or []) != str(new_options.tools or []):
        return True

    # These can be changed at runtime — no reconnect needed
    # (model via client.set_model(), permission_mode via client.set_permission_mode())
    return False


# Global singleton — initialized lazily to avoid circular imports with settings
_session_pool: Optional[SessionPool] = None


def get_session_pool() -> SessionPool:
    """Get the global SessionPool singleton, creating it if needed."""
    global _session_pool
    if _session_pool is None:
        from app.core.config import settings
        pool_size = getattr(settings, 'sdk_session_pool_size', 3)
        _session_pool = SessionPool(max_size=pool_size)
        logger.info(f"[Pool] Initialized with max_size={pool_size}")
    return _session_pool
