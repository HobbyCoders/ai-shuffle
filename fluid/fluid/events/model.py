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


#: What started a turn, as recorded on :class:`TurnStarted`.
#:
#: * ``user`` — a prompt submitted from outside the engine: the user, or a
#:   host acting for them (an API call, a failover retry of a user's prompt).
#: * ``agent_message`` — a peer's message woke an idle receiver.
#: * ``wakeup`` — a timer fired: a scheduled wakeup or a host continuation.
#: * ``unpark`` — background work came to rest and woke its parked parent.
#: * ``promotion`` — peer messages held while the parent was parked were
#:   promoted to a wake of their own.
#: * ``automation`` — a host automation started the turn on a schedule or
#:   trigger, with no person behind it.
#: A host's retry of a turn (a failover to another account) records the
#: source of the turn it retries.
TURN_SOURCES = ("user", "agent_message", "wakeup", "unpark", "promotion", "automation")


@dataclass(frozen=True, kw_only=True)
class TurnStarted(Event):
    model: str
    provider: str
    source: str | None = None
    """One of :data:`TURN_SOURCES`. ``None`` means not recorded: a row written
    before this field existed, or an emitter that does not know. Optional so a
    reader that predates it drops the key and a row without it still loads."""
    # The session's thinking setting as the host passed it (Anthropic shape),
    # None when it sends none. Recorded on every turn because a session keeps
    # the setting it started with: a resumed engine reads it back from here
    # (NativeLoop._resolve_session_thinking). Optional and last so rows
    # persisted before the field existed still load, as None.
    thinking: dict[str, Any] | None = None


@dataclass(frozen=True, kw_only=True)
class ProfileResolved(Event):
    """Which guidance family the session's model id resolved to.

    Emitted once per engine when its first turn starts, and again if the
    model changes. ``fallback`` is True when no rule recognised the id and
    the generic pack was used, so "sessions with no model targeting" can be
    counted from the event log. Every field is optional so a reader that
    predates one drops it and a row without it still loads.
    """

    model: str | None = None
    family: str | None = None
    generation: str | None = None
    matched_by: str | None = None
    fallback: bool = False


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
    # The HTTP status the provider refused the call with (a non-200 response,
    # or the code an in-stream error carried), when it gave one. A host reads
    # it the way it reads the CLI's ``api_error_status``: 429 is a rate limit,
    # 5xx a service fault. ``None`` for errors that carry no status (a dropped
    # connection, a truncated stream, an engine-side failure).
    status: int | None = None
    # The provider's own short explanation (its ``error.message``, or the
    # response body with HTML stripped), at most ~300 characters. ``message``
    # may carry the raw body; this is what a person should read.
    provider_message: str | None = None


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
    images: list[dict[str, str]] = field(default_factory=list)  # live clients and durable replay


@dataclass(frozen=True, kw_only=True)
class SteerDelivered(Event):
    """Pending mid-turn steers were appended to a tool result the model will
    read on its next call — the delivery receipt for the ``Steer`` op.

    Emitted once per delivery batch, immediately after the ``ToolResult`` that
    carries the markers, and only when the text is irrevocably attached.
    ``steers`` holds one record per delivered steer in submission order:
    ``{"id": <the Steer's id, or None>, "text": <the user's text>}``. A peer's
    ``AgentMessage``, which shares this queue and this receipt, carries an
    extra ``"from"`` naming the sender; a ``Steer`` submitted with a non-default
    ``origin`` (a harness notice) carries ``"origin"`` instead. Neither key on a
    record marks the user's own words. A user steer the delegated engine
    carried on a PostToolUse hook answer also names that tool's
    ``"tool_use_id"``: the CLI never echoes hook text, so transcript readers
    place the steer from this receipt.

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
    """A question for the host to display.

    ``nonblocking=True`` marks a durable AskUser question. Its tool already
    returned a receipt; RespondQuestion delivers the answer asynchronously.
    False preserves the blocking MRTR contract below.

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
    nonblocking: bool = False


@dataclass(frozen=True, kw_only=True)
class QuestionAnswered(Event):
    """A durable answer accepted from the host, not yet necessarily delivered."""

    request_id: str
    answer: str
    user_answered: bool = False
    wake: bool = True
    external_delivery: bool = False


@dataclass(frozen=True, kw_only=True)
class QuestionDelivered(Event):
    """Answers incorporated into model-visible history."""

    request_ids: tuple[str, ...]


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
    # context; unlike text, HistoryExpand cannot bring it back).
    # The delegated leg does not run this ladder: the claude CLI compacts its
    # own context and reports a boundary, which arrives as
    # "cli_auto_compact" / "cli_manual_compact" with tier 3 (the whole prefix
    # folded), empty ``trimmed``/``spans``, and the token counts below.
    reason: str
    trimmed: list[list[str]] = field(default_factory=list)  # [node_id, tool_use_id]
    spans: list[list[str]] = field(default_factory=list)  # [start, end, kind]
    reclaimed_chars: int = 0
    # Prompt size across the boundary, when the compactor reports it in tokens
    # rather than in decisions (the delegated leg). 0 means "not reported" —
    # including on every event persisted before these fields existed.
    before_tokens: int = 0
    after_tokens: int = 0
    # Only the last rung of a batch produces its durable host audit card. Keep
    # every rung event for lossless rehydration (image and text trims differ).
    # Defaults preserve older and delegated events as standalone boundaries.
    batch_final: bool = True
    context_window: int = 0
    # The first rung's ``before_tokens``: the size the whole batch started
    # from, so the card reads first-before → last-after. A provider-overflow
    # escalation sizes each rung on its own, so a later rung's
    # ``before_tokens`` is only what the previous rung left. 0 when unknown.
    batch_before_tokens: int = 0
    # The delegated leg only: the summary the claude CLI wrote for this
    # compaction, read back from its transcript, and ``node_id`` names the
    # last node the CLI had seen before it (``None`` between turns). The CLI's
    # transcript lives on one worker; these are what let a CLI elsewhere be
    # re-seeded from the compacted context instead of the whole tree. "" when
    # the summary could not be read, and on every older event.
    summary: str = ""
    decision_reason: str = ""
    first_changed_message: int | None = None
    # What the window decision was sized on: "measured" (the last billed
    # prompt carried forward) or "estimate" (chars/4, only when no measured
    # call is on the path). "" on provider-overflow rungs and older events.
    token_basis: str = ""
    measured_prompt_tokens: int = 0
    # The cache-priced schedule (plan item 3). ``schedule`` is the zone the
    # decision fell in ("priced", "opportunistic", "hard", "window", ...) or
    # "" when no cache signal priced it; ``cold_reason`` says why the cache
    # was presumed cold ("gap", "static_changed", "resumed") or "".
    # ``threshold_tokens`` and ``target_tokens`` are the trigger and the
    # batch target in the chars/4 units of ``before_tokens``; ``rent_paid``
    # (warm-deferral rent at the commit) and ``turn_live_tokens`` (the
    # in-flight turn's size) are measured tokens. All default to "not
    # reported", which is what every older event replays as.
    schedule: str = ""
    cold_reason: str = ""
    threshold_tokens: int = 0
    target_tokens: int = 0
    rent_paid: int = 0
    turn_live_tokens: int = 0
    # Renderer version the decisions were made under (see
    # fluid.context.steer_render). 1 on every event persisted before the field
    # existed, which keeps old sessions rendering byte-for-byte as they did.
    render_version: int = 1
    # Estimated tokens (chars/4) from ``first_changed_message`` to the end of
    # the rewritten view: the part of the prompt the provider has to bill
    # fresh on the next call. The next Usage row repeats it in ``raw`` next
    # to the cache_read it actually got. None when the batch has no
    # ``first_changed_message`` and on events written before the field.
    rewrite_tail_tokens_est: int | None = None


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
class CacheMissAlert(Event):
    """A run of prompt-cache misses fluid has ruled itself out of.

    Raised once per run, when ``consecutive`` unexplained misses in a row have
    happened inside one turn: every shared prefix checkpoint hashed identical,
    no compaction rung ran between the requests, and the provider still billed
    the whole prompt fresh. At production prefix sizes that is roughly a
    dollar a call, so the host wants to see it rather than find it in a
    month-end bill. Never terminal — the turn continues.

    ``cache_key`` is the affinity key the request carried (OpenAI Responses
    ``prompt_cache_key`` / OpenRouter ``session_id``), so a reader can tell a
    routing problem from a partitioning one.

    ``miss_class`` is one of the classes in
    :mod:`fluid.context.cache_miss_classes`. The engine also raises this
    alert, with ``reason="sticky_key_dropped"`` and class
    ``provider_fallback``, when it drops OpenRouter's sticky routing key after
    an empty completion. None on rows written before the field existed.
    """

    consecutive: int
    reason: str
    input_tokens: int = 0
    cache_read_tokens: int = 0
    provider: str = ""
    model: str = ""
    cache_key: str | None = None
    note: str = ""
    miss_class: str | None = None


@dataclass(frozen=True, kw_only=True)
class RateLimit(Event):
    kind: str
    resets_at: str | None = None
    utilization: float | None = None
    # The CLI's verdict on the window: ``allowed``, ``allowed_warning`` or
    # ``rejected``. A ``rejected`` reading may carry no utilization at all.
    status: str | None = None


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
    # Machine-readable kind of failure being retried, for hosts that show some
    # retries and hide others. ``"connection_dropped"``: the provider closed the
    # stream mid-response and the same request is being re-sent. None for the
    # other retry kinds (empty or truncated responses, runaway arguments).
    cause: str | None = None
    # Set when this retry goes out with reasoning effort lowered one step (the
    # provider cut two long responses in a row): the effort the retry uses.
    lowered_effort: str | None = None


@dataclass(frozen=True, kw_only=True)
class ProviderAlive(Event):
    """The provider has accepted this model call and its stream is open.

    Carries no content, and it says nothing about whether the model is doing
    useful work: only that the request was accepted and the connection is
    still delivering frames. It exists for the stretch where a stream is
    healthy but silent. A reasoning model whose reasoning is encrypted-only
    streams nothing visible until the reasoning item ends, and without this a
    host watching the event stream cannot tell that from a request that never
    started.

    ``signal`` names what the provider sent:

    * ``"accepted"``: the response began (``message_start``; Responses
      ``response.created``). Emitted by every provider.
    * ``"reasoning"``: a reasoning item opened (Responses
      ``output_item.added``), which may stream nothing until it finishes.
    * ``"keepalive"``: an SSE comment heartbeat on an otherwise quiet stream.

    Bounded by the engine: the first signal of each model call is published,
    and later ones only after ``NativeLoop.PROVIDER_ALIVE_INTERVAL_S`` without
    any other event. A call that streams content publishes one of these at
    most. Never terminal, never part of the model conversation.
    """

    signal: str = "accepted"


@dataclass(frozen=True, kw_only=True)
class DelegatedSurface(Event):
    """The tool surface a delegated CLI announced in its ``init`` frame.

    The delegated engine does not choose the child's tools — the CLI does, from
    its own settings, flags and MCP config — so the only way to know what that
    child can actually call is to read the list it announces at startup. This
    event carries it verbatim: ``tools`` are the announced tool names,
    ``mcp_servers`` the announced server entries (each a raw dict, typically
    ``{"name": ..., "status": ...}``).

    The same frame says what the CLI is: ``cli_version`` (its
    ``claude_code_version``), the ``permission_mode`` and ``model`` it runs,
    and ``api_key_source`` (where its credentials come from, e.g. ``none`` on
    a subscription login). ``cli_session_id`` names the CLI's own transcript.
    ``child_env_overrides`` is the environment the loop forced on the child
    (``FORCED_CHILD_ENV``), so a session records whether the bash-first steer
    override was in force. All of these default to empty, so rows persisted
    before they existed still load.

    Emitted once per ``init`` frame. In stream-json input mode the CLI writes
    an init frame at the start of every turn, not once per process, so this
    event is per turn: nothing scoped to a CLI process may be keyed on it.
    Purely informational — never terminal, never part of the model
    conversation.
    """

    tools: tuple[str, ...]
    mcp_servers: tuple[dict[str, Any], ...] = ()
    cli_version: str | None = None
    permission_mode: str | None = None
    model: str | None = None
    api_key_source: str | None = None
    cli_session_id: str | None = None
    child_env_overrides: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class DelegatedDiagnostic(Event):
    """Something the delegated engine found out about its CLI child that a
    later audit needs to see.

    ``kind`` names the finding; ``message`` says it in words; ``data`` holds
    the details. Kinds:

    * ``"bash_first_steer"``: the CLI's transcript shows it attached its
      "do your work through Bash" steer although the loop turned it off.
      ``data`` carries ``cli_version``, ``variant`` (the CLI's
      ``bashFirstSteer`` value), ``resumed`` and ``cli_session_id``.

    Stored with the session for audits; hosts that do not know it skip it.
    Never terminal, never part of the model conversation.
    """

    kind: str
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)


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
      down. The ``wake=False`` this produces does not mean the session never
      woke.
    * ``"not_applicable"`` — a non-terminal event (``started``,
      ``completion_pending``) where waking is not a question yet; their
      ``wake`` is just the field default.

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


@dataclass(frozen=True, kw_only=True)
class SwarmUpdated(Event):
    swarm: dict[str, Any] = field(default_factory=dict)


# ─── Durable event codec ─────────────────────────────────────────────────────
#
# The EventBus keeps an in-process log; durable replay across restarts belongs
# to the SessionStore, which serializes events with these helpers. Round-trips
# by concrete class name so ``events_since`` restores events after a reload.
# Derived cache reports are repaired from counters on read. Every row carries
# ``_v``, the codec schema version, so a future
# incompatible change can be told apart from a row of this shape.

SCHEMA_VERSION = 1

_EVENT_TYPES: dict[str, type[Event]] = {
    cls.__name__: cls
    for cls in (
        TurnStarted, TurnComplete, SessionParked, SessionWoken,
        ProfileResolved,
        Interrupted, ErrorEvent,
        BlockStart, BlockStop, TextDelta, ThinkingDelta, ToolArgsDelta,
        ToolUse, ToolResult, SteerDelivered,
        PermissionRequest, QuestionRequest, QuestionAnswered, QuestionDelivered, HandoffCreated,
        ContextCompaction, Usage, RateLimit, ProviderRetry, ProviderAlive,
        SubagentEvent, HookEvent,
        CacheMissAlert,
        ContinualUpdate, DelegatedSurface, DelegatedDiagnostic, SwarmUpdated,
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
    if cls is Usage and isinstance(kwargs.get("raw"), dict):
        usage_raw = kwargs["raw"]
        if "cache_read_report" in usage_raw:
            # Older stored reports used provider capabilities to interpret
            # already-normalized input. Recompute from the durable exclusive
            # counters, including plausible but incorrect full hits.
            from fluid.context.cache_probe import cache_read_report

            kwargs["raw"] = {
                **usage_raw,
                "cache_read_report": cache_read_report(kwargs, input_includes_cached=False),
            }
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
