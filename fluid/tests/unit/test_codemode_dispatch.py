"""M5 — end-to-end code-mode dispatch through the real gate.

A real ToolRegistry + PermissionEngine + HostBridge behind a live
SubprocessKernel: model-authored Python imports projected stubs, calls tools,
and the calls route through hooks/permissions/dispatch. Covers the combine
case, the ``code_mode_allowed=False`` escape hatch, the ``ask`` permission
round-trip (allow + deny), and large-result truncation via ResultRef.
"""

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
