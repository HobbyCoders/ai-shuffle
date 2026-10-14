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


def test_real_chat_access_tools_have_serializable_provider_schemas():
    import json
    from app.mcp.chat_access_sdk import create_chat_access_server
    from fluid.providers.base import ProviderRequest
    from fluid.messages import Message, Text
    from fluid.providers.openai_responses.serialize import build_request_body

    registry = ToolRegistry()
    register_recorded_sdk_servers(registry, {"chat-access": create_chat_access_server("schema-test")})
    request = registry.get("mcp__chat-access__RequestChatAccess")
    assert request.input_schema == {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["machine", "folder", "service"]},
            "purpose": {"type": "string"},
            "saved_id": {
                "type": "string",
                "description": "Saved connection ID from ChatResources, of the requested kind. Preselects the private approval card; never grants access. Omit connection hints and plugin/service when using this.",
            },
            "suggested_host": {
                "type": "string", "maxLength": 64,
                "description": "Machine only: known unicast IPv4 or IPv6 literal, editable for review. Hostnames are not supported; pass the machine IP. No URLs, credentials or interface scope; omit when unknown.",
            },
            "suggested_port": {
                "type": "integer", "minimum": 1, "maximum": 65535,
                "description": "Machine only: known SSH port, editable for review. Omit when unknown.",
            },
            "suggested_username": {
                "type": "string", "maxLength": 128,
                "description": "Machine only: known POSIX username, editable for review. Never passwords, keys or credentials; omit when unknown.",
            },
            "suggested_path": {
                "type": "string",
                "description": "Folder only: known absolute path on the owning worker, prefilled for user review. Omit when unknown.",
            },
            "service_provider": {
                "type": "string", "enum": ["cloudflare", "custom"],
                "description": "Service only: known provider preset, or custom. Omit for other kinds.",
            },
            "suggested_url": {
                "type": "string",
                "description": "Service only: known public HTTPS API base, without credentials, query or fragment. Never private connection details; omit when unknown.",
            },
            "plugin": {
                "type": "string",
                "description": "Service only: id (name@source) of an installed plugin that declares a sign-in for the service. Use with service; omit the hints.",
            },
            "service": {
                "type": "string",
                "description": "Service only: the service name that plugin declares. Use with plugin.",
            },
        },
        "required": ["kind", "purpose"],
    }
    wire = build_request_body(ProviderRequest(
        model="test", messages=[Message(role="user", content=[Text("hello")])],
        tools=[{"name": tool.name, "description": tool.description, "input_schema": tool.input_schema}
               for tool in registry.list_tools()],
    ))
    encoded = json.dumps(wire)
    assert "RequestChatAccess" in encoded and "ChatWriteFile" in encoded
    for hint in ("suggested_path", "suggested_host", "suggested_port", "suggested_username"):
        assert hint in encoded
    # The broker's one-shot card is the approval; the generic gate would ask twice.
    for name in ("ChatRunCommand", "ChatWriteFile"):
        assert registry.get(f"mcp__chat-access__{name}").risk == "safe"


def test_shorthand_schema_matches_pinned_sdk_semantics():
    from typing import TypedDict, NotRequired
    from app.core.provider.fluid_tool_bridge import sdk_tool_to_inproc

    Arguments = TypedDict("Arguments", {"path": str, "limit": NotRequired[int]})

    sdk_tool = _tool("Example")
    sdk_tool.input_schema = {"names": list[str], "flag": bool, "count": int, "rate": float}
    schema = sdk_tool_to_inproc(sdk_tool).input_schema
    assert schema["properties"] == {
        "names": {"type": "array", "items": {"type": "string"}},
        "flag": {"type": "boolean"}, "count": {"type": "integer"}, "rate": {"type": "number"},
    }
    assert schema["required"] == ["names", "flag", "count", "rate"]
    sdk_tool.input_schema = Arguments
    assert sdk_tool_to_inproc(sdk_tool).input_schema == {
        "type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
        "required": ["path"],
    }
    sdk_tool.input_schema = {"type": "object", "additionalProperties": False}
    assert sdk_tool_to_inproc(sdk_tool).input_schema is sdk_tool.input_schema


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
    assert monitor.risk == "write"
    assert cron_delete.risk == "destructive"  # from BUILTINS_RISK_MAP
    assert gateway.risk == "write"  # dynamic gateway tool → server default


def test_android_device_listing_is_safe_and_action_stays_gated():
    """A read-only device listing must not ask for approval in Ask mode; a
    phone action keeps the write gate."""
    cfg = {"type": "sdk", "name": "builtins", "instance": _Instance()}
    record_sdk_server_tools(cfg, [_tool("AndroidDevices"), _tool("AndroidAction")])

    registry = ToolRegistry()
    register_recorded_sdk_servers(registry, {"builtins": cfg})
    assert registry.get("mcp__builtins__AndroidDevices").risk == "safe"
    assert registry.get("mcp__builtins__AndroidAction").risk == "write"


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
    """``skip_tools`` is how the adapter drops a bridged copy — the live case
    is NATIVE_SHADOWED_BUILTINS (Task/Monitor/ToolSearch plus the memory
    four), whose fluid bare-named builtins replace the bridged
    mcp__builtins__ copies. The tool names here are arbitrary; the map is per
    server."""
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


def test_every_static_risk_map_tool_name_is_current(monkeypatch):
    """The risk maps must track the modules' actual @tool names — a rename in
    app/mcp/* without a map update silently regresses that tool to the gated
    default, so surface drift here (informational, cheap to keep exact)."""
    import re

    module_files = {
        "builtins": ["app/mcp/builtin_tools.py", "app/mcp/plugin_tools.py"],
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
        if server == "builtins":
            from app.core.config import settings
            from app.mcp.builtin_tools import _build_memory_bridge_tools
            from fluid.tools.inproc import tool

            # Building these tool objects does not execute their handlers or
            # open a store. Conversation-history registration is covered by
            # the literal decorators above and has its own database tests.
            monkeypatch.setattr(type(settings), "managed_database_available", lambda self: False)
            actual.update(entry.name for entry in _build_memory_bridge_tools(
                tool, session_id="risk-map-probe", cwd=None,
            ))
            from fluid.tools.builtins.session_orchestration import SPECS
            actual.update(spec.name for spec, _ in SPECS)
            # The agent-messaging bridge re-exposes fluid's own specs.
            from fluid.tools.builtins import list_agents, send_message
            actual.update({list_agents.SPEC.name, send_message.SPEC.name})
        mapped = set(SERVER_RISK_MAPS[server])
        stale = mapped - actual
        assert not stale, f"{server}: risk map entries with no matching registered tool: {sorted(stale)}"
