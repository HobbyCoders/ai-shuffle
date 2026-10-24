"""Bundled CLI identity reaches the real one-shot owner approval gate (#559)."""
from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fluid.engine.delegated import DelegatedLoop
from fluid.store.base import MemoryStore
from fluid.tools.mcp.bundled_server import _socket_rpc
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolRegistry, ToolSpec

from app.core import assistant_confirm as confirm
from app.core.permission_handler import PermissionHandler
from app.core.provider.harness_adapter import HarnessAdapter
from app.core.provider.types import PermissionRequestEvent, SessionOptions
from app.db import interactions
from tests.core.interaction_harness import (  # noqa: F401 — autouse transport fixtures
    no_connector_delivery,
    no_gateway_listener,
    no_push,
    reset_backfill_memo_on_setup,
    reset_interaction_wait_state,
)


class _RealOwnerGate:
    """Forward adapter context into #535's real permission/pass implementation."""

    def __init__(self) -> None:
        self.handler = PermissionHandler()
        self.contexts: list[Any] = []
        self.prompts: list[dict[str, Any]] = []

    def arm(self, name: str, args: dict[str, Any]) -> None:
        assert confirm.arm_pass("home", {confirm.PASS_KEY: {
            "tool_name": name, "tool_input": args, "interaction_id": "approved-559",
        }}, "turn-559")

    async def deny_prompt(self, event: dict[str, Any]) -> None:
        """A synthetic owner's no answers a real durable permission request."""
        if event.get("type") != "permission_request":
            return
        self.prompts.append(event)
        row = next(row for row in interactions.get_pending_interactions("home")
                   if row["payload"]["request_id"] == event["request_id"])
        assert interactions.answer_interaction(row["id"], {
            "decision": "deny", "message": "Ask the owner for this caller",
        })

    async def __call__(self, name: str, args: dict[str, Any], context: Any) -> Any:
        self.contexts.append(context)
        assert context.assistant_owner is True
        return await self.handler.request_permission(
            request_id=f"request-559-{len(self.contexts)}", session_id="home",
            profile_id="default", tool_name=name, tool_input=args,
            broadcast_func=self.deny_prompt, tool_use_id=context.tool_use_id,
            agent_id=context.agent_id, skip_saved_rules=True,
            assistant_confirm=confirm.marker(name, args),
        )


@pytest.fixture
def real_owner_gate(
    mock_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[_RealOwnerGate]:
    # Only unrelated Away/notification delivery is isolated. Pass storage,
    # the main-thread guard, durable requests and answers all use real code.
    monkeypatch.setattr("app.core.away_mode.defer_if_away", AsyncMock(return_value=None))
    confirm.reset()
    yield _RealOwnerGate()
    confirm.reset()


@pytest.mark.parametrize("source", ["hook", "stream", "unknown", "missing-meta"])
async def test_subagent_cannot_spend_main_pass(
    tmp_path: Path, source: str, real_owner_gate: _RealOwnerGate,
) -> None:
    """Subagent/unknown calls ask; the main call then spends the real pass once."""
    options = SessionOptions(
        model="claude-test", permission_mode="bypassPermissions",
        extra={"assistant_scope": {"session_id": "home", "home_session_id": "home",
                                   "roots": [str(tmp_path)], "cwd": str(tmp_path)}},
    )
    adapter = HarnessAdapter()
    adapter._options = options
    adapter._is_delegated = True
    adapter._can_use_tool = real_owner_gate
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
    loop._tmpdir = str(tmp_path)
    await loop._start_bundled_server()
    assert loop._socket_path is not None
    args = {"worker": "test-worker"}
    # The approver's canonical tool name is what #535 stores on the pass.
    real_owner_gate.arm(adapter._permission_tool_name(name), args)
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
        await loop._cli_pre_tool_use({
            "tool_name": f"mcp__fluid_bundled__{name}", "tool_use_id": "sub-call",
        }, "sub-call")
        denied = await asyncio.wait_for(_socket_rpc(loop._socket_path, {
            "op": "call", "name": name, "arguments": args,
            **({"tool_use_id": "sub-call"} if source != "missing-meta" else {}),
            "agent_id": None,  # socket input cannot impersonate the main thread
        }, loop._bridge_token), timeout=5)
        assert denied["is_error"] and "Ask the owner" in denied["text"]
        assert not ran and len(real_owner_gate.prompts) == 1
        expected = {"hook": "cli-agent-559", "stream": "delegated-parent:task-559"}
        assert real_owner_gate.contexts[0].agent_id == expected.get(source, "delegated:unknown")
        assert real_owner_gate.prompts[0]["agent_id"] == real_owner_gate.contexts[0].agent_id
        loop._track_bridge_agents({
            "type": "assistant", "parent_tool_use_id": None,
            "message": {"content": [{"type": "tool_use", "id": "main-call"}]},
        })
        await loop._cli_pre_tool_use({
            "tool_name": f"mcp__fluid_bundled__{name}", "tool_use_id": "main-call",
        }, "main-call")
        request = {"op": "call", "name": name, "arguments": args, "tool_use_id": "main-call"}
        allowed = await asyncio.wait_for(
            _socket_rpc(loop._socket_path, request, loop._bridge_token), timeout=5,
        )
        assert not allowed["is_error"] and ran == [args]
        assert len(real_owner_gate.prompts) == 1  # no second ask: the real pass survived
        assert real_owner_gate.contexts[-1].agent_id is None
        assert "agent_id" not in ctx.extra
        again = await asyncio.wait_for(
            _socket_rpc(loop._socket_path, request, loop._bridge_token), timeout=5,
        )
        assert again["is_error"] and len(real_owner_gate.prompts) == 2 and ran == [args]
        assert not confirm.take_approved_once("home", adapter._permission_tool_name(name), args)
    finally:
        assert loop._socket_server is not None
        loop._socket_server.close()
        await loop._socket_server.wait_closed()


async def test_native_owner_prompt_keeps_main_identity(real_owner_gate: _RealOwnerGate) -> None:
    """Native owner approval also spends the real pass once, with no CLI identity."""
    adapter = HarnessAdapter()
    adapter._options = SessionOptions(
        model="test", permission_mode="bypassPermissions",
        extra={"assistant_scope": {"session_id": "home", "home_session_id": "home",
                                   "roots": [], "cwd": "."}},
    )
    adapter._can_use_tool = real_owner_gate
    adapter.respond_to_permission = AsyncMock()
    args = {"worker": "test-worker"}
    real_owner_gate.arm("HandOffSession", args)
    event = PermissionRequestEvent(
        tool_use_id="native-559", name="HandOffSession", input=args, risk="write",
    )
    await asyncio.wait_for(adapter._bridge_permission(event), timeout=5)
    context = real_owner_gate.contexts[-1]
    assert context.agent_id is None and context.tool_use_id == "native-559"
    assert context.assistant_owner is True and context.delegated is False
    assert adapter.respond_to_permission.await_args.args[1] is True
    assert real_owner_gate.prompts == []
    await asyncio.wait_for(adapter._bridge_permission(event), timeout=5)
    assert adapter.respond_to_permission.await_args.args[1] is False
    assert len(real_owner_gate.prompts) == 1
    assert not confirm.take_approved_once("home", "HandOffSession", args)
