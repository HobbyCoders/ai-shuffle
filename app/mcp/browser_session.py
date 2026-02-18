"""
Browser Session Manager for AI agent web browsing.

Manages Playwright browser instances per chat session, providing
isolated browser contexts with automatic cleanup.

Supports two rendering modes:
- CDP Screencast: Live frame streaming via Chrome DevTools Protocol
- Legacy Screenshots: One-shot JPEG captures (kept for browser_screenshot tool)

The CDP screencast mode enables dynamic viewport sizing (matching the
frontend panel dimensions), real-time frame delivery over WebSocket,
and low-latency input dispatch — replacing the old screenshot-poll loop.
"""

import asyncio
import base64
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    CDPSession,
    Page,
    Playwright,
)

logger = logging.getLogger(__name__)

# Initial viewport — overridden when a screencast client connects with panel dimensions
INITIAL_VIEWPORT = {"width": 1280, "height": 720}
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
SESSION_IDLE_TIMEOUT = 300  # 5 minutes


@dataclass
class BrowserSession:
    """A browser session tied to a chat session.

    Wraps a Playwright browser with CDP-level access for screencast
    streaming, accessibility tree extraction, and direct input dispatch.
    """

    session_id: str
    browser: Browser
    context: BrowserContext
    page: Page
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    _pages: list = field(default_factory=list)

    # CDP screencast state
    _cdp_session: Optional[CDPSession] = field(default=None, repr=False)
    _screencast_active: bool = field(default=False)
    _screencast_callback: Optional[Callable[[bytes], Awaitable[None]]] = field(
        default=None, repr=False
    )
    _screencast_handler: Optional[Callable] = field(default=None, repr=False)
    _frame_tasks: set = field(default_factory=set, repr=False)
    _viewport_width: int = field(default=INITIAL_VIEWPORT["width"])
    _viewport_height: int = field(default=INITIAL_VIEWPORT["height"])
    # Asyncio lock to serialize CDP operations between WS input and MCP tools
    _cdp_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def touch(self):
        """Update last activity timestamp."""
        self.last_activity = time.time()

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.last_activity) > SESSION_IDLE_TIMEOUT

    @property
    def current_url(self) -> str:
        try:
            return self.page.url
        except Exception:
            return ""

    @property
    def current_title(self) -> str:
        try:
            return ""  # Title fetched async in tools
        except Exception:
            return ""

    async def get_title(self) -> str:
        try:
            return await self.page.title()
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Legacy screenshot (kept for browser_screenshot tool / fallback)
    # ------------------------------------------------------------------

    async def take_screenshot(self, full_page: bool = False) -> str:
        """Take a screenshot and return as base64 JPEG string."""
        try:
            screenshot_bytes = await self.page.screenshot(
                type="jpeg",
                quality=75,
                full_page=full_page,
            )
            return base64.b64encode(screenshot_bytes).decode("utf-8")
        except Exception as e:
            logger.error(f"Screenshot failed for session {self.session_id}: {e}")
            return ""

    # ------------------------------------------------------------------
    # CDP session management
    # ------------------------------------------------------------------

    async def _ensure_cdp_session(self) -> CDPSession:
        """Get or create a CDP session for the current page.

        Recreates the session if the current page changed (e.g., after tab switch),
        because a CDP session is bound to the page target it was created for.
        """
        if self._cdp_session is not None:
            # Check if the CDP session's page target still matches self.page.
            # After browser_tab_switch, self.page changes but the cached CDP
            # session still points at the old page. Detect this and recreate.
            try:
                # Attempt a lightweight CDP call to verify the session is alive
                await self._cdp_session.send("Runtime.evaluate", {"expression": "1"})
            except Exception:
                # Session is stale or detached — recreate
                try:
                    await self._cdp_session.detach()
                except Exception:
                    pass
                self._cdp_session = None
                self._screencast_handler = None

        if self._cdp_session is None:
            self._cdp_session = await self.page.context.new_cdp_session(self.page)
        return self._cdp_session

    # ------------------------------------------------------------------
    # Screencast — live frame streaming via CDP
    # ------------------------------------------------------------------

    async def start_screencast(
        self,
        width: int,
        height: int,
        quality: int = 60,
        every_nth_frame: int = 1,
        callback: Optional[Callable[[bytes], Awaitable[None]]] = None,
    ) -> None:
        """Start CDP screencast, streaming JPEG frames to the callback.

        Args:
            width: Target frame width (should match panel dimensions)
            height: Target frame height (should match panel dimensions)
            quality: JPEG quality (0-100). Lower = smaller frames, faster delivery.
            every_nth_frame: Send every Nth frame (1 = all frames, ~60fps base).
            callback: Async function receiving raw JPEG bytes for each frame.
        """
        self.touch()

        # Stop any existing screencast first
        if self._screencast_active:
            await self.stop_screencast()

        # Resize viewport to match panel dimensions
        await self.set_viewport(width, height)

        cdp = await self._ensure_cdp_session()
        self._screencast_callback = callback

        # Remove any previously registered handler to avoid accumulating
        # duplicate handlers across start/stop/start cycles (e.g., resizes).
        if self._screencast_handler is not None:
            cdp.off("Page.screencastFrame", self._screencast_handler)
            self._screencast_handler = None

        # Frame handler: decode base64 → invoke callback → ack frame
        def on_frame(params: dict) -> None:
            # Capture callback ref locally to avoid TOCTOU race with stop_screencast
            cb = self._screencast_callback
            if not self._screencast_active or cb is None:
                return

            frame_b64 = params.get("data", "")
            session_id = params.get("metadata", {}).get("sessionId", 0)

            if frame_b64:
                frame_bytes = base64.b64decode(frame_b64)

                async def deliver_and_ack():
                    try:
                        await cb(frame_bytes)
                    except Exception as e:
                        logger.debug(f"Frame delivery failed: {e}")
                    try:
                        await cdp.send(
                            "Page.screencastFrameAck",
                            {"sessionId": session_id},
                        )
                    except Exception:
                        pass

                task = asyncio.create_task(deliver_and_ack())
                self._frame_tasks.add(task)
                task.add_done_callback(self._frame_tasks.discard)

        self._screencast_handler = on_frame
        cdp.on("Page.screencastFrame", on_frame)

        await cdp.send(
            "Page.startScreencast",
            {
                "format": "jpeg",
                "quality": quality,
                "maxWidth": width,
                "maxHeight": height,
                "everyNthFrame": every_nth_frame,
            },
        )
        self._screencast_active = True
        logger.debug(
            f"Started screencast for {self.session_id}: "
            f"{width}x{height} q={quality} every={every_nth_frame}"
        )

    async def stop_screencast(self) -> None:
        """Stop the CDP screencast if active."""
        if self._cdp_session and self._screencast_active:
            # Set flags first to prevent any in-flight on_frame from invoking callback
            self._screencast_active = False
            self._screencast_callback = None
            # Cancel outstanding frame delivery tasks
            for task in list(self._frame_tasks):
                task.cancel()
            self._frame_tasks.clear()
            try:
                await self._cdp_session.send("Page.stopScreencast", {})
            except Exception:
                pass
            # Remove the event handler to prevent accumulation
            if self._screencast_handler is not None:
                try:
                    self._cdp_session.off("Page.screencastFrame", self._screencast_handler)
                except Exception:
                    pass
                self._screencast_handler = None
            logger.debug(f"Stopped screencast for {self.session_id}")

    # ------------------------------------------------------------------
    # Dynamic viewport
    # ------------------------------------------------------------------

    async def set_viewport(self, width: int, height: int) -> None:
        """Resize the browser viewport to match the panel dimensions."""
        self.touch()
        # Clamp to reasonable bounds
        width = max(320, min(3840, width))
        height = max(240, min(2160, height))
        self._viewport_width = width
        self._viewport_height = height
        await self.page.set_viewport_size({"width": width, "height": height})

    # ------------------------------------------------------------------
    # Accessibility tree — semantic page understanding for AI
    # ------------------------------------------------------------------

    async def get_accessibility_tree(
        self, interesting_only: bool = True
    ) -> Optional[dict]:
        """Get the accessibility tree snapshot for AI consumption.

        Returns a structured dict with roles, names, values, and children
        representing the semantic page structure. This is orders of magnitude
        smaller than a screenshot and more useful for AI understanding.

        Args:
            interesting_only: If True, only return "interesting" nodes
                (those with a name, role, or value). Filters noise.
        """
        self.touch()
        try:
            snapshot = await self.page.accessibility.snapshot(
                interesting_only=interesting_only
            )
            return snapshot
        except Exception as e:
            logger.error(
                f"Accessibility tree failed for session {self.session_id}: {e}"
            )
            return None

    # ------------------------------------------------------------------
    # CDP input dispatch — low-latency mouse/keyboard/scroll
    # ------------------------------------------------------------------

    async def cdp_mouse_event(
        self,
        event_type: str,
        x: float,
        y: float,
        button: str = "left",
        click_count: int = 1,
    ) -> None:
        """Dispatch a mouse event via CDP for minimal-latency interaction.

        Args:
            event_type: "mousePressed", "mouseReleased", "mouseMoved"
            x: X coordinate in viewport space
            y: Y coordinate in viewport space
            button: "left", "right", or "middle"
            click_count: Number of clicks (1 for single, 2 for double)
        """
        self.touch()
        async with self._cdp_lock:
            cdp = await self._ensure_cdp_session()
            params: Dict[str, Any] = {
                "type": event_type,
                "x": x,
                "y": y,
                "button": button,
                "clickCount": click_count,
            }
            # mousePressed/mouseReleased require buttons bitmask
            if event_type in ("mousePressed", "mouseReleased"):
                buttons_map = {"left": 1, "right": 2, "middle": 4}
                params["buttons"] = buttons_map.get(button, 1)
            await cdp.send("Input.dispatchMouseEvent", params)

    async def cdp_key_event(
        self,
        event_type: str,
        key: str = "",
        code: str = "",
        text: str = "",
        modifiers: int = 0,
    ) -> None:
        """Dispatch a keyboard event via CDP for minimal-latency input.

        Args:
            event_type: "keyDown", "keyUp", or "char"
            key: Key value (e.g., "a", "Enter", "Backspace")
            code: Physical key code (e.g., "KeyA", "Enter")
            text: Text generated by the key (for "char" events)
            modifiers: Bitmask — 1=Alt, 2=Ctrl, 4=Meta, 8=Shift
        """
        self.touch()
        async with self._cdp_lock:
            cdp = await self._ensure_cdp_session()
            params: Dict[str, Any] = {"type": event_type}
            if key:
                params["key"] = key
            if code:
                params["code"] = code
            if text:
                params["text"] = text
            if modifiers:
                params["modifiers"] = modifiers
            await cdp.send("Input.dispatchKeyEvent", params)

    async def cdp_scroll(
        self, x: float, y: float, delta_x: float, delta_y: float
    ) -> None:
        """Dispatch a scroll (wheel) event via CDP.

        Args:
            x: X coordinate of the scroll location
            y: Y coordinate of the scroll location
            delta_x: Horizontal scroll delta
            delta_y: Vertical scroll delta
        """
        self.touch()
        async with self._cdp_lock:
            cdp = await self._ensure_cdp_session()
            await cdp.send(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseWheel",
                    "x": x,
                    "y": y,
                    "deltaX": delta_x,
                    "deltaY": delta_y,
                },
            )

    # ------------------------------------------------------------------
    # Navigation helpers (unchanged from original)
    # ------------------------------------------------------------------

    async def navigate(self, url: str) -> None:
        """Navigate to a URL."""
        self.touch()
        if url and not url.startswith(("http://", "https://", "about:", "data:")):
            url = "https://" + url
        await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)

    async def go_back(self) -> None:
        """Navigate back in history."""
        self.touch()
        await self.page.go_back(wait_until="domcontentloaded", timeout=15000)

    async def go_forward(self) -> None:
        """Navigate forward in history."""
        self.touch()
        await self.page.go_forward(wait_until="domcontentloaded", timeout=15000)

    async def reload(self) -> None:
        """Reload the current page."""
        self.touch()
        await self.page.reload(wait_until="domcontentloaded", timeout=15000)

    async def click_at(self, x: int, y: int) -> None:
        """Click at the given coordinates (Playwright-level, used by tools)."""
        self.touch()
        await self.page.mouse.click(x, y)
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=3000)
        except Exception:
            pass

    async def type_text(self, text: str, selector: Optional[str] = None) -> None:
        """Type text into the focused element or a specific selector."""
        self.touch()
        if selector:
            await self.page.fill(selector, text)
        else:
            await self.page.keyboard.type(text)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def close(self):
        """Close this browser session, cleaning up CDP and Playwright resources."""
        # Stop screencast first (also removes handler)
        await self.stop_screencast()
        self._screencast_handler = None
        # Detach CDP session
        if self._cdp_session:
            try:
                await self._cdp_session.detach()
            except Exception:
                pass
            self._cdp_session = None
        # Close Playwright resources
        try:
            await self.context.close()
        except Exception:
            pass
        try:
            await self.browser.close()
        except Exception:
            pass


class BrowserSessionManager:
    """Manages browser sessions for chat sessions."""

    def __init__(self):
        self._sessions: Dict[str, BrowserSession] = {}
        self._playwright: Optional[Playwright] = None
        self._lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None

    async def _ensure_playwright(self) -> Playwright:
        """Lazily initialize Playwright."""
        if self._playwright is None:
            self._playwright = await async_playwright().start()
            # Start cleanup task
            if self._cleanup_task is None:
                self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        return self._playwright

    async def get_or_create_session(self, session_id: str) -> BrowserSession:
        """Get existing session or create a new one."""
        async with self._lock:
            if session_id in self._sessions:
                session = self._sessions[session_id]
                session.touch()
                return session

            return await self._create_session(session_id)

    async def _create_session(self, session_id: str) -> BrowserSession:
        """Create a new browser session."""
        pw = await self._ensure_playwright()

        try:
            browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )
        except Exception as e:
            if "Executable doesn't exist" in str(e):
                msg = (
                    "Playwright Chromium browser is not installed. "
                    "Run: playwright install chromium"
                )
                logger.error(msg)
                raise RuntimeError(msg) from e
            raise

        context = await browser.new_context(
            viewport=INITIAL_VIEWPORT,
            user_agent=DEFAULT_USER_AGENT,
            ignore_https_errors=True,
        )

        page = await context.new_page()

        session = BrowserSession(
            session_id=session_id,
            browser=browser,
            context=context,
            page=page,
        )
        session._pages = [page]

        self._sessions[session_id] = session
        logger.info(f"Created browser session: {session_id}")
        return session

    async def get_session(self, session_id: str) -> Optional[BrowserSession]:
        """Get an existing session without creating one."""
        session = self._sessions.get(session_id)
        if session:
            session.touch()
        return session

    async def close_session(self, session_id: str):
        """Close and remove a browser session."""
        async with self._lock:
            session = self._sessions.pop(session_id, None)
            if session:
                await session.close()
                logger.info(f"Closed browser session: {session_id}")

    async def close_all(self):
        """Close all browser sessions and Playwright."""
        async with self._lock:
            for session_id, session in list(self._sessions.items()):
                await session.close()
                logger.info(f"Closed browser session: {session_id}")
            self._sessions.clear()

            if self._cleanup_task:
                self._cleanup_task.cancel()
                self._cleanup_task = None

            if self._playwright:
                await self._playwright.stop()
                self._playwright = None

    async def _cleanup_loop(self):
        """Periodically clean up idle sessions."""
        while True:
            try:
                await asyncio.sleep(60)  # Check every minute
                async with self._lock:
                    expired = [
                        sid
                        for sid, session in self._sessions.items()
                        if session.is_expired
                    ]
                    for sid in expired:
                        session = self._sessions.pop(sid)
                        await session.close()
                        logger.info(f"Cleaned up expired browser session: {sid}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in browser cleanup loop: {e}")

    def has_session(self, session_id: str) -> bool:
        """Check if a session exists."""
        return session_id in self._sessions

    def get_session_state(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get session state for API responses."""
        session = self._sessions.get(session_id)
        if not session:
            return None
        return {
            "url": session.current_url,
            "is_active": True,
            "created_at": session.created_at,
            "last_activity": session.last_activity,
        }


# Singleton instance
browser_session_manager = BrowserSessionManager()
