"""fluid_tool_bridge: the app's in-process ``@tool`` servers on fluid's ToolRegistry.

The app's tool modules (``app.mcp.inproc``: ``@tool`` handlers assembled by
``create_tool_server``) share their surface with fluid's ``fluid.tools.inproc``,
and ``ToolRegistry._normalize`` accepts the SDK result envelope
(``{"content": [{"type": "text", "text": ...}]}``), so conversion is mechanical.

``register_recorded_sdk_servers`` (called by the harness adapter) re-registers
every recorded in-proc server with per-tool risk classes. The lower-level
converters are ``sdk_tool_to_inproc`` and ``register_sdk_tools``.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, List, Mapping, Optional

from fluid.tools.inproc import InprocServer, InprocTool, create_inproc_server
from fluid.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


def _input_schema(schema: Any) -> dict:
    from fluid.tools.inproc import input_schema
    return input_schema(schema)


def sdk_tool_to_inproc(sdk_tool: Any, *, risk: str = "safe") -> InprocTool:
    """Convert one Claude-Agent-SDK ``@tool`` object into a fluid ``InprocTool``.

    The SDK ``@tool`` decorator yields an object with ``.name``, ``.description``,
    ``.input_schema`` and ``.handler`` (an ``async def(args: dict)``) — the exact
    shape fluid's ``InprocTool`` needs. Attribute access is defensive so this also
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
        input_schema=_input_schema(getattr(sdk_tool, "input_schema", None)),
        handler=handler,
        risk=risk,
    )


def register_sdk_tools(
    registry: ToolRegistry,
    server_name: str,
    sdk_tools: Iterable[Any],
    *,
    version: str = "1.0.0",
    risk_map: Optional[Mapping[str, str]] = None,
    default_risk: str = "safe",
    defer: bool = False,
) -> InprocServer:
    """Convert + register in one call. Each tool surfaces as
    ``mcp__<server_name>__<tool>`` (fluid's naming, matching Claude Code).

    ``default_risk`` applies to tools absent from ``risk_map`` — used by
    servers whose tool set is dynamic (connector gateway tools on ``builtins``,
    the ProviderAgent tool) so unknown tools default to gated, not open.

    ``defer=True`` registers the server's tools in fluid's deferred tier
    (announced by name + one-line description, schema loaded on demand via
    ToolSearch) instead of pushing every schema into the prompt prefix.
    """
    risk_map = risk_map or {}
    tools: List[InprocTool] = [
        sdk_tool_to_inproc(t, risk=risk_map.get(getattr(t, "name", ""), default_risk))
        for t in sdk_tools
    ]
    server = create_inproc_server(server_name, tools, version=version)
    try:
        registry.register_inproc(server, defer=defer)
    except TypeError:
        # Older vendored fluid without the defer parameter: eager fallback.
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
# Every server passes through sdk_compat.patch_sdk_server, which records the
# raw @tool objects (see provider/sdk_tool_records.py);
# ``register_recorded_sdk_servers`` below re-registers them on a fluid
# ToolRegistry with the risk classifications here, so fluid's PermissionEngine
# gates writes/deletes the way the SDK path's permission callback did.

BUILTINS_RISK_MAP: Mapping[str, str] = {
    # The delegated leg's app-memory bridge — same risk fluid assigns the
    # bare-named originals. Explicit memory writes require the write policy.
    "Remember": "write",
    "Forget": "write",
    "Recall": "safe",
    "SessionSearch": "safe",
    "SessionView": "safe",
    "ListSessions": "safe",
    "MessageSession": "write",
    "StartSession": "write",
    "ScheduleWakeup": "safe",
    "NotifyDevices": "safe",
    # Monitor runs a host shell command; fluid rates its own Monitor "write".
    "Monitor": "write",
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
    "GetAppLogs": "safe",
    "GetUsage": "safe",
    "ToolSearch": "safe",
    "RemoteTrigger": "write",
    # Dispatching an agent spawns a session that can act — gated like a write.
    "Task": "write",
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
    "zoom": "safe", "wait": "safe", "release_control": "safe",
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
    # The broker enforces grants and per-action confirmation outside Bypass.
    # Gating writes/commands here too would ask the user twice.
    "chat-access": {
        "RequestChatAccess": "safe", "ChatResources": "safe", "ChatListFiles": "safe", "ChatReadFile": "safe",
        "ChatWriteFile": "safe", "ChatRunCommand": "safe",
    },
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

# Servers whose schemas stay OUT of the prompt prefix on the native leg
# (deferred tier: announced by name, loaded on demand via ToolSearch). The
# app-config CRUD surface alone is ~35 schemas; pushing all of them into every
# coding session buried the core tools. ``builtins`` stays eager — its tools
# (the memory bridge, Monitor, task tools, scheduling) are the session's
# operating surface. Claude Code applies the same tiering to its MCP servers.
DEFERRED_SDK_SERVERS: frozenset[str] = frozenset({
    "app-config",
    "ai-tools",
    "computer-use",
    "provider-agent",
})

# Builtins whose ``mcp__builtins__`` copies are skipped on EVERY native leg:
# fluid registers its own bare-named versions there, so bridging
# the copy would show the model two of each. One set, two readers — the harness
# adapter's ``skip_tools`` (what actually drops them) and the tool catalog's
# availability marking (what tells the picker why the row is greyed). The
# memory four (Remember/Recall/SessionSearch/SessionView) are the delegated
# leg's app-memory bridge; native legs run fluid's originals over the same
# stores via ctx.extra.
NATIVE_SHADOWED_BUILTINS: frozenset[str] = frozenset({
    "Task", "Monitor", "ToolSearch", "GetUsage",
    "Remember", "Recall", "Forget", "SessionSearch", "SessionView",
    "ListSessions", "MessageSession", "StartSession",
})


def register_recorded_sdk_servers(
    registry: ToolRegistry,
    mcp_servers: Optional[Mapping[str, Any]],
    *,
    skip_tools: Optional[Mapping[str, Any]] = None,
    defer_servers: Optional[frozenset[str]] = None,
) -> List[str]:
    """Register every recorded in-proc SDK server from an ``mcp_servers``
    mapping onto ``registry``. Returns the server names registered.

    This is the whole port of the app's in-proc tool modules: the tools were
    recorded (per server INSTANCE, so per-session closures stay correct) when
    ``patch_sdk_server`` assembled each server; here they are re-registered on
    fluid's ToolRegistry with the risk maps above. Servers with no recording
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
                "[fluid_tool_bridge] no recorded tools for sdk server %r — skipped",
                name,
            )
            continue
        # ``skip_tools``: {server_name: {tool names}} exclusions — used by the
        # native leg to drop mcp__builtins__Task when fluid's own bare-named
        # Task builtin is registered (one orchestration tool, not two).
        excluded = set((skip_tools or {}).get(name) or ())
        if excluded:
            tools = [t for t in tools if getattr(t, "name", "") not in excluded]
            if not tools:
                continue
        register_sdk_tools(
            registry,
            name,
            tools,
            risk_map=SERVER_RISK_MAPS.get(name),
            default_risk=SERVER_DEFAULT_RISK.get(name, "write"),
            defer=name in (defer_servers or ()),
        )
        registered.append(name)
    return registered
