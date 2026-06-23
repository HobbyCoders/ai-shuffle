"""
LRU Session Pool for AI provider sessions.

Keeps provider adapters alive between queries to avoid spawning a new
subprocess for every message. Bounded by max_size to limit memory usage.
Eviction happens lazily: only when the pool is full and a new session
needs to enter.

The lifecycle is: connect() -> [query() -> receive_response()]* -> disconnect()
This module keeps adapters in the [query -> receive_response]* phase between
user messages, avoiding repeated subprocess spawns and JSONL history replays.
"""

import asyncio
import logging
import os
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any

from claude_agent_sdk import ClaudeAgentOptions

from app.core.provider.claude_adapter import ClaudeAgentAdapter
from app.core.provider.types import mcp_servers_equivalent

logger = logging.getLogger(__name__)


@dataclass
class SessionState:
    """Track state for an active provider session.

    The adapter field holds the provider-agnostic interface. The `client`
    property provides backward-compatible access to the underlying SDK client
    during the migration period.

    - callback_refs: Mutable dict for indirect callback references (broadcast_func, etc.)
      These are updated at the start of each query so the can_use_tool callback
      (bound at connect() time) always references the current WebSocket.
    - connect_options: The ClaudeAgentOptions used at connect() time, for detecting
      whether a reconnect is needed when options change between queries.
    """
    adapter: ClaudeAgentAdapter
    sdk_session_id: Optional[str] = None
    is_connected: bool = False
    is_streaming: bool = False
    interrupt_requested: bool = False
    last_activity: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    streaming_started_at: Optional[datetime] = None  # Track when streaming began (for timeout detection)
    background_task: Optional[asyncio.Task] = None
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

    @property
    def client(self) -> Optional["ClaudeSDKClient"]:
        """Backward-compatible access to the underlying SDK client.

        TRANSITIONAL — streaming loops still call state.client.query(),
        state.client.receive_response(), etc. Will be removed once they
        migrate to adapter methods.
        """
        return self.adapter.sdk_client


def is_subprocess_alive(state: SessionState) -> bool:
    """Check if the subprocess for a session state is still running."""
    return state.adapter.is_alive()


def subprocess_pid(state: SessionState) -> Optional[int]:
    """Best-effort PID of the SDK subprocess for a session, or None.

    Used by the dead-tree reaper to record which process tree to reap once
    the SDK subprocess has terminated. Never raises.
    """
    try:
        client = state.adapter.sdk_client
        if client is None:
            return None
        from app.core.sdk_process_handle import SdkProcessHandle
        return SdkProcessHandle(client).pid
    except Exception:
        return None


async def graceful_disconnect(adapter: ClaudeAgentAdapter, session_id: str) -> None:
    """Disconnect the provider adapter, giving the subprocess time to flush JSONL writes.

    The adapter's disconnect() handles the full grace period logic:
    waiting for natural exit, SIGTERM, then SIGKILL if needed.
    """
    try:
        await adapter.disconnect()
        logger.debug(f"[Pool] Disconnected adapter for session {session_id}")
    except Exception as e:
        logger.warning(f"[Pool] Error during graceful disconnect for {session_id}: {e}")


# ─── Dead process-tree reaper ─────────────────────────────────────────────
#
# When an SDK subprocess terminates (natural exit, eviction-disconnect, or a
# silently-dead subprocess we removed), it can leave behind orphaned descendant
# processes — e.g. the node CLIs spawned for each subagent in a large fan-out.
# These orphans never get cleaned up by the normal lifecycle and show up as the
# "N child processes but pool size is M" leak.
#
# We record the PID of each TERMINATED session's subprocess into a bounded
# registry. reap_terminated_process_trees() then walks the descendants of those
# PIDs and kills any that are still alive. This only ever touches the process
# tree of a subprocess that has ALREADY terminated — it never signals a live
# session. (Note: once the parent PID is gone the OS reparents orphans, so we
# snapshot the descendant set at record time and reap by that snapshot.)

# Bounded set of (pid, [descendant_pids]) snapshots awaiting reaping.
_terminated_trees: "OrderedDict[int, list[int]]" = OrderedDict()
_TERMINATED_TREES_MAX = 256


def snapshot_descendants(pid: Optional[int]) -> list[int]:
    """Return the PIDs of all live descendants of `pid` (recursive), or [].

    Must be called WHILE the parent is still alive — once the parent exits the
    OS reparents orphans and they can no longer be discovered via the parent.
    Never raises.
    """
    if not pid:
        return []
    try:
        import psutil
    except ImportError:
        return []
    try:
        return [c.pid for c in psutil.Process(pid).children(recursive=True)]
    except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
        return []


def record_terminated_subprocess(pid: Optional[int], descendants: Optional[list[int]] = None) -> None:
    """Record a now-terminated SDK subprocess's descendant tree for reaping.

    Pass `descendants` captured via snapshot_descendants() BEFORE the parent
    exited; if omitted we try to snapshot now (only useful if the parent is
    still resolvable). Safe to call with None / without psutil — no-ops.
    """
    if not pid:
        return
    if descendants is None:
        descendants = snapshot_descendants(pid)
    if not descendants:
        return
    _terminated_trees[pid] = descendants
    _terminated_trees.move_to_end(pid)
    while len(_terminated_trees) > _TERMINATED_TREES_MAX:
        _terminated_trees.popitem(last=False)


def reap_terminated_process_trees() -> int:
    """Kill orphaned descendants of subprocesses that have already terminated.

    Walks each recorded terminated-subprocess descendant snapshot and, for any
    descendant PID that is BOTH still alive AND no longer has a live ancestor
    inside our own server process, sends SIGTERM (then SIGKILL on stragglers).

    This is process hygiene only — it operates exclusively on the recorded
    descendants of subprocesses that have already exited, so it can never kill
    a live session. Returns the number of orphans reaped.

    Safe no-op if psutil is unavailable or there is nothing to reap.
    """
    if not _terminated_trees:
        return 0
    try:
        import psutil
    except ImportError:
        _terminated_trees.clear()
        return 0

    reaped = 0
    own_pid = os.getpid()
    # PIDs of all SDK subprocesses we currently know are LIVE — descendants of
    # these must never be touched (they belong to an active session).
    live_subprocess_pids: set[int] = set()
    try:
        if _session_pool is not None:
            for st in _session_pool._pool.values():
                p = subprocess_pid(st)
                if p and is_subprocess_alive(st):
                    live_subprocess_pids.add(p)
    except Exception:
        pass

    def _has_live_protected_ancestor(proc: "psutil.Process") -> bool:
        """True if the process descends from a still-live SDK subprocess.

        Guards against killing a descendant that, after the original parent
        died, happens to share a PID-reuse or got reparented under a new live
        session. We walk up to our own server PID; if we cross a live SDK
        subprocess PID on the way, the process is protected.
        """
        try:
            cur = proc
            for _ in range(64):  # bounded walk
                ppid = cur.ppid()
                if ppid in live_subprocess_pids:
                    return True
                if ppid in (0, 1) or ppid == own_pid:
                    return False
                cur = psutil.Process(ppid)
        except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
            return False
        return False

    to_kill: list["psutil.Process"] = []
    for parent_pid, descendant_pids in list(_terminated_trees.items()):
        # If the recorded parent is somehow alive again, skip — only reap trees
        # whose parent has genuinely terminated.
        try:
            if psutil.pid_exists(parent_pid) and psutil.Process(parent_pid).is_running():
                # Parent still around (rare PID reuse) — leave its tree alone.
                _terminated_trees.pop(parent_pid, None)
                continue
        except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
            pass
        for dpid in descendant_pids:
            try:
                proc = psutil.Process(dpid)
            except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
                continue
            if _has_live_protected_ancestor(proc):
                continue
            to_kill.append(proc)
        _terminated_trees.pop(parent_pid, None)

    if not to_kill:
        return 0

    # SIGTERM first, then SIGKILL the stragglers.
    for proc in to_kill:
        try:
            proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
            continue
    gone, alive = psutil.wait_procs(to_kill, timeout=2.0)
    reaped += len(gone)
    for proc in alive:
        try:
            proc.kill()
            reaped += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
            continue

    if reaped:
        logger.warning(
            f"[Pool] Reaped {reaped} orphaned descendant process(es) of "
            f"terminated SDK subprocess(es)"
        )
    return reaped


class SessionPool:
    """
    Bounded LRU pool of SDK client sessions.

    - OrderedDict for O(1) LRU tracking (move_to_end on access)
    - Configurable max_size (default 3)
    - Hard cap at max_size * 2 to prevent unbounded growth when all sessions stream
    - Eviction only on insertion when full (lazy — no background timer)
    - Thread-safe within a single asyncio event loop
    """

    # Hard cap multiplier — even with all sessions streaming, never exceed this
    _HARD_CAP_MULTIPLIER = 2

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

    @property
    def hard_cap(self) -> int:
        """Absolute maximum pool size, even when all sessions are streaming."""
        return self._max_size * self._HARD_CAP_MULTIPLIER

    def is_at_hard_cap(self) -> bool:
        """Check if the pool has reached its absolute maximum size.

        Callers should check this before starting new sessions and reject
        with a "server busy" error if True.
        """
        return len(self._pool) >= self.hard_cap

    def admission_check(self) -> Optional[str]:
        """Admission backpressure: decide whether a NEW session may be admitted.

        This is the non-destructive alternative to a watchdog that reaps
        in-flight work. When the box is at a ceiling (pool hard cap, too many
        descendant processes from subagent fan-out, or RAM), we REFUSE to admit
        a new top-level session rather than killing a running one.

        Returns None if admission is allowed, or a human-readable refusal reason
        string if the new session should be rejected. Never terminates anything.
        """
        # 1) Pool hard cap (subprocess count for top-level sessions).
        if self.is_at_hard_cap():
            return (
                f"server busy: session pool at hard cap "
                f"({len(self._pool)}/{self.hard_cap})"
            )

        # 2) Process / RAM ceiling (covers subagent node subprocess fan-out,
        #    which is NOT counted by the pool). Configurable; disabled at 0.
        try:
            from app.core.config import settings
            max_children = getattr(settings, "sdk_max_child_processes", 0) or 0
            max_rss_mb = getattr(settings, "sdk_admission_rss_mb", 0) or 0
        except Exception:
            max_children = 0
            max_rss_mb = 0

        if max_children <= 0 and max_rss_mb <= 0:
            return None  # ceiling checks disabled

        try:
            import psutil
            proc = psutil.Process(os.getpid())
            if max_children > 0:
                child_count = len(proc.children(recursive=True))
                if child_count >= max_children:
                    return (
                        f"server busy: {child_count} descendant processes "
                        f">= ceiling {max_children}"
                    )
            if max_rss_mb > 0:
                tree_rss = proc.memory_info().rss
                for c in proc.children(recursive=True):
                    try:
                        tree_rss += c.memory_info().rss
                    except Exception:
                        continue
                tree_rss_mb = tree_rss / (1024 * 1024)
                if tree_rss_mb >= max_rss_mb:
                    return (
                        f"server busy: tree RSS {tree_rss_mb:.0f}MB "
                        f">= ceiling {max_rss_mb}MB"
                    )
        except ImportError:
            return None  # psutil unavailable — cannot enforce, allow
        except Exception:
            return None  # never block admission on a measurement error

        return None

    async def evict_if_needed(self) -> Optional[str]:
        """If pool is at capacity, evict the LRU non-streaming session.

        Returns the evicted session_id, or None if no eviction was needed/possible.

        IMPORTANT: Prefers non-streaming sessions for eviction. If all sessions
        are streaming, allows temporary over-capacity up to the hard cap
        (max_size * 2). Beyond the hard cap, forcibly evicts the LRU streaming
        session to prevent unbounded memory growth.
        """
        async with self._eviction_lock:
            if len(self._pool) < self._max_size:
                return None

            # Find the LRU session that is NOT streaming
            for session_id, state in self._pool.items():
                if not state.is_streaming:
                    return await self._do_evict(session_id, state)

            # All sessions are streaming — allow temporary over-capacity
            # up to the hard cap
            hard_cap = self.hard_cap
            if len(self._pool) < hard_cap:
                logger.warning(
                    f"[Pool] At capacity ({self._max_size}) with all sessions streaming. "
                    f"Allowing temporary over-capacity ({len(self._pool)}/{hard_cap} sessions)."
                )
                return None

            # Hard cap reached — forcibly evict the LRU session (even if streaming)
            # to prevent unbounded memory/subprocess growth
            lru_session_id, lru_state = next(iter(self._pool.items()))
            logger.error(
                f"[Pool] Hard cap reached ({hard_cap}). Force-evicting LRU streaming "
                f"session {lru_session_id} to prevent resource exhaustion."
            )
            return await self._do_evict(lru_session_id, lru_state)

    async def _do_evict(self, session_id: str, state: SessionState) -> str:
        """Evict a specific session. Returns the evicted session_id."""
        del self._pool[session_id]
        # Snapshot the subprocess PID + descendant tree BEFORE disconnect (while
        # the parent is still alive and its children are discoverable) so the
        # dead-tree reaper can clean up any orphaned descendants (e.g. subagent
        # node CLIs) that outlive the parent after disconnect terminates it.
        pid = subprocess_pid(state)
        descendants = snapshot_descendants(pid)
        if state.is_connected and is_subprocess_alive(state):
            try:
                await graceful_disconnect(state.adapter, session_id)
            except Exception as e:
                logger.warning(f"[Pool] Error during eviction disconnect for {session_id}: {e}")
        record_terminated_subprocess(pid, descendants)
        logger.info(f"[Pool] Evicted session {session_id} (pool size: {len(self._pool)})")
        return session_id

    async def remove(self, session_id: str) -> Optional[SessionState]:
        """Explicitly remove a session (for close_session, clear_context, delete).

        Returns the removed state, or None if not found.
        """
        state = self._pool.pop(session_id, None)
        if state is None:
            return None
        pid = subprocess_pid(state)
        descendants = snapshot_descendants(pid)
        if state.is_connected and is_subprocess_alive(state):
            try:
                await graceful_disconnect(state.adapter, session_id)
            except Exception as e:
                logger.warning(f"[Pool] Error disconnecting session {session_id}: {e}")
        record_terminated_subprocess(pid, descendants)
        logger.info(f"[Pool] Removed session {session_id} (pool size: {len(self._pool)})")
        return state

    async def remove_dead(self, session_id: str) -> Optional[SessionState]:
        """Remove a dead session entry without attempting disconnect.

        Used when the subprocess has already died and we just need to clean up the entry.
        """
        state = self._pool.pop(session_id, None)
        if state is None:
            return None
        logger.info(f"[Pool] Removed dead session {session_id} (pool size: {len(self._pool)})")
        return state

    async def evict_idle(self, max_idle_seconds: int = 600) -> list[str]:
        """Evict sessions that have been idle (not streaming) for too long.

        This prevents idle SDK subprocesses from consuming server resources
        indefinitely. Called by the resource monitor's periodic cleanup.

        Args:
            max_idle_seconds: Maximum idle time before eviction (default: 10 minutes).

        Returns:
            List of evicted session IDs.
        """
        now = datetime.now(timezone.utc)
        evicted = []
        # Snapshot keys to avoid mutation during iteration
        for session_id in list(self._pool.keys()):
            state = self._pool.get(session_id)
            if state is None:
                continue
            # Never evict sessions that are currently streaming
            if state.is_streaming:
                continue
            idle_seconds = (now - state.last_activity).total_seconds()
            if idle_seconds >= max_idle_seconds:
                await self._do_evict(session_id, state)
                evicted.append(session_id)
                logger.info(
                    f"[Pool] Idle-evicted session {session_id} "
                    f"(idle {idle_seconds:.0f}s > {max_idle_seconds}s)"
                )
        return evicted

    async def evict_dead(self) -> list[str]:
        """Evict sessions whose subprocess has died silently.

        SDK subprocesses can die without the pool being notified (e.g., OOM kill,
        signal). This method checks ALL sessions — including streaming ones — and
        removes those with dead subprocesses.

        Previously, streaming sessions were skipped, which meant an OOM-killed
        subprocess during streaming would leave is_streaming=True forever,
        preventing all eviction paths from cleaning it up.

        Returns:
            List of evicted session IDs.
        """
        evicted = []
        for session_id in list(self._pool.keys()):
            state = self._pool.get(session_id)
            if state is None:
                continue
            if state.is_connected and not is_subprocess_alive(state):
                was_streaming = state.is_streaming
                pid = subprocess_pid(state)
                del self._pool[session_id]
                # Subprocess is already dead, so its descendants are already
                # orphaned (reparented to init). Best-effort record so the
                # reaper still tries; descendants may be empty here.
                record_terminated_subprocess(pid)
                if was_streaming:
                    logger.warning(
                        f"[Pool] Removed dead STREAMING subprocess for session {session_id} "
                        f"(subprocess died during active stream, pool size: {len(self._pool)})"
                    )
                else:
                    logger.warning(
                        f"[Pool] Removed dead subprocess for session {session_id} "
                        f"(pool size: {len(self._pool)})"
                    )
                evicted.append(session_id)
        return evicted

    def report_long_streaming(self, warn_after_seconds: int = 600) -> list[tuple[str, float]]:
        """OBSERVE-ONLY report of sessions that have been streaming a long time.

        WHY THIS IS NOT AN EVICTION ANYMORE
        -----------------------------------
        Elapsed wall-clock streaming time cannot distinguish a legitimately busy
        long orchestration (e.g. driving 30+ subagents overnight) from a
        genuinely hung session. The previous `evict_stuck_streaming` force-
        terminated LIVE sessions on a timer, which in production SIGTERM'd a
        healthy orchestrator mid-run, discarded its in-memory transcript,
        truncated the Postgres mirror, and orphaned the subagent child trees —
        i.e. the watchdog CREATED the very leak it existed to detect.

        The SDK owns query lifecycle and is the ONLY thing that ends a live
        session (completion or user interrupt). Genuinely dead subprocesses are
        reaped by evict_dead() + the dead-tree reaper. This method therefore
        only OBSERVES: it returns (session_id, streaming_seconds) for sessions
        streaming longer than warn_after_seconds so the monitor can log a
        warning. It NEVER evicts, disconnects, or signals a live session.

        Returns:
            List of (session_id, streaming_seconds) for long-streaming sessions.
        """
        now = datetime.now(timezone.utc)
        report: list[tuple[str, float]] = []
        for session_id in list(self._pool.keys()):
            state = self._pool.get(session_id)
            if state is None:
                continue
            if not state.is_streaming:
                continue
            started = state.streaming_started_at
            if started is None:
                # Legacy session without timestamp — use last_activity as fallback
                started = state.last_activity
            streaming_seconds = (now - started).total_seconds()
            if streaming_seconds >= warn_after_seconds:
                report.append((session_id, streaming_seconds))
        return report

    async def evict_orphaned_worktree_sessions(self) -> list[str]:
        """Evict sessions whose working directory no longer exists.

        Safety net for worktree sessions that weren't cleaned up properly —
        e.g., worktree removed via CLI, or terminate_worktree_sessions() was
        skipped due to a crash. If the CWD recorded at connect time no longer
        exists on disk, the session is useless and should be evicted.

        Returns:
            List of evicted session IDs.
        """
        evicted = []
        for session_id in list(self._pool.keys()):
            state = self._pool.get(session_id)
            if state is None:
                continue
            if state.is_streaming:
                continue
            # Check if the session's CWD still exists
            cwd = getattr(state.connect_options, 'cwd', None) if state.connect_options else None
            if cwd and not Path(cwd).exists():
                await self._do_evict(session_id, state)
                evicted.append(session_id)
                logger.info(
                    f"[Pool] Evicted orphaned worktree session {session_id} "
                    f"(cwd no longer exists: {cwd})"
                )
        return evicted

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


def _agents_equivalent(old: Optional[Dict], new: Optional[Dict]) -> bool:
    """Compare agent dicts by their serialisable fields.

    AgentDefinition dataclasses don't implement __eq__, so we compare
    the fields that affect subprocess behaviour: description, prompt,
    tools, and model.
    """
    if old is None and new is None:
        return True
    if old is None or new is None:
        return False
    if set(old.keys()) != set(new.keys()):
        return False
    for key in old:
        a, b = old[key], new[key]
        if (getattr(a, 'description', None) != getattr(b, 'description', None)
                or getattr(a, 'prompt', None) != getattr(b, 'prompt', None)
                or getattr(a, 'tools', None) != getattr(b, 'tools', None)
                or getattr(a, 'model', None) != getattr(b, 'model', None)):
            return False
    return True


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
    if not mcp_servers_equivalent(old.mcp_servers, new_options.mcp_servers):
        return True
    if (old.plugins or []) != (new_options.plugins or []):
        return True
    if (old.tools or []) != (new_options.tools or []):
        return True
    if not _agents_equivalent(old.agents, new_options.agents):
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
