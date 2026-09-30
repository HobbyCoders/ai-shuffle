"""Per-tool call bounds for in-process server tools.

``ToolRegistry`` cancels an in-process call after ``external_call_timeout`` so
a wedged handler cannot hang the turn. A tool with a legitimate long block and
its own deadline (the delegated leg's synchronous Task) registers an override
instead of being cancelled mid-run.
"""

from __future__ import annotations

import asyncio

from fluid.tools.inproc import create_inproc_server, tool
from fluid.tools.registry import ToolCtx, ToolRegistry, current_tool_use_id


def _server(name: str = "srv"):
    @tool("slow", "sleeps", {"type": "object", "properties": {}})
    async def slow(args: dict) -> dict:
        await asyncio.sleep(0.4)
        return {"content": [{"type": "text", "text": "finished"}]}

    @tool("slower", "sleeps too", {"type": "object", "properties": {}})
    async def slower(args: dict) -> dict:
        await asyncio.sleep(0.4)
        return {"content": [{"type": "text", "text": "also finished"}]}

    return create_inproc_server(name, [slow, slower])


async def test_unbounded_override_lets_a_long_call_finish() -> None:
    reg = ToolRegistry(external_call_timeout=0.1)
    reg.register_inproc(_server(), call_timeouts={"slow": None})

    out = await reg.dispatch("mcp__srv__slow", {}, ToolCtx())

    assert not out.is_error
    assert out.text == "finished"


async def test_a_tool_without_an_override_keeps_the_registry_bound() -> None:
    reg = ToolRegistry(external_call_timeout=0.1)
    reg.register_inproc(_server(), call_timeouts={"slow": None})

    out = await reg.dispatch("mcp__srv__slower", {}, ToolCtx())

    assert out.is_error
    assert out.error_code == "tool_timeout"


async def test_numeric_override_replaces_the_registry_bound() -> None:
    reg = ToolRegistry(external_call_timeout=0.1)
    reg.register_inproc(_server(), call_timeouts={"slow": 5.0, "slower": 0.05})

    ok = await reg.dispatch("mcp__srv__slow", {}, ToolCtx())
    cut = await reg.dispatch("mcp__srv__slower", {}, ToolCtx())

    assert not ok.is_error
    assert cut.error_code == "tool_timeout"
    assert "0.05 s" in cut.text


async def test_register_inproc_without_overrides_is_unchanged() -> None:
    reg = ToolRegistry(external_call_timeout=0.1)
    reg.register_inproc(_server())

    out = await reg.dispatch("mcp__srv__slow", {}, ToolCtx())

    assert out.error_code == "tool_timeout"


async def test_dispatch_publishes_the_ctx_tool_use_id() -> None:
    seen: list[str | None] = []

    @tool("probe", "reads the id", {"type": "object", "properties": {}})
    async def probe(args: dict) -> dict:
        seen.append(current_tool_use_id.get())
        return {"content": [{"type": "text", "text": "ok"}]}

    reg = ToolRegistry()
    reg.register_inproc(create_inproc_server("p", [probe]))

    await reg.dispatch("mcp__p__probe", {}, ToolCtx(extra={"tool_use_id": "toolu_1"}))
    await reg.dispatch("mcp__p__probe", {}, ToolCtx())

    assert seen == ["toolu_1", None]
    assert current_tool_use_id.get() is None


async def test_an_id_less_call_does_not_inherit_the_enclosing_calls_id() -> None:
    """A call made from inside another call's context (a worker started by a
    bridged Task copies it) must not read the outer call's tool_use_id."""
    seen: list[str | None] = []

    @tool("probe", "reads the id", {"type": "object", "properties": {}})
    async def probe(args: dict) -> dict:
        seen.append(current_tool_use_id.get())
        return {"content": [{"type": "text", "text": "ok"}]}

    reg = ToolRegistry()
    reg.register_inproc(create_inproc_server("p", [probe]))

    token = current_tool_use_id.set("toolu_outer")
    try:
        await reg.dispatch("mcp__p__probe", {}, ToolCtx())
        assert current_tool_use_id.get() == "toolu_outer"
    finally:
        current_tool_use_id.reset(token)

    assert seen == [None]
