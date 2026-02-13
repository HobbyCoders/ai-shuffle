"""
In-process SDK MCP server for browser tools.

Replaces the external stdio MCP subprocess (browser_mcp_server.py) with an
in-process server using the Claude Agent SDK's create_sdk_mcp_server().
Each tool proxies HTTP requests to the main FastAPI browser REST API,
so both AI and user share the same Playwright browser session.

Usage:
    from app.mcp.browser_sdk_tools import create_browser_tools_server

    server = create_browser_tools_server(
        base_url="http://127.0.0.1:8000",
        session_id="chat-session-id",
        auth_token="internal-token",
    )
    # Pass to ClaudeAgentOptions.mcp_servers dict
"""

import json
import logging
from typing import Any

import httpx

from claude_agent_sdk import tool, create_sdk_mcp_server

logger = logging.getLogger(__name__)

# HTTP method and path for each tool, matching browser_mcp_server.py TOOL_ROUTES
TOOL_ROUTES: dict[str, tuple[str, str]] = {
    "browser_navigate":     ("POST",   "/navigate"),
    "browser_screenshot":   ("GET",    "/screenshot/json"),
    "browser_click":        ("POST",   "/click"),
    "browser_type":         ("POST",   "/type"),
    "browser_scroll":       ("POST",   "/scroll"),
    "browser_get_content":  ("POST",   "/get-content"),
    "browser_get_elements": ("POST",   "/get-elements"),
    "browser_back":         ("POST",   "/back"),
    "browser_forward":      ("POST",   "/forward"),
    "browser_evaluate":     ("POST",   "/evaluate"),
    "browser_fill":         ("POST",   "/fill"),
    "browser_select":       ("POST",   "/select"),
    "browser_wait":         ("POST",   "/wait"),
    "browser_tab_new":      ("POST",   "/tab/new"),
    "browser_tab_list":     ("GET",    "/tab/list"),
    "browser_tab_switch":   ("POST",   "/tab/switch"),
    "browser_close":        ("DELETE", ""),
}


def _text_result(data: dict) -> dict[str, Any]:
    """Create a text-only MCP tool result."""
    return {"content": [{"type": "text", "text": json.dumps(data, indent=2)}]}


def _text_and_image_result(data: dict, screenshot_b64: str) -> dict[str, Any]:
    """Create an MCP result with text and optional screenshot image."""
    content: list[dict[str, Any]] = [
        {"type": "text", "text": json.dumps(data, indent=2)},
    ]
    if screenshot_b64:
        content.append({
            "type": "image",
            "data": screenshot_b64,
            "mimeType": "image/jpeg",
        })
    return {"content": content}


def _error_result(error: str) -> dict[str, Any]:
    """Create an error MCP tool result."""
    return {"content": [{"type": "text", "text": json.dumps({"success": False, "error": error})}]}


async def _proxy_request(
    api_url: str,
    headers: dict[str, str],
    method: str,
    path: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    """
    Make an HTTP request to the browser REST API and return the MCP result.

    Handles GET (query params), POST (JSON body), and DELETE methods.
    Extracts screenshot data from the response and returns it as an image
    content block alongside the text JSON.
    """
    url = f"{api_url}{path}"

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            if method == "GET":
                resp = await client.get(url, headers=headers, params=args or None)
            elif method == "DELETE":
                resp = await client.delete(url, headers=headers)
            else:
                resp = await client.post(url, headers=headers, json=args or {})

            if resp.status_code >= 400:
                error_detail = resp.text[:500]
                logger.error(f"Browser REST API error {resp.status_code}: {error_detail}")
                return _error_result(f"API error {resp.status_code}: {error_detail}")

            data = resp.json()

    except httpx.ConnectError as e:
        logger.error(f"Cannot connect to browser REST API: {e}")
        return _error_result(f"Cannot connect to browser API: {e}")
    except Exception as e:
        logger.error(f"Browser tool proxy failed: {e}", exc_info=True)
        return _error_result(str(e))

    # Extract screenshot from response (if present) and build content blocks
    screenshot_b64 = data.pop("screenshot", None)
    if screenshot_b64:
        return _text_and_image_result(data, screenshot_b64)
    return _text_result(data)


def create_browser_tools_server(
    base_url: str,
    session_id: str,
    auth_token: str,
):
    """
    Create an in-process SDK MCP server for browser tools.

    All tool calls are proxied to the main FastAPI browser REST API so that
    both the AI agent and the user share the same Playwright browser session.

    Args:
        base_url: Backend base URL, e.g. "http://127.0.0.1:8000"
        session_id: Browser/chat session ID for session isolation
        auth_token: Internal auth token for the browser REST API

    Returns:
        McpSdkServerConfig to be added to ClaudeAgentOptions.mcp_servers
    """
    api_url = f"{base_url}/api/v1/browser/{session_id}"
    headers = {"X-Internal-Token": auth_token}

    # ------------------------------------------------------------------
    # Tool definitions — schemas match browser_tools.py BROWSER_TOOLS
    # ------------------------------------------------------------------

    @tool("browser_navigate", "Navigate to a URL in the browser. Returns page title, status, content summary, and screenshot.", {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "URL to navigate to (e.g., 'https://example.com' or 'example.com')",
            },
        },
        "required": ["url"],
    })
    async def browser_navigate(args: dict[str, Any]) -> dict[str, Any]:
        return await _proxy_request(api_url, headers, "POST", "/navigate", args)

    @tool("browser_screenshot", "Take a screenshot of the current browser page.", {
        "type": "object",
        "properties": {
            "full_page": {
                "type": "boolean",
                "description": "Capture the full scrollable page (default: false, captures viewport only)",
                "default": False,
            },
        },
    })
    async def browser_screenshot(args: dict[str, Any]) -> dict[str, Any]:
        return await _proxy_request(api_url, headers, "GET", "/screenshot/json", args)

    @tool("browser_click", "Click an element on the page. Use element_index (from browser_get_elements), visible text, CSS selector, or x/y coordinates.", {
        "type": "object",
        "properties": {
            "element_index": {
                "type": "integer",
                "description": "Element index from browser_get_elements (1-based)",
            },
            "text": {
                "type": "string",
                "description": "Visible text of the element to click",
            },
            "selector": {
                "type": "string",
                "description": "CSS selector of the element to click",
            },
            "x": {
                "type": "integer",
                "description": "X coordinate to click",
            },
            "y": {
                "type": "integer",
                "description": "Y coordinate to click",
            },
        },
    })
    async def browser_click(args: dict[str, Any]) -> dict[str, Any]:
        return await _proxy_request(api_url, headers, "POST", "/click", args)

    @tool("browser_type", "Type text into the focused element or a specified selector. Optionally press Enter after typing.", {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Text to type",
            },
            "selector": {
                "type": "string",
                "description": "Optional CSS selector to type into (uses fill for immediate input). If omitted, types into the currently focused element.",
            },
            "press_enter": {
                "type": "boolean",
                "description": "Press Enter after typing (default: false)",
                "default": False,
            },
        },
        "required": ["text"],
    })
    async def browser_type(args: dict[str, Any]) -> dict[str, Any]:
        return await _proxy_request(api_url, headers, "POST", "/type", args)

    @tool("browser_scroll", "Scroll the page up or down.", {
        "type": "object",
        "properties": {
            "direction": {
                "type": "string",
                "description": "Scroll direction",
                "enum": ["up", "down"],
                "default": "down",
            },
            "amount": {
                "type": "integer",
                "description": "Number of scroll units (1 unit \u2248 300px, default: 3)",
                "default": 3,
            },
        },
    })
    async def browser_scroll(args: dict[str, Any]) -> dict[str, Any]:
        return await _proxy_request(api_url, headers, "POST", "/scroll", args)

    @tool("browser_get_content", "Get the readable text content of the page. Returns clean text (not raw HTML), truncated to ~4000 chars. Use selector to extract content from a specific element.", {
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": "Optional CSS selector to extract content from (default: entire page body)",
            },
        },
    })
    async def browser_get_content(args: dict[str, Any]) -> dict[str, Any]:
        return await _proxy_request(api_url, headers, "POST", "/get-content", args)

    @tool("browser_get_elements", "Get a numbered list of interactive elements on the page (links, buttons, inputs, selects). Use the returned index numbers with browser_click(element_index=N) to interact.", {
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": "Optional CSS selector to scope the search",
            },
        },
    })
    async def browser_get_elements(args: dict[str, Any]) -> dict[str, Any]:
        return await _proxy_request(api_url, headers, "POST", "/get-elements", args)

    @tool("browser_back", "Navigate back in browser history.", {
        "type": "object",
        "properties": {},
    })
    async def browser_back(args: dict[str, Any]) -> dict[str, Any]:
        return await _proxy_request(api_url, headers, "POST", "/back", args)

    @tool("browser_forward", "Navigate forward in browser history.", {
        "type": "object",
        "properties": {},
    })
    async def browser_forward(args: dict[str, Any]) -> dict[str, Any]:
        return await _proxy_request(api_url, headers, "POST", "/forward", args)

    @tool("browser_evaluate", "Execute JavaScript code in the browser and return the result. Use for advanced interactions or data extraction.", {
        "type": "object",
        "properties": {
            "script": {
                "type": "string",
                "description": "JavaScript code to execute in the browser context",
            },
        },
        "required": ["script"],
    })
    async def browser_evaluate(args: dict[str, Any]) -> dict[str, Any]:
        return await _proxy_request(api_url, headers, "POST", "/evaluate", args)

    @tool("browser_fill", "Fill a form field identified by its label, placeholder, aria-label, or name attribute.", {
        "type": "object",
        "properties": {
            "label": {
                "type": "string",
                "description": "Label text, placeholder, aria-label, or name of the form field",
            },
            "value": {
                "type": "string",
                "description": "Value to fill into the field",
            },
        },
        "required": ["label", "value"],
    })
    async def browser_fill(args: dict[str, Any]) -> dict[str, Any]:
        return await _proxy_request(api_url, headers, "POST", "/fill", args)

    @tool("browser_select", "Select an option from a dropdown/select element.", {
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": "CSS selector of the select element",
            },
            "value": {
                "type": "string",
                "description": "Option value or visible text to select",
            },
        },
        "required": ["selector", "value"],
    })
    async def browser_select(args: dict[str, Any]) -> dict[str, Any]:
        return await _proxy_request(api_url, headers, "POST", "/select", args)

    @tool("browser_wait", "Wait for a condition \u2014 an element to appear/disappear, or network to become idle.", {
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": "CSS selector to wait for (omit to wait for network idle)",
            },
            "state": {
                "type": "string",
                "description": "State to wait for",
                "enum": ["visible", "hidden", "attached", "detached"],
                "default": "visible",
            },
            "timeout": {
                "type": "integer",
                "description": "Maximum wait time in milliseconds (default: 10000)",
                "default": 10000,
            },
        },
    })
    async def browser_wait(args: dict[str, Any]) -> dict[str, Any]:
        return await _proxy_request(api_url, headers, "POST", "/wait", args)

    @tool("browser_tab_new", "Open a new browser tab, optionally navigating to a URL.", {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Optional URL to open in the new tab",
            },
        },
    })
    async def browser_tab_new(args: dict[str, Any]) -> dict[str, Any]:
        return await _proxy_request(api_url, headers, "POST", "/tab/new", args)

    @tool("browser_tab_list", "List all open browser tabs with their URLs and titles.", {
        "type": "object",
        "properties": {},
    })
    async def browser_tab_list(args: dict[str, Any]) -> dict[str, Any]:
        return await _proxy_request(api_url, headers, "GET", "/tab/list", args)

    @tool("browser_tab_switch", "Switch to a different browser tab by index (1-based).", {
        "type": "object",
        "properties": {
            "index": {
                "type": "integer",
                "description": "Tab index to switch to (1-based, from browser_tab_list)",
            },
        },
        "required": ["index"],
    })
    async def browser_tab_switch(args: dict[str, Any]) -> dict[str, Any]:
        return await _proxy_request(api_url, headers, "POST", "/tab/switch", args)

    @tool("browser_close", "Close the browser session and free resources.", {
        "type": "object",
        "properties": {},
    })
    async def browser_close(args: dict[str, Any]) -> dict[str, Any]:
        return await _proxy_request(api_url, headers, "DELETE", "", args)

    # ------------------------------------------------------------------
    # Assemble and return the SDK MCP server
    # ------------------------------------------------------------------
    all_tools = [
        browser_navigate,
        browser_screenshot,
        browser_click,
        browser_type,
        browser_scroll,
        browser_get_content,
        browser_get_elements,
        browser_back,
        browser_forward,
        browser_evaluate,
        browser_fill,
        browser_select,
        browser_wait,
        browser_tab_new,
        browser_tab_list,
        browser_tab_switch,
        browser_close,
    ]

    return create_sdk_mcp_server(name="browser-tools", tools=all_tools)
