"""omni_tool_bridge — port AI-Shuffle's SDK ``@tool`` modules onto omni's registry.

AI-Shuffle's ~10 in-process tool modules under ``app/mcp/`` all follow the
Claude Agent SDK pattern:

    from claude_agent_sdk import tool, create_sdk_mcp_server

    @tool("Name", "description", {json-schema})
    async def handler(args: dict) -> dict:            # -> {"content": [{"type":"text",...}]}
        ...

    server = create_sdk_mcp_server(name="builtins", version="1.0.0", tools=[handler, ...])

omni's ``omni.tools.inproc`` mirrors that surface deliberately, and its
``ToolRegistry._normalize`` already accepts the SDK envelope
(``{"content":[{"type":"text","text":...}]}``) as a handler return. So porting a
module is *mechanical*: hand this bridge the same list of ``@tool``-decorated
objects the module already passes to ``create_sdk_mcp_server`` and it produces a
omni ``InprocServer`` that ``ToolRegistry.register_inproc`` accepts.

This module ships the generic converters used to port every module, plus a
worked example (``bridge_builtins_tools``) proving the pattern for
``app/mcp/builtin_tools.py``. The remaining modules are the same one-liner.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, List, Mapping, Optional

from omni.tools.inproc import InprocServer, InprocTool, create_inproc_server
from omni.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


def sdk_tool_to_inproc(sdk_tool: Any, *, risk: str = "safe") -> InprocTool:
    """Convert one Claude-Agent-SDK ``@tool`` object into a omni ``InprocTool``.

    The SDK ``@tool`` decorator yields an object with ``.name``, ``.description``,
    ``.input_schema`` and ``.handler`` (an ``async def(args: dict)``) — the exact
    shape omni's ``InprocTool`` needs. Attribute access is defensive so this also
    works with any duck-typed equivalent.
    """
    name = getattr(sdk_tool, "name", None)
    handler = getattr(sdk_tool, "handler", None)
    if name is None or handler is None:
        raise TypeError(
            f"not an SDK @tool object (missing name/handler): {sdk_tool!r}"
        )
    return InprocTool(
        name=name,
        description=getattr(sdk_tool, "description", "") or "",
        input_schema=getattr(sdk_tool, "input_schema", None) or {"type": "object"},
        handler=handler,
        risk=risk,
    )


def sdk_tools_to_inproc_server(
    server_name: str,
    sdk_tools: Iterable[Any],
    *,
    version: str = "1.0.0",
    risk_map: Optional[Mapping[str, str]] = None,
) -> InprocServer:
    """Bundle a list of SDK ``@tool`` objects into a named omni ``InprocServer``.

    ``risk_map`` optionally assigns per-tool risk (``"safe"|"write"|"destructive"``)
    by tool name; anything unlisted defaults to ``"safe"``. This is where a
    module declares that, e.g., ``CronDelete`` is ``destructive`` so omni's
    PermissionEngine gates it.
    """
    risk_map = risk_map or {}
    tools: List[InprocTool] = [
        sdk_tool_to_inproc(t, risk=risk_map.get(getattr(t, "name", ""), "safe"))
        for t in sdk_tools
    ]
    return create_inproc_server(server_name, tools, version=version)


def register_sdk_tools(
    registry: ToolRegistry,
    server_name: str,
    sdk_tools: Iterable[Any],
    *,
    version: str = "1.0.0",
    risk_map: Optional[Mapping[str, str]] = None,
    default_risk: str = "safe",
) -> InprocServer:
    """Convert + register in one call. Each tool surfaces as
    ``mcp__<server_name>__<tool>`` (omni's naming, matching Claude Code).

    ``default_risk`` applies to tools absent from ``risk_map`` — used by
    servers whose tool set is dynamic (connector gateway tools on ``builtins``,
    the ProviderAgent tool) so unknown tools default to gated, not open.
    """
    risk_map = risk_map or {}
    tools: List[InprocTool] = [
        sdk_tool_to_inproc(t, risk=risk_map.get(getattr(t, "name", ""), default_risk))
        for t in sdk_tools
    ]
    server = create_inproc_server(server_name, tools, version=version)
    registry.register_inproc(server)
    return server


# ─── Per-server risk classification ───────────────────────────────────────────
#
# The app assembles five in-proc SDK servers (query_options / app_config_sdk
# aggregate the individual app/mcp modules into these):
#
#   builtins        builtin_tools.py  (+ dynamic connector gateway tools)
#   app-config      app_config_sdk.py (aggregates all config_* modules)
#   ai-tools        ai_tools_sdk.py
#   computer-use    computer_use_sdk.py
#   provider-agent  provider_agent_sdk.py
#
# (phone_tools.py exports raw definitions but is not wired into any SDK server,
# so there is nothing to bridge for it.)
#
# Every server passes through sdk_compat.patch_sdk_server, which records the
# raw @tool objects (see provider/sdk_tool_records.py);
# ``register_recorded_sdk_servers`` below re-registers them on an omni
# ToolRegistry with the risk classifications here, so omni's PermissionEngine
# gates writes/deletes the way the SDK path's permission callback did.

BUILTINS_RISK_MAP: Mapping[str, str] = {
    "ScheduleWakeup": "safe",
    "NotifyDevices": "safe",
    "Monitor": "safe",
    "TaskOutput": "safe",
    "TaskGet": "safe",
    "TaskList": "safe",
    "TaskStop": "write",
    "CronList": "safe",
    "CronCreate": "write",
    "CronDelete": "destructive",
    "EnterWorktree": "write",
    "ExitWorktree": "write",
    "GetAppErrors": "safe",
    "ToolSearch": "safe",
    "RemoteTrigger": "write",
}

# Dynamic gateway tools (per-connector, e.g. sending messages through a linked
# service) also land on the builtins server — unknown names default to "write".
BUILTINS_DEFAULT_RISK = "write"


def _classify_app_config(name: str) -> str:
    if name.startswith(("delete", "remove")):
        return "destructive"
    if name.startswith(("list", "get")):
        return "safe"
    # create*/update*/toggle*/add*/test*/trigger* mutate app configuration.
    return "write"


APP_CONFIG_RISK_MAP: Mapping[str, str] = {
    name: _classify_app_config(name)
    for name in (
        "getConfigCapabilities", "listHooks", "getHook", "createHook",
        "updateHook", "deleteHook", "toggleHook", "testHook",
        "listAutomations", "getAutomation", "getAutomationRuns",
        "createAutomation", "updateAutomation", "deleteAutomation",
        "toggleAutomation", "triggerAutomation",
        "listMcpServers", "addMcpServer", "removeMcpServer", "toggleMcpServer",
        "listProfiles", "getProfile", "createProfile", "updateProfile",
        "deleteProfile",
        "listConnectors", "getConnectorStatus", "toggleConnector",
        "listSubagents", "getSubagent", "createSubagent", "updateSubagent",
        "deleteSubagent",
    )
}

AI_TOOLS_RISK_MAP: Mapping[str, str] = {
    # Generation tools spend provider credits and write artifacts.
    "generateImage": "write", "editImage": "write", "generateWithReference": "write",
    "generateVideo": "write", "imageToVideo": "write", "extendVideo": "write",
    "generateAudio": "write", "audioToAudio": "write",
    "imageTo3D": "write", "textTo3D": "write", "runFalModel": "write",
    "searchModels": "safe",
}

COMPUTER_USE_RISK_MAP: Mapping[str, str] = {
    # Pure observation:
    "screenshot": "safe", "cursor_position": "safe", "list_displays": "safe",
    "list_granted_applications": "safe", "read_clipboard": "safe",
    "zoom": "safe", "wait": "safe",
    # Everything that drives the desktop mutates it:
    "left_click": "write", "right_click": "write", "middle_click": "write",
    "double_click": "write", "triple_click": "write", "left_click_drag": "write",
    "left_mouse_down": "write", "left_mouse_up": "write", "mouse_move": "write",
    "key": "write", "hold_key": "write", "type": "write", "scroll": "write",
    "switch_display": "write", "open_application": "write",
    "write_clipboard": "write", "request_access": "write",
    "computer_batch": "write",
}

PROVIDER_AGENT_RISK_MAP: Mapping[str, str] = {
    "ProviderAgent": "write",  # spawns a worker agent that can act
}

SERVER_RISK_MAPS: Mapping[str, Mapping[str, str]] = {
    "builtins": BUILTINS_RISK_MAP,
    "app-config": APP_CONFIG_RISK_MAP,
    "ai-tools": AI_TOOLS_RISK_MAP,
    "computer-use": COMPUTER_USE_RISK_MAP,
    "provider-agent": PROVIDER_AGENT_RISK_MAP,
}

SERVER_DEFAULT_RISK: Mapping[str, str] = {
    "builtins": BUILTINS_DEFAULT_RISK,
    # Unknown/new tools on the other servers default to gated as well —
    # "safe" must be earned by an explicit entry above.
    "app-config": "write",
    "ai-tools": "write",
    "computer-use": "write",
    "provider-agent": "write",
}


def register_recorded_sdk_servers(
    registry: ToolRegistry, mcp_servers: Optional[Mapping[str, Any]]
) -> List[str]:
    """Register every recorded in-proc SDK server from an ``mcp_servers``
    mapping onto ``registry``. Returns the server names registered.

    This is the whole port of the app's in-proc tool modules: the tools were
    recorded (per server INSTANCE, so per-session closures stay correct) when
    ``patch_sdk_server`` assembled each server; here they are re-registered on
    omni's ToolRegistry with the risk maps above. Servers with no recording
    (e.g. built before sdk_compat imported, or non-dict configs) are skipped —
    the external stdio/sse/http entries are handled by ``connect_mcp`` instead.
    """
    from app.core.provider.sdk_tool_records import recorded_sdk_server_tools

    registered: List[str] = []
    for name, cfg in (mcp_servers or {}).items():
        if not (isinstance(cfg, dict) and cfg.get("type") == "sdk"):
            continue
        tools = recorded_sdk_server_tools(cfg)
        if not tools:
            logger.warning(
                "[omni_tool_bridge] no recorded tools for sdk server %r — skipped",
                name,
            )
            continue
        register_sdk_tools(
            registry,
            name,
            tools,
            risk_map=SERVER_RISK_MAPS.get(name),
            default_risk=SERVER_DEFAULT_RISK.get(name, "write"),
        )
        registered.append(name)
    return registered
