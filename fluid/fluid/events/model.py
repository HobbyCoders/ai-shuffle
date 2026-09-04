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
    """Base event. ``seq`` and ``ts`` are assigned by the EventBus at publish time."""

    session_id: str
    node_id: str | None = None
    turn_id: str | None = None
    seq: int = -1
    ts: float = 0.0
    """Wall clock at publish, epoch seconds — the same convention as
    ``SessionNode.ts``.

    ``seq`` orders events but says nothing about time, so a persisted stream
    could not answer how long anything actually took: the gap between a task
    finishing and its session waking was simply not measurable from the log.

    Assigned by ``EventBus.publish`` rather than by a ``default_factory``, for
    two reasons. Constructing an event is not the same instant as emitting it,
    and publish is already where ``seq`` is stamped, so both facts about the
    event's position in the stream are decided in one place. It also keeps a
    freshly constructed event equal to an identical one, which is what the
    codec round-trip tests compare.

    ``0.0`` means unstamped: an event that never went through the bus, or a row
    persisted before this field existed. Read it as "unknown", never as 1970."""

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
class SessionParked(Event):
    pending_task_ids: tuple[str, ...] = ()
    reason: str = "background_tasks"


@dataclass(frozen=True, kw_only=True)
class SessionWoken(Event):
    task_ids: tuple[str, ...] = ()
    reason: str = "background_task_completion"


@dataclass(frozen=True, kw_only=True)
class Interrupted(Event):
    message: str = "interrupted"
    # A turn-ending Interrupted carries the turn's accumulated partial
    # usage/cost — parity with the delegated leg, whose CLI result frame
    # reports total_cost_usd even when the turn was interrupted.
    usage: dict[str, Any] = field(default_factory=dict)
    cost_usd: float | None = None
    # Model calls the turn had issued before the interrupt landed. Without it,
    # a turn a user stopped books zero calls downstream even though it paid for
    # every one of them. ``None`` means "not reported" (events persisted before
    # the field existed, or an emitter that does not count calls).
    num_model_calls: int | None = None


@dataclass(frozen=True, kw_only=True)
class ErrorEvent(Event):
    message: str
    retriable: bool = False
    # Set only when the error ends the turn: the turn's accumulated partial
    # usage/cost (same parity rationale as Interrupted). Mid-turn, non-fatal
    # ErrorEvents leave these at their defaults.
    usage: dict[str, Any] = field(default_factory=dict)
    cost_usd: float | None = None
    # Model calls issued before the error ended the turn — same accounting
    # rationale as Interrupted. ``None`` means "not reported".
    num_model_calls: int | None = None


# ─── Streaming content ───────────────────────────────────────────────────────


@dataclass(frozen=True, kw_only=True)
class BlockStart(Event):
    index: int
    block_type: str  # "text" | "thinking" | "tool_use"
    tool_name: str | None = None
    tool_use_id: str | None = None
    # What KIND of block this is within its type, when the provider draws a
    # distinction the block_type does not. Today only thinking blocks carry
    # one: "reasoning_summary" is the provider's own summary of its reasoning
    # (OpenAI Responses ``summary: "auto"``, xAI included), "reasoning" is the
    # reasoning text itself. A host can label the summary as the provider's
    # rather than the model's voice. None whenever no distinction was drawn —
    # which is every provider that has only one reasoning channel, and every
    # event persisted before this field existed.
    subtype: str | None = None


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
class ToolArgsDelta(Event):
    """One fragment of a tool call's arguments, as the model writes them.

    The counterpart to :class:`TextDelta` for tool_use blocks. ``partial_json``
    is raw JSON *text*, not a parsed value — fragments only become valid JSON
    once concatenated, and the final parsed form arrives with :class:`ToolUse`.
    Consumers render progress from these and never depend on them for the
    call's actual arguments.
    """

    index: int
    tool_use_id: str
    name: str
    partial_json: str


@dataclass(frozen=True, kw_only=True)
class ToolResult(Event):
    tool_use_id: str
    name: str
    output: Any
    is_error: bool = False
    ui: Any = None  # dual output: structured payload for clients, never sent to the model


@dataclass(frozen=True, kw_only=True)
class SteerDelivered(Event):
    """Pending mid-turn steers were appended to a tool result the model will
    read on its next call — the delivery receipt for the ``Steer`` op.

    Emitted once per delivery batch, immediately after the ``ToolResult`` that
    carries the markers, and only when the text is irrevocably attached.
    ``steers`` holds one record per delivered steer in submission order:
    ``{"id": <the Steer's id, or None>, "text": <the user's text>}``. A peer's
    ``AgentMessage``, which shares this queue and this receipt, carries an
    extra ``"from"`` naming the sender; the key's absence marks the user's own.

    A host with a durable queue retires an entry the moment its id shows up
    here. An id that never arrives was never seen by the model: the engine
    drops undelivered steers at turn end (with a warning naming the ids), and
    the host should re-send that text as an ordinary turn.
    """

    steers: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True, kw_only=True)
class PermissionRequest(Event):
    request_id: str
    tool_use_id: str
    name: str
    input: Any
    risk: str = "safe"  # effective risk of this call (Bash read-only commands are "safe")
    # Why the engine is asking, when it is not simply the policy: e.g. the
    # doom-loop guard. Responders fold it into their denial text.
    reason: str | None = None


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


@dataclass(frozen=True, kw_only=True)
class HandoffCreated(Event):
    """A Handoff op finished: ``new_session_id`` starts from the handoff doc."""

    new_session_id: str
    content: str  # the handoff document (also the new session's first message)
    goal: str | None = None


@dataclass(frozen=True, kw_only=True)
class ContextCompaction(Event):
    """A batch promotion in the mechanical eviction ladder (view-level; the
    stored tree keeps full fidelity). Carries the DECISIONS, not the digest
    text — rendering is deterministic, so replaying these events rehydrates a
    byte-identical view after a restart."""

    tier: int  # highest rung this batch reached: 1 trim | 2 digest | 3 epoch
    # "trigger" (crossed the size threshold) | "overflow" (provider rejected the
    # prompt) | "image_consumed" (an image the model has already seen left the
    # context; unlike text, HistoryExpand cannot bring it back)
    reason: str
    trimmed: list[list[str]] = field(default_factory=list)  # [node_id, tool_use_id]
    spans: list[list[str]] = field(default_factory=list)  # [start, end, kind]
    reclaimed_chars: int = 0


# ─── Accounting / signals ────────────────────────────────────────────────────


@dataclass(frozen=True, kw_only=True)
class Usage(Event):
    """Per-model-call usage. ``cache_hit_rate`` = cache_read / total prompt tokens.

    ``context_usage`` is the context breakdown for THIS call — the same dict
    ``get_context_usage()`` returns (see :mod:`fluid.context.usage`), sized from
    the tokens the provider actually billed rather than the chars/4 estimate.
    None whenever the snapshot could not be taken: it is best-effort accounting,
    never a reason to fail a turn. Optional and last so events persisted before
    the field existed still rehydrate.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    raw: dict[str, Any] = field(default_factory=dict)
    context_usage: dict[str, Any] | None = None

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
class ProviderRetry(Event):
    """The engine is about to re-send the same request after a bad response.

    Emitted once per retry attempt, BEFORE the backoff sleep, so a client can
    show "retrying (attempt N of M)" while the wait is happening. Never
    terminal: either a retry eventually succeeds, or the loop gives up and
    emits the usual ErrorEvent.
    """

    attempt: int  # 1-based retry number about to be made
    max_attempts: int  # total retries allowed
    reason: str  # short human text, e.g. "empty response from provider"
    delay_seconds: float  # backoff before this retry
    model: str | None = None


@dataclass(frozen=True, kw_only=True)
class DelegatedSurface(Event):
    """The tool surface a delegated CLI announced in its ``init`` frame.

    The delegated engine does not choose the child's tools — the CLI does, from
    its own settings, flags and MCP config — so the only way to know what that
    child can actually call is to read the list it announces at startup. This
    event carries it verbatim: ``tools`` are the announced tool names,
    ``mcp_servers`` the announced server entries (each a raw dict, typically
    ``{"name": ..., "status": ...}``).

    Emitted once per ``init`` frame, i.e. once per CLI process start, not per
    turn. Purely informational — never terminal, never part of the model
    conversation.
    """

    tools: tuple[str, ...]
    mcp_servers: tuple[dict[str, Any], ...] = ()


# ``SubagentEvent.wake_disposition`` values. See the field's docstring.
WAKE_AUTO = "auto"
WAKE_SUPPRESSED = "suppressed"
WAKE_NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True, kw_only=True)
class SubagentEvent(Event):
    task_id: str
    kind: str  # started | progress | completion_pending | finished | failed | cancelled
    description: str | None = None
    summary: str | None = None
    worker_session_id: str | None = None
    wake: bool = True
    """Whether the ENGINE should run an autonomous continuation turn for this
    completion. A directive to the engine, not an observation about the world —
    ``wake=False`` does not mean no wake happened, it means this engine is not
    the thing that will perform it. Read ``wake_disposition`` instead when what
    you want to know is what actually became of the wake."""
    status: str | None = None
    wake_disposition: str = WAKE_AUTO
    """What became of the wake, for consumers that need to tell three cases
    apart which ``wake`` alone collapses into two:

    * ``"auto"`` — a terminal completion the engine will wake the parent for.
    * ``"suppressed"`` — a terminal completion where a wake IS owed but the
      host owns it: the embedding app already unparks its own session, or the
      CLI drives the continuation itself, so the engine deliberately stands
      down. Audits repeatedly misread the ``wake=False`` this produces as "the
      session never woke", and concluded a latch was stuck when nothing was.
    * ``"not_applicable"`` — a non-terminal event (``started``,
      ``completion_pending``) where waking is not a question yet. These
      previously carried ``wake=True`` purely because that is the field default,
      which was never a claim about anything.

    Additive and defaulted, so rows persisted before it existed decode as
    ``"auto"`` — which is what the old default ``wake=True`` already meant."""
    # ``started``-kind only: the task is a SERVICE meant to stay up (see
    # ``BackgroundTaskReceipt.serves``). Durable so a rehydrated engine keeps
    # the task out of park accounting across a restart.
    serves: bool = False
    # Where the worker's full report was written, when the host wrote one; it
    # rides the completion so a woken parent can open the report directly.
    # Optional and last so events persisted before the field existed rehydrate.
    report_path: str | None = None


@dataclass(frozen=True, kw_only=True)
class ContinualUpdate(Event):
    """The continual learner landed changes — invisible learning is
    indistinguishable from no learning, so every synthesis/consolidation pass
    that changes the store announces what it did. ``changes`` uses the
    synthesizer's change vocabulary: ``"<skill_name>"`` (saved),
    ``"-<skill_name>"`` (deleted), ``"memory:m3"`` (added/rewritten),
    ``"-memory:m3"`` (retired). Never terminal — clients render a line and
    move on."""

    changes: tuple[str, ...] = ()
    # What the pass cost (fluid's four token counters), so learning has a
    # visible bill.
    usage: dict[str, Any] = field(default_factory=dict)
    # Memories saved this pass whose only evidence was tool output, as
    # ``"<id>: <text>"``: stored, held out of the prompt, waiting for the user
    # to confirm (a "remember ..." does it) or ignore them.
    held: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class HookEvent(Event):
    """A configured hook spoke: ``level`` is ``"deny"`` (a hook blocked a
    call or a prompt; the model sees the same reason as an error ToolResult),
    ``"warning"`` (a hook timed out or exited with an unexpected code and the
    engine carried on), or ``"info"`` (a hook approved a call past the
    permission gate, or sent a ``systemMessage``). Never terminal — clients
    render it and move on."""

    level: str
    message: str
    hook_event: str = ""
    tool_name: str | None = None


@dataclass(frozen=True, kw_only=True)
class UnknownEvent(Event):
    """A persisted row this build cannot rebuild into a concrete event.

    Produced by :func:`event_from_dict` instead of an exception when the row's
    ``_cls`` is not registered, a required field is missing, or construction
    fails — a session written by a newer build must still load in an older
    one, with the foreign rows carried along rather than making the whole
    stream unreadable. ``cls_name`` is the original ``_cls``; ``raw`` is the
    row exactly as read, and :func:`event_to_dict` writes it back unchanged
    so a rewrite of the file loses nothing. Subscribers that switch on event
    type fall through it; it is never terminal.
    """

    cls_name: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


# ─── Durable event codec ─────────────────────────────────────────────────────
#
# The EventBus keeps an in-process log; durable replay across restarts belongs
# to the SessionStore, which serializes events with these helpers. Round-trips
# by concrete class name so ``events_since`` returns byte-identical events after
# a reload. Every row carries ``_v``, the codec schema version, so a future
# incompatible change can be told apart from a row of this shape.

SCHEMA_VERSION = 1

_EVENT_TYPES: dict[str, type[Event]] = {
    cls.__name__: cls
    for cls in (
        TurnStarted, TurnComplete, SessionParked, SessionWoken,
        Interrupted, ErrorEvent,
        BlockStart, BlockStop, TextDelta, ThinkingDelta, ToolArgsDelta,
        ToolUse, ToolResult, SteerDelivered,
        PermissionRequest, QuestionRequest, HandoffCreated,
        ContextCompaction, Usage, RateLimit, ProviderRetry, SubagentEvent, HookEvent,
        ContinualUpdate, DelegatedSurface,
    )
}


def register_event_type(cls: type[Event]) -> type[Event]:
    """Teach the codec about an event class defined outside fluid.

    An embedder that emits its own Event subclasses — or that has to keep
    decoding a shape fluid itself no longer emits — registers the class once at
    import time so ``event_from_dict`` can rebuild it. Returns the class, so it
    also works as a decorator. Re-registering the same class is a no-op; a
    different class under a name already registered is refused, because the
    codec would otherwise decode existing events into a foreign type.
    """
    name = cls.__name__
    existing = _EVENT_TYPES.get(name)
    if existing is not None and existing is not cls:
        raise ValueError(f"event type {name!r} is already registered")
    _EVENT_TYPES[name] = cls
    return cls


def event_to_dict(event: Event) -> dict[str, Any]:
    if isinstance(event, UnknownEvent):
        # Write the foreign row back exactly as it was read.
        return dict(event.raw)
    d: dict[str, Any] = {"_cls": type(event).__name__, "_v": SCHEMA_VERSION}
    for f in dataclasses.fields(event):
        d[f.name] = getattr(event, f.name)
    return d


def _unknown_event(cls_name: object, raw: dict[str, Any]) -> UnknownEvent:
    """Placeholder carrying whatever base fields the row does have."""
    def _int(v: object, default: int) -> int:
        try:
            return int(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default

    def _float(v: object, default: float) -> float:
        try:
            return float(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default

    node_id = raw.get("node_id")
    turn_id = raw.get("turn_id")
    return UnknownEvent(
        session_id=str(raw.get("session_id") or ""),
        node_id=str(node_id) if node_id is not None else None,
        turn_id=str(turn_id) if turn_id is not None else None,
        seq=_int(raw.get("seq"), -1),
        ts=_float(raw.get("ts"), 0.0),
        cls_name=str(cls_name) if cls_name is not None else "",
        raw=dict(raw),
    )


def event_from_dict(d: dict[str, Any]) -> Event:
    """Rebuild one persisted row. Never raises on a row of the wrong shape.

    An unregistered ``_cls``, a missing required field, or a value the
    dataclass refuses all decode to :class:`UnknownEvent`. Fields the target
    class does not declare are dropped, so a row written by a build that
    added a field still loads here.
    """
    raw = dict(d)
    d = dict(d)
    d.pop("_v", None)
    cls_name = d.pop("_cls", None)
    cls = _EVENT_TYPES.get(cls_name) if isinstance(cls_name, str) else None
    if cls is None:
        return _unknown_event(cls_name, raw)
    known = {f.name: f for f in dataclasses.fields(cls) if f.init}
    kwargs = {k: v for k, v in d.items() if k in known}
    for name, f in known.items():
        # JSON round-trips every tuple back as a list; the annotation (a
        # string, this module defers annotations) says which fields to
        # restore so the revived event compares equal to the original.
        value = kwargs.get(name)
        if isinstance(value, list) and str(f.type).startswith("tuple"):
            kwargs[name] = tuple(value)
        if (
            name not in kwargs
            and f.default is dataclasses.MISSING
            and f.default_factory is dataclasses.MISSING
        ):
            return _unknown_event(cls_name, raw)
    try:
        return cls(**kwargs)
    except (TypeError, ValueError):
        return _unknown_event(cls_name, raw)
