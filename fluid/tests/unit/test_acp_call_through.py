"""The ACP bridge's deferral: a small loaded core, the rest behind CallTool.

ACP agents (Google's ``agy acp``) have no deferral of their own. With
``AcpLoop(deferred=...)`` the bundled bridge lists the core with schemas plus
``LoadTools`` (every other tool announced by name) and ``CallTool`` (runs one
by its real name). These drive the real AcpLoop and bridge socket end to end
against the scripted agent in tests/fixtures/fake_acp_agent.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fluid.events.model import PermissionRequest, ToolUse, TurnComplete
from fluid.events.ops import RespondPermission, UserTurn
from fluid.hooks.bus import PRE_TOOL_USE, HookBus, HookDecision
from fluid.tools.inproc import create_inproc_server, tool
from fluid.tools.mcp.call_through import CALL_TOOL, LOAD_TOOL, catalog_rows, unwrap_call
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolRegistry, ToolSpec

from tests.unit.test_acp_engine import TERMINAL, _driver

SCHEMA = {"type": "object", "properties": {"text": {"type": "string"}}}


def _registry(ran: list[tuple[str, dict]]) -> ToolRegistry:
    registry = ToolRegistry()

    def handler(name: str):
        async def run(args: dict, ctx: ToolCtx) -> ToolOutcome:
            ran.append((name, args))
            return ToolOutcome(text=f"{name} ran {args.get('text')}")
        return run

    for name in ("Core", "Echo", "Hidden"):
        registry.register_builtin(ToolSpec(name=name, description=f"{name} tool", input_schema=SCHEMA),
                                  handler(name))
    paint_run = handler("mcp__media__paint")

    @tool("paint", "paint a picture", SCHEMA)
    async def paint(args: dict) -> dict:
        outcome = await paint_run(args, ToolCtx())
        return {"content": [{"type": "text", "text": outcome.text}]}

    # A tool the registry itself keeps deferred is served behind CallTool too.
    registry.register_inproc(create_inproc_server("media", [paint]), defer=True)
    return registry


async def _deferred_driver(tmp_path: Path, ran: list, **kw: Any):
    return await _driver(
        tmp_path, registry=_registry(ran), ctx=ToolCtx(cwd=str(tmp_path)),
        tool_filter=lambda name: name != "Hidden", deferred=lambda name: name != "Core", **kw,
    )


def _served_list(tmp_path: Path) -> list[dict]:
    return json.loads((tmp_path / "tools_list.json").read_text())


async def test_the_agent_is_served_the_core_and_the_rest_by_name(tmp_path: Path) -> None:
    d = await _deferred_driver(tmp_path, [])
    try:
        end = await d.turn("mcp __list__ {}", timeout=60)
        assert isinstance(end, TurnComplete), end
        tools = {t["name"]: t for t in _served_list(tmp_path)}
        assert list(tools) == ["Core", LOAD_TOOL, CALL_TOOL]
        assert tools["Core"]["inputSchema"] == SCHEMA
        catalog = tools[LOAD_TOOL]["description"]
        # Announced by name, grouped by server; the filtered tool is not.
        assert "core (<tool>): Echo" in catalog
        assert "media (mcp__media__<tool>): paint" in catalog
        assert "Hidden" not in catalog and "paint a picture" not in catalog
        assert d.loop.bridged_tools() == ["Core", LOAD_TOOL, CALL_TOOL]
        assert d.loop.deferred_tools() == ["Echo", "mcp__media__paint"]
    finally:
        await d.close()


async def test_load_tools_returns_schemas_without_asking(tmp_path: Path) -> None:
    d = await _deferred_driver(tmp_path, [])
    try:
        await d.turn('mcpask LoadTools {"names": ["mcp__media__paint", "Hidden"]}', timeout=60)
        assert not [e for e in d.events if isinstance(e, PermissionRequest)]
        text = d.text()
        assert '"name": "mcp__media__paint"' in text and '"input_schema"' in text
        # A tool outside the session's filter reads as one that does not exist.
        assert "Not found: Hidden" in text
        await d.turn('mcp LoadTools {"query": "picture"}', timeout=60)
        assert d.text().count('"name": "mcp__media__paint"') == 2
    finally:
        await d.close()


async def test_call_tool_runs_the_real_tool_through_policy_card_and_hooks(tmp_path: Path) -> None:
    ran: list = []
    policy_saw: list = []
    hook_saw: list = []
    hooks = HookBus()

    async def pre(event: str, payload: dict) -> HookDecision:
        hook_saw.append((payload["name"], payload["input"]))
        return HookDecision()

    hooks.on(PRE_TOOL_USE, pre)

    async def policy(name: str, data: dict) -> str | None:
        policy_saw.append((name, data))
        return None

    d = await _deferred_driver(tmp_path, ran, hooks=hooks, tool_policy=policy)
    try:
        await d.loop.submit(UserTurn(
            content='mcpask CallTool {"name": "mcp__media__paint", "input": {"text": "cat"}}'))
        ask = await d.until(PermissionRequest)
        # The owner's card and saved rules see the real tool and its input.
        assert ask.name == "mcp__fluid_bundled__mcp__media__paint"
        assert ask.input == {"text": "cat"}
        assert policy_saw == [("mcp__fluid_bundled__mcp__media__paint", {"text": "cat"})]
        await d.loop.submit(RespondPermission(request_id=ask.request_id, decision="allow"))
        end = await d.until(TERMINAL, timeout=60)
        assert isinstance(end, TurnComplete), end
        assert ran == [("mcp__media__paint", {"text": "cat"})]
        assert hook_saw == [("mcp__media__paint", {"text": "cat"})]
        assert "mcp said: mcp__media__paint ran cat" in d.text()
        uses = [e for e in d.events if isinstance(e, ToolUse)]
        assert {u.name for u in uses} == {"mcp__fluid_bundled__mcp__media__paint"}

        # "Always" on the card allows a CallTool call once: the agent must
        # not remember CallTool itself. A directly listed tool keeps it.
        assert "chose=allow " in d.text()
        await d.loop.submit(UserTurn(content='mcpask CallTool {"name": "Echo", "input": {"text": "a"}}'))
        ask = await d.until(PermissionRequest)
        await d.loop.submit(RespondPermission(request_id=ask.request_id, decision="allow_always"))
        await d.until(TERMINAL, timeout=60)
        assert d.text().count("chose=allow ") == 2 and "chose=always" not in d.text()
        assert ran[-1] == ("Echo", {"text": "a"})
        await d.loop.submit(UserTurn(content='mcpask Core {"text": "b"}'))
        ask = await d.until(PermissionRequest)
        assert ask.name == "mcp__fluid_bundled__Core"
        await d.loop.submit(RespondPermission(request_id=ask.request_id, decision="allow_always"))
        await d.until(TERMINAL, timeout=60)
        assert "chose=always" in d.text()
        del ran[1:]

        # Denied on the card: nothing runs.
        await d.loop.submit(UserTurn(content='mcpask CallTool {"name": "Echo", "input": {"text": "x"}}'))
        ask = await d.until(PermissionRequest)
        assert ask.name == "mcp__fluid_bundled__Echo"
        await d.loop.submit(RespondPermission(request_id=ask.request_id, decision="deny"))
        await d.until(TERMINAL, timeout=60)
        assert ran == [("mcp__media__paint", {"text": "cat"})]
        assert d.text().endswith("mcp denied")
    finally:
        await d.close()


async def test_call_tool_refuses_a_tool_outside_the_filter(tmp_path: Path) -> None:
    ran: list = []
    d = await _deferred_driver(tmp_path, ran)
    try:
        await d.loop.submit(UserTurn(content='mcpask CallTool {"name": "Hidden", "input": {"text": "x"}}'))
        ask = await d.until(PermissionRequest)
        # Not a served tool: the card stays on the proxy, never the hidden name.
        assert ask.name == "mcp__fluid_bundled__CallTool"
        await d.loop.submit(RespondPermission(request_id=ask.request_id, decision="allow"))
        await d.until(TERMINAL, timeout=60)
        assert ran == []
        assert "tool not available in this session: Hidden" in d.text()
    finally:
        await d.close()


async def test_without_deferral_the_bridge_lists_every_tool(tmp_path: Path) -> None:
    d = await _driver(tmp_path, registry=_registry([]), ctx=ToolCtx(cwd=str(tmp_path)),
                      tool_filter=lambda name: name != "Hidden")
    try:
        await d.turn("mcp __list__ {}", timeout=60)
        # The registry's own deferred tier stays off an eager bridge, as before.
        assert [t["name"] for t in _served_list(tmp_path)] == ["Core", "Echo"]
        assert d.loop.deferred_tools() == []
    finally:
        await d.close()


def test_unwrap_and_catalog_helpers() -> None:
    assert unwrap_call({"name": "mcp__fluid_bundled__Echo", "input": '{"a": 1}'}) == ("Echo", {"a": 1})
    assert unwrap_call({"name": "Echo", "input": "nope"}) == ("Echo", {})
    assert unwrap_call(None) == ("", {})
    assert catalog_rows(["mcp__b__y", "Zed", "mcp__b__x", "mcp__a__q"]) == [
        "core (<tool>): Zed", "a (mcp__a__<tool>): q", "b (mcp__b__<tool>): x, y",
    ]
