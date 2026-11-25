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
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from fluid.tools.job_object import assign_kill_on_close_job, close_job

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
# A single update can carry a whole file diff or an embedded image. A longer
# line cannot be read whole, so the connection ends (``_read_loop``).
STREAM_LINE_LIMIT = 16 * 1024 * 1024
# Every write to the agent's stdin finishes within this or the connection is
# taken as broken: an agent that stopped reading must not hold the write lock
# (and with it ``session/cancel``) forever.
WRITE_TIMEOUT = 30.0
# ``initialize`` and other replies the engine cannot go on without.
INITIALIZE_TIMEOUT = 60.0
# ``close``: how long the agent gets to exit after stdin closes, and how long
# its tree gets to die after it is killed. Kept short: the host closes an
# engine under its own deadline (``AcpLoop.aclose``).
CLOSE_GRACE = 1.0
_GROUP_GRACE = 0.5
_KILL_WAIT = 1.0
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


class AcpTimeout(AcpError):
    """The agent sent no reply to a request within its timeout."""

    def __init__(self, method: str, timeout: float) -> None:
        super().__init__(INTERNAL_ERROR, f"the agent did not answer within {timeout:g}s", method=method)


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


async def _end_agent(proc: asyncio.subprocess.Process, grace: float) -> None:
    """Let the agent exit on its closed stdin for ``grace`` seconds, then end
    its tree; and end what is left of its process group either way.

    The agent runs in its own process group (POSIX), and an MCP server or a
    shell command it started stays in that group after the agent itself is
    gone. ``kill_tree`` (via ``_kill_cli_tree``) reaches the group while the
    agent lives and ``_end_process_group`` once it has exited. Cancelled
    during the grace (the host's deadline came first), the tree is killed at
    once: nothing else holds its handle. Windows' kill-on-close job is closed
    by the caller.
    """
    from fluid.engine.delegated import _end_process_group, _kill_cli_tree

    if proc.returncode is not None:
        await _end_process_group(proc.pid, grace=_GROUP_GRACE)
        return
    with contextlib.suppress(Exception):
        if proc.stdin is not None and not proc.stdin.is_closing():
            proc.stdin.close()
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace)
        await _end_process_group(proc.pid, grace=_GROUP_GRACE)
        return
    except (asyncio.TimeoutError, ProcessLookupError):
        pass
    except asyncio.CancelledError:
        logger.info("ending the ACP agent tree: its close was cancelled during the exit grace")
        with contextlib.suppress(Exception):
            await asyncio.wait_for(_kill_cli_tree(proc, group_grace=0), timeout=_KILL_WAIT)
        raise
    logger.info("ending the ACP agent tree: still running %.1fs after its stdin closed", grace)
    with contextlib.suppress(Exception):
        await asyncio.wait_for(_kill_cli_tree(proc, group_grace=_GROUP_GRACE), timeout=_KILL_WAIT)


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
        # Windows: a kill-on-close job holding the agent and all it starts.
        self._job: int | None = None
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
        # Windows has no process group to end once the agent is gone, and
        # ``taskkill /T`` cannot reach an orphaned grandchild: the job can.
        self._job = assign_kill_on_close_job(self._proc.pid)
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
        result = await self.request("initialize", params, timeout=INITIALIZE_TIMEOUT)
        self.info = AgentInfo(result if isinstance(result, dict) else {})
        return self.info

    def abort(self, detail: str) -> None:
        """Take the connection as broken: every waiting caller fails with
        ``detail`` and ``alive`` turns False, so the owner's ``close`` ends
        the process and a later turn starts a new one."""
        self._fail_pending(AcpConnectionClosed(detail))
        self._closed.set()

    async def close(self, *, grace: float = CLOSE_GRACE) -> None:
        """Close stdin, give the agent ``grace`` seconds, then end its tree.

        The tree goes also when the agent already exited on its own, and when
        this call is cancelled (``_end_agent``).
        """
        proc, job = self._proc, self._job
        self._job = None
        try:
            if proc is not None:
                await _end_agent(proc, grace)
        finally:
            # Windows: whatever the agent started, orphans included, ends here.
            close_job(job)
            self._shut_down()
        for task in (self._reader, self._stderr_task, *self._handlers):
            if task is not None:
                with contextlib.suppress(BaseException):
                    await task

    def _shut_down(self) -> None:
        self._fail_pending(AcpConnectionClosed("the connection was closed"))
        self._closed.set()
        for task in (self._reader, self._stderr_task, *self._handlers):
            if task is not None and not task.done():
                task.cancel()

    # ─── outgoing ───────────────────────────────────────────────────────

    async def _write(self, message: dict[str, Any], *, timeout: float = WRITE_TIMEOUT) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.returncode is not None or self._closed.is_set():
            raise AcpConnectionClosed()
        stdin = proc.stdin
        data = (json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")

        async def write() -> None:
            async with self._write_lock:
                stdin.write(data)
                await stdin.drain()

        try:
            await asyncio.wait_for(write(), timeout)
        except asyncio.TimeoutError as exc:
            # The agent stopped reading (or a writer ahead of this one is
            # stuck). A frame may be half written: nothing more can be sent.
            detail = f"the agent did not read its input within {timeout:g}s"
            self.abort(detail)
            raise AcpConnectionClosed(detail) from exc
        except (BrokenPipeError, ConnectionResetError, RuntimeError) as exc:
            raise AcpConnectionClosed(f"could not write to the agent: {exc!r}") from exc

    async def request(self, method: str, params: Mapping[str, Any] | None = None, *,
                      timeout: float | None = None) -> Any:
        """Send a request and wait for its reply; ``timeout`` bounds the wait
        for the reply (None: as long as it takes, for ``session/prompt``)."""
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": dict(params or {})})
            try:
                result = await (asyncio.wait_for(fut, timeout) if timeout else fut)
            except asyncio.TimeoutError:
                raise AcpTimeout(method, timeout or 0) from None
        except AcpError as exc:
            if not exc.method:
                exc.method = method
                exc.args = (f"{method}: {exc.message} (code {exc.code})",)
            raise
        finally:
            self._pending.pop(rid, None)
            if fut.done() and not fut.cancelled():
                fut.exception()  # failed (``abort``) while its write was still under way
        return result

    async def notify(self, method: str, params: Mapping[str, Any] | None = None, *,
                     timeout: float = WRITE_TIMEOUT) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": dict(params or {})}, timeout=timeout)

    # ─── incoming ───────────────────────────────────────────────────────

    def _fail_pending(self, exc: AcpError) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(exc)

    async def _read_loop(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        detail = "the agent process exited"
        try:
            while True:
                try:
                    line = await proc.stdout.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    # The line could be any message, a reply included: dropping
                    # it could leave its caller waiting forever. The stream is
                    # not trusted past it, so the connection ends, and the next
                    # turn starts a new process on the saved session.
                    logger.warning("ACP agent wrote a line over %d bytes; ending the connection",
                                   STREAM_LINE_LIMIT)
                    detail = f"the agent sent a message over {STREAM_LINE_LIMIT // (1024 * 1024)} MiB"
                    break
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
            self.abort(detail + (f": {tail}" if tail else ""))

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
