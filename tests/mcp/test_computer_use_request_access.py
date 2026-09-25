"""request_access grants only on a server-side allow decision.

The permission callback (or the headless auto-approver) records one approval
per allowed call; the tool spends it. Bypass mode is itself the decision. A
call that reaches the tool with neither is refused and grants nothing. No real
display or input device is touched.
"""

import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.mcp import computer_use_sdk as sdk
from app.mcp.computer_use import abort, access, activity, scaling, safety

SID = "cu-approval"


def payload(result):
    return json.loads(result["content"][0]["text"])


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(abort, "_HALTED", {})
    monkeypatch.setattr(abort, "_GENERATIONS", {})
    monkeypatch.setattr(activity, "_sessions", {})
    monkeypatch.setattr(activity, "_last_stop", None)
    access.revoke(SID)
    monkeypatch.setitem(sys.modules, "pynput.mouse", SimpleNamespace(
        Controller=MagicMock, Button=SimpleNamespace(left="left", right="right", middle="middle")))
    monkeypatch.setitem(sys.modules, "pynput.keyboard", SimpleNamespace(
        Controller=MagicMock, Key=SimpleNamespace()))
    monkeypatch.setattr(sdk, "computer_use_available", lambda: (True, "ok"))
    monkeypatch.setattr("app.mcp.computer_use.overlay.ensure_started", lambda: None)
    monkeypatch.setattr(safety, "ensure_started", lambda: True)
    monkeypatch.setattr(scaling, "_ensure_dpi_awareness", lambda: None)

    class Capture:  # display enumeration only; nothing is captured
        monitors = [{"left": 0, "top": 0, "width": 200, "height": 100}] * 2

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass
    monkeypatch.setitem(sys.modules, "mss", SimpleNamespace(mss=Capture))
    monkeypatch.setattr("app.mcp.sdk_compat.patch_sdk_server",
                        lambda cfg, tools: {t.name: t.handler for t in tools})

    def build(permission_mode):
        return sdk.create_computer_use_server(
            enabled=True, permission_mode=permission_mode, session_id=SID)

    yield build
    access.revoke(SID)


async def test_approved_call_grants_and_the_approval_is_one_shot(server):
    tools = server("default")
    access.approve_request(SID)
    result = payload(await tools["request_access"]({"apps": ["Notepad"], "reason": "r"}))
    assert result["granted_apps"] == ["Notepad"]
    again = await tools["request_access"]({"apps": ["Calculator"], "reason": "r"})
    assert again["isError"]
    assert access.info(SID)["apps"] == ["Notepad"]


async def test_direct_call_without_a_decision_is_refused(server):
    tools = server("default")
    result = await tools["request_access"](
        {"apps": ["Notepad"], "reason": "r", "clipboardWrite": True})
    assert result["isError"]
    assert "not approved" in payload(result)["error"]
    assert not access.is_granted(SID)


async def test_approval_is_keyed_to_the_servers_session(server):
    tools = server("default")
    access.approve_request("another-session")
    try:
        assert (await tools["request_access"]({"apps": ["Notepad"], "reason": "r"}))["isError"]
    finally:
        access.revoke("another-session")


async def test_bypass_mode_needs_no_record(server):
    result = payload(await server("bypassPermissions")["request_access"](
        {"apps": ["Notepad"], "reason": "r"}))
    assert result["granted_apps"] == ["Notepad"]


async def test_live_permission_mode_wins_over_the_build_time_mode(server, monkeypatch):
    """The mode can change between turns without rebuilding the server."""
    from app.core import session_pool
    from app.core.provider.harness_adapter import HarnessAdapter
    from app.core.provider.types import SessionOptions

    adapter = HarnessAdapter()
    adapter._options = SessionOptions(model="m", permission_mode="bypassPermissions")
    state = SimpleNamespace(adapter=adapter)
    pool = SimpleNamespace(peek=lambda sid: state if sid == SID else None)
    monkeypatch.setattr(session_pool, "get_session_pool", lambda: pool)
    assert payload(await server("default")["request_access"](
        {"apps": ["A"], "reason": "r"}))["success"]
    adapter._options.permission_mode = "default"
    assert (await server("bypassPermissions")["request_access"](
        {"apps": ["B"], "reason": "r"}))["isError"]


def test_unspent_approval_expires(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(access.time, "monotonic", lambda: now[0])
    access.approve_request(SID)
    now[0] += access._APPROVAL_TTL_S + 1
    assert not access.consume_request_approval(SID)


# ─── where the runtime decides: every allow path records exactly once ───────
#
# Fluid reports each call a gate lets through (HookBus.tool_allowed) before it
# dispatches; the adapter records the approval from that one signal, whatever
# allowed the call. The permission callback and the headless auto-approver
# record nothing themselves, so no allow is counted twice.

TOOL = "mcp__computer-use__request_access"


def _adapter(permission_mode="default"):
    from app.core.provider.harness_adapter import HarnessAdapter
    from app.core.provider.types import SessionOptions

    adapter = HarnessAdapter()
    adapter._options = SessionOptions(model="m", permission_mode=permission_mode)
    adapter._options.extra["app_session_id"] = SID
    return adapter


async def test_headless_auto_approval_records_only_at_the_gate():
    """No interactive approver + a mode that approves on its own: the answer
    itself records nothing; the gate's report (under the bundled alias the
    delegated CLI uses) records one."""
    from unittest.mock import AsyncMock

    from app.core.provider.types import PermissionRequestEvent

    adapter = _adapter("acceptEdits")
    adapter._engine = SimpleNamespace(_registry_spec=lambda name: SimpleNamespace(name=TOOL))
    adapter.respond_to_permission = AsyncMock()
    try:
        await adapter._answer_without_approver(PermissionRequestEvent(
            tool_use_id="t", input={}, name="mcp__fluid_bundled__" + TOOL))
        adapter.respond_to_permission.assert_awaited_once()
        assert not access.consume_request_approval(SID)
        await adapter._hooks.tool_allowed(
            {"name": "mcp__fluid_bundled__" + TOOL, "input": {}, "tool_use_id": None})
        assert access.consume_request_approval(SID)
        assert not access.consume_request_approval(SID)
    finally:
        access.revoke(SID)


async def test_other_allowed_tools_record_nothing():
    adapter = _adapter()
    try:
        await adapter._hooks.tool_allowed(
            {"name": "mcp__computer-use__left_click", "input": {}, "tool_use_id": "t"})
        assert not access.consume_request_approval(SID)
    finally:
        access.revoke(SID)


# End to end: the app's computer-use server on fluid's registry through the
# bridge, a native loop emitting into the adapter's HookBus.


@pytest.fixture
def bridged(server, monkeypatch):
    """The computer-use tools registered on a fluid registry the way the
    harness registers them (fluid_tool_bridge risk map: request_access is
    ``write``, so the on_write policy asks)."""
    from fluid.tools.registry import ToolRegistry

    from app.core.provider.fluid_tool_bridge import (
        COMPUTER_USE_RISK_MAP,
        register_sdk_tools,
    )

    monkeypatch.setattr("app.mcp.sdk_compat.patch_sdk_server", lambda cfg, tools: tools)
    registry = ToolRegistry()
    register_sdk_tools(registry, "computer-use", server("default"),
                       risk_map=COMPUTER_USE_RISK_MAP, default_risk="write")
    return registry


class _Scripted:
    """A provider that calls request_access once, then ends the turn."""
    name = "anthropic"

    def __init__(self):
        from fluid.providers.base import ProviderCaps
        self.caps = ProviderCaps()
        self.calls = 0

    async def stream(self, request):
        from fluid.providers.base import StreamChunk
        self.calls += 1
        yield StreamChunk("message_start", {"usage": {"input_tokens": 1}})
        if self.calls == 1:
            yield StreamChunk("block_start", {"index": 0, "block_type": "tool_use",
                                              "tool_name": TOOL, "tool_use_id": "tu1"})
            yield StreamChunk("tool_args_delta", {"index": 0, "partial_json": json.dumps(
                {"apps": ["Notepad"], "reason": "r"})})
            stop = "tool_use"
        else:
            yield StreamChunk("block_start", {"index": 0, "block_type": "text"})
            yield StreamChunk("text_delta", {"index": 0, "text": "done"})
            stop = "end_turn"
        yield StreamChunk("block_stop", {"index": 0})
        yield StreamChunk("usage", {"usage": {"output_tokens": 1}, "stop_reason": stop})
        yield StreamChunk("message_stop", {})


async def _turn(registry, adapter, permissions, *, answer=None):
    """Run one native turn calling request_access; ``answer`` handles the
    prompt (none expected when it is None). Returns (prompted, ToolResult)."""
    import asyncio

    from fluid.engine.native import NativeLoop
    from fluid.events import PermissionRequest, ToolResult, UserTurn
    from fluid.store.base import MemoryStore

    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = NativeLoop(provider=_Scripted(), store=store, model="m", session_id=sid,
                      registry=registry, hooks=adapter._hooks, permissions=permissions)
    adapter._engine = loop
    prompted = []

    async def watch():
        async for event in loop.bus.subscribe(sid):
            if isinstance(event, PermissionRequest):
                prompted.append(event)
                await answer(event)
                return

    watcher = asyncio.create_task(watch())
    await loop.submit(UserTurn(content="go"))
    await asyncio.wait_for(loop.wait_turn(), timeout=10)
    watcher.cancel()
    result = next(e for e in loop.bus.log(sid) if isinstance(e, ToolResult))
    return bool(prompted), result


async def test_hook_allowed_request_access_executes(bridged):
    """A PreToolUse hook's allow skips the prompt; the call still grants."""
    from fluid.hooks.bus import PRE_TOOL_USE, HookDecision
    from fluid.permissions.engine import PermissionEngine

    adapter = _adapter()

    async def allow(_event, _payload):
        return HookDecision(permission_decision="allow")
    adapter._hooks.on(PRE_TOOL_USE, allow)
    prompted, result = await _turn(bridged, adapter, PermissionEngine("on_write"))
    assert not prompted
    assert not result.is_error, result.output
    assert access.info(SID)["apps"] == ["Notepad"]
    assert not access.consume_request_approval(SID)  # exactly one, spent


async def test_remembered_allow_executes(bridged):
    from fluid.permissions.engine import PermissionEngine

    permissions = PermissionEngine("on_write")
    permissions.allow_always(TOOL)
    prompted, result = await _turn(bridged, _adapter(), permissions)
    assert not prompted
    assert not result.is_error, result.output
    assert access.info(SID)["apps"] == ["Notepad"]
    assert not access.consume_request_approval(SID)


@pytest.mark.parametrize("allowed", [True, False])
async def test_prompt_answer_through_the_adapter_bridge(bridged, allowed):
    """The prompt goes through the adapter's can_use_tool bridge: an allow
    grants on exactly one record (the callback records none of its own), a
    deny is refused and leaves nothing behind."""
    from fluid.permissions.engine import PermissionEngine

    from app.core.agent_types import PermissionResultAllow, PermissionResultDeny
    from app.core.provider.types import PermissionRequestEvent

    adapter = _adapter()
    adapter._can_use_tool = AsyncMock(
        return_value=PermissionResultAllow() if allowed else PermissionResultDeny(message="no"))

    async def answer(event):
        adapter._perm_requests[event.tool_use_id] = event.request_id
        await adapter._bridge_permission(PermissionRequestEvent(
            tool_use_id=event.tool_use_id, name=event.name, input=event.input))

    prompted, result = await _turn(bridged, adapter, PermissionEngine("on_write"), answer=answer)
    assert prompted
    adapter._can_use_tool.assert_awaited_once()
    assert result.is_error is (not allowed)
    assert access.is_granted(SID) is allowed
    assert not access.consume_request_approval(SID)


async def test_ungated_dispatch_is_still_refused(bridged):
    """A dispatch that skips every gate carries no allow and grants nothing."""
    from fluid.tools.registry import ToolCtx

    _adapter()  # an adapter exists, but no gate reports the call
    outcome = await bridged.dispatch(TOOL, {"apps": ["Notepad"], "reason": "r"}, ToolCtx())
    assert outcome.is_error and "not approved" in outcome.text
    assert not access.is_granted(SID)
