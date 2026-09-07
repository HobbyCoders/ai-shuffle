"""MCP tool names as the provider will accept them.

A server picks its own tool names; the provider only takes ``[A-Za-z0-9_-]``
up to 64 characters, and one bad name fails every request for the rest of the
session. Registration rewrites the name, remembers the server's original for
dispatch, and refuses to replace a builtin or in-proc tool.
"""

import logging

from fluid.tools.mcp.client import ToolDescriptor
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolRegistry, ToolSpec, api_tool_name

VALID = __import__("re").compile(r"^[a-zA-Z0-9_-]{1,64}$")


class _FakeManager:
    """Stands in for ``McpManager``: a fixed catalog and a dispatch log."""

    def __init__(self, tools: list[str]) -> None:
        self.tools = tools
        self.calls: list[tuple[str, str]] = []

    async def connect(self, name, config):
        return [
            ToolDescriptor(name=t, description=f"tool {t}", input_schema={"type": "object"})
            for t in self.tools
        ]

    async def dispatch(self, server, tool, input, *, input_responses=None, request_state=None):
        self.calls.append((server, tool))
        return ToolOutcome(text="ok")

    async def aclose(self):
        pass


async def _registry(tools: list[str]) -> tuple[ToolRegistry, _FakeManager, list[ToolSpec]]:
    reg = ToolRegistry()
    manager = _FakeManager(tools)
    reg._manager = manager  # type: ignore[assignment]
    specs = await reg.connect_mcp("srv", {})
    return reg, manager, specs


async def test_odd_names_are_rewritten_and_dispatched_by_original(caplog) -> None:
    long_name = "x" * 90
    with caplog.at_level(logging.WARNING):
        reg, manager, specs = await _registry(["get weather!", long_name, "ünïcode", "plain"])

    names = [s.name for s in specs]
    assert all(VALID.match(n) for n in names), names
    assert "mcp__srv__get_weather_" in names
    assert "mcp__srv__plain" in names
    assert len({n for n in names}) == 4
    changed = [r.getMessage() for r in caplog.records if "made valid" in r.getMessage()]
    assert len(changed) == 3

    for spec in specs:
        await reg.dispatch(spec.name, {}, ToolCtx())
    assert [tool for _, tool in manager.calls] == ["get weather!", long_name, "ünïcode", "plain"]


async def test_two_names_that_sanitize_alike_stay_distinct() -> None:
    reg, manager, specs = await _registry(["get weather", "get_weather", "get-weather"])
    names = [s.name for s in specs]
    assert len(set(names)) == 3
    assert "mcp__srv__get_weather" in names and "mcp__srv__get-weather" in names
    for spec in specs:
        await reg.dispatch(spec.name, {}, ToolCtx())
    assert [tool for _, tool in manager.calls] == ["get weather", "get_weather", "get-weather"]


def test_long_names_keep_the_server_prefix_and_differ_by_digest() -> None:
    a = api_tool_name("srv", "a" * 100, lambda n: False)
    b = api_tool_name("srv", "a" * 100 + "b", lambda n: False)
    assert a.startswith("mcp__srv__") and b.startswith("mcp__srv__")
    assert len(a) == 64 and len(b) == 64
    assert a != b


async def test_server_cannot_shadow_a_builtin_or_inproc_tool(caplog) -> None:
    reg = ToolRegistry()

    async def handler(input, ctx):
        return ToolOutcome(text="local")

    reg.register_builtin(
        ToolSpec(name="mcp__srv__Read", description="local", input_schema={}), handler
    )
    reg._manager = _FakeManager(["Read", "other"])  # type: ignore[assignment]
    with caplog.at_level(logging.WARNING):
        specs = await reg.connect_mcp("srv", {})

    assert [s.name for s in specs] == ["mcp__srv__other"]
    assert reg.get("mcp__srv__Read").source == "builtin"
    assert "mcp__srv__Read" in {s.name for s in reg.list_tools(loaded_only=True)}
    assert "mcp__srv__other" not in {s.name for s in reg.list_tools(loaded_only=True)}
    out = await reg.dispatch("mcp__srv__Read", {}, ToolCtx())
    assert out.text == "local"
    assert any("skipped" in r.getMessage() for r in caplog.records)
