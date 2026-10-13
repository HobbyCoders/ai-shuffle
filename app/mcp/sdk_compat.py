"""Host-owned tool assembly and optional MCP content adapter.

Fluid InprocServer instances carry their tools directly. Older plugins may
supply an MCP Server; keep its text/image conversion without any agent SDK.
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
        server_cfg: The dict returned by create_tool_server().
        tools: The same tools list passed to create_tool_server().

    Returns:
        The same dict, patched in-place.
    """
    # Every in-proc server passes through here with its raw @tool objects
    # still in hand — record the pair so the fluid harness can re-register the
    # same tools on its own registry (see provider/sdk_tool_records.py).
    try:
        from app.core.provider.sdk_tool_records import record_sdk_server_tools
        record_sdk_server_tools(server_cfg, tools)
    except Exception:
        logger.debug("sdk tool recording skipped", exc_info=True)

    try:
        from mcp.types import TextContent, ImageContent

        instance = server_cfg.get("instance")
        if instance is None or not hasattr(instance, "call_tool"):
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
                    elif item_type == "image":
                        # Pass inline images through as proper ImageContent so the
                        # model can actually see them (e.g. computer-use screenshots).
                        # Without this branch they'd be JSON-stringified into text.
                        content.append(ImageContent(
                            type="image",
                            data=item["data"],
                            mimeType=item.get("mimeType", "image/png"),
                        ))
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
