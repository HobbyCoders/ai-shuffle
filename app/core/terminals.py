"""In-app terminals: owner-only shells on this worker's own pseudo-terminal.

Each terminal is an interactive shell on a Unix PTY (bash or sh) or Windows
ConPTY (PowerShell). A Unix session or Windows Job Object contains everything
the shell starts, background jobs included. Output goes
into a bounded scrollback (about 5,000 lines or 2 MB, whichever is smaller)
and to every attached viewer; a browser reload reattaches by id and replays
the scrollback. Terminals live in this process only: when the app stops every
shell and its descendants are killed and the list starts empty.

The wire contract (routes, WebSocket messages, errors) is
``docs/internal/terminals.md``. Authorization lives in ``app/api/terminals.py``;
nothing here is reachable from a tool, hook, automation or agent.

The shell environment is the worker's own environment minus every secret
(:func:`shell_environment`). It is stricter than the Bash-tool scrubber: the
provider keys that tool deliberately lets through are dropped here too.

On Windows, ConPTY runs PowerShell in a kill-on-close Job Object. The
HTTP/WebSocket contract and bounded output buffers are shared with Unix.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Mapping, Optional, Tuple

from fluid.tools import agent_runner

logger = logging.getLogger(__name__)

_PTY_OK = False
if sys.platform == "win32":
    from app.core import windows_conpty

    _PTY_OK = windows_conpty.supported()
else:
    try:
        import fcntl
        import select
        import struct
        import termios

        _PTY_OK = True
    except ImportError:  # pragma: no cover — every Unix CPython has these
        pass

# ---------------------------------------------------------------------------
# Limits (all documented in docs/internal/terminals.md)
# ---------------------------------------------------------------------------

TERMINAL_LIMIT = 8
SCROLLBACK_MAX_BYTES = 2 * 1024 * 1024
SCROLLBACK_MAX_LINES = 5000
# Output a viewer may fall behind by before it is disconnected (4408) and
# told to reattach. Bounds per-viewer memory.
VIEWER_MAX_PENDING_BYTES = 1024 * 1024
OUTPUT_FRAME_BYTES = 64 * 1024
PASTE_MAX_CHARS = 256 * 1024
COLS_RANGE = (10, 500)
ROWS_RANGE = (2, 200)
DEFAULT_COLS, DEFAULT_ROWS = 80, 24
BUSY_POLL_SECONDS = 1.0
WRITE_TIMEOUT_SECONDS = 5.0
PASTE_READY_TIMEOUT_SECONDS = 5.0
PASTE_QUIET_SECONDS = 0.15
_READ_SIZE = 16 * 1024
_COALESCE_BYTES = 16 * 1024

STATE_STARTING = "starting"
STATE_LIVE = "live"
STATE_EXITED = "exited"

TARGET_WORKER = "worker"

# Bracketed paste (xterm DECSET 2004): readline turns it on while it waits
# for a line and off before running it. Any program's output can carry these
# too, so the flag alone never decides a paste (Terminal.holds_bracketed_paste).
_BRACKETED_ON = b"\x1b[?2004h"
_BRACKETED_OFF = b"\x1b[?2004l"
_PASTE_START = "\x1b[200~"
_PASTE_END = "\x1b[201~"

# Shells we offer, in preference order (the first one present is the default).
_SHELLS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("bash", ("/bin/bash", "/usr/bin/bash", "/usr/local/bin/bash")),
    ("sh", ("/bin/sh", "/usr/bin/sh")),
)

# Becomes a controlling-terminal session leader, then execs the shell. Run
# through ``subprocess`` (fork+exec in C) rather than ``pty.fork`` so the
# multi-threaded worker never runs Python between fork and exec.
# Signal dispositions are inherited through exec. A worker started as a
# background job of a non-interactive shell (`python -m app &` in an
# entrypoint script) runs with SIGINT and SIGQUIT ignored, and a shell started
# that way cannot reset them for its children, so Ctrl-C would never interrupt
# a command in the terminal. Restore the defaults before handing over to the
# shell.
_TRAMPOLINE = (
    "import os,sys,fcntl,termios,signal;"
    "fcntl.ioctl(0,termios.TIOCSCTTY,0);"
    "signal.signal(signal.SIGINT,signal.SIG_DFL);signal.signal(signal.SIGQUIT,signal.SIG_DFL);"
    "os.execv(sys.argv[1],sys.argv[1:])"
)


class TerminalError(Exception):
    """A refusal with a stable ``code`` and a plain-language ``message``."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


# Start in a stable, non-user cwd, then select the directory by descriptor.
# This runs AFTER exec in a new interpreter, so it is safe in a threaded host.
# Verify inode ancestry again in the child: an ancestor could have been moved
# out of the workspace after the no-follow walk in the parent.
_CWD_TRAMPOLINE = """
import os, sys
folder, root = map(int, sys.argv[1:3])
flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
identity = lambda fd: (os.fstat(fd).st_dev, os.fstat(fd).st_ino)
expected = identity(root)
current = os.dup(folder)
try:
    while identity(current) != expected:
        parent = os.open("..", flags, dir_fd=current)
        if identity(parent) == identity(current):
            os.close(parent)
            raise RuntimeError("Terminal folder moved outside the Files root")
        os.close(current)
        current = parent
finally:
    os.close(current)
os.fchdir(folder)
os.close(folder)
os.close(root)
os.execv(sys.argv[3], sys.argv[3:])
"""


@contextmanager
def _pin_folder(path: str, root: Optional[str]):
    """Revalidate and hold an explicit Files folder until the child owns it.

    No pathname lookup of the selected cwd happens after the POSIX walk.
    Windows cannot fchdir, so pin *all* ancestors without delete sharing:
    they cannot be renamed/replaced until ConPTY CreateProcess has returned.
    """
    if root is None:
        yield path, ()
        return
    with ExitStack() as stack:
        try:
            base, target = Path(root), Path(path)
            names = target.relative_to(base).parts
            if not base.is_absolute() or ".." in names:
                raise ValueError("Folder is outside the Files root")
            if sys.platform == "win32":
                from app.core.confined_files import _windows_directory

                held = stack.enter_context(_windows_directory(base, list(names)))
                # Every component is now pinned and reparse-point-free; this
                # final name check cannot race a rename before CreateProcess.
                if held.resolve(strict=True) != target:
                    raise ValueError("Folder changed while opening")
                fds = ()
            else:
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

                def opened(name, parent=None):
                    fd = os.open(name, flags, dir_fd=parent)
                    stack.callback(os.close, fd)
                    return fd

                root_fd = opened(base.anchor)
                for name in base.parts[1:]:
                    root_fd = opened(name, root_fd)
                folder_fd = opened(".", root_fd)
                for name in names:
                    folder_fd = opened(name, folder_fd)
                fds = (folder_fd, root_fd)
        except (OSError, ValueError) as exc:
            raise TerminalError("bad_folder", "The Files folder changed or is no longer accessible.", 400) from exc
        yield path, fds


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Platform, shells and identity
# ---------------------------------------------------------------------------

def pty_supported() -> bool:
    """Whether this worker has Unix PTYs or Windows ConPTY."""
    return _PTY_OK


def available_shells() -> List[Dict[str, Any]]:
    """The shells present on this worker; the first is the default."""
    found: List[Dict[str, Any]] = []
    shells = _SHELLS
    if sys.platform == "win32":
        # Desktop launches need not have PowerShell 7 on PATH.
        shells = (
            ("pwsh", (os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                                    "PowerShell", "7", "pwsh.exe"),)),
            ("powershell", (os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                          "System32", "WindowsPowerShell", "v1.0", "powershell.exe"),)),
        )
    for name, candidates in shells:
        path = next((p for p in candidates if os.path.isfile(p) and os.access(p, os.X_OK)), None)
        if path is None:
            which = shutil.which(name)
            path = which if which and os.path.isabs(which) else None
        if path:
            found.append({"name": name, "path": path, "default": not found})
    return found


def supported() -> bool:
    return pty_supported() and bool(available_shells())


def capability_version() -> int:
    """The ``terminal`` capability a worker advertises: 1 = phase-1 PTY
    terminals, 0 = none (no PTY support or no shell)."""
    try:
        return 1 if supported() else 0
    except Exception:  # noqa: BLE001 — capability detection never raises
        return 0


def worker_identity() -> Dict[str, Any]:
    """``{id, name, platform, container}`` for this worker. Never raises."""
    name, worker_id = "", ""
    try:
        from app.core import environment

        found = environment._worker_identity()
        name, worker_id = found.get("name") or "", found.get("device_id") or ""
    except Exception:  # noqa: BLE001
        logger.debug("terminals: worker identity unavailable", exc_info=True)
    if not worker_id:
        try:
            from app.core.config import settings

            worker_id = settings.effective_instance_id
        except Exception:  # noqa: BLE001
            worker_id = ""
    if not name:
        try:
            from app.core.cluster import default_device_name

            name = default_device_name()
        except Exception:  # noqa: BLE001
            name = "this worker"
    container = False
    try:
        from app.core.platform import is_docker_mode

        container = bool(is_docker_mode())
    except Exception:  # noqa: BLE001
        pass
    plat = "windows" if sys.platform.startswith("win") else ("darwin" if sys.platform == "darwin" else "linux")
    return {"id": worker_id, "name": name or "this worker", "platform": plat, "container": container}


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

# Anything that names a credential. Stricter than fluid.tools.child_env: the
# provider keys that scrubber allows for the Bash tool are secrets here too.
_SECRET_NAME_RE = re.compile(
    r"(SECRET|TOKEN|PASSW|CREDENTIAL|PRIVATE|_KEY$|^KEY$|_KEYS$|APIKEY|_DSN$|^DSN$|AUTH|COOKIE|SESSION|SALT|SIGNING|WEBHOOK)",
    re.IGNORECASE,
)
# A URL carrying a user:password part (proxy or database URLs).
_CREDENTIAL_URL_RE = re.compile(r"^[a-z][a-z0-9+.-]*://[^/@\s:]*:[^/@\s]*@", re.IGNORECASE)
# A URL that carries its credential in the query (signed links, ?token=).
_CREDENTIAL_QUERY_RE = re.compile(
    r"^[a-z][a-z0-9+.-]*://[^\s]*[?&][a-z0-9_.-]*(?:token|key|secret|sig|signature|password|passwd|auth)=",
    re.IGNORECASE)
_DROP_NAMES = frozenset({
    "FLUID_CHILD_ENV_PASSTHROUGH", "FLUID_CHILD_ENV_UNSET",
})


def is_terminal_secret(name: str, value: str = "") -> bool:
    """True when ``name``/``value`` must not reach a terminal's shell."""
    from fluid.tools.child_env import ALLOWED_CREDENTIAL_NAMES, is_host_secret

    upper = name.upper()
    if upper in _DROP_NAMES or upper in ALLOWED_CREDENTIAL_NAMES:
        return True
    if is_host_secret(name) or _SECRET_NAME_RE.search(upper):
        return True
    return bool(value and (_CREDENTIAL_URL_RE.match(value) or _CREDENTIAL_QUERY_RE.match(value)))


def shell_environment(shell_name: str, shell_path: str,
                      source: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """The environment a terminal's shell starts with: this worker's own
    environment without any secret, plus terminal settings."""
    base = os.environ if source is None else source
    env = {k: v for k, v in base.items() if not is_terminal_secret(k, v)}
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    env["SHELL"] = shell_path
    env.setdefault("LANG", "C.UTF-8")
    if not env.get("HOME"):
        env["HOME"] = os.path.expanduser("~")
    if "PS1" not in env:
        env["PS1"] = r"\u@\h:\w\$ " if shell_name == "bash" else "$ "
    return env


# ---------------------------------------------------------------------------
# Scrollback
# ---------------------------------------------------------------------------

class Scrollback:
    """Bounded output history: at most ``max_lines`` lines and ``max_bytes``
    bytes, trimmed from the front at a line boundary where possible."""

    def __init__(self, max_bytes: int = SCROLLBACK_MAX_BYTES, max_lines: int = SCROLLBACK_MAX_LINES):
        self.max_bytes = max_bytes
        self.max_lines = max_lines
        self._chunks: Deque[bytes] = deque()
        self._bytes = 0
        self._lines = 0

    @property
    def size(self) -> int:
        return self._bytes

    @property
    def lines(self) -> int:
        return self._lines

    def append(self, data: bytes) -> None:
        if not data:
            return
        if self._chunks and len(self._chunks[-1]) < _COALESCE_BYTES:
            self._chunks[-1] = self._chunks[-1] + data
        else:
            self._chunks.append(data)
        self._bytes += len(data)
        self._lines += data.count(b"\n")
        self._trim()

    def _trim(self) -> None:
        while self._chunks and (self._bytes > self.max_bytes or self._lines > self.max_lines):
            head = self._chunks[0]
            cut = 0
            if self._lines > self.max_lines:
                pos = -1
                for _ in range(self._lines - self.max_lines):
                    pos = head.find(b"\n", pos + 1)
                    if pos < 0:
                        break
                cut = len(head) if pos < 0 else pos + 1
            if self._bytes > self.max_bytes:
                need = self._bytes - self.max_bytes
                if need >= len(head):
                    cut = len(head)
                else:
                    nl = head.find(b"\n", need - 1)
                    cut = max(cut, len(head) if nl < 0 else nl + 1)
            if cut >= len(head):
                self._chunks.popleft()
                dropped = head
            else:
                self._chunks[0] = head[cut:]
                dropped = head[:cut]
            self._bytes -= len(dropped)
            self._lines -= dropped.count(b"\n")

    def snapshot(self) -> bytes:
        return b"".join(self._chunks)


# ---------------------------------------------------------------------------
# Viewers
# ---------------------------------------------------------------------------

class Viewer:
    """One attachment's bounded outbox. Filled on the event loop only."""

    def __init__(self, max_pending: int = VIEWER_MAX_PENDING_BYTES):
        self.max_pending = max_pending
        self.items: Deque[Tuple[str, Any]] = deque()
        self.pending = 0
        self.overflowed = False
        self.closed_reason: Optional[str] = None
        self.event = asyncio.Event()

    def push_output(self, data: bytes) -> None:
        if self.overflowed or self.closed_reason:
            return
        if self.pending + len(data) > self.max_pending:
            self.overflowed = True
            self.items.clear()
            self.pending = 0
        else:
            if self.items and self.items[-1][0] == "output" and len(self.items[-1][1]) < OUTPUT_FRAME_BYTES:
                self.items[-1] = ("output", self.items[-1][1] + data)
            else:
                self.items.append(("output", data))
            self.pending += len(data)
        self.event.set()

    def push_state(self, payload: Dict[str, Any]) -> None:
        """Queue a JSON message (state, error, pong)."""
        if not self.overflowed and not self.closed_reason:
            self.items.append(("state", payload))
            self.event.set()

    def close(self, reason: str) -> None:
        self.closed_reason = reason
        self.event.set()

    def drain(self) -> List[Tuple[str, Any]]:
        items = list(self.items)
        self.items.clear()
        self.pending = 0
        self.event.clear()
        return items


# ---------------------------------------------------------------------------
# Process-session helpers
# ---------------------------------------------------------------------------

def _session_members(sid: int) -> List[int]:
    """Every live pid whose session id is ``sid`` (Linux ``/proc``); on other
    platforms the shell's descendants via psutil when it is present."""
    pids: List[int] = []
    if os.path.isdir("/proc"):
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/stat", "rb") as fh:
                    stat = fh.read()
                fields = stat[stat.rfind(b")") + 2:].split()
                if int(fields[3]) == sid and fields[0] != b"Z":
                    pids.append(int(entry))
            except (OSError, ValueError, IndexError):
                continue
        return pids
    try:
        import psutil

        return [sid] + [c.pid for c in psutil.Process(sid).children(recursive=True)]
    except Exception:  # noqa: BLE001
        return [sid]


def _signal_session(sid: int, sig: int) -> None:
    """Signal the shell's whole session: its own group, every job's group and
    every member pid. Never signals this worker's own session."""
    if sid <= 1 or sid == os.getpid():
        return
    try:
        if sid == os.getsid(0):
            return
    except OSError:
        pass
    groups = {sid}
    for pid in _session_members(sid):
        try:
            groups.add(os.getpgid(pid))
        except OSError:
            pass
        try:
            os.kill(pid, sig)
        except OSError:
            pass
    own_group = os.getpgid(0)
    for pgid in groups:
        if pgid <= 1 or pgid == own_group:
            continue
        try:
            os.killpg(pgid, sig)
        except OSError:
            pass


def kill_session(sid: int, proc: Optional[subprocess.Popen] = None, grace: float = 1.0) -> None:
    """Hang up the shell's session, then SIGKILL anything still there. Blocking."""
    _signal_session(sid, signal.SIGHUP)
    _signal_session(sid, signal.SIGCONT)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not _session_members(sid) and (proc is None or proc.poll() is not None):
            break
        time.sleep(0.05)
    _signal_session(sid, signal.SIGKILL)
    if proc is not None:
        try:
            proc.wait(timeout=2)
        except Exception:  # noqa: BLE001
            pass


def _exit_code(returncode: Optional[int]) -> int:
    if returncode is None:
        return -1
    return returncode if returncode >= 0 else 128 + (-returncode)


# ---------------------------------------------------------------------------
# Terminal
# ---------------------------------------------------------------------------

class Terminal:
    """One shell on one PTY. All state changes happen on the event loop; the
    reader thread only hands bytes and the exit over with
    ``call_soon_threadsafe``."""

    def __init__(self, *, shell: Dict[str, Any], cwd: str, folder_source: str,
                 session_id: Optional[str], created_by: str, worker_id: str,
                 cols: int, rows: int):
        self.id = "term_" + secrets.token_hex(8)
        self.target = {"kind": TARGET_WORKER, "worker_id": worker_id}
        self.shell = shell["name"]
        self.shell_path = shell["path"]
        self.cwd = cwd
        # Only explicit Files-authorized folders carry a root. Defaults keep
        # their existing project/worktree/home policy, including on restart.
        self.cwd_root: Optional[str] = None
        self.folder_source = folder_source
        self.session_id = session_id
        self.created_by = created_by
        self.created_at = _now_iso()
        self.cols, self.rows = cols, rows
        self.state = STATE_STARTING
        self.exit_code: Optional[int] = None
        self.exited_at: Optional[str] = None
        self.busy = False
        self.scrollback = Scrollback()
        self.viewers: set = set()
        self.bracketed = False
        self._tail = b""
        self._last_output = 0.0
        self._started_at = 0.0
        self._proc: Optional[subprocess.Popen] = None
        self._fd: Optional[int] = None
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._write_lock = asyncio.Lock()
        self._closing = False
        self._exited = asyncio.Event()

    # -- lifecycle ---------------------------------------------------------

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid if self._proc else None

    def spawn(self) -> None:
        """Start the shell. Must be called on the event loop."""
        self._loop = asyncio.get_running_loop()
        env = shell_environment(self.shell, self.shell_path)
        master, slave = os.openpty()
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", self.rows, self.cols, 0, 0))
            # Under Docker agent isolation an owner terminal also starts as
            # the agent account, through the spawn runner.
            argv, cwd, env = agent_runner.wrap(
                [sys.executable, "-I", "-S", "-c", _TRAMPOLINE, self.shell_path, "-i"], self.cwd, env)
            with _pin_folder(cwd, self.cwd_root) as (cwd, fds):
                if fds:
                    # A fresh exec, NOT preexec_fn in the threaded parent.
                    # fchdir runs before the isolation runner, which still
                    # checks the cwd and drops ids before launching the shell.
                    argv = [sys.executable, "-I", "-S", "-c", _CWD_TRAMPOLINE,
                            *(str(fd) for fd in fds), *argv]
                    cwd = "/"  # never re-resolve the user-controlled pathname
                proc = subprocess.Popen(
                    argv,
                    stdin=slave, stdout=slave, stderr=slave, cwd=cwd, env=env,
                    start_new_session=True, close_fds=True, pass_fds=fds,
                )
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        os.set_blocking(master, False)
        self._start_reader(proc, master, self._read_loop)

    def _start_reader(self, proc, fd, target) -> None:
        self._proc, self._fd = proc, fd
        self.state = STATE_STARTING
        self.exit_code = None
        self.exited_at = None
        self.busy = False
        self.bracketed = False
        self._tail = b""
        self._last_output = 0.0
        self._started_at = time.monotonic()
        self._exited = asyncio.Event()
        self._thread = threading.Thread(target=target, args=(proc, fd),
                                        name=f"terminal-{self.id}", daemon=True)
        self._thread.start()

    def _read_loop(self, proc: subprocess.Popen, fd: int) -> None:
        # poll, not select: a busy worker can hold more than 1024 descriptors.
        poller = select.poll()
        poller.register(fd, select.POLLIN | select.POLLHUP | select.POLLERR)
        try:
            while True:
                try:
                    readable = poller.poll(250)
                except (OSError, ValueError):
                    break
                if not readable:
                    if proc.poll() is not None:
                        self._drain(fd)
                        break
                    continue
                try:
                    data = os.read(fd, _READ_SIZE)
                except BlockingIOError:
                    continue
                except OSError:  # EIO: every slave end closed
                    break
                if not data:
                    break
                self._post(self._on_output, data)
        finally:
            # A shell that exits leaves no background job behind either.
            try:
                kill_session(proc.pid, proc, grace=0.3)
            except Exception:  # noqa: BLE001
                logger.debug("terminal %s: session cleanup failed", self.id, exc_info=True)
            try:
                os.close(fd)
            except OSError:
                pass
            self._post(self._on_exit, _exit_code(proc.returncode))

    def _drain(self, fd: int) -> None:
        for _ in range(64):
            try:
                data = os.read(fd, _READ_SIZE)
            except (BlockingIOError, OSError):
                return
            if not data:
                return
            self._post(self._on_output, data)

    def _post(self, fn, *args) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:  # loop shut down
            pass

    def _on_output(self, data: bytes) -> None:
        self._last_output = time.monotonic()
        window = self._tail + data
        on, off = window.rfind(_BRACKETED_ON), window.rfind(_BRACKETED_OFF)
        if on > off:
            self.bracketed = True
        elif off > on:
            self.bracketed = False
        self._tail = window[-(len(_BRACKETED_ON) - 1):]
        self.scrollback.append(data)
        for viewer in list(self.viewers):
            viewer.push_output(data)
        if self.state == STATE_STARTING:
            self.state = STATE_LIVE
            self.broadcast_state()

    def _on_exit(self, code: int) -> None:
        self._fd = None
        self.state = STATE_EXITED
        self.exit_code = code
        self.exited_at = _now_iso()
        self.busy = False
        self.bracketed = False
        self._exited.set()
        if not self._closing:
            logger.info("Terminal exited: id=%s code=%s shell=%s folder=%s",
                        self.id, code, self.shell, self.cwd)
        self.broadcast_state()

    async def wait_exited(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._exited.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def kill(self) -> None:
        """Kill the shell's whole process session and wait for the exit."""
        self._closing = True
        proc = self._proc
        if proc is not None and self.state != STATE_EXITED:
            await asyncio.to_thread(kill_session, proc.pid, proc)
            await self.wait_exited(3.0)

    def kill_now(self) -> None:
        """Synchronous kill for process exit (no event loop needed)."""
        proc = self._proc
        if proc is not None and proc.poll() is None:
            kill_session(proc.pid, proc, grace=0.5)

    # -- status ------------------------------------------------------------

    def refresh_busy(self) -> bool:
        """Whether a foreground command (not the shell itself) holds the
        terminal. Returns True when the value changed."""
        busy = False
        fd, proc = self._fd, self._proc
        if self.state != STATE_EXITED and fd is not None and proc is not None:
            try:
                # 0 (or an error) while the shell is still taking the terminal.
                foreground = os.tcgetpgrp(fd)
                busy = foreground > 0 and foreground != proc.pid
            except OSError:
                busy = False
        changed = busy != self.busy
        self.busy = busy
        if self.state == STATE_STARTING and proc is not None and proc.poll() is None \
                and time.monotonic() - self._started_at > 2.0:
            self.state = STATE_LIVE
            changed = True
        return changed

    def to_dict(self) -> Dict[str, Any]:
        self.refresh_busy()
        return {
            "id": self.id,
            "target": dict(self.target),
            "shell": self.shell,
            "cwd": self.cwd,
            "folder_source": self.folder_source,
            "session_id": self.session_id,
            "state": self.state,
            "exit_code": self.exit_code,
            "created_at": self.created_at,
            "exited_at": self.exited_at,
            "busy": self.busy,
            "cols": self.cols,
            "rows": self.rows,
            "attached": len(self.viewers),
        }

    def broadcast_state(self) -> None:
        payload = {"type": "state", "terminal": self.to_dict()}
        for viewer in list(self.viewers):
            viewer.push_state(payload)

    # -- viewers -----------------------------------------------------------

    def attach(self) -> Tuple[Viewer, bytes]:
        """Subscribe and snapshot together (no await between, so no output is
        lost or duplicated). Must be called on the event loop."""
        viewer = Viewer()
        self.viewers.add(viewer)
        return viewer, self.scrollback.snapshot()

    def detach(self, viewer: Viewer) -> None:
        self.viewers.discard(viewer)

    # -- input -------------------------------------------------------------

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = cols, rows
        fd = self._fd
        if fd is None or self.state == STATE_EXITED:
            return
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    async def write(self, data: bytes) -> None:
        """Write to the shell without ever blocking the event loop."""
        async with self._write_lock:
            deadline = time.monotonic() + WRITE_TIMEOUT_SECONDS
            view = memoryview(data)
            while view:
                fd = self._fd
                if fd is None or self.state == STATE_EXITED:
                    raise TerminalError("terminal_exited", "This terminal has exited. Restart it to type again.", 409)
                try:
                    written = os.write(fd, view)
                    view = view[written:]
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        raise TerminalError("terminal_unresponsive",
                                            "The terminal isn't reading input right now. Try again in a moment.", 409)
                    await asyncio.sleep(0.01)
                except OSError:
                    raise TerminalError("terminal_exited", "This terminal has exited. Restart it to type again.", 409)

    def holds_bracketed_paste(self) -> bool:
        """Whether a bracketed paste will certainly sit at the prompt unrun.

        ``bracketed`` alone is not enough: any program's output can carry
        ``ESC[?2004h`` (a ``cat``, a ``printf``, a program killed with the mode
        on), and a shell without bash's readline runs every pasted line but the
        last. So all of these must hold, each checked now, not remembered:

        - the terminal was created as bash (sh/dash never get the wrapper);
        - the most recent mode sequence seen was "on";
        - the PTY is in readline's mode (ICANON and ECHO off), which output
          cannot fake: it is off when line editing is off (``set +o emacs``)
          or another shell reads the line;
        - on Linux, the shell process is still that bash (not ``exec sh``).
        """
        fd, proc = self._fd, self._proc
        if self.shell != "bash" or not self.bracketed or fd is None or proc is None \
                or self.state == STATE_EXITED:
            return False
        try:
            lflag = termios.tcgetattr(fd)[3]
        except (OSError, termios.error):
            return False
        if lflag & (termios.ICANON | termios.ECHO):
            return False
        if os.path.isdir("/proc"):
            try:
                if os.readlink(f"/proc/{proc.pid}/exe") != os.path.realpath(self.shell_path):
                    return False
            except OSError:
                return False
        return True

    async def wait_ready(self, timeout: float = PASTE_READY_TIMEOUT_SECONDS) -> None:
        """Wait until the shell is at its prompt: bash's readline holding
        bracketed paste, or output seen and then quiet for a moment."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self.state != STATE_EXITED:
            if self.holds_bracketed_paste():
                return
            quiet = time.monotonic() - self._last_output
            if self._last_output and quiet >= PASTE_QUIET_SECONDS:
                return
            await asyncio.sleep(0.03)


class WindowsTerminal(Terminal):
    """ConPTY transport; registry, viewers, scrollback and wire state stay shared."""

    def spawn(self) -> None:
        self._loop = asyncio.get_running_loop()
        with _pin_folder(self.cwd, self.cwd_root) as (cwd, _fds):
            proc = windows_conpty.ConPTYProcess(
                self.shell_path, cwd, shell_environment(self.shell, self.shell_path),
                self.cols, self.rows,
            )
        self._start_reader(proc, None, self._read_windows)

    def _read_windows(self, proc, _fd) -> None:
        closer = None
        try:
            while True:
                data = proc.read(_READ_SIZE)
                if data == b"":
                    break
                if data:
                    self._post(self._on_output, data)
                else:
                    time.sleep(0.01)
                if proc.poll() is not None and closer is None:
                    # ClosePseudoConsole may wait for us to drain its output.
                    closer = threading.Thread(target=proc.close, daemon=True)
                    closer.start()
        except OSError:
            logger.debug("terminal %s: ConPTY reader stopped", self.id, exc_info=True)
        finally:
            if closer is None:
                closer = threading.Thread(target=proc.close, daemon=True)
                closer.start()
            closer.join()
            proc.closed.wait()
            code = proc.poll()
            proc.release()
            self._post(self._on_exit, _exit_code(code))

    async def kill(self) -> None:
        self._closing = True
        if self._proc is not None and self.state != STATE_EXITED:
            await asyncio.to_thread(self._proc.close)
            await self.wait_exited(3.0)
        if self._thread is not None:
            await asyncio.to_thread(self._thread.join, 3.0)

    def kill_now(self) -> None:
        if self._proc is not None:
            self._proc.close()
            if self._thread is not None:
                self._thread.join(timeout=3.0)

    def refresh_busy(self) -> bool:
        busy = False
        proc = self._proc
        if self.state != STATE_EXITED and proc is not None:
            try:
                busy = proc.busy()
            except OSError:
                pass
        changed = busy != self.busy
        self.busy = busy
        if self.state == STATE_STARTING and proc is not None and proc.poll() is None \
                and time.monotonic() - self._started_at > 2.0:
            self.state = STATE_LIVE
            changed = True
        return changed

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = cols, rows
        if self._proc is not None and self.state != STATE_EXITED:
            try:
                self._proc.resize(cols, rows)
            except OSError:
                pass

    async def write(self, data: bytes) -> None:
        async with self._write_lock:
            if self._proc is None or self.state == STATE_EXITED:
                raise TerminalError("terminal_exited", "This terminal has exited. Restart it to type again.", 409)
            try:
                await asyncio.wait_for(asyncio.to_thread(self._proc.write, data), WRITE_TIMEOUT_SECONDS)
            except asyncio.TimeoutError as exc:
                # A stuck pipe writer must not accumulate executor threads.
                await self.kill()
                raise TerminalError("terminal_unresponsive",
                                    "The terminal isn't reading input right now. Try again in a moment.", 409) from exc
            except OSError as exc:
                raise TerminalError("terminal_exited", "This terminal has exited. Restart it to type again.", 409) from exc

    def holds_bracketed_paste(self) -> bool:
        # PSReadLine doesn't offer the bash/readline guarantee used by paste.
        return False


# ---------------------------------------------------------------------------
# Paste
# ---------------------------------------------------------------------------

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\x80-\x9f]")


def clean_paste(text: str) -> str:
    """Normalise pasted text: CRLF/CR to LF, control characters (ESC, ^C,
    ^D, C1 controls ...) removed, trailing newlines stripped. Tabs and
    embedded LFs stay."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_RE.sub("", text)
    return text.rstrip("\n")


def paste_bytes(text: str, bracketed: bool, shell: str = "") -> Tuple[bytes, str]:
    """The exact bytes written for a paste and the mode used. Refuses a
    multi-line paste when the shell cannot hold it without running it.

    ``bracketed`` must mean the shell will certainly hold the paste
    (:meth:`Terminal.holds_bracketed_paste`), never just that some output
    switched the mode on. ``shell`` only picks the refusal's wording."""
    cleaned = clean_paste(text)
    if bracketed:
        return (_PASTE_START + cleaned + _PASTE_END).encode("utf-8"), "bracketed"
    if "\n" in cleaned:
        message = ("This shell can't hold a multi-line command without running it. Paste one line, or use bash."
                   if shell != "bash" else
                   "This terminal can't hold a multi-line command without running it right now "
                   "(bash's line editor or its bracketed paste is off). Paste one line.")
        raise TerminalError("paste_multiline_unsupported", message, 422)
    return cleaned.replace("\t", " ").encode("utf-8"), "plain"


# ---------------------------------------------------------------------------
# Folder resolution
# ---------------------------------------------------------------------------

def resolve_folder(session_id: Optional[str], requested: Optional[str]) -> Tuple[str, str]:
    """``(path, source)`` for a new terminal. Blocking (database reads).

    A requested folder must be an absolute, existing directory; anything else
    is refused, never replaced. Without one: the chat's worktree, else its
    project folder, else the workspace folder, else the home folder.
    """
    if requested is not None:
        path = os.path.expanduser(str(requested).strip())
        if not path or not os.path.isabs(path):
            raise TerminalError("bad_folder", "The folder must be a full path, for example /workspace/my-project.", 400)
        if not os.path.exists(path):
            raise TerminalError("bad_folder", f"The folder {path} doesn't exist on this worker.", 400)
        if not os.path.isdir(path):
            raise TerminalError("bad_folder", f"{path} is a file, not a folder.", 400)
        return os.path.normpath(path), "requested"
    if session_id:
        found = chat_folder(session_id)
        if found:
            return found
    return default_folder()


def chat_folder(session_id: str) -> Optional[Tuple[str, str]]:
    """The chat's worktree (``worktree``) or project folder (``project``) on
    this worker, or None. Raises for an unknown chat or one running elsewhere."""
    from app.db import database

    row = database.get_session(session_id)
    if not row:
        raise TerminalError("chat_not_found", "That chat doesn't exist.", 404)
    try:
        from app.network import control

        if control.owner_of(row):
            raise TerminalError("chat_on_other_worker",
                                "That chat runs on another worker. Terminals open on this worker only for now.", 409)
    except TerminalError:
        raise
    except Exception:  # noqa: BLE001 — not networked
        pass
    from app.core import project_resolver
    from app.core.config import settings

    base: Optional[str] = None
    try:
        project = project_resolver.resolve_project_v2(row)
        if project:
            base_row = project_resolver._local_checkout(project["id"], settings.effective_instance_id)
            if base_row and base_row.get("path"):
                from app.core import checkout_manager

                candidate = checkout_manager._resolve_checkout_path(base_row["path"])
                base = candidate if candidate and os.path.isdir(candidate) else None
        path = project_resolver.resolve_existing_cwd(row)
    except Exception:  # noqa: BLE001
        logger.debug("terminals: checkout lookup failed for a chat", exc_info=True)
        path = None
    if path and os.path.isdir(path):
        same = base is not None and os.path.realpath(path) == os.path.realpath(base)
        return path, ("project" if same else "worktree")
    if base:
        return base, "project"
    legacy_id = row.get("project_id")
    if legacy_id:
        try:
            from app.core.project_utils import get_project_working_dir

            project_row = database.get_project(legacy_id)
            legacy = get_project_working_dir(project_row) if project_row else None
            if legacy and os.path.isdir(legacy):
                return legacy, "project"
        except Exception:  # noqa: BLE001
            logger.debug("terminals: legacy project lookup failed", exc_info=True)
    return None


def default_folder() -> Tuple[str, str]:
    try:
        from app.core.config import settings

        workspace = str(settings.effective_workspace_dir)
        if os.path.isdir(workspace):
            return workspace, "workspace"
    except Exception:  # noqa: BLE001
        pass
    return os.path.expanduser("~"), "home"


def _clamp(value: Optional[int], bounds: Tuple[int, int], default: int) -> int:
    if value is None:
        return default
    return max(bounds[0], min(bounds[1], int(value)))


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class TerminalRegistry:
    """Every terminal on this worker. Event-loop only."""

    def __init__(self, limit: int = TERMINAL_LIMIT):
        self.limit = limit
        self._terminals: Dict[str, Terminal] = {}
        self._monitor: Optional[asyncio.Task] = None

    def list(self, session_id: Optional[str] = None) -> List[Terminal]:
        items = list(self._terminals.values())
        if session_id is not None:
            items = [t for t in items if t.session_id == session_id]
        return items

    def get(self, terminal_id: str) -> Terminal:
        found = self._terminals.get(terminal_id)
        if found is None:
            raise TerminalError("not_found", "That terminal is closed or never existed.", 404)
        return found

    @property
    def count(self) -> int:
        return len(self._terminals)

    def limit_message(self, worker_name: str) -> str:
        return f"{self.limit} terminals are open on {worker_name}. Close one to open another."

    def _require_supported(self) -> None:
        if not supported():
            raise TerminalError("not_supported", "Terminals are not supported on this worker yet.", 501)

    async def create(self, *, created_by: str, session_id: Optional[str] = None,
                     cwd: Optional[str] = None, shell: Optional[str] = None,
                     cols: Optional[int] = None, rows: Optional[int] = None,
                     cwd_root: Optional[str] = None) -> Terminal:
        self._require_supported()
        shells = available_shells()
        if shell:
            chosen = next((s for s in shells if s["name"] == shell), None)
            if chosen is None:
                names = ", ".join(s["name"] for s in shells)
                raise TerminalError("bad_shell", f"{shell} isn't available on this worker. Choose one of: {names}.", 400)
        else:
            chosen = shells[0]
        from app.db.async_database import run_sync

        worker = await run_sync(worker_identity)
        if self.count >= self.limit:
            raise TerminalError("limit_reached", self.limit_message(worker["name"]), 409)
        path, source = await run_sync(resolve_folder, session_id, cwd)
        # Re-checked after the await: two creates may have raced.
        if self.count >= self.limit:
            raise TerminalError("limit_reached", self.limit_message(worker["name"]), 409)
        terminal_class = WindowsTerminal if sys.platform == "win32" else Terminal
        terminal = terminal_class(
            shell=chosen, cwd=path, folder_source=source, session_id=session_id,
            created_by=created_by, worker_id=worker["id"],
            cols=_clamp(cols, COLS_RANGE, DEFAULT_COLS), rows=_clamp(rows, ROWS_RANGE, DEFAULT_ROWS),
        )
        terminal.cwd_root = cwd_root
        try:
            terminal.spawn()
        except OSError as exc:
            logger.warning("Terminal failed to start: shell=%s folder=%s by=%s (%s)",
                           terminal.shell, path, created_by, type(exc).__name__)
            raise TerminalError("start_failed", "The shell couldn't start on this worker.", 500) from exc
        self._terminals[terminal.id] = terminal
        logger.info("Terminal opened: id=%s shell=%s folder=%s chat=%s by=%s",
                    terminal.id, terminal.shell, path, session_id or "-", created_by)
        self._ensure_monitor()
        return terminal

    async def restart(self, terminal_id: str, *, by: str) -> Terminal:
        terminal = self.get(terminal_id)
        if terminal.state != STATE_EXITED:
            raise TerminalError("not_exited", "This terminal is still running. Close it or wait for it to exit.", 409)
        self._require_supported()
        if not os.path.isdir(terminal.cwd):
            raise TerminalError("bad_folder", f"The folder {terminal.cwd} doesn't exist on this worker anymore.", 400)
        terminal._closing = False
        try:
            terminal.spawn()
        except OSError as exc:
            raise TerminalError("start_failed", "The shell couldn't start on this worker.", 500) from exc
        logger.info("Terminal restarted: id=%s shell=%s folder=%s by=%s",
                    terminal.id, terminal.shell, terminal.cwd, by)
        terminal.broadcast_state()
        self._ensure_monitor()
        return terminal

    async def close(self, terminal_id: str, *, by: str) -> None:
        terminal = self.get(terminal_id)
        self._terminals.pop(terminal_id, None)
        await terminal.kill()
        for viewer in list(terminal.viewers):
            viewer.close("closed")
        terminal.viewers.clear()
        logger.info("Terminal closed: id=%s shell=%s folder=%s by=%s",
                    terminal.id, terminal.shell, terminal.cwd, by)

    async def paste(self, terminal_id: str, text: str) -> str:
        terminal = self.get(terminal_id)
        if len(text) > PASTE_MAX_CHARS:
            raise TerminalError("paste_too_large",
                                f"That's too much text to paste at once (limit {PASTE_MAX_CHARS // 1024} KB).", 413)
        if terminal.state == STATE_EXITED:
            raise TerminalError("terminal_exited", "This terminal has exited. Restart it to type again.", 409)
        await terminal.wait_ready()
        terminal.refresh_busy()
        if terminal.busy:
            raise TerminalError("terminal_busy",
                                "A command is running in this terminal. Paste when it's back at the prompt.", 409)
        data, mode = paste_bytes(text, terminal.holds_bracketed_paste(), terminal.shell)
        if data:
            await terminal.write(data)
        return mode

    def _ensure_monitor(self) -> None:
        if self._monitor is None or self._monitor.done():
            self._monitor = asyncio.get_running_loop().create_task(self._watch_busy())

    async def _watch_busy(self) -> None:
        while self._terminals:
            await asyncio.sleep(BUSY_POLL_SECONDS)
            for terminal in list(self._terminals.values()):
                try:
                    if terminal.refresh_busy():
                        terminal.broadcast_state()
                except Exception:  # noqa: BLE001
                    logger.debug("terminal busy check failed", exc_info=True)

    async def shutdown(self) -> None:
        """Kill every terminal's process session (app stop)."""
        terminals = list(self._terminals.values())
        self._terminals.clear()
        if self._monitor is not None:
            self._monitor.cancel()
            self._monitor = None
        for terminal in terminals:
            terminal._closing = True
            for viewer in list(terminal.viewers):
                viewer.close("shutdown")
        if terminals:
            await asyncio.gather(*(asyncio.to_thread(t.kill_now) for t in terminals), return_exceptions=True)
            logger.info("Terminals stopped with the app: %d killed", len(terminals))

    def kill_all_now(self) -> None:
        """Last-resort synchronous kill (interpreter exit)."""
        for terminal in list(self._terminals.values()):
            try:
                terminal.kill_now()
            except Exception:  # noqa: BLE001
                pass


registry = TerminalRegistry()


def _atexit_kill() -> None:
    registry.kill_all_now()


import atexit  # noqa: E402

atexit.register(_atexit_kill)
