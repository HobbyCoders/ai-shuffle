"""The Antigravity (ACP) leg defers app and external MCP tools through CallTool.

ACP has no deferral of its own, so before this the bundled bridge listed every
app tool with its schema (~98 tools, ~26.5k tokens per request on the code
lane) and the session's external MCP servers went to the agent whole. Now the
bridge lists ``ACP_LOADED_TOOLS`` plus fluid's ``LoadTools`` (the rest by name)
and ``CallTool`` (runs one by its real name), and external servers connect
host-side so they ride the same proxy.

The end-to-end case drives the real ``_connect_antigravity`` with the scripted
agent (fluid/tests/fixtures/fake_acp_agent.py) behind the binary override and
a real stdio MCP server (fluid/tests/fixtures/fake_mcp_server.py).
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

import app.core.provider.harness_adapter as ha
from app.core.provider.fluid_tool_bridge import ACP_LOADED_TOOLS, BRIDGE_DEFER_ENV
from app.core.provider.types import SessionOptions
from tests.core.test_antigravity_provider import _create, client, fake_agy  # noqa: F401
from tests.core.test_delegated_bridge_tiers import _isolated, _servers  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
FAKE_MCP = str(ROOT / "fluid" / "tests" / "fixtures" / "fake_mcp_server.py")


def _options(tmp_path, lane="code", **kw):
    options = SessionOptions(model="m", cwd=str(tmp_path), permission_mode="default",
                             mcp_servers=_servers(str(tmp_path)), **kw)
    options.extra["session_mode"] = lane
    if lane == "chat":
        from app.core.session_modes import CHAT_MODE_TOOL_SURFACE
        options.tool_allowlist = list(CHAT_MODE_TOOL_SURFACE)
    return options


async def _listing(tmp_path, options, call_through):
    from fluid.tools.mcp.bundled_server import _answer
    from fluid.tools.registry import ToolCtx

    registry = await ha.HarnessAdapter()._build_registry(options, native=False)
    blocked = ha._delegated_disallowed(options, registry)
    surface = ha._delegated_allowed(options)
    reply = await _answer(
        registry, ToolCtx(cwd=str(tmp_path)), {"op": "list"},
        tool_filter=lambda n: ha._bridged_tool_permitted(n, surface, blocked),
        call_through=call_through,
    )
    return {tool["name"]: tool for tool in reply["tools"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("lane, core", [
    ("code", ["mcp__builtins__Task", "mcp__builtins__Monitor"]),
    ("chat", ["mcp__builtins__Task", "mcp__attachments__Read"]),
])
async def test_the_bridge_lists_the_core_and_announces_the_rest(tmp_path, lane, core):
    options = _options(tmp_path, lane)
    deferral = ha._acp_bridge_deferral(options)
    assert deferral is not None
    eager = await _listing(tmp_path, options, None)
    tools = await _listing(tmp_path, options, deferral)
    assert list(tools) == [*core, "LoadTools", "CallTool"]
    assert set(core) <= ACP_LOADED_TOOLS
    catalog = tools["LoadTools"]["description"]
    for name in eager:
        if name not in ACP_LOADED_TOOLS:
            _server, _, tool = name.removeprefix("mcp__").partition("__")
            # Every other served tool is announced by name under its server.
            assert tool in catalog if tool else name in catalog, name
    assert "app-config (mcp__app-config__<tool>)" in catalog
    assert len(json.dumps(list(tools.values()))) < len(json.dumps(list(eager.values()))) / 4
    if lane == "chat":
        # The chat ceiling still applies: a code-lane-only tool is not announced.
        code_only = set(await _listing(tmp_path, _options(tmp_path), None)) - set(eager)
        assert code_only
        for name in code_only:
            assert f" {name.rsplit('__', 1)[-1]}," not in catalog.replace("\n", ",") + ","


def test_no_deferral_when_toolsearch_is_off_the_surface_or_switched_off(tmp_path, monkeypatch):
    assert ha._acp_bridge_deferral(_options(tmp_path)) is not None
    # A deferred tool must never become unreachable.
    assert ha._acp_bridge_deferral(_options(tmp_path, disallowed_tools=["ToolSearch"])) is None
    assert ha._acp_bridge_deferral(_options(tmp_path, tool_allowlist=["Read", "mcp__builtins__Task"])) is None
    proactive = _options(tmp_path)
    proactive.extra["assistant_proactive"] = {"kind": "cron"}
    assert ha._acp_bridge_deferral(proactive) is None
    monkeypatch.setenv(BRIDGE_DEFER_ENV, "0")
    assert ha._acp_bridge_deferral(_options(tmp_path)) is None


async def _run_turn(engine, text, timeout=60):
    from fluid.events.model import ErrorEvent, Interrupted, TextDelta, TurnComplete
    from fluid.events.ops import UserTurn

    events = []
    await engine._ensure_session()

    async def pump():
        async for event in engine.events():
            events.append(event)
            if isinstance(event, (TurnComplete, ErrorEvent, Interrupted)):
                return

    task = asyncio.create_task(pump())
    await asyncio.sleep(0)
    await engine.submit(UserTurn(content=text))
    await asyncio.wait_for(task, timeout)
    return "".join(e.text for e in events if isinstance(e, TextDelta)), events


async def _connected(tmp_path, client, monkeypatch, *, defer=True):  # noqa: F811
    from fluid.store.base import MemoryStore

    if not defer:
        monkeypatch.setenv(BRIDGE_DEFER_ENV, "0")
    row = _create(client, "gemini-api-key", api_key="test-gemini-secret-value")
    servers = _servers(str(tmp_path))
    servers["weather"] = {"type": "stdio", "command": sys.executable, "args": ["-I", FAKE_MCP],
                          "env": {"FLUID_FAKE_INSTRUCTIONS": "Weather server rules: cite the city."}}
    options = SessionOptions(model="gemini-3.1-pro", cwd=str(tmp_path), permission_mode="bypassPermissions",
                             mcp_servers=servers, extra={"provider_id": row["id"], "app_session_id": "s-acp"})
    adapter = ha.HarnessAdapter()
    adapter._options = options
    # connect()'s Antigravity steps, in order.
    adapter._acp_deferred = ha._acp_bridge_deferral(options)
    adapter._registry = await adapter._build_registry(
        options, native=False, host_mcp=adapter._acp_deferred is not None)
    await adapter._connect_antigravity(options, MemoryStore(), "fs-acp", None, "Be brief.", False)
    return adapter


@pytest.mark.asyncio
async def test_external_servers_connect_host_side_and_run_through_call_tool(
        tmp_path, client, monkeypatch):  # noqa: F811
    adapter = await _connected(tmp_path, client, monkeypatch)
    engine = adapter._engine
    try:
        assert adapter._host_mcp_servers == {"weather"}
        # The agent is not handed the server; its tools are announced instead.
        assert "weather" not in engine.mcp_servers
        assert engine.bridged_tools() == ["mcp__builtins__Task", "mcp__builtins__Monitor",
                                          "LoadTools", "CallTool"]
        assert "mcp__weather__get_weather" in engine.deferred_tools()
        # Its own instructions still reach the agent, with the proxy note.
        assert "Weather server rules: cite the city." in engine.system_append
        assert "mcp__fluid_bundled__CallTool" in engine.system_append
        text, events = await _run_turn(
            engine, 'mcp CallTool {"name": "mcp__weather__get_weather", "input": {"city": "Oslo"}}')
        assert "Oslo" in text, text
        from fluid.events.model import ToolUse
        assert [e.name for e in events if isinstance(e, ToolUse)] == [
            "mcp__fluid_bundled__mcp__weather__get_weather"]
    finally:
        await engine.aclose()
        await adapter._registry.aclose()


@pytest.mark.asyncio
async def test_kill_switch_restores_the_eager_bridge_and_agent_side_servers(
        tmp_path, client, monkeypatch):  # noqa: F811
    adapter = await _connected(tmp_path, client, monkeypatch, defer=False)
    engine = adapter._engine
    try:
        assert engine.deferred is None and adapter._host_mcp_servers == set()
        assert "weather" in engine.mcp_servers
        assert "CallTool" not in engine.bridged_tools()
        assert len(engine.bridged_tools()) > 50
    finally:
        await engine.aclose()
        await adapter._registry.aclose()
