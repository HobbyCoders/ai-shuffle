"""
Connector Tools SDK MCP Server — dynamic tool registration from active connectors.

When connectors with TOOLS definitions are running, this module creates an SDK
MCP server that exposes each connector tool to the AI model.  The AI can then
invoke connector capabilities directly (e.g. control Spotify, manage Hue lights).

Tools are registered lazily at query time based on which connectors are active.
"""

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _tool_result(result: dict) -> dict:
    """Wrap a result dict in the MCP tool response format."""
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}


def create_connector_tools_server() -> Optional[Any]:
    """
    Create an SDK MCP server with tools from all active connectors.

    Iterates over running connectors, collects their TOOLS definitions, and
    registers each as an AI-callable tool.  Returns ``None`` if no connector
    tools are available.

    Returns:
        McpSdkServerConfig from create_sdk_mcp_server(), or None
    """
    try:
        from claude_agent_sdk import tool, create_sdk_mcp_server
    except ImportError:
        logger.debug("claude_agent_sdk not available — connector tools disabled")
        return None

    from app.core.connectors.manager import connector_manager

    tools: list = []
    tool_names: List[str] = []

    for connector_id, instance in connector_manager.get_running_connectors():
        for tool_def in instance.TOOLS:
            # Build a unique tool name: {connector_type}_{tool_id}
            tool_name = f"{instance.connector_type}_{tool_def.id}"

            # Avoid duplicate names if multiple connectors of the same type
            # are running — append a short suffix from the connector ID
            if tool_name in tool_names:
                short_id = connector_id.replace("conn-", "")[:6]
                tool_name = f"{tool_name}_{short_id}"

            tool_names.append(tool_name)

            # Build the tool description with connector context
            description = (
                f"{tool_def.description} "
                f"(via {instance.connector_type} connector '{connector_id}')"
            )

            # Use closures to capture the connector_id and tool_id
            # for each tool function
            _cid = connector_id
            _tid = tool_def.id
            _schema = tool_def.input_schema or {
                "type": "object",
                "properties": {},
            }

            @tool(tool_name, description, _schema)
            async def tool_fn(
                args: dict,
                __cid: str = _cid,
                __tid: str = _tid,
            ) -> dict:
                inst = await connector_manager.get_running_connector(__cid)
                if not inst:
                    return _tool_result({
                        "success": False,
                        "error": f"Connector {__cid} is not running",
                    })
                try:
                    result = await inst.execute_tool(__tid, args)
                    return _tool_result(result)
                except NotImplementedError:
                    return _tool_result({
                        "success": False,
                        "error": f"Tool '{__tid}' is not implemented",
                    })
                except Exception as e:
                    logger.error(
                        "Connector tool %s/%s failed: %s", __cid, __tid, e,
                        exc_info=True,
                    )
                    return _tool_result({
                        "success": False,
                        "error": str(e),
                    })

            tools.append(tool_fn)

    if not tools:
        return None

    logger.info(
        "Registering %d connector tool(s) as MCP tools: %s",
        len(tools), tool_names,
    )

    return create_sdk_mcp_server("connector-tools", tools)


def get_connector_tool_names() -> List[str]:
    """
    Return list of MCP tool names from active connectors.

    These are in the ``mcp__connector-tools__<tool_name>`` format for
    use in ``allowed_tools`` configuration.
    """
    from app.core.connectors.manager import connector_manager

    names: List[str] = []
    seen: set = set()

    for connector_id, instance in connector_manager.get_running_connectors():
        for tool_def in instance.TOOLS:
            tool_name = f"{instance.connector_type}_{tool_def.id}"
            if tool_name in seen:
                short_id = connector_id.replace("conn-", "")[:6]
                tool_name = f"{tool_name}_{short_id}"
            seen.add(tool_name)
            names.append(f"mcp__connector-tools__{tool_name}")

    return names
