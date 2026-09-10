"""The app policy sees registered names, not transport-qualified CLI aliases."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from claude_agent_sdk import PermissionResultDeny
from fluid.engine.delegated import DelegatedLoop
from fluid.tools.registry import ToolRegistry, ToolSpec
from app.core.provider.harness_adapter import HarnessAdapter
from app.core.provider.types import PermissionRequestEvent


@pytest.mark.asyncio
@pytest.mark.parametrize("wire, expected", [
    ("mcp__fluid_bundled__mcp__builtins__Remember", "mcp__builtins__Remember"),
    ("mcp__builtins__Remember", "mcp__builtins__Remember"),
    ("mcp__untrusted__mcp__builtins__Remember", "mcp__untrusted__mcp__builtins__Remember"),
    ("mcp__fluid_bundled__Missing", "mcp__fluid_bundled__Missing"),
])
async def test_permission_alias_resolves_only_registered_tools_and_keeps_denial(wire, expected):
    registry = ToolRegistry()
    async def handler(*args):
        raise AssertionError("A denied tool must not execute")
    registry.register_builtin(ToolSpec(name="mcp__builtins__Remember", description="test", input_schema={}), handler)
    engine = SimpleNamespace(registry=registry)
    engine._registry_spec = lambda name: DelegatedLoop._registry_spec(engine, name)
    adapter = HarnessAdapter()
    adapter._engine = engine
    adapter._can_use_tool = AsyncMock(return_value=PermissionResultDeny(message="profile denial"))
    adapter.respond_to_permission = AsyncMock()
    await adapter._bridge_permission(PermissionRequestEvent(tool_use_id="call", name=wire, input={}))
    assert adapter._can_use_tool.await_args.args[0] == expected
    adapter.respond_to_permission.assert_awaited_once_with("call", False, message="profile denial", updated_input=None)


def test_warm_cli_cache_does_not_hide_allowed_bundled_memory(monkeypatch):
    from app.core.provider.harness_adapter import _delegated_disallowed
    from app.core.provider.types import SessionOptions
    names = ["mcp__fluid_bundled__mcp__builtins__Remember",
             "mcp__fluid_bundled__Bash", "mcp__other__mcp__builtins__Remember"]
    monkeypatch.setattr("app.core.cli_tool_surface.cached_cli_tools", lambda: tuple(names))
    options = SessionOptions(model="sonnet", tool_allowlist=["mcp__builtins__Remember"], disallowed_tools=[])
    banned = _delegated_disallowed(options)
    assert names[0] not in banned
    assert names[1] in banned and names[2] in banned
    options.disallowed_tools = [names[0]]
    assert names[0] in _delegated_disallowed(options)
