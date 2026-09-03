"""
LRU Session Pool for AI provider sessions.

Keeps provider adapters alive between queries to avoid spawning a new
subprocess for every message. Bounded by max_size to limit memory usage.

Eviction has two triggers:
  * LRU pressure — the pool is full and a new session needs to enter; and
  * the TIERED IDLE POLICY (:class:`IdlePolicy`) — a recency tier, a
    last-device-disconnect grace, and an activity floor, enforced by a
    deadline-driven watchdog that sleeps exactly until the next deadline (and
    not at all while the pool is empty), so a subprocess's ~100-300MB is
    released when it goes stale rather than on some unrelated 5-minute tick.

The lifecycle is: connect() -> [query() -> receive_response()]* -> disconnect()
This module keeps adapters in the [query -> receive_response]* phase between
user messages, avoiding repeated subprocess spawns and JSONL history replays.
"""

import asyncio
import logging
import os
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, Callable, Awaitable

from claude_agent_sdk import ClaudeAgentOptions

from app.core.provider.claude_adapter import ClaudeAgentAdapter
from app.core.provider.types import mcp_servers_equivalent

logger = logging.getLogger(__name__)

# Env keys that carry provider identity/config (base URL, auth, model overlays).
# A change to any of these means the subprocess is talking to a different
# provider/account and must be respawned — this is the CANONICAL definition
# (mirrored, not imported, in app.core.provider.claude_adapter to avoid an
# import cycle: session_pool already imports ClaudeAgentAdapter at module load).
# Deliberately NOT full env-dict equality (benign env churn would force
# respawns that kill background subagents) and NOT a narrower 3-key tuple
# (would miss provider config edits) — see docs/MULTI_PROVIDER_PLAN.md R3.
#
# SPLIT into identity vs overlay keys (see provider_env_requires_reconnect):
#   * IDENTITY keys select the provider/account and are ALWAYS compared.
#   * OVERLAY keys (small/fast + subagent model, output/compaction tuning,
#     upstream-mitigation flags) are only meaningful for a PROVIDER (gateway)
#     session; comparing them on a NATIVE session wrongly forces a respawn.
#     Concretely: native sessions no longer emit CLAUDE_CODE_AUTO_COMPACT_WINDOW
#     at all (the CLI derives per-model windows), but a profile-preset value can
#     still appear/disappear across native model switches, which must stay a
#     hot-swap (respawning kills every background subagent). So overlay keys are
#     compared ONLY when either side is a provider session.
PROVIDER_IDENTITY_ENV_KEYS = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
)
PROVIDER_OVERLAY_ENV_KEYS = (
    "ANTHROPIC_SMALL_FAST_MODEL",
    # TRANSITION GUARD (S6). Dead as a write-target: since S3, subagent model
    # selection moved to per-agent frontmatter stamping and NOTHING emits this
    # var anymore (see app/core/environment.py — it is intentionally not set).
    # It stays in this overlay set ONLY to purge pre-S3 pooled sessions: such a
    # session still carries CLAUDE_CODE_SUBAGENT_MODEL in its old_env, and S0
    # proved the env var OVERRIDES every frontmatter pin — so a surviving pinned
    # session would silently ignore per-subagent config. Keeping the key here
    # makes the key-present→absent diff force the one respawn that drops the pin.
    # Do NOT remove until no pre-S3 pooled session can plausibly still be alive
    # (a future cleanup, not now).
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
    "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
    "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    "CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK",
)
# Kept as the union for existing importers/tests.
PROVIDER_ENV_KEYS = PROVIDER_IDENTITY_ENV_KEYS + PROVIDER_OVERLAY_ENV_KEYS


def provider_env_requires_reconnect(
    old_env: Optional[Dict[str, str]], new_env: Optional[Dict[str, str]]
) -> bool:
    """True when a provider env change between queries requires a fresh subprocess.

    Identity keys (base URL / auth) are always compared. Overlay keys are
    compared ONLY when either side is a provider (gateway) session — detected by
    KEY PRESENCE of ``ANTHROPIC_BASE_URL`` (presence, not truthiness), since a
    provider overlay always sets it. This keeps native model switches — where
    overlay-key values (e.g. a profile-preset ``CLAUDE_CODE_AUTO_COMPACT_WINDOW``)
    can appear or disappear — as hot-swaps rather than subagent-killing respawns.

    Logs the differing key NAME only, never its value (provider env carries the
    decrypted auth token).

    POST-S3 LEG SEMANTICS (S6) — the designed contract, DO NOT "fix":
    With frontmatter stamping, the ONLY leg-driven respawn is the legs
    empty↔non-empty flip on an anthropic-provider main, because that flips
    ANTHROPIC_BASE_URL identity between direct and gateway (caught by the
    identity-key loop above). A leg-CONTENT change (same non-empty legs,
    different subagent models/providers) re-mints the SAME gateway token →
    byte-identical env → this function returns False → no respawn. That is
    correct: the gateway leg-map refresh takes effect IMMEDIATELY for routing,
    and frontmatter stamping takes effect on the NEXT spawn. Profile-default-leg
    edits are likewise next-spawn-effective. This is ACCEPTED behavior, not a
    bug. A future maintainer must NEVER add any leg-derived value into
    PROVIDER_OVERLAY_ENV_KEYS or into the emitted env content to make
    leg-content changes respawn — that would churn a subagent-killing respawn on
    every mapping edit. Immediate-routing + next-spawn-stamping is the contract.
    """
    old_env = old_env or {}
    new_env = new_env or {}

    for key in PROVIDER_IDENTITY_ENV_KEYS:
        if old_env.get(key) != new_env.get(key):
            logger.debug(
                f"[SessionPool] requires_reconnect: provider identity env key differs: {key}"
            )
            return True

    # Overlay keys only matter for a provider session on either side.
    is_provider = ("ANTHROPIC_BASE_URL" in old_env) or ("ANTHROPIC_BASE_URL" in new_env)
    if is_provider:
        for key in PROVIDER_OVERLAY_ENV_KEYS:
            if old_env.get(key) != new_env.get(key):
                logger.debug(
                    f"[SessionPool] requires_reconnect: provider overlay env key differs: {key}"
                )
                return True

    return False


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
    # True while the between-turns idle reader (app/core/idle_reader.py) holds
    # this pooled client. Set at reader start and cleared in the reader's exit
    # ``finally`` (run_idle_reader), so a crashed or cancelled reader can never
    # leave it stuck. Checked by the LRU/idle eviction guards: during the
    # CLI-wake grace window the reader legitimately holds the client with
    # ``is_streaming`` False and ZERO running background work (it is waiting up
    # to ``cli_wake_grace_seconds`` for the CLI's own delayed wake turn), and it
    # reads via ``peek`` (no LRU promote) so ``last_activity`` never bumps —
    # without this flag either eviction path would tear the subprocess down
    # mid-wait and kill the wake.
    idle_reader_active: bool = False
    last_activity: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # When the LAST device (browser/app) watching this session disconnected, or
    # None while a device is attached / while attachment is unknown. Stamped by
    # SessionPool.note_session_detached (pushed by the sync engine) and/or by the
    # pool's own attachment probe during a watchdog re-evaluation; cleared by
    # note_session_attached and by any put() (a query is running, so somebody is
    # driving this session). Feeds the DETACHED leg of the idle policy — see
    # IdlePolicy.detached_grace_seconds.
    detached_since: Optional[datetime] = None
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

    # Live capture list for this subprocess's stderr, shared with the wrapped
    # stderr callback registered at connect() time (see _wrap_stderr_capture).
    # Persisted here so the streaming exception handlers can log the captured
    # CLI output at ERROR when the subprocess dies MID-STREAM — at that point
    # the connect-scoped cli_stderr_lines local is long out of scope, which is
    # why mid-stream exit-1 crashes previously recorded no reason. None until a
    # client is created (a reused pooled client keeps the ring from creation).
    # A bounded ``deque(maxlen=50)`` (see _wrap_stderr_capture) — an unbounded
    # list leaked for the subprocess's whole lifetime.
    cli_stderr_lines: "Optional[deque[str]]" = None

    @property
    def client(self) -> Optional["ClaudeSDKClient"]:
        """Backward-compatible access to the underlying SDK client.

        TRANSITIONAL — streaming loops still call state.client.query(),
        state.client.receive_response(), etc. Will be removed once they
        migrate to adapter methods.
        """
        # Fluid adapters have no SDK client; callers must handle None.
        return getattr(self.adapter, "sdk_client", None)


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


def _host_available_mb() -> Optional[float]:
    """Host MemAvailable in MB from /proc/meminfo, or None if unavailable.

    MemAvailable already accounts for reclaimable page cache, so it is a good
    proxy for "how close is the WHOLE host to OOM" — which, on a shared host
    with little/no swap, is exactly what trips the global kernel OOM-killer that
    reaps our subprocesses. Whole-host, every-container view. Returns None on any
    error (non-Linux, missing file) so callers fail open.
    """
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024.0
    except Exception:
        return None
    return None


def mark_subprocess_oom_killable(state: SessionState) -> None:
    """Make the SDK subprocess the kernel's preferred OOM-kill victim.

    Writes a positive ``oom_score_adj`` to the subprocess; subagents it forks
    afterward inherit the value. Under a host OOM the kernel then reaps one
    session's disposable, auto-recoverable node subprocess instead of the Python
    orchestrator (PID 1, which runs EVERY session) or the database. Raising the
    score needs no privilege — only lowering does — so this works in an
    unprivileged container. Best-effort: never raises, never blocks a turn.
    """
    try:
        from app.core.config import settings
        adj = int(getattr(settings, "sdk_subprocess_oom_score_adj", 0) or 0)
    except Exception:
        adj = 0
    if adj <= 0:
        return
    pid = subprocess_pid(state)
    if not pid:
        return
    try:
        with open(f"/proc/{pid}/oom_score_adj", "w") as f:
            f.write(str(min(adj, 1000)))
    except Exception:
        pass  # non-Linux, missing /proc, or denied — degrade silently


async def graceful_disconnect(adapter: ClaudeAgentAdapter, session_id: str) -> None:
    """Disconnect the provider adapter, giving the subprocess time to flush JSONL writes.

    The adapter's disconnect() handles the full grace period logic:
    waiting for natural exit, SIGTERM, then SIGKILL if needed.
    """
    try:
        # Pass the session id so the adapter can flag an intentional teardown:
        # the SDK reader-task "Fatal error in message reader" that a force-
        # terminate provokes is then demoted to WARNING instead of persisted as
        # a durable error_log ERROR (see app/core/expected_shutdown.py).
        await adapter.disconnect(session_id=session_id)
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
            # Snapshot — the pool dict can be mutated (evictions/removals) while
            # this loop runs; iterating a live view would raise RuntimeError and
            # abort with a PARTIAL live-pid set, risking a live session's tree.
            for st in list(_session_pool._pool.values()):
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


# ─── Tiered idle policy (F1) ──────────────────────────────────────────────
#
# One pooled session == one long-lived Node CLI subprocess at 100-300+MB RSS,
# so the pool's steady-state occupancy IS the app's idle-RAM floor. The policy
# below is the SINGLE place that decides when a subprocess has outlived its
# usefulness; nothing else in the codebase should carry an idle constant.
#
# Three legs, evaluated together, earliest deadline wins:
#
#   * TIER   — the ``hot_size`` most-recently-used sessions keep the long
#     ``hot_idle_seconds``; everything ranked below them is COLD and gets the
#     much shorter ``cold_idle_seconds``. A flat timeout has to be set for the
#     session you are most likely to return to, which means paying that timeout
#     for every session you are NOT going to return to. Ranking by recency is
#     precisely the signal that separates the two.
#
#   * DETACHED — once the last device watching a session disconnects, its
#     subprocess is evicted after ``detached_grace_seconds`` regardless of tier.
#     A session nobody is watching cannot receive a UI message without a
#     reconnect first, and that reconnect is slower than the respawn it would
#     have saved. The grace absorbs page reloads and brief network drops.
#
#   * FLOOR  — no session is ever evicted within ``min_idle_floor_seconds`` of
#     its last activity, whatever the other legs say. This is the guard against
#     an over-eager detach: a WS flap between two turns of a live conversation
#     must not cost the subprocess.
#
# Eviction never overrides the pool's existing busy guards (streaming, idle
# reader, in-flight background work) — those are checked first and independently.


@dataclass(frozen=True)
class IdlePolicy:
    """Declarative idle/eviction policy for the session pool.

    Immutable so a policy can be swapped atomically (tests, runtime reconfig)
    without a half-applied state. Build the configured one with
    :meth:`from_settings`.
    """

    hot_size: int = 2
    hot_idle_seconds: float = 1800.0
    cold_idle_seconds: float = 420.0
    detached_grace_seconds: float = 180.0
    min_idle_floor_seconds: float = 60.0
    reevaluate_seconds: float = 60.0

    @classmethod
    def from_settings(cls, settings: Any = None) -> "IdlePolicy":
        """Build the policy from app settings, falling back to the defaults.

        Never raises: a missing/garbled setting degrades to this class's default
        rather than leaving the pool with no policy at all.
        """
        if settings is None:
            try:
                from app.core.config import settings as _settings
                settings = _settings
            except Exception:  # pragma: no cover - config import is not optional
                return cls()

        def _num(name: str, default: float) -> float:
            try:
                value = getattr(settings, name, None)
                if value is None:
                    return default
                value = float(value)
            except (TypeError, ValueError):
                return default
            return value if value >= 0 else default

        return cls(
            hot_size=max(0, int(_num("sdk_session_hot_pool_size", cls.hot_size))),
            hot_idle_seconds=_num(
                "sdk_session_idle_timeout_seconds", cls.hot_idle_seconds
            ),
            cold_idle_seconds=_num(
                "sdk_session_cold_idle_timeout_seconds", cls.cold_idle_seconds
            ),
            detached_grace_seconds=_num(
                "sdk_session_detached_grace_seconds", cls.detached_grace_seconds
            ),
            min_idle_floor_seconds=cls.min_idle_floor_seconds,
            reevaluate_seconds=_num(
                "sdk_session_idle_reevaluate_seconds", cls.reevaluate_seconds
            ),
        )

    def tier_idle_seconds(self, rank_from_mru: int) -> float:
        """Idle allowance for a session at ``rank_from_mru`` (0 == most recent).

        A cold timeout longer than the hot one would be nonsense (it would make
        an old session outlive a fresh one), so the cold leg is clamped.
        """
        if rank_from_mru < self.hot_size:
            return self.hot_idle_seconds
        return min(self.cold_idle_seconds, self.hot_idle_seconds)


def _default_attachment_probe(session_id: str) -> Optional[bool]:
    """Best-effort "is a device watching this session right now?".

    Returns True/False, or None when the answer cannot be determined — the
    caller MUST treat None as "assume attached" and leave any existing detach
    stamp alone, so a sync-engine hiccup can never manufacture an eviction.

    Lazy-imported: the pool must not depend on the WS layer at module load.
    """
    try:
        from app.core.sync_engine import sync_engine

        return sync_engine.get_device_count(session_id) > 0
    except Exception:
        return None


class SessionPool:
    """
    Bounded LRU pool of SDK client sessions.

    - OrderedDict for O(1) LRU tracking (move_to_end on access)
    - Configurable max_size (``settings.sdk_session_pool_size``)
    - Hard cap at max_size * 2 to prevent unbounded growth when all sessions stream
    - Eviction on insertion when full (LRU) AND on the tiered idle policy's
      deadlines (see :class:`IdlePolicy` and :meth:`start_idle_watchdog`)
    - Thread-safe within a single asyncio event loop
    """

    # Hard cap multiplier — even with all sessions streaming, never exceed this
    _HARD_CAP_MULTIPLIER = 2

    def __init__(
        self, max_size: int = 3, idle_policy: Optional[IdlePolicy] = None
    ):
        self._pool: OrderedDict[str, SessionState] = OrderedDict()
        self._max_size = max(1, max_size)
        self._eviction_lock = asyncio.Lock()
        # Tiered idle policy (F1) — the single source of truth for "when may
        # this subprocess be reclaimed". Defaults are used when no settings are
        # available (tests construct bare pools).
        self._idle_policy: IdlePolicy = idle_policy or IdlePolicy()
        # Attachment probe: "is a device watching this session?" (None = unknown).
        self._attachment_probe: Callable[[str], Optional[bool]] = (
            _default_attachment_probe
        )
        # Idle watchdog plumbing. The watchdog sleeps until the NEXT known
        # eviction deadline instead of riding the resource monitor's 5-minute
        # tick, so RAM is released on time; it sleeps forever (no wake at all)
        # while the pool is empty.
        self._idle_task: Optional[asyncio.Task] = None
        self._idle_wake: Optional[asyncio.Event] = None
        # Optional async hook fired from _do_evict AFTER a session leaves the pool
        # (LRU + idle paths, which both funnel through _do_evict). It lets a higher
        # layer tear down the per-session artifacts the pool itself knows nothing
        # about — the live-store message buffer and the task registry — so a
        # session's lifecycle is driven by ONE clock (pool membership) instead of a
        # separate cleanup timer. See SessionPool.set_eviction_callback and the
        # resource_monitor wiring. The pool stays decoupled: it never imports the
        # live store or registry; the callback is injected at startup.
        # NOTE: dead-eviction (evict_dead) does NOT go through _do_evict and is
        # cleaned by the resource monitor directly, so it does not double-fire here.
        self._eviction_callback: Optional[Callable[[str, str], Awaitable[None]]] = None
        # Strong refs to remove_broken's fire-and-forget teardown tasks — the
        # event loop only holds weak refs, so an unreferenced task can be GC'd
        # mid-disconnect.
        self._background_teardowns: set[asyncio.Task] = set()

    def set_eviction_callback(
        self, callback: Optional[Callable[[str, str], Awaitable[None]]]
    ) -> None:
        """Register the async hook fired after a session is evicted via _do_evict.

        The callback receives ``(session_id, reason)`` where reason ∈
        {"lru", "lru_force", "idle"}. Injected at startup so the pool need not
        import the live store or task registry. Passing ``None`` clears it.
        """
        self._eviction_callback = callback

    def _has_background_work(self, session_id: str) -> bool:
        """True iff in-flight background work is still running for this session.

        Covers BOTH a backgrounded sub-agent (``kind=="agent"``) AND an
        SDK-backgrounded shell command (``local_bash`` run_in_background, tracked
        in the registry's ``_sdk_background_tasks``) — both park the parent turn and
        deliver their completion LATER on the pooled SDK client.

        Lazy-imports the task registry (a leaf module that imports nothing back)
        to avoid an import cycle while keeping ONE source of truth for the busy
        signal. A session parked on such work looks idle to ``is_streaming`` (its
        parent turn already returned), so without this check LRU/idle eviction would
        disconnect the pooled SDK client out from under the running work — killing it
        mid-run, dropping the completion notification, and stranding the parent so the
        frontend never unparks. Defensive: any failure resolves to ``False`` (never
        block eviction on a registry hiccup; the hard-cap force path remains the
        ultimate bound).
        """
        try:
            from app.core.task_registry import task_registry
            return task_registry.has_active_background_work(session_id)
        except Exception:
            return False

    @property
    def max_size(self) -> int:
        return self._max_size

    @max_size.setter
    def max_size(self, value: int):
        self._max_size = max(1, value)

    # ─── Idle policy / attachment ────────────────────────────────────────

    @property
    def idle_policy(self) -> IdlePolicy:
        return self._idle_policy

    @idle_policy.setter
    def idle_policy(self, policy: IdlePolicy) -> None:
        self._idle_policy = policy
        self._wake_idle_watchdog()

    def set_attachment_probe(
        self, probe: Optional[Callable[[str], Optional[bool]]]
    ) -> None:
        """Replace the "is a device watching this session?" probe.

        ``None`` restores the default (sync-engine) probe. A probe MUST return
        ``None`` when it cannot tell — the pool treats unknown as attached.
        """
        self._attachment_probe = probe or _default_attachment_probe

    def note_session_attached(self, session_id: str) -> None:
        """A device (re)connected to this session — clear any detach stamp.

        Push hook for the WS layer. Safe to call for a session that is not
        pooled, and safe to call repeatedly.
        """
        state = self._pool.get(session_id)
        if state is not None and state.detached_since is not None:
            state.detached_since = None
            self._wake_idle_watchdog()

    def note_session_detached(self, session_id: str) -> None:
        """The LAST device watching this session disconnected.

        Starts the detach grace (see IdlePolicy.detached_grace_seconds) and
        wakes the watchdog so the eviction is scheduled for the grace deadline
        rather than discovered on some later tick. Idempotent: an existing
        stamp is kept, so repeated calls do not extend the grace.
        """
        state = self._pool.get(session_id)
        if state is None:
            return
        if state.detached_since is None:
            state.detached_since = datetime.now(timezone.utc)
            self._wake_idle_watchdog()

    def get(self, session_id: str) -> Optional[SessionState]:
        """Get session state, promoting to MRU position. Returns None if not found."""
        state = self._pool.get(session_id)
        if state is not None:
            # A promotion re-ranks the pool, which can demote another session
            # from the hot tier to the cold one (and so pull its deadline in).
            was_mru = next(reversed(self._pool)) == session_id
            self._pool.move_to_end(session_id)
            if not was_mru:
                self._wake_idle_watchdog()
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
        # A query is running against this session, so it is not abandoned:
        # clear any detach stamp left by a probe that ran while no browser
        # happened to be attached (automations, API-driven turns).
        state.detached_since = None
        self._wake_idle_watchdog()

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

        # 2) Host memory floor. The crash that stops live sessions is the GLOBAL
        #    kernel OOM-killer (NOT a cgroup limit — this container has none) SIGKILLing
        #    our node subprocesses when the WHOLE host is out of memory, which is
        #    catastrophic with little/no swap. When host MemAvailable is below the
        #    floor, refuse to admit a NEW top-level session so we stop piling on at
        #    the cliff — existing sessions and their subagent fan-out keep running.
        #    This is the only check that sees the whole host (every container), not
        #    just our own tree. Fails open (None) on any read error.
        try:
            from app.core.config import settings
            min_avail_mb = getattr(settings, "sdk_admission_min_available_mb", 0) or 0
        except Exception:
            min_avail_mb = 0
        if min_avail_mb > 0:
            avail_mb = _host_available_mb()
            if avail_mb is not None and avail_mb < min_avail_mb:
                return (
                    f"host low on memory: {avail_mb:.0f}MB available "
                    f"< floor {min_avail_mb}MB"
                )

        # 3) Process / RAM ceiling (covers subagent node subprocess fan-out,
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

        LOCK SCOPE: `_eviction_lock` (non-reentrant asyncio.Lock) guards ONLY
        victim selection + removal from the pool dict — a critical section with
        NO awaits, so it can never be held across a context switch. The slow
        subprocess teardown + eviction callback run AFTER the lock is released
        (see _finish_evict), so concurrent new-session admissions are never
        stalled behind a multi-second disconnect and a callback that re-enters
        the pool cannot deadlock on the lock.
        """
        async with self._eviction_lock:
            if len(self._pool) < self._max_size:
                return None

            victim: Optional[tuple[str, SessionState, str]] = None

            # Find the LRU session that is neither streaming, NOR held by the
            # between-turns idle reader, NOR parked on an in-flight background
            # sub-agent. Evicting any of these would disconnect the pooled SDK
            # client out from under live work: a streaming turn, a backgrounded
            # sub-agent whose parent turn already returned (parked), or — the
            # idle-reader case — the CLI-wake grace window, where the reader
            # legitimately holds the client with zero running background work
            # while it waits for the CLI's delayed wake turn (evicting mid-wait
            # kills the wake). Snapshot the items so a mutation from
            # concurrently-scheduled code (remove/remove_broken/evict_dead run
            # WITHOUT this lock) can never raise "dictionary changed size during
            # iteration".
            for session_id, state in list(self._pool.items()):
                if (
                    not state.is_streaming
                    and not state.idle_reader_active
                    and not self._has_background_work(session_id)
                ):
                    victim = (session_id, state, "lru")
                    break

            if victim is None:
                # Every session is busy (streaming, held by an idle reader, or
                # running a background sub-agent) — allow temporary
                # over-capacity up to the hard cap.
                hard_cap = self.hard_cap
                if len(self._pool) < hard_cap:
                    logger.warning(
                        f"[Pool] At capacity ({self._max_size}) with all sessions busy "
                        f"(streaming or background work). Allowing temporary over-capacity "
                        f"({len(self._pool)}/{hard_cap} sessions)."
                    )
                    return None

                # Hard cap reached — forcibly evict the LRU session even if busy, to
                # prevent unbounded memory/subprocess growth. This is the ultimate
                # bound: it CAN kill a parked background sub-agent, but only when the
                # box is already over 2x capacity with every session busy.
                lru_session_id, lru_state = next(iter(self._pool.items()))
                logger.error(
                    f"[Pool] Hard cap reached ({hard_cap}). Force-evicting LRU busy "
                    f"session {lru_session_id} to prevent resource exhaustion."
                )
                victim = (lru_session_id, lru_state, "lru_force")

            # Remove the entry while still holding the lock so concurrent
            # evict_if_needed callers immediately see the freed slot.
            session_id, state, reason = victim
            self._pool.pop(session_id, None)

        # Slow work (subprocess teardown, eviction callback) happens OUTSIDE the
        # critical section. The evicted id is still returned only after teardown
        # completes, so per-caller semantics are unchanged.
        return await self._finish_evict(session_id, state, reason)

    async def _do_evict(
        self, session_id: str, state: SessionState, reason: str = "evicted"
    ) -> str:
        """Evict a specific session. Returns the evicted session_id.

        After the subprocess is torn down, fires ``_eviction_callback`` (if set)
        so the higher layer can clean up the live-store buffer + task registry for
        this session. This is the single funnel for LRU + idle eviction, which is
        why wiring cleanup here covers both without a separate timer. The callback
        is best-effort: its failure never propagates out of eviction.

        NOTE: does NOT take ``_eviction_lock`` — callers (evict_idle,
        evict_orphaned_worktree_sessions) run without it, and the removal itself
        is a single sync dict op (atomic on the event loop). evict_if_needed does
        its own locked removal and calls _finish_evict directly.
        """
        self._pool.pop(session_id, None)
        return await self._finish_evict(session_id, state, reason)

    async def _finish_evict(
        self, session_id: str, state: SessionState, reason: str
    ) -> str:
        """Teardown half of eviction: disconnect the subprocess + fire the callback.

        The pool entry has ALREADY been removed by the caller. Deliberately runs
        outside ``_eviction_lock``: graceful_disconnect can take multiple seconds
        (exit-grace/SIGTERM/SIGKILL) and the eviction callback is arbitrary
        higher-layer code — holding the lock across either would stall every
        new-session admission and risk deadlock if the callback re-enters the
        pool (the lock is non-reentrant).
        """
        # Snapshot the subprocess PID + descendant tree BEFORE disconnect (while
        # the parent is still alive and its children are discoverable) so the
        # dead-tree reaper can clean up any orphaned descendants (e.g. subagent
        # node CLIs) that outlive the parent after disconnect terminates it.
        pid = subprocess_pid(state)
        # psutil's recursive children() scans the whole process table — off
        # the loop (this runs on the new-session admission path).
        descendants = await asyncio.to_thread(snapshot_descendants, pid)
        if state.is_connected and is_subprocess_alive(state):
            try:
                await graceful_disconnect(state.adapter, session_id)
            except Exception as e:
                logger.warning(f"[Pool] Error during eviction disconnect for {session_id}: {e}")
        record_terminated_subprocess(pid, descendants)
        logger.info(f"[Pool] Evicted session {session_id} (pool size: {len(self._pool)})")
        if self._eviction_callback is not None:
            try:
                await self._eviction_callback(session_id, reason)
            except Exception:
                logger.warning(
                    f"[Pool] Eviction callback failed for {session_id}", exc_info=True
                )
        return session_id

    async def remove(self, session_id: str) -> Optional[SessionState]:
        """Explicitly remove a session (for close_session, clear_context, delete).

        Returns the removed state, or None if not found.
        """
        state = self._pool.pop(session_id, None)
        if state is None:
            return None
        pid = subprocess_pid(state)
        # psutil's recursive children() scans the whole process table — off
        # the loop (this runs on the new-session admission path).
        descendants = await asyncio.to_thread(snapshot_descendants, pid)
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

    async def remove_broken(self, session_id: str) -> Optional[SessionState]:
        """Evict a session whose subprocess is alive but in an unknown state.

        Used by the two-phase interrupt when the streaming task had to be
        force-cancelled: the CLI never acknowledged the interrupt and is
        likely still mid-turn, so reusing the client would write the next
        user message into a busy subprocess and kill it ("Fatal error in
        message reader"). The pool entry is removed synchronously — the
        follow-up query must get a fresh client — while the subprocess
        teardown (up to several seconds of exit-grace/SIGTERM) runs in the
        background so the interrupt confirmation is not delayed past the
        frontend's window.
        """
        state = self._pool.pop(session_id, None)
        if state is None:
            return None
        state.is_connected = False
        pid = subprocess_pid(state)
        # Snapshot while the parent is still alive — orphans are undiscoverable
        # after it exits.
        descendants = snapshot_descendants(pid)

        async def _teardown() -> None:
            try:
                await graceful_disconnect(state.adapter, session_id)
            finally:
                record_terminated_subprocess(pid, descendants)

        task = asyncio.create_task(_teardown())
        self._background_teardowns.add(task)
        task.add_done_callback(self._background_teardowns.discard)
        logger.info(
            f"[Pool] Removed broken session {session_id} (pool size: {len(self._pool)}); "
            f"subprocess teardown continues in background"
        )
        return state

    # ─── Idle eviction (tiered) ──────────────────────────────────────────

    def _is_busy(self, session_id: str, state: SessionState) -> bool:
        """True when live work forbids evicting this session.

        Streaming turns, the between-turns idle reader (which holds the client
        through the CLI-wake grace window while looking idle to
        ``is_streaming``), and in-flight background sub-agents / SDK-backgrounded
        shells all mean "disconnecting would kill running work". This guard is
        checked BEFORE any deadline arithmetic and is never overridden by the
        idle policy.
        """
        return (
            state.is_streaming
            or state.idle_reader_active
            or self._has_background_work(session_id)
        )

    def _refresh_detachment(self, session_id: str, state: SessionState,
                            now: datetime) -> None:
        """Sync ``state.detached_since`` with the attachment probe.

        The push hooks (:meth:`note_session_detached` / :meth:`note_session_attached`)
        are the responsive path; this is the self-healing one, so the policy
        still works on an install where nothing wired the hooks up. An *unknown*
        probe result (None) is deliberately inert: it neither stamps nor clears,
        so a sync-engine failure can never manufacture an eviction.
        """
        try:
            attached = self._attachment_probe(session_id)
        except Exception:
            attached = None
        if attached is None:
            return
        if attached:
            state.detached_since = None
        elif state.detached_since is None:
            state.detached_since = now

    def idle_seconds_remaining(
        self, session_id: str, now: Optional[datetime] = None
    ) -> Optional[float]:
        """Seconds until this session becomes idle-evictable, or None.

        None means "not evictable on a timer": the session is busy, or it is not
        pooled. A value <= 0 means it is due now. This is the one place the
        three policy legs (tier / detached / floor) are combined, and it is what
        both :meth:`evict_idle` and the watchdog's sleep computation read, so a
        deadline can never be evaluated two different ways.
        """
        state = self._pool.get(session_id)
        if state is None:
            return None
        if self._is_busy(session_id, state):
            return None

        now = now or datetime.now(timezone.utc)
        policy = self._idle_policy

        # Rank from the MRU end: 0 is the most recently used session.
        try:
            rank = len(self._pool) - 1 - list(self._pool.keys()).index(session_id)
        except ValueError:  # pragma: no cover - popped concurrently
            return None

        idle_seconds = (now - state.last_activity).total_seconds()
        remaining = policy.tier_idle_seconds(rank) - idle_seconds

        # DETACHED leg: nobody is watching, so the subprocess is dead weight
        # once the grace expires — whatever tier the session is in.
        if policy.detached_grace_seconds > 0 and state.detached_since is not None:
            detached_for = (now - state.detached_since).total_seconds()
            remaining = min(remaining, policy.detached_grace_seconds - detached_for)

        # FLOOR leg: never reclaim a subprocess that was doing something a
        # moment ago, no matter which leg fired. Guards a WS flap mid-conversation.
        floor_remaining = policy.min_idle_floor_seconds - idle_seconds
        return max(remaining, floor_remaining)

    async def evict_idle(
        self, max_idle_seconds: Optional[int] = None
    ) -> list[str]:
        """Evict pooled sessions whose idle deadline has passed.

        ``max_idle_seconds=None`` (the default) applies the tiered
        :class:`IdlePolicy` — recency tier, detach grace, and activity floor.
        Passing an explicit ``max_idle_seconds`` applies that FLAT timeout to
        every session instead, which is what the legacy callers/tests expect and
        what an operator wants from a one-off "reclaim everything older than N"
        sweep.

        Busy sessions (streaming, idle-reader-held, or parked on in-flight
        background work) are never evicted by either mode.

        Returns the list of evicted session IDs.
        """
        now = datetime.now(timezone.utc)
        evicted: list[str] = []

        # Decide against a SNAPSHOT: evicting mutates LRU ranks, and a session's
        # tier must not change because an earlier victim left the pool mid-sweep.
        victims: list[tuple[str, SessionState, float]] = []
        for session_id, state in list(self._pool.items()):
            if self._is_busy(session_id, state):
                continue
            if max_idle_seconds is None:
                self._refresh_detachment(session_id, state, now)
                remaining = self.idle_seconds_remaining(session_id, now)
                if remaining is None or remaining > 0:
                    continue
            else:
                idle_seconds = (now - state.last_activity).total_seconds()
                if idle_seconds < max_idle_seconds:
                    continue
            victims.append(
                (session_id, state, (now - state.last_activity).total_seconds())
            )

        for session_id, state, idle_seconds in victims:
            if self._pool.get(session_id) is not state:
                continue  # removed while we were awaiting an earlier eviction
            if self._is_busy(session_id, state):
                continue  # became busy mid-sweep
            await self._do_evict(session_id, state, "idle")
            evicted.append(session_id)
            logger.info(
                "[Pool] Idle-evicted session %s (idle %.0fs, detached=%s, "
                "policy=%s)",
                session_id,
                idle_seconds,
                state.detached_since is not None,
                "flat" if max_idle_seconds is not None else "tiered",
            )
        return evicted

    # ─── Idle watchdog ───────────────────────────────────────────────────

    def _wake_idle_watchdog(self) -> None:
        """Ask the watchdog to recompute its sleep (cheap, never raises)."""
        wake = self._idle_wake
        if wake is None:
            return
        try:
            wake.set()
        except Exception:  # pragma: no cover - defensive
            pass

    def next_idle_deadline(self) -> Optional[float]:
        """Seconds until the EARLIEST idle deadline in the pool, or None.

        None means no pooled session is on a timer right now (empty pool, or
        every session busy) — the watchdog then parks on its wake event and
        costs nothing at all.
        """
        now = datetime.now(timezone.utc)
        soonest: Optional[float] = None
        for session_id, state in list(self._pool.items()):
            self._refresh_detachment(session_id, state, now)
            remaining = self.idle_seconds_remaining(session_id, now)
            if remaining is None:
                continue
            if soonest is None or remaining < soonest:
                soonest = remaining
        return soonest

    async def _idle_watchdog_loop(self) -> None:
        """Evict on the deadline instead of on someone else's tick.

        Sleeps until the earliest known eviction deadline (capped by
        ``IdlePolicy.reevaluate_seconds`` so a state change nobody told us about
        — typically a device disconnect on an install that has not wired the
        push hooks — is noticed within a bounded window), and parks on the wake
        event with no timeout at all while the pool is empty. Pure in-process
        timing: no DB, no thread, no work while idle.
        """
        assert self._idle_wake is not None
        while True:
            try:
                delay = self.next_idle_deadline()
                if delay is None:
                    timeout: Optional[float] = None
                elif delay <= 0:
                    timeout = 0.0
                else:
                    timeout = min(delay, self._idle_policy.reevaluate_seconds)

                if timeout is None:
                    await self._idle_wake.wait()
                    self._idle_wake.clear()
                    continue
                if timeout > 0:
                    try:
                        await asyncio.wait_for(self._idle_wake.wait(), timeout=timeout)
                        self._idle_wake.clear()
                        continue  # state changed — recompute before evicting
                    except asyncio.TimeoutError:
                        pass
                self._idle_wake.clear()
                await self.evict_idle()
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001 — the watchdog must survive anything
                logger.warning("[Pool] idle watchdog pass failed", exc_info=True)
                try:
                    await asyncio.sleep(5.0)
                except asyncio.CancelledError:
                    break

    def start_idle_watchdog(self) -> Optional[asyncio.Task]:
        """Start the deadline-driven idle watchdog (idempotent)."""
        if self._idle_task is not None and not self._idle_task.done():
            return self._idle_task
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("[Pool] idle watchdog not started (no running loop)")
            return None
        self._idle_wake = asyncio.Event()
        self._idle_task = loop.create_task(self._idle_watchdog_loop())
        logger.info(
            "[Pool] Idle watchdog started (hot=%d/%.0fs cold=%.0fs "
            "detached_grace=%.0fs)",
            self._idle_policy.hot_size,
            self._idle_policy.hot_idle_seconds,
            self._idle_policy.cold_idle_seconds,
            self._idle_policy.detached_grace_seconds,
        )
        return self._idle_task

    async def stop_idle_watchdog(self) -> None:
        """Cancel the idle watchdog and wait for it to exit (idempotent)."""
        task = self._idle_task
        self._idle_task = None
        self._idle_wake = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 — teardown must never raise
            logger.debug("[Pool] idle watchdog raised during shutdown", exc_info=True)

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
        await self.stop_idle_watchdog()
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
        """Snapshot of (session_id, state) pairs.

        Returns a LIST (not a live view) so callers can iterate across awaits
        while removals/evictions mutate the pool without raising
        "dictionary changed size during iteration".
        """
        return list(self._pool.items())

    def active_session_ids(self) -> list:
        """Get list of session IDs with connected clients."""
        return [sid for sid, s in list(self._pool.items()) if s.is_connected]

    def streaming_session_ids(self) -> list:
        """Get list of session IDs currently streaming."""
        return [sid for sid, s in list(self._pool.items()) if s.is_streaming]


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
    Others (system_prompt, cwd, mcp_servers, plugins, and the resolved provider
    binding) are fixed at spawn/connect time and require a new subprocess.
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

    # Provider identity/config (base URL, auth, model overlays) — a change here
    # means the subprocess would be talking to a different provider/account.
    # Overlay-key comparison is scoped to provider sessions so a native model
    # switch stays a hot-swap (see provider_env_requires_reconnect).
    if provider_env_requires_reconnect(old.env, new_options.env):
        return True

    # Resolved provider BINDING (PA13 / W-R4). The env comparison above only
    # sees the gateway overlay vars, which are identical (absent) on two
    # different NATIVE-leg providers and can coincide across rows — so a
    # cross-provider switch was invisible here and the wrong client got silently
    # reused (wrong credentials, wrong leg, wrong base URL). The binding is
    # stamped on the options object by ``query_options`` (None = native), so
    # comparing it catches every provider change, including
    # provider→native-Claude. A provider change MUST evict + reconnect: the
    # engine/leg and its credentials are fixed at connect time, and
    # ``adapter.set_model()`` can only hot-swap the model within one client.
    if getattr(old, "aih_provider_id", None) != getattr(
        new_options, "aih_provider_id", None
    ):
        return True

    # Context limits are fixed into both delegated CLI env and native-loop
    # compaction state at connect time, so a picker change must rebuild the leg.
    if getattr(old, "aih_context_window", None) != getattr(
        new_options, "aih_context_window", None
    ):
        return True

    # Per-session fast mode (v81) is baked in at connect time the same way — a
    # ``--settings {"fastMode": true}`` CLI arg on the delegated leg, a
    # ``service_tier`` on the native request builder — so a flip must rebuild
    # the leg rather than ride an in-place set_model. Compared as a bool so
    # absent and explicitly-off are the same standard-speed state.
    if bool(getattr(old, "aih_fast", None)) != bool(
        getattr(new_options, "aih_fast", None)
    ):
        return True

    # The session's sidepanel lane. The chat lane's read fence is built INTO the
    # engine at connect time — a fluid permission rule on the native leg, a
    # withheld ``--allowed-tools`` grant on the delegated one — so a client
    # connected for one lane cannot be reused for another. The cwd comparison
    # above almost always catches this on its own (a chat session runs in the
    # uploads folder), but the fence must not depend on "almost always".
    if getattr(old, "aih_session_mode", None) != getattr(
        new_options, "aih_session_mode", None
    ):
        return True

    # Tool-surface restriction (ruling CE12) — stamped by ``query_options`` as
    # ``aih_tool_allowlist`` and frozen into the engine at connect time (fluid
    # never renegotiates a session's tool surface mid-flight), so a change can
    # only be honored by a rebuild. Also compared directly: None means
    # unrestricted, which is a real and distinct value.
    if getattr(old, "aih_tool_allowlist", None) != getattr(
        new_options, "aih_tool_allowlist", None
    ):
        return True

    # The blocklist is spawn-time on BOTH legs now: a CLI arg
    # (``--disallowed-tools``) on the delegated one, and since CE12 a
    # construction-time filter on the native one. Order-insensitive so a
    # reshuffled list is not mistaken for a change.
    if sorted(getattr(old, "disallowed_tools", None) or []) != sorted(
        getattr(new_options, "disallowed_tools", None) or []
    ):
        return True

    # NOTE: on-disk OAuth credential rotation is deliberately NOT a reconnect
    # reason. Respawning kills every background task/sub-agent parented to the
    # subprocess, and the delegated CLI re-reads credentials from its config dir
    # when it needs to re-auth — so a rotation on disk is picked up in place.

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
        pool_size = getattr(settings, 'sdk_session_pool_size', 4)
        policy = IdlePolicy.from_settings(settings)
        _session_pool = SessionPool(max_size=pool_size, idle_policy=policy)
        logger.info(
            "[Pool] Initialized with max_size=%s hot=%s/%.0fs cold=%.0fs "
            "detached_grace=%.0fs",
            pool_size, policy.hot_size, policy.hot_idle_seconds,
            policy.cold_idle_seconds, policy.detached_grace_seconds,
        )
    return _session_pool


def note_session_attached(session_id: str) -> None:
    """Module-level push hook: a device connected to ``session_id``.

    Wiring seam for the WS layer (``sync_engine.register_device``) — it must not
    need to know whether a pool has been created yet, so this is a no-op before
    the singleton exists and never raises.
    """
    if _session_pool is None:
        return
    try:
        _session_pool.note_session_attached(session_id)
    except Exception:  # noqa: BLE001 — a lifecycle hint must never break a WS path
        logger.debug("[Pool] note_session_attached failed", exc_info=True)


def note_session_detached(session_id: str) -> None:
    """Module-level push hook: the LAST device watching ``session_id`` left.

    Wiring seam for ``sync_engine.unregister_device`` (fire it only when the
    session's device map becomes empty). Starts the detach grace and wakes the
    idle watchdog so the subprocess is reclaimed on the grace deadline instead
    of at the end of a long idle timeout. No-op before the pool exists.
    """
    if _session_pool is None:
        return
    try:
        _session_pool.note_session_detached(session_id)
    except Exception:  # noqa: BLE001 — a lifecycle hint must never break a WS path
        logger.debug("[Pool] note_session_detached failed", exc_info=True)
