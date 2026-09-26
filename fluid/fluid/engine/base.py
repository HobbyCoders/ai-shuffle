"""ExecutionEngine protocol — one interface over the native and delegated loops."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any, Protocol

from fluid.events.model import Event
from fluid.events.ops import Op


@dataclass(frozen=True)
class EngineCaps:
    code_mode: bool = False
    inproc_tools: bool = False
    fork: bool = False
    rewind: bool = False
    subagents: bool = False
    cache_metrics: bool = False
    # ``get_context_usage()`` returns a Claude-Code-parity context breakdown
    # (native computes it locally; delegated proxies the CLI's own numbers).
    context_usage: bool = False


def matches_tool_pattern(name: str, patterns: Iterable[str]) -> bool:
    """Exact name match, or a case-sensitive fnmatch pattern (ruling CE12).

    ``mcp__chrome-devtools__*`` is the motivating shape. The equality check is
    kept so a name containing ``[`` (legal in MCP tool names, a character class
    to fnmatch) still matches itself.
    """
    return any(name == p or fnmatchcase(name, p) for p in patterns)


class ExecutionEngine(Protocol):
    caps: EngineCaps

    async def submit(self, op: Op) -> None: ...
    def events(self, *, since_seq: int = 0) -> AsyncIterator[Event]: ...
    async def aclose(self) -> None: ...

    async def cancel_steer(self, steer_id: str) -> bool:
        """Recall a submitted ``Steer`` that has not reached the model yet.

        Returns True only when the steer was still pending and is now gone:
        it will never be delivered, never receipted with ``SteerDelivered``,
        and never dropped at turn end. False means there was nothing to recall
        — unknown id, or already delivered, in which case the host must treat
        the text as part of the conversation.

        The default is False: an engine that cannot recall a steer must say so
        rather than let a caller believe it un-sent something.
        """
        return False

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
