"""Bundled CLI calls retain caller identity at the owner approval boundary (#559)."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fluid.engine.delegated import DelegatedLoop
from fluid.store.base import MemoryStore
from fluid.tools.mcp.bundled_server import _socket_rpc
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolRegistry, ToolSpec

from app.core.agent_types import PermissionResultAllow, PermissionResultDeny
from app.core.provider.harness_adapter import HarnessAdapter
from app.core.provider.types import PermissionRequestEvent, SessionOptions


@pytest.mark.parametrize("source", ["hook", "stream", "unknown", "missing-meta"])
async def test_subagent_cannot_spend_main_pass(tmp_path: Path, source: str) -> None:
    """An armed exact-call pass in the approver is reserved for the main caller.

    The callback models #535's pass contract; this tests the producing bridge
    and adapter, not a second implementation of conversational approval.
    """
    options = SessionOptions(
        model="claude-test", permission_mode="bypassPermissions",
        extra={"assistant_scope": {"session_id": "home", "home_session_id": "home",
                                   "roots": [str(tmp_path)], "cwd": str(tmp_path)}},
    )
    adapter = HarnessAdapter()
    adapter._options = options
    adapter._is_delegated = True
    pass_armed = True
    identities: list[str | None] = []

    async def approve(name: str, args: dict[str, Any], context: Any) -> Any:
        nonlocal pass_armed
        identities.append(context.agent_id)
        if context.agent_id is None and pass_armed:
            pass_armed = False
            return PermissionResultAllow()
        return PermissionResultDeny(message="Ask the owner for this caller")

    adapter._can_use_tool = approve
    registry = ToolRegistry()
    ran: list[dict[str, Any]] = []

    async def handoff(args: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
        ran.append(args)
        return ToolOutcome(text="ran")

    name = "mcp__builtins__HandOffSession"
    registry.register_builtin(ToolSpec(
        name=name, description="test", input_schema={"type": "object"},
    ), handoff)
    adapter._guard_assistant_dispatch(registry, options)
    ctx = ToolCtx()
    loop = DelegatedLoop(store=MemoryStore(), model="claude-test", registry=registry, ctx=ctx)
    # Keep all bridge artifacts in pytest's disposable directory.
    loop._tmpdir = str(tmp_path)
    await loop._start_bundled_server()
    assert loop._socket_path is not None
    args = {"worker": "test-worker"}
    try:
        if source == "hook":
            await loop._cli_pre_tool_use({
                "tool_name": f"mcp__fluid_bundled__{name}", "tool_use_id": "sub-call",
                "agent_id": "cli-agent-559",
            }, "sub-call")
        elif source == "stream":
            loop._track_bridge_agents({
                "type": "assistant", "parent_tool_use_id": "task-559",
                "message": {"content": [{"type": "tool_use", "id": "sub-call"}]},
            })
        # An agent-less hook cannot turn unknown or stream-derived ancestry
        # into a main-thread identity.
        await loop._cli_pre_tool_use({
            "tool_name": f"mcp__fluid_bundled__{name}", "tool_use_id": "sub-call",
        }, "sub-call")
        denied = await _socket_rpc(loop._socket_path, {
            "op": "call", "name": name, "arguments": args,
            **({"tool_use_id": "sub-call"} if source != "missing-meta" else {}),
            # An argument cannot impersonate the calling thread.
            "agent_id": None,
        }, loop._bridge_token)
        assert denied["is_error"] and "Ask the owner" in denied["text"]
        assert pass_armed and not ran
        expected = {"hook": "cli-agent-559", "stream": "delegated-parent:task-559"}
        assert identities == [expected.get(source, "delegated:unknown")]
        loop._track_bridge_agents({
            "type": "assistant", "parent_tool_use_id": None,
            "message": {"content": [{"type": "tool_use", "id": "main-call"}]},
        })
        await loop._cli_pre_tool_use({
            "tool_name": f"mcp__fluid_bundled__{name}", "tool_use_id": "main-call",
        }, "main-call")
        allowed = await _socket_rpc(loop._socket_path, {
            "op": "call", "name": name, "arguments": args, "tool_use_id": "main-call",
        }, loop._bridge_token)
        assert not allowed["is_error"] and ran == [args] and not pass_armed
        assert identities[-1] is None
        assert "agent_id" not in ctx.extra
    finally:
        assert loop._socket_server is not None
        loop._socket_server.close()
        await loop._socket_server.wait_closed()


async def test_native_owner_prompt_keeps_main_identity() -> None:
    """Native agents have their own adapter/session; no CLI ancestry is injected."""
    adapter = HarnessAdapter()
    adapter._options = SessionOptions(
        model="test", permission_mode="bypassPermissions",
        extra={"assistant_scope": {"session_id": "home", "home_session_id": "home",
                                   "roots": [], "cwd": "."}},
    )
    adapter._can_use_tool = AsyncMock(return_value=PermissionResultAllow())
    adapter.respond_to_permission = AsyncMock()
    await adapter._bridge_permission(PermissionRequestEvent(
        tool_use_id="native-559", name="HandOffSession", input={"worker": "test-worker"},
        risk="write",
    ))
    context = adapter._can_use_tool.await_args.args[2]
    assert context.agent_id is None and context.tool_use_id == "native-559"
    assert context.assistant_owner is True
    assert context.delegated is False
