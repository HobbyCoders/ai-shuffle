"""MCP 2.0 client (spec 2026-07-28) on the official ``mcp`` Python SDK.

Transport: stdio (required) and streamable HTTP (best-effort). Handshake
prefers the modern ``server/discover`` (negotiating 2026-07-28); on failure it
falls back to legacy ``initialize`` (2025-era servers — e.g. browser-mcp — keep
working).

Concurrency: an ``mcp`` ``ClientSession`` owns an anyio task group and its I/O
must stay on the task that opened it. So each client runs a single **actor**
task that owns the session for its whole lifetime; callers submit commands over
an ``asyncio.Queue`` and await a future. This keeps all session traffic
single-threaded regardless of which task calls ``list_tools``/``call_tool``.

Caching: ``tools/list`` honors the spec's ``ttlMs`` freshness hint (the cache
is per client, so ``cacheScope`` is not consulted). Within the TTL a repeat
``list_tools`` is served from cache without a round-trip; a legacy server that
sends no TTL is simply never cached.

Deadlines: every await on the server is bounded. ``connect_timeout`` covers
opening the transport plus the handshake, ``call_timeout`` covers one
``tools/call`` (and, via the SDK's ``read_timeout_seconds``, every other
request), ``shutdown_timeout`` covers teardown. Each is a per-server config
key; the defaults below apply when the key is absent.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_CONNECT_TIMEOUT_S = 30.0
# One tools/call, so a server that never answers cannot hold the turn open.
# 60 s is generous for any tool that is answering at all; a server that needs
# longer sets call_timeout.
DEFAULT_CALL_TIMEOUT_S = 60.0
DEFAULT_SHUTDOWN_TIMEOUT_S = 5.0


def _timeout_from(config: dict[str, Any], key: str, default: float) -> float:
    """A positive number of seconds from ``config[key]``, else ``default``."""
    raw = config.get(key)
    try:
        value = float(raw) if raw is not None else default
    except (TypeError, ValueError):
        log.warning("mcp: ignoring non-numeric %s=%r, using %g s", key, raw, default)
        return default
    return value if value > 0 else default


@dataclass
class ToolDescriptor:
    name: str
    description: str
    input_schema: dict[str, Any]
    risk: str = "safe"
    annotations: dict[str, Any] | None = None


@dataclass
class _ListCache:
    tools: list[ToolDescriptor]
    expires_at: float | None  # monotonic deadline; None = do not cache


class McpClient:
    def __init__(self, name: str, config: dict[str, Any]) -> None:
        self.name = name
        self.config = config
        self.protocol: str | None = None
        self.legacy: bool = False
        self.list_fetches: int = 0  # underlying tools/list round-trips (test/obs hook)
        self.connect_timeout = _timeout_from(config, "connect_timeout", DEFAULT_CONNECT_TIMEOUT_S)
        self.call_timeout = _timeout_from(config, "call_timeout", DEFAULT_CALL_TIMEOUT_S)
        self.shutdown_timeout = _timeout_from(
            config, "shutdown_timeout", DEFAULT_SHUTDOWN_TIMEOUT_S
        )
        self._cmd_q: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None
        self._started: asyncio.Future | None = None
        self._cache: _ListCache | None = None

    # ─── Lifecycle ───────────────────────────────────────────────────────

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._cmd_q = asyncio.Queue()
        self._started = loop.create_future()
        self._task = asyncio.create_task(self._serve(), name=f"mcp-{self.name}")
        # The actor enforces ``connect_timeout`` itself; this outer deadline
        # only catches a transport whose open ignores cancellation, so the
        # caller gets an error instead of a stalled first turn.
        try:
            await asyncio.wait_for(
                asyncio.shield(self._started), self.connect_timeout + DEFAULT_SHUTDOWN_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            self._task.cancel()
            raise TimeoutError(
                f"MCP server {self.name!r} did not finish connecting within "
                f"{self.connect_timeout:g} s"
            ) from None

    async def aclose(self) -> None:
        """Stop the actor; never waits longer than ``shutdown_timeout`` twice.

        A polite stop first (the actor exits its command loop and the transport
        tears the server down); if that does not finish in time the task is
        cancelled and given the same window again, after which it is abandoned.
        """
        if self._cmd_q is not None:
            await self._cmd_q.put(None)
        task = self._task
        if task is None:
            return
        if await self._wait_task(task, self.shutdown_timeout):
            return
        task.cancel()
        if not await self._wait_task(task, self.shutdown_timeout):
            log.warning(
                "mcp: server %r did not shut down within %g s; abandoning it",
                self.name, self.shutdown_timeout,
            )

    @staticmethod
    async def _wait_task(task: asyncio.Task, timeout: float) -> bool:
        """True once ``task`` has finished (however it finished) within ``timeout``."""
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout)
        except asyncio.TimeoutError:
            return False
        except (asyncio.CancelledError, Exception):
            if not task.done():
                raise
        return True

    def _open_transport(self) -> tuple[Any, Any]:
        """The transport context manager plus an HTTP client to own, or None.

        Imports are local so the harness only needs ``mcp`` when MCP is used.
        """
        transport = self.config.get("transport", "stdio")
        if transport == "stdio":
            from mcp.client.stdio import StdioServerParameters, stdio_client
            params = StdioServerParameters(
                command=self.config["command"],
                args=list(self.config.get("args", [])),
                env=self.config.get("env"),
                cwd=self.config.get("cwd"),
            )
            return stdio_client(params), None
        if transport in ("http", "streamable_http"):
            return _open_http_transport(self.config)
        raise ValueError(f"unsupported MCP transport: {transport!r}")

    async def _serve(self) -> None:
        from mcp.client.session import ClientSession
        try:
            cm, http_client = self._open_transport()
            async with contextlib.AsyncExitStack() as stack:
                try:
                    async with asyncio.timeout(self.connect_timeout):
                        if http_client is not None:
                            await stack.enter_async_context(http_client)
                        streams = await stack.enter_async_context(cm)
                        read, write = streams[0], streams[1]
                        session = await stack.enter_async_context(
                            ClientSession(
                                read, write,
                                **_session_timeout_kwargs(ClientSession, self.call_timeout),
                                **_session_notification_kwargs(
                                    ClientSession, self._on_server_notification
                                ),
                            )
                        )
                        result = await self._handshake(session)
                except TimeoutError:
                    raise TimeoutError(
                        f"MCP server {self.name!r} did not finish its handshake within "
                        f"{self.connect_timeout:g} s"
                    ) from None
                # The handshake result carries the negotiated version on
                # every supported mcp release; ``ClientSession`` only grew a
                # ``protocol_version`` attribute later, so read the result
                # first and fall back to the attribute when present.
                self.protocol = (
                    getattr(result, "protocolVersion", None)
                    or getattr(session, "protocol_version", None)
                )
                assert self._started is not None
                if not self._started.done():
                    self._started.set_result(None)
                await self._command_loop(session)
        except Exception as exc:
            if self._started is not None and not self._started.done():
                self._started.set_exception(_unwrap_group(exc))
        finally:
            self._fail_pending("connection closed")

    async def _handshake(self, session: Any) -> Any:
        try:
            result = await session.discover()  # modern 2026-07-28 handshake
            self.legacy = False
        except Exception:
            result = await session.initialize()  # legacy 2025-era fallback
            self.legacy = True
        return result

    def _fail_pending(self, reason: str) -> None:
        """Resolve every queued command so no caller waits on a dead actor."""
        if self._cmd_q is None:
            return
        while not self._cmd_q.empty():
            cmd = self._cmd_q.get_nowait()
            if cmd is None:
                continue
            fut = cmd[0]
            if not fut.done():
                fut.set_exception(RuntimeError(f"MCP server {self.name!r}: {reason}"))

    async def _command_loop(self, session: Any) -> None:
        assert self._cmd_q is not None
        while True:
            cmd = await self._cmd_q.get()
            if cmd is None:
                return
            fut, method, args, kwargs = cmd
            if fut.cancelled():
                continue
            try:
                fut.set_result(await self._handle(session, method, args, kwargs))
            except Exception as exc:  # noqa: BLE001
                if not fut.cancelled():
                    fut.set_exception(exc)

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        assert self._cmd_q is not None, "client not started"
        task = self._task
        if task is None or task.done():
            raise RuntimeError(f"MCP server {self.name!r}: connection closed")
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        await self._cmd_q.put((fut, method, args, kwargs))
        # Race the reply against the actor itself: if the transport dies while
        # a command is queued, the caller gets an error instead of a hang.
        await asyncio.wait({fut, task}, return_when=asyncio.FIRST_COMPLETED)
        if fut.done():
            return fut.result()
        fut.cancel()
        raise RuntimeError(f"MCP server {self.name!r}: connection closed")

    # ─── Session handlers (run on the actor task) ────────────────────────

    async def _handle(self, session: Any, method: str, args: tuple, kwargs: dict) -> Any:
        if method == "list":
            return await self._do_list(session)
        if method == "call":
            return await self._do_call(session, *args, **kwargs)
        raise ValueError(f"unknown mcp command: {method}")

    async def _on_server_notification(self, message: Any) -> None:
        """Server notifications: a changed tool list drops the cached listing.

        Anything else (log messages, transport errors the SDK routes here) is
        left to the SDK's own default handling.
        """
        if _is_tool_list_changed(message):
            self._cache = None
            log.info("mcp: server %r reported a changed tool list", self.name)

    async def _do_list(self, session: Any) -> list[ToolDescriptor]:
        now = time.monotonic()
        if self._cache is not None and self._cache.expires_at is not None and now < self._cache.expires_at:
            return self._cache.tools
        self.list_fetches += 1
        result = await session.list_tools()
        tools: list[ToolDescriptor] = []
        for t in result.tools:
            annotations = _model_dump(getattr(t, "annotations", None))
            tools.append(ToolDescriptor(
                name=t.name,
                description=t.description or "",
                # ``inputSchema`` is the only spelling on current mcp releases
                # (``input_schema`` is the 2.0-SDK alias) — go through _field so
                # both work instead of raising AttributeError on the camelCase-
                # only model.
                input_schema=_field(t, "input_schema", "inputSchema") or {"type": "object"},
                risk=_risk_from_annotations(annotations),
                annotations=annotations,
            ))
        # ``ttlMs`` is native on the 2.0 SDK; fall back to the raw dict for
        # older SDKs that don't surface it as an attribute.
        ttl_ms = _field(result, "ttl_ms", "ttlMs")
        expires = now + ttl_ms / 1000.0 if ttl_ms else None
        self._cache = _ListCache(tools=tools, expires_at=expires)
        return tools

    async def _do_call(
        self,
        session: Any,
        tool: str,
        input: dict[str, Any],
        *,
        input_responses: Any = None,
        request_state: str | None = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"allow_input_required": True}
        if input_responses is not None:
            kwargs["input_responses"] = input_responses
        if request_state is not None:
            kwargs["request_state"] = request_state
        try:
            async with asyncio.timeout(self.call_timeout):
                try:
                    result = await session.call_tool(tool, input or {}, **kwargs)
                except TypeError:
                    # Legacy SDKs lack ``allow_input_required``/MRTR kwargs.
                    result = await session.call_tool(tool, input or {})
        except TimeoutError:
            return self._timed_out(tool)
        except Exception as exc:
            # The SDK's own ``read_timeout_seconds`` may fire first with the
            # same deadline; report it the same way.
            if _is_sdk_timeout(exc):
                return self._timed_out(tool)
            raise
        return _map_call_result(result)

    def _timed_out(self, tool: str) -> dict[str, Any]:
        return {
            "kind": "result",
            "text": (
                f"MCP server {self.name!r} did not respond to {tool} within "
                f"{self.call_timeout:g} s"
            ),
            "images": [],
            "is_error": True,
            "structured": None,
            # Lets the code-mode bridge keep a timeout fatal to the cell
            # while ordinary tool errors (a stale ref) become soft results.
            "error_code": "tool_timeout",
        }

    # ─── Public API ──────────────────────────────────────────────────────

    async def list_tools(self) -> list[ToolDescriptor]:
        return await self._call("list")

    async def call_tool(
        self,
        tool: str,
        input: dict[str, Any],
        *,
        input_responses: Any = None,
        request_state: str | None = None,
    ) -> dict[str, Any]:
        return await self._call(
            "call", tool, input,
            input_responses=input_responses, request_state=request_state,
        )


# ─── Transport and SDK-shape helpers ─────────────────────────────────────────


def _open_http_transport(config: dict[str, Any]) -> tuple[Any, Any]:
    """Streamable-HTTP transport for ``config``; also the httpx client to own.

    mcp 2.0 exports ``streamable_http_client`` and takes a caller-built
    ``http_client`` for headers; 1.x exported ``streamablehttp_client`` and
    took ``headers`` directly. Both are handled. A client we build is returned
    so the actor can enter it on its exit stack (the SDK only closes clients
    it created itself).
    """
    from mcp.client import streamable_http as mod

    connect = getattr(mod, "streamable_http_client", None) or getattr(
        mod, "streamablehttp_client"
    )
    url = config["url"]
    headers = config.get("headers")
    if not headers:
        return connect(url), None
    headers = {str(k): str(v) for k, v in dict(headers).items()}
    params = inspect.signature(connect).parameters
    if "http_client" in params:
        factory = getattr(mod, "create_mcp_http_client", None)
        if factory is not None:
            http_client = factory(headers=headers)
        else:
            import httpx

            http_client = httpx.AsyncClient(headers=headers, follow_redirects=True)
        return connect(url, http_client=http_client), http_client
    return connect(url, headers=headers), None


def _session_notification_kwargs(session_cls: Any, handler: Any) -> dict[str, Any]:
    """``message_handler=handler`` when this SDK's ``ClientSession`` takes one."""
    try:
        params = inspect.signature(session_cls.__init__).parameters
    except (TypeError, ValueError):
        return {}
    return {"message_handler": handler} if "message_handler" in params else {}


def _is_tool_list_changed(message: Any) -> bool:
    """True for the server's ``notifications/tools/list_changed``."""
    if type(message).__name__ == "ToolListChangedNotification":
        return True
    method = getattr(message, "method", None)
    if method is None and hasattr(message, "root"):
        method = getattr(message.root, "method", None)
    return method == "notifications/tools/list_changed"


def _session_timeout_kwargs(session_cls: Any, seconds: float) -> dict[str, Any]:
    """``read_timeout_seconds`` in the shape this SDK's ``ClientSession`` takes.

    mcp 2.0 declares it as ``float | None``; 1.x wanted a ``timedelta``. A
    session class without the parameter (a test double) gets nothing.
    """
    try:
        param = inspect.signature(session_cls.__init__).parameters.get("read_timeout_seconds")
    except (TypeError, ValueError):
        return {}
    if param is None:
        return {}
    if "timedelta" in str(param.annotation):
        return {"read_timeout_seconds": timedelta(seconds=seconds)}
    return {"read_timeout_seconds": seconds}


def _unwrap_group(exc: BaseException) -> BaseException:
    """The one real error inside the ExceptionGroups anyio task groups add.

    The transport's task group wraps whatever escaped the actor (a handshake
    timeout, say) in a group; the caller wants the leaf so ``except
    TimeoutError`` still matches. A group carrying several errors is returned
    as is.
    """
    while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
        exc = exc.exceptions[0]
    return exc


def _is_sdk_timeout(exc: Exception) -> bool:
    """An ``McpError`` the SDK raised for its own read timeout (code 408)."""
    error = getattr(exc, "error", None)
    return getattr(error, "code", None) == 408


# ─── Result mapping helpers ──────────────────────────────────────────────────


def _field(obj: Any, *names: str) -> Any:
    """Read the first present attribute, else the first present dict key."""
    for n in names:
        if hasattr(obj, n):
            v = getattr(obj, n)
            if v is not None:
                return v
    if isinstance(obj, dict):
        for n in names:
            if obj.get(n) is not None:
                return obj[n]
    return None


def _model_dump(obj: Any) -> dict[str, Any] | None:
    if obj is None:
        return None
    if hasattr(obj, "model_dump"):
        return obj.model_dump(exclude_none=True)
    if isinstance(obj, dict):
        return obj
    return None


def _risk_from_annotations(annotations: dict[str, Any] | None) -> str:
    """Read-only hints map to ``safe``; everything else defaults to ``write``."""
    if annotations and (annotations.get("readOnlyHint") or annotations.get("read_only_hint")):
        return "safe"
    if annotations and (annotations.get("destructiveHint") or annotations.get("destructive_hint")):
        return "destructive"
    return "write"


def _map_call_result(result: Any) -> dict[str, Any]:
    """Normalize a CallToolResult / InputRequiredResult into a plain dict so no
    SDK types leak off the actor task."""
    result_type = _field(result, "result_type", "resultType")
    if result_type == "input_required":
        raw_requests = _field(result, "input_requests", "inputRequests") or {}
        requests: dict[str, Any] = {}
        for req_id, req in _items(raw_requests):
            requests[req_id] = _describe_input_request(req)
        return {
            "kind": "input_required",
            "request_state": _field(result, "request_state", "requestState"),
            "requests": requests,
        }
    content = _field(result, "content") or []
    text_parts: list[str] = []
    images: list[dict[str, str]] = []
    for c in content:
        ctype = _field(c, "type")
        if ctype == "text" and _field(c, "text") is not None:
            text_parts.append(_field(c, "text"))
        elif ctype == "image" and _field(c, "data"):
            # ImageContent: base64 ``data`` + ``mimeType``; screenshot-style
            # tools answer with nothing else.
            images.append({
                "media_type": _field(c, "mime_type", "mimeType") or "image/png",
                "data": _field(c, "data"),
            })
        elif ctype == "resource":
            text_parts.append(_embedded_resource_text(_field(c, "resource")))
        elif ctype == "resource_link":
            uri = _field(c, "uri") or ""
            title = _field(c, "title") or _field(c, "name") or ""
            text_parts.append(f"[resource link: {uri}{' ' + title if title else ''}]")
        elif ctype == "audio":
            mime = _field(c, "mime_type", "mimeType") or "audio"
            size = len(_field(c, "data") or "")
            text_parts.append(f"[audio result omitted: {mime}, {size} base64 chars]")
    structured = _model_dump(_field(result, "structured_content", "structuredContent"))
    text = "".join(text_parts)
    if not text and structured:
        # A tool that answers only through structuredContent: the structure
        # is the answer, not an empty success.
        import json

        text = json.dumps(structured, ensure_ascii=False, indent=2)
    return {
        "kind": "result",
        "text": text,
        "images": images,
        "is_error": bool(_field(result, "is_error", "isError")),
        "structured": structured,
    }


def _embedded_resource_text(resource: Any) -> str:
    """Text for an EmbeddedResource: its text, or a note for a binary blob."""
    if resource is None:
        return "[resource result was empty]"
    uri = _field(resource, "uri") or ""
    text = _field(resource, "text")
    if text is not None:
        return f"[resource {uri}]\n{text}" if uri else str(text)
    mime = _field(resource, "mime_type", "mimeType") or "binary"
    size = len(_field(resource, "blob") or "")
    return f"[binary resource omitted: {uri} ({mime}, {size} base64 chars)]"


def _items(mapping: Any):
    if isinstance(mapping, dict):
        return list(mapping.items())
    if hasattr(mapping, "items"):
        return list(mapping.items())
    # a list of requests → synthesize ids
    return [(str(i), r) for i, r in enumerate(mapping or [])]


def _describe_input_request(req: Any) -> dict[str, Any]:
    method = _field(req, "method") or ""
    params = _field(req, "params")
    pd = _model_dump(params) or {}
    return {
        "method": method,
        "message": pd.get("message"),
        "schema": pd.get("requested_schema") or pd.get("requestedSchema"),
        "params": pd,
    }
