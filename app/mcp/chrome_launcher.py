"""
Platform-aware Chrome/Chromium launcher.

Finds Chrome on the system and launches instances with appropriate flags
for browser automation. Handles both Windows (native display) and
Linux/Docker (Xvfb virtual display + VNC) modes.
"""

import asyncio
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# Port ranges for allocation
CDP_PORT_START = 9222
CDP_PORT_END = 9322  # Max 100 concurrent sessions
VNC_PORT_START = 5900
VNC_PORT_END = 5999
NOVNC_PORT_START = 6080
NOVNC_PORT_END = 6179
DISPLAY_START = 100
DISPLAY_END = 199


class LaunchError(Exception):
    """Error launching Chrome or supporting processes."""
    pass


def find_chrome() -> Optional[str]:
    """Find Chrome/Chromium binary on the system.

    Checks common locations based on platform.

    Returns:
        Path to Chrome executable, or None if not found
    """
    # Try PATH first
    for name in ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"]:
        path = shutil.which(name)
        if path:
            return path

    if sys.platform == "win32":
        # Windows-specific locations
        candidates = []
        for env_var in ["PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"]:
            base = os.environ.get(env_var, "")
            if base:
                candidates.extend([
                    Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe",
                    Path(base) / "Chromium" / "Application" / "chrome.exe",
                ])
        # Also check user-level Chrome install
        localappdata = os.environ.get("LOCALAPPDATA", "")
        if localappdata:
            candidates.append(
                Path(localappdata) / "Google" / "Chrome" / "Application" / "chrome.exe"
            )

        for p in candidates:
            if p.exists():
                return str(p)

    elif sys.platform == "darwin":
        # macOS
        candidates = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        ]
        for p in candidates:
            if p.exists():
                return str(p)

    else:
        # Linux — additional paths
        candidates = [
            Path("/usr/bin/google-chrome"),
            Path("/usr/bin/google-chrome-stable"),
            Path("/usr/bin/chromium"),
            Path("/usr/bin/chromium-browser"),
            Path("/snap/bin/chromium"),
        ]
        for p in candidates:
            if p.exists():
                return str(p)

    return None


class PortAllocator:
    """Thread-safe port range allocator."""

    def __init__(self, start: int, end: int):
        self.start = start
        self.end = end
        self._used: Set[int] = set()
        self._lock = asyncio.Lock()

    async def allocate(self) -> int:
        """Allocate the next available port.

        Raises:
            LaunchError: If no ports are available
        """
        async with self._lock:
            for port in range(self.start, self.end + 1):
                if port not in self._used:
                    self._used.add(port)
                    return port
        raise LaunchError(f"No ports available in range {self.start}-{self.end}")

    async def release(self, port: int) -> None:
        """Release a previously allocated port."""
        async with self._lock:
            self._used.discard(port)

    @property
    def used_count(self) -> int:
        return len(self._used)


class DisplayAllocator:
    """Allocates virtual display numbers for Xvfb (Linux only)."""

    def __init__(self, start: int = DISPLAY_START, end: int = DISPLAY_END):
        self.start = start
        self.end = end
        self._used: Set[int] = set()
        self._lock = asyncio.Lock()

    async def allocate(self) -> int:
        async with self._lock:
            for num in range(self.start, self.end + 1):
                if num not in self._used:
                    self._used.add(num)
                    return num
        raise LaunchError(f"No display numbers available in range {self.start}-{self.end}")

    async def release(self, num: int) -> None:
        async with self._lock:
            self._used.discard(num)


# Global allocators
cdp_ports = PortAllocator(CDP_PORT_START, CDP_PORT_END)
vnc_ports = PortAllocator(VNC_PORT_START, VNC_PORT_END)
novnc_ports = PortAllocator(NOVNC_PORT_START, NOVNC_PORT_END)
displays = DisplayAllocator()


def _chrome_base_args(user_data_dir: str, cdp_port: int) -> List[str]:
    """Common Chrome launch arguments."""
    return [
        f"--remote-debugging-port={cdp_port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-sync",
        "--disable-translate",
        "--disable-extensions",
        "--disable-component-update",
        "--disable-client-side-phishing-detection",
        "--metrics-recording-only",
        # Security: run with sandbox unless Docker
        "--disable-features=TranslateUI",
        # Start with a blank page
        "about:blank",
    ]


async def launch_chrome_headless(
    chrome_path: str,
    user_data_dir: str,
    cdp_port: int,
    width: int = 1280,
    height: int = 720,
) -> subprocess.Popen:
    """Launch Chrome in headless mode (Windows fallback, or simple mode).

    Args:
        chrome_path: Path to Chrome executable
        user_data_dir: Path to user data directory for session isolation
        cdp_port: CDP remote debugging port
        width: Initial viewport width
        height: Initial viewport height

    Returns:
        subprocess.Popen handle for the Chrome process
    """
    args = [chrome_path] + _chrome_base_args(user_data_dir, cdp_port) + [
        "--headless=new",
        f"--window-size={width},{height}",
        "--disable-gpu",
        "--no-sandbox",
    ]

    logger.info(f"Launching headless Chrome on CDP port {cdp_port}")
    logger.debug(f"Chrome args: {' '.join(args)}")

    env = os.environ.copy()

    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        # Windows-specific: don't create console window
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )

    return proc


async def launch_chrome_xvfb(
    chrome_path: str,
    user_data_dir: str,
    cdp_port: int,
    display_num: int,
    width: int = 1280,
    height: int = 720,
) -> subprocess.Popen:
    """Launch Chrome on a virtual display (Linux/Docker).

    Requires Xvfb to already be running on the given display number.

    Args:
        chrome_path: Path to Chrome executable
        user_data_dir: Path to user data directory
        cdp_port: CDP remote debugging port
        display_num: Xvfb display number
        width: Window width
        height: Window height

    Returns:
        subprocess.Popen handle for the Chrome process
    """
    args = [chrome_path] + _chrome_base_args(user_data_dir, cdp_port) + [
        f"--window-size={width},{height}",
        "--no-sandbox",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        # Start maximized on the virtual display
        "--start-maximized",
    ]

    env = os.environ.copy()
    env["DISPLAY"] = f":{display_num}"

    logger.info(f"Launching Chrome on display :{display_num}, CDP port {cdp_port}")

    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    return proc


async def start_xvfb(display_num: int, width: int = 1280, height: int = 720) -> subprocess.Popen:
    """Start Xvfb virtual display server.

    Args:
        display_num: Display number (e.g., 100 → :100)
        width: Screen width
        height: Screen height

    Returns:
        subprocess.Popen handle
    """
    xvfb_path = shutil.which("Xvfb")
    if not xvfb_path:
        raise LaunchError("Xvfb not found. Install with: apt-get install xvfb")

    args = [
        xvfb_path,
        f":{display_num}",
        "-screen", "0", f"{width}x{height}x24",
        "-ac",  # Disable access control
        "-nolisten", "tcp",
    ]

    logger.info(f"Starting Xvfb on display :{display_num} ({width}x{height})")

    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait briefly for Xvfb to start
    await asyncio.sleep(0.5)

    if proc.poll() is not None:
        stderr = proc.stderr.read().decode() if proc.stderr else ""
        raise LaunchError(f"Xvfb failed to start: {stderr}")

    return proc


async def start_vnc_server(display_num: int, vnc_port: int) -> subprocess.Popen:
    """Start x11vnc VNC server connected to an Xvfb display.

    Args:
        display_num: Xvfb display number to connect to
        vnc_port: Port for VNC server to listen on

    Returns:
        subprocess.Popen handle
    """
    x11vnc_path = shutil.which("x11vnc")
    if not x11vnc_path:
        raise LaunchError("x11vnc not found. Install with: apt-get install x11vnc")

    args = [
        x11vnc_path,
        "-display", f":{display_num}",
        "-rfbport", str(vnc_port),
        "-nopw",        # No password
        "-shared",      # Allow multiple connections
        "-forever",     # Don't exit on client disconnect
        "-noxdamage",   # Avoid Xdamage issues
        "-cursor", "arrow",
    ]

    logger.info(f"Starting x11vnc on display :{display_num}, VNC port {vnc_port}")

    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    await asyncio.sleep(0.5)

    if proc.poll() is not None:
        stderr = proc.stderr.read().decode() if proc.stderr else ""
        raise LaunchError(f"x11vnc failed to start: {stderr}")

    return proc


async def start_websockify(listen_port: int, vnc_port: int) -> subprocess.Popen:
    """Start websockify to bridge VNC to WebSocket for noVNC.

    Args:
        listen_port: Port for WebSocket server
        vnc_port: VNC server port to proxy to

    Returns:
        subprocess.Popen handle
    """
    websockify_path = shutil.which("websockify")
    if not websockify_path:
        raise LaunchError("websockify not found. Install with: pip install websockify")

    # Find noVNC web root
    novnc_web_root = None
    for candidate in ["/usr/share/novnc", "/usr/share/noVNC", "/opt/novnc"]:
        if Path(candidate).exists():
            novnc_web_root = candidate
            break

    args = [
        websockify_path,
        str(listen_port),
        f"localhost:{vnc_port}",
    ]

    if novnc_web_root:
        args.extend(["--web", novnc_web_root])

    logger.info(f"Starting websockify on port {listen_port} → VNC port {vnc_port}")

    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    await asyncio.sleep(0.3)

    if proc.poll() is not None:
        stderr = proc.stderr.read().decode() if proc.stderr else ""
        raise LaunchError(f"websockify failed to start: {stderr}")

    return proc


def kill_process_tree(proc: subprocess.Popen) -> None:
    """Kill a process and all its children.

    Platform-aware: uses taskkill on Windows, kill on Unix.
    """
    if proc.poll() is not None:
        return  # Already dead

    pid = proc.pid

    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True,
                timeout=5,
            )
        else:
            import signal
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception as e:
        logger.debug(f"Error killing process {pid}: {e}")
        try:
            proc.kill()
        except Exception:
            pass


async def wait_for_cdp_ready(host: str = "localhost", port: int = 9222, timeout: float = 15.0) -> bool:
    """Wait for Chrome's CDP debug endpoint to become available.

    Polls http://{host}:{port}/json/version until it responds.

    Args:
        host: Chrome debug host
        port: Chrome debug port
        timeout: Maximum wait time in seconds

    Returns:
        True if ready, False if timed out
    """
    import aiohttp

    deadline = asyncio.get_event_loop().time() + timeout
    url = f"http://{host}:{port}/json/version"

    while asyncio.get_event_loop().time() < deadline:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=2)) as resp:
                    if resp.status == 200:
                        return True
        except Exception:
            pass
        await asyncio.sleep(0.3)

    return False
