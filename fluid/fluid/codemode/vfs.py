"""In-kernel virtual filesystem + import guard (runs *inside* the kernel).

This module is loaded by ``_runner.py`` as a top-level module (the runner is
launched with ``python -I -S <runner>``, so its own directory — ``fluid/codemode``
— is ``sys.path[0]``). It therefore imports only the standard library.

Two responsibilities:

1. **Tool VFS** — projected typed stubs live under a virtual ``tools`` package.
   ``from tools import get_weather`` compiles the stub source on demand
   (list → read → import, lazy) and, when the stub is called, routes to
   ``__host_call__`` which RPCs the host. Every spelling a model reaches for
   resolves to the same callable: ``from tools import X``, ``import tools;
   tools.X(...)``, ``import tools.X; tools.X(...)`` and the long form
   ``from tools.X import X``. Stub modules are *callable modules* that
   forward to the function they define (see :class:`_VfsPackage` and
   :class:`_LeafModule`), so ``tools.X(...)`` is never "'module' object is
   not callable". A second virtual package, ``skills``,
   mounts the persistent model-authored modules from the SkillStore (the
   continual harness) with the same dual shape; those execute under guarded
   builtins since they are untrusted model output.
2. **Import guard** — a replacement ``__import__`` installed in the exec
   namespace's builtins. Only a stdlib-safe allowlist (plus the ``tools`` and
   ``skills`` VFS packages) is importable; ``os``/``sys``/``subprocess``/
   ``socket``/``ctypes``/… raise a clear error. This is a *soft* boundary by
   design (the threat model treats the kernel as untrusted); the hard gates
   are host-side.
"""

from __future__ import annotations

import builtins
import importlib
import importlib.abc
import importlib.util
import sys
import types
from collections.abc import Callable
from typing import Any

# Stdlib-safe subset the model may import from inside the kernel. Submodules of
# these (``collections.abc``, ``os.path`` is NOT here on purpose) are allowed via
# the top-level name check. ``tools`` (the projected VFS) is always allowed.
DEFAULT_ALLOWLIST = frozenset({
    "__future__",
    "json", "re", "math", "statistics", "itertools", "functools",
    "collections", "datetime", "textwrap", "pathlib", "csv", "io",
    "base64", "hashlib", "random", "string", "typing", "dataclasses",
    "decimal", "fractions", "enum", "operator", "heapq", "bisect", "uuid",
    # Pure text/data helpers models reach for constantly; none touch the
    # filesystem, network, or process state.
    "shlex", "fnmatch", "difflib", "copy", "pprint", "unicodedata", "html",
    "ipaddress", "struct", "zlib", "time",
})



# ─── names that will never be under ``tools`` ────────────────────────────────

# The catalog tools. They are TOP-LEVEL tool calls, siblings of run_python, so
# nothing under ``tools`` will ever answer to them. Reaching for one from
# inside a cell was the most expensive small mistake in the audited code-mode
# sessions: seven of nine wrote ``from tools.read_tool_docs import
# read_tool_docs`` at the top of a batched cell and lost the whole cell to a
# bare ModuleNotFoundError with nothing in it to correct from.
# ``save_skill``/``delete_skill`` are deliberately absent: those ARE projected
# into the kernel, so ``from tools import save_skill`` is the right spelling.
TOP_LEVEL_TOOL_NAMES = frozenset({
    "list_tools", "search_tools", "read_tool_docs",
})

# Builtins the prompt names but a given host may not mount, because the
# capability behind them was never wired onto the tool context. Naming the
# capability turns "no module named tools.SendMessage" — which reads as a typo
# worth retrying — into a fact: it is absent, and no spelling will find it.
CAPABILITY_TOOL_REASONS: dict[str, str] = {
    "HistoryExpand": "no history recall capability",
    "HistorySearch": "no history recall capability",
    "ListAgents": "no agent roster capability",
    "Monitor": "no monitor spawner",
    "Recall": "no recall store",
    "Remember": "no continual journal",
    "SendMessage": "no agent messaging capability",
    "SessionSearch": "no session search capability",
    "SessionView": "no session view capability",
    "Swarm": "no swarm coordinator or task dispatch capability",
    "Task": "no task dispatch capability",
    "ToolSearch": "no tool registry capability",
}

def missing_leaf_message(pkg: str, name: str) -> str:
    """Why ``pkg.name`` does not resolve, and what to reach for instead.

    Shared by attribute access (``tools.read_tool_docs``) and the import path
    (``from tools.read_tool_docs import read_tool_docs``) so both spellings of
    the same mistake get the same correction.
    """
    kind = "tool" if pkg == "tools" else "skill"
    head = f"{pkg} has no {kind} {name!r}"
    if pkg != "tools":
        return f"{head} — `dir({pkg})` lists what is mounted here."
    if name in TOP_LEVEL_TOOL_NAMES:
        return (
            f"{head} — {name} is a top-level tool call, not an in-kernel "
            "function, and it cannot be reached from inside a cell by any "
            "import. Call it as its own tool, outside run_python. From in "
            "here use `dir(tools)` for the names mounted and "
            "`tools.<name>.__doc__` for one tool's documentation."
        )
    reason = CAPABILITY_TOOL_REASONS.get(name)
    if reason is not None:
        return (
            f"{head} — {name} is not available on this host ({reason}), so no "
            "import spelling will reach it. `dir(tools)` lists what is "
            "mounted here."
        )
    return f"{head} — `dir({pkg})` lists what is mounted here."


class ToolError(RuntimeError):
    """Raised inside the kernel when a host tool call returns an error result."""


class HostCallDenied(PermissionError):
    """Raised inside the kernel when the host permission gate denies a call."""


class ResultRef(str):
    """A large tool result that *stays in the kernel* behind a small handle.

    Subclasses ``str`` so the model can slice it programmatically
    (``ref[:200]``), measure it (``len(ref)``), and parse it (``json.loads(ref)``)
    entirely in-kernel with no further host round-trips — the persistent
    namespace keeps the full payload alive across ``run_python`` calls. Only its
    ``repr`` is truncated, so it never floods the model-facing output.

    Every ref handed to a cell is registered in the namespace's
    :class:`RefRegistry` (``_refs``) and carries that registry ``key``, which
    its ``repr`` prints. A ref the cell forgot to bind — the classic
    ``print(Read(...))`` with no assignment — is therefore never lost: the
    model reads ``#7`` off the repr it already has and recovers the payload
    with ``_refs[7]`` instead of re-running the tool.
    """

    _PREVIEW = 200

    #: Registry id, stamped by :meth:`RefRegistry.register`. ``None`` until then.
    key: int | None = None

    def __repr__(self) -> str:
        n = len(self)
        head = str.__str__(self)[: self._PREVIEW]
        more = " …" if n > self._PREVIEW else ""
        ident = f" #{self.key}:" if self.key is not None else ""
        return f"<ResultRef{ident} {n} bytes; preview={head!r}{more}>"


REF_REGISTRY_MAX = 50


class RefRegistry:
    """The cell namespace's ``_refs``: recent :class:`ResultRef`s by short id.

    Bounded and FIFO — the oldest entry is evicted past ``max_entries`` so a
    long session cannot pin every payload it ever read in kernel memory. Keys
    are monotonic, so an evicted ``#3`` is never reused for a later ref (a
    stale ``_refs[3]`` fails loudly instead of returning the wrong payload).
    """

    def __init__(self, max_entries: int = REF_REGISTRY_MAX) -> None:
        self._max = max(1, max_entries)
        self._by_key: dict[int, ResultRef] = {}
        self._next = 1

    def register(self, ref: ResultRef) -> ResultRef:
        """Stamp ``ref`` with the next key (if unstamped) and store it."""
        if getattr(ref, "key", None) is None:
            try:
                ref.key = self._next
            except AttributeError:  # pragma: no cover - defensive
                return ref
            self._next += 1
        return self.adopt(ref)

    def adopt(self, ref: ResultRef) -> ResultRef:
        """Re-register an already-keyed ref (checkpoint restore)."""
        key = getattr(ref, "key", None)
        if key is None:
            return ref
        self._by_key.pop(key, None)  # re-insert so it counts as newest
        self._by_key[key] = ref
        self._next = max(self._next, key + 1)
        while len(self._by_key) > self._max:
            self._by_key.pop(next(iter(self._by_key)))
        return ref

    def __getitem__(self, key: int) -> ResultRef:
        try:
            return self._by_key[key]
        except KeyError:
            known = ", ".join(f"#{k}" for k in sorted(self._by_key)) or "none"
            raise KeyError(
                f"no ResultRef #{key} in _refs (the last {self._max} are kept; "
                f"currently held: {known}) — re-run the tool call to get a fresh one"
            ) from None

    def __contains__(self, key: object) -> bool:
        return key in self._by_key

    def __iter__(self):
        return iter(sorted(self._by_key))

    def __len__(self) -> int:
        return len(self._by_key)

    def get(self, key: int, default: Any = None) -> Any:
        return self._by_key.get(key, default)

    def keys(self) -> list[int]:
        return sorted(self._by_key)

    def items(self) -> list[tuple[int, ResultRef]]:
        return [(k, self._by_key[k]) for k in sorted(self._by_key)]

    def values(self) -> list[ResultRef]:
        return [self._by_key[k] for k in sorted(self._by_key)]

    def __repr__(self) -> str:
        if not self._by_key:
            return "<_refs: empty — tool results over the ref threshold land here>"
        held = ", ".join(
            f"#{k} ({len(v)} bytes)" for k, v in sorted(self._by_key.items())
        )
        return f"<_refs: {len(self._by_key)} held — {held}>"


def _leaf_callable(leaf: str, module: types.ModuleType) -> Any:
    """The function a leaf module exports under its own name, or None."""
    value = getattr(module, leaf, None)
    return value if callable(value) else None


class _LeafModule(types.ModuleType):
    """A ``tools.<leaf>`` / ``skills.<leaf>`` module that is itself callable.

    After ``import tools.Glob`` importlib binds the *module* as ``tools.Glob``;
    a plain module is not callable, so ``tools.Glob(...)`` would fail with
    "'module' object is not callable". Calling this module forwards to the
    function it defines under its own name, so every spelling of the import
    reaches the tool. ``import skills.x as m; m._selftest()`` keeps working
    because it is still a module underneath.
    """

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        leaf = self.__name__.split(".", 1)[1]
        func = _leaf_callable(leaf, self)
        if func is None:
            raise TypeError(
                f"{self.__name__} defines no function named {leaf!r}; "
                f"use its attributes instead ({', '.join(n for n in dir(self) if not n.startswith('_')) or 'none'})"
            )
        return func(*args, **kwargs)


class _VfsPackage(types.ModuleType):
    """The ``tools`` / ``skills`` package object.

    ``__getattr__`` (missing attribute) imports ``<pkg>.<leaf>`` lazily, so
    ``from tools import Glob`` and ``import tools; tools.Glob(...)`` work with
    no explicit submodule import. Whichever spelling the model uses, the
    object it holds is the same :class:`_LeafModule`: callable (forwards to
    the stub function), carrying the tool's docstring, and still a module, so
    ``import skills.x as m; m._selftest()`` and attribute access on a skill
    that only exports helpers keep working. The plain function remains
    reachable as ``tools.Glob.Glob`` / ``from tools.Glob import Glob``.
    """

    def __init__(self, name: str, doc: str, leaves: Callable[[], list[str]]) -> None:
        super().__init__(name, doc)
        self._leaves = leaves
        self.__path__ = []  # mark as a (namespace-ish) package

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") or name not in self._leaves():
            raise AttributeError(self._missing_message(name))
        # importlib binds the submodule as ``self.<name>`` on first load, so
        # this runs once per leaf.
        return importlib.import_module(f"{self.__name__}.{name}")

    def _missing_message(self, name: str) -> str:
        return missing_leaf_message(self.__name__, name)

    def __dir__(self) -> list[str]:
        return sorted(set(super().__dir__()) | set(self._leaves()))

    @property
    def __all__(self) -> list[str]:  # ``from tools import *``
        return sorted(self._leaves())


# What to reach for instead, per blocked module. A bare allowlist dump tells the
# model what it may not do; these tell it what to do — the difference between a
# wasted cell and a corrected one.
BLOCKED_IMPORT_HINTS = {
    "os": (
        "use the Bash tool for shell/env work and Glob/Grep/Read for the "
        "filesystem; `pathlib` is allowed for pure path manipulation "
        "(PurePath.joinpath, .name, .suffix)"
    ),
    "sys": "there is no interpreter state to inspect here; Bash covers process work",
    "subprocess": "use the Bash tool — it is permission-gated and captures output",
    "shutil": "use the Bash tool for copy/move/remove, Glob to enumerate paths",
    "glob": "use the Glob tool",
    "tempfile": (
        "keep intermediates in kernel variables (they persist across cells); "
        "if a real file is required, Write one to an explicit path"
    ),
    "socket": "network access is blocked in the kernel",
    "requests": "network access is blocked in the kernel; use the WebFetch tool",
    "urllib": "network access is blocked in the kernel; use the WebFetch tool",
    "httpx": "network access is blocked in the kernel; use the WebFetch tool",
    "aiohttp": "network access is blocked in the kernel; use the WebFetch tool",
    "sqlite3": "run queries through the Bash tool instead",
    "asyncio": "tools are synchronous functions — call them directly, no event loop",
    "multiprocessing": "the kernel is single-process; loop in plain Python",
    "threading": "the kernel is single-threaded; loop in plain Python",
    "ctypes": "native code is blocked in the kernel",
    "importlib": "only the `tools` and `skills` packages are importable here",
    "pickle": "the kernel namespace already persists values across cells",
    "yaml": "third-party packages are not installed; parse with `re`, or shell out via Bash",
    "numpy": "third-party packages are not installed; use plain Python or shell out via Bash",
    "pandas": "third-party packages are not installed; use `csv`, or shell out via Bash",
}


def blocked_import_message(top: str, allowlist: set[str]) -> str:
    """The ImportError text for a blocked module — hint first, allowlist last."""
    hint = BLOCKED_IMPORT_HINTS.get(top)
    lead = f"import of {top!r} is blocked in the code-mode kernel"
    if hint:
        lead += f" — {hint}"
    allowed = ", ".join(sorted(allowlist))
    return f"{lead}.\nAllowed: {allowed} (plus the `tools` and `skills` packages)."


def make_guarded_import(allowlist: set[str]) -> Callable[..., Any]:
    """Return an ``__import__`` permitting only ``allowlist`` + ``tools``/``skills``."""
    real_import = builtins.__import__

    def guarded(name, globals=None, locals=None, fromlist=(), level=0):
        top = name.split(".", 1)[0] if name else name
        if top in ("tools", "skills") or top in allowlist:
            return real_import(name, globals, locals, fromlist, level)
        raise ImportError(blocked_import_message(top, allowlist))

    return guarded


class VfsFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Resolves two virtual packages: ``tools`` (host-generated stubs) and
    ``skills`` (persistent, model-authored modules from the SkillStore).

    Reads both source dicts live (shared with the runner) so ``set_vfs``
    updates take effect after the runner invalidates cached modules. Stub
    modules are host-generated and trusted; skill modules are model-authored,
    so they execute under the same *guarded builtins* as cell code (blocked
    ``open``, guarded ``__import__``) — a skill must not widen the sandbox.
    """

    def __init__(
        self,
        sources: dict[str, str],
        host_call: Callable[..., Any],
        *,
        skills: dict[str, str] | None = None,
        guarded_builtins: dict[str, Any] | None = None,
    ) -> None:
        self._sources = sources
        self._skills = skills if skills is not None else {}
        self._guarded_builtins = guarded_builtins
        self._host_call = host_call

    # ── finder ──
    def find_spec(self, fullname, path=None, target=None):
        if fullname in ("tools", "skills"):
            return importlib.util.spec_from_loader(fullname, self, is_package=True)
        if fullname.startswith("tools."):
            leaf = fullname.split(".", 1)[1]
            if leaf in self._sources:
                return importlib.util.spec_from_loader(fullname, self)
            # A plain ImportError, deliberately NOT ModuleNotFoundError:
            # ``from tools import read_tool_docs`` swallows a
            # ModuleNotFoundError raised for the submodule and then reports its
            # own generic "cannot import name", which is what left seven
            # audited cells with nothing to correct from. A plain ImportError
            # propagates, so the correction reaches the model whichever
            # spelling it used.
            if leaf in TOP_LEVEL_TOOL_NAMES or leaf in CAPABILITY_TOOL_REASONS:
                raise ImportError(missing_leaf_message("tools", leaf))
            return None
        if fullname.startswith("skills."):
            if fullname.split(".", 1)[1] in self._skills:
                return importlib.util.spec_from_loader(fullname, self)
        return None

    # ── loader ──
    def create_module(self, spec):
        if spec.name == "tools":
            return _VfsPackage(
                "tools", "Projected tool stubs (code-mode VFS).",
                lambda: list(self._sources),
            )
        if spec.name == "skills":
            return _VfsPackage(
                "skills", "Persistent model-authored skills (continual harness).",
                lambda: list(self._skills),
            )
        return _LeafModule(spec.name)

    def exec_module(self, module):
        name = module.__name__
        if name in ("tools", "skills"):
            return  # fully built by create_module
        pkg, leaf = name.split(".", 1)
        module.__host_call__ = self._host_call
        module.ResultRef = ResultRef
        module.ToolError = ToolError
        if pkg == "skills":
            # Model-authored: same guarded builtins as cell code.
            if self._guarded_builtins is not None:
                module.__dict__["__builtins__"] = self._guarded_builtins
            source = self._skills[leaf]
        else:
            source = self._sources[leaf]
        code = compile(source, f"{pkg}/{leaf}.py", "exec")
        exec(code, module.__dict__)
        # ``tools.Glob.__doc__`` reads as the tool's docs whichever object the
        # model ended up holding (module or function).
        func = _leaf_callable(leaf, module)
        if func is not None and not module.__doc__:
            module.__doc__ = func.__doc__


def install_finder(
    sources: dict[str, str],
    host_call: Callable[..., Any],
    *,
    skills: dict[str, str] | None = None,
    guarded_builtins: dict[str, Any] | None = None,
) -> VfsFinder:
    """Install the VFS finder at the front of ``sys.meta_path`` (idempotent)."""
    sys.meta_path = [f for f in sys.meta_path if not isinstance(f, VfsFinder)]
    finder = VfsFinder(
        sources, host_call, skills=skills, guarded_builtins=guarded_builtins
    )
    sys.meta_path.insert(0, finder)
    return finder


# Pre-bound in every cell so the modules models reach for reflexively never cost
# a NameError round-trip. All are on the allowlist already — this only saves the
# import line. A cell that assigns one of these names simply shadows it.
PRELUDE_MODULES = ("json", "re", "math", "itertools", "collections", "textwrap")


def _prelude(allowlist: set[str]) -> dict[str, Any]:
    ns: dict[str, Any] = {}
    for name in PRELUDE_MODULES:
        if name not in allowlist:
            continue
        try:
            ns[name] = importlib.import_module(name)
        except ImportError:  # pragma: no cover - stdlib is always present
            continue
    if "pathlib" in allowlist:
        try:
            ns["Path"] = importlib.import_module("pathlib").Path
        except (ImportError, AttributeError):  # pragma: no cover
            pass
    return ns


def make_namespace(
    *,
    allowlist: set[str],
    host_call: Callable[..., Any],
    refs: RefRegistry | None = None,
) -> dict[str, Any]:
    """Build the persistent exec namespace with a guarded ``__import__``.

    ``_refs`` (the ResultRef registry) and ``_`` (the previous cell's displayed
    value, REPL-style) are pre-bound so a model that dropped a handle always
    has a way back to the payload.
    """
    guarded_builtins = dict(vars(builtins))
    guarded_builtins["__import__"] = make_guarded_import(allowlist)
    return {
        "__builtins__": guarded_builtins,
        "__name__": "__cell__",
        "__host_call__": host_call,
        "ResultRef": ResultRef,
        "ToolError": ToolError,
        "HostCallDenied": HostCallDenied,
        "_refs": refs if refs is not None else RefRegistry(),
        "_": None,
        **_prelude(allowlist),
    }
