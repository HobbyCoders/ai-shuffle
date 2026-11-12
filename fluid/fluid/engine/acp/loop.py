"""AcpLoop — an ExecutionEngine that drives an external ACP agent.

The agent runs as a subprocess (one per engine) speaking ACP over stdio. It
owns its own model loop, its own transcript and its own built-in tools; this
engine translates between it and fluid:

* ``UserTurn`` → ``session/prompt``; ``session/update`` notifications →
  fluid events (text and thought chunks, tool calls and their results, plans);
  the turn's content is also stored as transcript nodes, like the delegated
  Claude leg does.
* ``session/request_permission`` → ``PermissionRequest`` and back from
  ``RespondPermission`` (Bypass answers Allow at once; the host's deny policy
  is applied first in every mode).
* ``elicitation/create`` → ``QuestionRequest`` / ``RespondQuestion``.
* ``Steer`` during a turn → a second ``session/prompt``, which the protocol
  lets an agent take as steering of the running turn.
* ``Interrupt`` → ``session/cancel``.
* The host's tool registry is served to the agent as an MCP server (fluid's
  bundled stdio bridge), passed in ``session/new``.
* The agent's session id is kept as the store's ``cli_session_id``, so a new
  engine (an app restart) resumes it with ``session/resume``.

What is agent-specific (how to launch it, which auth method, how a system
prompt is passed, the names of its config options) comes from an
:class:`AcpAgentSpec`; see ``antigravity.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
import secrets
import socket
import tempfile
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from fluid.engine.acp.client import (
    AcpConnection,
    AcpError,
    AgentInfo,
    RpcFailure,
    INVALID_PARAMS,
)
from fluid.engine.acp.mapping import TurnMapper, safe_server_name
from fluid.engine.base import EngineCaps
from fluid.events.bus import EventBus
from fluid.events.model import (
    ErrorEvent,
    Event,
    Interrupted,
    PermissionRequest,
    QuestionAnswered,
    QuestionRequest,
    SteerDelivered,
    TurnComplete,
    TurnStarted,
    Usage,
)
from fluid.events.ops import (
    AgentMessage,
    BackgroundTaskFinished,
    Interrupt,
    Op,
    Resume,
    RespondPermission,
    RespondQuestion,
    SetModel,
    SetPermissionMode,
    Steer,
    UserTurn,
)
from fluid.messages.content import Image, Message, Text
from fluid.store.base import SessionMeta, SessionNode, SessionStore, flush_events, new_id, persist_event

logger = logging.getLogger(__name__)

PERMISSION_TIMEOUT = 900.0
UNWATCHED_PERMISSION_TIMEOUT = 10.0
QUESTION_TIMEOUT = 1800.0
BUNDLED_SERVER_NAME = "fluid_bundled"

# Client permission mode -> the agent's ACP mode id (``AcpAgentSpec.mode_id``).
# Bypass has no agent equivalent: the agent runs normally and the host
# answers every permission request itself (``AcpLoop._permission``).
_ACP_NORMAL_MODE = "default"
_ACP_MODES: dict[str, str] = {
    "default": _ACP_NORMAL_MODE,
    "acceptEdits": "accept-edits",
    "plan": "plan",
    "bypassPermissions": _ACP_NORMAL_MODE,
    "auto": _ACP_NORMAL_MODE,
}


class AcpAuthRequired(RuntimeError):
    """The agent refused a session because it is not signed in."""


@dataclass
class AcpAgentSpec:
    """How to run one ACP agent. Subclass for agent specifics.

    ``auth_method`` is the ACP auth method id to ``authenticate`` with (None
    leaves it to the agent's own stored sign-in). ``env`` is the agent's whole
    environment: nothing else from the host process reaches it.
    """

    argv: list[str]
    provider: str = "acp"
    auth_method: str | None = None
    env: dict[str, str] | None = None
    client_info: dict[str, Any] = field(default_factory=lambda: {"name": "fluid", "version": "1"})

    def client_capabilities(self) -> dict[str, Any]:
        # No fs / terminal: the agent uses its own file and shell tools in its
        # own sandbox, under its own permission prompts (relayed to the host).
        return {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False,
                "elicitation": {"form": {}}}

    def authenticate_params(self) -> dict[str, Any]:
        return {"methodId": self.auth_method}

    def system_prompt_params(self, info: AgentInfo, text: str | None) -> dict[str, Any]:
        """``session/new`` params that carry the host's instructions."""
        if text and info.standard_system_prompt:
            return {"systemPrompt": text}
        return {}

    def config_value(self, options: Sequence[Mapping[str, Any]], kind: str, wanted: str | None) -> tuple[str, str] | None:
        """``(configId, value)`` selecting ``wanted`` for a ``model`` or
        ``thought_level`` option, or None when the agent offers no match."""
        if not wanted:
            return None
        for option in options:
            if option.get("category") != kind and option.get("id") != kind:
                continue
            values = [str(v.get("value")) for v in option.get("options") or [] if isinstance(v, Mapping)]
            if wanted in values:
                return str(option.get("id")), wanted
        return None

    def effort_value(self, effort: str | None) -> str | None:
        return effort

    def mode_id(self, permission_mode: str | None, available: Sequence[str]) -> str | None:
        """The agent's mode for a client permission mode, from ``available``.

        Every client mode maps to one: a mode with no agent equivalent
        (Bypass, where the host approves each request itself; ``auto``) runs
        in the agent's normal mode, so a change always moves the agent out of
        a mode it should no longer be in (Plan above all). None only when the
        agent advertises neither the mapped mode nor its normal one.
        """
        mode = _ACP_MODES.get(permission_mode or "default", _ACP_NORMAL_MODE)
        if mode in available:
            return mode
        return _ACP_NORMAL_MODE if _ACP_NORMAL_MODE in available else None

    def turn_failure(self, text: str) -> str | None:
        """An error the agent reported as message text instead of an RPC error."""
        return None

    def auth_message(self, exc: AcpError) -> str:
        return f"The agent is not signed in ({exc.message})."


def _accepts(fn: Callable[..., Any], name: str) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _env_list(env: Mapping[str, str] | None) -> list[dict[str, str]]:
    return [{"name": str(k), "value": str(v)} for k, v in (env or {}).items()]


def acp_server_entry(name: str, server: Mapping[str, Any]) -> dict[str, Any] | None:
    """A fluid / ``.mcp.json`` server entry in ACP's ``McpServer`` shape."""
    transport = server.get("type") or server.get("transport") or ("stdio" if server.get("command") else None)
    if transport in (None, "stdio"):
        if not server.get("command"):
            return None
        return {"name": safe_server_name(name), "command": str(server["command"]),
                "args": [str(a) for a in server.get("args") or []], "env": _env_list(server.get("env"))}
    if transport in ("http", "streamable_http", "sse") and server.get("url"):
        headers = [{"name": str(k), "value": str(v)} for k, v in (server.get("headers") or {}).items()]
        return {"type": "sse" if transport == "sse" else "http", "name": safe_server_name(name),
                "url": str(server["url"]), "headers": headers}
    return None


class AcpLoop:
    caps = EngineCaps()
    supports_async_tool_policy = True
    deny_carries_steers: Callable[[str], bool] | None = None

    def __init__(
        self,
        *,
        store: SessionStore,
        spec: AcpAgentSpec,
        model: str | None = None,
        effort: str | None = None,
        permission_mode: str | None = None,
        registry: Any = None,
        ctx: Any = None,
        session_id: str | None = None,
        acp_session_id: str | None = None,
        cwd: str | None = None,
        add_dirs: Sequence[str] | None = None,
        mcp_servers: Mapping[str, Mapping[str, Any]] | None = None,
        tool_filter: Callable[[str], bool] | None = None,
        permissions: Any = None,
        hooks: Any = None,
        tool_policy: Callable[..., Any] | None = None,
        bus: EventBus | None = None,
        system_append: str | None = None,
        connection_factory: Callable[..., AcpConnection] = AcpConnection,
    ) -> None:
        self.store = store
        self.spec = spec
        self.provider = spec.provider
        self.model = model
        self.effort = effort
        self.permission_mode = permission_mode or "default"
        self.registry = registry
        self.ctx = ctx
        self.cwd = os.path.abspath(cwd or os.getcwd())
        self.add_dirs = [os.path.abspath(d) for d in add_dirs or ()]
        self.mcp_servers = dict(mcp_servers or {})
        self.tool_filter = tool_filter
        self.permissions = permissions
        self.hooks = hooks
        self.tool_policy = tool_policy
        self.bus = bus or EventBus()
        self.system_append = system_append
        self.request_extra: dict[str, Any] = {}
        self.system: str | None = None
        self.system_sections: Any = None
        self.agents: Any = None
        self.cli_session_id = acp_session_id
        self.cli_version: str | None = None
        self._connection_factory = connection_factory
        self._session_id = session_id
        self._session: SessionMeta | None = None
        self._conn: AcpConnection | None = None
        self._acp_session: str | None = None
        self._session_resumed = False
        self._config_options: list[dict[str, Any]] = []
        self._modes: list[str] = []
        self._turn_task: asyncio.Task[None] | None = None
        self._mapper: TurnMapper | None = None
        self._interrupted = asyncio.Event()
        self._pending_permissions: dict[str, asyncio.Future[RespondPermission]] = {}
        self._pending_questions: dict[str, asyncio.Future[RespondQuestion]] = {}
        self._steer_requests: list[asyncio.Task[Any]] = []
        self._socket_server: Any = None
        self._socket_path: str | None = None
        self._tmpdir: tempfile.TemporaryDirectory[str] | None = None
        self._bridge_token = secrets.token_urlsafe(24)
        self._start_lock = asyncio.Lock()
        self._closed = False
        # Set by ``stop_agent`` while a turn runs: the text that turn ends with.
        self._stop_reason: str | None = None
        self.last_init: dict[str, Any] | None = None

    # ─── ExecutionEngine surface ────────────────────────────────────────

    @property
    def session_id(self) -> str:
        assert self._session is not None, "no session yet — submit a turn first"
        return self._session.session_id

    @property
    def pid(self) -> int | None:
        return self._conn.pid if self._conn is not None else None

    @property
    def acp_session_id(self) -> str | None:
        return self._acp_session

    async def _ensure_session(self) -> SessionMeta:
        if self._session is None:
            if self._session_id is not None:
                session = await self.store.get_session(self._session_id)
                if session is not None:
                    await self._seed_bus_seq(session.session_id)
                    self._session = session
                    self.cli_session_id = self.cli_session_id or session.cli_session_id
            if self._session is None:
                self._session = await self.store.create_session(self._session_id, cwd=self.cwd)
            if self.ctx is not None and getattr(self.ctx, "session_id", None) is None:
                self.ctx.session_id = self._session.session_id
        return self._session

    async def _seed_bus_seq(self, sid: str) -> None:
        last_event_seq = getattr(self.store, "last_event_seq", None)
        if callable(last_event_seq):
            last = int(await last_event_seq(sid))
        else:
            events = await self.store.events_since(sid, 0)
            last = max((e.seq for e in events), default=0)
        self.bus.seed(sid, last)

    def events(self, *, since_seq: int = 0) -> AsyncIterator[Event]:
        assert self._session is not None, "no session yet — submit a turn first"
        return self.bus.subscribe(self.session_id, since_seq=since_seq)

    async def _emit(self, event: Event) -> Event:
        sequenced = await self.bus.publish(event)
        await persist_event(self.store, sequenced)
        return sequenced

    async def cancel_steer(self, steer_id: str) -> bool:
        return False  # a steer is written to the agent at once

    async def get_context_usage(self) -> dict[str, Any] | None:
        return None

    def fast_waits_for_next_turn(self) -> bool:
        return False

    async def wait_turn(self) -> None:
        task = self._turn_task
        if task is not None:
            with contextlib.suppress(BaseException):
                await task

    async def submit(self, op: Op) -> None:
        if isinstance(op, (UserTurn, Resume)):
            if self._closed:
                raise RuntimeError("this engine was closed; start a new one")
            if self._turn_task is not None and not self._turn_task.done():
                raise RuntimeError("a turn is already running; Interrupt it first")
            self._interrupted.clear()
            self._turn_task = asyncio.create_task(self._run_turn(op))
        elif isinstance(op, Steer):
            await self._steer(op.text, op.id, op.origin)
        elif isinstance(op, AgentMessage):
            await self._steer(op.text, op.id, "peer")
        elif isinstance(op, Interrupt):
            self._interrupted.set()
            for fut in [*self._pending_permissions.values(), *self._pending_questions.values()]:
                if not fut.done():
                    fut.cancel()
            await self._cancel_remote()
        elif isinstance(op, RespondPermission):
            fut = self._pending_permissions.get(op.request_id)
            if fut is not None and not fut.done():
                fut.set_result(op)
            else:
                logger.warning("permission decision for %s matched no open request; ignored", op.request_id)
        elif isinstance(op, RespondQuestion):
            fut = self._pending_questions.get(op.request_id)
            if fut is None or fut.done():
                raise ValueError(f"unknown question: {op.request_id}")
            fut.set_result(op)
        elif isinstance(op, SetModel):
            self.model = op.model
            await self._apply_config("model", op.model)
            if self.effort:
                await self._apply_config("thought_level", self.spec.effort_value(self.effort))
        elif isinstance(op, SetPermissionMode):
            self.permission_mode = op.mode
            await self._apply_mode()
        elif isinstance(op, BackgroundTaskFinished):
            # The host's own turn queue owns the wake on this leg.
            logger.debug("ACP engine: background task %s finished; the host wakes the session", op.task_id)
        else:
            raise NotImplementedError(f"op not supported by AcpLoop: {type(op).__name__}")

    async def set_run_settings(self, *, effort: str | None, fast: bool) -> None:
        if fast:
            raise ValueError("Fast mode is not supported by this agent")
        self.effort = effort
        await self._apply_config("thought_level", self.spec.effort_value(effort))

    async def aclose(self) -> None:
        self._closed = True
        self._interrupted.set()
        task = self._turn_task
        if task is not None and not task.done():
            await self._cancel_remote()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
            except (asyncio.TimeoutError, Exception):
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
        await self._stop_process()

    async def replace_spec(self, spec: AcpAgentSpec) -> None:
        """Use ``spec`` from the next turn: the running process is stopped
        and the next turn starts a fresh one, resuming the saved session.
        Only between turns; a running turn keeps its process."""
        if self._turn_task is not None and not self._turn_task.done():
            raise RuntimeError("a turn is running; replace the spec between turns")
        async with self._start_lock:
            self.spec = spec
            await self._stop_process()

    async def stop_agent(self, reason: str) -> None:
        """End the agent process now, also in the middle of a turn.

        For a credential taken away (signed out, removed, disabled, key
        replaced): a running turn ends with ``reason`` instead of going on
        with the old process; the next turn starts a fresh process.
        """
        task = self._turn_task
        if task is not None and not task.done():
            self._stop_reason = reason
            conn = self._conn
            if conn is not None:
                await conn.close()  # the pending prompt fails; ``_drive`` reports reason
            return
        async with self._start_lock:
            await self._stop_process()

    async def release_abandoned(self) -> None:
        await self.aclose()

    # ─── process and session ────────────────────────────────────────────

    async def _stop_process(self) -> None:
        conn, self._conn = self._conn, None
        self._acp_session = None
        if conn is not None:
            if self._acp_session_closeable(conn):
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(conn.request("session/close", {"sessionId": self.cli_session_id}), 2)
            await conn.close()
        server, self._socket_server = self._socket_server, None
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
        tmp, self._tmpdir = self._tmpdir, None
        if tmp is not None:
            with contextlib.suppress(Exception):
                tmp.cleanup()

    def _acp_session_closeable(self, conn: AcpConnection) -> bool:
        # Closing ends the agent's in-memory session only; its saved
        # conversation stays resumable. Skipped when the process is gone.
        return bool(conn.alive and self.cli_session_id and conn.info
                    and conn.info.session_capability("close"))

    async def _start_bridge(self) -> dict[str, Any] | None:
        """Serve the registry's host tools to the agent; its ACP server entry."""
        if self.registry is None:
            return None
        from fluid.tools.mcp.bundled_server import bundled_server_config, serve_registry_socket

        specs = [s for s in self.registry.list_tools(loaded_only=True)
                 if s.source in ("inproc", "builtin") and self._bridge_permitted(s.name)]
        if not specs:
            return None
        if self._socket_server is None:
            self._tmpdir = tempfile.TemporaryDirectory(prefix="fluid-acp-")
            use_unix = hasattr(socket, "AF_UNIX")
            self._socket_path = (os.path.join(self._tmpdir.name, "bridge.sock")
                                 if use_unix else "tcp:127.0.0.1:0")
            self._socket_server = await serve_registry_socket(
                self.registry, self._socket_path, self.ctx, token=self._bridge_token,
                tool_filter=self._bridge_permitted, permissions=self.permissions, hooks=self.hooks,
            )
            if not use_unix:
                port = self._socket_server.sockets[0].getsockname()[1]
                self._socket_path = f"tcp:127.0.0.1:{port}"
        config = bundled_server_config(self._socket_path or "", self._bridge_token)
        return acp_server_entry(BUNDLED_SERVER_NAME, config)

    def _bridge_permitted(self, name: str) -> bool:
        return self.tool_filter is None or bool(self.tool_filter(name))

    def bridged_tools(self) -> list[str]:
        if self.registry is None:
            return []
        return [s.name for s in self.registry.list_tools(loaded_only=True)
                if s.source in ("inproc", "builtin") and self._bridge_permitted(s.name)]

    def _bridged_name(self, call: Mapping[str, Any]) -> str | None:
        title = str(call.get("title") or "")
        meta = call.get("_meta") if isinstance(call.get("_meta"), Mapping) else {}
        candidates = [str(meta.get(k)) for k in ("toolName", "tool_name", "mcpTool") if meta.get(k)]
        candidates.append(title)
        served = self.bridged_tools()
        for text in candidates:
            for name in served:
                tail = name.rsplit("__", 1)[-1]
                for spelled in (name, f"mcp__{BUNDLED_SERVER_NAME}__{name}", f"{BUNDLED_SERVER_NAME}/{name}",
                                f"{BUNDLED_SERVER_NAME}_{name}", f"mcp_{BUNDLED_SERVER_NAME}_{name}"):
                    if text == spelled or text.startswith(spelled + " ") or text.startswith(spelled + ":"):
                        return f"mcp__{BUNDLED_SERVER_NAME}__{name}"
                if text == tail and meta:
                    return f"mcp__{BUNDLED_SERVER_NAME}__{name}"
        return None

    async def _mcp_servers(self, info: AgentInfo) -> list[dict[str, Any]]:
        servers: list[dict[str, Any]] = []
        bridge = await self._start_bridge()
        if bridge is not None:
            servers.append(bridge)
        transports = info.mcp_transports
        for name, server in self.mcp_servers.items():
            entry = acp_server_entry(name, server)
            if entry is None:
                continue
            kind = entry.get("type", "stdio")
            if kind not in transports:
                logger.info("ACP agent takes no %s MCP servers; %s is not passed", kind, name)
                continue
            servers.append(entry)
        return servers

    async def _ensure_process(self) -> None:
        async with self._start_lock:
            if self._conn is not None and self._conn.alive and self._acp_session:
                return
            if self._conn is not None:
                await self._stop_process()
            conn = self._connection_factory(
                self.spec.argv, cwd=self.cwd, env=self.spec.env,
                on_notification=self._on_notification, on_request=self._on_request,
                stderr_sink=lambda line: logger.debug("acp agent: %s", line),
            )
            await conn.start()
            self._conn = conn
            try:
                info = await conn.initialize(self.spec.client_capabilities(), self.spec.client_info)
                self.last_init = info.raw
                self.cli_version = info.version or None
                if self.spec.auth_method:
                    await conn.request("authenticate", self.spec.authenticate_params())
                servers = await self._mcp_servers(info)
                result: Any = None
                self._session_resumed = False
                if self.cli_session_id and info.session_capability("resume"):
                    try:
                        result = await conn.request("session/resume", {
                            "sessionId": self.cli_session_id, "cwd": self.cwd, "mcpServers": servers,
                        })
                        self._session_resumed = True
                    except AcpError as exc:
                        if exc.auth_required:
                            raise
                        logger.warning("ACP agent did not resume session %s (%s); starting a new one",
                                       self.cli_session_id, exc.message)
                if not self._session_resumed:
                    params: dict[str, Any] = {"cwd": self.cwd, "mcpServers": servers}
                    if self.add_dirs and info.session_capability("additionalDirectories"):
                        params["additionalDirectories"] = self.add_dirs
                    params.update(self.spec.system_prompt_params(info, self.system_append))
                    result = await conn.request("session/new", params)
                    new_sid = str((result or {}).get("sessionId") or "")
                    if not new_sid:
                        raise AcpError(-32603, "session/new returned no sessionId")
                    self.cli_session_id = new_sid
                    if self._session is not None:
                        with contextlib.suppress(Exception):
                            await self.store.set_cli_session_id(self._session.session_id, new_sid)
                self._acp_session = self.cli_session_id
                self._read_session_state(result if isinstance(result, Mapping) else {})
                await self._apply_config("model", self.model)
                await self._apply_config("thought_level", self.spec.effort_value(self.effort))
                await self._apply_mode()
            except BaseException:
                self._acp_session = None
                await self._stop_process()
                raise

    def _read_session_state(self, result: Mapping[str, Any]) -> None:
        options = result.get("configOptions")
        if isinstance(options, list):
            self._config_options = [o for o in options if isinstance(o, dict)]
        modes = result.get("modes") if isinstance(result.get("modes"), Mapping) else {}
        available = [str(m.get("id")) for m in modes.get("availableModes") or [] if isinstance(m, Mapping)]
        if not available:
            # A session/resume reply names the modes only as the "mode" config
            # option. Without this a resumed session never changes mode again.
            for option in self._config_options:
                if option.get("category") == "mode" or option.get("id") == "mode":
                    available = [str(v.get("value")) for v in option.get("options") or []
                                 if isinstance(v, Mapping)]
                    break
        self._modes = available

    def _current_config(self, config_id: str) -> str | None:
        for option in self._config_options:
            if option.get("id") == config_id:
                value = option.get("currentValue")
                return str(value) if value is not None else None
        return None

    async def _apply_config(self, kind: str, wanted: str | None) -> None:
        conn, sid = self._conn, self._acp_session
        if conn is None or not sid or not wanted:
            return
        choice = self.spec.config_value(self._config_options, kind, wanted)
        if choice is None:
            if self._config_options:
                logger.info("ACP agent offers no %s %r; keeping its own choice", kind, wanted)
            return
        config_id, value = choice
        if self._current_config(config_id) == value:
            return
        try:
            result = await conn.request("session/set_config_option",
                                        {"sessionId": sid, "configId": config_id, "value": value})
        except AcpError as exc:
            logger.warning("ACP agent refused %s=%s: %s", config_id, value, exc.message)
            return
        if isinstance(result, Mapping) and isinstance(result.get("configOptions"), list):
            self._config_options = [o for o in result["configOptions"] if isinstance(o, dict)]
        else:
            for option in self._config_options:
                if option.get("id") == config_id:
                    option["currentValue"] = value

    async def _apply_mode(self) -> None:
        conn, sid = self._conn, self._acp_session
        if conn is None or not sid:
            return
        mode = self.spec.mode_id(self.permission_mode, self._modes)
        if mode is None:
            return
        with contextlib.suppress(AcpError):
            await conn.request("session/set_mode", {"sessionId": sid, "modeId": mode})

    async def _cancel_remote(self) -> None:
        conn, sid = self._conn, self._acp_session
        if conn is not None and sid and conn.alive:
            with contextlib.suppress(AcpError):
                await conn.notify("session/cancel", {"sessionId": sid})

    # ─── turns ──────────────────────────────────────────────────────────

    def _prompt_blocks(self, content: Sequence[Any]) -> list[dict[str, Any]]:
        images = bool(self._conn and self._conn.info and self._conn.info.prompt_images)
        blocks: list[dict[str, Any]] = []
        for block in content:
            if isinstance(block, Text) and block.text:
                blocks.append({"type": "text", "text": block.text})
            elif isinstance(block, Image):
                if images:
                    blocks.append({"type": "image", "mimeType": block.media_type, "data": block.data})
                else:
                    blocks.append({"type": "text", "text": "[an image was attached; this agent takes no images]"})
            elif isinstance(block, str) and block:
                blocks.append({"type": "text", "text": block})
        return blocks or [{"type": "text", "text": ""}]

    def _failure_event(self, sid: str, turn_id: str, exc: AcpError, *, prefix: str = "", **extra: Any) -> ErrorEvent:
        if not exc.auth_required:
            return ErrorEvent(session_id=sid, turn_id=turn_id, message=f"{prefix}{exc.message}", **extra)
        # The sign-in guidance replaces the agent's bare "Authentication
        # required". No HTTP status: nothing here was an HTTP call, and a
        # host would prefix one ("provider returned HTTP 401: ...").
        message = self.spec.auth_message(exc)
        return ErrorEvent(session_id=sid, turn_id=turn_id, message=message,
                          provider_message=message, **extra)

    async def _run_turn(self, op: UserTurn | Resume) -> None:
        meta = await self._ensure_session()
        sid = meta.session_id
        turn_id = new_id("turn")
        try:
            try:
                await self._ensure_process()
            except AcpError as exc:
                await self._emit(self._failure_event(sid, turn_id, exc, prefix="Could not start the agent: "))
                return
            except Exception as exc:  # noqa: BLE001 — missing binary, spawn failure
                await self._emit(ErrorEvent(session_id=sid, turn_id=turn_id,
                                            message=f"Could not start the agent: {exc}"))
                return
            if isinstance(op, Resume):
                # Retry: send the unanswered prompt at the head again.
                head = await self.store.get_node(sid, meta.head_node_id) if meta.head_node_id else None
                if (head is not None and head.message.role == "assistant" and head.parent_id
                        and (head.message.provider_meta or {}).get("interrupted")):
                    # A reply cut off by an error is not an answer (as in native.py).
                    head = await self.store.get_node(sid, head.parent_id)
                if head is None or head.message.role != "user":
                    await self._emit(ErrorEvent(session_id=sid, turn_id=turn_id,
                                                message="Nothing to resume: the last message was answered."))
                    return
                user_node = head
                content = [b for b in head.message.content if isinstance(b, (Text, Image))]
                if op.context_note:
                    content.append(Text(op.context_note))
            else:
                content = [Text(op.content)] if isinstance(op.content, str) else list(op.content)
                user_node = SessionNode(
                    node_id=new_id("node"), parent_id=op.parent_node_id or meta.head_node_id,
                    message=Message(role="user", content=content, context_pin=op.context_pin),
                    **({"ts": op.accepted_at} if op.accepted_at is not None else {}),
                )
                await self.store.append(sid, user_node)
            await self._emit(TurnStarted(session_id=sid, node_id=user_node.node_id, turn_id=turn_id,
                                         model=self.model or self._current_config("model") or "",
                                         provider=self.provider, source=op.source))
            if self._interrupted.is_set():
                await self._emit(Interrupted(session_id=sid, node_id=user_node.node_id, turn_id=turn_id))
                return
            await self._drive(sid, turn_id, user_node, content)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — every turn ends on a terminal event
            logger.warning("ACP turn failed", exc_info=True)
            await self._emit(ErrorEvent(session_id=sid, turn_id=turn_id, message=repr(exc)))
        finally:
            self._mapper = None
            self._steer_requests = []
            self._stop_reason = None
            with contextlib.suppress(Exception):
                await flush_events(self.store, sid)

    async def _drive(self, sid: str, turn_id: str, user_node: SessionNode, content: list[Any]) -> None:
        conn = self._conn
        assert conn is not None and self._acp_session is not None
        mapper = TurnMapper(session_id=sid, turn_id=turn_id, bridged_name=self._bridged_name)
        self._mapper = mapper
        failure: AcpError | None = None
        result: Any = None
        try:
            # A stop that came while the process was starting: nothing is sent.
            if self._stop_reason is None:
                result = await conn.request("session/prompt", {
                    "sessionId": self._acp_session, "prompt": self._prompt_blocks(content),
                })
            # Steering prompts sent during the turn end with it.
            for task in list(self._steer_requests):
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(task, timeout=30)
        except AcpError as exc:
            failure = exc
        trailing, segments = mapper.finish()
        for event in trailing:
            await self._emit(event)
        parent = user_node
        for role, blocks in segments:
            node = SessionNode(node_id=new_id("node"), parent_id=parent.node_id,
                               message=Message(role="assistant" if role == "assistant" else "user", content=blocks))
            await self.store.append(sid, node)
            parent = node
        usage, cost = self._usage(result, mapper)
        if usage:
            await self._emit(Usage(session_id=sid, turn_id=turn_id,
                                   input_tokens=usage.get("input_tokens", 0),
                                   output_tokens=usage.get("output_tokens", 0),
                                   cache_read_tokens=usage.get("cache_read_tokens", 0),
                                   cache_creation_tokens=usage.get("cache_creation_tokens", 0),
                                   raw={"acp_usage": (result or {}).get("usage") if isinstance(result, Mapping) else None}))
        stopped, self._stop_reason = self._stop_reason, None
        if stopped:
            await self._stop_process()
            await self._emit(ErrorEvent(session_id=sid, turn_id=turn_id, message=stopped,
                                        usage=usage, cost_usd=cost))
            return
        if failure is not None:
            if self._interrupted.is_set():
                await self._emit(Interrupted(session_id=sid, node_id=parent.node_id, turn_id=turn_id))
                return
            if not conn.alive:
                await self._stop_process()
            await self._emit(self._failure_event(sid, turn_id, failure, usage=usage, cost_usd=cost))
            return
        stop = str((result or {}).get("stopReason") or "end_turn") if isinstance(result, Mapping) else "end_turn"
        if stop == "cancelled" or (self._interrupted.is_set() and stop != "end_turn"):
            await self._emit(Interrupted(session_id=sid, node_id=parent.node_id, turn_id=turn_id,
                                         usage=usage, cost_usd=cost))
            return
        reported = self.spec.turn_failure(mapper.text)
        if reported:
            await self._emit(ErrorEvent(session_id=sid, turn_id=turn_id, message=reported,
                                        provider_message=" | ".join(conn.stderr_tail[-3:]) or None,
                                        usage=usage, cost_usd=cost))
            return
        if stop == "refusal":
            await self._emit(ErrorEvent(session_id=sid, turn_id=turn_id,
                                        message="The agent refused this request.", usage=usage, cost_usd=cost))
            return
        await self._emit(TurnComplete(session_id=sid, node_id=parent.node_id, turn_id=turn_id,
                                      stop_reason=stop, num_model_calls=1 if mapper.text or segments else 0,
                                      usage=usage, cost_usd=cost))

    @staticmethod
    def _usage(result: Any, mapper: TurnMapper) -> tuple[dict[str, int], float | None]:
        """Only what the agent reported: never estimated."""
        usage: dict[str, int] = {}
        raw = result.get("usage") if isinstance(result, Mapping) else None
        if isinstance(raw, Mapping):
            for ours, theirs in (("input_tokens", "inputTokens"), ("output_tokens", "outputTokens"),
                                 ("cache_read_tokens", "cachedReadTokens"),
                                 ("cache_creation_tokens", "cachedWriteTokens"),
                                 ("thinking_tokens", "thoughtTokens"), ("total_tokens", "totalTokens")):
                value = raw.get(theirs)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    usage[ours] = int(value)
        cost: float | None = None
        update = mapper.usage_update or {}
        amount = update.get("cost") if isinstance(update.get("cost"), Mapping) else None
        if amount and str(amount.get("currency") or "").upper() == "USD":
            value = amount.get("amount")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                cost = float(value)
        return usage, cost

    async def _steer(self, text: str, steer_id: str | None, origin: str) -> None:
        conn, sid = self._conn, self._acp_session
        running = self._turn_task is not None and not self._turn_task.done() and self._mapper is not None
        if not running or conn is None or not sid:
            raise RuntimeError("no running turn to steer")
        task = asyncio.create_task(conn.request("session/prompt", {
            "sessionId": sid, "prompt": [{"type": "text", "text": text}],
        }))
        self._steer_requests.append(task)
        mapper = self._mapper
        await self._emit(SteerDelivered(session_id=self.session_id, turn_id=mapper.turn_id if mapper else None,
                                        steers=[{"id": steer_id, "text": text, "origin": origin}]))

    # ─── agent → client ─────────────────────────────────────────────────

    async def _on_notification(self, method: str, params: dict[str, Any]) -> None:
        if method != "session/update":
            return
        update = params.get("update") if isinstance(params.get("update"), Mapping) else None
        mapper = self._mapper
        if update is None:
            return
        if mapper is None:
            # Between turns: keep config / mode changes, drop content.
            kind = update.get("sessionUpdate")
            if kind == "config_option_update" and isinstance(update.get("configOptions"), list):
                self._config_options = [o for o in update["configOptions"] if isinstance(o, dict)]
            return
        for event in mapper.map_update(update):
            await self._emit(event)
        if mapper.config_options is not None:
            self._config_options, mapper.config_options = mapper.config_options, None

    async def _on_request(self, method: str, params: dict[str, Any]) -> Any:
        if method == "session/request_permission":
            return await self._permission(params)
        if method in ("elicitation/create", "session/elicitation"):
            return await self._elicit(params)
        raise RpcFailure(-32601, f"{method} is not supported by this client")

    async def _policy_denial(self, name: str, data: dict[str, Any]) -> str | None:
        if self.tool_policy is None:
            return None
        try:
            extra: dict[str, Any] = {"cwd": self.cwd} if _accepts(self.tool_policy, "cwd") else {}
            verdict = await asyncio.to_thread(self.tool_policy, name, data, **extra)
            if inspect.isawaitable(verdict):
                verdict = await verdict
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — fails closed
            logger.error("tool policy failed for %s; denying the call", name, exc_info=True)
            return f"tool policy error ({type(exc).__name__}); call refused"
        return verdict or None

    @staticmethod
    def _option(options: Sequence[Mapping[str, Any]], *kinds: str) -> str | None:
        for kind in kinds:
            for option in options:
                if option.get("kind") == kind and option.get("optionId"):
                    return str(option["optionId"])
        return None

    async def _permission(self, params: dict[str, Any]) -> dict[str, Any]:
        call = params.get("toolCall") if isinstance(params.get("toolCall"), Mapping) else {}
        options = [o for o in params.get("options") or [] if isinstance(o, Mapping)]
        cancelled = {"outcome": {"outcome": "cancelled"}}

        def chose(*kinds: str) -> dict[str, Any]:
            option_id = self._option(options, *kinds)
            return {"outcome": {"outcome": "selected", "optionId": option_id}} if option_id else cancelled

        mapper = self._mapper
        if mapper is not None:
            name, data = mapper.tool_name_and_input(call)
            # The prompt's own tool card: start it now so the transcript
            # shows what is being asked about.
            for event in mapper.map_update({"sessionUpdate": "tool_call", **call}):
                await self._emit(event)
        else:
            name, data = TurnMapper(session_id="", turn_id="").tool_name_and_input(call)
        if self._interrupted.is_set():
            return cancelled
        denial = await self._policy_denial(name, data)
        if denial:
            logger.info("ACP permission for %s refused by policy: %s", name, denial)
            if mapper is not None and call.get("toolCallId"):
                # The card says why (as the delegated leg's PreToolUse denial
                # does), not just "failed": the agent only hears "rejected".
                for event in mapper.map_update({
                    "sessionUpdate": "tool_call_update", "toolCallId": call["toolCallId"],
                    "status": "failed",
                    "content": [{"type": "content", "content": {"type": "text", "text": denial}}],
                }):
                    await self._emit(event)
            return chose("reject_once", "reject_always")
        if self.permission_mode == "bypassPermissions":
            return chose("allow_once", "allow_always")
        if self.permission_mode == "plan":
            return chose("reject_once", "reject_always")
        request_id = new_id("perm")
        tool_use_id = str(call.get("toolCallId") or request_id)
        fut: asyncio.Future[RespondPermission] = asyncio.get_running_loop().create_future()
        self._pending_permissions[request_id] = fut
        try:
            watched = self._session is not None and self.bus.subscriber_count(self.session_id) > 0
            timeout = PERMISSION_TIMEOUT if watched else UNWATCHED_PERMISSION_TIMEOUT
            await self._emit(PermissionRequest(
                session_id=self.session_id, turn_id=mapper.turn_id if mapper else None,
                request_id=request_id, tool_use_id=tool_use_id, name=name, input=data,
                risk="write" if str(call.get("kind")) in ("edit", "delete", "move", "execute") else "safe",
                reason=str(call.get("title") or "") or None,
            ))
            try:
                response = await asyncio.wait_for(fut, timeout=timeout)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                return cancelled if self._interrupted.is_set() else chose("reject_once", "reject_always")
        finally:
            self._pending_permissions.pop(request_id, None)
        if response.decision == "allow_always":
            return chose("allow_always", "allow_once")
        if response.decision == "allow":
            return chose("allow_once", "allow_always")
        return chose("reject_once", "reject_always")

    async def _elicit(self, params: dict[str, Any]) -> dict[str, Any]:
        mapper = self._mapper
        if self._session is None:
            raise RpcFailure(INVALID_PARAMS, "no session")
        message = str(params.get("message") or "The agent has a question.")
        schema = params.get("requestedSchema") if isinstance(params.get("requestedSchema"), Mapping) else None
        request_id = new_id("question")
        fut: asyncio.Future[RespondQuestion] = asyncio.get_running_loop().create_future()
        self._pending_questions[request_id] = fut
        try:
            # A blocking question the agent waits on. Named as fluid's AskUser,
            # not the CLI's AskUserQuestion: hosts post an AskUserQuestion and
            # deny it at once, which would answer the agent with nothing.
            await self._emit(QuestionRequest(
                session_id=self.session_id, turn_id=mapper.turn_id if mapper else None,
                request_id=request_id, tool_use_id=request_id, name="AskUser",
                message=message, schema=dict(schema) if schema else None,
            ))
            try:
                answer = await asyncio.wait_for(fut, timeout=QUESTION_TIMEOUT)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                return {"action": "cancel"}
        finally:
            self._pending_questions.pop(request_id, None)
        await self._emit(QuestionAnswered(session_id=self.session_id, turn_id=mapper.turn_id if mapper else None,
                                          request_id=request_id, answer=answer.answer,
                                          user_answered=answer.user_answered))
        return {"action": "accept", "content": _elicitation_content(answer.answer, schema)}


def _elicitation_content(answer: Any, schema: Mapping[str, Any] | None) -> dict[str, Any]:
    """The host's answer in the shape the agent asked for."""
    if isinstance(answer, Mapping):
        return dict(answer)
    props = (schema or {}).get("properties") if isinstance((schema or {}).get("properties"), Mapping) else {}
    key = next(iter(props), "answer") if props else "answer"
    return {key: answer if isinstance(answer, (str, int, float, bool)) else str(answer)}
