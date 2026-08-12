from loom.engine.base import EngineCaps, ExecutionEngine
from loom.engine.delegated import DelegatedLoop

__all__ = [
    "DelegatedLoop",
    "EngineCaps",
    "ExecutionEngine",
]

# NativeLoop lives behind an optional import: it pulls in the code-mode stack,
# so the delegated backend (and its tests) stay importable even while that
# concurrently-developed subtree is mid-change.
try:  # pragma: no cover - exercised indirectly
    from loom.engine.native import NativeLoop

    __all__.append("NativeLoop")
except Exception:  # pragma: no cover
    pass
