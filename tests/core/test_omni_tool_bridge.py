"""End-to-end guard for the in-proc SDK tool port onto omni's ToolRegistry.

Chain under test:
  patch_sdk_server(cfg, tools)                # records tools per instance
    → sdk_tool_records.recorded_sdk_server_tools
    → omni_tool_bridge.register_recorded_sdk_servers
    → HarnessAdapter._build_registry          # flag-on connect path

The recording is keyed by server INSTANCE (tools are closures over per-session
state), so two sessions' same-named servers must not collide.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.core.provider.omni_tool_bridge import (
    SERVER_RISK_MAPS,
    register_recorded_sdk_servers,
)
from app.core.provider.sdk_tool_records import (
    record_sdk_server_tools,
    recorded_sdk_server_tools,
)
from app.mcp.sdk_compat import patch_sdk_server

from omni.tools.registry import ToolRegistry


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
    registry = await adapter._build_registry(options)
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
