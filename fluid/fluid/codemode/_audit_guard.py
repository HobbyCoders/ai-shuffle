"""Audit-hook enforcement for the code-mode kernel child.

The builtins patches in ``_runner`` (``open``, ``io.*``, ``socket.socket``,
the guarded ``__import__``) are soft: a cell that reaches the real builtins
dict through any pre-bound module (``json.__builtins__['__import__']``) gets
the real ``os``, ``_socket`` and ``subprocess``. This module is the hard
layer under them. ``sys.addaudithook`` cannot be removed once installed, and
the interpreter raises an audit event at every one of the operations listed
in the deny tuples below no matter which Python object the call went through.

The hook denies an event only when the call originates from cell code: a
frame compiled under the cell pseudo-filename, a frame running in the cell
namespace, or any thread other than the runner's main thread. The runner's
own work — checkpoint pickling, the host-call pipe, VFS module loading —
never has a cell frame on its stack and is untouched.

Two carve-outs keep allowlisted imports and traceback rendering working from
a cell: ``open``/``os.listdir``/``os.scandir`` raised by importlib's file
loader or by ``tokenize.open`` (linecache) are allowed when the path is a
module file under a ``sys.path`` root frozen at install time. Everything
else on the list is refused with a ``RuntimeError`` naming the operation.

Stdlib-only: the runner imports this as a top-level sibling module.
"""

import os
import sys
import threading

CELL_FILENAME = "<cell>"
CELL_MODULE_NAME = "__cell__"
# Saved skills are model-authored too: the VFS compiles ``skills.<name>`` as
# ``skills/<name>.py``, and a frame from one counts as cell code whether or
# not a cell frame sits below it on the stack.
SKILL_FILENAME_PREFIX = "skills/"
SKILL_MODULE_PREFIX = "skills."

# Events the child refuses from cell code regardless of config: process
# spawning, signals, native code loading, registry access, environment edits.
_ALWAYS_DENIED = (
    "os.system", "os.exec", "os.spawn", "os.posix_spawn", "os.fork",
    "os.forkpty", "os.startfile", "subprocess.Popen", "pty.spawn",
    "os.kill", "os.killpg", "signal.pthread_kill",
    "os.putenv", "os.unsetenv",
    "ctypes.", "winreg.", "os.add_dll_directory", "webbrowser.open",
    "sys.addaudithook", "sys.setprofile", "sys.settrace",
    "cpython.run_", "cpython._PySys_ClearAuditHooks",
)

# Events refused unless the kernel was configured with ``allow_network``.
_NETWORK_DENIED = (
    "socket.", "urllib.Request", "http.client.", "ftplib.", "smtplib.",
    "poplib.", "imaplib.", "nntplib.", "telnetlib.",
)

# Events refused unless the kernel was configured with ``allow_fs``. Read and
# write paths both; the projected file tools are the sanctioned route.
_FS_DENIED = (
    "open", "os.remove", "os.rename", "os.truncate", "os.chmod", "os.chown",
    "os.link", "os.symlink", "os.mkdir", "os.rmdir", "os.utime",
    "os.scandir", "os.listdir", "os.walk", "os.fwalk", "glob.glob",
    "pathlib.Path.glob", "pathlib.Path.rglob", "shutil.", "sqlite3.connect",
    "tempfile.mkstemp", "tempfile.mkdtemp",
)

# ``open`` / directory listing raised from these callers is import machinery
# or source-line lookup, not cell I/O — allowed when the path is a module
# file under a frozen root.
_IMPORT_CALLER_FILES = ("<frozen importlib._bootstrap_external>",)
_TOKENIZE_CALLER = ("tokenize.py", "open")
_MODULE_SUFFIXES = (".py", ".pyc", ".pyd", ".so", ".dylib", ".pth", ".zip")
_IMPORT_EXEMPT_EVENTS = ("open", "os.listdir", "os.scandir")


def _advice(event):
    """Which projected tool does the job the blocked call was reaching for."""
    if event in _FS_DENIED or event.startswith("shutil."):
        return (
            "direct file and directory access must go through the projected "
            "file tools (Read, Write, Glob, Grep) so the host permission gate "
            "applies"
        )
    if event.startswith(_NETWORK_DENIED):
        return (
            "the network is reached through the projected tools (WebFetch, "
            "Bash) so the host permission gate applies"
        )
    return (
        "processes, native code and the environment stay host-side; run "
        "commands through the projected Bash tool so the host permission "
        "gate applies"
    )


class AuditGuard:
    """The hook object; ``install`` registers it once per process."""

    def __init__(self, *, allow_fs, allow_network, roots=None):
        prefixes = list(_ALWAYS_DENIED)
        if not allow_network:
            prefixes += list(_NETWORK_DENIED)
        if not allow_fs:
            prefixes += list(_FS_DENIED)
        self._prefixes = tuple(prefixes)
        self._exact = frozenset(p for p in self._prefixes if not p.endswith("."))
        self._starts = tuple(p for p in self._prefixes if p.endswith("."))
        self._main_thread = threading.get_ident()
        self._roots = tuple(
            os.path.normcase(os.path.abspath(p))
            for p in (sys.path if roots is None else roots) if p
        )
        self._busy = False
        # Depth of the "treat events as cell-originated" region. Restoring a
        # checkpoint unpickles model-authored objects, and a crafted payload's
        # reconstructor (``__reduce__`` returning ``(os.system, …)``) is invoked
        # by pickle's own internals — no ``<cell>`` frame on the stack — so it
        # would otherwise slip past ``from_cell``. The runner raises this depth
        # around ``pickle.loads`` so a malicious checkpoint can do nothing a
        # live cell could not.
        self._attributed_depth = 0

    def enter_attributed(self) -> None:
        self._attributed_depth += 1

    def exit_attributed(self) -> None:
        if self._attributed_depth > 0:
            self._attributed_depth -= 1

    # ── decision, split out so it is testable without installing anything ──

    def denied_event(self, event):
        """True when ``event`` is on this guard's deny list."""
        if event in self._exact:
            return True
        return any(event.startswith(p) for p in self._starts)

    def _module_path_under_roots(self, path):
        if isinstance(path, bytes):
            path = os.fsdecode(path)
        elif not isinstance(path, str):
            path = os.fspath(path) if hasattr(path, "__fspath__") else None
        if not isinstance(path, str):
            return False
        normalized = os.path.normcase(os.path.abspath(path))
        if not any(
            normalized == root or normalized.startswith(root + os.sep)
            for root in self._roots
        ):
            return False
        return os.path.isdir(normalized) or normalized.endswith(_MODULE_SUFFIXES)

    def from_cell(self, frame):
        """True when a cell frame is anywhere below ``frame`` on the stack, or
        the current thread is not the runner's main thread."""
        if self._attributed_depth > 0:
            return True
        if threading.get_ident() != self._main_thread:
            return True
        while frame is not None:
            filename = frame.f_code.co_filename
            if filename == CELL_FILENAME or filename.startswith(SKILL_FILENAME_PREFIX):
                return True
            name = frame.f_globals.get("__name__")
            if name == CELL_MODULE_NAME or (
                isinstance(name, str) and name.startswith(SKILL_MODULE_PREFIX)
            ):
                return True
            frame = frame.f_back
        return False

    def import_exempt(self, event, args, frame):
        """Importlib or linecache reading a module file under a frozen root."""
        if event not in _IMPORT_EXEMPT_EVENTS or frame is None:
            return False
        code = frame.f_code
        caller_ok = code.co_filename in _IMPORT_CALLER_FILES or (
            code.co_filename.endswith(_TOKENIZE_CALLER[0])
            and code.co_name == _TOKENIZE_CALLER[1]
        )
        if not caller_ok:
            return False
        if event == "open" and len(args) > 1:
            mode = args[1]
            if isinstance(mode, str) and any(c in mode for c in "wax+"):
                return False
        return self._module_path_under_roots(args[0] if args else None)

    def __call__(self, event, args):
        if self._busy or not self.denied_event(event):
            return
        self._busy = True
        try:
            # Frame 1 is the Python caller of the C function that raised the
            # event; the hook's own frame is 0.
            try:
                frame = sys._getframe(1)
            except ValueError:  # pragma: no cover - no caller frame
                return
            if not self.from_cell(frame):
                return
            if self.import_exempt(event, args, frame):
                return
        finally:
            self._busy = False
        raise RuntimeError(f"{event} is blocked in the code-mode kernel; {_advice(event)}")


_INSTALLED = None


class attributed_region:
    """Attribute audit events raised inside the block to cell code.

    Used by the runner around ``pickle.loads`` when restoring a checkpoint:
    the unpickled objects are model-authored, and their reconstructors run with
    no cell frame on the stack, so without this a crafted payload would reach
    ``os.system``/sockets/``open`` that a live cell is denied. A no-op if no
    guard is installed (the host-side unit tests that never boot a child).
    """

    def __enter__(self):
        if _INSTALLED is not None:
            _INSTALLED.enter_attributed()
        return self

    def __exit__(self, *exc):
        if _INSTALLED is not None:
            _INSTALLED.exit_attributed()
        return False


def install(*, allow_fs=False, allow_network=False):
    """Register the guard with the interpreter (idempotent per process)."""
    global _INSTALLED
    if _INSTALLED is not None:
        return _INSTALLED
    guard = AuditGuard(allow_fs=allow_fs, allow_network=allow_network)
    sys.addaudithook(guard)
    _INSTALLED = guard
    return guard
