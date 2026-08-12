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
same ``ToolRegistry`` (``omni.tools.mcp.bundled_server``); the CLI runs them and
sends the results back as ``user``/``tool_result`` frames, so the loop never
dispatches tools directly. Permission prompts arrive as ``can_use_tool``
control_requests: the loop surfaces a ``PermissionRequest`` event, parks until
``RespondPermission``, and replies with the matching ``control_response`` frame.

The process is kept alive across turns; ``cli_session_id`` (from the ``init``
frame) is the ``--resume`` handle for reconnecting to the same CLI conversation.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import tempfile
from typing import Any, AsyncIterator

from omni.engine.base import EngineCaps
from omni.engine.delegated_proto import PermissionBridge, StreamJsonMapper
from omni.events.bus import EventBus
from omni.events.model import ErrorEvent, Event, TurnStarted
from omni.events.ops import (
    Interrupt,
    Op,
    RespondPermission,
    SetModel,
    SetPermissionMode,
    UserTurn,
)
from omni.messages import Image, Message, Text
from omni.store.base import SessionMeta, SessionNode, SessionStore, new_id
from omni.tools.registry import ToolCtx, ToolRegistry


class DelegatedLoop:
    caps = EngineCaps(
        code_mode=False,
        inproc_tools=True,
        schema_plan_mode=False,
        fork=False,
        rewind=False,
        subagents=True,
        cache_metrics=False,
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
        self.cli_version: str | None = None

        self._session: SessionMeta | None = None
        self._session_id = session_id
        self._proc: asyncio.subprocess.Process | None = None
        self._req_counter = 0
        self._interrupted = asyncio.Event()
        self._turn_task: asyncio.Task | None = None
        self._pending_permissions: dict[str, asyncio.Future] = {}

        # Bundled inproc/builtin tool server (lazy — only if a registry is set).
        self._mcp_config_path: str | None = None
        self._socket_path: str | None = None
        self._socket_server: Any | None = None

    # ─── session plumbing (mirrors NativeLoop) ───────────────────────────

    async def _ensure_session(self) -> SessionMeta:
        if self._session is None:
            if self._session_id is not None:
                self._session = await self.store.get_session(self._session_id)
            if self._session is None:
                self._session = await self.store.create_session(self._session_id)
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
        if self._turn_task:
            await self._turn_task

    # ─── ExecutionEngine interface ───────────────────────────────────────

    async def submit(self, op: Op) -> None:
        if isinstance(op, UserTurn):
            if self._turn_task and not self._turn_task.done():
                raise RuntimeError("a turn is already running; Interrupt it first")
            self._interrupted.clear()
            self._turn_task = asyncio.create_task(self._run_turn(op))
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
        self._interrupted.set()
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
            except (asyncio.TimeoutError, ProcessLookupError):
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
        for path in (self._socket_path, self._mcp_config_path):
            if path and os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass
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

    async def _start_bundled_server(self) -> None:
        """Expose the registry's inproc/builtin tools over a unix socket and
        point ``--mcp-config`` at a bundled stdio server that bridges to it."""
        if self.registry is None or self._socket_server is not None:
            return
        from omni.tools.mcp.bundled_server import serve_registry_socket, write_bundled_config

        specs = [
            s for s in self.registry.list_tools(loaded_only=True)
            if s.source in ("inproc", "builtin")
        ]
        if not specs and not self.mcp_servers:
            return
        tmpdir = tempfile.mkdtemp(prefix="omni-delegated-")
        self._socket_path = os.path.join(tmpdir, "bundled.sock")
        self._mcp_config_path = os.path.join(tmpdir, "mcp.json")
        if specs:
            self._socket_server = await serve_registry_socket(
                self.registry, self._socket_path, self.ctx,
            )
        write_bundled_config(
            self.registry, self._mcp_config_path,
            socket_path=self._socket_path, user_servers=self.mcp_servers,
        )

    def _build_argv(self) -> list[str]:
        argv = list(self.claude_cmd) + [
            "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
        ]
        if self.model:
            argv += ["--model", self.model]
        if self.permission_mode:
            argv += ["--permission-mode", self.permission_mode]
        if self.cli_session_id:
            argv += ["--resume", self.cli_session_id]
        if self._mcp_config_path:
            argv += ["--mcp-config", self._mcp_config_path]
        if self.append_system_prompt:
            argv += ["--append-system-prompt", self.append_system_prompt]
        elif self.system:
            argv += ["--system-prompt", self.system]
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
        try:
            proc = await self._ensure_process()
        except Exception as exc:
            await self._emit(ErrorEvent(session_id=sid, turn_id=turn_id, message=f"spawn failed: {exc!r}"))
            return

        content = [Text(op.content)] if isinstance(op.content, str) else list(op.content)
        user_node = SessionNode(
            node_id=new_id("node"),
            parent_id=op.parent_node_id or meta.head_node_id,
            message=Message(role="user", content=content),
        )
        await self.store.append(sid, user_node)
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
            await self._read_until_result(sid, turn_id, mapper, user_node)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit(ErrorEvent(session_id=sid, turn_id=turn_id, message=repr(exc)))
        finally:
            if mapper.cli_session_id:
                self.cli_session_id = mapper.cli_session_id

    async def _read_until_result(
        self, sid: str, turn_id: str, mapper: StreamJsonMapper, user_node: SessionNode,
    ) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        parent = user_node
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                await self._emit(ErrorEvent(
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
            for event in mapped.events:
                await self._emit(event)
            if mapped.message is not None and mapped.node_id is not None:
                node = SessionNode(
                    node_id=mapped.node_id, parent_id=parent.node_id, message=mapped.message,
                )
                await self.store.append(sid, node)
                parent = node
            if mapped.permission is not None:
                await self._handle_permission(mapped.permission)
            if mapped.is_result:
                return

    # ─── permission bridge ───────────────────────────────────────────────

    async def _handle_permission(self, bridge: PermissionBridge) -> None:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending_permissions[bridge.omni_request_id] = fut
        try:
            resp: RespondPermission = await fut
        finally:
            self._pending_permissions.pop(bridge.omni_request_id, None)
        allow = resp.decision in ("allow", "allow_always")
        await self._write_control_response(bridge.cli_request_id, allow, bridge.input)

    async def _write_control_response(
        self, cli_request_id: str, allow: bool, input: dict[str, Any],
    ) -> None:
        if allow:
            response_data: dict[str, Any] = {"behavior": "allow", "updatedInput": input}
        else:
            response_data = {"behavior": "deny", "message": "permission denied"}
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
        if self._proc is None or self._proc.returncode is not None:
            return
        await self._write_frame({
            "type": "control_request",
            "request_id": self._next_req_id(),
            "request": request,
        })

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
