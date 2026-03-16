"""
Lightweight async Chrome DevTools Protocol (CDP) client.

Connects to Chrome/Chromium via the CDP WebSocket endpoint and provides
methods for page control, input dispatch, content extraction, and screenshots.

This replaces the Playwright CDP session with a direct WebSocket connection,
enabling use of real (non-headless) Chrome instances launched externally.
"""

import asyncio
import base64
import json
import logging
from typing import Any, Callable, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)


class CDPError(Exception):
    """Error from a CDP command."""
    pass


class CDPClient:
    """Async CDP client for a single browser target (page).

    Connects to a Chrome DevTools Protocol WebSocket endpoint and provides
    methods for sending CDP commands and receiving events.

    Usage:
        client = CDPClient(ws_url="ws://localhost:9222/devtools/page/ABC123")
        await client.connect()
        result = await client.send("Page.navigate", {"url": "https://example.com"})
        await client.close()
    """

    def __init__(self, ws_url: str, timeout: float = 30.0):
        self.ws_url = ws_url
        self.timeout = timeout
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._msg_id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._event_handlers: Dict[str, List[Callable]] = {}
        self._recv_task: Optional[asyncio.Task] = None
        self._connected = False
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._connected and self._ws is not None and not self._ws.closed

    async def connect(self) -> None:
        """Connect to the CDP WebSocket endpoint."""
        if self._connected:
            return

        self._session = aiohttp.ClientSession()
        try:
            self._ws = await self._session.ws_connect(
                self.ws_url,
                max_msg_size=50 * 1024 * 1024,  # 50MB for screenshots
                heartbeat=30.0,
            )
        except Exception as e:
            await self._session.close()
            self._session = None
            raise CDPError(f"Failed to connect to CDP: {e}") from e

        self._connected = True
        self._recv_task = asyncio.create_task(self._recv_loop())
        logger.debug(f"CDP connected: {self.ws_url}")

    async def close(self) -> None:
        """Close the CDP connection."""
        self._connected = False

        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except (asyncio.CancelledError, Exception):
                pass
            self._recv_task = None

        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None

        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

        # Cancel all pending futures
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()

        logger.debug(f"CDP disconnected: {self.ws_url}")

    async def send(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Send a CDP command and wait for the response.

        Args:
            method: CDP method name (e.g., "Page.navigate")
            params: Optional parameters dict

        Returns:
            Result dict from CDP response

        Raises:
            CDPError: If the command fails or times out
        """
        if not self.connected:
            raise CDPError("Not connected to CDP")

        async with self._lock:
            self._msg_id += 1
            msg_id = self._msg_id

        message = {"id": msg_id, "method": method}
        if params:
            message["params"] = params

        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = future

        try:
            await self._ws.send_json(message)  # type: ignore
        except Exception as e:
            self._pending.pop(msg_id, None)
            raise CDPError(f"Failed to send CDP command {method}: {e}") from e

        try:
            result = await asyncio.wait_for(future, timeout=self.timeout)
        except asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            raise CDPError(f"CDP command {method} timed out after {self.timeout}s")

        return result

    def on(self, event: str, handler: Callable) -> None:
        """Register an event handler for a CDP event.

        Args:
            event: CDP event name (e.g., "Page.screencastFrame")
            handler: Callback function(params_dict)
        """
        if event not in self._event_handlers:
            self._event_handlers[event] = []
        self._event_handlers[event].append(handler)

    def off(self, event: str, handler: Optional[Callable] = None) -> None:
        """Remove an event handler.

        Args:
            event: CDP event name
            handler: Specific handler to remove, or None to remove all
        """
        if event not in self._event_handlers:
            return
        if handler is None:
            del self._event_handlers[event]
        else:
            self._event_handlers[event] = [
                h for h in self._event_handlers[event] if h != handler
            ]
            if not self._event_handlers[event]:
                del self._event_handlers[event]

    async def _recv_loop(self) -> None:
        """Background task to receive and dispatch CDP messages."""
        try:
            async for msg in self._ws:  # type: ignore
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue

                    # Response to a command
                    if "id" in data:
                        msg_id = data["id"]
                        future = self._pending.pop(msg_id, None)
                        if future and not future.done():
                            if "error" in data:
                                error = data["error"]
                                future.set_exception(
                                    CDPError(f"CDP error: {error.get('message', error)}")
                                )
                            else:
                                future.set_result(data.get("result", {}))

                    # Event notification
                    elif "method" in data:
                        event_name = data["method"]
                        params = data.get("params", {})
                        handlers = self._event_handlers.get(event_name, [])
                        for handler in handlers:
                            try:
                                result = handler(params)
                                if asyncio.iscoroutine(result):
                                    asyncio.create_task(result)
                            except Exception as e:
                                logger.debug(f"CDP event handler error for {event_name}: {e}")

                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"CDP recv loop error: {e}")
        finally:
            self._connected = False

    # ------------------------------------------------------------------
    # Convenience methods for common CDP operations
    # ------------------------------------------------------------------

    async def navigate(self, url: str) -> Dict[str, Any]:
        """Navigate to a URL."""
        return await self.send("Page.navigate", {"url": url})

    async def reload(self) -> Dict[str, Any]:
        """Reload the current page."""
        return await self.send("Page.reload")

    async def evaluate(self, expression: str) -> Any:
        """Evaluate JavaScript expression and return the result value."""
        result = await self.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        })
        remote_obj = result.get("result", {})
        if "exceptionDetails" in result:
            desc = result["exceptionDetails"].get("text", "JS evaluation error")
            raise CDPError(f"JavaScript error: {desc}")
        return remote_obj.get("value")

    async def screenshot(self, format: str = "jpeg", quality: int = 75, full_page: bool = False) -> str:
        """Take a screenshot and return base64-encoded data.

        Args:
            format: "jpeg" or "png"
            quality: JPEG quality (0-100)
            full_page: Whether to capture full scrollable page

        Returns:
            Base64-encoded screenshot string
        """
        params: Dict[str, Any] = {"format": format}
        if format == "jpeg":
            params["quality"] = quality
        if full_page:
            # Get full page metrics
            metrics = await self.send("Page.getLayoutMetrics")
            content_size = metrics.get("contentSize", metrics.get("cssContentSize", {}))
            if content_size:
                params["clip"] = {
                    "x": 0,
                    "y": 0,
                    "width": content_size.get("width", 1280),
                    "height": content_size.get("height", 720),
                    "scale": 1,
                }

        result = await self.send("Page.captureScreenshot", params)
        return result.get("data", "")

    async def get_title(self) -> str:
        """Get the current page title."""
        try:
            return await self.evaluate("document.title") or ""
        except Exception:
            return ""

    async def get_url(self) -> str:
        """Get the current page URL."""
        try:
            return await self.evaluate("window.location.href") or ""
        except Exception:
            return ""

    async def dispatch_mouse_event(
        self,
        event_type: str,
        x: float,
        y: float,
        button: str = "left",
        click_count: int = 1,
    ) -> None:
        """Dispatch a mouse event."""
        params: Dict[str, Any] = {
            "type": event_type,
            "x": x,
            "y": y,
            "button": button,
            "clickCount": click_count,
        }
        if event_type in ("mousePressed", "mouseReleased"):
            buttons_map = {"left": 1, "right": 2, "middle": 4}
            params["buttons"] = buttons_map.get(button, 1)
        await self.send("Input.dispatchMouseEvent", params)

    async def dispatch_key_event(
        self,
        event_type: str,
        key: str = "",
        code: str = "",
        text: str = "",
        modifiers: int = 0,
    ) -> None:
        """Dispatch a keyboard event."""
        params: Dict[str, Any] = {"type": event_type}
        if key:
            params["key"] = key
        if code:
            params["code"] = code
        if text:
            params["text"] = text
        if modifiers:
            params["modifiers"] = modifiers
        await self.send("Input.dispatchKeyEvent", params)

    async def dispatch_scroll(
        self, x: float, y: float, delta_x: float, delta_y: float
    ) -> None:
        """Dispatch a scroll (wheel) event."""
        await self.send("Input.dispatchMouseEvent", {
            "type": "mouseWheel",
            "x": x,
            "y": y,
            "deltaX": delta_x,
            "deltaY": delta_y,
        })

    async def insert_text(self, text: str) -> None:
        """Insert text at the current cursor position."""
        await self.send("Input.insertText", {"text": text})

    async def get_accessibility_tree(self) -> Optional[Dict[str, Any]]:
        """Get the full accessibility tree in Playwright-compatible format.

        CDP returns a flat array with parentId references. This method
        reconstructs it into a nested {role, name, value, children} tree
        matching the format that browser_tools.py expects.
        """
        try:
            result = await self.send("Accessibility.getFullAXTree")
            nodes = result.get("nodes", [])
            if not nodes:
                return None
            return self._build_a11y_tree(nodes)
        except Exception as e:
            logger.debug(f"Accessibility tree failed: {e}")
            return None

    def _build_a11y_tree(self, nodes: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Convert CDP flat accessibility nodes to nested tree format."""
        if not nodes:
            return None

        # Build lookup by nodeId
        by_id: Dict[str, Dict[str, Any]] = {}
        for node in nodes:
            node_id = node.get("nodeId", "")
            role = node.get("role", {}).get("value", "")
            name = node.get("name", {}).get("value", "")
            value = node.get("value", {}).get("value", "")

            tree_node: Dict[str, Any] = {"role": role}
            if name:
                tree_node["name"] = name
            if value:
                tree_node["value"] = value

            # Extract boolean properties
            properties = node.get("properties", [])
            for prop in properties:
                prop_name = prop.get("name", "")
                prop_value = prop.get("value", {}).get("value")
                if prop_name in ("checked", "disabled", "expanded", "selected", "required", "pressed"):
                    if prop_value is not None and prop_value is not False:
                        tree_node[prop_name] = prop_value

            tree_node["children"] = []
            tree_node["_child_ids"] = node.get("childIds", [])
            by_id[node_id] = tree_node

        # Build tree from parent-child relationships
        root = by_id.get(nodes[0].get("nodeId", ""))
        if not root:
            return None

        def resolve_children(tree_node: Dict[str, Any]) -> None:
            child_ids = tree_node.pop("_child_ids", [])
            for cid in child_ids:
                child = by_id.get(cid)
                if child:
                    resolve_children(child)
                    tree_node["children"].append(child)

        resolve_children(root)
        return root

    # ------------------------------------------------------------------
    # Tab management via Target domain
    # ------------------------------------------------------------------

    async def get_targets(self) -> List[Dict[str, Any]]:
        """Get all browser targets (tabs)."""
        result = await self.send("Target.getTargets")
        return result.get("targetInfos", [])

    async def create_target(self, url: str = "about:blank") -> str:
        """Create a new tab and return its target ID."""
        result = await self.send("Target.createTarget", {"url": url})
        return result.get("targetId", "")

    async def activate_target(self, target_id: str) -> None:
        """Activate (focus) a target/tab."""
        await self.send("Target.activateTarget", {"targetId": target_id})

    async def close_target(self, target_id: str) -> None:
        """Close a target/tab."""
        await self.send("Target.closeTarget", {"targetId": target_id})

    # ------------------------------------------------------------------
    # Screencast (for Windows fallback / CDP-based display)
    # ------------------------------------------------------------------

    async def start_screencast(
        self,
        format: str = "jpeg",
        quality: int = 60,
        max_width: int = 1280,
        max_height: int = 720,
        every_nth_frame: int = 1,
    ) -> None:
        """Start CDP page screencast."""
        await self.send("Page.startScreencast", {
            "format": format,
            "quality": quality,
            "maxWidth": max_width,
            "maxHeight": max_height,
            "everyNthFrame": every_nth_frame,
        })

    async def stop_screencast(self) -> None:
        """Stop CDP page screencast."""
        await self.send("Page.stopScreencast")

    async def ack_screencast_frame(self, session_id: int) -> None:
        """Acknowledge a screencast frame."""
        await self.send("Page.screencastFrameAck", {"sessionId": session_id})


# ------------------------------------------------------------------
# Utility: Discover targets from Chrome's HTTP debug endpoint
# ------------------------------------------------------------------

async def get_cdp_targets(host: str = "localhost", port: int = 9222) -> List[Dict[str, Any]]:
    """Query Chrome's /json endpoint to discover debuggable targets.

    Returns list of target dicts with fields like:
    - id: target ID
    - type: "page", "background_page", etc.
    - url: current URL
    - title: page title
    - webSocketDebuggerUrl: WebSocket URL for CDP connection
    """
    url = f"http://{host}:{port}/json"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            return await resp.json()


async def get_cdp_version(host: str = "localhost", port: int = 9222) -> Dict[str, Any]:
    """Query Chrome's /json/version endpoint for browser info.

    Returns dict with fields like:
    - Browser: browser name and version
    - webSocketDebuggerUrl: browser-level CDP WebSocket URL
    """
    url = f"http://{host}:{port}/json/version"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            return await resp.json()


async def connect_to_page(host: str = "localhost", port: int = 9222, page_index: int = 0) -> CDPClient:
    """Convenience: discover targets and connect to a page.

    Args:
        host: Chrome debug host
        port: Chrome debug port
        page_index: Index of the page target to connect to

    Returns:
        Connected CDPClient instance
    """
    targets = await get_cdp_targets(host, port)
    pages = [t for t in targets if t.get("type") == "page"]
    if not pages:
        raise CDPError("No page targets found")
    if page_index >= len(pages):
        raise CDPError(f"Page index {page_index} out of range (found {len(pages)} pages)")

    ws_url = pages[page_index].get("webSocketDebuggerUrl")
    if not ws_url:
        raise CDPError("Target has no webSocketDebuggerUrl")

    client = CDPClient(ws_url)
    await client.connect()
    return client
