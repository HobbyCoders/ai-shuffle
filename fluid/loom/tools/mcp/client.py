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

Caching: ``tools/list`` honors the spec's ``ttlMs``/``cacheScope`` freshness
hints. Within the TTL a repeat ``list_tools`` is served from cache without a
round-trip; a legacy server that sends no TTL is simply never cached.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any


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
    scope: str | None = None


class McpClient:
    def __init__(self, name: str, config: dict[str, Any]) -> None:
        self.name = name
        self.config = config
        self.protocol: str | None = None
        self.legacy: bool = False
        self.list_fetches: int = 0  # underlying tools/list round-trips (test/obs hook)
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
        await self._started  # propagates any startup error

    async def aclose(self) -> None:
        if self._cmd_q is not None:
            await self._cmd_q.put(None)
        if self._task is not None:
            try:
                await self._task
            except Exception:
                pass

    async def _serve(self) -> None:
        # Imports are local so the harness only needs ``mcp`` when MCP is used.
        from mcp.client.session import ClientSession
        try:
            transport = self.config.get("transport", "stdio")
            if transport == "stdio":
                from mcp.client.stdio import StdioServerParameters, stdio_client
                params = StdioServerParameters(
                    command=self.config["command"],
                    args=list(self.config.get("args", [])),
                    env=self.config.get("env"),
                    cwd=self.config.get("cwd"),
                )
                cm = stdio_client(params)
            elif transport in ("http", "streamable_http"):
                from mcp.client.streamable_http import streamablehttp_client
                cm = streamablehttp_client(self.config["url"])
            else:
                raise ValueError(f"unsupported MCP transport: {transport!r}")

            async with cm as streams:
                read, write = streams[0], streams[1]
                async with ClientSession(read, write) as session:
                    try:
                        await session.discover()  # modern 2026-07-28 handshake
                        self.legacy = False
                    except Exception:
                        await session.initialize()  # legacy 2025-era fallback
                        self.legacy = True
                    self.protocol = session.protocol_version
                    assert self._started is not None
                    if not self._started.done():
                        self._started.set_result(None)
                    await self._command_loop(session)
        except Exception as exc:
            if self._started is not None and not self._started.done():
                self._started.set_exception(exc)

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
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        await self._cmd_q.put((fut, method, args, kwargs))
        return await fut

    # ─── Session handlers (run on the actor task) ────────────────────────

    async def _handle(self, session: Any, method: str, args: tuple, kwargs: dict) -> Any:
        if method == "list":
            return await self._do_list(session)
        if method == "call":
            return await self._do_call(session, *args, **kwargs)
        raise ValueError(f"unknown mcp command: {method}")

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
                input_schema=t.input_schema or {"type": "object"},
                risk=_risk_from_annotations(annotations),
                annotations=annotations,
            ))
        # ``ttlMs``/``cacheScope`` are native on the 2.0 SDK; fall back to the
        # raw dict for older SDKs that don't surface them as attributes.
        ttl_ms = _field(result, "ttl_ms", "ttlMs")
        scope = _field(result, "cache_scope", "cacheScope")
        expires = now + ttl_ms / 1000.0 if ttl_ms else None
        self._cache = _ListCache(tools=tools, expires_at=expires, scope=scope)
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
            result = await session.call_tool(tool, input or {}, **kwargs)
        except TypeError:
            # Legacy SDKs lack ``allow_input_required``/MRTR kwargs.
            result = await session.call_tool(tool, input or {})
        return _map_call_result(result)

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
    text_parts = [
        _field(c, "text") for c in content
        if (_field(c, "type") == "text") and _field(c, "text") is not None
    ]
    return {
        "kind": "result",
        "text": "".join(text_parts),
        "is_error": bool(_field(result, "is_error", "isError")),
        "structured": _model_dump(_field(result, "structured_content", "structuredContent")),
    }


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
