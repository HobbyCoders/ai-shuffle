"""In-kernel virtual filesystem + import guard (runs *inside* the kernel).

This module is loaded by ``_runner.py`` as a top-level module (the runner is
launched with ``python -I -S <runner>``, so its own directory — ``omni/codemode``
— is ``sys.path[0]``). It therefore imports only the standard library.

Two responsibilities:

1. **Tool VFS** — projected typed stubs live under a virtual ``tools`` package.
   ``from tools.get_weather import get_weather`` compiles the stub source on
   demand (list → read → import, lazy) and, when the stub is called, routes to
   ``__host_call__`` which RPCs the host.
2. **Import guard** — a replacement ``__import__`` installed in the exec
   namespace's builtins. Only a stdlib-safe allowlist (plus the ``tools`` VFS)
   is importable; ``os``/``sys``/``subprocess``/``socket``/``ctypes``/… raise a
   clear error. This is a *soft* boundary by design (the threat model treats
   the kernel as untrusted); the hard gates are host-side.
"""

from __future__ import annotations

import builtins
import importlib.abc
import importlib.util
import sys
from typing import Any, Callable

# Stdlib-safe subset the model may import from inside the kernel. Submodules of
# these (``collections.abc``, ``os.path`` is NOT here on purpose) are allowed via
# the top-level name check. ``tools`` (the projected VFS) is always allowed.
DEFAULT_ALLOWLIST = frozenset({
    "__future__",
    "json", "re", "math", "statistics", "itertools", "functools",
    "collections", "datetime", "textwrap", "pathlib", "csv", "io",
    "base64", "hashlib", "random", "string", "typing", "dataclasses",
    "decimal", "fractions", "enum", "operator", "heapq", "bisect", "uuid",
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

    def __repr__(self) -> str:  # noqa: D105
        n = len(self)
        head = str.__str__(self)[: self._PREVIEW]
        more = " …" if n > self._PREVIEW else ""
        return f"<ResultRef {n} bytes; preview={head!r}{more}>"


def make_guarded_import(allowlist: set[str]) -> Callable[..., Any]:
    """Return an ``__import__`` that permits only ``allowlist`` (+ ``tools``)."""
    real_import = builtins.__import__

    def guarded(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: A002
        top = name.split(".", 1)[0] if name else name
        if top == "tools" or top in allowlist:
            return real_import(name, globals, locals, fromlist, level)
        allowed = ", ".join(sorted(allowlist))
        raise ImportError(
            f"import of {top!r} is blocked in the code-mode kernel. "
            f"Allowed: {allowed} (plus the `tools` package)."
        )

    return guarded


class VfsFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Resolves the virtual ``tools`` package and its per-tool stub modules.

    Reads ``sources`` live (a dict shared with the runner) so ``set_vfs``
    updates take effect after the runner invalidates cached ``tools.*`` modules.
    Each stub module is given ``__host_call__`` / ``ResultRef`` / ``ToolError``
    in its namespace before its source runs.
    """

    def __init__(self, sources: dict[str, str], host_call: Callable[..., Any]) -> None:
        self._sources = sources
        self._host_call = host_call

    # ── finder ──
    def find_spec(self, fullname, path=None, target=None):  # noqa: D102
        if fullname == "tools":
            return importlib.util.spec_from_loader(fullname, self, is_package=True)
        if fullname.startswith("tools."):
            leaf = fullname.split(".", 1)[1]
            if leaf in self._sources:
                return importlib.util.spec_from_loader(fullname, self)
        return None

    # ── loader ──
    def create_module(self, spec):  # noqa: D102
        return None

    def exec_module(self, module):  # noqa: D102
        name = module.__name__
        if name == "tools":
            module.__path__ = []  # mark as a (namespace-ish) package
            module.__doc__ = "Projected tool stubs (code-mode VFS)."
            return
        leaf = name.split(".", 1)[1]
        module.__host_call__ = self._host_call
        module.ResultRef = ResultRef
        module.ToolError = ToolError
        code = compile(self._sources[leaf], f"tools/{leaf}.py", "exec")
        exec(code, module.__dict__)


def install_finder(sources: dict[str, str], host_call: Callable[..., Any]) -> VfsFinder:
    """Install the VFS finder at the front of ``sys.meta_path`` (idempotent)."""
    sys.meta_path = [f for f in sys.meta_path if not isinstance(f, VfsFinder)]
    finder = VfsFinder(sources, host_call)
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
