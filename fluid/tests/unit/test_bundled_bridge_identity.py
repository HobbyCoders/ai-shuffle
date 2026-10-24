"""Caller identity crosses the bundled MCP bridge, independently of tool input."""
from __future__ import annotations

import asyncio
from typing import Any

from fluid.hooks.bus import POST_TOOL_USE, PRE_TOOL_USE, HookBus, HookDecision
from fluid.tools.mcp.bundled_server import (
    TOOL_USE_ID_META_KEY, build_child_server, serve_registry_socket,
)
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolRegistry, ToolSpec
from tests.unit.test_bundled_bridge_tool_use_id import _call_through_session


async def test_bridge_identity_reaches_hooks_and_handler() -> None:
    from fluid.engine.delegated import DelegatedLoop
    from fluid.store.base import MemoryStore

    reg = ToolRegistry()
    seen: list[tuple[str, Any]] = []

    async def handler(args: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
        await asyncio.sleep(0)
        seen.append(("handler", ctx.extra.get("agent_id")))
        return ToolOutcome(text="ok")

    reg.register_builtin(ToolSpec(
        name="Probe", description="test", input_schema={"type": "object"},
    ), handler)
    bus = HookBus()

    async def record(event: str, payload: dict[str, Any]) -> HookDecision:
        seen.append((event, payload.get("agent_id")))
        return HookDecision()

    async def allowed(payload: dict[str, Any]) -> None:
        seen.append(("allowed", payload.get("agent_id")))

    bus.on(PRE_TOOL_USE, record)
    bus.on(POST_TOOL_USE, record)
    bus.on_tool_allowed(allowed)
    loop = DelegatedLoop(store=MemoryStore(), model="claude-test")
    loop._remember_bridge_agent("sub", "agent-559")
    loop._remember_bridge_agent("main", None)
    ctx = ToolCtx()
    server = await serve_registry_socket(
        reg, "tcp:127.0.0.1:0", ctx, token="tok", hooks=bus,
        agent_id_for_tool=loop._bridge_agent_id,
    )
    port = server.sockets[0].getsockname()[1]
    child = build_child_server(f"tcp:127.0.0.1:{port}", "tok")
    try:
        for tid, agent in [("sub", "agent-559"), ("main", None)]:
            seen.clear()
            await _call_through_session(child, "Probe", {TOOL_USE_ID_META_KEY: tid})
            assert seen == [(PRE_TOOL_USE, agent), ("allowed", agent),
                            ("handler", agent), (POST_TOOL_USE, agent)]
        assert "agent_id" not in ctx.extra
    finally:
        server.close()
        await server.wait_closed()


def test_stream_identity_preserves_hook_identity_and_bounds_cache() -> None:
    from fluid.engine.delegated import DelegatedLoop
    from fluid.store.base import MemoryStore

    loop = DelegatedLoop(store=MemoryStore(), model="claude-test")
    loop._remember_bridge_agent("sub", "real-agent")
    loop._track_bridge_agents({
        "type": "assistant", "parent_tool_use_id": "parent",
        "message": {"content": [{"type": "tool_use", "id": "sub"},
                                 {"type": "tool_use", "id": "fallback"}]},
    })
    assert loop._bridge_agent_id("sub") == "real-agent"
    assert loop._bridge_agent_id("fallback") == "delegated-parent:parent"
    loop._remember_bridge_agent("fallback", None)  # an older hook omits agent_id
    assert loop._bridge_agent_id("fallback") == "delegated-parent:parent"
    for index in range(4096):
        loop._remember_bridge_agent(str(index), None)
    assert len(loop._bridge_agents) == 4096
    assert loop._bridge_agent_id("sub") == "delegated:unknown"
    assert loop._bridge_agent_id(None) == "delegated:unknown"


async def test_concurrent_bridge_calls_do_not_share_agent_identity() -> None:
    from fluid.tools.mcp.bundled_server import _socket_rpc

    reg = ToolRegistry()
    both_running = asyncio.Event()
    agents: dict[str, Any] = {}

    async def probe(args: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
        agents[ctx.extra["tool_use_id"]] = ctx.extra.get("agent_id")
        if len(agents) == 2:
            both_running.set()
        await asyncio.wait_for(both_running.wait(), timeout=2)
        assert agents[ctx.extra["tool_use_id"]] == ctx.extra.get("agent_id")
        ctx.extra.setdefault("completed", []).append(ctx.extra["tool_use_id"])
        return ToolOutcome(text="ok")

    reg.register_builtin(ToolSpec(
        name="Probe", description="test", input_schema={"type": "object"},
    ), probe)
    ctx = ToolCtx()
    server = await serve_registry_socket(
        reg, "tcp:127.0.0.1:0", ctx,
        agent_id_for_tool=lambda tid: "agent-559" if tid == "sub" else None,
    )
    address = f"tcp:127.0.0.1:{server.sockets[0].getsockname()[1]}"
    try:
        results = await asyncio.gather(*[
            _socket_rpc(address, {"op": "call", "name": "Probe", "tool_use_id": tid})
            for tid in ("sub", "main")
        ])
        assert all(not result["is_error"] for result in results)
        assert agents == {"sub": "agent-559", "main": None}
        assert sorted(ctx.extra["completed"]) == ["main", "sub"]
        assert "agent_id" not in ctx.extra and "tool_use_id" not in ctx.extra
    finally:
        server.close()
        await server.wait_closed()


async def test_reader_records_subagent_ancestry_before_bridge_dispatch() -> None:
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from fluid.engine.delegated import DelegatedLoop
    from fluid.engine.delegated_proto import StreamJsonMapper
    from fluid.messages import Message, Text
    from fluid.store.base import MemoryStore, SessionNode
    from fluid.tools.mcp.bundled_server import _socket_rpc

    store = MemoryStore()
    sid = (await store.create_session()).session_id
    user = SessionNode(node_id="user", parent_id=None,
                       message=Message(role="user", content=[Text("test")]))
    await store.append(sid, user)
    reg = ToolRegistry()
    identities: list[Any] = []

    async def probe(args: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
        identities.append(ctx.extra.get("agent_id"))
        return ToolOutcome(text="ok")

    reg.register_builtin(ToolSpec(
        name="Probe", description="test", input_schema={"type": "object"},
    ), probe)
    loop = DelegatedLoop(store=store, model="claude-test", registry=reg)
    loop._proc = SimpleNamespace(stdout=object())
    loop._emit = AsyncMock()  # type: ignore[method-assign]
    server = await serve_registry_socket(
        reg, "tcp:127.0.0.1:0", ToolCtx(), agent_id_for_tool=loop._bridge_agent_id,
    )
    address = f"tcp:127.0.0.1:{server.sockets[0].getsockname()[1]}"
    first = True

    async def next_line() -> bytes:
        nonlocal first
        if first:
            first = False
            return json.dumps({
                "type": "assistant", "parent_tool_use_id": "task-reader",
                "message": {"id": "msg", "role": "assistant", "model": "claude-test",
                            "content": [{"type": "tool_use", "id": "child-reader",
                                         "name": "Probe", "input": {}}]},
            }).encode()
        result = await _socket_rpc(address, {
            "op": "call", "name": "Probe", "arguments": {}, "tool_use_id": "child-reader",
        })
        assert not result["is_error"]
        return b""  # end the synthetic stream after exercising the bridge

    loop._next_stdout_line = next_line  # type: ignore[method-assign]
    mapper = StreamJsonMapper(session_id=sid, turn_id="turn", model="claude-test",
                              user_node_id="user")
    try:
        assert await loop._read_until_result(sid, "turn", mapper, user) == (
            "claude stream closed before result"
        )
        assert identities == ["delegated-parent:task-reader"]
    finally:
        server.close()
        await server.wait_closed()
