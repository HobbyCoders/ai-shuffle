"""DelegatedLoop — the ``claude`` CLI stream-json backend.

This engine does *not* run the agent loop itself: the official ``claude`` CLI
owns model calls, tool dispatch, compaction and caching. The loop spawns the CLI
in ``-p --input-format stream-json --output-format stream-json`` mode, feeds it
user turns on stdin, and maps every frame it writes back (via
``delegated_proto``) onto the same ``Event`` model ``NativeLoop`` emits. A client
reading ``events()`` cannot tell the two engines apart, modulo the documented
capability matrix (``EngineCaps``): no code-mode, no
arbitrary-node fork/rewind (only ``--resume``), no cache-breakpoint metrics.

In-process tools reach the CLI through a bundled stdio MCP server fronting the
same ``ToolRegistry`` (``fluid.tools.mcp.bundled_server``); the CLI runs them and
sends the results back as ``user``/``tool_result`` frames, so the loop never
dispatches tools directly. Permission prompts arrive as ``can_use_tool``
control_requests: the loop surfaces a ``PermissionRequest`` event, parks until
``RespondPermission``, and replies with the matching ``control_response`` frame.

Exactly one reader owns the CLI's stdout at a time (``_stdout_lock``): a turn's
frame reader, or — between turns — the control pump behind ``_request_control``.
Ownership is not fixed for the life of a wait: the pump yields the lock as soon
as a turn task exists, so a snapshot query can never stall a turn behind it. The
handover needs no frame pushback buffer, because a turn cannot make the CLI emit
anything until it writes its user frame, and it cannot write that frame until it
holds the lock the pump is still holding — so every line the pump reads is
either a control reply (routed by request_id) or genuinely stale.

The process is kept alive across turns; ``cli_session_id`` (from the ``init``
frame) is the ``--resume`` handle for reconnecting to the same CLI conversation.
It is written back to the store's ``SessionMeta`` every time it changes, so a
process restart can hand the CLI a handle it actually recognises instead of an
fluid session id. A handle the CLI no longer accepts (its own store was pruned,
or the id came from another machine) makes the first turn fail before any
frame is mapped; the loop then drops the handle, restarts the CLI without
``--resume`` and replays the turn once, so a stale handle costs a fresh CLI
conversation rather than a session that fails forever while reporting success.
The same frame announces the child's tool surface, kept on the loop as
``cli_tools`` / ``cli_mcp_servers`` and published as ``DelegatedSurface``.

Hooks: with a ``HookBus`` (or a deny policy) the loop sends the CLI an
``initialize`` control_request registering its own hook callbacks, the same
mechanism the Agent SDK uses. The CLI then asks the loop over
``hook_callback`` control_requests, and each one is forwarded into the HookBus
event the native loop emits at the same point (PreToolUse, PostToolUse,
TurnStart for UserPromptSubmit, TurnEnd for Stop, SessionStart, PreCompact,
SubagentStop), so a host observing the native loop observes this one the same
way. PreToolUse also applies the deny policy (``tool_policy``, else the
permission engine's deny rules), and a deny from either the hooks or the policy
wins over any allow. A SessionStart with source ``compact`` re-injects the
session ledger, the way the native loop recites it on every user turn.

Continual learning rides along when the loader wires a ``journal``: every
tool result the CLI reports is journaled with the input that produced it, the
user and assistant text of each turn become episodic entries, and Recall and
Remember reach the model through the bundled bridge like any other builtin.
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import os
import secrets
import signal
import socket
import tempfile
from collections.abc import AsyncIterator, Callable
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any

from fluid.engine.background import BackgroundLifecycleMixin
from fluid.engine.base import EngineCaps
from fluid.engine.delegated_proto import (
    ControlReply,
    HookCallbackRequest,
    PermissionBridge,
    StreamJsonMapper,
    flatten_tool_content,
    parse_control_response,
    parse_hook_callback,
)
from fluid.events.bus import EventBus
from fluid.events.model import (
    ErrorEvent,
    Event,
    HookEvent,
    Interrupted,
    PermissionRequest,
    SteerDelivered,
    ToolResult,
    TurnComplete,
    TurnStarted,
    Usage,
)
from fluid.events.ops import (
    BackgroundTaskFinished,
    Interrupt,
    Op,
    RespondPermission,
    RespondQuestion,
    Resume,
    SetModel,
    SetPermissionMode,
    Steer,
    UserTurn,
)
from fluid.hooks.bus import (
    POST_TOOL_USE,
    PRE_COMPACT,
    PRE_TOOL_USE,
    SESSION_START,
    SUBAGENT_STOP,
    TURN_END,
    TURN_START,
)
from fluid.messages import Image, Message, Text
from fluid.store.base import (
    SessionMeta,
    SessionNode,
    SessionStore,
    flush_events,
    new_id,
    persist_event,
)
from fluid.tools.mcp.bundled_server import STREAM_LINE_LIMIT
from fluid.tools.registry import ToolCtx, ToolRegistry

if TYPE_CHECKING:
    from fluid.hooks.bus import HookBus
    from fluid.permissions.engine import PermissionEngine

logger = logging.getLogger(__name__)

# The bundled bridge publishes each registry tool to the CLI under this MCP
# prefix; a session tool pattern may name either spelling.
_BUNDLED_PREFIX = "mcp__fluid_bundled__"


def _matches_tool_pattern(name: str, patterns: list[str]) -> bool:
    """Exact name or case-sensitive fnmatch pattern (same rule as NativeLoop)."""
    return any(name == p or fnmatchcase(name, p) for p in patterns)

# How long a control_request may wait for its control_response before the
# caller gives up and reports "unavailable". Short on purpose: these are
# snapshot queries a UI polls between turns, never something worth stalling on.
CONTROL_TIMEOUT = 5.0

# How long the CLI gets to answer the ``initialize`` request that registers
# the loop's hook callbacks. The SDK allows 60 s because MCP servers may still
# be starting. A CLI that never answers costs the hooks, never the turn.
INITIALIZE_TIMEOUT = 60.0

# The CLI hook events the loop registers when it has a HookBus, each with the
# HookBus event it forwards into (the one the native loop emits at the same
# point). PostToolUseFailure feeds PostToolUse with ``is_error`` set, which is
# how the native loop reports a failed call to the same hooks.
_CLI_HOOK_EVENTS: dict[str, str] = {
    "PreToolUse": PRE_TOOL_USE,
    "PostToolUse": POST_TOOL_USE,
    "PostToolUseFailure": POST_TOOL_USE,
    "UserPromptSubmit": TURN_START,
    "Stop": TURN_END,
    "SubagentStop": SUBAGENT_STOP,
    "PreCompact": PRE_COMPACT,
    "SessionStart": SESSION_START,
}
# Registered with only a deny policy and no HookBus: the policy gate, and the
# ledger re-injection after a compaction.
_POLICY_HOOK_EVENTS = ("PreToolUse", "SessionStart")
# Callback ids name their event, so an answer never depends on registration
# order surviving a respawn.
_HOOK_CALLBACK_PREFIX = "fluid_"

# Poll interval while another reader (a live turn) owns stdout and is expected
# to deliver our reply. Also caps a single pump read, so the pump notices a turn
# starting within one interval instead of sitting in ``readline`` until the
# control deadline.
_CONTROL_POLL = 0.05


class _PumpOutcome(enum.Enum):
    """Why ``_pump_control_stdout`` stopped reading.

    Four distinct reasons, four distinct values: overloading a bool here is what
    made "the CLI is gone" and "a turn wants stdout back" indistinguishable to
    the caller.
    """

    REPLY = "reply"  # the awaited control_response landed
    DEADLINE = "deadline"  # the caller's timeout expired first
    EOF = "eof"  # stdout closed — no reply will ever arrive
    YIELDED = "yielded"  # a turn started; stdout handed back to its reader


class DelegatedLoop(BackgroundLifecycleMixin):
    caps = EngineCaps(
        code_mode=False,
        inproc_tools=True,
        fork=False,
        rewind=False,
        subagents=True,
        cache_metrics=False,
        context_usage=True,
    )

    provider = "claude"

    def __init__(
        self,
        *,
        store: SessionStore,
        model: str,
        bus: EventBus | None = None,
        session_id: str | None = None,
        claude_path: str = "claude",
        claude_cmd: list[str] | None = None,
        system: str | None = None,
        append_system_prompt: str | None = None,
        permission_mode: str | None = None,
        registry: ToolRegistry | None = None,
        ctx: ToolCtx | None = None,
        cli_session_id: str | None = None,
        partial: bool = False,
        mcp_servers: dict[str, Any] | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        add_dirs: list[str] | None = None,
        allowed_tools: list[str] | None = None,
        disallowed_tools: list[str] | None = None,
        effort: str | None = None,
        settings: str | None = None,
        setting_sources: tuple[str, ...] | None = None,
        permissions: PermissionEngine | None = None,
        hooks: HookBus | None = None,
        tool_policy: Callable[[str, dict[str, Any]], str | None] | None = None,
        journal: Any | None = None,
    ) -> None:
        self.store = store
        self.model = model
        self.bus = bus or EventBus()
        self.claude_cmd = claude_cmd or [claude_path]
        self.system = system
        self.append_system_prompt = append_system_prompt
        # Always set a mode so old CLI settings or resumed sessions cannot
        # restore the retired planning mode.
        self.permission_mode = "default" if permission_mode in (None, "plan") else permission_mode
        self.registry = registry
        self.ctx = ctx or ToolCtx()
        self.cli_session_id = cli_session_id  # --resume handle; refreshed each turn
        self.partial = partial
        self.mcp_servers = mcp_servers
        self.cwd = cwd
        self.env = env
        self.add_dirs = add_dirs
        # Tool restriction (ruling CE12), the CLI's own flags: allowed is the
        # whole surface when set, disallowed subtracts. The native leg enforces
        # the same pair itself; here the child CLI does the enforcing.
        self.allowed_tools = allowed_tools
        # The delegated CLI supplies these tools itself; keep them off its surface.
        self.disallowed_tools = list(dict.fromkeys([
            *(disallowed_tools or ()), "EnterPlanMode", "ExitPlanMode",
        ]))
        # Gates for the bundled bridge only: the CLI owns its own tools, but a
        # registry tool served over the bridge is harness dispatch and gets the
        # native loop's hook + permission treatment. Optional; None skips.
        self.permissions = permissions
        self.hooks = hooks
        # Deny-only policy for the CLI's own tool calls, applied at the
        # PreToolUse hook: (tool name, input) -> the denial text, or None to
        # let the CLI's permission flow decide. None falls back to the
        # permission engine's deny rules. Bridge-served calls are gated at the
        # bridge instead and never reach it.
        self.tool_policy = tool_policy
        # True once the CLI accepted the ``initialize`` request that registered
        # this loop's hook callbacks (per process; reset on every spawn).
        self.cli_hooks_registered = False
        # Frames read while waiting for the ``initialize`` reply that belong
        # to the turn about to start (the CLI's init frame among them). The
        # turn's reader drains these before it reads stdout.
        self._stdout_backlog: list[bytes] = []
        # Stop hooks that sent the model back to work this turn (capped the
        # way the native loop caps them).
        self._stop_continuations = 0
        # The continual journal (fluid.codemode.journal.ToolJournal), fed from
        # the frames the CLI reports; None when learning is off.
        self._journal = journal
        # "Allow always" answers to the CLI's own permission prompts, keyed by
        # tool name. Scoped the way PermissionEngine.allow_always scopes: the
        # registry spec of the same name (Bash) narrows it to the programs the
        # call ran; a tool without one is allowed outright. The CLI also gets
        # its ``permission_suggestions`` echoed back as ``updatedPermissions``,
        # so a CLI that honours them never asks again either way.
        self._always_allowed: dict[str, set[str] | None] = {}
        # The --resume handle the live process was spawned with, and how many
        # turns it has completed. A first turn that fails before mapping a
        # single frame on a resumed process means the handle is dead.
        self._spawn_resume: str | None = None
        self._proc_results = 0
        # A hook on a bridge-served call answered ``continue: false``: the
        # reason, reported as the turn's ErrorEvent once the CLI's result
        # lands (the native loop ends its turn the same way).
        self._hook_stop_reason: str | None = None
        # Per-session shared secret every bridge request must carry. The socket
        # (a loopback port on Windows) is reachable by any local process; the
        # token is what makes it the CLI child's and nobody else's.
        self._bridge_token = secrets.token_urlsafe(32)
        # Reasoning-depth knob — forwarded verbatim as the CLI's --effort flag.
        self.effort = effort
        # A settings JSON blob, forwarded verbatim as the CLI's --settings flag.
        # The CLI accepts either a file path or inline JSON; the host decides
        # which and what goes in it (``fastMode``, ``env``, hooks, …). None
        # omits the flag entirely, leaving the CLI on its own settings files.
        self.settings = settings
        self.setting_sources = setting_sources
        self.cli_version: str | None = None
        # The tool surface the child CLI announced in its `init` frame — the
        # names it will actually accept, MCP servers included. The CLI decides
        # that surface from its own settings/flags, so this is the only place
        # the harness learns it. None until an init frame announces a tool
        # list; also published as a DelegatedSurface event.
        self.cli_tools: tuple[str, ...] | None = None
        self.cli_mcp_servers: tuple[dict[str, Any], ...] | None = None

        self._session: SessionMeta | None = None
        self._session_id = session_id
        self._proc: asyncio.subprocess.Process | None = None
        self._req_counter = 0
        self._interrupted = asyncio.Event()
        self._turn_task: asyncio.Task | None = None
        self._pending_permissions: dict[str, asyncio.Future] = {}
        # Loop-issued control_requests awaiting their control_response, by
        # request_id (the outbound mirror of _pending_permissions).
        self._pending_controls: dict[str, asyncio.Future] = {}
        # Context-usage snapshots asked for mid-turn, one per model call the
        # CLI reports: request_id -> the node that call produced. Answered
        # through the turn's own frame reader (see _read_until_result);
        # anything still outstanding when the turn ends is dropped.
        self._pending_usage_snapshots: dict[str, str] = {}
        # Only one reader may consume the CLI's stdout at a time: a turn's
        # frame reader, or a between-turns control pump.
        self._stdout_lock = asyncio.Lock()
        # Fire-and-forget writes of cli_session_id back to the store.
        self._cli_id_writes: set[asyncio.Task] = set()

        # Bundled inproc/builtin tool server (lazy — only if a registry is set).
        self._mcp_config_path: str | None = None
        self._socket_path: str | None = None
        self._socket_server: Any | None = None
        self._init_background_lifecycle()

    # ─── session plumbing (mirrors NativeLoop) ───────────────────────────

    async def _ensure_session(self) -> SessionMeta:
        if self._session is None:
            if self._session_id is not None:
                session = await self.store.get_session(self._session_id)
                if session is not None:
                    await self._seed_bus_seq(session.session_id)
                    self._session = session
            if self._session is None:
                self._session = await self.store.create_session(
                    self._session_id, cwd=self.cwd
                )
        return self._session

    async def _seed_bus_seq(self, sid: str) -> None:
        """Continue event numbering after the rows already persisted, the
        same way NativeLoop does, so a resumed delegated session does not
        re-issue seqs the events file already holds."""
        last_event_seq = getattr(self.store, "last_event_seq", None)
        # Failure must abort resume: starting at zero would silently collide
        # with durable events in stores that deduplicate by (session_id, seq).
        if callable(last_event_seq):
            last = int(await last_event_seq(sid))
        else:
            events = await self.store.events_since(sid, 0)
            last = max((e.seq for e in events), default=0)
        self.bus.seed(sid, last)

    @property
    def session_id(self) -> str:
        assert self._session is not None, "no session yet — submit a turn first"
        return self._session.session_id

    async def _emit(self, event: Event) -> Event:
        """Publish to the bus (assigns seq), then persist through the store.

        Same durability contract as NativeLoop: a store that batches
        (``append_stream_event``) may hold streamed Text/Thinking deltas
        uncommitted until 32 events, ~64 KiB, the next delta after 50 ms, or
        the next durable event. Every other event (block boundaries, tool
        calls and results, usage, permission prompts, lifecycle, terminals)
        is a barrier: it commits the held deltas and itself in one
        transaction, in seq order, before this returns. Batching adds no holes
        or reordering of its own: a delta commits with or ahead of the next
        durable event, never after it.

        Crash-loss window: an abrupt process exit loses at most the batched
        tail of deltas since the last commit, never a durable event, and a
        restarted producer continues numbering after the committed head, so
        the lost deltas' seqs are issued again. Their text is not lost: the
        assistant node the CLI's ``assistant`` frame stores carries it.

        Unlike NativeLoop, a failed write is not downgraded for deltas: it
        raises into the turn, which ends on an ErrorEvent. The rejected
        deltas stay batched, so that terminal write retries them first.
        """
        sequenced = await self.bus.publish(event)
        await persist_event(self.store, sequenced)
        return sequenced

    async def _flush_events(self, sid: str) -> None:
        """Commit any batched deltas when a turn ends without a durable
        terminal write behind them (cancellation, or a terminal that failed).

        Runs from ``finally``, so it can only log: the turn's own error, or
        its cancellation, is what the caller sees.
        """
        try:
            await flush_events(self.store, sid)
        except Exception:  # noqa: BLE001 — reported, never masks the turn's outcome
            logger.warning(
                "could not commit batched delegated deltas for %s; they stay "
                "queued for the session's next durable write", sid, exc_info=True,
            )

    def events(self, *, since_seq: int = 0) -> AsyncIterator[Event]:
        assert self._session is not None, "no session yet — submit a turn first"
        return self.bus.subscribe(self.session_id, since_seq=since_seq)

    async def wait_turn(self) -> None:
        await self._wait_background_turns()

    async def cancel_steer(self, steer_id: str) -> bool:
        """Always False: a delegated steer is delivered the moment it is sent.

        There is no pending queue to recall from here. ``_deliver_steer``
        writes the text straight to the CLI's stdin and receipts it as soon as
        the write drains, so by the time any caller could ask to cancel, the
        CLI already owns the message. Saying False keeps the host honest: the
        text is part of the conversation and must be treated as sent.
        """
        return False

    # ─── ExecutionEngine interface ───────────────────────────────────────

    async def submit(self, op: Op) -> None:
        if isinstance(op, BackgroundTaskFinished):
            await self._finish_background_task(op)
        elif isinstance(op, UserTurn):
            if self._turn_task and not self._turn_task.done():
                raise RuntimeError("a turn is already running; Interrupt it first")
            self._interrupted.clear()
            self._turn_task = asyncio.create_task(self._run_turn(op))
        elif isinstance(op, Resume):
            # The CLI owns its own transcript; fluid cannot hand it a head node
            # to continue from. Say so plainly rather than dropping the op.
            raise NotImplementedError("Resume is not supported by the delegated engine")
        elif isinstance(op, Steer):
            await self._deliver_steer(op)
        elif isinstance(op, Interrupt):
            self._interrupted.set()
            await self._send_interrupt()
        elif isinstance(op, RespondQuestion):
            if not await self._answer_question(op):
                raise ValueError(f"unknown question: {op.request_id}")
        elif isinstance(op, RespondPermission):
            fut = self._pending_permissions.get(op.request_id)
            if fut is not None and not fut.done():
                fut.set_result(op)
        elif isinstance(op, SetModel):
            self.model = op.model
            await self._send_control({"subtype": "set_model", "model": op.model})
        elif isinstance(op, SetPermissionMode):
            self.permission_mode = "default" if op.mode == "plan" else op.mode
            await self._send_control({"subtype": "set_permission_mode", "mode": self.permission_mode})
        else:  # Fork / Rewind: unsupported under delegation (see EngineCaps)
            raise NotImplementedError(f"op not supported by DelegatedLoop: {type(op).__name__}")

    async def aclose(self) -> None:
        await self._close_background_lifecycle()
        self._interrupted.set()
        # Settle anyone parked on a control reply: the CLI is going away, so
        # they get "unavailable" now instead of waiting out the timeout.
        for request_id, fut in list(self._pending_controls.items()):
            if not fut.done():
                fut.set_result(ControlReply(
                    request_id=request_id, ok=False, error="engine closed",
                ))
        if self._turn_task and not self._turn_task.done():
            self._turn_task.cancel()
            try:
                await self._turn_task
            except asyncio.CancelledError:
                pass
        await self._stop_process()
        if self._journal is not None:
            try:
                await self._journal.aclose()
            except Exception:
                logger.debug("closing the continual journal failed", exc_info=True)
        if self._socket_server is not None:
            self._socket_server.close()
            try:
                await self._socket_server.wait_closed()
            except Exception:
                pass
        # A "tcp:..." bridge address names no file — closing the server above is
        # the whole cleanup; only real paths get unlinked.
        for path in (self._socket_path, self._mcp_config_path):
            if path and not path.startswith("tcp:") and os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass
        # Let any in-flight resume-handle write land — losing it here is exactly
        # the restart case this persistence exists for.
        if self._cli_id_writes:
            try:
                await asyncio.wait(set(self._cli_id_writes), timeout=5)
            except Exception:
                logger.debug("draining cli_session_id writes failed", exc_info=True)
        await self.bus.close()

    # ─── process lifecycle ───────────────────────────────────────────────

    async def _version_guard(self) -> None:
        """Best-effort ``claude --version`` capture; never fatal."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.claude_cmd, "--version",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                env=self._child_env(),
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            text = out.decode(errors="replace").strip()
            self.cli_version = text or None
        except Exception:
            self.cli_version = None

    def _child_env(self) -> dict[str, str]:
        env = dict(os.environ)
        if self.env:
            env.update(self.env)
        return env

    async def _stop_process(self, *, grace: float = 5.0) -> None:
        """End the CLI process and everything it spawned.

        Closing stdin is the stream-json CLI's own exit signal, and the clean
        path lets it shut down its MCP children itself. A process still alive
        after ``grace`` seconds is killed as a tree: on POSIX the CLI runs in
        its own session so its whole process group goes, on Windows
        ``taskkill /T`` walks the descendants. Plain ``terminate()`` reached
        only the CLI and left those children orphaned.
        """
        proc, self._proc = self._proc, None
        self._stdout_backlog.clear()
        if proc is None or proc.returncode is not None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:  # noqa: BLE001 — a closed pipe is the goal
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace)
            return
        except (TimeoutError, ProcessLookupError):
            pass
        from fluid.tools.builtins.bash import kill_tree

        await kill_tree(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:  # noqa: BLE001 — best effort; the loop is closing
            pass

    def _bridge_tool_permitted(self, name: str) -> bool:
        """The session tool restriction, applied to a bridge-served tool.

        Mirrors ``NativeLoop._tool_permitted``: ``allowed_tools=None`` permits
        everything, otherwise the name must match; ``disallowed_tools`` then
        subtracts. The CLI sees the tool as ``mcp__fluid_bundled__<name>``, so
        a pattern written against either spelling counts. The CLI's own
        ``--disallowed-tools`` flag never reaches a bridge tool on its own:
        it matches the registry name while the CLI calls the prefixed one.
        """
        spellings = (name, _BUNDLED_PREFIX + name)
        if self.disallowed_tools and any(
            _matches_tool_pattern(s, self.disallowed_tools) for s in spellings
        ):
            return False
        if self.allowed_tools is None:
            return True
        return any(_matches_tool_pattern(s, self.allowed_tools) for s in spellings)

    async def _start_bundled_server(self) -> None:
        """Expose the registry's inproc/builtin tools over a local socket and
        point ``--mcp-config`` at a bundled stdio server that bridges to it.

        Transport: a unix domain socket where the platform has one, otherwise
        (Windows — no ``AF_UNIX``) a **loopback-only** TCP socket on an
        ephemeral port. Either way every request must carry this session's
        bridge token, handed to the child through the config's ``env``; the
        server also applies the session tool filter and, when the loop has
        them, the permission engine and hooks.
        """
        if self.registry is None or self._socket_server is not None:
            return
        from fluid.tools.mcp.bundled_server import (
            serve_registry_socket,
            write_bundled_config,
        )

        specs = [
            s for s in self.registry.list_tools(loaded_only=True)
            if s.source in ("inproc", "builtin") and self._bridge_tool_permitted(s.name)
        ]
        # Written even when it names no server: the config is what carries
        # --strict-mcp-config, and an empty one is how the child is told to
        # connect none of the user's own servers.
        tmpdir = tempfile.mkdtemp(prefix="fluid-delegated-")
        self._mcp_config_path = os.path.join(tmpdir, "mcp.json")
        use_unix = hasattr(socket, "AF_UNIX")
        self._socket_path = (
            os.path.join(tmpdir, "bundled.sock") if use_unix else "tcp:127.0.0.1:0"
        )
        if specs:
            self._socket_server = await serve_registry_socket(
                self.registry, self._socket_path, self.ctx,
                token=self._bridge_token,
                tool_filter=self._bridge_tool_permitted,
                permissions=self.permissions,
                hooks=self.hooks,
                on_stop=self._hook_stop,
            )
            if not use_unix:
                # Port 0 was an ask for *any* free port — resolve what we got so
                # the child config names the port it can actually dial.
                port = self._socket_server.sockets[0].getsockname()[1]
                self._socket_path = f"tcp:127.0.0.1:{port}"
        write_bundled_config(
            self.registry, self._mcp_config_path,
            socket_path=self._socket_path, user_servers=self.mcp_servers,
            token=self._bridge_token, tool_filter=self._bridge_tool_permitted,
        )

    def _build_argv(self) -> list[str]:
        argv = list(self.claude_cmd) + [
            "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
        ]
        if self.model:
            argv += ["--model", self.model]
        if self.permission_mode:
            argv += ["--permission-mode", self.permission_mode]
        # Route permission decisions to the harness: without this the headless
        # CLI never emits ``can_use_tool`` control_requests (it silently
        # auto-denies), so the PermissionBridge below would never fire.
        argv += ["--permission-prompt-tool", "stdio"]
        if self.cli_session_id:
            argv += ["--resume", self.cli_session_id]
        if self._mcp_config_path:
            # Strict: the child connects the servers fluid wrote (the bundled
            # bridge plus the session's configured servers) and none of the
            # ones its own settings files name.
            argv += ["--mcp-config", self._mcp_config_path, "--strict-mcp-config"]
        if self.append_system_prompt:
            argv += ["--append-system-prompt", self.append_system_prompt]
        elif self.system:
            argv += ["--system-prompt", self.system]
        for d in self.add_dirs or ():
            argv += ["--add-dir", d]
        if self.allowed_tools:
            argv += ["--allowed-tools", ",".join(self.allowed_tools)]
        if self.disallowed_tools:
            argv += ["--disallowed-tools", ",".join(self.disallowed_tools)]
        if self.effort:
            argv += ["--effort", self.effort]
        if self.settings:
            argv += ["--settings", self.settings]
        if self.setting_sources is not None:
            argv += ["--setting-sources", ",".join(self.setting_sources)]
        if self.partial:
            argv += ["--include-partial-messages"]
        return argv

    async def _ensure_process(self) -> asyncio.subprocess.Process:
        if self._proc is not None and self._proc.returncode is None:
            return self._proc
        await self._version_guard()
        await self._start_bundled_server()
        spawn_kwargs: dict[str, Any] = {}
        if os.name != "nt":
            # Own session, so _stop_process can kill the CLI's whole process
            # group without touching ours.
            spawn_kwargs["start_new_session"] = True
        self._spawn_resume = self.cli_session_id
        self._proc_results = 0
        self._proc = await asyncio.create_subprocess_exec(
            *self._build_argv(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=self.cwd,
            env=self._child_env(),
            limit=STREAM_LINE_LIMIT,
            **spawn_kwargs,
        )
        self._stdout_backlog.clear()
        self.cli_hooks_registered = False
        if self._cli_hook_events():
            async with self._stdout_lock:
                await self._initialize_cli_hooks()
        return self._proc

    # ─── turn lifecycle ──────────────────────────────────────────────────

    async def _run_turn(self, op: UserTurn) -> None:
        meta = await self._ensure_session()
        sid = meta.session_id
        turn_id = new_id("turn")
        self._background_turn_terminal = False
        self._active_turn_id = turn_id
        self._stop_continuations = 0
        mapper: StreamJsonMapper | None = None
        # Everything from here to the result sits under one guard: a failure
        # anywhere in it must still end the turn with a terminal event, or
        # ``events()`` waits forever for one.
        try:
            # The CLI comes up before the user node is written: a wake whose
            # spawn fails must stay undelivered so the lifecycle retries it.
            reused = self._proc is not None and self._proc.returncode is None
            try:
                await self._ensure_process()
            except Exception as exc:
                await self._emit_terminal_with_park(ErrorEvent(
                    session_id=sid, turn_id=turn_id, message=f"spawn failed: {exc!r}"
                ))
                return
            if reused:
                async with self._stdout_lock:
                    await self._discard_stale_stdout()
            op, question_ids = await self._question_turn_content(op)
            content = [Text(op.content)] if isinstance(op.content, str) else list(op.content)
            user_node = SessionNode(
                node_id=new_id("node"),
                parent_id=op.parent_node_id or meta.head_node_id,
                message=Message(role="user", content=content),
            )
            await self.store.append(sid, user_node)
            await self._mark_questions_delivered(question_ids)
            await self._mark_background_woken(op, sid, user_node.node_id, turn_id)
            # TurnStarted is emitted here (not from the init frame): a
            # persistent CLI process emits `init` only once, so per-turn
            # parity with NativeLoop requires the loop to own this event.
            await self._emit(TurnStarted(
                session_id=sid, node_id=user_node.node_id, turn_id=turn_id,
                model=self.model, provider=self.provider,
            ))
            self._journal_turn_start(content)
            mapper = await self._drive_turn(sid, turn_id, user_node, content)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit_terminal_with_park(
                ErrorEvent(session_id=sid, turn_id=turn_id, message=repr(exc))
            )
        finally:
            self._active_turn_id = None
            self._pending_usage_snapshots.clear()
            if (
                mapper is not None
                and mapper.cli_session_id
                and mapper.cli_session_id != self.cli_session_id
            ):
                self.cli_session_id = mapper.cli_session_id
                self._persist_cli_session_id(sid, mapper.cli_session_id)
            # Last: a second cancel landing on this await must not cost the
            # resume handle above.
            await self._flush_events(sid)

    async def _drive_turn(
        self, sid: str, turn_id: str, user_node: SessionNode, content: list[Any],
    ) -> StreamJsonMapper | None:
        """Write the user frame and read to the result, emitting the terminal
        event; the mapper that saw the result, or None when the retry's CLI
        could not be spawned.

        One retry: when the process was spawned with ``--resume`` and its
        first turn fails before a single frame is mapped, the handle is what
        the CLI rejected. The handle is dropped (and cleared in the store),
        the process restarted without it, and the same user frame replayed
        into a fresh CLI conversation.
        """
        mapper: StreamJsonMapper | None = None
        for attempt in (1, 2):
            if attempt == 2:
                # The retry runs on a fresh process (the rejected handle's
                # was stopped), so there is no stale stdout to drain.
                try:
                    await self._ensure_process()
                except Exception as exc:
                    await self._emit_terminal_with_park(ErrorEvent(
                        session_id=sid, turn_id=turn_id, message=f"spawn failed: {exc!r}"
                    ))
                    return mapper
            mapper = StreamJsonMapper(
                session_id=sid, turn_id=turn_id, model=self.model,
                provider=self.provider, user_node_id=user_node.node_id,
            )
            failure: str | None
            try:
                await self._write_frame({"type": "user", "message": {
                    "role": "user", "content": _content_to_cli(content),
                }})
            except (OSError, RuntimeError) as exc:
                # The CLI closed stdin before reading a turn: with a resume
                # handle in play that is the rejected-handle exit.
                failure = f"claude stdin closed before the turn was sent: {exc!r}"
            else:
                async with self._stdout_lock:
                    failure = await self._read_until_result(sid, turn_id, mapper, user_node)
            if failure is None:
                return mapper
            if attempt == 1 and self._resume_handle_rejected(mapper):
                logger.warning(
                    "claude did not accept --resume %s (%s); starting a fresh "
                    "CLI conversation", self._spawn_resume, failure,
                )
                await self._drop_resume_handle(sid)
                continue
            await self._emit_terminal_with_park(
                ErrorEvent(session_id=sid, turn_id=turn_id, message=failure)
            )
            return mapper
        return mapper

    async def _hook_stop(self, reason: str) -> None:
        """A bridge-served tool's hook ended the turn: ask the CLI to stop
        after this result, and remember why for the terminal event."""
        self._hook_stop_reason = reason
        try:
            await self._send_control({"subtype": "interrupt"})
        except Exception:  # noqa: BLE001 — the result frame still ends the turn
            logger.debug("interrupt after hook stop failed", exc_info=True)

    def _resume_handle_rejected(self, mapper: StreamJsonMapper) -> bool:
        """A first turn on a resumed process failed before mapping any frame."""
        return (
            self._spawn_resume is not None
            and self._proc_results == 0
            and not mapper.produced_nodes
        )

    async def _drop_resume_handle(self, sid: str) -> None:
        self.cli_session_id = None
        self._spawn_resume = None
        self._persist_cli_session_id(sid, None)
        await self._stop_process(grace=1.0)

    # ─── continual journal ───────────────────────────────────────────────

    def _journal_turn_start(self, content: list[Any]) -> None:
        if self._journal is None:
            return
        try:
            self._journal.begin_turn()
            text = " ".join(
                getattr(block, "text", "") for block in content
                if isinstance(block, Text)
            ).strip()
            self._journal.record_event("user", text)
        except Exception:  # noqa: BLE001 — learning never breaks a turn
            logger.debug("journal turn start failed", exc_info=True)

    def _journal_event(self, event: Event, mapper: StreamJsonMapper) -> None:
        """Feed the journal from the mapped stream: one entry per tool
        result (with the input the CLI reported for that call)."""
        if self._journal is None or not isinstance(event, ToolResult):
            return
        try:
            self._journal.record_tool(
                event.name, mapper.tool_inputs.get(event.tool_use_id, {}),
                event.output, event.is_error,
            )
        except Exception:  # noqa: BLE001
            logger.debug("journal tool record failed", exc_info=True)

    def _journal_turn_end(self, message: Message | None) -> None:
        if self._journal is None or message is None:
            return
        try:
            self._journal.record_event("assistant", message.text())
        except Exception:  # noqa: BLE001
            logger.debug("journal turn end failed", exc_info=True)

    # ─── mid-turn steering ───────────────────────────────────────────────

    async def _deliver_steer(self, op: Steer) -> None:
        """Hand mid-turn user text to the running CLI as a second ``user`` frame.

        Where NativeLoop queues a steer and folds it into the next tool result
        itself, the delegated leg has nothing to fold into — the CLI owns the
        conversation. Writing a second
        ``{"type": "user", "message": {"role": "user", "content": [...]}}`` frame
        to its stdin mid-turn is the supported way in (validated against claude
        CLI 2.1.239: no crash, nothing on stderr; the CLI injects the text at the
        next boundary — observed right after a tool result, before the final
        assistant text — and still emits a single ``result`` with ``num_turns``
        incremented). One frame per Steer, written in submission order; there is
        no batching and no per-frame acknowledgement to wait for, since the loop
        does not run with ``--replay-user-messages``.

        Written-to-stdin *is* delivery here: from that byte on the CLI owns the
        message and folds it into the turn, so ``SteerDelivered`` is receipted as
        soon as the write drains. The event is published straight from ``submit``
        rather than from the turn's frame reader; that is safe because ``_emit``
        does not suspend between assigning a seq and queueing on the store's
        event lock, and the lock admits writers in arrival order. The steer
        receipt is a durable barrier, so it also commits any deltas the reader
        had batched before it.

        Known narrow race: if the CLI's ``result`` for this turn was already in
        flight when the frame landed, the CLI holds the frame and processes it as
        a self-initiated follow-up cycle *after* the result. The text is never
        lost and the receipt still stands, but the assistant response to it
        arrives outside the turn — where the between-turn drain
        (``_discard_stale_stdout``) or the next turn's reader picks it up.

        Raises ``RuntimeError`` when there is nothing live to steer into: no
        running turn (send a ``UserTurn`` instead), an interrupt already
        requested for the running turn, or no live CLI process behind it. The
        caller is expected to fall back to queueing the message.
        """
        if self._turn_task is None or self._turn_task.done():
            raise RuntimeError("no turn is running; submit a UserTurn instead")
        if self._interrupted.is_set():
            raise RuntimeError(
                "the running turn is being interrupted; queue the message instead"
            )
        if (
            self._proc is None
            or self._proc.returncode is not None
            or self._active_turn_id is None
        ):
            # The turn task exists but its CLI process has not come up (or has
            # died): there is no stdin to fold the text into.
            raise RuntimeError("no live CLI turn to steer; queue the message instead")
        text = op.text
        record: dict[str, Any] = {"id": op.id, "text": op.text}
        if op.origin != "user":
            # A harness notice rides the same user frame — the CLI has no other
            # mid-turn channel — so the marker is the only thing separating it
            # from the user's own words. Wrap it, and defang the body first:
            # part of it can be text a sub-agent wrote.
            from fluid.context.steering import format_system_notice
            from fluid.tools.hardening import defang_harness_markers

            text = format_system_notice(defang_harness_markers(op.text)).strip()
            record["origin"] = op.origin
        await self._write_frame({"type": "user", "message": {
            "role": "user", "content": [{"type": "text", "text": text}],
        }})
        await self._emit(SteerDelivered(
            session_id=self.session_id, turn_id=self._active_turn_id,
            steers=[record],
        ))

    def _persist_cli_session_id(
        self, session_id: str, cli_session_id: str | None,
    ) -> None:
        """Write the refreshed ``--resume`` handle (or its removal) to the store.

        Fire-and-forget: this runs from the turn's ``finally`` (possibly while
        the task is being cancelled), so it must not await. Stores predating
        ``set_cli_session_id`` simply have no handle to persist; a failing write
        costs a fresh CLI conversation on the next restart, not a broken turn,
        so it is logged at debug rather than raised.
        """
        setter = getattr(self.store, "set_cli_session_id", None)
        if setter is None:
            logger.debug("store %s cannot persist cli_session_id", type(self.store).__name__)
            return
        try:
            task = asyncio.ensure_future(setter(session_id, cli_session_id))
        except RuntimeError:  # no running loop (shutdown) — nothing to persist onto
            logger.debug("no running loop; dropped cli_session_id persist", exc_info=True)
            return
        self._cli_id_writes.add(task)
        task.add_done_callback(self._on_cli_session_id_written)

    def _on_cli_session_id_written(self, task: asyncio.Task) -> None:
        self._cli_id_writes.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.debug("failed to persist cli_session_id: %r", exc)

    async def _discard_stale_stdout(self) -> None:
        """Drop frames the CLI emitted while no turn was active.

        ``_read_until_result`` only runs during a turn, so anything the CLI wrote
        between turns (e.g. an unsolicited wake) sits buffered in the
        StreamReader and would be consumed by the next turn's reader under that
        turn's mapper — and a stale ``result`` frame would end the fresh turn
        early. Draining here is race-free: the CLI cannot emit this turn's
        frames until the user frame is written, so every line read before that
        is stale by construction. ``wait_for`` cancelling ``readline`` on
        timeout is safe — ``readuntil`` only consumes from the buffer when a
        complete line is returned.
        """
        assert self._proc is not None and self._proc.stdout is not None
        for _ in range(1000):  # backstop against a continuously-spewing child
            try:
                line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=0.05)
            except TimeoutError:
                return
            if not line:  # EOF — let the turn's reader surface the closed stream
                return
            text = line.decode(errors="replace").strip()
            if not text:
                continue
            if self._route_control_response(text):
                # A control reply is never stale: some caller is awaiting that
                # exact request_id (a context-usage snapshot taken as this turn
                # was starting). Route it instead of dropping it on the floor.
                continue
            if await self._answer_hook_line(text):
                # A hook the CLI is blocked on: never stale either.
                continue
            logger.warning("discarding stale between-turn CLI frame: %.300s", text)
        logger.warning("stale-frame drain hit its line cap; continuing turn anyway")

    async def _read_until_result(
        self, sid: str, turn_id: str, mapper: StreamJsonMapper, user_node: SessionNode,
    ) -> str | None:
        """Map frames until the turn's result. None on a completed turn (its
        TurnComplete or Interrupted already emitted); otherwise the failure,
        with no terminal event emitted yet, for the caller to report or
        retry."""
        assert self._proc is not None and self._proc.stdout is not None
        parent = user_node
        last_assistant: Message | None = None
        while True:
            line = await self._next_stdout_line()
            if not line:
                return "claude stream closed before result"
            text = line.decode(errors="replace").strip()
            if not text:
                continue
            try:
                frame = json.loads(text)
            except json.JSONDecodeError:
                continue  # non-JSON diagnostic line — skip

            mapped = mapper.feed(frame)
            if mapped.hook_request is not None:
                # The CLI waits on this answer before it goes on, so it is
                # given here, in stream order.
                await self._answer_hook_request(mapped.hook_request)
                continue
            # Claude's native question tool is a posting operation, not a
            # permission decision. Never fabricate synchronous user answers.
            if mapped.permission is not None and mapped.permission.tool_name == "AskUserQuestion":
                from fluid.tools.builtins.ask_user import _validate

                bridge = mapped.permission
                questions = bridge.input.get("questions")
                error = _validate(questions)
                if error is None:
                    ids = await self._post_questions(questions, tool_use_id=bridge.tool_use_id)
                    message = json.dumps({
                        "status": "queued", "request_ids": ids,
                        "message": "Questions posted. Continue independent work or end "
                        "your turn if blocked; answers arrive automatically. Do not poll.",
                    })
                else:
                    message = error
                await self._write_control_response(
                    bridge.cli_request_id, False, bridge.input, message=message,
                )
                mapped.events = [e for e in mapped.events if not isinstance(e, PermissionRequest)]
                mapped.permission = None
            if mapper.cli_tools is not None and mapper.cli_tools != self.cli_tools:
                self.cli_tools = mapper.cli_tools
                self.cli_mcp_servers = mapper.cli_mcp_servers
            # An earlier "allow always" covers this request: answer it without
            # surfacing a prompt the host already answered.
            auto_allow = mapped.permission is not None and self._session_allows(
                mapped.permission.tool_name, mapped.permission.input,
            )
            if auto_allow:
                mapped.events = [
                    e for e in mapped.events if not isinstance(e, PermissionRequest)
                ]
            for event in mapped.events:
                if isinstance(event, TurnComplete) and self._hook_stop_reason is not None:
                    reason, self._hook_stop_reason = self._hook_stop_reason, None
                    event = ErrorEvent(
                        session_id=sid, node_id=event.node_id, turn_id=turn_id,
                        message=f"stopped by hook: {reason}", retriable=False,
                        usage=event.usage, cost_usd=event.cost_usd,
                        num_model_calls=event.num_model_calls,
                    )
                elif isinstance(event, TurnComplete) and self._interrupted.is_set():
                    # The CLI honoured the interrupt and closed the turn; say
                    # so the way the native loop does, cost and usage kept.
                    event = Interrupted(
                        session_id=sid, node_id=event.node_id, turn_id=turn_id,
                        usage=event.usage, cost_usd=event.cost_usd,
                        num_model_calls=event.num_model_calls,
                    )
                if isinstance(event, (TurnComplete, Interrupted, ErrorEvent)):
                    await self._emit_terminal_with_park(event)
                else:
                    await self._emit(event)
                self._journal_event(event, mapper)
                if isinstance(event, Usage) and event.node_id:
                    await self._ask_usage_snapshot(event.node_id)
            if mapped.message is not None and mapped.node_id is not None:
                node = SessionNode(
                    node_id=mapped.node_id, parent_id=parent.node_id, message=mapped.message,
                )
                await self.store.append(sid, node)
                parent = node
                if mapped.message.role == "assistant":
                    last_assistant = mapped.message
            if mapped.permission is not None:
                if auto_allow:
                    await self._write_control_response(
                        mapped.permission.cli_request_id, True, mapped.permission.input,
                    )
                else:
                    await self._handle_permission(mapped.permission)
            if mapped.control_reply is not None:
                # A control call issued while this turn runs: the turn owns
                # stdout, so its reader is what delivers the answer.
                if not await self._emit_usage_snapshot(sid, turn_id, mapped.control_reply):
                    self._resolve_control(mapped.control_reply)
            if mapped.is_result:
                # Snapshots the CLI never got round to answering die with the
                # turn — their Usage event already reported context_usage=None.
                self._pending_usage_snapshots.clear()
                if mapped.error is not None:
                    self._hook_stop_reason = None
                    return mapped.error
                self._proc_results += 1
                self._journal_turn_end(last_assistant)
                return None

    # ─── permission bridge ───────────────────────────────────────────────

    async def _handle_permission(self, bridge: PermissionBridge) -> None:
        """Answer a ``can_use_tool`` request by parking on the host's
        ``RespondPermission`` (the reader emitted the PermissionRequest).

        The wait also ends on an Interrupt. The turn's reader is what is
        parked here, holding stdout, so without that exit an interrupt could
        never be delivered: the CLI would sit on its unanswered request and
        the reader would sit on the future, each waiting for the other.
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending_permissions[bridge.fluid_request_id] = fut
        interrupt_wait = asyncio.create_task(self._interrupted.wait())
        try:
            done, _ = await asyncio.wait(
                {fut, interrupt_wait}, return_when=asyncio.FIRST_COMPLETED
            )
            if fut not in done:
                await self._write_control_response(
                    bridge.cli_request_id, False, bridge.input,
                    message="interrupted while awaiting permission",
                )
                return
            resp: RespondPermission = fut.result()
        finally:
            interrupt_wait.cancel()
            self._pending_permissions.pop(bridge.fluid_request_id, None)
        allow = resp.decision in ("allow", "allow_always")
        # An allow may carry a rewritten input (the SDK's ``updatedInput``) —
        # that is how a decider feeds data back into the tool call, e.g. the
        # answers collected for AskUserQuestion. Fall back to what the CLI asked
        # about when the responder had nothing to change.
        input = bridge.input
        if allow and resp.updated_input is not None:
            input = resp.updated_input
        suggestions: tuple[dict[str, Any], ...] = ()
        if resp.decision == "allow_always":
            self._remember_allow(bridge.tool_name, input)
            suggestions = bridge.suggestions
        await self._write_control_response(
            bridge.cli_request_id, allow, input, message=resp.message,
            permissions=suggestions,
        )

    def _registry_spec(self, tool_name: str) -> Any | None:
        """The registry spec behind a CLI tool name, bridged prefix or not."""
        if self.registry is None:
            return None
        base = tool_name[len(_BUNDLED_PREFIX):] if tool_name.startswith(_BUNDLED_PREFIX) else tool_name
        return self.registry.get(base)

    def _remember_allow(self, tool_name: str, input: dict[str, Any]) -> None:
        """Record an ``allow_always`` answer for the rest of the session,
        scoped by the tool's own labels when the registry knows the tool."""
        spec = self._registry_spec(tool_name)
        scope = spec.scope_of(input) if spec is not None else None
        if scope is None:
            self._always_allowed[tool_name] = None
            return
        current = self._always_allowed.get(tool_name, set())
        if current is None:
            return
        current.update(scope)
        self._always_allowed[tool_name] = current

    def _session_allows(self, tool_name: str, input: dict[str, Any]) -> bool:
        if tool_name not in self._always_allowed:
            return False
        allowed = self._always_allowed[tool_name]
        if allowed is None:
            return True
        spec = self._registry_spec(tool_name)
        scope = spec.scope_of(input) if spec is not None else None
        return scope is not None and scope <= allowed

    async def _write_control_response(
        self, cli_request_id: str, allow: bool, input: dict[str, Any],
        *, message: str | None = None,
        permissions: tuple[dict[str, Any], ...] = (),
    ) -> None:
        if allow:
            response_data: dict[str, Any] = {"behavior": "allow", "updatedInput": input}
            if permissions:
                # The CLI's own suggested rules for "always allow", handed
                # back so it stops asking about matching calls itself.
                response_data["updatedPermissions"] = list(permissions)
        else:
            # The CLI turns this message into the denied call's tool_result
            # text, so it is the only place a caller can explain the outcome to
            # the model. Generic default when the responder gave no reason.
            response_data = {"behavior": "deny", "message": message or "permission denied"}
        await self._write_frame({
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": cli_request_id,
                "response": response_data,
            },
        })

    # ─── Claude Code hooks ───────────────────────────────────────────────

    def _cli_hook_events(self) -> tuple[str, ...]:
        """The CLI hook events this loop registers; empty registers none."""
        if self.hooks is not None:
            return tuple(_CLI_HOOK_EVENTS)
        if self.tool_policy is not None or self._has_deny_rules():
            return _POLICY_HOOK_EVENTS
        return ()

    def _has_deny_rules(self) -> bool:
        rules = getattr(self.permissions, "rules", None) or ()
        return any(getattr(rule, "decision", None) == "deny" for rule in rules)

    async def _initialize_cli_hooks(self) -> None:
        """Register this loop's hook callbacks with a freshly spawned CLI.

        Sends the ``initialize`` control_request the Agent SDK sends, with one
        callback per event, and reads stdout until its reply. The reply can
        trail frames that belong to the first turn (the CLI's ``init`` frame),
        so those are kept for the turn's reader, and a hook the CLI fires in
        the meantime is answered. Never raises: a CLI that refuses, stays
        silent or has already exited leaves the hooks unregistered and the
        turn to report whatever is wrong with the process.
        """
        assert self._proc is not None and self._proc.stdout is not None
        events = self._cli_hook_events()
        request_id = self._next_req_id()
        try:
            await self._write_frame({
                "type": "control_request",
                "request_id": request_id,
                "request": {
                    "subtype": "initialize",
                    "hooks": {
                        event: [{
                            "matcher": None,
                            "hookCallbackIds": [_HOOK_CALLBACK_PREFIX + event],
                        }]
                        for event in events
                    },
                },
            })
        except (OSError, RuntimeError):
            # The CLI already closed stdin (a rejected --resume handle does
            # that); the turn's own write meets the same error and retries.
            logger.debug("claude CLI closed stdin before initialize", exc_info=True)
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + INITIALIZE_TIMEOUT
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                logger.warning(
                    "claude CLI did not answer initialize within %.0fs; its "
                    "hooks are not registered for this process", INITIALIZE_TIMEOUT,
                )
                return
            try:
                line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=remaining)
            except TimeoutError:
                continue
            if not line:
                return  # EOF: the turn's reader reports the closed stream
            text = line.decode(errors="replace").strip()
            try:
                frame = json.loads(text) if text else None
            except json.JSONDecodeError:
                frame = None
            if isinstance(frame, dict) and frame.get("type") == "control_response":
                reply = parse_control_response(frame)
                if reply is not None and reply.request_id == request_id:
                    if reply.ok:
                        self.cli_hooks_registered = True
                    else:
                        logger.warning(
                            "claude CLI refused initialize (%s); its hooks are "
                            "not registered for this process", reply.error,
                        )
                    return
                if reply is not None:
                    self._resolve_control(reply)
                continue
            hook = parse_hook_callback(frame) if isinstance(frame, dict) else None
            if hook is not None:
                await self._answer_hook_request(hook)
                continue
            self._stdout_backlog.append(line)

    async def _next_stdout_line(self) -> bytes:
        """The turn reader's next line: what ``initialize`` read ahead first."""
        if self._stdout_backlog:
            return self._stdout_backlog.pop(0)
        assert self._proc is not None and self._proc.stdout is not None
        return await self._proc.stdout.readline()

    async def _answer_hook_line(self, text: str) -> bool:
        """Answer ``text`` if it is a hook_callback control_request."""
        try:
            frame = json.loads(text)
        except json.JSONDecodeError:
            return False
        hook = parse_hook_callback(frame)
        if hook is None:
            return False
        await self._answer_hook_request(hook)
        return True

    async def _answer_hook_request(self, request: HookCallbackRequest) -> None:
        """Run one CLI hook through the HookBus and write the CLI's answer.

        An unknown callback id or a failure inside the forwarding answers with
        the error subtype, which the CLI treats as a failed hook and carries
        on; it never ends the turn.
        """
        event = None
        if request.callback_id.startswith(_HOOK_CALLBACK_PREFIX):
            event = request.callback_id[len(_HOOK_CALLBACK_PREFIX):]
        response: dict[str, Any]
        try:
            if event not in _CLI_HOOK_EVENTS:
                raise ValueError(f"unknown hook callback: {request.callback_id!r}")
            output = await self._run_cli_hook(event, request.input, request.tool_use_id)
            response = {
                "subtype": "success",
                "request_id": request.cli_request_id,
                "response": output,
            }
        except Exception as exc:  # noqa: BLE001 — a hook never ends the turn
            logger.warning("delegated %s hook failed", event or request.callback_id, exc_info=True)
            response = {
                "subtype": "error",
                "request_id": request.cli_request_id,
                "error": str(exc) or type(exc).__name__,
            }
        try:
            await self._write_frame({"type": "control_response", "response": response})
        except (OSError, RuntimeError):
            logger.debug("could not answer the CLI's hook callback", exc_info=True)

    async def _run_cli_hook(
        self, event: str, data: dict[str, Any], tool_use_id: str | None,
    ) -> dict[str, Any]:
        """The CLI's hook output for one ``event``, decided by the HookBus."""
        if event == "PreToolUse":
            return await self._cli_pre_tool_use(data, tool_use_id)
        if event in ("PostToolUse", "PostToolUseFailure"):
            return await self._cli_post_tool_use(event, data, tool_use_id)
        if event == "UserPromptSubmit":
            return await self._cli_prompt_submit(data)
        if event == "Stop":
            return await self._cli_stop(data)
        if event == "SubagentStop":
            return await self._cli_subagent_stop(data)
        if event == "PreCompact":
            return await self._cli_pre_compact(data)
        return await self._cli_session_start(data)

    def _cli_hook_payload(self, data: dict[str, Any], **fields: Any) -> dict[str, Any]:
        """The native loop's payload shape: fluid's session id, the transcript
        path (the CLI's own here), then the event's fields."""
        return {
            "session_id": self._session.session_id if self._session else "",
            "transcript_path": str(data.get("transcript_path") or ""),
            **fields,
        }

    async def _hook_event(
        self, level: str, message: str, hook_event: str, tool_name: str | None = None,
    ) -> None:
        if self._session is None:
            return
        await self._emit(HookEvent(
            session_id=self._session.session_id, turn_id=self._active_turn_id,
            level=level, message=message, hook_event=hook_event, tool_name=tool_name,
        ))

    async def _report_hook_warnings(
        self, hook_event: str, decision: Any, tool_name: str | None = None,
    ) -> None:
        """Hook diagnostics and ``systemMessage`` as warning HookEvents, as
        the native loop reports them."""
        messages = [*getattr(decision, "warnings", ())]
        if getattr(decision, "system_message", None):
            messages.append(decision.system_message)
        for message in messages:
            await self._hook_event("warning", message, hook_event, tool_name)

    def _bridge_serves(self, tool_name: str) -> bool:
        """True for a call the bundled bridge serves: the bridge runs that
        call's hooks and gates it, so the CLI's hooks leave it alone."""
        return tool_name.startswith(_BUNDLED_PREFIX) and self._socket_server is not None

    def _policy_denial(self, tool_name: str, tool_input: dict[str, Any]) -> str | None:
        """Why the deny policy refuses this call, or None."""
        if self.tool_policy is not None:
            try:
                return self.tool_policy(tool_name, tool_input) or None
            except Exception:  # noqa: BLE001 — the CLI's own permission flow still runs
                logger.warning("tool policy failed for %s", tool_name, exc_info=True)
                return None
        if self.permissions is None:
            return None
        from fluid.permissions.engine import Verdict
        from fluid.permissions.rules import match_rule

        rule = match_rule(self.permissions.rules, tool_name, tool_input, cwd=self.ctx.cwd)
        if rule is None or rule.decision != "deny":
            return None
        return Verdict("deny", "rule", "", rule).deny_message()

    async def _cli_pre_tool_use(
        self, data: dict[str, Any], tool_use_id: str | None,
    ) -> dict[str, Any]:
        """PreToolUse: the HookBus first, then the deny policy; a deny from
        either wins, even over a hook's ``allow``."""
        name = str(data.get("tool_name") or "")
        raw_input = data.get("tool_input")
        tool_input: dict[str, Any] = raw_input if isinstance(raw_input, dict) else {}
        if self._bridge_serves(name):
            return {}
        specific: dict[str, Any] = {"hookEventName": "PreToolUse"}
        if self.hooks is not None:
            decision = await self.hooks.emit(PRE_TOOL_USE, self._cli_hook_payload(
                data, name=name, input=tool_input,
                tool_use_id=data.get("tool_use_id") or tool_use_id,
                turn_id=self._active_turn_id,
            ))
            await self._report_hook_warnings(PRE_TOOL_USE, decision, name)
            if decision.action == "deny" or decision.stop:
                reason = decision.note or ("stopped by hook" if decision.stop else "denied")
                await self._hook_event("deny", reason, PRE_TOOL_USE, name)
                output: dict[str, Any] = {"hookSpecificOutput": {
                    **specific,
                    "permissionDecision": "deny",
                    "permissionDecisionReason": f"blocked by hook: {reason}",
                }}
                if decision.stop:
                    self._hook_stop_reason = reason
                    output.update({"continue": False, "stopReason": reason})
                return output
            if decision.action == "modify" and decision.replacement_input is not None:
                tool_input = decision.replacement_input
                specific["updatedInput"] = tool_input
            if decision.permission_decision in ("allow", "ask"):
                specific["permissionDecision"] = decision.permission_decision
                if decision.note:
                    specific["permissionDecisionReason"] = decision.note
            if decision.additional_context:
                specific["additionalContext"] = decision.additional_context
        denial = self._policy_denial(name, tool_input)
        if denial is not None:
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": denial,
            }}
        if specific.get("permissionDecision") == "allow":
            await self._hook_event(
                "info", f"hook approved {name}; permission prompt skipped", PRE_TOOL_USE, name,
            )
        return {"hookSpecificOutput": specific} if len(specific) > 1 else {}

    async def _cli_post_tool_use(
        self, event: str, data: dict[str, Any], tool_use_id: str | None,
    ) -> dict[str, Any]:
        name = str(data.get("tool_name") or "")
        if self.hooks is None or self._bridge_serves(name):
            return {}
        failed = event == "PostToolUseFailure"
        raw_input = data.get("tool_input")
        decision = await self.hooks.emit(POST_TOOL_USE, self._cli_hook_payload(
            data, name=name,
            tool_use_id=data.get("tool_use_id") or tool_use_id,
            input=raw_input if isinstance(raw_input, dict) else {},
            output=str(data.get("error") or "") if failed else _hook_text(data.get("tool_response")),
            is_error=failed, turn_id=self._active_turn_id,
        ))
        await self._report_hook_warnings(POST_TOOL_USE, decision, name)
        output: dict[str, Any] = {}
        if decision.stop:
            reason = decision.note or "stopped by hook"
            self._hook_stop_reason = reason
            await self._hook_event("warning", f"stopped by hook: {reason}", POST_TOOL_USE, name)
            output = {"continue": False, "stopReason": reason}
        elif decision.action == "deny":
            reason = decision.note or "blocked by hook"
            await self._hook_event("info", f"PostToolUse hook on {name}: {reason}", POST_TOOL_USE, name)
            output = {"decision": "block", "reason": reason}
        if decision.additional_context:
            output["hookSpecificOutput"] = {
                "hookEventName": event, "additionalContext": decision.additional_context,
            }
        return output

    async def _cli_prompt_submit(self, data: dict[str, Any]) -> dict[str, Any]:
        if self.hooks is None:
            return {}
        decision = await self.hooks.emit(TURN_START, self._cli_hook_payload(
            data, turn_id=self._active_turn_id, prompt=str(data.get("prompt") or ""),
        ))
        await self._report_hook_warnings(TURN_START, decision)
        if decision.action == "deny" or decision.stop:
            reason = decision.note or "blocked by hook"
            await self._hook_event("deny", reason, TURN_START)
            output: dict[str, Any] = {"decision": "block", "reason": reason}
            if decision.stop:
                output.update({"continue": False, "stopReason": reason})
            return output
        if decision.additional_context:
            return {"hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": decision.additional_context,
            }}
        return {}

    async def _cli_stop(self, data: dict[str, Any]) -> dict[str, Any]:
        """Stop (TurnEnd): a deny sends the model back to work, capped per
        turn at the native loop's limit; ``continue: false`` just ends it."""
        if self.hooks is None:
            return {}
        from fluid.engine.native import MAX_STOP_HOOK_CONTINUATIONS

        decision = await self.hooks.emit(TURN_END, self._cli_hook_payload(
            data, turn_id=self._active_turn_id,
            text=str(data.get("last_assistant_message") or ""),
            stop_reason="end_turn",
            stop_hook_active=bool(data.get("stop_hook_active")),
        ))
        await self._report_hook_warnings(TURN_END, decision)
        if decision.stop:
            await self._hook_event(
                "warning", f"stopped by hook: {decision.note or 'no reason given'}", TURN_END,
            )
            return {}
        if decision.action != "deny":
            return {}
        reason = decision.note or "a Stop hook asked the model to continue"
        if self._stop_continuations >= MAX_STOP_HOOK_CONTINUATIONS:
            await self._hook_event(
                "warning",
                f"Stop hook asked to continue ({reason}) but the turn already "
                f"continued {self._stop_continuations} times; ending it",
                TURN_END,
            )
            return {}
        self._stop_continuations += 1
        await self._hook_event("info", f"Stop hook asked to continue: {reason}", TURN_END)
        return {"decision": "block", "reason": reason}

    async def _cli_subagent_stop(self, data: dict[str, Any]) -> dict[str, Any]:
        """SubagentStop: observational, as on the native host — the subagent
        has already finished, so a block is reported and nothing else."""
        if self.hooks is None:
            return {}
        decision = await self.hooks.emit(SUBAGENT_STOP, {
            "session_id": self._session.session_id if self._session else "",
            "turn_id": self._active_turn_id,
            "task_id": data.get("agent_id"),
            "worker_session_id": None,
            "status": "completed",
            "summary": str(data.get("last_assistant_message") or ""),
            "agent_type": data.get("agent_type"),
        })
        await self._report_hook_warnings(SUBAGENT_STOP, decision)
        if decision.action == "deny":
            logger.warning(
                "SubagentStop hook asked to block %s (%s) but the subagent had "
                "already finished", data.get("agent_id"), decision.note,
            )
        return {}

    async def _cli_pre_compact(self, data: dict[str, Any]) -> dict[str, Any]:
        """PreCompact: observational, as on the native loop."""
        if self.hooks is None:
            return {}
        decision = await self.hooks.emit(PRE_COMPACT, self._cli_hook_payload(
            data, turn_id=self._active_turn_id,
            trigger=str(data.get("trigger") or "auto"),
            custom_instructions=str(data.get("custom_instructions") or ""),
            tiers=[],
        ))
        await self._report_hook_warnings(PRE_COMPACT, decision)
        return {}

    async def _cli_session_start(self, data: dict[str, Any]) -> dict[str, Any]:
        """SessionStart. After a compaction (source ``compact``) the session
        ledger goes back into context first: the CLI's summary may have
        dropped it, and it is the state that must survive every compaction."""
        source = str(data.get("source") or "startup")
        parts: list[str] = []
        if source == "compact":
            ledger = self._ledger_context()
            if ledger:
                parts.append(ledger)
        if self.hooks is not None:
            decision = await self.hooks.emit(
                SESSION_START, self._cli_hook_payload(data, source=source),
            )
            await self._report_hook_warnings(SESSION_START, decision)
            if decision.additional_context:
                parts.append(decision.additional_context)
        if not parts:
            return {}
        return {"hookSpecificOutput": {
            "hookEventName": "SessionStart", "additionalContext": "\n\n".join(parts),
        }}

    def _ledger_context(self) -> str | None:
        """The session ledger in the native loop's reminder form, or None."""
        from fluid.tools.builtins.ledger import read_ledger, render_ledger_reminder

        try:
            text = read_ledger(self.ctx, self._session.session_id if self._session else None)
        except Exception:  # noqa: BLE001 — a reminder never fails a hook
            logger.debug("reading the session ledger failed", exc_info=True)
            return None
        return render_ledger_reminder(text) if text else None

    # ─── control frames ──────────────────────────────────────────────────

    def _next_req_id(self) -> str:
        self._req_counter += 1
        return f"req_{self._req_counter}_{os.urandom(4).hex()}"

    async def _send_control(self, request: dict[str, Any]) -> None:
        """Fire-and-forget control_request (set_model, interrupt, …)."""
        if self._proc is None or self._proc.returncode is not None:
            return
        await self._write_frame({
            "type": "control_request",
            "request_id": self._next_req_id(),
            "request": request,
        })

    def _route_control_response(self, text: str) -> bool:
        """Resolve a raw stdout line if it is a control_response; else False.

        Used by every stdout consumer that is not the turn's frame mapper (the
        stale-frame drain, the between-turns control pump), so a reply can never
        be discarded by whichever reader happens to see it first.
        """
        try:
            frame = json.loads(text)
        except json.JSONDecodeError:
            return False
        if not isinstance(frame, dict) or frame.get("type") != "control_response":
            return False
        reply = parse_control_response(frame)
        if reply is not None:
            self._resolve_control(reply)
        return True

    def _resolve_control(self, reply: ControlReply) -> None:
        """Hand a control_response to whoever is awaiting that request_id.

        Replies nobody is waiting for (a timed-out caller, a request from a
        previous engine object sharing the CLI process) are dropped.
        """
        fut = self._pending_controls.get(reply.request_id)
        if fut is not None and not fut.done():
            fut.set_result(reply)

    async def _request_control(
        self, request: dict[str, Any], *, timeout: float = CONTROL_TIMEOUT,
    ) -> dict[str, Any] | None:
        """Send a control_request and await its control_response.

        Returns the reply's payload dict, or None when the CLI is not running,
        answered with the error subtype, or did not answer within ``timeout``.
        The pending entry is removed on every path — a late reply then simply
        finds nothing to resolve.
        """
        if self._proc is None or self._proc.returncode is not None:
            return None
        request_id = self._next_req_id()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_controls[request_id] = fut
        try:
            await self._write_frame({
                "type": "control_request",
                "request_id": request_id,
                "request": request,
            })
            await self._await_control_reply(fut, timeout)
            reply: ControlReply = fut.result()
        except TimeoutError:
            logger.debug("control_request %r timed out", request.get("subtype"))
            return None
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("control_request %r failed", request.get("subtype"), exc_info=True)
            return None
        finally:
            self._pending_controls.pop(request_id, None)
        if not reply.ok:
            logger.debug("control_request %r errored: %s", request.get("subtype"), reply.error)
            return None
        return reply.payload

    def _turn_is_active(self) -> bool:
        """True from the moment a turn task is created until it finishes.

        Deliberately keyed on the task, not on the user frame having been
        written: ``submit()`` creates the task before ``_run_turn`` writes
        anything, so this flips at the earliest instant a turn can exist.
        """
        return self._turn_task is not None and not self._turn_task.done()

    async def _await_control_reply(self, fut: asyncio.Future, timeout: float) -> None:
        """Wait for ``fut``, pumping stdout ourselves when nobody else is.

        Exactly one reader may consume the CLI's stdout. During a turn that is
        the turn's frame reader (which routes control replies through
        ``_resolve_control``), so here we only wait. Between turns nothing is
        reading, so this pumps lines itself under ``_stdout_lock`` and discards
        anything that is not the reply — matching ``_discard_stale_stdout``:
        frames arriving with no turn active are stale by construction.

        The two modes are not a one-time choice. A turn can start while we are
        pumping, and the pump then *yields*: it drops out of its read loop and
        releases the lock so the turn's own reader can take over (which is also
        what delivers our reply, via ``mapped.control_reply``). Without that,
        the pump would hold stdout for the rest of the deadline and the turn —
        which blocks on ``_stdout_lock`` before it can write its user frame —
        would stall for up to ``CONTROL_TIMEOUT`` behind a snapshot query.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not fut.done():
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            if self._turn_is_active() or self._stdout_lock.locked():
                # Someone else owns stdout; poll so a turn ending mid-wait hands
                # the pump back to us before the deadline.
                await asyncio.wait({fut}, timeout=min(_CONTROL_POLL, remaining))
                continue
            async with self._stdout_lock:
                outcome = await self._pump_control_stdout(fut, deadline)
            if outcome is _PumpOutcome.EOF and not fut.done():
                # stdout hit EOF: the CLI is gone and no reply is coming — stop
                # rather than spinning on a closed stream until the deadline.
                raise TimeoutError
            # REPLY / DEADLINE / YIELDED all fall back to the loop head, which
            # re-derives the state: reply → exit, deadline → TimeoutError,
            # yielded → poll while the turn's reader owns stdout.
        if not fut.done():
            raise TimeoutError

    async def _pump_control_stdout(
        self, fut: asyncio.Future, deadline: float,
    ) -> _PumpOutcome:
        """Read frames until the reply lands, a turn wants stdout, or time is up.

        Each read is capped at ``_CONTROL_POLL`` rather than the whole remaining
        budget: the turn-active check below only helps if the loop actually
        comes back around to it, and a bare ``readline`` would otherwise sit
        blocked until the deadline. Cancelling ``readline`` on timeout consumes
        nothing — ``readuntil`` only takes from the buffer when it returns a
        complete line (same guarantee ``_discard_stale_stdout`` relies on).
        """
        assert self._proc is not None and self._proc.stdout is not None
        loop = asyncio.get_running_loop()
        while not fut.done():
            if self._turn_is_active():
                # Checked *before* the read on purpose: stopping here means we
                # never hold a frame we cannot classify. Anything already read
                # was read while no turn existed — and a turn cannot make the
                # CLI emit until it writes its user frame, which it cannot do
                # until we release this lock — so nothing in flight belongs to
                # it (see the stdout-ownership note in the module docstring).
                return _PumpOutcome.YIELDED
            remaining = deadline - loop.time()
            if remaining <= 0:
                return _PumpOutcome.DEADLINE
            try:
                line = await asyncio.wait_for(
                    self._proc.stdout.readline(), timeout=min(remaining, _CONTROL_POLL),
                )
            except TimeoutError:
                continue  # nothing to read yet — re-check turn/deadline
            if not line:  # EOF — the CLI is gone; no reply is coming
                return _PumpOutcome.EOF
            text = line.decode(errors="replace").strip()
            if not text:
                continue
            if self._route_control_response(text) or await self._answer_hook_line(text):
                continue
            logger.warning("discarding between-turn CLI frame: %.300s", text)
        return _PumpOutcome.REPLY

    async def get_context_usage(self) -> dict[str, Any] | None:
        """The CLI's own ``/context`` breakdown, proxied verbatim.

        The delegated leg owns no context state — the CLI compiles the prompt,
        counts the tokens and knows the window — so this asks it and passes the
        answer straight through; it is already the documented shape (categories
        / totalTokens / maxTokens / rawMaxTokens / percentage / model /
        isAutoCompactEnabled). None when the CLI is not running, errored, or did
        not answer within ``CONTROL_TIMEOUT``. The native leg's ``compaction``
        stats field is absent here — the CLI compacts its own way, and reports
        each boundary as its own ``compact_boundary`` frame, which the mapper
        turns into a ``ContextCompaction`` event.
        """
        return await self._request_control({"subtype": "get_context_usage"})

    async def _ask_usage_snapshot(self, node_id: str) -> None:
        """Ask the CLI what the context looked like for the call just reported.

        Sent, not awaited. The turn's frame reader is the only stdout consumer
        while a turn runs, so awaiting the reply from inside that reader would
        deadlock against itself — the request goes out on stdin and the reply
        comes back through the same reader that would have been blocked
        (:meth:`_emit_usage_snapshot`), which is also why the answer arrives as
        a second ``Usage`` event rather than as a field on the first one.
        """
        if self._proc is None or self._proc.returncode is not None:
            return
        request_id = self._next_req_id()
        self._pending_usage_snapshots[request_id] = node_id
        try:
            await self._write_frame({
                "type": "control_request",
                "request_id": request_id,
                "request": {"subtype": "get_context_usage"},
            })
        except Exception:
            self._pending_usage_snapshots.pop(request_id, None)
            logger.debug("context-usage snapshot request failed", exc_info=True)

    async def _emit_usage_snapshot(
        self, sid: str, turn_id: str, reply: ControlReply,
    ) -> bool:
        """Publish a snapshot reply as a second ``Usage`` for the same node.

        Token fields stay 0 — the call's tokens were already reported by the
        first event, and double-counting them would inflate every host's spend
        accounting. Returns False when the reply belongs to somebody else.
        """
        node_id = self._pending_usage_snapshots.pop(reply.request_id, None)
        if node_id is None:
            return False
        if reply.ok and reply.payload is not None:
            await self._emit(Usage(
                session_id=sid, node_id=node_id, turn_id=turn_id,
                context_usage=reply.payload,
            ))
        else:
            logger.debug("context-usage snapshot unavailable: %s", reply.error)
        return True

    async def _send_interrupt(self) -> None:
        """Send the documented interrupt control_request; fall back to SIGINT.

        The fallback is POSIX-only: Windows has no SIGINT to send a child
        (``send_signal`` raises there), and the only alternative is a hard
        kill, which is ``aclose``'s job, not an interrupt's.
        """
        if self._proc is None or self._proc.returncode is not None:
            return
        try:
            await self._send_control({"subtype": "interrupt"})
        except Exception:
            if os.name == "nt":
                return
            try:
                self._proc.send_signal(signal.SIGINT)
            except (ProcessLookupError, OSError):
                pass

    async def _write_frame(self, frame: dict[str, Any]) -> None:
        """Write one JSON line to the CLI's stdin.

        No lock: the whole line goes into the transport in a single ``write``
        with no await inside it, so two writers (a turn, a mid-turn steer, a
        control frame) cannot interleave bytes — only ``drain`` suspends, and by
        then the line is already queued whole and in order.
        """
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write((json.dumps(frame) + "\n").encode())
        await self._proc.stdin.drain()


def _hook_text(value: Any) -> str:
    """A CLI ``tool_response`` as the text a PostToolUse hook sees.

    MCP tools answer with content blocks, the CLI's own tools with a dict
    (Bash: stdout/stderr/interrupted), so both are flattened to one string,
    the shape the native loop hands its hooks.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return flatten_tool_content(value)
    if value is None:
        return ""
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)


def _content_to_cli(content: list[Any]) -> list[dict[str, Any]]:
    """Normalized content blocks → Anthropic-style content for a user frame."""
    out: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, Text):
            out.append({"type": "text", "text": block.text})
        elif isinstance(block, Image):
            out.append({"type": "image", "source": {
                "type": "base64", "media_type": block.media_type, "data": block.data,
            }})
        else:
            out.append({"type": "text", "text": getattr(block, "text", "")})
    return out
