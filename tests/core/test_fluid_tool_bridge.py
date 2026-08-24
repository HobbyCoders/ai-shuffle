"""End-to-end guard for the in-proc SDK tool port onto fluid's ToolRegistry.

Chain under test:
  patch_sdk_server(cfg, tools)                # records tools per instance
    → sdk_tool_records.recorded_sdk_server_tools
    → fluid_tool_bridge.register_recorded_sdk_servers
    → HarnessAdapter._build_registry          # flag-on connect path

The recording is keyed by server INSTANCE (tools are closures over per-session
state), so two sessions' same-named servers must not collide.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.core.provider.fluid_tool_bridge import (
    DEFERRED_SDK_SERVERS,
    SERVER_RISK_MAPS,
    register_recorded_sdk_servers,
)
from app.core.provider.sdk_tool_records import (
    record_sdk_server_tools,
    recorded_sdk_server_tools,
)
from app.mcp.sdk_compat import patch_sdk_server

from fluid.tools.registry import ToolRegistry


class _Instance:
    """Weakref-able stand-in for an mcp.Server (call_tool patch is optional)."""

    def call_tool(self):
        return lambda fn: fn


def _tool(name: str, marker: str = ""):
    async def handler(args: dict) -> dict:
        return {"content": [{"type": "text", "text": f"{marker}{name}:{args}"}]}

    return SimpleNamespace(
        name=name,
        description=f"{name} tool",
        input_schema={"type": "object"},
        handler=handler,
    )


def test_patch_sdk_server_records_tools():
    tools = [_tool("Monitor"), _tool("CronDelete")]
    cfg = {"type": "sdk", "name": "builtins", "instance": _Instance()}
    patch_sdk_server(cfg, tools)
    assert recorded_sdk_server_tools(cfg) == tools


def test_recordings_are_per_instance_not_per_name():
    cfg_a = {"type": "sdk", "name": "builtins", "instance": _Instance()}
    cfg_b = {"type": "sdk", "name": "builtins", "instance": _Instance()}
    tools_a, tools_b = [_tool("Monitor", "a-")], [_tool("Monitor", "b-")]
    record_sdk_server_tools(cfg_a, tools_a)
    record_sdk_server_tools(cfg_b, tools_b)
    assert recorded_sdk_server_tools(cfg_a) == tools_a
    assert recorded_sdk_server_tools(cfg_b) == tools_b


def test_register_recorded_sdk_servers_applies_risk_maps():
    tools = [_tool("Monitor"), _tool("CronDelete"), _tool("gateway_slack_send")]
    cfg = {"type": "sdk", "name": "builtins", "instance": _Instance()}
    record_sdk_server_tools(cfg, tools)

    registry = ToolRegistry()
    registered = register_recorded_sdk_servers(
        registry,
        {
            "builtins": cfg,
            # External server: not bridged here (connect_mcp handles those).
            "github": {"type": "stdio", "command": "gh-mcp"},
        },
    )
    assert registered == ["builtins"]

    monitor = registry.get("mcp__builtins__Monitor")
    cron_delete = registry.get("mcp__builtins__CronDelete")
    gateway = registry.get("mcp__builtins__gateway_slack_send")
    assert monitor is not None and cron_delete is not None and gateway is not None
    assert monitor.risk == "safe"
    assert cron_delete.risk == "destructive"  # from BUILTINS_RISK_MAP
    assert gateway.risk == "write"  # dynamic gateway tool → server default


def test_deferred_servers_land_in_the_deferred_tier_and_stay_loadable():
    """Native-leg tiering: app-config/ai-tools schemas stay out of the prompt
    prefix (deferred, ToolSearch-loadable) while builtins remain eager."""
    config_cfg = {"type": "sdk", "name": "app-config", "instance": _Instance()}
    builtins_cfg = {"type": "sdk", "name": "builtins", "instance": _Instance()}
    record_sdk_server_tools(config_cfg, [_tool("listHooks")])
    record_sdk_server_tools(builtins_cfg, [_tool("Memory")])

    registry = ToolRegistry()
    register_recorded_sdk_servers(
        registry,
        {"app-config": config_cfg, "builtins": builtins_cfg},
        defer_servers=DEFERRED_SDK_SERVERS,
    )

    deferred = {spec.name for spec in registry.deferred_tools()}
    loaded = {spec.name for spec in registry.list_tools(loaded_only=True)}
    assert "mcp__app-config__listHooks" in deferred
    assert "mcp__app-config__listHooks" not in loaded
    assert "mcp__builtins__Memory" in loaded

    # ToolSearch's load path promotes a deferred tool into the loaded tier.
    registry.load_schema("mcp__app-config__listHooks")
    loaded_after = {spec.name for spec in registry.list_tools(loaded_only=True)}
    assert "mcp__app-config__listHooks" in loaded_after


def test_skip_tools_excludes_named_tools_from_one_server():
    """``skip_tools`` is how the adapter drops a bridged copy. Memory is the
    live case: a native session with a project memory dir must not get the
    ``mcp__builtins__Memory`` tool (the dir replaces it)."""
    builtins_cfg = {"type": "sdk", "name": "builtins", "instance": _Instance()}
    other_cfg = {"type": "sdk", "name": "ai-tools", "instance": _Instance()}
    record_sdk_server_tools(builtins_cfg, [_tool("Memory"), _tool("Ledger")])
    record_sdk_server_tools(other_cfg, [_tool("Memory")])

    registry = ToolRegistry()
    register_recorded_sdk_servers(
        registry,
        {"builtins": builtins_cfg, "ai-tools": other_cfg},
        skip_tools={"builtins": {"Memory"}},
    )

    names = {spec.name for spec in registry.list_tools(loaded_only=False)}
    assert "mcp__builtins__Memory" not in names
    # Same-named tool on another server is untouched — the map is per server.
    assert "mcp__ai-tools__Memory" in names
    assert "mcp__builtins__Ledger" in names


def test_unrecorded_sdk_server_is_skipped():
    registry = ToolRegistry()
    cfg = {"type": "sdk", "name": "mystery", "instance": _Instance()}
    assert register_recorded_sdk_servers(registry, {"mystery": cfg}) == []
    assert registry.list_tools(loaded_only=False) == []


@pytest.mark.asyncio
async def test_build_registry_bridges_recorded_servers_and_dispatches():
    from app.core.provider.harness_adapter import HarnessAdapter
    from app.core.provider.types import SessionOptions

    tools = [_tool("Monitor")]
    cfg = {"type": "sdk", "name": "builtins", "instance": _Instance()}
    record_sdk_server_tools(cfg, tools)

    adapter = HarnessAdapter()
    options = SessionOptions(model="fake", mcp_servers={"builtins": cfg})
    registry = await adapter._build_registry(options, native=False)
    assert registry is not None

    outcome = await registry.dispatch("mcp__builtins__Monitor", {"x": 1})
    text = getattr(outcome, "output", None) or str(outcome)
    assert "Monitor" in str(text)


def test_every_static_risk_map_tool_name_is_current():
    """The risk maps must track the modules' actual @tool names — a rename in
    app/mcp/* without a map update silently regresses that tool to the gated
    default, so surface drift here (informational, cheap to keep exact)."""
    import re

    module_files = {
        "builtins": ["app/mcp/builtin_tools.py"],
        "app-config": [
            "app/mcp/app_config_sdk.py", "app/mcp/config_automations.py",
            "app/mcp/config_connectors.py", "app/mcp/config_hooks.py",
            "app/mcp/config_mcp_servers.py", "app/mcp/config_profiles.py",
            "app/mcp/config_subagents.py",
        ],
        "ai-tools": ["app/mcp/ai_tools_sdk.py"],
        "computer-use": ["app/mcp/computer_use_sdk.py"],
    }
    for server, files in module_files.items():
        actual: set = set()
        for path in files:
            src = open(path).read()
            actual |= set(re.findall(r'@tool\(\s*\n?\s*["\']([\w\-]+)["\']', src))
            # computer_use_sdk builds its tools from the _TOOL_NAMES list.
            m = re.search(r"_TOOL_NAMES\s*=\s*\[(.*?)\]", src, re.S)
            if m:
                actual |= set(re.findall(r'["\']([\w\-]+)["\']', m.group(1)))
        mapped = set(SERVER_RISK_MAPS[server])
        stale = mapped - actual
        assert not stale, f"{server}: risk map entries with no matching @tool: {sorted(stale)}"
