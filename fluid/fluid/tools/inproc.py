"""Owned in-process tool servers and Python shorthand schema normalization.

Mirrors the ``@tool(name, description, schema)`` decorator + ``create_sdk_mcp_server``
pattern a host app's own tool modules already use, so porting them is an
import-line change. Handlers are async, take an ``args`` dict, and return the
SDK envelope ``{"content": [{"type": "text", "text": ...}]}`` — but a plain
str or a ``ToolOutcome`` is accepted too (normalized at dispatch).

Registered through ``ToolRegistry.register_inproc``, each tool surfaces as
``mcp__<server>__<tool>``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

RawHandler = Callable[[dict[str, Any]], Awaitable[Any]]


@dataclass
class InprocTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: RawHandler
    risk: str = "safe"
    annotations: Any = None

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
    annotations: Any = None,
) -> Callable[[RawHandler], InprocTool]:
    """Decorator turning an async ``args -> result`` function into an InprocTool."""

    def decorate(fn: RawHandler) -> InprocTool:
        return InprocTool(
            name=name, description=description, input_schema=input_schema,
            handler=fn, risk=risk, annotations=annotations,
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

def input_schema(schema: Any) -> dict[str, Any]:
    """Normalize JSON Schema or the host tools' Python type shorthand."""
    from types import UnionType
    from typing import Literal, NotRequired, Required, Union, get_args, get_origin, get_type_hints, is_typeddict

    def convert(value: Any) -> dict[str, Any]:
        scalar = {str: "string", int: "integer", float: "number", bool: "boolean",
                  type(None): "null", dict: "object", list: "array"}
        if value in scalar:
            return {"type": scalar[value]}
        origin, args = get_origin(value), get_args(value)
        if origin in (Required, NotRequired):
            return convert(args[0])
        if is_typeddict(value):
            props = {k: convert(v) for k, v in get_type_hints(value, include_extras=True).items()}
            return {"type": "object", "properties": props,
                    "required": [k for k in props if k in value.__required_keys__]}
        if origin is list:
            return {"type": "array", "items": convert(args[0]) if args else {}}
        if origin is dict:
            return {"type": "object", "additionalProperties": convert(args[1]) if args else {}}
        if origin in (Union, UnionType):
            return {"anyOf": [convert(arg) for arg in args]}
        if origin is Literal:
            return {"enum": list(args), **(convert(type(args[0])) if args else {})}
        return {}

    if isinstance(schema, dict):
        if isinstance(schema.get("type"), str):
            return schema
        return {"type": "object", "properties": {k: convert(v) for k, v in schema.items()},
                "required": list(schema)}
    if is_typeddict(schema):
        return convert(schema)
    return {"type": "object"}
