"""M5 — end-to-end code-mode dispatch through the real gate.

A real ToolRegistry + PermissionEngine + HostBridge behind a live
SubprocessKernel: model-authored Python imports projected stubs, calls tools,
and the calls route through hooks/permissions/dispatch. Covers the combine
case, the ``code_mode_allowed=False`` escape hatch, the ``ask`` permission
round-trip (allow + deny), and large-result truncation via ResultRef.
"""

import pytest

from fluid.codemode import HostBridge, Projection, SubprocessKernel
from fluid.permissions.engine import PermissionEngine
from fluid.tools.inproc import create_inproc_server, tool
from fluid.tools.registry import ToolRegistry, ToolSpec


def _wx_registry() -> ToolRegistry:
    @tool("get_weather", "Get the weather for a city",
          {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]})
    async def get_weather(args: dict) -> dict:
        return {"content": [{"type": "text", "text": f"{args['city']}: sunny"}]}

    @tool("get_temp", "Get the temperature for a city", risk="safe",
          input_schema={"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]})
    async def get_temp(args: dict) -> dict:
        return {"content": [{"type": "text", "text": "21C"}]}

    registry = ToolRegistry()
    registry.register_inproc(create_inproc_server("wx", [get_weather, get_temp]))
    return registry


async def _run(registry, bridge, code, *, timeout=8):
    kernel = SubprocessKernel()
    await kernel.start()
    try:
        await kernel.set_vfs(Projection(registry.list_tools()).sources())
        return await kernel.exec(code, timeout=timeout, on_host_call=bridge.on_host_call)
    finally:
        await kernel.stop()


@pytest.mark.parametrize("tool_name,args", [
    ("Read", {"file_path": "absent.txt"}),
])
async def test_missing_discovery_requires_explicit_recovery(tmp_path, tool_name, args):
    # Round 9: Grep and Glob on a missing path return "(path missing …)" as
    # a soft result and the cell continues (tests/unit/test_audit_r9_harness);
    # only Read keeps raising MissingPathError, because a Read's text is
    # consumed as content directly.
    from fluid.tools.builtins import read, grep, glob, edit
    from fluid.tools.registry import ToolCtx

    registry = ToolRegistry()
    for module in (read, grep, glob, edit):
        registry.register_builtin(module.SPEC, module.handler)
    present = tmp_path / "present.txt"
    present.write_text("needle", encoding="utf-8")
    ctx = ToolCtx(cwd=str(tmp_path))
    bridge = HostBridge(registry, PermissionEngine("never_ask"), ctx=ctx)
    call = f"{tool_name}(**{args!r})"
    prefix = f"from tools import {tool_name}, Read, Edit\n"
    result = await _run(registry, bridge, prefix + call + "\nprint('unreachable')")
    assert "MissingPathError" in result.error
    assert "unreachable" not in result.stdout
    assert not ctx.read_files

    code = prefix + (
        "try:\n"
        f"    missing = {call}\n"
        "except MissingPathError:\n"
        "    print('missing target')\n"
        "else:\n"
        "    raise AssertionError('failure became successful data')\n"
        "print(Read(file_path='present.txt'))\n"
    )
    result = await _run(registry, bridge, code)
    assert result.error is None, result.error
    assert "missing target" in result.stdout and "needle" in result.stdout
    assert str(present) in ctx.read_files
    assert not any("absent" in path for path in ctx.read_files)


@pytest.mark.parametrize("kind", ["mutation", "denial", "invalid_glob"])
async def test_missing_path_recovery_does_not_swallow_other_failures(tmp_path, kind):
    from fluid.tools.builtins import glob, read, edit
    from fluid.tools.registry import ToolCtx

    registry = ToolRegistry()
    for module in (glob, read, edit):
        registry.register_builtin(module.SPEC, module.handler)
    target = tmp_path / "unread.txt"
    target.write_text("old", encoding="utf-8")
    bridge = HostBridge(registry, PermissionEngine("never_ask"), ctx=ToolCtx(cwd=str(tmp_path)))
    call = "Edit(file_path='unread.txt', old_string='old', new_string='new')"
    if kind == "denial":
        bridge = HostBridge(registry, PermissionEngine("always_ask"), ctx=ToolCtx(cwd=str(tmp_path)))
        call = "Read(file_path='absent.txt')"
    elif kind == "invalid_glob":
        call = "Glob(pattern='[', path='.')"
    result = await _run(registry, bridge,
        "from tools import Read, Edit, Glob\n"
        "try:\n"
        f"    {call}\n"
        "except MissingPathError:\n"
        "    print('recovered')\n"
        "print('unreachable')\n")
    assert result.error is not None
    assert "recovered" not in result.stdout and "unreachable" not in result.stdout
    assert target.read_text(encoding="utf-8") == "old"


async def test_read_directory_is_not_a_recoverable_missing_path(tmp_path):
    from fluid.tools.builtins import read
    from fluid.tools.registry import ToolCtx

    registry = ToolRegistry()
    registry.register_builtin(read.SPEC, read.handler)
    bridge = HostBridge(registry, PermissionEngine("never_ask"), ctx=ToolCtx(cwd=str(tmp_path)))
    reply = await bridge.on_host_call("Read", {"file_path": "."})
    assert reply.error is not None and reply.error_code is None


async def test_discovery_cancellation_propagates():
    import asyncio
    from fluid.tools.builtins import read

    async def cancelled(_input, _ctx):
        raise asyncio.CancelledError

    registry = ToolRegistry()
    registry.register_builtin(read.SPEC, cancelled)
    bridge = HostBridge(registry, PermissionEngine("never_ask"))
    with pytest.raises(asyncio.CancelledError):
        await bridge.on_host_call("Read", {"file_path": "absent"})


async def test_two_tools_combined_in_one_cell() -> None:
    registry = _wx_registry()
    bridge = HostBridge(registry, PermissionEngine("never_ask"))
    code = (
        "from tools.get_weather import get_weather\n"
        "from tools.get_temp import get_temp\n"
        "w = get_weather(city='Paris')\n"
        "t = get_temp(city='Paris')\n"
        "w + ' | ' + t\n"
    )
    result = await _run(registry, bridge, code)
    assert result.error is None
    assert result.value_repr == "'Paris: sunny | 21C'"
    assert result.host_calls == 2  # both tools actually ran


async def test_code_mode_disallowed_tool_is_refused_in_kernel() -> None:
    registry = _wx_registry()

    async def _wipe(input: dict, ctx) -> str:
        return "WIPED"  # must never run

    registry.register_builtin(
        ToolSpec(
            name="mcp__danger__wipe", description="Destroy everything",
            input_schema={"type": "object", "properties": {}},
            risk="destructive", source="inproc", code_mode_allowed=False,
        ),
        _wipe,
    )
    bridge = HostBridge(registry, PermissionEngine("never_ask"))
    code = (
        "from tools.wipe import wipe\n"
        "wipe()\n"
    )
    result = await _run(registry, bridge, code)
    # The stub raised HostCallDenied inside the kernel; WIPED never happened.
    assert result.error is not None
    assert "HostCallDenied" in result.error
    assert "JSON tool interface" in result.error
    assert "WIPED" not in (result.value_repr or "")


async def test_ask_permission_allow_then_proceed() -> None:
    registry = _wx_registry()
    seen: list = []

    async def approve(spec, input) -> bool:
        seen.append((spec.name, dict(input)))
        return True

    # always_ask forces the round-trip even for a safe tool.
    bridge = HostBridge(registry, PermissionEngine("always_ask"), on_permission=approve)
    code = (
        "from tools.get_weather import get_weather\n"
        "get_weather(city='Rome')\n"
    )
    result = await _run(registry, bridge, code)
    assert result.error is None
    assert result.value_repr == "'Rome: sunny'"
    assert seen == [("mcp__wx__get_weather", {"city": "Rome"})]


async def test_ask_permission_deny_raises_in_kernel() -> None:
    registry = _wx_registry()

    async def refuse(spec, input) -> bool:
        return False

    bridge = HostBridge(registry, PermissionEngine("always_ask"), on_permission=refuse)
    code = (
        "from tools.get_weather import get_weather\n"
        "get_weather(city='Rome')\n"
    )
    result = await _run(registry, bridge, code)
    assert result.error is not None
    assert "HostCallDenied" in result.error
    assert "permission denied" in result.error


async def test_large_result_stays_behind_a_result_ref() -> None:
    @tool("dump", "Dump a large blob", {"type": "object", "properties": {}})
    async def dump(args: dict) -> dict:
        return {"content": [{"type": "text", "text": "A" * 20000}]}

    registry = ToolRegistry()
    registry.register_inproc(create_inproc_server("bulk", [dump]))
    bridge = HostBridge(registry, PermissionEngine("never_ask"))
    code = (
        "from tools.dump import dump\n"
        "blob = dump()\n"
        "print(type(blob).__name__)\n"
        "print(len(blob))\n"
        "blob[:5]\n"   # sliceable in-kernel, no host round-trip
    )
    result = await _run(registry, bridge, code)
    assert result.error is None
    assert "ResultRef" in result.stdout        # not a plain str
    assert "20000" in result.stdout            # full payload retained in-kernel
    assert result.value_repr == "'AAAAA'"      # sliced locally


async def test_hook_deny_short_circuits_host_call() -> None:
    from fluid.hooks.bus import PRE_TOOL_USE, HookBus, HookDecision

    registry = _wx_registry()
    hooks = HookBus()

    async def block(event: str, payload: dict):
        return HookDecision(action="deny", note="nope")

    hooks.on(PRE_TOOL_USE, block)
    bridge = HostBridge(registry, PermissionEngine("never_ask"), hooks=hooks)
    code = (
        "from tools.get_weather import get_weather\n"
        "get_weather(city='Rome')\n"
    )
    result = await _run(registry, bridge, code)
    assert result.error is not None
    assert "blocked by hook: nope" in result.error



# ─── Batch 6: the hook contract on in-kernel calls ───────────────────────────


async def test_host_bridge_honours_hook_stop_and_permission_decisions(tmp_path) -> None:
    """The code-mode bridge follows the JSON path's hook contract: a
    PreToolUse stop refuses the call and tells the engine to end the turn,
    ``permissionDecision: allow`` skips the permission gate, and PostToolUse
    feedback rides back on the result."""
    from fluid.codemode.bridge import HostBridge
    from fluid.hooks.bus import POST_TOOL_USE, PRE_TOOL_USE, HookBus, HookDecision
    from fluid.permissions.engine import PermissionEngine
    from fluid.tools.builtins import register_builtins
    from fluid.tools.registry import ToolCtx, ToolRegistry

    reg = ToolRegistry()
    register_builtins(reg)
    ctx = ToolCtx(cwd=str(tmp_path))
    (tmp_path / "hello.txt").write_text("hi")
    args = {"file_path": str(tmp_path / "hello.txt")}
    stops: list[str] = []

    async def decide(decision):
        return decision

    bus = HookBus()
    bus.on(PRE_TOOL_USE, lambda _e, _p: decide(HookDecision(stop=True, note="halt")))
    bridge = HostBridge(reg, PermissionEngine("never_ask"), hooks=bus, ctx=ctx, on_stop=stops.append)
    reply = await bridge.on_host_call("Read", args)
    assert reply.denied and reply.message == "blocked by hook: halt"
    assert stops == ["halt"]

    # An always_ask policy with no permission handler denies; the hook's
    # allow lifts that.
    bus = HookBus()
    bus.on(PRE_TOOL_USE, lambda _e, _p: decide(HookDecision(permission_decision="allow")))
    bridge = HostBridge(reg, PermissionEngine("always_ask"), hooks=bus, ctx=ctx)
    reply = await bridge.on_host_call("Read", args)
    assert not reply.denied and reply.result.endswith("hi")
    bare = HostBridge(reg, PermissionEngine("always_ask"), ctx=ctx)
    assert (await bare.on_host_call("Read", args)).denied

    bus = HookBus()
    bus.on(POST_TOOL_USE, lambda _e, _p: decide(
        HookDecision(action="deny", note="lint failed", additional_context="run ruff", stop=False),
    ))
    bridge = HostBridge(reg, PermissionEngine("never_ask"), hooks=bus, ctx=ctx, on_stop=stops.append)
    reply = await bridge.on_host_call("Read", args)
    assert reply.result.startswith("1	hi")
    assert "PostToolUse hook feedback:\nlint failed" in reply.result
    assert "PostToolUse hook context:\nrun ruff" in reply.result
    bus = HookBus()
    bus.on(POST_TOOL_USE, lambda _e, _p: decide(HookDecision(stop=True, note="done")))
    bridge = HostBridge(reg, PermissionEngine("never_ask"), hooks=bus, ctx=ctx, on_stop=stops.append)
    await bridge.on_host_call("Read", args)
    assert stops == ["halt", "done"]
