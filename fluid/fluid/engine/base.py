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
             "isAutoCompactEnabled": bool,
             # True when totalTokens is the prompt size a provider actually
             # billed for, False when it is the chars/4 estimate. Native leg
             # only — the delegated leg proxies the CLI's dict, which lacks it.
             "measured"?: bool,
             # Native leg only — eviction-ladder stats (the delegated leg
             # proxies the CLI's dict, which lacks the key):
             "compaction"?: {"digestedSpans": int, "epochStubs": int,
                             "trimmedToolResults": int,
                             "reclaimedTokens": int} | None,
             # Per-item detail behind the category rows (each row equals the
             # sum of its list; empty source → [], never a missing key):
             "memoryFiles": [{"path": str, "type": str, "tokens": int}, ...],
             "systemTools": [{"name": str, "tokens": int}, ...],
             "mcpTools": [{"name": str, "serverName": str, "tokens": int,
                           "isLoaded": bool}, ...],   # loaded AND deferred
             "deferredBuiltinTools": [{"name": str, "tokens": int}, ...],
             "agents": [{"agentType": str, "source": str, "tokens": int}, ...]}

        Category names match Claude Code's ``/context`` labels: "System
        prompt", "Memory files", "Skills", "System tools", "MCP tools",
        "Custom agents", "Messages", "Free space", plus the excluded-from-total
        "System tools (deferred)" / "MCP tools (deferred)" rows.

        A snapshot: no model request is issued and no history is mutated, so it
        is safe to call between turns.
        """
        ...
