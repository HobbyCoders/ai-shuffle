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
) -> InprocServer:
    """Convert + register in one call. Each tool surfaces as
    ``mcp__<server_name>__<tool>`` (omni's naming, matching Claude Code)."""
    server = sdk_tools_to_inproc_server(
        server_name, sdk_tools, version=version, risk_map=risk_map
    )
    registry.register_inproc(server)
    return server


# ─── Worked example: app/mcp/builtin_tools.py ─────────────────────────────────
#
# builtin_tools.py builds its ``@tool`` objects inside ``create_builtin_tools_server``
# and passes them straight into ``create_sdk_mcp_server`` (they are not returned
# separately). To port it WITHOUT editing that module, add a single line at the
# assembly site (right before the existing ``return patch_sdk_server(...)``):
#
#     if omni_registry is not None:  # only when running under the omni harness
#         from app.core.provider.omni_tool_bridge import register_sdk_tools
#         register_sdk_tools(omni_registry, "builtins", all_tools,
#                            risk_map=BUILTINS_RISK_MAP)
#
# That is the entire port. The risk map below classifies the write/destructive
# builtins so omni's PermissionEngine gates them the way the SDK path did.

BUILTINS_RISK_MAP: Mapping[str, str] = {
    "ScheduleWakeup": "safe",
    "NotifyDevices": "safe",
    "Monitor": "safe",
    "TaskOutput": "safe",
    "TaskStop": "write",
    "CronCreate": "write",
    "CronDelete": "destructive",
    "EnterWorktree": "write",
    "ExitWorktree": "write",
}


def bridge_builtins_tools(sdk_tools: Iterable[Any]) -> InprocServer:
    """Return the omni InprocServer for the ``builtins`` module's SDK tools.

    Pass the same ``all_tools`` list ``create_builtin_tools_server`` assembles.
    Kept separate from that module so builtin_tools.py stays free of any omni
    import (additive, reversible).
    """
    return sdk_tools_to_inproc_server("builtins", sdk_tools, risk_map=BUILTINS_RISK_MAP)


# Remaining in-process tool modules to port (identical one-liner each) — all use
# the SDK @tool / create_sdk_mcp_server pattern, so ``register_sdk_tools`` covers
# them with only a server-name + risk-map choice:
#
#   app/mcp/ai_tools_sdk.py
#   app/mcp/app_config_sdk.py
#   app/mcp/computer_use_sdk.py
#   app/mcp/config_automations.py            (config_* share config_helpers)
#   app/mcp/config_connectors.py
#   app/mcp/config_hooks.py
#   app/mcp/config_mcp_servers.py
#   app/mcp/config_profiles.py
#   app/mcp/config_subagents.py
#   app/mcp/connector_gateway_sdk.py
#   app/mcp/connector_tools_sdk.py
#   app/mcp/phone_tools.py
#
# (builtin_tools.py is the one proven above.)
REMAINING_TOOL_MODULES: List[str] = [
    "app/mcp/ai_tools_sdk.py",
    "app/mcp/app_config_sdk.py",
    "app/mcp/computer_use_sdk.py",
    "app/mcp/config_automations.py",
    "app/mcp/config_connectors.py",
    "app/mcp/config_hooks.py",
    "app/mcp/config_mcp_servers.py",
    "app/mcp/config_profiles.py",
    "app/mcp/config_subagents.py",
    "app/mcp/connector_gateway_sdk.py",
    "app/mcp/connector_tools_sdk.py",
    "app/mcp/phone_tools.py",
]
