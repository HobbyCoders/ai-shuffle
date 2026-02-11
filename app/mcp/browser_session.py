"""
Browser Session Manager for AI agent web browsing.

Manages Playwright browser instances per chat session, providing
isolated browser contexts with automatic cleanup.
"""

import asyncio
import base64
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    Playwright,
)

logger = logging.getLogger(__name__)

# Default browser configuration
DEFAULT_VIEWPORT = {"width": 1280, "height": 720}
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
SESSION_IDLE_TIMEOUT = 300  # 5 minutes


@dataclass
class BrowserSession:
    """A browser session tied to a chat session."""

    session_id: str
    browser: Browser
    context: BrowserContext
    page: Page
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    _pages: list = field(default_factory=list)

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
            # Use a sync-safe approach
            return ""  # Title fetched async in tools
        except Exception:
            return ""

    async def get_title(self) -> str:
        try:
            return await self.page.title()
        except Exception:
            return ""

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
        """Click at the given coordinates."""
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

    async def close(self):
        """Close this browser session."""
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

        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )

        context = await browser.new_context(
            viewport=DEFAULT_VIEWPORT,
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
