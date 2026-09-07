"""Planning is an ordinary request, with no mode or planning tool surface."""

import pytest

from fluid.cli.prompt import BUILTIN_TOOL_NAMES, DEFAULT_BASE_PROMPT
from fluid.engine.delegated import DelegatedLoop
from fluid.engine.native import NativeLoop
from fluid.events import SetPermissionMode
from fluid.permissions.engine import PermissionEngine
from fluid.store.base import MemoryStore
from fluid.tools.builtins import register_builtins
from fluid.tools.registry import ToolRegistry
from tests.unit.test_empty_completion_retry import ScriptedProvider


def test_planning_tools_are_not_registered_or_advertised():
    registry = ToolRegistry()
    register_builtins(registry)
    for name in ("Plan", "StopPlanning"):
        assert registry.get(name) is None
        assert name not in BUILTIN_TOOL_NAMES
    assert "StopPlanning" not in DEFAULT_BASE_PROMPT
    assert "enter plan mode" not in DEFAULT_BASE_PROMPT
    assert "StopPlanning" not in registry.get("AskUser").description


async def test_old_native_mode_request_uses_normal_permission_policy():
    registry = ToolRegistry()
    register_builtins(registry)
    loop = NativeLoop(
        provider=ScriptedProvider([]), store=MemoryStore(), model="m",
        registry=registry, permissions=PermissionEngine("on_write"),
    )
    await loop.submit(SetPermissionMode(mode="plan"))
    assert loop.permissions.approval_policy == "on_write"
    assert loop._tool_available("Write")
    assert "plan_mode_control" not in loop.ctx.extra
    assert not hasattr(loop, "plan_mode")


@pytest.mark.parametrize("permission_mode", [None, "default", "plan"])
def test_delegated_planning_tools_are_disabled(permission_mode):
    loop = DelegatedLoop(
        store=MemoryStore(), model="m", permission_mode=permission_mode,
        disallowed_tools=["Write"],
    )
    argv = loop._build_argv()
    denied = argv[argv.index("--disallowed-tools") + 1].split(",")
    assert {"EnterPlanMode", "ExitPlanMode", "Write"} <= set(denied)
    assert loop.permission_mode == "default"
    assert argv[argv.index("--permission-mode") + 1] == "default"
