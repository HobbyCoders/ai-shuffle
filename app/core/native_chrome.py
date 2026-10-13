"""
Native browser supervisor for local (non-Docker) installs.

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

If the CDP port is already answered by something else, we do NOT spawn (an
external browser owns the port); its detail is surfaced via ``get_detail()``.

The browser is started behind the egress proxy (``app.core.browser_egress``):
``node egress-launch.js --watch-stdin -- <chrome> <flags>``. Closing the
launcher's stdin stops the browser, which also works on Windows, where a
terminate cannot be forwarded. Without the launcher the browser is not started.
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


def browser_label() -> str:
    """Name of the browser this worker supervises, for agent- and user-facing text.

    The Docker image installs Debian Chromium on every architecture and links it
    as ``google-chrome-stable`` (see the Dockerfile), so a container always runs
    Chromium whatever ``CHROME_EXECUTABLE`` is called. Native installs run what
    ``resolve_chrome_executable`` finds: Google Chrome first, then Edge, then a
    Chromium on PATH. Falls back to "browser" when nothing is found.
    """
    if _in_container():
        return "Chromium"
    exe = resolve_chrome_executable()
    if not exe:
        return "browser"
    name = os.path.basename(os.path.realpath(exe)).lower()
    if "msedge" in name or "microsoft-edge" in name:
        return "Microsoft Edge"
    if "chromium" in name:
        return "Chromium"
    if "chrome" in name:
        return "Google Chrome"
    return "browser"


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
    return await cdp_get(path="/json/version", port=port) is not None


async def create_page(url: str, port: Optional[int] = None) -> Optional[dict]:
    """Open a new tab via DevTools ``PUT /json/new``. Returns the tab dict or None.

    Chrome takes the URL as the raw query string (``/json/new?https://…``),
    not a named parameter. Used by the live panel so typing an address can
    start a page even when the agent has not published a target yet.
    """
    from urllib.parse import quote

    p = port or _cdp_port()
    path = f"/json/new?{quote(url, safe=':/?&=%#')}"
    try:
        from app.core.config import settings

        # Billing/login tabs do not go through the MCP runtime. Demand-start
        # them too, but never launch for a disabled or explicitly different port.
        if settings.start_chrome and p == _cdp_port():
            await ensure_started()
            task = _supervisor_task
            if task is not None and not task.done():
                async with asyncio.timeout(10):
                    while not await _cdp_answers(p):
                        if task.done():
                            return None
                        await asyncio.sleep(0.1)
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.put(f"http://127.0.0.1:{p}{path}")
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and data.get("id"):
                return data
            return None
    except Exception:
        return None


async def cdp_get(path: str, port: Optional[int] = None) -> Optional[object]:
    """GET a Chrome DevTools HTTP endpoint (loopback only). None on failure.

    Shared probe for the supervisor and the browser-panel API; ``port`` defaults
    to ``CHROME_CDP_PORT`` (9221).
    """
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(
                f"http://127.0.0.1:{port or _cdp_port()}{path}"
            )
            resp.raise_for_status()
            return resp.json()
    except Exception:
        return None


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


def _egress_command(exe: str, args: List[str]) -> List[str]:
    """``exe args`` wrapped in the egress-proxy launcher (raises if unavailable)."""
    from app.core.browser_egress import launch_command

    return launch_command(exe, args)


def _egress_env() -> dict:
    """Process env plus the app's base SSRF policy for the proxy.

    Without the server-run marker: the browser is a service of this install,
    not a leftover of the run that launched it (#1711).
    """
    from app.core.browser_egress import egress_env
    from app.core.process_ledger import without_run_marker
    from app.core.process_tree import child_env

    return without_run_marker(child_env(egress_env()))


# Seconds between relaunches, and a run shorter than this counts as a rapid
# exit whose launcher output is surfaced in the detail.
_RELAUNCH_BACKOFF_S = 1.0
_RAPID_EXIT_S = 10.0
_LAUNCHER_LOG_MAX_BYTES = 1024 * 1024
_TAIL_LINES = 5


def _launcher_log_path(port: int) -> Path:
    """Where the egress launcher's stderr goes (``<data_dir>/logs/<instance>``).

    The launcher reports its own failures (``[browser-egress] could not
    start: ...``, the policy line) only on stderr.
    """
    try:
        from app.core.file_logging import get_log_dir

        log_dir = get_log_dir()
    except Exception:
        log_dir = _profile_dir().parent / "logs"
    return log_dir / f"chrome-launcher-{port}.log"


def _open_launcher_log(port: int):
    """Open the launcher log for appending (reset past 1 MiB), or None."""
    try:
        path = _launcher_log_path(port)
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = "ab"
        try:
            if path.stat().st_size > _LAUNCHER_LOG_MAX_BYTES:
                mode = "wb"
        except OSError:
            pass
        return open(path, mode)
    except Exception as e:
        logger.debug("native chrome supervisor: no launcher log: %s", e)
        return None


def _launcher_log_tail(log, start: int) -> str:
    """The last non-empty lines the launcher wrote since offset *start*."""
    if log is None:
        return ""
    try:
        log.flush()
        with open(log.name, "rb") as f:
            f.seek(start)
            text = f.read().decode("utf-8", errors="replace")
    except Exception:
        return ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return " | ".join(lines[-_TAIL_LINES:])[-500:]


async def _supervise(exe: str, port: int) -> None:
    """Background loop: (re)launch the browser; give up after crash-looping."""
    global _proc, _last_detail

    exits: List[float] = []
    loop = asyncio.get_event_loop()

    while not _stopping:
        log = _open_launcher_log(port)
        log_start = log.tell() if log is not None else 0
        started = loop.time()
        try:
            _proc = await asyncio.create_subprocess_exec(
                *_egress_command(exe, _launch_args(port)),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=log if log is not None else asyncio.subprocess.DEVNULL,
                env=_egress_env(),
            )
        except Exception as e:
            if log is not None:
                log.close()
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
            if log is not None:
                log.close()
            raise

        tail = ""
        now = loop.time()
        if now - started < _RAPID_EXIT_S:
            tail = _launcher_log_tail(log, log_start)
        if log is not None:
            log.close()

        logger.info(
            "native chrome supervisor: process exited (returncode=%s)%s",
            returncode,
            f": {tail}" if tail else "",
        )

        if _stopping:
            break

        if tail:
            _last_detail = f"browser launcher exited ({returncode}): {tail}"
        exits.append(now)
        # Keep only exits within the trailing 60s window.
        exits = [t for t in exits if now - t <= 60]
        if len(exits) >= 5:
            _last_detail = (
                f"Chrome crash-looped (5 exits in 60s): {returncode}"
                + (f"; last output: {tail}" if tail else "")
            )
            logger.warning(
                "native chrome supervisor: %s — giving up", _last_detail
            )
            return

        # Brief backoff before relaunch.
        await asyncio.sleep(_RELAUNCH_BACKOFF_S)


async def ensure_started() -> None:
    """Ensure a supervised native browser is running on the loopback CDP port.

    No-op when: (a) in a container, (b) already supervising, or (c) the CDP
    port is already answered by something else (external browser owns it —
    which also covers "run your own browser on the CDP port"). Otherwise
    resolves an executable and starts the supervision loop. Never raises —
    best-effort.
    """
    global _supervisor_task, _last_detail, _stopping

    if _in_container():
        return
    if _supervisor_task is not None and not _supervisor_task.done():
        return

    port = _cdp_port()

    answered = await _cdp_answers(port)
    # Different sessions can demand-start together. Another caller may have
    # installed our supervisor while this probe yielded; never launch a second
    # process or mistake our newly started browser for an external owner.
    if _supervisor_task is not None and not _supervisor_task.done():
        return
    if answered:
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
        _egress_command(exe, [])
    except Exception as e:
        # Fail closed: never run the browser without its egress proxy.
        _last_detail = f"browser not started: {e}"
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
        # End of stdin tells the egress launcher to stop the browser. On
        # Windows terminate() would kill only the launcher, so give it a moment.
        stdin = getattr(proc, "stdin", None)
        if stdin is not None:
            try:
                stdin.close()
            except Exception:
                pass
            if sys.platform == "win32":
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except (asyncio.TimeoutError, Exception):
                    pass
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
