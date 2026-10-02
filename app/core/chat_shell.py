"""Isolated shell for folder grants: one command inside a bubblewrap sandbox.

The sandbox sees the granted folder at ``/work`` (bound from the grant's open
descriptor, so a renamed or swapped path cannot redirect it), read-only system
files under ``/usr`` and the Python prefix, a private ``/proc``, ``/dev`` and a
small ``/tmp``. Nothing else of the worker is visible: no home directory, data
directory, credentials, ``/etc/passwd`` or other processes. It runs in new user,
pid, network, ipc, uts and cgroup namespaces with no network at all, cannot
create further user namespaces, and dies with the worker.

Limits apply inside the sandbox before the command starts: wall time and CPU
time, address space, file size, open files and processes. A read-only grant is
bound read-only.

There is no fallback. Without Linux, bubblewrap with the options used here,
permission to create user namespaces, a non-root worker, or a passing self-test,
every command is refused. The kernel namespaces are the boundary; no seccomp
filter is loaded (same as the code-mode sandbox in fluid.codemode.os_sandbox).
"""
from __future__ import annotations

import asyncio
import logging
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time

from app.core.chat_files import LIMIT, AccessDenied

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 60
CPU_SECONDS = 60
MEMORY_BYTES = 1024 * 1024 * 1024
FILE_BYTES = 64 * 1024 * 1024
OPEN_FILES = 256
PROCESSES = 64
TMP_BYTES = 64 * 1024 * 1024
MAX_RUNNING = 4
MOUNT_POINT = "/work"
REQUIRED_OPTIONS = ("--bind-fd", "--ro-bind-fd", "--size", "--disable-userns")

_SYSTEM_LINK_DIRS = ("/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32")
# /etc/alternatives holds Debian's tool symlinks (awk, editor, ...); the others
# are what the dynamic loader and localtime() read. Nothing else from /etc.
_SYSTEM_FILES = ("/etc/ld.so.cache", "/etc/localtime", "/etc/alternatives")
_FAILURE_TTL = 60.0
_SELFTEST_TIMEOUT = 15.0

# Runs inside the sandbox before the command. It closes every descriptor but
# stdio first: bwrap's --bind-fd source must not survive into the command,
# because a directory descriptor from outside the new root allows walking out
# through "..". The limits are set as both soft and hard, after the user
# namespace exists (RLIMIT_NPROC then counts only the sandbox's processes).
_LAUNCHER = r"""
import os, resource, sys
for name in os.listdir("/proc/self/fd"):
    if int(name) > 2:
        try:
            os.close(int(name))
        except OSError:
            pass
for key, value in zip(("CPU", "AS", "FSIZE", "NOFILE", "NPROC", "CORE"), map(int, sys.argv[1:7])):
    resource.setrlimit(getattr(resource, "RLIMIT_" + key), (value, value))
os.execv("/bin/sh", ["sh", "-c", sys.argv[7]])
"""

_lock = threading.Lock()
_result: tuple[bool, str, float] | None = None
_running = 0


def sandbox_env() -> dict[str, str]:
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
            "HOME": "/tmp", "TMPDIR": "/tmp"}


def _python_roots() -> list[str]:
    from fluid.codemode.os_sandbox import python_roots
    return python_roots(sys.executable)


def _too_broad(path: str) -> bool:
    home = os.path.realpath(os.path.expanduser("~"))
    path = os.path.realpath(path)
    return path == "/" or (home not in ("", "/") and (path == home or home.startswith(path.rstrip("/") + "/")))


def build_argv(bwrap: str, fd: int, writable: bool, command: str) -> list[str]:
    """The full argv: bwrap, then the in-sandbox launcher, then the command."""
    argv = [bwrap, "--unshare-all", "--unshare-user", "--disable-userns",
            "--die-with-parent", "--new-session", "--ro-bind", "/usr", "/usr"]
    for path in _SYSTEM_LINK_DIRS:
        if os.path.islink(path):
            argv += ["--symlink", os.readlink(path), path]
        elif os.path.isdir(path):
            argv += ["--ro-bind", path, path]
    for path in _SYSTEM_FILES:
        argv += ["--ro-bind-try", path, path]
    argv += ["--proc", "/proc", "--dev", "/dev", "--size", str(TMP_BYTES), "--tmpfs", "/tmp"]
    for root in _python_roots():
        argv += ["--ro-bind", root, root]
    argv += ["--bind-fd" if writable else "--ro-bind-fd", str(fd), MOUNT_POINT,
             "--chdir", MOUNT_POINT, "--",
             os.path.realpath(sys.executable), "-I", "-S", "-c", _LAUNCHER,
             *map(str, (CPU_SECONDS, MEMORY_BYTES, FILE_BYTES, OPEN_FILES, PROCESSES, 0)), command]
    return argv


def _bwrap() -> str | None:
    from fluid.codemode.os_sandbox import find_bwrap
    return find_bwrap()


def _preconditions() -> tuple[str | None, str]:
    """``(bwrap, "")`` when this host may try the sandbox, else ``(None, reason)``."""
    if sys.platform != "linux":
        return None, "the isolated shell is Linux-only"
    if os.geteuid() == 0:
        # A root worker's sandbox user maps to host root: files it creates in
        # the folder would be root-owned, setuid included.
        return None, "the worker runs as root"
    bwrap = _bwrap()
    if not bwrap:
        return None, "bubblewrap (bwrap) is not installed"
    try:
        help_text = subprocess.run([bwrap, "--help"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"bwrap --help failed: {type(exc).__name__}"
    text = help_text.stdout + help_text.stderr
    missing = [option for option in REQUIRED_OPTIONS if option not in text]
    if missing:
        return None, f"bwrap lacks {', '.join(missing)}"
    if any(_too_broad(root) for root in _python_roots()):
        return None, "the Python prefix is / or a home directory"
    return bwrap, ""


def _home_entry() -> str:
    """One entry of the worker's home that no read-only bind passes through
    (binding a Python prefix under ~ creates ~ itself inside the sandbox)."""
    home = os.path.realpath(os.path.expanduser("~"))
    if home in ("", "/"):
        return ""
    try:
        names = sorted(os.listdir(home))
    except OSError:
        return ""
    roots = _python_roots()
    for name in names:
        path = os.path.join(home, name)
        if not any(root == path or root.startswith(path + "/") for root in roots):
            return path
    return ""


def _probe(bwrap: str) -> tuple[bool, str]:
    """Run a real sandbox over a temporary folder and check its isolation."""
    with tempfile.TemporaryDirectory(prefix="chat-shell-probe-") as base:
        folder = os.path.join(base, "grant")
        os.mkdir(folder)
        outside = os.path.join(base, "outside")
        with open(outside, "w", encoding="utf-8") as stream:
            stream.write("outside")
        hidden = [path for path in ("/etc/passwd", outside, _home_entry()) if path and os.path.lexists(path)]
        checks = [(f"test ! -e {shlex.quote(path)}", f"host path {path} is visible") for path in hidden] + [
            # No route leaves the namespace. (Down fallback tunnel devices such
            # as sit0 may exist; they carry no address or route.)
            ("test \"$(wc -l < /proc/net/route)\" = 1 && ! grep -qv ' lo$' /proc/net/ipv6_route",
             "a network route besides loopback exists"),
            ("test \"$(ls /proc | grep -c '^[0-9]')\" -le 8", "the pid namespace is not private"),
            ("! (echo x > /usr/.chat-shell-probe) 2>/dev/null", "/usr is writable"),
            ("(true)", "the sandbox cannot start a process"),
            ("echo ok > probe", "the granted folder is not writable"),
        ]
        script = "".join(f"{check} || {{ echo {shlex.quote('self-test: ' + failure)} >&2; exit 3; }}\n"
                         for check, failure in checks) + "echo ok\n"
        fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
        try:
            out = subprocess.run(build_argv(bwrap, fd, True, script), capture_output=True, text=True,
                                 env=sandbox_env(), pass_fds=(fd,), stdin=subprocess.DEVNULL,
                                 timeout=_SELFTEST_TIMEOUT)
        except subprocess.TimeoutExpired:
            return False, "self-test timed out"
        except OSError as exc:
            return False, f"could not run bwrap: {type(exc).__name__}"
        finally:
            os.close(fd)
        try:
            with open(os.path.join(folder, "probe"), encoding="utf-8") as stream:
                wrote = stream.read().strip() == "ok"
        except OSError:
            wrote = False
    if out.returncode == 0 and out.stdout.strip() == "ok" and wrote:
        return True, ""
    lines = [line.strip() for line in (out.stderr or out.stdout).splitlines() if line.strip()]
    return False, " | ".join(lines[-3:]) or f"self-test exit status {out.returncode}"


def self_test() -> tuple[bool, str]:
    """Whether the sandbox works here. A pass is cached; a failure for a minute."""
    global _result
    with _lock:
        if _result is not None and (_result[0] or time.monotonic() - _result[2] < _FAILURE_TTL):
            return _result[0], _result[1]
        bwrap, reason = _preconditions()
        ok, reason = _probe(bwrap) if bwrap else (False, reason)
        if not ok:
            logger.warning("Isolated folder shell unavailable: %s", reason)
        _result = (ok, reason, time.monotonic())
        return ok, reason


def invalidate() -> None:
    """Forget the cached result. Called on the event loop, so it never waits
    for a self-test that holds the lock; a single assignment is atomic."""
    global _result
    _result = None


async def ensure_available() -> None:
    ok, reason = await asyncio.to_thread(self_test)
    if not ok:
        raise AccessDenied(f"This worker cannot run an isolated shell ({reason}). Use the folder file "
                           "tools instead; do not try a workaround.")


def _unescape(field: str) -> str:
    # mountinfo escapes space, tab, newline and backslash as \ooo.
    return field.encode().decode("unicode_escape").encode("latin-1").decode("utf-8", "replace")


def nested_mounts(fd: int) -> bool:
    """Whether a mount lies inside the folder ``fd`` names. bwrap binds
    recursively, so such a mount would be visible in the sandbox, unlike the
    file tools, which refuse to cross mounts."""
    path = os.readlink(f"/proc/self/fd/{fd}")
    with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as stream:
        return mounts_below(path, stream.read())


def mounts_below(path: str, mountinfo: str) -> bool:
    """Whether ``mountinfo`` lists a mount point strictly inside ``path``; a
    path that is not absolute or names a deleted folder counts as yes."""
    if not path.startswith("/") or path.endswith(" (deleted)"):
        return True
    prefix = path.rstrip("/") + "/"
    for line in mountinfo.splitlines():
        fields = line.split(" ")
        if len(fields) > 4 and _unescape(fields[4]).startswith(prefix):
            return True
    return False


async def _drain(stream) -> tuple[str, bool]:
    kept = bytearray()
    truncated = False
    while chunk := await stream.read(8192):
        room = max(0, LIMIT - len(kept))
        kept.extend(chunk[:room])
        truncated |= len(chunk) > room
    return kept.decode("utf-8", errors="replace"), truncated


async def run(fd: int, writable: bool, command: str) -> dict:
    """Run ``command`` with the folder open as ``fd`` at /work. The caller owns
    ``fd`` and keeps it open until this returns."""
    global _running
    await ensure_available()
    if await asyncio.to_thread(nested_mounts, fd):
        raise AccessDenied("This folder contains another mount or was removed; the isolated shell "
                           "does not run there.")
    bwrap = _bwrap()
    if not bwrap:
        raise AccessDenied("bubblewrap is no longer available on this worker.")
    if _running >= MAX_RUNNING:
        raise AccessDenied("Too many isolated commands are running; wait for one to finish.")
    _running += 1
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            *build_argv(bwrap, fd, writable, command), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=sandbox_env(),
            pass_fds=(fd,), start_new_session=True)
        try:
            async with asyncio.timeout(TIMEOUT_SECONDS):
                stdout, stderr = await asyncio.gather(_drain(process.stdout), _drain(process.stderr))
                code = await process.wait()
        except TimeoutError:
            raise AccessDenied(f"The command exceeded {TIMEOUT_SECONDS} seconds and the sandbox was "
                               "stopped. Changes it already made in the folder remain.") from None
        if code == 1 and stderr[0].lstrip().startswith("bwrap:"):
            invalidate()
            logger.warning("Isolated folder shell refused by bwrap: %s", stderr[0].strip()[:300])
            raise AccessDenied("The isolated shell could not start on this worker. Use the folder file tools instead.")
        return {"exit_code": code, "stdout": stdout[0], "stderr": stderr[0],
                "truncated": stdout[1] or stderr[1]}
    finally:
        _running -= 1
        if process is not None and process.returncode is None:
            # Killing outer bwrap kills the sandbox's pid namespace with it.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
