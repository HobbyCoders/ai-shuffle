"""HookBus.tool_allowed — one signal for every allowed tool call.

Each gate that dispatches a registry tool (the native loop, the code-mode
bridge, the delegated leg's bundled bridge) reports the call to
``on_tool_allowed`` observers once it is allowed and before it runs, whatever
allowed it: a PreToolUse ``allow``, a rule or mode, a remembered "allow
always", or a prompt answer. A denied or unanswered call is never reported.
"""

import asyncio

import pytest

from fluid.codemode.bridge import HostBridge
from fluid.engine.native import NativeLoop
from fluid.events import PermissionRequest, RespondPermission, ToolResult, UserTurn
from fluid.hooks.bus import PRE_TOOL_USE, HookBus, HookDecision
from fluid.permissions.engine import PermissionEngine
from fluid.permissions.rules import Rule
from fluid.store.base import MemoryStore
from fluid.tools.inproc import create_inproc_server, tool
from fluid.tools.mcp.bundled_server import _answer
from fluid.tools.registry import ToolCtx, ToolRegistry
from tests.unit.test_tool_loop import ScriptedProvider, _first_event, _text, _tool

TOOL = "mcp__m__touch"


def _recording_bus(*, hook: HookDecision | None = None):
    """A bus whose observer records each report, and a touch tool that
    records how many reports had landed when it ran."""
    bus = HookBus()
    seen: list[dict] = []
    ran: list[int] = []

    async def observe(payload: dict) -> None:
        seen.append(payload)

    bus.on_tool_allowed(observe)
    if hook is not None:
        async def decide(_event: str, _payload: dict) -> HookDecision:
            return hook
        bus.on(PRE_TOOL_USE, decide)

    @tool("touch", "Touch a marker", {"type": "object", "properties": {"n": {"type": "integer"}}},
          risk="write")
    async def touch(args: dict) -> dict:
        ran.append(len(seen))
        return {"content": [{"type": "text", "text": "touched"}]}

    registry = ToolRegistry()
    registry.register_inproc(create_inproc_server("m", [touch]))
    return bus, registry, seen, ran


async def _native_turn(bus, registry, permissions, *, answer: str | None = None):
    provider = ScriptedProvider([_tool("t1", TOOL, '{"n": 1}'), _text("done")])
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = NativeLoop(
        provider=provider, store=store, model="claude-sonnet-5", session_id=sid,
        registry=registry, hooks=bus, permissions=permissions,
    )
    holder, got, watcher = await _first_event(loop, sid, PermissionRequest)
    await loop.submit(UserTurn(content="go"))
    if answer is not None:
        await asyncio.wait_for(got.wait(), timeout=5)
        await loop.submit(RespondPermission(request_id=holder["e"].request_id, decision=answer))
    await asyncio.wait_for(loop.wait_turn(), timeout=10)
    watcher.cancel()
    events = loop.bus.log(sid)
    prompted = any(isinstance(e, PermissionRequest) for e in events)
    return sid, prompted, next(e for e in events if isinstance(e, ToolResult))


# ─── the bus ────────────────────────────────────────────────────────────────


async def test_observers_all_run_and_a_raising_one_is_skipped() -> None:
    bus = HookBus()
    seen: list[str] = []

    async def broken(payload: dict) -> None:
        raise RuntimeError("boom")

    async def fine(payload: dict) -> None:
        payload["name"] = "mutated"  # a copy: the next observer is unaffected
        seen.append("fine")

    async def last(payload: dict) -> None:
        seen.append(payload["name"])

    for callback in (broken, fine, last):
        bus.on_tool_allowed(callback)
    await bus.tool_allowed({"name": "Read", "input": {}})
    assert seen == ["fine", "Read"]


# ─── native loop ────────────────────────────────────────────────────────────


async def test_native_hook_allow_is_reported_before_the_tool_runs() -> None:
    """A PreToolUse allow skips the prompt; the call is still reported."""
    bus, registry, seen, ran = _recording_bus(hook=HookDecision(permission_decision="allow"))
    sid, prompted, result = await _native_turn(bus, registry, PermissionEngine("always_ask"))
    assert not prompted and not result.is_error
    assert seen == [{"session_id": sid, "name": TOOL, "input": {"n": 1}, "tool_use_id": "t1"}]
    assert ran == [1]  # the report landed before the tool ran


async def test_native_remembered_allow_is_reported() -> None:
    bus, registry, seen, ran = _recording_bus()
    permissions = PermissionEngine("on_write")
    permissions.allow_always(TOOL)
    _sid, prompted, result = await _native_turn(bus, registry, permissions)
    assert not prompted and not result.is_error
    assert len(seen) == 1 and ran == [1]


@pytest.mark.parametrize("answer,reported", [
    ("allow", 1), ("allow_always", 1), ("deny", 0),
])
async def test_native_prompt_answer_decides_the_report(answer, reported) -> None:
    bus, registry, seen, ran = _recording_bus()
    _sid, prompted, result = await _native_turn(
        bus, registry, PermissionEngine("on_write"), answer=answer,
    )
    assert prompted
    assert len(seen) == reported
    assert ran == ([1] if reported else [])
    assert result.is_error is (not reported)


async def test_native_rule_and_hook_denials_are_not_reported() -> None:
    bus, registry, seen, ran = _recording_bus()
    await _native_turn(
        bus, registry, PermissionEngine("never_ask", rules=[Rule(tool=TOOL, decision="deny")]),
    )
    bus2, registry2, seen2, ran2 = _recording_bus(
        hook=HookDecision(action="deny", note="no"),
    )
    await _native_turn(bus2, registry2, PermissionEngine("never_ask"))
    assert seen == seen2 == [] and ran == ran2 == []


# ─── code-mode bridge ───────────────────────────────────────────────────────


async def test_code_mode_bridge_reports_allowed_calls_only() -> None:
    bus, registry, seen, ran = _recording_bus(hook=HookDecision(permission_decision="allow"))
    ctx = ToolCtx(session_id="ses_cm")
    bridge = HostBridge(registry, PermissionEngine("always_ask"), hooks=bus, ctx=ctx)
    reply = await bridge.on_host_call(TOOL, {"n": 2}, tool_use_id="tu")
    assert not reply.denied
    assert seen == [{"session_id": "ses_cm", "name": TOOL, "input": {"n": 2}, "tool_use_id": "tu"}]
    assert ran == [1]

    # No hook allow and nobody to ask: denied, and nothing is reported.
    bus, registry, seen, ran = _recording_bus()
    bridge = HostBridge(registry, PermissionEngine("always_ask"), hooks=bus, ctx=ctx)
    assert (await bridge.on_host_call(TOOL, {"n": 2})).denied
    assert seen == [] and ran == []


# ─── the delegated leg's bundled bridge ─────────────────────────────────────


async def test_bundled_bridge_reports_calls_the_cli_sends_through() -> None:
    bus, registry, seen, ran = _recording_bus(hook=HookDecision(permission_decision="allow"))
    ctx = ToolCtx(session_id="ses_dl")
    call = {"op": "call", "name": TOOL, "arguments": {"n": 3}}
    answered = await _answer(
        registry, ctx, call, permissions=PermissionEngine("always_ask"), hooks=bus,
    )
    assert answered["is_error"] is False
    assert seen == [{"session_id": "ses_dl", "name": TOOL, "input": {"n": 3}, "tool_use_id": None}]
    assert ran == [1]

    # Without the hook's allow the bridge cannot prompt: denied, unreported.
    bus, registry, seen, ran = _recording_bus()
    denied = await _answer(
        registry, ctx, call, permissions=PermissionEngine("always_ask"), hooks=bus,
    )
    assert denied["is_error"] is True
    assert seen == [] and ran == []
