"""
Chrome Session Manager — manages real Chrome instances per chat session.

Replaces the Playwright-based BrowserSessionManager with direct Chrome
process management + CDP client connections.

Supports two display modes:
- Headless + CDP screencast (Windows, or when VNC stack is unavailable)
- Xvfb + VNC + noVNC (Linux/Docker — real Chrome window in browser)

Session profiles persist on disk for cookie/localStorage survival across restarts.
"""

import asyncio
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional
import subprocess

from app.mcp.cdp_client import CDPClient, CDPError, get_cdp_targets, connect_to_page, get_cdp_version
from app.mcp.chrome_launcher import (
    find_chrome,
    launch_chrome_headless,
    launch_chrome_xvfb,
    start_xvfb,
    start_vnc_server,
    start_websockify,
    kill_process_tree,
    wait_for_cdp_ready,
    cdp_ports,
    vnc_ports,
    novnc_ports,
    displays,
    LaunchError,
)

logger = logging.getLogger(__name__)

# Session config
SESSION_IDLE_TIMEOUT = 300  # 5 minutes
MAX_SESSIONS = 5
INITIAL_VIEWPORT = {"width": 1280, "height": 720}

# Display mode
DISPLAY_MODE_HEADLESS = "headless"   # CDP screencast (Windows, fallback)
DISPLAY_MODE_VNC = "vnc"             # Xvfb + VNC + noVNC (Linux/Docker)


def _detect_display_mode() -> str:
    """Detect which display mode to use based on platform."""
    if sys.platform == "win32" or sys.platform == "darwin":
        return DISPLAY_MODE_HEADLESS

    # On Linux, check if Xvfb and x11vnc are available
    import shutil
    has_xvfb = shutil.which("Xvfb") is not None
    has_vnc = shutil.which("x11vnc") is not None
    has_websockify = shutil.which("websockify") is not None

    if has_xvfb and has_vnc and has_websockify:
        return DISPLAY_MODE_VNC

    logger.info("VNC stack not available, falling back to headless mode")
    return DISPLAY_MODE_HEADLESS


@dataclass
class ChromeSession:
    """A browser session tied to a chat session.

    Wraps a real Chrome process with CDP client access for AI tools,
    and optionally VNC for user display.
    """
    session_id: str
    user_data_dir: Path
    display_mode: str  # "headless" or "vnc"
    cdp_port: int
    cdp_client: Optional[CDPClient] = field(default=None, repr=False)

    # Process handles
    chrome_process: Optional[subprocess.Popen] = field(default=None, repr=False)
    xvfb_process: Optional[subprocess.Popen] = field(default=None, repr=False)
    vnc_process: Optional[subprocess.Popen] = field(default=None, repr=False)
    novnc_process: Optional[subprocess.Popen] = field(default=None, repr=False)

    # VNC/noVNC info
    display_num: Optional[int] = None
    vnc_port: Optional[int] = None
    novnc_port: Optional[int] = None

    # Timing
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)

    # CDP state
    _cdp_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _screencast_active: bool = False
    _screencast_callback: Optional[Callable[[bytes], Awaitable[None]]] = field(
        default=None, repr=False
    )

    # Tab tracking
    _active_target_id: Optional[str] = None
    _pages: List[Dict[str, Any]] = field(default_factory=list)

    # Viewport
    _viewport_width: int = field(default=INITIAL_VIEWPORT["width"])
    _viewport_height: int = field(default=INITIAL_VIEWPORT["height"])

    def touch(self):
        """Update last activity timestamp."""
        self.last_activity = time.time()

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.last_activity) > SESSION_IDLE_TIMEOUT

    @property
    def current_url(self) -> str:
        """Get current page URL (sync, returns cached or empty)."""
        return ""  # Must call async get_url()

    async def get_url(self) -> str:
        if not self.cdp_client or not self.cdp_client.connected:
            return ""
        try:
            return await self.cdp_client.get_url()
        except Exception:
            return ""

    async def get_title(self) -> str:
        if not self.cdp_client or not self.cdp_client.connected:
            return ""
        try:
            return await self.cdp_client.get_title()
        except Exception:
            return ""

    async def take_screenshot(self, full_page: bool = False) -> str:
        """Take a screenshot and return as base64 JPEG string."""
        if not self.cdp_client or not self.cdp_client.connected:
            return ""
        try:
            return await self.cdp_client.screenshot(
                format="jpeg", quality=75, full_page=full_page
            )
        except Exception as e:
            logger.error(f"Screenshot failed for session {self.session_id}: {e}")
            return ""

    async def get_accessibility_tree(self, interesting_only: bool = True) -> Optional[dict]:
        """Get the accessibility tree for AI consumption."""
        self.touch()
        if not self.cdp_client or not self.cdp_client.connected:
            return None
        try:
            return await self.cdp_client.get_accessibility_tree()
        except Exception as e:
            logger.error(f"Accessibility tree failed for session {self.session_id}: {e}")
            return None

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    async def navigate(self, url: str) -> None:
        """Navigate to a URL."""
        self.touch()
        if not self.cdp_client:
            raise CDPError("No CDP client connected")
        if url and not url.startswith(("http://", "https://", "about:", "data:")):
            url = "https://" + url
        await self.cdp_client.navigate(url)
        # Wait for load
        await asyncio.sleep(1.0)

    async def go_back(self) -> None:
        self.touch()
        if self.cdp_client:
            await self.cdp_client.evaluate("history.back()")
            await asyncio.sleep(0.5)

    async def go_forward(self) -> None:
        self.touch()
        if self.cdp_client:
            await self.cdp_client.evaluate("history.forward()")
            await asyncio.sleep(0.5)

    async def reload(self) -> None:
        self.touch()
        if self.cdp_client:
            await self.cdp_client.reload()
            await asyncio.sleep(0.5)

    # ------------------------------------------------------------------
    # JavaScript evaluation
    # ------------------------------------------------------------------

    async def evaluate(self, expression: str) -> Any:
        """Evaluate JavaScript and return result."""
        self.touch()
        if not self.cdp_client:
            raise CDPError("No CDP client connected")
        return await self.cdp_client.evaluate(expression)

    # ------------------------------------------------------------------
    # Input dispatch
    # ------------------------------------------------------------------

    async def cdp_mouse_event(
        self, event_type: str, x: float, y: float,
        button: str = "left", click_count: int = 1
    ) -> None:
        self.touch()
        if not self.cdp_client:
            return
        async with self._cdp_lock:
            await self.cdp_client.dispatch_mouse_event(
                event_type, x, y, button, click_count
            )

    async def cdp_key_event(
        self, event_type: str, key: str = "", code: str = "",
        text: str = "", modifiers: int = 0
    ) -> None:
        self.touch()
        if not self.cdp_client:
            return
        async with self._cdp_lock:
            await self.cdp_client.dispatch_key_event(
                event_type, key, code, text, modifiers
            )

    async def cdp_scroll(
        self, x: float, y: float, delta_x: float, delta_y: float
    ) -> None:
        self.touch()
        if not self.cdp_client:
            return
        async with self._cdp_lock:
            await self.cdp_client.dispatch_scroll(x, y, delta_x, delta_y)

    async def click_at(self, x: int, y: int) -> None:
        """Click at coordinates."""
        self.touch()
        if not self.cdp_client:
            return
        await self.cdp_client.dispatch_mouse_event("mousePressed", x, y)
        await self.cdp_client.dispatch_mouse_event("mouseReleased", x, y)
        await asyncio.sleep(0.3)

    async def type_text(self, text: str, selector: Optional[str] = None) -> None:
        """Type text into focused element or selector."""
        self.touch()
        if not self.cdp_client:
            return
        if selector:
            # Focus the element first, then insert text
            await self.cdp_client.evaluate(f"""
                (() => {{
                    const el = document.querySelector({repr(selector)});
                    if (el) {{ el.focus(); el.value = ''; }}
                }})()
            """)
            await self.cdp_client.insert_text(text)
        else:
            await self.cdp_client.insert_text(text)

    # ------------------------------------------------------------------
    # Viewport
    # ------------------------------------------------------------------

    async def set_viewport(self, width: int, height: int) -> None:
        """Resize the browser viewport."""
        self.touch()
        width = max(320, min(3840, width))
        height = max(240, min(2160, height))
        self._viewport_width = width
        self._viewport_height = height

        if self.cdp_client and self.cdp_client.connected:
            try:
                await self.cdp_client.send("Emulation.setDeviceMetricsOverride", {
                    "width": width,
                    "height": height,
                    "deviceScaleFactor": 1,
                    "mobile": False,
                })
            except Exception as e:
                logger.debug(f"Viewport resize failed: {e}")

    # ------------------------------------------------------------------
    # Screencast (headless mode display for frontend)
    # ------------------------------------------------------------------

    async def start_screencast(
        self,
        width: int,
        height: int,
        quality: int = 60,
        every_nth_frame: int = 1,
        callback: Optional[Callable[[bytes], Awaitable[None]]] = None,
    ) -> None:
        """Start CDP screencast for live frame streaming."""
        self.touch()

        if self._screencast_active:
            await self.stop_screencast()

        await self.set_viewport(width, height)

        if not self.cdp_client or not self.cdp_client.connected:
            return

        self._screencast_callback = callback

        import base64

        def on_frame(params: dict) -> None:
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
                    except Exception:
                        pass
                    try:
                        await self.cdp_client.ack_screencast_frame(session_id)
                    except Exception:
                        pass

                asyncio.create_task(deliver_and_ack())

        self.cdp_client.on("Page.screencastFrame", on_frame)
        self._screencast_handler = on_frame

        await self.cdp_client.start_screencast(
            format="jpeg",
            quality=quality,
            max_width=width,
            max_height=height,
            every_nth_frame=every_nth_frame,
        )
        self._screencast_active = True

    async def stop_screencast(self) -> None:
        """Stop the CDP screencast."""
        if not self._screencast_active:
            return

        self._screencast_active = False
        self._screencast_callback = None

        if self.cdp_client and self.cdp_client.connected:
            try:
                await self.cdp_client.stop_screencast()
            except Exception:
                pass

            handler = getattr(self, "_screencast_handler", None)
            if handler:
                self.cdp_client.off("Page.screencastFrame", handler)
                self._screencast_handler = None

    # ------------------------------------------------------------------
    # Tab management
    # ------------------------------------------------------------------

    async def get_tabs(self) -> List[Dict[str, Any]]:
        """List all page tabs."""
        if not self.cdp_client:
            return []
        try:
            targets = await get_cdp_targets(port=self.cdp_port)
            return [t for t in targets if t.get("type") == "page"]
        except Exception:
            return []

    async def new_tab(self, url: str = "about:blank") -> Optional[str]:
        """Open a new tab and switch to it."""
        if not self.cdp_client:
            return None
        try:
            # Create new target via browser-level CDP
            version = await get_cdp_version(port=self.cdp_port)
            browser_ws = version.get("webSocketDebuggerUrl")
            if not browser_ws:
                return None

            # Use the current CDP client to create a target
            # Note: Target.createTarget works on page-level CDP too
            target_id = await self.cdp_client.create_target(url)
            if target_id:
                await self.switch_to_target(target_id)
            return target_id
        except Exception as e:
            logger.error(f"New tab failed: {e}")
            return None

    async def switch_to_target(self, target_id: str) -> None:
        """Switch CDP client to a different page target."""
        if not self.cdp_client:
            return

        # Close current CDP connection
        await self.cdp_client.close()

        # Find the new target's WebSocket URL
        targets = await get_cdp_targets(port=self.cdp_port)
        for t in targets:
            if t.get("id") == target_id:
                ws_url = t.get("webSocketDebuggerUrl")
                if ws_url:
                    self.cdp_client = CDPClient(ws_url)
                    await self.cdp_client.connect()
                    self._active_target_id = target_id
                    return

        raise CDPError(f"Target {target_id} not found")

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Close this browser session, cleaning up all resources."""
        await self.stop_screencast()

        # Close CDP client
        if self.cdp_client:
            try:
                await self.cdp_client.close()
            except Exception:
                pass
            self.cdp_client = None

        # Kill processes
        for proc_name in ("novnc_process", "vnc_process", "chrome_process", "xvfb_process"):
            proc = getattr(self, proc_name, None)
            if proc:
                kill_process_tree(proc)
                setattr(self, proc_name, None)

        # Release allocated ports/displays
        await cdp_ports.release(self.cdp_port)
        if self.vnc_port:
            await vnc_ports.release(self.vnc_port)
        if self.novnc_port:
            await novnc_ports.release(self.novnc_port)
        if self.display_num:
            await displays.release(self.display_num)

        logger.info(f"Closed Chrome session: {self.session_id}")


class ChromeSessionManager:
    """Manages Chrome sessions for chat sessions.

    Replaces BrowserSessionManager with real Chrome instances.
    """

    def __init__(self, profile_base_dir: Optional[Path] = None, max_sessions: int = MAX_SESSIONS):
        self._sessions: Dict[str, ChromeSession] = {}
        self._lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None
        self._chrome_path: Optional[str] = None
        self._display_mode: Optional[str] = None
        self._max_sessions = max_sessions
        self._profile_base_dir = profile_base_dir

    @property
    def display_mode(self) -> str:
        if self._display_mode is None:
            self._display_mode = _detect_display_mode()
        return self._display_mode

    @property
    def profile_dir(self) -> Path:
        if self._profile_base_dir:
            return self._profile_base_dir
        from app.core.config import settings
        return settings.effective_data_dir / "browser_profiles"

    def _get_chrome(self) -> str:
        """Find and cache Chrome path."""
        if self._chrome_path is None:
            self._chrome_path = find_chrome()
            if not self._chrome_path:
                raise LaunchError(
                    "Chrome/Chromium not found. Install Chrome or set it in PATH."
                )
        return self._chrome_path

    async def get_or_create_session(self, session_id: str) -> ChromeSession:
        """Get existing session or create a new one."""
        async with self._lock:
            if session_id in self._sessions:
                session = self._sessions[session_id]
                session.touch()
                return session

            # Check max sessions
            if len(self._sessions) >= self._max_sessions:
                # Close oldest idle session
                oldest = min(
                    self._sessions.values(),
                    key=lambda s: s.last_activity
                )
                logger.info(f"Max sessions reached, closing oldest: {oldest.session_id}")
                await self._close_session_unlocked(oldest.session_id)

            return await self._create_session(session_id)

    async def _create_session(self, session_id: str) -> ChromeSession:
        """Create a new Chrome session."""
        chrome_path = self._get_chrome()
        mode = self.display_mode

        # Create user data directory for session isolation
        user_data_dir = self.profile_dir / session_id
        user_data_dir.mkdir(parents=True, exist_ok=True)

        # Allocate CDP port
        cdp_port = await cdp_ports.allocate()

        session = ChromeSession(
            session_id=session_id,
            user_data_dir=user_data_dir,
            display_mode=mode,
            cdp_port=cdp_port,
        )

        try:
            if mode == DISPLAY_MODE_VNC:
                await self._launch_vnc_session(session, chrome_path)
            else:
                await self._launch_headless_session(session, chrome_path)

            # Wait for Chrome CDP to be ready
            ready = await wait_for_cdp_ready(port=cdp_port, timeout=15.0)
            if not ready:
                raise LaunchError(f"Chrome CDP not ready on port {cdp_port} after 15s")

            # Connect CDP client to the first page
            session.cdp_client = await connect_to_page(port=cdp_port)

            # Enable required CDP domains
            await session.cdp_client.send("Page.enable")
            await session.cdp_client.send("Runtime.enable")
            try:
                await session.cdp_client.send("Accessibility.enable")
            except Exception:
                pass  # Not all Chrome versions support this

        except Exception as e:
            # Cleanup on failure
            await session.close()
            raise LaunchError(f"Failed to create Chrome session: {e}") from e

        self._sessions[session_id] = session

        # Start cleanup loop if not running
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

        logger.info(
            f"Created Chrome session: {session_id} "
            f"(mode={mode}, cdp_port={cdp_port})"
        )
        return session

    async def _launch_headless_session(
        self, session: ChromeSession, chrome_path: str
    ) -> None:
        """Launch Chrome in headless mode with CDP screencast."""
        session.chrome_process = await launch_chrome_headless(
            chrome_path=chrome_path,
            user_data_dir=str(session.user_data_dir),
            cdp_port=session.cdp_port,
        )

    async def _launch_vnc_session(
        self, session: ChromeSession, chrome_path: str
    ) -> None:
        """Launch full VNC stack: Xvfb → Chrome → x11vnc → websockify."""
        # Allocate resources
        session.display_num = await displays.allocate()
        session.vnc_port = await vnc_ports.allocate()
        session.novnc_port = await novnc_ports.allocate()

        # 1. Start Xvfb
        session.xvfb_process = await start_xvfb(session.display_num)

        # 2. Launch Chrome on the virtual display
        session.chrome_process = await launch_chrome_xvfb(
            chrome_path=chrome_path,
            user_data_dir=str(session.user_data_dir),
            cdp_port=session.cdp_port,
            display_num=session.display_num,
        )

        # 3. Start VNC server
        session.vnc_process = await start_vnc_server(
            session.display_num, session.vnc_port
        )

        # 4. Start websockify (noVNC bridge)
        session.novnc_process = await start_websockify(
            session.novnc_port, session.vnc_port
        )

    async def get_session(self, session_id: str) -> Optional[ChromeSession]:
        """Get an existing session without creating one."""
        session = self._sessions.get(session_id)
        if session:
            session.touch()
        return session

    async def close_session(self, session_id: str) -> None:
        """Close and remove a browser session."""
        async with self._lock:
            await self._close_session_unlocked(session_id)

    async def _close_session_unlocked(self, session_id: str) -> None:
        """Close session without holding the lock (caller must hold it)."""
        session = self._sessions.pop(session_id, None)
        if session:
            await session.close()

    async def close_all(self) -> None:
        """Close all sessions and cleanup."""
        async with self._lock:
            for session_id, session in list(self._sessions.items()):
                await session.close()
            self._sessions.clear()

            if self._cleanup_task:
                self._cleanup_task.cancel()
                self._cleanup_task = None

    async def _cleanup_loop(self) -> None:
        """Periodically clean up idle sessions."""
        while True:
            try:
                await asyncio.sleep(60)
                async with self._lock:
                    expired = [
                        sid for sid, session in self._sessions.items()
                        if session.is_expired
                    ]
                    for sid in expired:
                        session = self._sessions.pop(sid)
                        await session.close()
                        logger.info(f"Cleaned up expired Chrome session: {sid}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in Chrome cleanup loop: {e}")

    def has_session(self, session_id: str) -> bool:
        return session_id in self._sessions

    def get_session_state(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get session state for API responses."""
        session = self._sessions.get(session_id)
        if not session:
            return None
        return {
            "url": "",  # Must use async get_url()
            "is_active": True,
            "display_mode": session.display_mode,
            "novnc_port": session.novnc_port,
            "cdp_port": session.cdp_port,
            "created_at": session.created_at,
            "last_activity": session.last_activity,
        }

    def get_session_display_info(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get display connection info for the frontend."""
        session = self._sessions.get(session_id)
        if not session:
            return None

        if session.display_mode == DISPLAY_MODE_VNC and session.novnc_port:
            return {
                "mode": "novnc",
                "novnc_port": session.novnc_port,
                "vnc_port": session.vnc_port,
            }
        else:
            return {
                "mode": "screencast",
                "cdp_port": session.cdp_port,
            }


# Singleton instance
chrome_session_manager = ChromeSessionManager()
