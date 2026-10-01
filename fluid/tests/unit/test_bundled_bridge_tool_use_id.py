"""The CLI's tool_use id crosses the bundled bridge.

Claude Code sends the id of the tool call it is making in the tools/call
request's ``_meta`` (``claudecode/toolUseId``, seen in 2.1.285). The bridge
used to drop it: hooks saw ``tool_use_id=None`` and host tools that key their
UI card or background task on the id (Task, Monitor) had nothing to key on.
"""

from __future__ import annotations

import asyncio
import inspect
from contextlib import asynccontextmanager
from typing import Any

import mcp.types as t
import pytest

from fluid.hooks.bus import POST_TOOL_USE, PRE_TOOL_USE, HookBus, HookDecision
from fluid.tools.inproc import create_inproc_server, tool
from fluid.tools.mcp.bundled_server import (
    TOOL_USE_ID_META_KEY,
    _answer,
    _tool_use_id_from_meta,
    build_child_server,
    serve_registry_socket,
)
from fluid.tools.registry import (
    ToolCtx,
    ToolOutcome,
    ToolRegistry,
    ToolSpec,
    current_tool_use_id,
)


def _modern_server_api() -> bool:
    from mcp.server import Server

    return "on_list_tools" in inspect.signature(Server.__init__).parameters


@asynccontextmanager
async def _served(reg: ToolRegistry, ctx: ToolCtx, hooks: HookBus | None = None):
    server = await serve_registry_socket(
        reg, "tcp:127.0.0.1:0", ctx, token="tok", hooks=hooks,
    )
    try:
        port = server.sockets[0].getsockname()[1]
        yield build_child_server(f"tcp:127.0.0.1:{port}", "tok")
    finally:
        server.close()
        await server.wait_closed()


async def _call_through_session(child: Any, name: str, meta: dict | None) -> Any:
    """Drive the child through a real MCP client session, as the CLI does."""
    if _modern_server_api():  # pragma: no cover - mcp>=2.0 only
        params = t.CallToolRequestParams.model_validate(
            {"name": name, "arguments": {}, **({"_meta": meta} if meta else {})}
        )
        return await child.get_request_handler("tools/call").handler(None, params)
    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(child) as client:
        await client.list_tools()
        return await client.call_tool(name, {}, meta=meta)


def _recording_registry() -> tuple[ToolRegistry, list[tuple[str, Any]]]:
    seen: list[tuple[str, Any]] = []
    reg = ToolRegistry()

    async def builtin(args: dict, ctx: ToolCtx) -> ToolOutcome:
        seen.append(("ctx", ctx.extra.get("tool_use_id")))
        seen.append(("var", current_tool_use_id.get()))
        return ToolOutcome(text="ok")

    reg.register_builtin(
        ToolSpec(name="Probe", description="d", input_schema={"type": "object", "properties": {}}),
        builtin,
    )

    @tool("probe", "inproc probe", {"type": "object", "properties": {}})
    async def inproc_probe(args: dict) -> dict:
        seen.append(("inproc", current_tool_use_id.get()))
        return {"content": [{"type": "text", "text": "ok"}]}

    reg.register_inproc(create_inproc_server("host", [inproc_probe]))
    return reg, seen


async def test_meta_id_reaches_hooks_and_the_handler() -> None:
    reg, seen = _recording_registry()
    bus = HookBus()
    hook_ids: list[tuple[str, Any]] = []

    async def record(event: str, payload: dict) -> HookDecision:
        hook_ids.append((event, payload.get("tool_use_id")))
        return HookDecision()

    async def allowed(payload: dict) -> None:
        hook_ids.append(("allowed", payload.get("tool_use_id")))

    bus.on(PRE_TOOL_USE, record)
    bus.on(POST_TOOL_USE, record)
    bus.on_tool_allowed(allowed)

    async with _served(reg, ToolCtx(), bus) as child:
        result = await _call_through_session(
            child, "Probe", {TOOL_USE_ID_META_KEY: "toolu_X"},
        )

    assert result.content[0].text == "ok"
    assert hook_ids == [
        (PRE_TOOL_USE, "toolu_X"), ("allowed", "toolu_X"), (POST_TOOL_USE, "toolu_X"),
    ]
    assert seen == [("ctx", "toolu_X"), ("var", "toolu_X")]


async def test_inproc_tool_reads_the_id_from_the_context_var() -> None:
    reg, seen = _recording_registry()
    async with _served(reg, ToolCtx()) as child:
        await _call_through_session(
            child, "mcp__host__probe", {TOOL_USE_ID_META_KEY: "toolu_Y"},
        )
    assert seen == [("inproc", "toolu_Y")]
    assert current_tool_use_id.get() is None


async def test_a_call_without_meta_still_works() -> None:
    reg, seen = _recording_registry()
    bus = HookBus()
    hook_ids: list[Any] = []

    async def record(event: str, payload: dict) -> HookDecision:
        hook_ids.append(payload.get("tool_use_id"))
        return HookDecision()

    bus.on(PRE_TOOL_USE, record)
    async with _served(reg, ToolCtx(), bus) as child:
        result = await _call_through_session(child, "Probe", None)

    assert result.content[0].text == "ok"
    assert hook_ids == [None]
    assert seen == [("ctx", None), ("var", None)]


async def test_handler_writes_to_extra_outlive_the_call() -> None:
    """The per-call view of ctx.extra writes through to the session's dict,
    so state a handler keeps there survives, while the call's own id does
    not leak into it."""
    reg = ToolRegistry()

    async def keeps_state(args: dict, ctx: ToolCtx) -> ToolOutcome:
        ctx.extra.setdefault("seen_ids", []).append(ctx.extra["tool_use_id"])
        return ToolOutcome(text="ok")

    reg.register_builtin(
        ToolSpec(name="Keep", description="d", input_schema={"type": "object"}), keeps_state,
    )
    ctx = ToolCtx(extra={"host": 1})
    for tid in ("toolu_1", "toolu_2"):
        await _answer(reg, ctx, {"op": "call", "name": "Keep", "arguments": {},
                                 "tool_use_id": tid})

    assert ctx.extra["seen_ids"] == ["toolu_1", "toolu_2"]
    assert "tool_use_id" not in ctx.extra
    assert ctx.extra["host"] == 1


async def test_concurrent_calls_keep_their_own_ids() -> None:
    reg = ToolRegistry()
    seen: dict[str, Any] = {}

    async def slow(args: dict, ctx: ToolCtx) -> ToolOutcome:
        await asyncio.sleep(0.05 if args.get("first") else 0)
        seen[args["label"]] = (ctx.extra["tool_use_id"], current_tool_use_id.get())
        return ToolOutcome(text="ok")

    reg.register_builtin(
        ToolSpec(name="Slow", description="d", input_schema={"type": "object"}), slow,
    )
    ctx = ToolCtx()
    await asyncio.gather(
        _answer(reg, ctx, {"op": "call", "name": "Slow", "tool_use_id": "toolu_a",
                           "arguments": {"label": "a", "first": True}}),
        _answer(reg, ctx, {"op": "call", "name": "Slow", "tool_use_id": "toolu_b",
                           "arguments": {"label": "b"}}),
    )
    assert seen == {"a": ("toolu_a", "toolu_a"), "b": ("toolu_b", "toolu_b")}


@pytest.mark.parametrize("meta,expected", [
    (None, None),
    ({}, None),
    ({TOOL_USE_ID_META_KEY: "toolu_Z"}, "toolu_Z"),
    ({TOOL_USE_ID_META_KEY: 5}, None),
    ({TOOL_USE_ID_META_KEY: ""}, None),
])
def test_tool_use_id_from_meta(meta: Any, expected: Any) -> None:
    assert _tool_use_id_from_meta(meta) == expected
    if meta is not None:
        # Through the request model, so the test holds on every mcp release:
        # 1.x parses ``_meta`` into ``RequestParams.Meta`` (extra fields
        # allowed), 2.x keeps it as a mapping. The helper accepts both.
        params = t.CallToolRequestParams.model_validate(
            {"name": "x", "arguments": {}, "_meta": meta}
        )
        assert _tool_use_id_from_meta(params.meta) == expected
