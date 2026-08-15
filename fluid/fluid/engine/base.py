"""ExecutionEngine protocol — one interface over the native and delegated loops."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator, Protocol

from fluid.events.model import Event
from fluid.events.ops import Op


@dataclass(frozen=True)
class EngineCaps:
    code_mode: bool = False
    inproc_tools: bool = False
    schema_plan_mode: bool = False
    fork: bool = False
    rewind: bool = False
    subagents: bool = False
    cache_metrics: bool = False
    # ``get_context_usage()`` returns a Claude-Code-parity context breakdown
    # (native computes it locally; delegated proxies the CLI's own numbers).
    context_usage: bool = False


class ExecutionEngine(Protocol):
    caps: EngineCaps

    async def submit(self, op: Op) -> None: ...
    def events(self, *, since_seq: int = 0) -> AsyncIterator[Event]: ...
    async def aclose(self) -> None: ...

    async def get_context_usage(self) -> dict[str, Any] | None:
        """Context-window breakdown, or ``None`` when unavailable.

        Shape (Claude Agent SDK / Claude Code parity)::

            {"categories": [{"name": str, "tokens": int, "color": str,
                             "isDeferred"?: bool}, ...],
             "totalTokens": int,
             "maxTokens": int | None,
             "rawMaxTokens": int | None,
             "percentage": float | None,   # None when maxTokens is unknown
             "model": str,
             "isAutoCompactEnabled": bool}

        A snapshot: no model request is issued and no history is mutated, so it
        is safe to call between turns.
        """
        ...
