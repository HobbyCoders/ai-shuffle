"""
Browser health watchdog and native-install process supervisor.

In the Docker image a supervised Chromium is launched by
``docker/chrome-supervisor.sh`` on the loopback CDP port, and the built-in
``browser`` MCP server attaches to it over CDP. On native installs (Windows /
macOS / bare Linux) nothing launches that browser — so the browser tools have
nothing to attach to and the status card shows "Enabled — browser unreachable".

This module fills that gap: when the ``browser`` built-in is enabled and we are
NOT in a container, it launches and supervises a local Chrome/Edge in headless
mode on the same loopback CDP port the MCP server attaches to. Native process
supervision is asyncio-only (no threads or POSIX-only APIs), so it runs on
Windows and stays importable/testable on Linux CI.

In Docker the shell remains the only launcher. A demand-started app watchdog
probes page-level CDP, defers while a viewer/agent owns the browser, and repairs
wedged tabs or signals only the verified supervised Chromium listener. The shell
then relaunches it against the same shared profile; recovery never writes it.

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
from contextlib import asynccontextmanager
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path
from typing import List, Optional

import httpx
import websockets

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


_PAGE_PROBE_TIMEOUT_S = 15.0
_HEALTH_INTERVAL_S = 10.0
_DOCKER_RESTART_GAP_S = 60.0
_DOCKER_RESTART_WINDOW_S = 600.0
_DOCKER_MAX_RESTARTS = 3
_docker_restarts: list[float] = []
_recovery_done: Optional[asyncio.Event] = None
_PAGE_FAILURE_MIN_S = 30.0
_PAGE_FAILURE_COUNT = 3
_HEALTH_BACKOFF_S = 180.0
_health_states: dict[int, dict] = {}
_external_users = 0
_protected_pages: set[tuple[int, str]] = set()


@asynccontextmanager
async def browser_in_use():
    """Owner sign-in flows participate in the shared recovery admission gate."""
    global _external_users
    await wait_for_recovery()
    _external_users += 1
    try:
        yield
    finally:
        _external_users -= 1


def _health_now():
    return time.monotonic()


def _health_state(port):
    return _health_states.setdefault(port, {"ready": False, "failures": {}, "backoff": 0.0})


def _failed_long_enough(port, key):
    failures = _health_state(port)["failures"]
    now = _health_now()
    first, count = failures.get(key, (now, 0))
    failures[key] = (first, count + 1)
    return count + 1 >= _PAGE_FAILURE_COUNT and now - first >= _PAGE_FAILURE_MIN_S


def _restart_allowed(port):
    now = _health_now()
    _docker_restarts[:] = [t for t in _docker_restarts if now - t < _DOCKER_RESTART_WINDOW_S]
    allowed = len(_docker_restarts) < _DOCKER_MAX_RESTARTS and (
        not _docker_restarts or now - _docker_restarts[-1] >= _DOCKER_RESTART_GAP_S)
    if not allowed:
        _health_state(port)["backoff"] = now + _HEALTH_BACKOFF_S
    return allowed


async def wait_for_recovery() -> None:
    """Admission barrier: new agent calls/takeovers wait for an idle repair."""
    while _recovery_done is not None:
        await _recovery_done.wait()


def _browser_in_use() -> bool:
    from app.core import browser_control, browser_runtime

    return bool(_external_users or _protected_pages) or browser_control.recovery_blocked() or browser_runtime.recovery_blocked()


def _docker_chrome_process(port: int):
    """Return only our browser listener, never a renderer/launcher/other Chrome.

    psutil's Process object retains creation identity and checks PID reuse when
    signalling. All listeners must agree on one PID; ancestry, UID, executable,
    profile and CDP-port arguments must match the supervised Chromium.
    """
    import psutil

    try:
        listeners = [c for c in psutil.net_connections(kind="tcp")
                     if c.status == psutil.CONN_LISTEN and c.laddr.port == port]
        pids = {c.pid for c in listeners}
        if not pids or None in pids or len(pids) != 1:
            return None
        process = psutil.Process(pids.pop())
        args = process.cmdline()
        if (process.uids().real != os.getuid()
                or Path(process.exe()).name not in {"chrome", "chromium", "chromium-browser"}
                or f"--remote-debugging-port={port}" not in args
                or f"--user-data-dir={Path.home() / 'chrome-profile'}" not in args
                or any(arg.startswith("--type=") for arg in args)):
            return None
        egress = False
        for ancestor in process.parents():
            names = {Path(arg).name for arg in ancestor.cmdline()}
            egress |= "egress-launch.js" in names
            if names & {"chrome-supervisor", "chrome-supervisor.sh"}:
                return process if egress else None
    except (psutil.Error, OSError):
        pass
    return None


async def _restart_docker_chrome(port: int, expected) -> bool:
    """Reverify the listener, request a clean exit, then force only that PID."""
    import psutil

    if _browser_in_use() or not _restart_allowed(port):
        return False
    current = await asyncio.to_thread(_docker_chrome_process, port)
    if (current is None or current.pid != expected.pid
            or current.create_time() != expected.create_time() or _browser_in_use()):
        return False
    try:
        current.terminate()
        _docker_restarts.append(_health_now())
        if len(_docker_restarts) >= _DOCKER_MAX_RESTARTS:
            _health_state(port)["backoff"] = _health_now() + _HEALTH_BACKOFF_S
        try:
            await asyncio.to_thread(current.wait, timeout=5)
        except psutil.TimeoutExpired:
            # psutil checks PID creation identity before every signal.
            current.kill()
    except (psutil.Error, OSError):
        return False
    logger.warning("browser health: stopped unresponsive supervised Chromium; waiting for supervisor relaunch")
    return True


async def _check_health(port: int, *, docker: bool = False) -> None:
    global _recovery_done
    from app.core import browser_runtime

    state = _health_state(port)
    if _protected_pages and not _external_users:
        live = await cdp_get("/json/list", port)
        if isinstance(live, list):
            ids = {t.get("id") for t in live if isinstance(t, dict)}
            _protected_pages.difference_update({(p, target) for p, target in _protected_pages if p == port and target not in ids})
    if _browser_in_use():
        state["failures"].clear()
        return
    if _recovery_done is not None or _health_now() < state["backoff"]:
        return
    process = await asyncio.to_thread(_docker_chrome_process, port) if docker else None
    if docker and process is None:
        return
    if docker:
        identity = (process.pid, process.create_time())
        if state.get("process") != identity:
            state.update(process=identity, ready=False, failures={})
    # Probes never hold admission: normal calls may start while a page is slow.
    if await browser_runtime.dialogs_block_recovery() or _browser_in_use():
        state["failures"].clear()
        return
    candidates = await _probe_pages(port)
    if _browser_in_use():
        state["failures"].clear()
        return
    if candidates is None:
        return
    if await browser_runtime.dialogs_block_recovery() or _browser_in_use():
        state["failures"].clear()
        return
    done = asyncio.Event()
    _recovery_done = done  # Idle check and repair admission are atomic.
    try:
        version, targets = candidates
        if version is not None and await _repair_pages(port, version, targets):
            state["failures"].clear()
            return
        if docker:
            await _restart_docker_chrome(port, process)
        elif _restart_allowed(port):
            _docker_restarts.append(_health_now())
            if len(_docker_restarts) >= _DOCKER_MAX_RESTARTS:
                state["backoff"] = _health_now() + _HEALTH_BACKOFF_S
            await _stop_process(_proc)
    finally:
        _recovery_done = None
        done.set()


async def _watch_docker(port: int) -> None:
    """App-owned health watchdog; the shell remains the only process launcher."""
    while not _stopping:
        await asyncio.sleep(_HEALTH_INTERVAL_S)
        try:
            await _check_health(port, docker=True)
        except Exception:
            logger.warning("browser health: Docker check failed; deferring", exc_info=True)


async def _cdp_command(url: str, method: str, params: dict) -> dict:
    """One bounded CDP request; ignore unsolicited events on the socket."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme != "ws" or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("Expected a loopback CDP WebSocket")
    async with asyncio.timeout(_PAGE_PROBE_TIMEOUT_S):
        async with websockets.connect(url, open_timeout=_PAGE_PROBE_TIMEOUT_S, close_timeout=0.1) as ws:
            await ws.send(json.dumps({"id": 1, "method": method, "params": params}))
            async for raw in ws:
                message = json.loads(raw)
                if message.get("id") == 1:
                    if "error" in message:
                        raise RuntimeError("CDP command failed")
                    return message.get("result", {})
    raise RuntimeError("CDP socket closed before replying")


async def _probe_pages(port: int):
    """Non-mutating probes: require >=3 failures spanning >=30 seconds.

    No renderer checks or startup restarts until the browser CDP endpoint has
    answered at least once. One slow script or transient HTTP miss is harmless.
    """
    state = _health_state(port)
    version = await cdp_get("/json/version", port)
    if not isinstance(version, dict):
        if state["ready"] and _failed_long_enough(port, "browser"):
            return None, None
        return None
    state["ready"] = True
    state["failures"].pop("browser", None)
    targets = await cdp_get("/json/list", port)
    if not isinstance(targets, list):
        if _failed_long_enough(port, "targets"):
            return None, None
        return None
    state["failures"].pop("targets", None)
    live = {t.get("id") for t in targets if isinstance(t, dict)}
    state["failures"] = {k: v for k, v in state["failures"].items() if k in live}
    wedged = []
    for target in targets:
        if _browser_in_use():
            state["failures"].clear()
            return None
        if not isinstance(target, dict) or target.get("type") != "page":
            continue
        key = target.get("id")
        try:
            await _cdp_command(target["webSocketDebuggerUrl"], "Runtime.evaluate", {"expression": "1"})
            state["failures"].pop(key, None)
        except Exception:
            if _failed_long_enough(port, key):
                wedged.append(target)
    return (version, wedged) if wedged else None


async def _repair_pages(port: int, version: dict, targets: list) -> bool:
    """Called only for confirmed failures, while idle and behind admission."""
    for target in targets:
        logger.warning("browser health: replacing repeatedly unresponsive page")
        created = None
        try:
            url = version["webSocketDebuggerUrl"]
            created = await _cdp_command(url, "Target.createTarget", {"url": "about:blank"})
            if not created.get("targetId"):
                return False
            fresh = await cdp_get("/json/list", port)
            replacement = next(t for t in fresh if t.get("id") == created["targetId"])
            await _cdp_command(replacement["webSocketDebuggerUrl"], "Runtime.evaluate", {"expression": "1"})
            closed = await _cdp_command(url, "Target.closeTarget", {"targetId": target["id"]})
            if not closed.get("success"):
                raise RuntimeError("Could not close unresponsive page")
        except Exception:
            if created and created.get("targetId"):
                try:
                    await _cdp_command(url, "Target.closeTarget", {"targetId": created["targetId"]})
                except Exception:
                    pass
            return False
    return True


async def wait_until_answering(port: int, task=None):
    """Bounded startup wait shared by tab creation and private sign-in."""
    async with asyncio.timeout(10):
        while not await _cdp_answers(port):
            if task is not None and task.done():
                return False
            await asyncio.sleep(0.1)
    return True


def _owns_cdp_port(port: int) -> bool:
    """Verify the listener's process ancestry, not just an answering CDP URL.

    Docker's launcher is outside this Python process. Require both its Chrome
    supervisor and egress launcher. Inspection failures are deliberately closed.
    """
    import psutil

    try:
        if not _in_container():
            if _proc is None or _proc.returncode is not None:
                return False
            # macOS denies system-wide connection inspection without root.
            # Inspect only our supervised launcher and its own descendants.
            process = psutil.Process(_proc.pid)
            for child in [process, *process.children(recursive=True)]:
                connections = getattr(child, "net_connections", None) or child.connections
                if any(c.status == psutil.CONN_LISTEN and c.laddr.port == port
                       for c in connections(kind="tcp")):
                    return True
            return False
        listeners = [c for c in psutil.net_connections(kind="tcp")
                     if c.status == psutil.CONN_LISTEN and c.laddr.port == port]
        if not listeners:
            return False
        for listener in listeners:
            if not listener.pid:
                return False
            process = psutil.Process(listener.pid)
            chain = [process, *process.parents()]
            supervisor = egress = False
            for ancestor in chain:
                names = {Path(arg).name for arg in ancestor.cmdline()}
                supervisor |= bool(names & {"chrome-supervisor", "chrome-supervisor.sh"})
                egress |= "egress-launch.js" in names
                if supervisor and egress:
                    break  # Do not inspect protected ancestors above our supervisor.
            if not (supervisor and egress):
                return False
        return True
    except (psutil.Error, OSError):
        return False


class BrowserSignInUnavailable(RuntimeError):
    """Fixed, credential-free startup errors safe to show in the owner viewer."""


async def sign_in_browser_version():
    from app.core.config import settings

    if not settings.start_chrome:
        raise BrowserSignInUnavailable("Browser disabled. Enable the built-in browser before signing in.")
    await ensure_started()
    task = _supervisor_task
    if not _in_container() and (task is None or task.done()):
        raise BrowserSignInUnavailable("App-supervised browser unavailable; external browsers cannot be used for sign-in.")
    try:
        if not await wait_until_answering(_cdp_port(), task):
            raise BrowserSignInUnavailable("App-supervised browser stopped during startup.")
    except TimeoutError:
        raise BrowserSignInUnavailable("App-supervised browser startup timed out.") from None
    if not await asyncio.to_thread(_owns_cdp_port, _cdp_port()):
        raise BrowserSignInUnavailable("CDP port is not owned by this worker's supervised egress-protected browser.")
    version = await cdp_get("/json/version")
    if not version:
        raise BrowserSignInUnavailable("App-supervised browser disconnected.")
    return version


async def create_page(url: str, port: Optional[int] = None) -> Optional[dict]:
    async with browser_in_use():
        page = await _create_page(url, port)
        if page and (port is None or port == _cdp_port()):
            _protected_pages.add((port or _cdp_port(), page["id"]))
        return page


async def _create_page(url: str, port: Optional[int] = None) -> Optional[dict]:
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
                if not await wait_until_answering(p, task):
                    return None
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
        # A new process must first answer CDP; restart pacing survives relaunch.
        backoff = _health_state(port)["backoff"]
        _health_states[port] = {"ready": False, "failures": {}, "backoff": backoff}

        try:
            while True:
                try:
                    returncode = await asyncio.wait_for(_proc.wait(), _HEALTH_INTERVAL_S)
                    break
                except asyncio.TimeoutError:
                    await _check_health(port)
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

    Docker starts only an app health watchdog; the shell owns Chrome launches.
    No-op when already supervising, or the native CDP port is already answered
    by something else (external browser owns it —
    which also covers "run your own browser on the CDP port"). Otherwise
    resolves an executable and starts the supervision loop. Never raises —
    best-effort.
    """
    global _supervisor_task, _last_detail, _stopping

    if _supervisor_task is not None and not _supervisor_task.done():
        return

    port = _cdp_port()
    if _in_container():
        _stopping = False
        _last_detail = None
        _supervisor_task = asyncio.create_task(_watch_docker(port))
        return

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


async def _stop_process(proc) -> None:
    """Stop the egress launcher without ever changing its Chrome profile."""
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
    await _stop_process(proc)

    _last_detail = None
    _stopping = False
