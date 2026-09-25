"""Audit-hook enforcement for the code-mode kernel child.

The builtins patches in ``_runner`` (``open``, ``io.*``, ``socket.socket``,
the guarded ``__import__``) are soft: a cell that reaches the real builtins
dict through any pre-bound module (``json.__builtins__['__import__']``) or a
``type(...).__subclasses__()`` walk gets the real ``os``, ``_socket`` and
``subprocess``. This module is the hard layer under them. ``sys.addaudithook``
cannot be removed once installed, and the interpreter raises an audit event at
every one of the operations listed in the deny tuples below no matter which
Python object the call went through.

Enforcement model (hardened after review passes 2, 8 and 9)
------------------------------------------------------------
The hook denies a listed event **in any frame** — not only frames compiled
under ``<cell>`` — once the process is *armed*. The runner arms it the first
time it hands control to model-controlled code (entering
:class:`attributed_region` around a cell, a checkpoint's pickling or a
restore's unpickling), and **it never disarms**: leaving the region does not
lower it. From then on everything the runner does is covered — the cell body
and the ``repr()`` of its result, the traceback, hint and "never attempted"
rendering of a failed cell (an exception's ``__str__``, ``__notes__``,
``__cause__``/``__context__`` chain), the stdout finishing, the checkpoint's
namespace walk and ``pickle.dump``, the restore's ``pickle.loads``, stub
re-import and ref adoption, and any ``__del__``/weakref finalizer a garbage
collection runs at an arbitrary point in the runner.

This closes the frame-attribution bypass: a cell object whose
``__repr__``/``__str__``/``__getstate__``/``__reduce__`` is a C callable
(``functools.partial(exec, …)``) executes with no ``<cell>`` frame on the
stack, so attributing by frame alone would let it reach ``os.system``. Review
pass 9 (R9-2) found the region closing before a failed cell's error was
rendered, so an exception's ``__str__`` ran unguarded; a latch that never
closes leaves no such gap. The runner performs no denied operation of its own
after boot (pipe reads and writes raise no audit event; imports fall under the
import carve-out below), so it needs no exit. Before the process is armed —
boot, and a fresh kernel's first ``set_vfs`` — the hook falls back to frame
attribution (cell frame, skill frame, or a non-main thread).

Arming is **not** a trusted/allow region: nothing re-permits a denied event.
The runner's own plumbing (the framing-pipe writes, the host-call RPC) performs
no denied event at all, so it needs no carve-out. A cell that reaches this
module and calls the arming closure only denies more; there is no disarm.

Un-neuterability (SE-1)
-----------------------
The guard holds no mutable enforcement state on any object a cell can reach:
the deny sets, the re-entrancy flag and the armed latch live only in
closure cells of the installed hook. There is no ``_INSTALLED`` module
attribute, and after install the runner drops this module from ``sys.modules``.
The hook denies ``gc.get_objects``/``gc.get_referrers``/``gc.get_referents``
and ``sys._current_frames`` from a cell, so a cell cannot enumerate live
objects to find the hook (and thence its closure cells via ``__closure__``) or
the framing pipe. ``sys.addaudithook`` stays denied (a second hook cannot
remove the first, and CPython suppresses a hook added while an existing hook
raises on that event), as do ``sys.setprofile``/``sys.settrace``.

Import carve-out
----------------
``open``/``os.listdir``/``os.scandir`` raised by importlib's file loader or by
``tokenize.open`` (linecache) are allowed when the path is a module file under
a ``sys.path`` root frozen at install time. Everything else on the list is
refused with a ``RuntimeError`` naming the operation.

Trust boundary — what this guard is and is NOT
----------------------------------------------
This guard is **defence in depth inside one process**, not a security boundary.
CPython audit hooks are the strongest same-process lever available, and this
module uses it as hard as CPython allows, but a process that runs attacker code
cannot fully sandbox itself: an object graph is reachable, the C-level real
``os``/``socket`` modules are reachable via ``type(...).__subclasses__()``
walks, and pipe writes raise no audit event, so a determined cell can still
reach the framing pipe's file object through ``__closure__`` traversal of a
tool stub it is handed. What the guard guarantees is that the dangerous *audit
events* (process spawn, native code, sockets, direct file I/O, environment
edits) are denied from cell-run code no matter how the target object was
obtained, and that its own decision state cannot be flipped from a cell.

**The real trust boundary is the process / OS sandbox.** Operators who run
untrusted models MUST run the kernel child under OS-level isolation (a seccomp
profile denying ``execve``/``socket``/``open`` beyond the RPC pipe, a separate
uid, a namespace, or a container). As of this writing the kernel ships no such
wrapper (there is no bwrap/seccomp/nsjail/firejail/docker launcher under
``fluid/`` for the kernel child — ``subprocess_kernel`` spawns a bare
``python -I -S``), so this in-process guard is the only layer unless the
deployment adds one. Do not treat it as sufficient for hostile input.

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
# spawning, signals, native code loading, registry access, environment edits,
# and the introspection primitives a cell would use to find and neuter the
# guard (gc object enumeration, the interpreter's per-thread frame table).
_ALWAYS_DENIED = (
    "os.system", "os.exec", "os.spawn", "os.posix_spawn", "os.fork",
    "os.forkpty", "os.startfile", "subprocess.Popen", "pty.spawn",
    "os.kill", "os.killpg", "signal.pthread_kill",
    "os.putenv", "os.unsetenv",
    "ctypes.", "winreg.", "os.add_dll_directory", "webbrowser.open",
    "sys.addaudithook", "sys.setprofile", "sys.settrace",
    "sys._current_frames",
    "gc.get_objects", "gc.get_referrers", "gc.get_referents",
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
    """Pure decision object for the deny rules.

    The *installed* hook (see :func:`install`) does not use an instance of this
    class for its enforcement state — that lives in closure cells so a cell can
    never reach it. This class keeps the decision logic in one testable place;
    ``denied_event``/``from_cell``/``import_exempt`` are pure given the
    immutable sets built in ``__init__``.
    """

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
        # Kept only so the standalone ``__call__`` (exercised by unit tests) has
        # a re-entrancy flag and an attribution depth. The installed hook uses
        # its own closure state instead; see :func:`install`.
        self._busy = False
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
        return bool(self._starts) and event.startswith(self._starts)

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


# Module-level flag ONLY (a bool, never the live guard): a cell that reaches
# this module finds no object carrying enforcement state. The arming control is
# a closure set by ``install``; ``attributed_region`` calls it.
_installed = False
_arm = None


class attributed_region:
    """Mark code that runs model-controlled hooks; arms deny-any-frame for good.

    The runner opens it around a cell (body, result ``repr``, error rendering,
    stdout finishing), a checkpoint and a restore's unpickling — every place a
    cell's own ``__repr__``/``__str__``/``__reduce__``/``__getstate__`` runs.
    Entering it arms the installed hook: from then on a listed audit event is
    denied regardless of which frame raised it, so a C-callable hook with no
    ``<cell>`` frame on the stack (``functools.partial(exec, …)``) is refused
    exactly as a live cell's ``os.system`` is. Leaving it does NOT disarm (see
    the module docstring: a region that closed let an exception's ``__str__``
    run unguarded, R9-2). A no-op if no guard is installed (host-side unit
    tests that never boot a child).

    This is deliberately NOT a trusted/allow region: it never re-permits a
    denied event; it only widens denial from cell frames to all frames.
    """

    def __enter__(self):
        if _arm is not None:
            _arm()
        return self

    def __exit__(self, *exc):
        return False


def install(*, allow_fs=False, allow_network=False):
    """Register the guard with the interpreter (idempotent per process).

    The enforcement state (deny sets, re-entrancy flag, armed latch,
    main-thread id, import roots) lives only in the closure of the ``hook``
    below. Nothing here stores the hook, its state, or a guard object on a
    module attribute or any other cell-reachable place; ``_runner`` additionally
    drops this module from ``sys.modules`` after calling us.
    """
    global _installed, _arm
    if _installed:
        return None

    guard = AuditGuard(allow_fs=allow_fs, allow_network=allow_network)
    # Mutable enforcement state, held ONLY in this closure. ``busy`` is the
    # re-entrancy flag; ``armed`` is the one-way deny-any-frame latch. A cell
    # cannot reach these cells: the hook object is unreachable (no module
    # attribute, gc enumeration denied), and the only control handed out
    # (``arm``) sets the latch — nothing clears it.
    state = {"busy": False, "armed": False}

    def hook(event, args):
        if state["busy"] or not guard.denied_event(event):
            return
        state["busy"] = True
        try:
            try:
                frame = sys._getframe(1)
            except ValueError:  # pragma: no cover - no caller frame
                return
            if not state["armed"] and not guard.from_cell(frame):
                return
            if guard.import_exempt(event, args, frame):
                return
        finally:
            state["busy"] = False
        raise RuntimeError(
            f"{event} is blocked in the code-mode kernel; {_advice(event)}"
        )

    def arm():
        state["armed"] = True

    sys.addaudithook(hook)
    _installed = True
    _arm = arm
    return None
