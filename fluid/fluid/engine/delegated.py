"""DelegatedLoop — the ``claude`` CLI stream-json backend.

This engine does *not* run the agent loop itself: the official ``claude`` CLI
owns model calls, tool dispatch, compaction and caching. The loop spawns the CLI
in ``-p --input-format stream-json --output-format stream-json`` mode, feeds it
user turns on stdin, and maps every frame it writes back (via
``delegated_proto``) onto the same ``Event`` model ``NativeLoop`` emits. A client
reading ``events()`` cannot tell the two engines apart, modulo the documented
capability matrix (``EngineCaps``): no code-mode, no schema plan mode, no
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
fluid session id. The same frame announces the child's tool surface, kept on the
loop as ``cli_tools`` / ``cli_mcp_servers`` and published as ``DelegatedSurface``.
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
from collections.abc import AsyncIterator
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any

from fluid.engine.background import BackgroundLifecycleMixin
from fluid.engine.base import EngineCaps
from fluid.engine.delegated_proto import (
    ControlReply,
    PermissionBridge,
    StreamJsonMapper,
    parse_control_response,
)
from fluid.events.bus import EventBus
from fluid.events.model import (
    ErrorEvent,
    Event,
    Interrupted,
    SteerDelivered,
    TurnComplete,
    TurnStarted,
    Usage,
)
from fluid.events.ops import (
    BackgroundTaskFinished,
    Interrupt,
    Op,
    RespondPermission,
    Resume,
    SetModel,
    SetPermissionMode,
    Steer,
    UserTurn,
)
from fluid.messages import Image, Message, Text
from fluid.store.base import SessionMeta, SessionNode, SessionStore, new_id
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
        schema_plan_mode=False,
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
        permissions: PermissionEngine | None = None,
        hooks: HookBus | None = None,
    ) -> None:
        self.store = store
        self.model = model
        self.bus = bus or EventBus()
        self.claude_cmd = claude_cmd or [claude_path]
        self.system = system
        self.append_system_prompt = append_system_prompt
        self.permission_mode = permission_mode
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
        self.disallowed_tools = disallowed_tools
        # Gates for the bundled bridge only: the CLI owns its own tools, but a
        # registry tool served over the bridge is harness dispatch and gets the
        # native loop's hook + permission treatment. Optional; None skips.
        self.permissions = permissions
        self.hooks = hooks
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
                self._session = await self.store.get_session(self._session_id)
            if self._session is None:
                self._session = await self.store.create_session(
                    self._session_id, cwd=self.cwd
                )
        return self._session

    @property
    def session_id(self) -> str:
        assert self._session is not None, "no session yet — submit a turn first"
        return self._session.session_id

    async def _emit(self, event: Event) -> Event:
        sequenced = await self.bus.publish(event)
        await self.store.append_event(event.session_id, sequenced)
        return sequenced

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
        elif isinstance(op, RespondPermission):
            fut = self._pending_permissions.get(op.request_id)
            if fut is not None and not fut.done():
                fut.set_result(op)
        elif isinstance(op, SetModel):
            self.model = op.model
            await self._send_control({"subtype": "set_model", "model": op.model})
        elif isinstance(op, SetPermissionMode):
            self.permission_mode = op.mode
            await self._send_control({"subtype": "set_permission_mode", "mode": op.mode})
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
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except (TimeoutError, ProcessLookupError):
                try:
                    self._proc.kill()
                except ProcessLookupError:
                    pass
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
        if not specs and not self.mcp_servers:
            return
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
            argv += ["--mcp-config", self._mcp_config_path]
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
        if self.partial:
            argv += ["--include-partial-messages"]
        return argv

    async def _ensure_process(self) -> asyncio.subprocess.Process:
        if self._proc is not None and self._proc.returncode is None:
            return self._proc
        await self._version_guard()
        await self._start_bundled_server()
        self._proc = await asyncio.create_subprocess_exec(
            *self._build_argv(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=self.cwd,
            env=self._child_env(),
        )
        return self._proc

    # ─── turn lifecycle ──────────────────────────────────────────────────

    async def _run_turn(self, op: UserTurn) -> None:
        meta = await self._ensure_session()
        sid = meta.session_id
        turn_id = new_id("turn")
        self._background_turn_terminal = False
        self._active_turn_id = turn_id
        reused = self._proc is not None and self._proc.returncode is None
        try:
            proc = await self._ensure_process()
        except Exception as exc:
            await self._emit_terminal_with_park(ErrorEvent(
                session_id=sid, turn_id=turn_id, message=f"spawn failed: {exc!r}"
            ))
            self._active_turn_id = None
            return
        if reused:
            async with self._stdout_lock:
                await self._discard_stale_stdout()

        content = [Text(op.content)] if isinstance(op.content, str) else list(op.content)
        user_node = SessionNode(
            node_id=new_id("node"),
            parent_id=op.parent_node_id or meta.head_node_id,
            message=Message(role="user", content=content),
        )
        await self.store.append(sid, user_node)
        await self._mark_background_woken(op, sid, user_node.node_id, turn_id)
        # TurnStarted is emitted here (not from the init frame): a persistent CLI
        # process emits `init` only once, so per-turn parity with NativeLoop
        # requires the loop to own this event.
        await self._emit(TurnStarted(
            session_id=sid, node_id=user_node.node_id, turn_id=turn_id,
            model=self.model, provider=self.provider,
        ))

        mapper = StreamJsonMapper(
            session_id=sid, turn_id=turn_id, model=self.model,
            provider=self.provider, user_node_id=user_node.node_id,
        )
        try:
            await self._write_frame({"type": "user", "message": {
                "role": "user", "content": _content_to_cli(content),
            }})
            async with self._stdout_lock:
                await self._read_until_result(sid, turn_id, mapper, user_node)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit_terminal_with_park(
                ErrorEvent(session_id=sid, turn_id=turn_id, message=repr(exc))
            )
        finally:
            self._active_turn_id = None
            self._pending_usage_snapshots.clear()
            if mapper.cli_session_id and mapper.cli_session_id != self.cli_session_id:
                self.cli_session_id = mapper.cli_session_id
                self._persist_cli_session_id(sid, mapper.cli_session_id)

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
        (bus ``publish`` + store ``append_event``) never suspends between
        assigning a seq and recording the event, so it cannot interleave with the
        reader task's own emits.

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
        await self._write_frame({"type": "user", "message": {
            "role": "user", "content": [{"type": "text", "text": op.text}],
        }})
        await self._emit(SteerDelivered(
            session_id=self.session_id, turn_id=self._active_turn_id,
            steers=[{"id": op.id, "text": op.text}],
        ))

    def _persist_cli_session_id(self, session_id: str, cli_session_id: str) -> None:
        """Write the refreshed ``--resume`` handle back to the store.

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
            logger.warning("discarding stale between-turn CLI frame: %.300s", text)
        logger.warning("stale-frame drain hit its line cap; continuing turn anyway")

    async def _read_until_result(
        self, sid: str, turn_id: str, mapper: StreamJsonMapper, user_node: SessionNode,
    ) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        parent = user_node
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                await self._emit_terminal_with_park(ErrorEvent(
                    session_id=sid, turn_id=turn_id, message="claude stream closed before result",
                ))
                return
            text = line.decode(errors="replace").strip()
            if not text:
                continue
            try:
                frame = json.loads(text)
            except json.JSONDecodeError:
                continue  # non-JSON diagnostic line — skip

            mapped = mapper.feed(frame)
            if mapper.cli_tools is not None and mapper.cli_tools != self.cli_tools:
                self.cli_tools = mapper.cli_tools
                self.cli_mcp_servers = mapper.cli_mcp_servers
            for event in mapped.events:
                if isinstance(event, (TurnComplete, Interrupted, ErrorEvent)):
                    await self._emit_terminal_with_park(event)
                else:
                    await self._emit(event)
                if isinstance(event, Usage) and event.node_id:
                    await self._ask_usage_snapshot(event.node_id)
            if mapped.message is not None and mapped.node_id is not None:
                node = SessionNode(
                    node_id=mapped.node_id, parent_id=parent.node_id, message=mapped.message,
                )
                await self.store.append(sid, node)
                parent = node
            if mapped.permission is not None:
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
                return

    # ─── permission bridge ───────────────────────────────────────────────

    async def _handle_permission(self, bridge: PermissionBridge) -> None:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending_permissions[bridge.fluid_request_id] = fut
        try:
            resp: RespondPermission = await fut
        finally:
            self._pending_permissions.pop(bridge.fluid_request_id, None)
        allow = resp.decision in ("allow", "allow_always")
        # An allow may carry a rewritten input (the SDK's ``updatedInput``) —
        # that is how a decider feeds data back into the tool call, e.g. the
        # answers collected for AskUserQuestion. Fall back to what the CLI asked
        # about when the responder had nothing to change.
        input = bridge.input
        if allow and resp.updated_input is not None:
            input = resp.updated_input
        await self._write_control_response(
            bridge.cli_request_id, allow, input, message=resp.message,
        )

    async def _write_control_response(
        self, cli_request_id: str, allow: bool, input: dict[str, Any],
        *, message: str | None = None,
    ) -> None:
        if allow:
            response_data: dict[str, Any] = {"behavior": "allow", "updatedInput": input}
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
            if not self._route_control_response(text):
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
        stats field is absent here — the CLI compacts its own way.
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
        """Send the documented interrupt control_request; fall back to SIGINT."""
        if self._proc is None or self._proc.returncode is not None:
            return
        try:
            await self._send_control({"subtype": "interrupt"})
        except Exception:
            try:
                self._proc.send_signal(signal.SIGINT)
            except ProcessLookupError:
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
