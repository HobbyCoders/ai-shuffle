from fluid.engine.base import EngineCaps, ExecutionEngine
from fluid.engine.delegated import DelegatedLoop

__all__ = [
    "DelegatedLoop",
    "EngineCaps",
    "ExecutionEngine",
]

# NativeLoop lives behind an optional import: it pulls in the code-mode stack,
# so the delegated backend (and its tests) stay importable even while that
# concurrently-developed subtree is mid-change.
try:  # pragma: no cover - exercised indirectly
    from fluid.engine.native import NativeLoop, ToolArgStreamGuard

    __all__ += ["NativeLoop", "ToolArgStreamGuard"]
except Exception:  # pragma: no cover
    pass
