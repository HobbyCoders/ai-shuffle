from fluid.engine.base import EngineCaps, ExecutionEngine
from fluid.engine.delegated import ClaudeCliMissingError, DelegatedLoop

__all__ = [
    "ClaudeCliMissingError",
    "DelegatedLoop",
    "EngineCaps",
    "ExecutionEngine",
]

# NativeLoop is re-exported only when it imports: it pulls in the code-mode
# stack, and a failure there must not make the delegated backend unimportable.
# Import ``fluid.engine.native`` directly to see such an error.
try:  # pragma: no cover - exercised indirectly
    from fluid.engine.native import NativeLoop, ToolArgStreamGuard

    __all__ += ["NativeLoop", "ToolArgStreamGuard"]
except Exception:  # pragma: no cover
    pass
