#!/usr/bin/env python3
"""
Browser MCP Server — Standalone stdio MCP server for web browser tools.

This server exposes browser automation tools via the Model Context Protocol,
allowing Claude to navigate, click, type, and interact with web pages using
a headless Chromium browser powered by Playwright.

Usage:
    python -m app.mcp.browser_mcp_server

The server communicates via stdio (stdin/stdout) as per the MCP spec.
"""

import asyncio
import json
import logging
import os
import sys

# Ensure the project root is on the path so app imports work
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent, ImageContent

from app.mcp.browser_session import browser_session_manager
from app.mcp.browser_tools import (
    BROWSER_TOOLS,
    execute_browser_tool,
    set_session_id,
)

# Configure logging to stderr (stdout is for MCP protocol)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)

# Create MCP server
server = Server("browser-tools")

# Default session ID (can be overridden via environment variable)
DEFAULT_SESSION_ID = os.environ.get("BROWSER_SESSION_ID", "default")


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List all available browser tools."""
    tools = []
    for tool_def in BROWSER_TOOLS:
        tools.append(Tool(
            name=tool_def["name"],
            description=tool_def["description"],
            inputSchema=tool_def["input_schema"],
        ))
    return tools


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent | ImageContent]:
    """Execute a browser tool and return results."""
    session_id = os.environ.get("BROWSER_SESSION_ID", DEFAULT_SESSION_ID)

    logger.info(f"Executing browser tool: {name} with args: {json.dumps(arguments, default=str)[:200]}")

    try:
        result = await execute_browser_tool(name, arguments, session_id=session_id)
    except Exception as e:
        logger.error(f"Browser tool {name} failed: {e}", exc_info=True)
        return [TextContent(
            type="text",
            text=json.dumps({"success": False, "error": str(e)}),
        )]

    # Convert our internal result format to MCP content types
    content_blocks = []
    for item in result.get("content", []):
        if item["type"] == "text":
            content_blocks.append(TextContent(
                type="text",
                text=item["text"],
            ))
        elif item["type"] == "image":
            content_blocks.append(ImageContent(
                type="image",
                data=item["data"],
                mimeType=item.get("mimeType", "image/jpeg"),
            ))

    return content_blocks if content_blocks else [TextContent(
        type="text",
        text=json.dumps({"success": False, "error": "No result returned"}),
    )]


async def main():
    """Run the MCP server via stdio."""
    logger.info("Starting Browser MCP Server...")

    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
    finally:
        # Cleanup browser sessions on exit
        await browser_session_manager.close_all()
        logger.info("Browser MCP Server stopped.")


if __name__ == "__main__":
    asyncio.run(main())
