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
    """

    _PREVIEW = 200

    def __repr__(self) -> str:
        n = len(self)
        head = str.__str__(self)[: self._PREVIEW]
        more = " …" if n > self._PREVIEW else ""
        return f"<ResultRef {n} bytes; preview={head!r}{more}>"


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
            raise AttributeError(f"{self.__name__} has no tool {name!r}")
        # importlib binds the submodule as ``self.<name>`` on first load, so
        # this runs once per leaf.
        return importlib.import_module(f"{self.__name__}.{name}")

    def __dir__(self) -> list[str]:
        return sorted(set(super().__dir__()) | set(self._leaves()))

    @property
    def __all__(self) -> list[str]:  # ``from tools import *``
        return sorted(self._leaves())


def make_guarded_import(allowlist: set[str]) -> Callable[..., Any]:
    """Return an ``__import__`` permitting only ``allowlist`` + ``tools``/``skills``."""
    real_import = builtins.__import__

    def guarded(name, globals=None, locals=None, fromlist=(), level=0):
        top = name.split(".", 1)[0] if name else name
        if top in ("tools", "skills") or top in allowlist:
            return real_import(name, globals, locals, fromlist, level)
        allowed = ", ".join(sorted(allowlist))
        raise ImportError(
            f"import of {top!r} is blocked in the code-mode kernel. "
            f"Allowed: {allowed} (plus the `tools` and `skills` packages)."
        )

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
            if fullname.split(".", 1)[1] in self._sources:
                return importlib.util.spec_from_loader(fullname, self)
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


def make_namespace(*, allowlist: set[str], host_call: Callable[..., Any]) -> dict[str, Any]:
    """Build the persistent exec namespace with a guarded ``__import__``."""
    guarded_builtins = dict(vars(builtins))
    guarded_builtins["__import__"] = make_guarded_import(allowlist)
    return {
        "__builtins__": guarded_builtins,
        "__name__": "__cell__",
        "__host_call__": host_call,
        "ResultRef": ResultRef,
        "ToolError": ToolError,
        "HostCallDenied": HostCallDenied,
    }
