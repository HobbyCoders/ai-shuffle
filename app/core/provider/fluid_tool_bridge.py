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

import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Any, Callable, Iterable, List, Mapping, Optional

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
    call_timeouts: Optional[Mapping[str, Optional[float]]] = None,
) -> InprocServer:
    """Convert + register in one call. Each tool surfaces as
    ``mcp__<server_name>__<tool>`` (fluid's naming, matching Claude Code).

    ``default_risk`` applies to tools absent from ``risk_map`` — used by
    servers whose tool set is dynamic (connector gateway tools on ``builtins``,
    the ProviderAgent tool) so unknown tools default to gated, not open.

    ``defer=True`` registers the server's tools in fluid's deferred tier
    (announced by name + one-line description, schema loaded on demand via
    ToolSearch) instead of pushing every schema into the prompt prefix.

    ``call_timeouts`` ({tool name: seconds or None}) replaces the registry's
    per-call bound for the named tools (see ``ToolRegistry.register_inproc``).
    """
    risk_map = risk_map or {}
    tools: List[InprocTool] = [
        sdk_tool_to_inproc(t, risk=risk_map.get(getattr(t, "name", ""), default_risk))
        for t in sdk_tools
    ]
    server = create_inproc_server(server_name, tools, version=version)
    if call_timeouts:
        registry.register_inproc(server, defer=defer, call_timeouts=call_timeouts)
    else:
        registry.register_inproc(server, defer=defer)
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
    "SettleSession": "write",
    "HandOffSession": "write",
    "UnsettleSession": "write",
    "ArchiveSession": "write",
    # Agent messaging with this session's Task workers and its parent — the
    # same risk fluid's bare-named originals carry.
    "ListAgents": "safe",
    "SendMessage": "write",
    "ScheduleWakeup": "safe",
    "NotifyDevices": "safe",
    # Listing the session's authorized phones reads state only. A phone
    # action changes the device, so it keeps the write gate (and most
    # actions also wait for an on-phone Allow).
    "AndroidDevices": "safe",
    "AndroidAction": "write",
    # Monitor runs a host shell command; fluid rates its own Monitor "write".
    "Monitor": "write",
    "TaskOutput": "safe",
    "TaskGet": "safe",
    "TaskList": "safe",
    "TaskStop": "write",
    "CronList": "safe",
    "CronCreate": "write",
    "CronDelete": "destructive",
    "GetAppErrors": "safe",
    "GetAppLogs": "safe",
    "GetUsage": "safe",
    "DeployStatus": "safe",
    # Plugins (app/mcp/plugin_tools.py). An install lands switched off and
    # pending; the owner enables it in Settings, so this gate is the first
    # of two.
    "FindPlugins": "safe",
    "CheckPlugin": "safe",
    "InstallPlugin": "write",
    "WorktreeReport": "safe",
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
    "zoom": "safe", "wait": "safe", "release_control": "safe", "list_windows": "safe",
    "ui_tree": "safe",
    # Everything that drives the desktop mutates it:
    "left_click": "write", "right_click": "write", "middle_click": "write",
    "double_click": "write", "triple_click": "write", "left_click_drag": "write",
    "left_mouse_down": "write", "left_mouse_up": "write", "mouse_move": "write",
    "key": "write", "hold_key": "write", "type": "write", "scroll": "write",
    "switch_display": "write", "open_application": "write",
    "focus_window": "write", "window_control": "write",
    "click_element": "write", "set_value": "write",
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
        "ChatWriteFile": "safe", "ChatRunCommand": "safe", "ChatHttpRequest": "safe",
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
# stores via ctx.extra. ListAgents/SendMessage are the delegated leg's route to
# its Task workers; native legs wire fluid's originals through agent_roster /
# agent_message on the engine ctx.
NATIVE_SHADOWED_BUILTINS: frozenset[str] = frozenset({
    "Task", "Monitor", "ToolSearch", "GetUsage",
    "Remember", "Recall", "Forget", "SessionSearch", "SessionView",
    "ListSessions", "MessageSession", "StartSession",
    "SettleSession", "UnsettleSession", "ArchiveSession",
    "ListAgents", "SendMessage",
})

# Individual tools of EAGER servers (and fluid builtins) that the native leg
# moves to the deferred tier: each serves a specific owner request (pair an
# Android device, build a plugin, schedule a cron, read the app's own logs,
# tidy the sidebar), not the turn-by-turn work, and together their full docs
# cost ~3.8k tokens on every first request (AndroidAction's JSON schema alone
# is ~1.5k). Deferred, each is still announced by name + one-liner and stays
# callable: ToolSearch loads the schema on the JSON path, ``read_tool_docs``
# serves the docs under code mode. The set is applied once at session build,
# so the prompt prefix stays byte-stable across turns. Registry names, so a
# fluid builtin and an ``mcp__builtins__`` bridge copy are both expressible.
NATIVE_DEFERRED_TOOLS: frozenset[str] = frozenset({
    "mcp__builtins__AndroidAction",
    "mcp__builtins__AndroidDevices",
    "mcp__builtins__CheckPlugin",
    "mcp__builtins__FindPlugins",
    "mcp__builtins__InstallPlugin",
    "mcp__builtins__CronCreate",
    "mcp__builtins__CronDelete",
    "mcp__builtins__CronList",
    "mcp__builtins__RemoteTrigger",
    "mcp__builtins__DeployStatus",
    "mcp__builtins__GetAppErrors",
    "mcp__builtins__GetAppLogs",
    "mcp__builtins__HandOffSession",
    "mcp__builtins__NotifyDevices",
    "mcp__builtins__WorktreeReport",
    "SettleSession",
    "UnsettleSession",
    "ArchiveSession",
})


def defer_native_tools(registry: ToolRegistry) -> List[str]:
    """Move :data:`NATIVE_DEFERRED_TOOLS` into the registry's deferred tier.

    Returns the names actually deferred (absent tools are skipped). A vendored
    fluid without ``defer_schema`` leaves everything eager, the old behaviour.
    """
    defer = getattr(registry, "defer_schema", None)
    if not callable(defer):
        return []
    return [name for name in sorted(NATIVE_DEFERRED_TOOLS) if defer(name)]


def _builtins_call_timeouts(registry: ToolRegistry) -> dict[str, Optional[float]]:
    """Per-tool call bounds for the ``builtins`` server, over the registry's.

    A synchronous Task blocks until its worker reports, and task_dispatch
    caps the run at ``task_worker_sync_max_seconds``. That cap starts only
    after the dispatch clears pool admission, which can queue for up to
    ``_ADMISSION_WAIT_SECONDS`` (or the longer provider-row queue budget),
    and a retry attempt queues for admission again inside the cap. The
    registry's general bound (330 s by default) would cancel such a call
    with ``tool_timeout`` before task_dispatch could return its own
    worker-failed result, so Task gets the sync cap plus two admission
    waits plus a margin for the report, or no bound when the cap is off.
    The general bound still applies if it is the larger one. Monitor's
    inline wait (at most 120 s) and TaskOutput (never blocks) fit under the
    general bound and keep it.
    """
    from app.core import task_dispatch

    cap = task_dispatch._sync_cap_seconds()
    if cap <= 0:
        return {"Task": None}
    general = getattr(registry, "external_call_timeout", None)
    if not general or general <= 0:
        return {"Task": None}
    queue = max(
        float(task_dispatch._ADMISSION_WAIT_SECONDS),
        task_dispatch._provider_queue_seconds(),
    )
    return {"Task": max(cap + 2 * queue + 60.0, float(general))}


# ─── computer-use: never cancel a desktop action from a timer ────────────────
#
# Desktop actions run on a worker thread, and a timer cancel in the middle of
# one leaves its outcome unknown. The registry's hang guard
# (``fluid_external_call_timeout_seconds``) and code mode's per-call ceiling
# both cancel by timer. So the computer-use server's tools are registered
# without the registry bound and wrapped: past the bound the call answers with
# an "outcome unknown" error (EXECUTION_OUTCOME_UNKNOWN, the desktop's own
# vocabulary) and the action keeps running to its end, holding the desktop
# lock, so a repeat is refused as DESKTOP_BUSY rather than performed twice.
# Cancelling the caller (a Stop, steering, an engine close) does cancel the
# action: the handler invalidates its input (``abort.cancel_inflight``)
# without halting the session, so a call reported cancelled is never still
# typing. A real Stop has already halted the desktop with its true reason
# (``abort.request_stop`` in the session runtime).
COMPUTER_USE_SERVER = "computer-use"
# Detached desktop calls still running, so they are not garbage-collected.
_DETACHED_CALLS: set = set()


def _still_running_result(tool: str, seconds: float) -> dict:
    payload = {
        "success": False,
        "error": (
            f"{tool} did not finish within {seconds:g} s and is still running on "
            "the desktop. Its outcome is unknown: do not repeat it. Desktop calls "
            "answer DESKTOP_BUSY until it ends; then take a screenshot to see what "
            "actually happened before acting again."
        ),
        "code": "EXECUTION_OUTCOME_UNKNOWN",
        "outcome": "unknown",
    }
    return {"isError": True, "content": [{"type": "text", "text": json.dumps(payload)}]}


def _forget_detached(task: "asyncio.Task") -> None:
    _DETACHED_CALLS.discard(task)
    if not task.cancelled():
        task.exception()  # retrieved, so a late failure is not logged as unhandled


def detach_on_timeout(
    tool_name: str, handler: Callable[[dict], Any], bound: Callable[[], Optional[float]],
) -> Callable[[dict], Any]:
    """``handler`` answered within ``bound()`` seconds, never cancelled by it.

    Cancelling the wrapper (a Stop, steering, an engine close) cancels the
    handler too, whose cancel path stops its input without halting the
    session. Only the timer leaves it running.
    """

    async def call(args: dict) -> Any:
        task = asyncio.ensure_future(handler(args))
        _DETACHED_CALLS.add(task)
        task.add_done_callback(_forget_detached)
        limit = bound()
        try:
            await asyncio.wait({task}, timeout=limit if limit and limit > 0 else None)
        except asyncio.CancelledError:
            task.cancel()
            raise
        if task.done():
            return task.result()
        logger.warning(
            "[fluid_tool_bridge] computer-use %s still running after %gs; answered "
            "without cancelling it", tool_name, limit,
        )
        return _still_running_result(tool_name, float(limit or 0))

    return call


def _detached_tools(registry: ToolRegistry, tools: List[Any]) -> List[Any]:
    """The computer-use tools with ``detach_on_timeout`` handlers."""

    def bound() -> Optional[float]:
        return getattr(registry, "external_call_timeout", None)

    out: List[Any] = []
    for tool in tools:
        name = getattr(tool, "name", "")
        out.append(SimpleNamespace(
            name=name,
            description=getattr(tool, "description", "") or "",
            input_schema=getattr(tool, "input_schema", None),
            handler=detach_on_timeout(name, tool.handler, bound),
        ))
    return out


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
        call_timeouts: Optional[Mapping[str, Optional[float]]] = None
        if name == "builtins":
            call_timeouts = _builtins_call_timeouts(registry)
        elif name == COMPUTER_USE_SERVER:
            tools = _detached_tools(registry, tools)
            call_timeouts = {getattr(t, "name", ""): None for t in tools}
        register_sdk_tools(
            registry,
            name,
            tools,
            risk_map=SERVER_RISK_MAPS.get(name),
            default_risk=SERVER_DEFAULT_RISK.get(name, "write"),
            defer=name in (defer_servers or ()),
            call_timeouts=call_timeouts,
        )
        registered.append(name)
    return registered
