"""In-process tool servers — surface-compatible with claude_agent_sdk.

Mirrors the ``@tool(name, description, schema)`` decorator + ``create_sdk_mcp_server``
pattern AI-Shuffle's ~10 tool modules already use, so porting them is an
import-line change. Handlers are async, take an ``args`` dict, and return the
SDK envelope ``{"content": [{"type": "text", "text": ...}]}`` — but a plain
str or a ``ToolOutcome`` is accepted too (normalized at dispatch).

Registered through ``ToolRegistry.register_inproc``, each tool surfaces as
``mcp__<server>__<tool>``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

RawHandler = Callable[[dict[str, Any]], Awaitable[Any]]


@dataclass
class InprocTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: RawHandler
    risk: str = "safe"

    def as_handler(self) -> Callable[[dict[str, Any], Any], Awaitable[Any]]:
        """Adapt the SDK-style ``args``-only handler to the registry's
        ``(input, ctx)`` calling convention (ctx is ignored for inproc tools)."""
        async def _call(input: dict[str, Any], _ctx: Any = None) -> Any:
            return await self.handler(input)
        return _call


def tool(
    name: str,
    description: str,
    input_schema: dict[str, Any],
    *,
    risk: str = "safe",
) -> Callable[[RawHandler], InprocTool]:
    """Decorator turning an async ``args -> result`` function into an InprocTool."""

    def decorate(fn: RawHandler) -> InprocTool:
        return InprocTool(
            name=name, description=description, input_schema=input_schema,
            handler=fn, risk=risk,
        )

    return decorate


@dataclass
class InprocServer:
    name: str
    version: str = "1.0.0"
    tools: list[InprocTool] = field(default_factory=list)


def create_inproc_server(
    server_name: str, tools: list[InprocTool], *, version: str = "1.0.0"
) -> InprocServer:
    """Bundle decorated tools into a named in-process server."""
    return InprocServer(name=server_name, version=version, tools=list(tools))
