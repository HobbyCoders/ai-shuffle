"""A generic Agent Client Protocol (ACP) client over a subprocess's stdio.

ACP (https://agentclientprotocol.com) is JSON-RPC 2.0, one JSON object per
line, both directions: the client sends ``initialize``, ``session/new``,
``session/prompt`` and friends; the agent streams ``session/update``
notifications and may call back into the client (``session/request_permission``,
``elicitation/create``, ``fs/*``, ``terminal/*``).

This module knows the wire format only. What to do with each update, and how a
particular agent is launched, lives with the caller (see ``loop.py`` for the
engine and ``antigravity.py`` for one agent's specifics).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
# A single update can carry a whole file diff or an embedded image.
STREAM_LINE_LIMIT = 16 * 1024 * 1024
# JSON-RPC / ACP error codes the client acts on.
AUTH_REQUIRED = -32000
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

NotificationHandler = Callable[[str, dict[str, Any]], Awaitable[None]]
RequestHandler = Callable[[str, dict[str, Any]], Awaitable[Any]]


class AcpError(Exception):
    """An error response from the agent (or a dead connection)."""

    def __init__(self, code: int, message: str, data: Any = None, *, method: str = "") -> None:
        super().__init__(f"{method + ': ' if method else ''}{message} (code {code})")
        self.code = code
        self.message = message
        self.data = data
        self.method = method

    @property
    def auth_required(self) -> bool:
        return self.code == AUTH_REQUIRED


class AcpConnectionClosed(AcpError):
    def __init__(self, detail: str = "the agent process exited") -> None:
        super().__init__(INTERNAL_ERROR, detail)


class RpcFailure(Exception):
    """Raised by a request handler to answer the agent with a JSON-RPC error."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class AgentInfo:
    """What ``initialize`` advertised, kept verbatim plus a few lookups."""

    raw: dict[str, Any]

    @property
    def capabilities(self) -> dict[str, Any]:
        return dict(self.raw.get("agentCapabilities") or {})

    @property
    def auth_methods(self) -> list[dict[str, Any]]:
        return [m for m in self.raw.get("authMethods") or [] if isinstance(m, dict)]

    @property
    def name(self) -> str:
        return str((self.raw.get("agentInfo") or {}).get("name") or "")

    @property
    def version(self) -> str:
        return str((self.raw.get("agentInfo") or {}).get("version") or "")

    def session_capability(self, name: str) -> bool:
        caps = self.capabilities
        session = caps.get("sessionCapabilities") or caps.get("session_capabilities") or {}
        if name == "load" and caps.get("loadSession"):
            return True
        return isinstance(session, Mapping) and name in session

    @property
    def prompt_images(self) -> bool:
        return bool((self.capabilities.get("promptCapabilities") or {}).get("image"))

    @property
    def mcp_transports(self) -> set[str]:
        """stdio always (the protocol requires it), plus what the agent lists."""
        mcp = self.capabilities.get("mcpCapabilities") or {}
        return {"stdio", *(k for k in ("http", "sse") if mcp.get(k))}

    @property
    def standard_system_prompt(self) -> bool:
        session = self.capabilities.get("sessionCapabilities") or {}
        return isinstance(session, Mapping) and "systemPrompt" in session


def _terminate_tree(proc: asyncio.subprocess.Process, sig: int) -> None:
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        if sys.platform != "win32":
            os.killpg(proc.pid, sig)
            return
    with contextlib.suppress(ProcessLookupError, OSError):
        if sig == getattr(signal, "SIGKILL", None):
            proc.kill()
        else:
            proc.terminate()


class AcpConnection:
    """One agent subprocess and its JSON-RPC session.

    ``on_notification`` receives every notification (``session/update``
    mostly). ``on_request`` answers agent→client requests; it may raise
    :class:`RpcFailure`, and anything else becomes an internal error so the
    agent is never left waiting. Unhandled requests get ``METHOD_NOT_FOUND``.
    """

    def __init__(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        on_notification: NotificationHandler | None = None,
        on_request: RequestHandler | None = None,
        stderr_sink: Callable[[str], None] | None = None,
    ) -> None:
        self.argv = list(argv)
        self.cwd = cwd
        self.env = dict(env) if env is not None else None
        self.on_notification = on_notification
        self.on_request = on_request
        self.stderr_sink = stderr_sink
        self._proc: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next_id = 0
        self._reader: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._handlers: set[asyncio.Task[None]] = set()
        self._write_lock = asyncio.Lock()
        self._closed = asyncio.Event()
        self.stderr_tail: list[str] = []
        self.info: AgentInfo | None = None

    # ─── lifecycle ──────────────────────────────────────────────────────

    @property
    def pid(self) -> int | None:
        proc = self._proc
        return proc.pid if proc is not None and proc.returncode is None else None

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None and not self._closed.is_set()

    async def start(self) -> None:
        kwargs: dict[str, Any] = {}
        if sys.platform != "win32":
            # Its own process group: shutdown reaches the agent's children too.
            kwargs["start_new_session"] = True
        self._proc = await asyncio.create_subprocess_exec(
            *self.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
            env=self.env,
            limit=STREAM_LINE_LIMIT,
            **kwargs,
        )
        self._reader = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._read_stderr())

    async def initialize(
        self, client_capabilities: Mapping[str, Any], client_info: Mapping[str, Any] | None = None,
    ) -> AgentInfo:
        params: dict[str, Any] = {
            "protocolVersion": PROTOCOL_VERSION,
            "clientCapabilities": dict(client_capabilities),
        }
        if client_info:
            params["clientInfo"] = dict(client_info)
        result = await self.request("initialize", params)
        self.info = AgentInfo(result if isinstance(result, dict) else {})
        return self.info

    async def close(self, *, grace: float = 3.0) -> None:
        """Close stdin, give the agent ``grace`` seconds, then end its tree."""
        proc = self._proc
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(Exception):
                if proc.stdin is not None and not proc.stdin.is_closing():
                    proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), timeout=grace)
            except (asyncio.TimeoutError, Exception):
                _terminate_tree(proc, signal.SIGTERM)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=grace)
                except (asyncio.TimeoutError, Exception):
                    _terminate_tree(proc, getattr(signal, "SIGKILL", signal.SIGTERM))
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(proc.wait(), timeout=grace)
        self._fail_pending(AcpConnectionClosed("the connection was closed"))
        self._closed.set()
        for task in (self._reader, self._stderr_task, *self._handlers):
            if task is not None and not task.done():
                task.cancel()
        for task in (self._reader, self._stderr_task, *self._handlers):
            if task is not None:
                with contextlib.suppress(BaseException):
                    await task

    # ─── outgoing ───────────────────────────────────────────────────────

    async def _write(self, message: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.returncode is not None or self._closed.is_set():
            raise AcpConnectionClosed()
        data = (json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
        async with self._write_lock:
            try:
                proc.stdin.write(data)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, RuntimeError) as exc:
                raise AcpConnectionClosed(f"could not write to the agent: {exc!r}") from exc

    async def request(self, method: str, params: Mapping[str, Any] | None = None, *,
                      timeout: float | None = None) -> Any:
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": dict(params or {})})
            result = await (asyncio.wait_for(fut, timeout) if timeout else fut)
        except AcpError as exc:
            if not exc.method:
                exc.method = method
                exc.args = (f"{method}: {exc.message} (code {exc.code})",)
            raise
        finally:
            self._pending.pop(rid, None)
        return result

    async def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": dict(params or {})})

    # ─── incoming ───────────────────────────────────────────────────────

    def _fail_pending(self, exc: AcpError) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(exc)

    async def _read_loop(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        try:
            while True:
                try:
                    line = await proc.stdout.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    logger.warning("ACP agent wrote a line over %d bytes; dropped", STREAM_LINE_LIMIT)
                    continue
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    message = json.loads(text)
                except json.JSONDecodeError:
                    logger.debug("ACP agent wrote a non-JSON line: %.200s", text)
                    continue
                if isinstance(message, dict):
                    await self._dispatch(message)
        finally:
            tail = " | ".join(self.stderr_tail[-3:])
            self._fail_pending(AcpConnectionClosed(
                "the agent process exited" + (f": {tail}" if tail else "")
            ))
            self._closed.set()

    async def _read_stderr(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        while True:
            try:
                line = await proc.stderr.readline()
            except (ValueError, asyncio.LimitOverrunError):
                continue
            if not line:
                return
            text = line.decode("utf-8", errors="replace").rstrip()
            if not text:
                continue
            self.stderr_tail = [*self.stderr_tail[-19:], text[:500]]
            if self.stderr_sink is not None:
                with contextlib.suppress(Exception):
                    self.stderr_sink(text)

    async def _dispatch(self, message: dict[str, Any]) -> None:
        if "method" not in message:
            rid = message.get("id")
            fut = self._pending.get(rid) if isinstance(rid, int) else None
            if fut is None or fut.done():
                return
            error = message.get("error")
            if isinstance(error, dict):
                fut.set_exception(AcpError(
                    int(error.get("code") or INTERNAL_ERROR), str(error.get("message") or "error"),
                    error.get("data"),
                ))
            else:
                fut.set_result(message.get("result"))
            return
        method = str(message.get("method"))
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if "id" not in message:
            if self.on_notification is not None:
                try:
                    await self.on_notification(method, params)
                except Exception:  # noqa: BLE001 — one bad update never ends the stream
                    logger.warning("ACP notification %s handler failed", method, exc_info=True)
            return
        # A request: answered off the reader, since an answer may wait on a
        # person (a permission prompt) while updates keep streaming.
        task = asyncio.create_task(self._answer(message.get("id"), method, params))
        self._handlers.add(task)
        task.add_done_callback(self._handlers.discard)

    async def _answer(self, rid: Any, method: str, params: dict[str, Any]) -> None:
        reply: dict[str, Any] = {"jsonrpc": "2.0", "id": rid}
        try:
            if self.on_request is None:
                raise RpcFailure(METHOD_NOT_FOUND, f"{method} is not supported by this client")
            reply["result"] = await self.on_request(method, params)
        except RpcFailure as exc:
            reply["error"] = {"code": exc.code, "message": exc.message}
        except asyncio.CancelledError:
            reply["error"] = {"code": INTERNAL_ERROR, "message": "the client stopped"}
            with contextlib.suppress(Exception):
                await self._write(reply)
            raise
        except Exception as exc:  # noqa: BLE001 — the agent must always get an answer
            logger.warning("ACP request %s handler failed", method, exc_info=True)
            reply["error"] = {"code": INTERNAL_ERROR, "message": f"client error: {exc}"}
        with contextlib.suppress(AcpError):
            await self._write(reply)
