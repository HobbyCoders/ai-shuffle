"""
Native Chromium supervisor for local (non-Docker) installs.

In the Docker image a supervised Chromium is launched by
``docker/chrome-supervisor.sh`` on the loopback CDP port, and the built-in
``browser`` MCP server attaches to it over CDP. On native installs (Windows /
macOS / bare Linux) nothing launches that browser — so the browser tools have
nothing to attach to and the status card shows "Enabled — browser unreachable".

This module fills that gap: when the ``browser`` built-in is enabled and we are
NOT in a container, it launches and supervises a local Chrome/Edge in headless
mode on the same loopback CDP port the MCP server attaches to. It is deliberately
small and asyncio-only (no threads, no POSIX-only APIs) so it runs on Windows and
stays importable/testable on Linux CI.

Knobs:
  * ``CHROME_CDP_PORT`` (default 9221) — the loopback DevTools port.
  * ``CHROME_EXECUTABLE`` — explicit path to a Chrome/Edge/Chromium binary.
  * ``START_CHROME=0`` — opt out of auto-launch entirely (e.g. you run your own).

If the CDP port is already answered by something else, we do NOT spawn (an
external browser owns the port); its detail is surfaced via ``get_detail()``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import List, Optional

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Container detection
# ---------------------------------------------------------------------------
def _in_container() -> bool:
    """True when running inside the Docker image (native supervisor is a no-op).

    Reuses ``app.core.platform.is_docker_mode`` (the project's canonical
    deployment-mode predicate) when available; falls back to the standard
    container markers otherwise.
    """
    try:
        from app.core.platform import is_docker_mode

        return is_docker_mode()
    except Exception:
        return (
            os.path.exists("/.dockerenv")
            or os.environ.get("AI_SHUFFLE_IN_DOCKER") == "1"
        )


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------
def _cdp_port() -> int:
    try:
        return int(os.environ.get("CHROME_CDP_PORT", "9221"))
    except (TypeError, ValueError):
        return 9221


def _profile_dir() -> Path:
    """Dedicated profile dir for the supervised browser.

    MUST be a dedicated directory: Chrome 136+ silently ignores
    ``--remote-debugging-port`` when pointed at the default profile dir.

    The CDP-port suffix keeps concurrent instances (each with a distinct
    ``CHROME_CDP_PORT``) from colliding on Chrome's user-data-dir singleton:
    two launches sharing one user-data-dir dedup, the second handing off to the
    first process and exiting, so its CDP port never opens.
    """
    return Path.home() / ".ai-shuffle" / f"chrome-profile-{_cdp_port()}"


def resolve_chrome_executable() -> Optional[str]:
    """Locate a Chrome/Edge/Chromium executable, or None if none is found.

    Order: ``CHROME_EXECUTABLE`` env (if it exists) → standard Chrome install
    paths → standard Edge install paths (Windows) → ``shutil.which`` over common
    Chromium binary names (non-Windows native). Pure function — unit-testable by
    monkeypatching ``os.path.exists`` / ``shutil.which``.
    """
    env_exe = os.environ.get("CHROME_EXECUTABLE")
    if env_exe and os.path.exists(env_exe):
        return env_exe

    candidates: List[str] = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    localappdata = os.environ.get("LOCALAPPDATA")
    if localappdata:
        candidates.append(
            os.path.join(
                localappdata, "Google", "Chrome", "Application", "chrome.exe"
            )
        )
    candidates += [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path

    if sys.platform != "win32":
        for name in (
            "google-chrome-stable",
            "google-chrome",
            "chromium",
            "chromium-browser",
        ):
            found = shutil.which(name)
            if found:
                return found

    return None


# ---------------------------------------------------------------------------
# Supervisor singleton state
# ---------------------------------------------------------------------------
_proc: Optional[asyncio.subprocess.Process] = None
_supervisor_task: Optional[asyncio.Task] = None
_last_detail: Optional[str] = None
_stopping: bool = False


def get_detail() -> Optional[str]:
    """Last diagnostic detail for status surfacing (None when healthy/N-A)."""
    return _last_detail


async def _cdp_answers(port: int) -> bool:
    """True if something answers ``GET /json/version`` on the loopback CDP port."""
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"http://127.0.0.1:{port}/json/version")
            resp.raise_for_status()
            return True
    except Exception:
        return False


def _launch_args(port: int) -> List[str]:
    """CDP-headless launch flags. No ``--no-sandbox`` / ``--disable-dev-shm-usage``
    (those are Linux/docker-only concerns)."""
    return [
        f"--remote-debugging-port={port}",
        f"--user-data-dir={_profile_dir()}",
        "--headless=new",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-sync",
        "about:blank",
    ]


async def _supervise(exe: str, port: int) -> None:
    """Background loop: (re)launch the browser; give up after crash-looping."""
    global _proc, _last_detail

    exits: List[float] = []
    loop = asyncio.get_event_loop()

    while not _stopping:
        try:
            _proc = await asyncio.create_subprocess_exec(
                exe,
                *_launch_args(port),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except Exception as e:
            _last_detail = f"failed to launch native chrome: {e}"
            logger.warning("native chrome supervisor: launch failed: %s", e)
            return

        logger.info(
            "native chrome supervisor: launched %s pid=%s cdp=%s",
            exe,
            _proc.pid,
            port,
        )
        _last_detail = None

        try:
            returncode = await _proc.wait()
        except asyncio.CancelledError:
            raise

        logger.info(
            "native chrome supervisor: process exited (returncode=%s)", returncode
        )

        if _stopping:
            break

        now = loop.time()
        exits.append(now)
        # Keep only exits within the trailing 60s window.
        exits = [t for t in exits if now - t <= 60]
        if len(exits) >= 5:
            _last_detail = (
                f"Chrome crash-looped (5 exits in 60s): {returncode}"
            )
            logger.warning(
                "native chrome supervisor: %s — giving up", _last_detail
            )
            return

        # Brief backoff before relaunch.
        await asyncio.sleep(1.0)


async def ensure_started() -> None:
    """Ensure a supervised native browser is running on the loopback CDP port.

    No-op when: (a) in a container, (b) ``START_CHROME=0``, (c) already
    supervising, or (d) the CDP port is already answered by something else
    (external browser owns it). Otherwise resolves an executable and starts the
    supervision loop. Never raises — best-effort.
    """
    global _supervisor_task, _last_detail, _stopping

    if _in_container():
        return
    if os.environ.get("START_CHROME") == "0":
        return
    if _supervisor_task is not None and not _supervisor_task.done():
        return

    port = _cdp_port()

    if await _cdp_answers(port):
        _last_detail = f"external browser on port {port}"
        return

    exe = resolve_chrome_executable()
    if not exe:
        _last_detail = (
            "no Chrome/Edge executable found; set CHROME_EXECUTABLE"
        )
        logger.warning("native chrome supervisor: %s", _last_detail)
        return

    try:
        _profile_dir().mkdir(parents=True, exist_ok=True)
    except Exception as e:
        _last_detail = f"could not create chrome profile dir: {e}"
        logger.warning("native chrome supervisor: %s", _last_detail)
        return

    _stopping = False
    _last_detail = None
    _supervisor_task = asyncio.create_task(_supervise(exe, port))


async def stop() -> None:
    """Cancel supervision and terminate the browser. Idempotent."""
    global _proc, _supervisor_task, _stopping, _last_detail

    _stopping = True

    task = _supervisor_task
    _supervisor_task = None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    proc = _proc
    _proc = None
    if proc is not None and proc.returncode is None:
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except (asyncio.TimeoutError, Exception):
            try:
                proc.kill()
            except Exception:
                pass

    _last_detail = None
    _stopping = False
