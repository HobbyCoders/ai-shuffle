"""
Compatibility shim for claude_agent_sdk + mcp >=1.12.

MCP 1.12 changed how @server.call_tool() processes handler return values.
The SDK's handler returns a CallToolResult pydantic model, but MCP 1.12's
wrapper checks `hasattr(results, "__iter__")` — which is True for pydantic
models — and iterates it, yielding (field_name, value) tuples instead of
content objects.  This causes 20 validation errors per tool call.

Fix: re-register the call_tool handler on the server instance so tool
functions return a raw content list instead of CallToolResult.
"""

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def patch_sdk_server(server_cfg: dict, tools: list) -> dict:
    """Patch an SDK MCP server so tool calls work with mcp >=1.12.

    Re-registers the @server.call_tool() handler to return a content list
    (which MCP 1.12 correctly wraps) instead of a CallToolResult pydantic
    model (which MCP 1.12 incorrectly iterates).

    Args:
        server_cfg: The dict returned by create_sdk_mcp_server().
        tools: The same tools list passed to create_sdk_mcp_server().

    Returns:
        The same dict, patched in-place.
    """
    try:
        from mcp.types import TextContent

        instance = server_cfg.get("instance")
        if instance is None:
            return server_cfg

        tool_map = {t.name: t for t in tools}

        @instance.call_tool()
        async def call_tool(name: str, arguments: dict[str, Any]) -> list:
            if name not in tool_map:
                raise ValueError(f"Tool '{name}' not found")

            result = await tool_map[name].handler(arguments)

            content = []
            if "content" in result:
                for item in result["content"]:
                    item_type = item.get("type")
                    if item_type == "text":
                        content.append(TextContent(type="text", text=item["text"]))
                    else:
                        content.append(TextContent(
                            type="text",
                            text=json.dumps(item, indent=2),
                        ))

            return content

        logger.debug(
            "Patched SDK call_tool handler for %s (mcp >=1.12 compat)",
            server_cfg.get("name", "unknown"),
        )
    except Exception:
        logger.debug("SDK call_tool patch skipped", exc_info=True)

    return server_cfg
