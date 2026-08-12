"""Typed event stream — the spine of the harness.

Every observable thing the engine does is an Event. Clients (TUI, server
bridge, embedding apps, the persistence layer) subscribe to the EventBus and
receive the same stream. Events are immutable and carry a per-session
monotonic ``seq`` so any subscriber can detect gaps and replay from a
checkpoint (``EventBus.subscribe(since_seq=...)``).

Engines must emit events identically whether the loop is native or delegated;
capability differences are declared in ``EngineCaps``, never improvised in the
event shapes.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, kw_only=True)
class Event:
    """Base event. ``seq`` is assigned by the EventBus at publish time."""

    session_id: str
    node_id: str | None = None
    turn_id: str | None = None
    seq: int = -1

    @property
    def type(self) -> str:
        """Wire name: CamelCase class name → snake_case (``TextDelta`` → ``text_delta``)."""
        name = self.__class__.__name__
        out = [name[0].lower()]
        for ch in name[1:]:
            if ch.isupper():
                out.append("_")
                out.append(ch.lower())
            else:
                out.append(ch)
        return "".join(out)


# ─── Turn lifecycle ──────────────────────────────────────────────────────────


@dataclass(frozen=True, kw_only=True)
class TurnStarted(Event):
    model: str
    provider: str


@dataclass(frozen=True, kw_only=True)
class TurnComplete(Event):
    stop_reason: str
    num_model_calls: int
    usage: dict[str, Any] = field(default_factory=dict)
    cost_usd: float | None = None


@dataclass(frozen=True, kw_only=True)
class Interrupted(Event):
    message: str = "interrupted"


@dataclass(frozen=True, kw_only=True)
class ErrorEvent(Event):
    message: str
    retriable: bool = False


# ─── Streaming content ───────────────────────────────────────────────────────


@dataclass(frozen=True, kw_only=True)
class BlockStart(Event):
    index: int
    block_type: str  # "text" | "thinking" | "tool_use"
    tool_name: str | None = None
    tool_use_id: str | None = None


@dataclass(frozen=True, kw_only=True)
class BlockStop(Event):
    index: int


@dataclass(frozen=True, kw_only=True)
class TextDelta(Event):
    index: int
    text: str


@dataclass(frozen=True, kw_only=True)
class ThinkingDelta(Event):
    index: int
    text: str


# ─── Tools ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True, kw_only=True)
class ToolUse(Event):
    """A complete tool call (args fully accumulated)."""

    tool_use_id: str
    name: str
    input: Any


@dataclass(frozen=True, kw_only=True)
class ToolResult(Event):
    tool_use_id: str
    name: str
    output: Any
    is_error: bool = False
    ui: Any = None  # dual output: structured payload for clients, never sent to the model


@dataclass(frozen=True, kw_only=True)
class PermissionRequest(Event):
    request_id: str
    tool_use_id: str
    name: str
    input: Any
    risk: str = "safe"


@dataclass(frozen=True, kw_only=True)
class QuestionRequest(Event):
    """The engine needs a user-supplied answer to finish a tool call.

    Emitted once per MRTR ``input_required`` request (MCP spec 2026-07-28: a
    ``tools/call`` can pause for elicitation/sampling input). Clients answer
    with a ``RespondQuestion`` op carrying this ``request_id``; the engine
    retries the call with the collected ``inputResponses``.
    """

    request_id: str
    tool_use_id: str
    name: str  # tool being retried
    message: str  # human-facing prompt from the server's request descriptor
    schema: dict[str, Any] | None = None  # expected answer shape, if declared


# ─── Accounting / signals ────────────────────────────────────────────────────


@dataclass(frozen=True, kw_only=True)
class Usage(Event):
    """Per-model-call usage. ``cache_hit_rate`` = cache_read / total prompt tokens."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def cache_hit_rate(self) -> float:
        prompt = self.input_tokens + self.cache_read_tokens + self.cache_creation_tokens
        return self.cache_read_tokens / prompt if prompt else 0.0


@dataclass(frozen=True, kw_only=True)
class RateLimit(Event):
    kind: str
    resets_at: str | None = None
    utilization: float | None = None


@dataclass(frozen=True, kw_only=True)
class SubagentEvent(Event):
    task_id: str
    kind: str  # "started" | "progress" | "finished"
    description: str | None = None
    summary: str | None = None


# ─── Durable event codec ─────────────────────────────────────────────────────
#
# The EventBus keeps an in-process log; durable replay across restarts belongs
# to the SessionStore, which serializes events with these helpers. Round-trips
# by concrete class name so ``events_since`` returns byte-identical events after
# a reload.

_EVENT_TYPES: dict[str, type[Event]] = {
    cls.__name__: cls
    for cls in (
        TurnStarted, TurnComplete, Interrupted, ErrorEvent,
        BlockStart, BlockStop, TextDelta, ThinkingDelta,
        ToolUse, ToolResult, PermissionRequest, QuestionRequest,
        Usage, RateLimit, SubagentEvent,
    )
}


def event_to_dict(event: Event) -> dict[str, Any]:
    d: dict[str, Any] = {"_cls": type(event).__name__}
    for f in dataclasses.fields(event):
        d[f.name] = getattr(event, f.name)
    return d


def event_from_dict(d: dict[str, Any]) -> Event:
    d = dict(d)
    cls = _EVENT_TYPES[d.pop("_cls")]
    return cls(**d)
