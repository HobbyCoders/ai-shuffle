#!/usr/bin/env python3
"""
Browser MCP Server — Thin httpx proxy to the main process's REST API.

All browser tool calls are proxied through the main FastAPI process's
browser REST API, so both AI and user share the same Playwright browser.

Environment variables (set by browser_mcp_registration.py):
    BROWSER_API_BASE_URL  - e.g. http://127.0.0.1:8000
    BROWSER_INTERNAL_TOKEN - auth token for internal API calls
    BROWSER_SESSION_ID     - chat session ID for browser isolation
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

from app.mcp.browser_tools import BROWSER_TOOLS

# Configure logging to stderr (stdout is for MCP protocol)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)

# Create MCP server
server = Server("browser-tools")

# Env vars injected by browser_mcp_registration.py + query_engine.py
API_BASE_URL = os.environ.get("BROWSER_API_BASE_URL", "http://127.0.0.1:8000")
INTERNAL_TOKEN = os.environ.get("BROWSER_INTERNAL_TOKEN", "")
SESSION_ID = os.environ.get("BROWSER_SESSION_ID", "default")

# Tool name → (HTTP method, endpoint path, body fields)
# Body fields are extracted from tool arguments to build the request JSON.
TOOL_ROUTES: dict[str, tuple[str, str]] = {
    "browser_navigate":    ("POST", "/navigate"),
    "browser_screenshot":  ("GET",  "/screenshot/json"),
    "browser_click":       ("POST", "/click"),
    "browser_type":        ("POST", "/type"),
    "browser_scroll":      ("POST", "/scroll"),
    "browser_get_content": ("POST", "/get-content"),
    "browser_get_elements":("POST", "/get-elements"),
    "browser_back":        ("POST", "/back"),
    "browser_forward":     ("POST", "/forward"),
    "browser_evaluate":    ("POST", "/evaluate"),
    "browser_fill":        ("POST", "/fill"),
    "browser_select":      ("POST", "/select"),
    "browser_wait":        ("POST", "/wait"),
    "browser_tab_new":     ("POST", "/tab/new"),
    "browser_tab_list":    ("GET",  "/tab/list"),
    "browser_tab_switch":  ("POST", "/tab/switch"),
    "browser_close":       ("DELETE", ""),
}


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
    """Proxy a browser tool call to the main process REST API."""
    import httpx

    session_id = os.environ.get("BROWSER_SESSION_ID", SESSION_ID)

    logger.info(f"Proxying browser tool: {name} args: {json.dumps(arguments, default=str)[:200]}")

    route = TOOL_ROUTES.get(name)
    if not route:
        return [TextContent(
            type="text",
            text=json.dumps({"success": False, "error": f"Unknown browser tool: {name}"}),
        )]

    method, path = route
    url = f"{API_BASE_URL}/api/v1/browser/{session_id}{path}"
    headers = {"X-Internal-Token": INTERNAL_TOKEN}

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            if method == "GET":
                # Pass arguments as query params
                resp = await client.get(url, headers=headers, params=arguments or None)
            elif method == "DELETE":
                resp = await client.delete(url, headers=headers)
            else:
                resp = await client.post(url, headers=headers, json=arguments or {})

            if resp.status_code >= 400:
                error_detail = resp.text[:500]
                logger.error(f"REST API error {resp.status_code}: {error_detail}")
                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "success": False,
                        "error": f"API error {resp.status_code}: {error_detail}",
                    }),
                )]

            data = resp.json()

    except httpx.ConnectError as e:
        logger.error(f"Cannot connect to REST API at {API_BASE_URL}: {e}")
        return [TextContent(
            type="text",
            text=json.dumps({
                "success": False,
                "error": f"Cannot connect to browser API: {e}",
            }),
        )]
    except Exception as e:
        logger.error(f"Browser tool proxy failed: {e}", exc_info=True)
        return [TextContent(
            type="text",
            text=json.dumps({"success": False, "error": str(e)}),
        )]

    # Build MCP content blocks from REST response
    content_blocks: list[TextContent | ImageContent] = []

    # Extract screenshot from response (if present)
    screenshot_b64 = data.pop("screenshot", None)

    # Text content (the remaining JSON)
    content_blocks.append(TextContent(
        type="text",
        text=json.dumps(data, indent=2),
    ))

    # Image content (screenshot)
    if screenshot_b64:
        content_blocks.append(ImageContent(
            type="image",
            data=screenshot_b64,
            mimeType="image/jpeg",
        ))

    return content_blocks


async def main():
    """Run the MCP server via stdio."""
    logger.info(
        f"Starting Browser MCP Server (proxy mode) — "
        f"API: {API_BASE_URL}, session: {SESSION_ID}"
    )

    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
    finally:
        logger.info("Browser MCP Server stopped.")


if __name__ == "__main__":
    asyncio.run(main())
