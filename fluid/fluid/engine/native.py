"""NativeLoop — the harness-owned agent loop.

Turn lifecycle:
  UserTurn → persist user node → build request (append-only prefix + cache
  plan, tool defs, plan-mode filter) → stream provider chunks → re-emit deltas
  as Events → persist assistant node → if the model stopped on tool_use,
  dispatch each tool (PreToolUse hook → PermissionEngine → dispatch →
  PostToolUse), append a ToolResult user message, and loop back for the next
  model call → else TurnComplete.

Tools/permissions/hooks are all optional: with no registry the loop behaves as
the M0 text/thinking streamer (a tool_use stop is terminal).

Empty completions are retried rather than accepted. Some providers answer a
request with HTTP 200 and nothing in it — either a terminal usage chunk
reporting zero tokens and no content, or a stream that closes before any
terminal chunk arrives (which the chat-completions client reports as an error).
Both used to end the turn, the first by persisting an empty assistant node and
stopping the agent mid-work. The loop now re-sends the same history up to
``EMPTY_COMPLETION_MAX_RETRIES`` times, emitting a ProviderRetry event before
each attempt and backing off (interruptibly) in between; only after the cap is
reached does the turn end with the usual terminal ErrorEvent. No empty
assistant node is ever written, and each failed call's usage is still folded
into the turn totals so nothing billed is lost.

A turn that ends on an ErrorEvent leaves the head on an unanswered user node —
a text prompt, or the tool results the model never got to read. The Resume op
picks the loop back up from exactly that node without appending anything, so
the retry re-sends the same history the failed call saw. Resume on a head that
is already an assistant reply has nothing to continue and ends the turn with an
ErrorEvent saying so.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import suppress
from dataclasses import dataclass, field, replace
from fnmatch import fnmatchcase
from typing import Any

from fluid.codemode.session import (
    CODE_MODE_SOURCE,
    CodeModeSession,
    register_code_mode_tools,
)
from fluid.context.cache_plan import plan_cache
from fluid.context.cache_probe import (
    CacheProbeConfig,
    diagnose_cache_miss,
    fingerprint_prefix,
)
from fluid.context.compaction import CompactionConfig, Compactor, Promotion
from fluid.context.compiler import CompiledPrompt
from fluid.context.recall import HistoryRecall
from fluid.context.trim import (
    TrimConfig,
    estimate_tokens_of_messages,
    estimate_tokens_of_text,
)
from fluid.context.usage import MemoryFileUsage, build_context_usage, memory_file_usage
from fluid.engine.background import BackgroundLifecycleMixin
from fluid.engine.base import EngineCaps
from fluid.events.bus import EventBus
from fluid.events.model import (
    BlockStart,
    BlockStop,
    ContextCompaction,
    ErrorEvent,
    Event,
    HandoffCreated,
    HookEvent,
    Interrupted,
    PermissionRequest,
    ProviderRetry,
    QuestionRequest,
    SteerDelivered,
    TextDelta,
    ThinkingDelta,
    ToolResult,
    ToolArgsDelta,
    ToolUse,
    TurnComplete,
    TurnStarted,
    Usage,
)
from fluid.events.ops import (
    BackgroundTaskFinished,
    Fork,
    Handoff,
    Interrupt,
    Op,
    RespondPermission,
    RespondQuestion,
    Resume,
    Rewind,
    SetModel,
    SetPermissionMode,
    Steer,
    UserTurn,
)
from fluid.hooks.bus import (
    POST_TOOL_USE,
    PRE_TOOL_USE,
    TURN_END,
    TURN_START,
    HookBus,
)
from fluid.messages import (
    Image,
    Message,
    Reasoning,
    Text,
    Thinking,
    ToolResultBlock,
    ToolUseBlock,
)
from fluid.permissions.engine import (
    PROMPT_DENIAL,
    PermissionEngine,
    plan_mode_blocks,
    plan_mode_denial,
    plan_mode_tools,
)
from fluid.providers.base import ProviderClient, ProviderRequest
from fluid.store.base import SessionMeta, SessionNode, SessionStore, new_id
from fluid.tools.builtins._util import resolve as resolve_tool_path
from fluid.tools.builtins.tool_search import (
    parse_deferred_catalog,
    render_deferred_catalog,
)
from fluid.tools.images import condition_image
from fluid.tools.mrtr import resolve_input_required
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolRegistry

log = logging.getLogger(__name__)

_BACKGROUND_SPAWN_INTERRUPT_GRACE = 1.0

# Opening words of the per-turn plan-mode reminder. They double as the handle
# the render path uses to drop a reminder the live flag has outlived — see
# NativeLoop._without_stale_plan_reminder.
PLAN_MODE_REMINDER_MARK = "Plan mode is active:"

# Render-time replacement for the newest stale reminder once the flag is off.
# Never written to the store: it exists only in the rendered request, so it
# vanishes on its own if plan mode is ever entered again. The wording must not
# contain PLAN_MODE_REMINDER_MARK verbatim, or the replacement would match its
# own output on the next pass.
PLAN_EXIT_REMINDER_MARK = "Plan mode is over:"
PLAN_EXIT_REMINDER = (
    "<system-reminder>\n"
    f"{PLAN_EXIT_REMINDER_MARK} plan mode has ENDED (the plan was approved, "
    "or the user switched modes) and is no longer active. Treat any earlier "
    "notice that plan mode is active as stale. Your write and shell tools "
    "are back on the surface — implement the approved plan; do not call "
    "StopPlanning or re-present the plan for approval.\n"
    "</system-reminder>"
)

# The builtins that put a file into ``ctx.read_files`` (read.py, write.py).
# Replaying their calls is what rebuilds the ledger on resume.
_READ_LEDGER_TOOLS = frozenset({"Read", "Write"})


def _read_ledger_path(block: ToolUseBlock, ctx: ToolCtx) -> str | None:
    """The read-ledger key a replayed Read/Write would have recorded."""
    if block.name not in _READ_LEDGER_TOOLS or not isinstance(block.input, dict):
        return None
    raw = block.input.get("file_path")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return str(resolve_tool_path(raw, ctx))
    except (OSError, ValueError):
        return None


# Doom-loop guard texts. The first is the in-context denial under never_ask;
# the second rides on the PermissionRequest (and any denial) when the guard
# forces a prompt, so the model learns why the call was questioned.
_DOOM_LOOP_DENIAL = (
    "doom-loop guard: this exact tool call has now been repeated several times "
    "in a row — not running it again. Change the input or the approach."
)
_DOOM_LOOP_REASON = (
    "doom-loop guard: this exact tool call was repeated several times in a row"
)


_CONTINUE_NUDGE = (
    "Your previous reply was cut off by the output token limit. Continue "
    "exactly where you stopped without repeating what you already said. "
    "Use tools to act rather than narrating long plans."
)


def _steer_parts(pending: Steer | str) -> tuple[str | None, str]:
    """Split a queued steer into ``(id, text)``.

    The queue normally holds Steer ops, but a bare string is accepted so
    callers that pushed text straight onto ``_pending_steers`` keep working;
    such a steer simply has no correlation id.
    """
    if isinstance(pending, str):
        return (None, pending)
    return (pending.id, pending.text)


@dataclass
class _CallResult:
    """Outcome of one streamed model call."""

    message: Message
    tool_uses: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)  # tokens (int) + optional cost_usd (float)
    stop_reason: str = "end_turn"
    interrupted: bool = False
    error_message: str | None = None  # set when stop_reason == "error"
    error_retriable: bool = False
    # The provider's own verdict on the upstream call when it differs from the
    # normalized stop reason (OpenRouter's native_finish_reason). "stop" with a
    # native reason of "network_error" is how a dead upstream replica looks.
    native_stop_reason: str | None = None


# Provider error-message markers that mean "the prompt no longer fits" — the
# one failure the trimmer can recover from by force-trimming and retrying.
_OVERFLOW_MARKERS = (
    "prompt is too long",
    "context_length_exceeded",
    "maximum context length",
    "exceed context limit",
    "context window",
)


def _matches_tool_pattern(name: str, patterns: Sequence[str]) -> bool:
    """Exact name match, or a case-sensitive fnmatch pattern (ruling CE12).

    ``mcp__chrome-devtools__*`` is the motivating shape; plain names carry no
    wildcards and fall out of ``fnmatchcase`` as exact comparisons anyway, but
    the equality check is kept so a name containing ``[`` (legal in MCP tool
    names, a character class to fnmatch) still matches itself.
    """
    return any(name == p or fnmatchcase(name, p) for p in patterns)


def _is_context_overflow(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _OVERFLOW_MARKERS)


def _measured_prompt_tokens(usage: dict[str, Any]) -> int:
    """What a provider actually billed for the PROMPT of one call.

    Cache reads and cache writes are prompt tokens that happen to be priced
    differently — all three keys have to be added back together to recover the
    size of the context that was sent.
    """
    total = 0
    for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        value = usage.get(key, 0)
        if isinstance(value, int) and not isinstance(value, bool):
            total += value
    return total


# ─── Empty-completion retry ──────────────────────────────────────────────────
#
# A provider can answer 200 with nothing in it. Two shapes are seen in the
# wild (both on OpenRouter): a terminal usage chunk reporting zero prompt
# tokens with no content blocks, and a stream that closes before any terminal
# chunk, which _chat_completions turns into an error chunk naming the missing
# "terminal usage chunk". Both are transient — the same request on another
# session succeeds — so the loop re-sends rather than ending the turn.

EMPTY_COMPLETION_MAX_RETRIES = 10

_EMPTY_STREAM_MARKER = "terminal usage chunk"


def _empty_retry_delay(attempt: int) -> float:
    """Backoff before retry ``attempt`` (1-based): 1, 2, 4, 8, 16, 30, 30 …

    Module-level so tests can replace it with a zero-delay stub.
    """
    return min(1.0 * 2 ** (attempt - 1), 30.0)


def _upstream_suffix(native_stop_reason: str | None) -> str:
    """Name the upstream failure when the provider reported one.

    OpenRouter normalizes a dead upstream replica to finish_reason "stop" and
    keeps the real verdict in native_finish_reason. "stop" itself says nothing
    a reader does not already know from "empty response", so it is dropped.
    """
    if not native_stop_reason or native_stop_reason == "stop":
        return ""
    return f" (upstream: {native_stop_reason})"


def _is_empty_completion(result: _CallResult) -> bool:
    """Did this call come back with no content whatsoever?

    Deliberately narrow. An interrupted call, a call carrying tool uses, and a
    call whose message has any text or thinking are all real results. A
    reasoning-only completion is not empty either: _stream_once has already
    promoted its thinking to a text block by the time this runs. What is left
    is a completion the provider billed nothing for, or one whose stream died
    before the terminal chunk — never a genuine provider error, which carries
    its own message and keeps the existing error path.
    """
    if result.interrupted or result.tool_uses:
        return False
    for block in result.message.content:
        if isinstance(block, (Text, Thinking)) and block.text:
            return False
    if result.stop_reason == "error":
        return bool(result.error_message and _EMPTY_STREAM_MARKER in result.error_message)
    return _measured_prompt_tokens(result.usage) == 0


def _plain_text(blocks: list[Any]) -> str:
    """Concatenated Text-block text of a message (for the episodic journal)."""
    return "\n".join(b.text for b in blocks if isinstance(b, Text) and b.text).strip()


LOST_TOOL_RESULT_TEXT = (
    "[interrupted: this tool call never completed (e.g. the server restarted "
    "mid-call); its result was lost]"
)


def repair_dangling_tool_calls(history: list[Message]) -> list[Message]:
    """Splice a synthetic result after every tool call that never got one.

    A transcript whose assistant message holds a tool call with no matching
    tool result is malformed, and providers reject the WHOLE request for it
    ("No tool output found for function call call_XXX" on the OpenAI dialect) —
    which poisons the session permanently: every later turn replays the same
    broken prefix. The hole appears whenever the process dies between the model
    call and the result append (a server restart or crash mid tool dispatch),
    and latently if a trimmer/view ever drops a result while keeping its call.

    Repair happens here, at request-build time, at the Message level: nothing is
    written back to the store (the tree keeps its true, incomplete shape) and
    nothing is provider-specific. The synthetic result goes DIRECTLY AFTER the
    message carrying the call — the OpenAI dialect requires a call's output to
    precede any later item — and is marked is_error so the model reads it as a
    failure rather than as output worth acting on.

    Pure and idempotent: repaired history has no dangling calls, so a second
    pass returns it unchanged (the same list object when nothing was missing).
    """
    resolved = {
        block.tool_use_id
        for message in history
        for block in message.content
        if isinstance(block, ToolResultBlock)
    }
    out: list[Message] = []
    repaired = False
    for message in history:
        out.append(message)
        missing: list[str] = []
        for block in message.content:
            if (
                isinstance(block, ToolUseBlock)
                and block.id
                and block.id not in resolved
                and block.id not in missing
            ):
                missing.append(block.id)
        if not missing:
            continue
        repaired = True
        resolved.update(missing)
        out.append(Message(
            role="user",
            content=[
                ToolResultBlock(
                    tool_use_id=call_id,
                    content=[Text(LOST_TOOL_RESULT_TEXT)],
                    is_error=True,
                )
                for call_id in missing
            ],
        ))
    return out if repaired else history


def strip_opaque_reasoning(history: list[Message]) -> list[Message]:
    """Drop every Reasoning block carrying opaque provider state (an encrypted
    blob or a verbatim-resend item).

    The recovery half of the invalid_encrypted_content self-heal (see
    _agent_loop): a history can hold blobs the current model+account cannot
    decrypt — legacy envelopes with no model tag, written before a model or
    account switch on the same provider — and one such blob 400s every request
    that carries it, permanently. Resending with the items omitted is accepted;
    only the model's memory of its prior reasoning is lost. Summary-text-only
    envelopes carry nothing opaque and stay.

    Like repair_dangling_tool_calls: request-build time, Message level, pure —
    affected messages are rebuilt, stored nodes are never mutated, and the
    same list object comes back when nothing was stripped.
    """
    out: list[Message] = []
    stripped = False
    for message in history:
        kept = [
            block for block in message.content
            if not (
                isinstance(block, Reasoning)
                and (block.encrypted_content is not None or block.raw is not None)
            )
        ]
        if len(kept) == len(message.content):
            out.append(message)
            continue
        stripped = True
        out.append(Message(
            role=message.role, content=kept, provider_meta=message.provider_meta,
        ))
    return out if stripped else history


# How much of the context window to hold back for the response when the session
# sets no explicit output cap. Only used for compaction arithmetic — it is never
# put on the wire, so an uncapped request still gets the model's full output
# allowance; this just stops the reserve from collapsing to zero.
_DEFAULT_OUTPUT_RESERVE = 32_000


# Arguments long enough to be worth previewing back to the model, short enough
# not to re-send a whole truncated cell.
_ARG_PREVIEW_CHARS = 300


_OUTPUT_BUDGET_CAUSE = (
    " The response hit its output-token limit mid-call, which is what "
    "truncated them — the payload itself is not the problem. Raise the "
    "session's max_tokens (leaving it unset gives the model its own maximum), "
    "or reissue the call carrying less text."
)


def _explain_truncation(arg_error: str, stop_reason: str | None) -> str:
    """Fold the stream's own verdict into the argument-decode failure.

    Truncated arguments have exactly two causes and they call for opposite
    responses: an exhausted output budget (raise the ceiling) or a dropped
    connection (just reissue). The stream knows which; the decoder does not, so
    the cause is attached once the terminal chunk has landed.
    """
    if stop_reason != "max_tokens":
        return arg_error
    head, _, tail = arg_error.partition("\nReceived: ")
    return f"{head}{_OUTPUT_BUDGET_CAUSE}" + (f"\nReceived: {tail}" if tail else "")


def _parse_tool_args(raw_args: str) -> tuple[dict[str, Any], str | None]:
    """Decode a tool call's accumulated argument JSON.

    Truncation here is routine, not exceptional: a model that runs out of
    output budget mid-``arguments`` — or a provider that drops the tail of a
    stream — leaves a prefix like ``{"code":"...`` behind. The bare
    ``json.loads`` this replaces raised out of the stream consumer and killed
    the whole turn with an opaque ``JSONDecodeError``, losing every completed
    step with it. Returning the failure instead lets the loop hand the model a
    tool error it can act on.
    """
    if not raw_args:
        return {}, None
    try:
        decoded = json.loads(raw_args)
    except (json.JSONDecodeError, ValueError) as exc:
        preview = raw_args[:_ARG_PREVIEW_CHARS]
        if len(raw_args) > _ARG_PREVIEW_CHARS:
            preview += "…"
        return {}, (
            f"Tool arguments were not valid JSON ({exc}) and the call was not "
            "run — they were cut off mid-value. Reissue the call.\n"
            f"Received: {preview}"
        )
    if not isinstance(decoded, dict):
        return {}, (
            "Tool arguments must be a JSON object, got "
            f"{type(decoded).__name__}. Reissue the call with named parameters."
        )
    return decoded, None


class NativeLoop(BackgroundLifecycleMixin):
    caps = EngineCaps(
        inproc_tools=True, schema_plan_mode=True,
        fork=True, rewind=True, cache_metrics=True, context_usage=True,
    )

    def __init__(
        self,
        *,
        provider: ProviderClient,
        store: SessionStore,
        bus: EventBus | None = None,
        session_id: str | None = None,
        model: str,
        system: str | None = None,
        system_sections: CompiledPrompt | None = None,
        context_window: int | None = None,
        max_tokens: int | None = None,
        effort: str | None = None,
        request_extra: dict[str, Any] | None = None,
        registry: ToolRegistry | None = None,
        permissions: PermissionEngine | None = None,
        hooks: HookBus | None = None,
        ctx: ToolCtx | None = None,
        plan_mode: bool = False,
        codemode: CodeModeSession | None = None,
        journal: Any | None = None,
        max_model_calls_per_turn: int | None = 200,
        max_continuations_per_turn: int = 3,
        trim: TrimConfig | None = None,
        compaction: CompactionConfig | None = None,
        cache_probe: CacheProbeConfig | None = None,
        mcp_servers: dict[str, dict[str, Any]] | None = None,
        agents: Sequence[dict[str, Any]] | None = None,
        allowed_tools: Sequence[str] | None = None,
        disallowed_tools: Sequence[str] | None = None,
        native_agents: bool = True,
        setting_sources: Sequence[str] | None = None,
        claude_settings: bool = False,
        claude_plugins: bool = False,
        claude_agents: bool = False,
    ) -> None:
        self.provider = provider
        self.store = store
        self.bus = bus or EventBus()
        self.model = model
        self.system = system
        # The compiled prompt's labeled sections, when the caller compiled one
        # (fluid.context.compiler). Purely descriptive — the request always
        # sends ``system`` — but it is what lets get_context_usage price memory
        # files and skills separately instead of as one opaque blob.
        self.system_sections = system_sections
        # Model context window, when the host app knows it. None → the usage
        # breakdown reports maxTokens/percentage as None rather than guessing.
        self.context_window = context_window
        # Agent-definition descriptors behind the "Custom agents" row of
        # get_context_usage() (see usage.agent_usage). Host-declared when the
        # host passed any; otherwise auto-filled from native ``.agents/agents``
        # definitions below. Dispatch remains host territory either way.
        self.agents = list(agents or ())
        self._host_declared_agents = bool(self.agents)
        self.max_tokens = max_tokens
        # Reasoning-depth knob, provider-mapped by each serializer (Anthropic
        # output_config.effort; OpenAI-shaped reasoning.effort, clamped).
        self.effort = effort
        # Host-supplied passthrough merged into every ProviderRequest.extra,
        # which each serializer spreads onto the request body verbatim. For
        # wire-level knobs fluid has no first-class field for (an OpenAI
        # ``service_tier``, a provider-specific flag). The host owns the keys
        # and their validity; fluid neither inspects nor maps them.
        self.request_extra = dict(request_extra or {})
        self.registry = registry
        self.permissions = permissions
        self.hooks = hooks
        self.ctx = ctx or ToolCtx()
        if registry is not None:
            # The ToolSearch builtin reaches the registry through the ctx.
            self.ctx.extra.setdefault("tool_registry", registry)
        # Session-static tool restriction (ruling CE12). ``allowed_tools=None``
        # means "everything"; a list makes the exposed surface exactly that set,
        # and ``disallowed_tools`` subtracts afterwards (deny beats allow). Both
        # accept exact names and fnmatch patterns. Frozen here on purpose: a
        # worker's tool surface is decided when the session is created, never
        # renegotiated mid-turn. Nothing is ever re-added — a restricted set that
        # omits an "essential" tool simply does not get it.
        self.allowed_tools: list[str] | None = (
            list(allowed_tools) if allowed_tools is not None else None
        )
        self.disallowed_tools: list[str] = list(disallowed_tools or ())
        if self.allowed_tools is not None or self.disallowed_tools:
            # The ToolSearch builtin (and any other registry-reading tool) sees
            # this session's *view* of the shared registry through this
            # predicate. The registry itself stays unfiltered — a host app may
            # share one registry across sessions with different restrictions.
            self.ctx.extra["tool_filter"] = self._tool_permitted
        self.plan_mode = plan_mode
        # The Plan/StopPlanning builtins flip the same flag the request build
        # reads, so plan mode can start and (approval-gated) end mid-session.
        # setdefault: a host wiring its own controller wins.
        from fluid.tools.builtins.plan import PlanModeControl

        self.ctx.extra.setdefault(
            "plan_mode_control",
            PlanModeControl(
                is_active=lambda: self.plan_mode,
                set_active=self._set_plan_mode,
            ),
        )
        self._codemode = codemode
        # JSON-mode episodic journal (fluid.codemode.journal.ToolJournal).
        # Code-mode sessions journal through the CodeModeSession instead; a
        # host passing both gets the codemode path (never double-journaled).
        self._journal = journal if codemode is None else None
        if codemode is not None and registry is not None:
            # Ruling T9: code-mode discovery, the projected kernel VFS, and the
            # in-kernel host-call gate all see this session's tool surface, so
            # a filtered tool is never described and never callable from code.
            # Plan mode rides in as a live predicate for the same reason: the
            # mode ends mid-turn, and a snapshot taken at construction would
            # keep the kernel read-only for the rest of the session.
            register_code_mode_tools(
                registry, codemode,
                tool_filter=self._tool_available,
                plan_mode=lambda: self.plan_mode,
            )
        # ``None`` means unlimited: the provider stays the source of truth for
        # when a turn has to stop. Every comparison below guards that case.
        self.max_model_calls_per_turn = max_model_calls_per_turn
        self.max_continuations_per_turn = max_continuations_per_turn
        self._session: SessionMeta | None = None
        self._session_id = session_id
        self._interrupted = asyncio.Event()
        # Model calls the current turn has issued so far. Mirrors the agent
        # loop's local counter so terminal events raised OUTSIDE the loop (the
        # catch-all handler in _start_and_loop) can still report the count.
        self._turn_model_calls = 0
        self._turn_task: asyncio.Task | None = None
        self._pending_permissions: dict[str, asyncio.Future] = {}
        self._pending_questions: dict[str, asyncio.Future] = {}
        # Mid-turn steers awaiting the next tool result (see the Steer op).
        self._pending_steers: list[Steer | str] = []
        # Set once a sticky-routed call comes back empty: this loop instance
        # never sends the routing key again (see _agent_loop).
        self._sticky_poisoned = False
        # Set once a call 400s with invalid_encrypted_content: this loop
        # instance strips opaque reasoning from every later request instead of
        # paying a doomed call per turn (see _agent_loop; the strip itself is
        # strip_opaque_reasoning).
        self._encrypted_reasoning_poisoned = False
        # Nested-instruction injection: instruction-file paths already injected
        # this session (each nested AGENTS-family file enters context once).
        self._nested_injected: set[str] = set()
        # Deferred-catalog injection: tool names already announced to the model
        # (each known-but-unloaded tool is listed once, on the user turn after
        # it becomes known — the static system prompt stays byte-stable).
        # Seeded from the stored history on the first turn, so a *resumed*
        # session does not replay a catalog it already contains.
        self._deferred_announced: set[str] = set()
        self._deferred_rehydrated = False
        # Read-before-edit ledger rehydration: ``ctx.read_files`` is process
        # memory, so a resumed session starts blind to every earlier Read.
        # Replayed once from the stored transcript on the first turn.
        self._read_files_rehydrated = False
        # The mechanical eviction ladder (view-level; the stored tree keeps
        # full fidelity). Sticky per engine lifetime; promotions are persisted
        # as ContextCompaction events and rehydrated on resume — see
        # context/compaction.py. A legacy ``trim=`` arg (tier-1 knobs only)
        # folds into a compaction config that inherits its enabled flag, so
        # ``trim=TrimConfig(enabled=False)`` turns context rewriting off
        # instead of silently enabling the ladder above it. To run tier-1 trim
        # *without* digests/epoch folds, pass the explicit contract:
        # ``compaction=CompactionConfig(enabled=False, trim=TrimConfig(enabled=True))``.
        if compaction is None:
            compaction = (
                CompactionConfig(trim=trim, enabled=trim.enabled)
                if trim is not None
                else CompactionConfig()
            )
        self._compactor = Compactor(config=compaction)
        # Compatibility seam: existing callers/tests reach the tier-1 trimmer.
        self._trimmer = self._compactor.trimmer
        self._compaction_rehydrated = False
        # Last model call's real prompt accounting, per session:
        # ``session_id -> (assistant node the call produced, provider usage)``.
        # get_context_usage() prefers it over the chars/4 estimate, but only
        # while that node is still on the loaded path — a fork or rewind past
        # it means the numbers describe a prompt this session no longer sends.
        self._last_call_usage: dict[str, tuple[str, dict[str, Any]]] = {}
        # Prompt-cache instrumentation (context/cache_probe.py). Every request
        # gets a short prefix fingerprint; when a call bills a big fresh prompt
        # with ~no cache read, the two fingerprints are bisected to name the
        # segment that moved. Purely diagnostic — it never changes a request.
        self._cache_probe = cache_probe or CacheProbeConfig()
        self._prefix_fingerprint: dict[str, str] | None = None
        self._prev_prefix_fingerprint: dict[str, str] | None = None
        # MCP servers from config, connected lazily on the first turn (the
        # constructor is sync; connecting needs the event loop).
        self._mcp_servers = dict(mcp_servers or {})
        self._mcp_connected = False
        # Which filesystem scopes native-agent discovery may draw from (the
        # Agent SDK's setting_sources vocabulary). None = no restriction.
        self._setting_sources = (
            tuple(setting_sources) if setting_sources is not None else None
        )
        # Deprecated Claude ecosystem keywords and observability attributes are
        # inert compatibility seams. Provider homes are never read by NativeLoop.
        self.claude_settings: dict[str, Any] = {}
        self.claude_plugins: list[Any] = []
        self.native_agents: list[Any] = []
        if native_agents:
            self._apply_native_agents()
        self.claude_agents = self.native_agents
        self._init_background_lifecycle()

    # Discovery scope → the ``source`` label carried on a usage agent row.
    _AGENT_SOURCES = {"project": "project", "global": "user", "plugin": "plugin"}

    def _apply_native_agents(self) -> None:
        """Discover native agent definitions and publish them to the session.

        Two seams, both non-dispatching (task.py doctrine — fluid discovers and
        surfaces, the host spawns):

        * ``ctx.extra["agent_registry"]`` — ``{name: AgentDefinition}`` for the
          host's Task dispatcher. Host-provided entries WIN: an app that
          declared an agent under a name a file also uses keeps its own, with a
          warning, because its dispatcher is what actually runs the thing.
        * ``self.agents`` — descriptors for the "Custom agents" usage row, but
          only when the host declared none: a host that passed its own catalog
          already priced what it injected, and adding to it would double-count.
        """
        from fluid.context.agents import discover_agents

        sources = self._setting_sources
        user_on = sources is None or "user" in sources
        project_on = sources is None or "project" in sources
        self.native_agents = discover_agents(
            self.ctx.cwd or ".",
            include_global=user_on,
            include_project=project_on,
            # Plugins live in the user scope (~/.fluid/plugins).
            include_plugins=user_on,
        )
        if not self.native_agents:
            return
        registry = self.ctx.extra.get("agent_registry")
        if not isinstance(registry, dict):
            registry = {}
            self.ctx.extra["agent_registry"] = registry
        for definition in self.native_agents:
            if definition.name in registry:
                log.warning(
                    "agent %r discovered at %s is already declared by the host — "
                    "keeping the host's definition",
                    definition.name, definition.path,
                )
                continue
            registry[definition.name] = definition
        if not self._host_declared_agents:
            self.agents = [
                {
                    "agentType": d.name,
                    "source": self._AGENT_SOURCES.get(d.scope, d.scope or "app"),
                    "text": d.body,
                }
                for d in self.native_agents
            ]

    async def _ensure_session(self) -> SessionMeta:
        if self._session is None:
            if self._session_id is not None:
                self._session = await self.store.get_session(self._session_id)
            if self._session is None:
                self._session = await self.store.create_session(
                    self._session_id, cwd=self.ctx.cwd
                )
            # The HistoryExpand/HistorySearch builtins reach the store through
            # the ctx (same seam as tool_registry) — recall re-reads originals
            # that compaction evicted from the rendered view.
            self.ctx.extra["history_recall"] = HistoryRecall(
                store=self.store, session_id=self._session.session_id
            )
        return self._session

    @property
    def session_id(self) -> str:
        assert self._session is not None, "no session yet — submit a turn first"
        return self._session.session_id

    # ─── ExecutionEngine interface ───────────────────────────────────────

    async def submit(self, op: Op) -> None:
        if isinstance(op, BackgroundTaskFinished):
            await self._finish_background_task(op)
        elif isinstance(op, UserTurn | Resume):
            if self._turn_task and not self._turn_task.done():
                raise RuntimeError("a turn is already running; Interrupt it first")
            self._interrupted.clear()
            self._turn_task = asyncio.create_task(self._run_turn(op))
        elif isinstance(op, Interrupt):
            self._interrupted.set()
        elif isinstance(op, Steer):
            # Mid-turn user text: queued here, appended to the NEXT tool
            # result inside the steering marker (fluid.context.steering) —
            # the one role-alternation-safe slot. Delivery is receipted with a
            # SteerDelivered event; if the turn ends first the steer is dropped
            # with a warning (the user can just send a message then).
            if self._turn_task is not None and not self._turn_task.done():
                self._pending_steers.append(op)
            else:
                raise RuntimeError(
                    "no turn is running — send a normal message instead of a steer"
                )
        elif isinstance(op, RespondPermission):
            fut = self._pending_permissions.get(op.request_id)
            if fut is not None and not fut.done():
                fut.set_result(op)
        elif isinstance(op, RespondQuestion):
            fut = self._pending_questions.get(op.request_id)
            if fut is not None and not fut.done():
                fut.set_result(op)
        elif isinstance(op, SetModel):
            # Hot-swap: the next _build_request reads self.model.
            self.model = op.model
        elif isinstance(op, SetPermissionMode):
            # Accept fluid's native policies directly, plus the Claude-Code-style
            # mode names clients (the CLI, embedding apps) already speak.
            mode = op.mode
            policy = mode if mode in ("never_ask", "on_write", "always_ask") else {
                "bypassPermissions": "never_ask",
                "acceptEdits": "on_write",
                "default": "on_write",
                "plan": "always_ask",
            }.get(mode, "on_write")
            if self.permissions is not None:
                self.permissions.approval_policy = policy
            # Through the injected controller, not the raw attribute: a host
            # that wrapped PlanModeControl (to persist the mode, update its own
            # UI, restore its approval policy) has to hear about a chip-driven
            # flip too, or its idea of the mode and the engine's diverge —
            # which is how a session ends up read-only with nothing left that
            # knows how to turn it back off.
            self._apply_plan_mode(mode == "plan")
        elif isinstance(op, Handoff):
            await self._do_handoff(op)
        elif isinstance(op, Fork):
            # Move the head to an earlier node; the next parent-less UserTurn
            # continues from there. Nothing is copied — the shared prefix stays
            # byte-identical, which is what makes forks cache-preserving.
            meta = await self._ensure_session()
            await self.store.set_head(meta.session_id, op.from_node_id)
            meta.head_node_id = op.from_node_id
        elif isinstance(op, Rewind):
            # Move the head back to ``to_node_id``. Nodes after it remain in the
            # tree but off-path; a subsequent turn branches a sibling.
            meta = await self._ensure_session()
            await self.store.set_head(meta.session_id, op.to_node_id)
            meta.head_node_id = op.to_node_id
        else:
            raise NotImplementedError(f"op not supported: {type(op).__name__}")

    async def _do_handoff(self, op: Handoff) -> None:
        """Amp-style handoff: ask the model for a structured handoff document
        over the current session, then seed a NEW session with it as the first
        user message. Nothing is summarized in place — the old session stays
        intact; the new one starts small, with a fresh cacheable prefix.
        """
        meta = await self._ensure_session()
        nodes = await self.store.load_path(meta.session_id)

        # Bounded plain-text transcript for the summary call (newest last).
        lines: list[str] = []
        for node in nodes[-40:]:
            text = node.message.text().strip()
            if text:
                lines.append(f"{node.message.role}: {text[:2000]}")
        goal_line = f"\nThe stated goal for the next session: {op.goal}" if op.goal else ""
        prompt = (
            "Write a handoff document for a fresh agent session continuing this "
            "work. Structure it as: Goal, Current state (what is done and "
            "verified), Key decisions and constraints, Relevant files/paths, "
            "Next steps (ordered). Be specific; the reader has NO other "
            f"context.{goal_line}\n\n--- transcript ---\n" + "\n".join(lines)
        )
        request = ProviderRequest(
            model=self.model,
            messages=[Message(role="user", content=[Text(prompt)])],
            system="You write precise engineering handoff documents.",
            tools=[],
            max_tokens=self.max_tokens,
            cache_breakpoints=0,
        )
        parts: list[str] = []
        async for chunk in self.provider.stream(request):
            if chunk.kind == "text_delta":
                parts.append(chunk.payload["text"])
        content = "".join(parts).strip() or "(handoff document generation failed)"

        new_meta = await self.store.create_session(cwd=self.ctx.cwd)
        first = SessionNode(
            node_id=new_id("node"), parent_id=None,
            message=Message(role="user", content=[Text(content)]),
        )
        await self.store.append(new_meta.session_id, first)
        await self._emit(HandoffCreated(
            session_id=meta.session_id,
            new_session_id=new_meta.session_id,
            content=content,
            goal=op.goal,
        ))

    async def _emit(self, event: Event) -> Event:
        """Publish to the bus (assigns seq) and persist for durable replay."""
        sequenced = await self.bus.publish(event)
        await self.store.append_event(event.session_id, sequenced)
        return sequenced

    def events(self, *, since_seq: int = 0) -> AsyncIterator[Event]:
        assert self._session is not None, "no session yet — submit a turn first"
        return self.bus.subscribe(self.session_id, since_seq=since_seq)

    async def wait_turn(self) -> None:
        await self._wait_background_turns()

    async def cancel_steer(self, steer_id: str) -> bool:
        """Recall a pending ``Steer`` before it reaches the model.

        Returns True when the steer was still queued and is now removed: it
        will never ride out on a tool result, never produce a
        ``SteerDelivered`` receipt, and never show up in the turn-end drop log.
        False means there was nothing to recall — an unknown id, or a steer
        already folded into a tool result, which is part of the conversation
        from that point on and cannot be un-sent.

        The decision is atomic against delivery: this coroutine and
        ``_finish_tool`` both run on the engine's event loop and neither
        suspends between reading and clearing ``_pending_steers``, so a steer
        is either cancelled here or already delivered there — never both, never
        half of each.

        Bare-string entries in the queue carry no id and so never match.
        """
        pending = self._pending_steers
        keep = [
            op for op in pending
            if not (isinstance(op, Steer) and op.id == steer_id)
        ]
        if len(keep) == len(pending):
            return False
        pending[:] = keep  # in place: _finish_tool may hold this exact list
        return True

    async def aclose(self) -> None:
        await self._close_background_lifecycle()
        self._interrupted.set()
        if self._turn_task and not self._turn_task.done():
            self._turn_task.cancel()
            try:
                await self._turn_task
            except asyncio.CancelledError:
                pass
        if self._codemode is not None:
            await self._codemode.aclose()
        if self._journal is not None:
            await self._journal.aclose()
        await self.bus.close()

    # ─── Context-usage snapshot ──────────────────────────────────────────

    def memory_file_usage(self) -> list[MemoryFileUsage]:
        """Per-instruction-file token estimates behind the "Memory files"
        category, which carries only their total. Same list the breakdown
        publishes as ``memoryFiles``, in :class:`MemoryFileUsage` form."""
        if self.system_sections is None:
            return []
        return memory_file_usage(self.system_sections.sections)

    async def get_context_usage(self) -> dict[str, Any] | None:
        """Claude-Code-parity breakdown of what the NEXT request would carry.

        A snapshot, safe between turns: it reads the store, renders the history
        through the trimmer's read-only view (:meth:`ToolResultTrimmer.view` —
        no batch is armed, nothing is trimmed as a side effect) and prices the
        same tool schemas ``_build_request`` would send. No model call is made
        and no node is written; a session that does not exist yet simply prices
        as an empty history rather than being created here.

        The total is the last call's REAL prompt size whenever one is available
        for the current path (see ``_last_call_usage``); with no call yet — or
        after a fork/rewind past it — it falls back to the chars/4 estimate.

        See :mod:`fluid.context.usage` for the category rules and the palette.
        """
        history: list[Message] = []
        compaction_stats: dict[str, int] | None = None
        measured: int | None = None
        # Resolve the session READ-ONLY: _ensure_session would create one, and
        # asking what the context looks like must not bring a session into being.
        sid = self._session.session_id if self._session is not None else None
        if sid is None and self._session_id is not None:
            sid = self._session_id if await self.store.get_session(self._session_id) else None
        if sid is not None:
            nodes = await self.store.load_path(sid)
            history = repair_dangling_tool_calls(self._compactor.view(nodes))
            # ``stats`` prices the compacted render unconditionally (the intake
            # stub is a stateless view policy inside ``Compactor._render``), so
            # with context rewriting disabled it would report reclaimed tokens
            # the request never actually saves. Report nothing instead.
            if self._compactor.active:
                compaction_stats = self._compactor.stats(nodes)
            recorded = self._last_call_usage.get(sid)
            if recorded is not None and any(n.node_id == recorded[0] for n in nodes):
                measured = _measured_prompt_tokens(recorded[1])
        return self._context_usage_snapshot(
            history=history,
            compaction=compaction_stats,
            measured_prompt_tokens=measured,
        )

    def _deferred_tools_api(self) -> list[dict[str, Any]]:
        """Schemas of known-but-unloaded tools — the ToolSearch catalog."""
        if self.registry is None or self._codemode is not None:
            return []
        return [
            s.to_api() for s in self.registry.deferred_tools()
            if self._tool_permitted(s.name)
        ]

    def _context_usage_snapshot(
        self,
        *,
        history: Sequence[Message],
        compaction: dict[str, int] | None,
        measured_prompt_tokens: int | None = None,
    ) -> dict[str, Any]:
        """The breakdown for a given history — the one place its inputs are
        assembled, so a between-turns snapshot and a per-call one agree."""
        return build_context_usage(
            sections=self.system_sections.sections if self.system_sections else (),
            system_text=self.system,
            history=history,
            tools_api=self._tools_api(),
            deferred_tools_api=self._deferred_tools_api(),
            agents=self.agents,
            model=self.model,
            context_window=self.context_window,
            compaction=compaction,
            measured_prompt_tokens=measured_prompt_tokens,
        )

    # ─── Turn lifecycle ──────────────────────────────────────────────────

    async def _run_turn(self, op: UserTurn | Resume) -> None:
        meta = await self._ensure_session()
        sid = meta.session_id
        turn_id = new_id("turn")
        self._background_turn_terminal = False
        self._turn_model_calls = 0
        self._active_turn_id = turn_id

        # Connect config MCP servers before building the user node so newly
        # known deferred tools can be announced on this very turn.
        await self._connect_mcp_once(sid, turn_id)
        await self._rehydrate_deferred_announced(sid, meta.head_node_id)
        await self._rehydrate_read_files(sid, meta.head_node_id)
        await self._rehydrate_compaction(sid)

        if isinstance(op, Resume):
            # Nothing is appended: the head user node IS the prompt, so the
            # request is byte-identical to the one that failed and the cache
            # survives. Everything a fresh user turn carries (reminders, the
            # background wake marker, the code-mode journal entry) belongs to
            # writing a node, so none of it applies here.
            path = await self.store.load_path(sid)
            head = path[-1] if path else None
            if head is None or head.message.role != "user":
                await self._emit(TurnStarted(
                    session_id=sid, node_id=head.node_id if head else "",
                    turn_id=turn_id, model=self.model, provider=self.provider.name,
                ))
                await self._emit_terminal_with_park(ErrorEvent(
                    session_id=sid,
                    node_id=head.node_id if head else None,
                    turn_id=turn_id,
                    message=(
                        "nothing to resume: the last message is already an "
                        "assistant reply"
                    ),
                    retriable=False,
                    num_model_calls=0,
                ))
                self._active_turn_id = None
                return
            await self._start_and_loop(sid, turn_id, head)
            return

        content: list[Any] = (
            [Text(op.content)] if isinstance(op.content, str) else list(op.content)
        )
        reminder = self._deferred_catalog_reminder()
        if reminder is not None:
            content.append(Text(reminder))
        ledger = self._ledger_reminder()
        if ledger is not None:
            content.append(Text(ledger))
        notes = self._notes_reminder()
        if notes is not None:
            content.append(Text(notes))
        plan_reminder = self._plan_mode_reminder()
        if plan_reminder is not None:
            content.append(Text(plan_reminder))
        user_node = SessionNode(
            node_id=new_id("node"),
            parent_id=op.parent_node_id or meta.head_node_id,
            message=Message(role="user", content=content),
        )
        await self.store.append(sid, user_node)
        await self._mark_background_woken(op, sid, user_node.node_id, turn_id)
        if self._codemode is not None:
            # Episodic half of the continual journal: the intent behind the turn.
            self._codemode.record_event("user", _plain_text(content))
        elif self._journal is not None:
            self._journal.record_event("user", _plain_text(content))
        await self._start_and_loop(sid, turn_id, user_node)

    async def _start_and_loop(
        self, sid: str, turn_id: str, from_node: SessionNode
    ) -> None:
        """The half of a turn that runs once the user node exists: announce it,
        fire TURN_START, run the agent loop, and end on a terminal event. Shared
        by UserTurn (which just wrote the node) and Resume (which continues from
        the node already at the head)."""
        await self._emit(
            TurnStarted(
                session_id=sid, node_id=from_node.node_id, turn_id=turn_id,
                model=self.model, provider=self.provider.name,
            )
        )
        if self.hooks is not None:
            decision = await self.hooks.emit(
                TURN_START, {"session_id": sid, "turn_id": turn_id}
            )
            await self._report_hook_warnings(sid, turn_id, TURN_START, decision)
        try:
            await self._agent_loop(sid, turn_id, from_node)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # surface, never swallow
            await self._emit_terminal_with_park(
                ErrorEvent(
                    session_id=sid, turn_id=turn_id, message=repr(exc),
                    num_model_calls=self._turn_model_calls,
                )
            )
        finally:
            self._active_turn_id = None

    async def _connect_mcp_once(self, sid: str, turn_id: str) -> None:
        """Connect configured MCP servers on the first turn (idempotent).

        Their tools become searchable/loadable via ToolSearch (progressive
        disclosure) without entering the prompt schema. A server that fails to
        connect surfaces as a non-fatal ErrorEvent; the turn proceeds.
        """
        if self._mcp_connected or not self._mcp_servers or self.registry is None:
            return
        self._mcp_connected = True
        for name, cfg in self._mcp_servers.items():
            try:
                await self.registry.connect_mcp(name, dict(cfg))
            except Exception as exc:
                await self._emit(ErrorEvent(
                    session_id=sid, turn_id=turn_id,
                    message=f"mcp server {name!r} failed to connect: {exc!r}",
                    retriable=False,
                ))

    async def _rehydrate_deferred_announced(
        self, sid: str, head_node_id: str | None
    ) -> None:
        """Seed the announced set from the stored history (once per engine).

        ``_deferred_announced`` is process memory; the session outlives the
        process. On resume a fresh loop would otherwise re-announce the whole
        catalog into a session whose history already carries it — pure token
        waste and a confusing duplicate for the model. So on the first turn we
        read back the catalog blocks already in the history and treat those
        names as announced; only genuinely new tools get injected.
        """
        if self._deferred_rehydrated:
            return
        self._deferred_rehydrated = True
        if self.registry is None or self._codemode is not None:
            return
        try:
            nodes = await self.store.load_path(sid, head_node_id)
        except Exception:  # a store that cannot replay just loses the dedupe
            return
        for node in nodes:
            text = node.message.text()
            if text:
                self._deferred_announced.update(parse_deferred_catalog(text))

    async def _rehydrate_read_files(self, sid: str, head_node_id: str | None) -> None:
        """Seed the read-before-edit ledger from the stored transcript (once).

        ``ctx.read_files`` is process memory that only Read and Write add to,
        but the session outlives the process — and the ToolCtx is rebuilt with
        it. Without this, a resumed session treats a file the model
        demonstrably Read as unread and refuses the next Edit to it, pointing
        at a Read that is sitting right there in the history. The replay
        records exactly what the live path records: the resolved absolute path
        of every Read or Write whose result was not an error.

        Deliberately tolerant — a call with no ``file_path``, a store that
        cannot replay, or a history written before any of this simply
        contributes nothing and the session re-Reads the old way.
        """
        if self._read_files_rehydrated:
            return
        self._read_files_rehydrated = True
        try:
            nodes = await self.store.load_path(sid, head_node_id)
        except Exception:  # a store that cannot replay just re-Reads
            return
        seen: dict[str, str] = {}  # tool_use_id -> resolved path
        for node in nodes:
            for block in node.message.content:
                if isinstance(block, ToolUseBlock):
                    path = _read_ledger_path(block, self.ctx)
                    if path is not None:
                        seen[block.id] = path
                elif isinstance(block, ToolResultBlock) and block.is_error:
                    seen.pop(block.tool_use_id, None)
        self.ctx.read_files.update(seen.values())

    def _deferred_catalog_reminder(self) -> str | None:
        """Announce known-but-unloaded tools, once per tool (name + one-liner).

        Rides the user turn as a system-reminder block so the static system
        prompt — compiled before MCP servers connect — stays byte-stable for
        the cache. Without this the model has ToolSearch but no idea what
        exists to search for. Skipped under code-mode, where discovery goes
        through the in-kernel list_tools/search_tools instead.
        """
        if self.registry is None or self._codemode is not None:
            return None
        specs = [
            s for s in self.registry.deferred_tools()
            if s.name not in self._deferred_announced and self._tool_permitted(s.name)
        ]
        if not specs:
            return None
        self._deferred_announced.update(s.name for s in specs)
        return render_deferred_catalog(specs)

    # ─── Compaction plumbing ─────────────────────────────────────────────

    async def _rehydrate_compaction(self, sid: str) -> None:
        """Replay persisted ContextCompaction events into the compactor.

        Promotion state is process memory; the session outlives the process.
        Digest rendering is deterministic, so re-applying the persisted
        decisions makes the resumed view byte-identical to the pre-restart one
        — the cache survives the restart too.
        """
        if self._compaction_rehydrated:
            return
        self._compaction_rehydrated = True
        try:
            events = await self.store.events_since(sid, 0)
        except Exception:  # a store that cannot replay just re-compacts later
            return
        self._compactor.rehydrate(
            Promotion(
                tier=e.tier,
                reason=e.reason,
                trimmed=tuple((n, t) for n, t in e.trimmed),
                spans=tuple((s, en, k) for s, en, k in e.spans),
            )
            for e in events
            if isinstance(e, ContextCompaction)
        )

    async def _emit_compaction(
        self, sid: str, turn_id: str, promo: Promotion
    ) -> None:
        await self._emit(ContextCompaction(
            session_id=sid, turn_id=turn_id,
            tier=promo.tier, reason=promo.reason,
            trimmed=[list(p) for p in promo.trimmed],
            spans=[list(s) for s in promo.spans],
            reclaimed_chars=promo.reclaimed_chars,
        ))

    def _context_overhead_tokens(self) -> int:
        """Estimated tokens of everything the next request carries besides
        history: system prompt + tool schemas. Priced with the same estimator
        the usage breakdown uses, so the trigger and the report agree."""
        total = estimate_tokens_of_text(self.system)
        for api in self._tools_api():
            try:
                total += estimate_tokens_of_text(json.dumps(api))
            except (TypeError, ValueError):
                pass
        return total

    def _ledger_reminder(self) -> str | None:
        """Recite the session ledger (model-curated, never-lossy state) onto
        the new user turn. The newest copy always rides the recent window —
        which compaction never digests — while older copies are stripped when
        their turns digest, so the ledger survives every tier without pinning
        anything in the cached prefix."""
        try:
            from fluid.tools.builtins.ledger import read_ledger, render_ledger_reminder
        except ImportError:
            return None
        text = read_ledger(self.ctx, self._session.session_id if self._session else None)
        if not text:
            return None
        return render_ledger_reminder(text)

    def _notes_reminder(self) -> str | None:
        """Recite the session notes (the model's working plan) onto the new
        user turn — same recitation contract as the ledger: the newest copy
        rides the recent window while older copies are stripped when their
        turns digest. Only fires when the model has actually written notes,
        so an untouched tool costs nothing."""
        try:
            from fluid.tools.builtins.note import read_notes, render_notes_reminder
        except ImportError:
            return None
        text = read_notes(self.ctx, self._session.session_id if self._session else None)
        if not text:
            return None
        return render_notes_reminder(text)

    def _set_plan_mode(self, active: bool) -> None:
        """Plan/StopPlanning's switch: the next request's schemas follow."""
        self.plan_mode = bool(active)

    def _apply_plan_mode(self, active: bool) -> None:
        """Flip plan mode the way the TOOLS do — through the injected control.

        ``ctx.extra["plan_mode_control"]`` may have been replaced by the host
        with a wrapper that persists the mode and updates its own UI, so an
        engine-side flip (SetPermissionMode) has to go through it rather than
        assigning the attribute. The flag is force-set afterwards regardless:
        a host wrapper that raises must never leave the engine believing it is
        still read-only when the session has left plan mode (or vice versa) —
        that divergence is unrecoverable from inside the loop.
        """
        active = bool(active)
        if active == self.plan_mode:
            return
        control = (self.ctx.extra or {}).get("plan_mode_control")
        setter = getattr(control, "set_active", None)
        if callable(setter):
            try:
                setter(active)
            except Exception:  # noqa: BLE001 — host bookkeeping, never fatal
                log.warning(
                    "plan_mode_control.set_active(%s) failed; forcing the flag",
                    active, exc_info=True,
                )
        self.plan_mode = active

    def _without_stale_plan_reminder(self, history: list[Message]) -> list[Message]:
        """Rewrite plan-mode reminders that the live flag has since contradicted.

        The reminder is written into the user node when the turn starts, but
        approval lands MID-turn: from that moment every remaining call of the
        same turn re-reads "Plan mode is active" from a node that predates the
        approval, which is exactly the contradiction the audit watched models
        try to obey. Dropping the block silently was not enough — models that
        had already read it kept believing plan mode was on (an approval whose
        tool result was lost to an interrupt or restart leaves NOTHING saying
        it ended). So the NEWEST stale reminder is replaced with an explicit
        "plan mode is over" notice and the older ones are dropped. All of this
        is render-time only: the stored tree keeps its true shape, and the
        notice disappears by itself if plan mode is ever entered again.
        """
        if self.plan_mode:
            return history
        last_msg = -1  # index of the message carrying the newest stale reminder
        for i, message in enumerate(history):
            if any(
                isinstance(b, Text) and PLAN_MODE_REMINDER_MARK in b.text
                for b in message.content
            ):
                last_msg = i
        if last_msg < 0:
            return history
        out: list[Message] = []
        for i, message in enumerate(history):
            replaced: list[Any] = []
            stale = [
                b for b in message.content
                if isinstance(b, Text) and PLAN_MODE_REMINDER_MARK in b.text
            ]
            if not stale:
                out.append(message)
                continue
            for b in message.content:
                if not (isinstance(b, Text) and PLAN_MODE_REMINDER_MARK in b.text):
                    replaced.append(b)
                elif i == last_msg and b is stale[-1]:
                    replaced.append(Text(PLAN_EXIT_REMINDER))
            if not replaced:
                # Emptying a message would break the request shape; keep it
                # whole (it cannot be the newest one — that got the notice).
                out.append(message)
                continue
            out.append(Message(
                role=message.role, content=replaced,
                provider_meta=message.provider_meta,
            ))
        return out

    def _plan_mode_reminder(self) -> str | None:
        """Recite the plan-mode contract onto each user turn while active —
        the mode's rules must survive long exploration stretches, the same
        way Claude Code re-reminds every turn."""
        if not self.plan_mode:
            return None
        registered = (
            self.registry.get("StopPlanning") is not None
            if self.registry is not None
            else False
        )
        exit_clause = (
            "When the plan is ready, present it with StopPlanning for "
            "approval; only the user's approval ends plan mode."
            if registered and self._tool_permitted("StopPlanning")
            else "Only the user can end plan mode."
        )
        return (
            "<system-reminder>\n"
            f"{PLAN_MODE_REMINDER_MARK} a read-only exploration and planning "
            "phase. Do not write or edit files, run state-changing "
            "commands, or describe edits as if you had made them. Explore "
            "the codebase, weigh approaches and their trade-offs, and "
            f"design a concrete implementation strategy. {exit_clause}\n"
            "</system-reminder>"
        )

    # ─── Agent loop (model call → tool dispatch → repeat) ────────────────

    async def _agent_loop(self, sid: str, turn_id: str, user_node: SessionNode) -> None:
        parent = user_node
        num_calls = 0
        self._turn_model_calls = 0
        total_usage: dict[str, Any] = {}
        final_stop = "end_turn"
        overflow_retries = 0
        result: _CallResult | None = None
        empty_retries = 0  # CONSECUTIVE empty completions; any real call resets it
        continuations = 0

        while True:
            nodes = await self.store.load_path(sid, parent.node_id)
            rendered, promotions = self._compactor.apply(
                nodes,
                overhead_tokens=self._context_overhead_tokens(),
                context_window=self.context_window,
                output_reserve_tokens=self.max_tokens or _DEFAULT_OUTPUT_RESERVE,
            )
            history = repair_dangling_tool_calls(rendered)
            if self._encrypted_reasoning_poisoned:
                # A prior call proved the history holds reasoning blobs the
                # current model+account cannot decrypt; render without them
                # rather than pay another rejected request (the stored tree
                # keeps them — a switch back could decrypt them again).
                history = strip_opaque_reasoning(history)
            for promo in promotions:
                await self._emit_compaction(sid, turn_id, promo)
            projected_prompt = self._estimate_request_input_tokens(history)
            call_was_sticky = not self._sticky_poisoned and empty_retries == 0
            result = await self._stream_once(
                sid, turn_id, history, sticky=call_was_sticky
            )
            num_calls += 1
            self._turn_model_calls = num_calls
            if not _is_empty_completion(result):
                empty_retries = 0
            elif empty_retries < EMPTY_COMPLETION_MAX_RETRIES:
                # Nothing came back. Re-send the SAME history — the transcript
                # gains no node — but WITHOUT the sticky-routing key, so
                # OpenRouter may pick a different upstream replica. Retrying
                # byte-identically pins the retry to the replica that just
                # failed, which is how ten retries in a row all come back empty.
                #
                # A key whose replica is dead stays dead for longer than one
                # call: a turn that re-probed stickiness after every recovery
                # paid a dud call plus a backoff on all 16 of its model calls.
                # So if THIS call carried the key, drop it — and keep it dropped
                # past the end of the turn. Re-probing once per turn was cheap
                # while dead replicas failed fast, but a replica that HANGS
                # makes every probe cost a full first-data watchdog timeout. A
                # new session (or a new process) starts clean.
                if call_was_sticky:
                    self._sticky_poisoned = True
                empty_retries += 1
                # The dud call may still have billed something.
                self._fold_call_usage(total_usage, result.usage)
                delay = _empty_retry_delay(empty_retries)
                await self._emit(ProviderRetry(
                    session_id=sid, turn_id=turn_id,
                    attempt=empty_retries,
                    max_attempts=EMPTY_COMPLETION_MAX_RETRIES,
                    reason=(
                        "empty response from provider"
                        + _upstream_suffix(result.native_stop_reason)
                    ),
                    delay_seconds=delay, model=self.model,
                ))
                try:
                    await asyncio.wait_for(self._interrupted.wait(), timeout=delay)
                except TimeoutError:
                    pass  # normal case: the backoff elapsed uninterrupted
                if self._interrupted.is_set():
                    # Stop pressed during the backoff — end the turn the same
                    # way an interrupt between model calls does.
                    partial, partial_cost = self._partial_usage(total_usage)
                    await self._emit_terminal_with_park(Interrupted(
                        session_id=sid, node_id=parent.node_id, turn_id=turn_id,
                        usage=partial, cost_usd=partial_cost,
                        num_model_calls=num_calls,
                    ))
                    return
                continue
            else:
                # Out of retries. Hand the existing error path a result it can
                # end the turn on; the message deliberately carries no
                # context-overflow marker, so the trim-and-retry branch below
                # cannot mistake it for a too-long prompt.
                result = _CallResult(
                    message=result.message,
                    usage=result.usage,
                    stop_reason="error",
                    error_message=(
                        f"provider returned an empty response "
                        f"{empty_retries + 1} times in a row; giving up"
                        + _upstream_suffix(result.native_stop_reason)
                    ),
                    error_retriable=False,
                    native_stop_reason=result.native_stop_reason,
                )
            if result.stop_reason == "error":
                # A failed HTTP request can still be billable. Record and
                # publish it before deciding whether the context-overflow path
                # is eligible for another escalation retry.
                self._fold_call_usage(total_usage, result.usage)
                usage_raw = dict(result.usage)
                usage_raw["model_call_index"] = num_calls
                usage_raw["projected_prompt_tokens"] = projected_prompt
                usage_raw.update(self._cache_probe_fields(result.usage))
                await self._emit(Usage(
                    session_id=sid, node_id=parent.node_id, turn_id=turn_id,
                    input_tokens=result.usage.get("input_tokens", 0),
                    output_tokens=result.usage.get("output_tokens", 0),
                    cache_read_tokens=result.usage.get("cache_read_input_tokens", 0),
                    cache_creation_tokens=result.usage.get(
                        "cache_creation_input_tokens", 0
                    ),
                    raw=usage_raw,
                ))
                if (
                    not self._encrypted_reasoning_poisoned
                    and result.error_message
                    and "invalid_encrypted_content" in result.error_message
                ):
                    # The history carries an encrypted reasoning blob the
                    # current model+account cannot decrypt — the session
                    # switched model or account on the same provider dialect
                    # (e.g. Grok → GPT, both openai_responses) and a legacy
                    # untagged envelope kept resending. Every replay of this
                    # history fails identically, so retry once with opaque
                    # reasoning stripped (the flag above pre-strips the
                    # re-render, and every later turn). One retry only: if
                    # the stripped request still fails, the flag is already
                    # set and the error surfaces below.
                    self._encrypted_reasoning_poisoned = True
                    await self._emit(ProviderRetry(
                        session_id=sid, turn_id=turn_id,
                        attempt=1, max_attempts=1,
                        reason=(
                            "history contains encrypted reasoning this model "
                            "cannot decrypt; retrying without it"
                        ),
                        delay_seconds=0.0, model=self.model,
                    ))
                    continue
                if (
                    overflow_retries < 3
                    and result.error_message
                    and _is_context_overflow(result.error_message)
                ):
                    # The prompt outgrew the window: force the next rung of
                    # the eviction ladder and retry on the smaller view. Each
                    # rung is a batch (tier 1 trim → tier 2 digests → tier 3
                    # epoch folds); when no rung can reclaim anything, fall
                    # through to the normal error path.
                    escalations = self._compactor.escalate(nodes)
                    if escalations:
                        overflow_retries += 1
                        for promo in escalations:
                            await self._emit_compaction(sid, turn_id, promo)
                        continue
                # The failed call's usage was folded in above (before the
                # escalation decision) — snapshot it, don't re-fold.
                partial, partial_cost = self._partial_usage(total_usage)
                await self._emit_terminal_with_park(ErrorEvent(
                    session_id=sid, turn_id=turn_id,
                    message=result.error_message or "provider error",
                    retriable=result.error_retriable,
                    usage=partial, cost_usd=partial_cost,
                    num_model_calls=num_calls,
                ))
                return  # leave the turn open-ended
            self._fold_call_usage(total_usage, result.usage)

            node = SessionNode(
                node_id=new_id("node"), parent_id=parent.node_id, message=result.message
            )
            await self.store.append(sid, node)
            # One durable Usage event per logical model call. The call index and
            # provider-request attempt metadata in ``raw`` make retries and
            # interrupted turns auditable without inferring from tool events.
            usage_raw = dict(result.usage)
            usage_raw["model_call_index"] = num_calls
            usage_raw["projected_prompt_tokens"] = projected_prompt
            usage_raw.update(self._cache_probe_fields(result.usage))
            prompt_tokens = _measured_prompt_tokens(result.usage)
            if prompt_tokens > 0:
                # A provider that reported nothing leaves the estimate in place
                # rather than pinning the whole context at zero tokens.
                self._last_call_usage[sid] = (node.node_id, dict(result.usage))
            snapshot: dict[str, Any] | None = None
            try:
                snapshot = self._context_usage_snapshot(
                    # `history` is exactly what this call sent, so the measured
                    # prompt size and the category rows describe the same thing.
                    history=history,
                    # Same opt-in rule get_context_usage follows: with context
                    # rewriting disabled, ``stats`` would price reclaimed tokens
                    # the request never actually saved.
                    compaction=(
                        self._compactor.stats(nodes)
                        if self._compactor.active
                        else None
                    ),
                    measured_prompt_tokens=prompt_tokens if prompt_tokens > 0 else None,
                )
            except Exception:  # accounting is never worth failing a turn over
                log.debug("per-call context-usage snapshot failed", exc_info=True)
            await self._emit(Usage(
                session_id=sid, node_id=node.node_id, turn_id=turn_id,
                input_tokens=result.usage.get("input_tokens", 0),
                output_tokens=result.usage.get("output_tokens", 0),
                cache_read_tokens=result.usage.get("cache_read_input_tokens", 0),
                cache_creation_tokens=result.usage.get("cache_creation_input_tokens", 0),
                raw=usage_raw,
                context_usage=snapshot,
            ))
            parent = node

            if result.interrupted:
                # This call's usage is already folded in above — snapshot, don't re-fold.
                partial, partial_cost = self._partial_usage(total_usage)
                await self._emit_terminal_with_park(Interrupted(
                    session_id=sid, node_id=node.node_id, turn_id=turn_id,
                    usage=partial, cost_usd=partial_cost,
                    num_model_calls=num_calls,
                ))
                return

            # Only run the tool loop when tools are actually configured; without
            # a registry a tool_use stop is terminal (M0 behavior).
            dispatchable = (
                self.registry is not None
                and bool(result.tool_uses)
                and (
                    result.stop_reason == "tool_use"
                    # A stream that ran out of output budget mid-call still
                    # emitted ToolUse events for the blocks that closed. Those
                    # calls have to be answered — continuing without a result
                    # leaves the transcript malformed and the UI showing a tool
                    # that never finishes.
                    or any(tu.get("arg_error") for tu in result.tool_uses)
                )
            )
            if (
                not dispatchable
                and result.stop_reason == "max_tokens"
                and continuations < self.max_continuations_per_turn
                and (
                    self.max_model_calls_per_turn is None
                    or num_calls < self.max_model_calls_per_turn
                )
            ):
                # The model ran out of output budget mid-answer (reasoning-
                # heavy models do this routinely). Ending the turn here would
                # abandon the work, so ask it to pick up where it stopped.
                continuations += 1
                nudge = SessionNode(
                    node_id=new_id("node"), parent_id=parent.node_id,
                    message=Message(role="user", content=[Text(_CONTINUE_NUDGE)]),
                )
                await self.store.append(sid, nudge)
                parent = nudge
                continue
            if not dispatchable:
                final_stop = result.stop_reason
                break
            call_cap_reached = (
                self.max_model_calls_per_turn is not None
                and num_calls >= self.max_model_calls_per_turn
            )

            blocks: list[Any] = []
            if call_cap_reached:
                # ToolUse events were already emitted by _stream_once. Pair
                # every one with a deterministic ToolResult before stopping, so
                # the stream keeps its one-result-per-use invariant.
                for tu in result.tool_uses:
                    text = (
                        "Tool not executed: the configured maximum number of "
                        "model calls for this turn was reached."
                    )
                    await self._emit(ToolResult(
                        session_id=sid, turn_id=turn_id,
                        tool_use_id=tu["tool_use_id"], name=tu["name"],
                        output=text, is_error=True,
                    ))
                    blocks.append(ToolResultBlock(
                        tool_use_id=tu["tool_use_id"], content=[Text(text)],
                        is_error=True,
                    ))
            else:
                for tu in result.tool_uses:
                    if tu.get("arg_error"):
                        tu["arg_error"] = _explain_truncation(
                            tu["arg_error"], result.stop_reason
                        )
                    blocks.append(
                        await self._dispatch_tool_interruptible(sid, turn_id, tu)
                    )
            tr_node = SessionNode(
                node_id=new_id("node"), parent_id=parent.node_id,
                message=Message(role="user", content=blocks),
            )
            await self.store.append(sid, tr_node)
            parent = tr_node
            if call_cap_reached:
                final_stop = "max_calls"
                break
            if self._interrupted.is_set():
                # Interrupt landed during tool dispatch (e.g. while parked on a
                # permission prompt) — stop before issuing another model call.
                partial, partial_cost = self._partial_usage(total_usage)
                await self._emit_terminal_with_park(Interrupted(
                    session_id=sid, node_id=parent.node_id, turn_id=turn_id,
                    usage=partial, cost_usd=partial_cost,
                    num_model_calls=num_calls,
                ))
                return

        if result is not None:
            if self._codemode is not None:
                # Episodic journal: the turn's final text (the acceptance /
                # feedback signal).
                self._codemode.record_event(
                    "assistant", _plain_text(result.message.content)
                )
            elif self._journal is not None:
                self._journal.record_event(
                    "assistant", _plain_text(result.message.content)
                )
        if self.hooks is not None:
            decision = await self.hooks.emit(
                TURN_END,
                {"session_id": sid, "turn_id": turn_id,
                 "text": _plain_text(result.message.content)},
            )
            await self._report_hook_warnings(sid, turn_id, TURN_END, decision)
        if self._pending_steers:
            # No further tool result will carry them; the user should send a
            # normal message. This is the designed fallback for a turn that
            # ends without another tool call, not an anomaly — but dropping
            # silently would lose user input, so log it.
            log.info(
                "dropping %d undelivered mid-turn steer(s) at turn end (ids: %s)",
                len(self._pending_steers),
                ", ".join(
                    _steer_parts(pending)[0] or "-" for pending in self._pending_steers
                ),
            )
            self._pending_steers.clear()
        # Cost rides its own TurnComplete field; keep the usage dict token-shaped.
        cost_total = total_usage.pop("cost_usd", None)
        await self._emit_terminal_with_park(TurnComplete(
            session_id=sid, node_id=parent.node_id, turn_id=turn_id,
            stop_reason=final_stop or "end_turn", num_model_calls=num_calls,
            usage=dict(total_usage), cost_usd=cost_total,
        ))

    def _estimate_request_input_tokens(self, history: list[Message]) -> int:
        """Cheap, conservative estimate of everything resent on the next call."""
        tools = self._tools_api()
        try:
            tools_text = json.dumps(tools, sort_keys=True, default=str)
        except (TypeError, ValueError):
            tools_text = str(tools)
        system_text = (
            self.system if isinstance(self.system, str) else str(self.system or "")
        )
        return (
            estimate_tokens_of_messages(history)
            + estimate_tokens_of_text(system_text)
            + estimate_tokens_of_text(tools_text)
        )

    def _tool_permitted(self, name: str) -> bool:
        """Is ``name`` inside this session's tool surface? (ruling CE12)

        Enforced at four points — the request schemas (:meth:`_tools_api`), the
        deferred catalog + ToolSearch (via ``ctx.extra["tool_filter"]``),
        dispatch (:meth:`_gated_call`), and code mode (the ``CodeModeSession``
        this predicate is stamped onto at construction) — so a filtered tool is
        invisible, unloadable, and uncallable rather than merely discouraged.

        Under code mode the four surface tools (``source == "codemode"``) are
        exempt from the ALLOWLIST: the list names work tools, which the kernel
        filters through the stamped predicate. ``disallowed_tools`` still
        removes them.
        """
        if self._codemode is not None and self.registry is not None:
            spec = self.registry.get(name)
            if spec is not None and spec.source == CODE_MODE_SOURCE:
                # Code mode's four tools ARE the schema surface
                # (``register_code_mode_tools``): the CE12 allowlist names
                # WORK tools, and the kernel enforces it through the stamped
                # ``tool_filter``. An allowlist of work tools must not strip
                # the surface that reaches them — with every schema filtered
                # the model can only narrate its calls as text. Deny still
                # beats everything.
                return not _matches_tool_pattern(name, self.disallowed_tools)
        if self.allowed_tools is not None and not _matches_tool_pattern(
            name, self.allowed_tools
        ):
            return False
        return not _matches_tool_pattern(name, self.disallowed_tools)

    def _tool_available(self, name: str) -> bool:
        """Permitted, plan-mode-allowed, *and* backed by a wired host capability.

        The JSON surface drops a ``requires_extra`` tool whose injection is
        missing (:meth:`_capability_present`), but code mode projected it
        anyway — so a kernel cell could import ``SessionSearch`` and get
        "unavailable: no session store directory injected" back as a tool
        error. Absent capability must be invisible on both surfaces.

        Plan mode is here for the mirror-image reason. It used to live only in
        :meth:`_tools_api`'s non-codemode branch, i.e. only in the request
        schemas — so a code-mode session in "plan mode" was handed a kernel
        that still had ``Write`` in it, and ``from tools import Write`` wrote
        the file while the Plan tool's own result claimed write tools had left
        the surface. This predicate is what code mode filters its projection
        and its discovery tools through, so the withholding now happens on the
        surface the model actually uses. It is read live: an approval that ends
        plan mode mid-turn puts the tools back on the next call.
        """
        if not self._tool_permitted(name):
            return False
        if self.registry is None:
            return True
        spec = self.registry.get(name)
        if spec is None:
            return True
        if self.plan_mode and plan_mode_blocks(spec):
            return False
        return self._capability_present(spec)

    def _tools_api(self) -> list[dict[str, Any]]:
        """The tool schemas the next request would carry (code-mode surface or
        the loaded registry, plan-filtered, then CE12 tool-restriction filtered).
        Pure — no request is built."""
        if self.registry is None:
            return []
        if self._codemode is not None:
            # Code-mode replaces N schemas with the four-tool surface; the
            # work-tools stay in the registry (reachable via host calls) but
            # never enter the prompt schema. Plan mode therefore has nothing to
            # filter HERE — the four tools are all ``safe``, and the write tools
            # it withholds are not on this surface to begin with. It is enforced
            # where code mode actually reaches them: the projection and the
            # host-call gate (both via :meth:`_tool_available`), plus
            # :meth:`_gated_call`.
            specs = [
                s for s in self.registry.list_tools(loaded_only=True)
                if s.source == "codemode"
            ]
        else:
            specs = self.registry.list_tools(loaded_only=True)
            if self.plan_mode:
                specs = plan_mode_tools(specs)
        return [
            s.to_api()
            for s in specs
            if self._tool_permitted(s.name) and self._capability_present(s)
        ]

    def _capability_present(self, spec: Any) -> bool:
        """Every ``requires_extra`` key the spec names is wired on the ctx.

        A tool whose host injection is missing (no task dispatcher, no monitor
        spawner, no recall store) is dropped from the request schemas — absent
        capability must be invisible, not a schema that errors on call.
        """
        required = getattr(spec, "requires_extra", ()) or ()
        return all(key in self.ctx.extra for key in required)

    def _build_request(
        self, history: list[Message], *, sticky: bool = True
    ) -> ProviderRequest:
        tools_api = self._tools_api()
        plan = plan_cache(self.provider.caps, has_tools=bool(tools_api))
        return ProviderRequest(
            model=self.model,
            messages=history,
            system=self.system,
            tools=tools_api,
            max_tokens=self.max_tokens,
            effort=self.effort,
            cache_breakpoints=plan.breakpoints,
            # OpenRouter documents session_id as the explicit sticky-routing
            # key for prompt-cache locality. Other providers receive no extra.
            # ``sticky=False`` drops the key so OpenRouter is free to pick a
            # different upstream replica (see the empty-completion retry in
            # _agent_loop). The host passthrough lands on top, so its keys win.
            extra={
                **(
                    {"session_id": self.session_id}
                    if sticky and self.provider.name == "openrouter" else {}
                ),
                **self.request_extra,
            },
        )

    def _fingerprint_request(self, request: ProviderRequest) -> None:
        """Record the prefix fingerprint of the request about to be sent.

        Best-effort: a fingerprint that cannot be computed costs a diagnostic,
        never a turn.
        """
        if not self._cache_probe.enabled:
            return
        try:
            self._prefix_fingerprint = fingerprint_prefix(
                system=request.system,
                tools=request.tools,
                messages=request.messages,
            )
        except Exception:  # noqa: BLE001 — instrumentation is never fatal
            log.debug("prefix fingerprint failed", exc_info=True)
            self._prefix_fingerprint = None

    def _cache_probe_fields(self, usage: dict[str, Any]) -> dict[str, Any]:
        """``prefix_hashes`` (+ ``cache_miss`` on a miss) for a Usage payload.

        Consumes the fingerprint of the call that just completed and rotates it
        into the "previous" slot, so the next call diffs against this one.
        """
        fingerprint = self._prefix_fingerprint
        if fingerprint is None:
            return {}
        self._prefix_fingerprint = None
        out: dict[str, Any] = {"prefix_hashes": fingerprint}
        try:
            diagnosis = diagnose_cache_miss(
                previous=self._prev_prefix_fingerprint,
                current=fingerprint,
                input_tokens=int(usage.get("input_tokens", 0) or 0),
                cache_read_tokens=int(
                    usage.get("cache_read_input_tokens", 0) or 0
                ),
                config=self._cache_probe,
            )
        except Exception:  # noqa: BLE001 — instrumentation is never fatal
            log.debug("cache-miss diagnosis failed", exc_info=True)
            diagnosis = None
        self._prev_prefix_fingerprint = fingerprint
        if diagnosis is not None:
            out["cache_miss"] = diagnosis
            log.warning(
                "prompt cache miss: %s (%s); fresh=%s cache_read=%s",
                diagnosis.get("reason"),
                diagnosis.get("note", ""),
                diagnosis.get("input_tokens"),
                diagnosis.get("cache_read_tokens"),
            )
        return out

    async def _stream_once(
        self, sid: str, turn_id: str, history: list[Message], *, sticky: bool = True
    ) -> _CallResult:
        request = self._build_request(
            self._without_stale_plan_reminder(history), sticky=sticky
        )
        self._fingerprint_request(request)

        blocks: dict[int, dict[str, Any]] = {}
        usage_totals: dict[str, Any] = {}
        tool_uses: list[dict[str, Any]] = []
        stop_reason = "end_turn"
        native_stop_reason: str | None = None
        interrupted = False

        async for chunk in self._provider_chunks_interruptible(request):
            if self._interrupted.is_set():
                interrupted = True
                break
            payload = chunk.payload
            # The upstream verdict rides whichever terminal chunk the stream
            # actually reaches — usage/message_stop normally, the synthesized
            # error chunk when the stream dies before terminal usage.
            if payload.get("native_stop_reason"):
                native_stop_reason = payload["native_stop_reason"]
            if chunk.kind == "provider_request":
                # A provider that reports its physical request/generation id in
                # the response headers does so before terminal usage. Preserve
                # it now so an interrupt cannot erase the evidence of a
                # potentially billed request.
                if payload.get("provider_requests"):
                    usage_totals["provider_requests"] = payload["provider_requests"]
            elif chunk.kind == "message_start":
                self._merge_usage(usage_totals, payload.get("usage", {}))
            elif chunk.kind == "block_start":
                index = payload["index"]
                blocks[index] = {
                    "type": payload["block_type"],
                    "text": "",
                    "args": "",
                    "signature": "",
                    "tool_name": payload.get("tool_name"),
                    "tool_use_id": payload.get("tool_use_id"),
                }
                await self._emit(
                    BlockStart(
                        session_id=sid, turn_id=turn_id, index=index,
                        block_type=payload["block_type"],
                        tool_name=payload.get("tool_name"),
                        tool_use_id=payload.get("tool_use_id"),
                    )
                )
            elif chunk.kind == "text_delta":
                blocks[payload["index"]]["text"] += payload["text"]
                await self._emit(
                    TextDelta(session_id=sid, turn_id=turn_id,
                              index=payload["index"], text=payload["text"])
                )
            elif chunk.kind == "thinking_delta":
                blocks[payload["index"]]["text"] += payload["text"]
                await self._emit(
                    ThinkingDelta(session_id=sid, turn_id=turn_id,
                                  index=payload["index"], text=payload["text"])
                )
            elif chunk.kind == "tool_args_delta":
                index = payload["index"]
                fragment = payload["partial_json"]
                block = blocks[index]
                block["args"] += fragment
                await self._emit(ToolArgsDelta(
                    session_id=sid, turn_id=turn_id, index=index,
                    tool_use_id=block["tool_use_id"] or "",
                    name=block["tool_name"] or "",
                    partial_json=fragment,
                ))
            elif chunk.kind == "signature_delta":
                blocks[payload["index"]]["signature"] += payload["signature"]
            elif chunk.kind == "block_stop":
                index = payload["index"]
                await self._emit(BlockStop(session_id=sid, turn_id=turn_id, index=index))
                block = blocks.get(index)
                if block is not None and payload.get("block"):
                    # Authoritative done item (OpenAI Responses): carries the
                    # encrypted reasoning content / full function-call arguments
                    # that the deltas alone don't (serialize.py docstring).
                    block["done_item"] = payload["block"]
                if block and block["type"] == "tool_use":
                    raw_args = block["args"]
                    if not raw_args:
                        # No deltas streamed — fall back to the done item's
                        # complete arguments (a JSON string on Responses).
                        fallback = (block.get("done_item") or {}).get("arguments")
                        if isinstance(fallback, str):
                            raw_args = fallback
                    args, arg_error = _parse_tool_args(raw_args)
                    block["input"] = args
                    tool_uses.append({
                        "tool_use_id": block["tool_use_id"] or "",
                        "name": block["tool_name"] or "",
                        "input": args,
                        "arg_error": arg_error,
                    })
                    await self._emit(
                        ToolUse(session_id=sid, turn_id=turn_id,
                                tool_use_id=block["tool_use_id"] or "",
                                name=block["tool_name"] or "", input=args)
                    )
            elif chunk.kind == "usage":
                self._merge_usage(usage_totals, payload.get("usage", {}))
                if payload.get("stop_reason"):
                    stop_reason = payload["stop_reason"]
            elif chunk.kind == "error":
                if payload.get("provider_requests"):
                    usage_totals["provider_requests"] = payload["provider_requests"]
                # No ErrorEvent here — _agent_loop decides whether this error
                # is terminal or recoverable (context overflow → forced trim
                # + one retry) before surfacing it.
                return _CallResult(
                    message=self._assemble_message(blocks, request.model),
                    usage=usage_totals,
                    stop_reason="error",
                    error_message=str(payload.get("message", payload)),
                    error_retriable=bool(payload.get("retriable")),
                    native_stop_reason=native_stop_reason,
                )

        if self._interrupted.is_set():
            interrupted = True

        # Reasoning-channel answer fallback (observed on GLM-4.5 via
        # OpenRouter): the model ends its final call with the entire answer in
        # thinking_deltas and an EMPTY text channel. Without this, the turn's
        # Message.text() is "" and text-channel clients render nothing. When a
        # completed non-tool call produced thinking but zero text, promote the
        # thinking content to a synthetic text block (streamed as a TextDelta
        # so live clients see it too).
        if not interrupted and not tool_uses:
            has_text = any(
                b["type"] == "text" and b["text"] for b in blocks.values()
            )
            thinking_text = "".join(
                b["text"] for _, b in sorted(blocks.items())
                if b["type"] == "thinking" and b["text"]
            )
            if not has_text and thinking_text:
                index = (max(blocks) + 1) if blocks else 0
                await self._emit(BlockStart(
                    session_id=sid, turn_id=turn_id, index=index, block_type="text",
                ))
                await self._emit(TextDelta(
                    session_id=sid, turn_id=turn_id, index=index, text=thinking_text,
                ))
                await self._emit(BlockStop(session_id=sid, turn_id=turn_id, index=index))
                blocks[index] = {
                    "type": "text", "text": thinking_text, "args": "",
                    "signature": "", "tool_name": None, "tool_use_id": None,
                }

        return _CallResult(
            message=self._assemble_message(blocks, request.model),
            tool_uses=tool_uses,
            usage=usage_totals,
            stop_reason=stop_reason,
            interrupted=interrupted,
            native_stop_reason=native_stop_reason,
        )

    async def _provider_chunks_interruptible(
        self, request: ProviderRequest
    ) -> AsyncIterator[Any]:
        """Race every provider read against Interrupt and close it before return.

        This guarantees a corrective/new turn cannot overlap a still-running
        metered HTTP stream from the interrupted turn.
        """
        stream = self.provider.stream(request)
        iterator = stream.__aiter__()
        try:
            while not self._interrupted.is_set():
                next_chunk = asyncio.create_task(iterator.__anext__())
                interrupted = asyncio.create_task(self._interrupted.wait())
                try:
                    done, _ = await asyncio.wait(
                        {next_chunk, interrupted}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if interrupted in done:
                        next_chunk.cancel()
                        with suppress(asyncio.CancelledError, StopAsyncIteration):
                            await next_chunk
                        return
                    try:
                        chunk = next_chunk.result()
                    except StopAsyncIteration:
                        return
                    yield chunk
                finally:
                    interrupted.cancel()
                    with suppress(asyncio.CancelledError):
                        await interrupted
        finally:
            close = getattr(iterator, "aclose", None)
            if close is not None:
                with suppress(Exception):
                    await close()

    # ─── Tool dispatch ───────────────────────────────────────────────────

    async def _dispatch_tool_interruptible(
        self, sid: str, turn_id: str, tu: dict[str, Any]
    ) -> ToolResultBlock:
        """Dispatch one tool call, racing it against the interrupt flag.

        Interrupt used to be invisible here: the flag was polled only between
        provider stream chunks and after a full dispatch round, so a
        long-running tool (a sync Task worker, a slow shell command) pinned the
        turn arbitrarily long after the user pressed stop. An interrupt now
        cancels the in-flight dispatch. The cancelled call still surfaces as an
        is_error ToolResult — every tool_use must get a result or the
        transcript is malformed for the next model call — and the agent loop's
        existing post-dispatch interrupt check then ends the turn.
        """
        if not self._interrupted.is_set():
            dispatch = asyncio.create_task(self._dispatch_tool(sid, turn_id, tu))
            interrupt_wait = asyncio.create_task(self._interrupted.wait())
            try:
                done, _ = await asyncio.wait(
                    {dispatch, interrupt_wait}, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                interrupt_wait.cancel()
            # Prefer the finished dispatch even when the interrupt landed in the
            # same tick — a real result beats a synthesized cancellation.
            if dispatch in done:
                return dispatch.result()
            detached_spawn = (
                tu.get("name") == "Task"
                and isinstance(tu.get("input"), dict)
                and tu["input"].get("mode", "sync") == "background"
            )
            if detached_spawn:
                # Give the short spawn handshake a bounded chance to cross its
                # atomic detach boundary. The handler's inner spawn task is
                # shielded and lifecycle-tracked, so timing out this tool call
                # does not cancel a worker the host may already have created.
                try:
                    return await asyncio.wait_for(
                        asyncio.shield(dispatch),
                        timeout=_BACKGROUND_SPAWN_INTERRUPT_GRACE,
                    )
                except TimeoutError:
                    dispatch.cancel()
                    try:
                        await dispatch
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
                except Exception:  # noqa: BLE001
                    pass
            else:
                dispatch.cancel()
                try:
                    await dispatch
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        text = "Tool execution cancelled: the turn was interrupted."
        await self._emit(ToolResult(
            session_id=sid, turn_id=turn_id, tool_use_id=tu["tool_use_id"],
            name=tu["name"], output=text, is_error=True,
        ))
        return ToolResultBlock(
            tool_use_id=tu["tool_use_id"], content=[Text(text)], is_error=True,
        )

    async def _dispatch_tool(
        self, sid: str, turn_id: str, tu: dict[str, Any]
    ) -> ToolResultBlock:
        tool_use_id = tu["tool_use_id"]
        arg_error = tu.get("arg_error")
        if arg_error:
            # Arguments that never parsed: fail this call, not the turn. The
            # model reissues the call; the transcript keeps its
            # one-result-per-use invariant either way.
            await self._emit(ToolResult(
                session_id=sid, turn_id=turn_id, tool_use_id=tool_use_id,
                name=tu["name"], output=arg_error, is_error=True,
            ))
            return ToolResultBlock(
                tool_use_id=tool_use_id, content=[Text(arg_error)], is_error=True,
            )
        # Expose the call's id to handlers for the duration of the dispatch
        # (dispatches are sequential): host tools that spawn UI-joined work
        # (the Task dispatcher) need it to correlate their cards with this
        # tool_use block.
        self.ctx.extra["tool_use_id"] = tool_use_id
        try:
            outcome = await self._gated_call(sid, turn_id, tu["name"], tool_use_id, tu["input"])
        finally:
            self.ctx.extra.pop("tool_use_id", None)
        content: list[Any] = [Text(outcome.text)]
        # Image payloads (file reads, MCP screenshots) become Image blocks on
        # the result; the base64 never rides the event bus — ToolResult events
        # carry only ``outcome.text``. Each is conditioned to the model-facing
        # pixel budget first (providers bill by pixels; oversized originals are
        # transport waste that also poisons the chars/4 context estimate).
        content.extend(
            Image(media_type=media_type, data=data_b64)
            for media_type, data_b64 in (
                condition_image(img.media_type, img.data) for img in outcome.images
            )
        )
        # Nested-instruction injection (instruction-file seam): when a tool
        # touches a path whose own directory carries an AGENTS-family file not
        # in the project stack, ride those instructions into context on this
        # tool result — model-facing only, once per file per session.
        reminder = self._nested_instruction_reminder(tu["input"])
        if reminder:
            content.append(Text(reminder))
        return ToolResultBlock(
            tool_use_id=tool_use_id,
            content=content,
            is_error=outcome.is_error,
        )

    # Tool-input keys that name a filesystem target worth checking for nested
    # instruction files.
    _PATH_INPUT_KEYS = ("file_path", "path", "notebook_path", "directory")

    def _nested_instruction_reminder(self, input: dict[str, Any]) -> str | None:
        from pathlib import Path

        from fluid.context.instructions import _walk_up_dirs, nested_instructions

        if not isinstance(input, dict):
            return None
        targets = [
            v for k in self._PATH_INPUT_KEYS
            if isinstance((v := input.get(k)), str) and v
        ]
        if not targets:
            return None
        # Directories already covered by the project stack (git root → cwd)
        # never re-inject.
        try:
            covered = {str(d) for d in _walk_up_dirs(Path(self.ctx.cwd or "."))}
        except OSError:
            covered = set()
        blocks: list[str] = []
        for target in targets:
            try:
                for f in nested_instructions(target):
                    directory = str(Path(f.path).parent)
                    if directory in covered or f.path in self._nested_injected:
                        continue
                    self._nested_injected.add(f.path)
                    if f.content.strip():
                        blocks.append(f"# From: {f.path}\n{f.content.rstrip()}")
            except OSError:
                continue
        if not blocks:
            return None
        joined = "\n\n".join(blocks)
        return (
            "<system-reminder>\nInstructions apply to the directory just "
            f"accessed:\n\n{joined}\n</system-reminder>"
        )

    async def _gated_call(
        self, sid: str, turn_id: str, name: str, tool_use_id: str,
        input: dict[str, Any],
    ) -> ToolOutcome:
        """The gated tool-execution path for the JSON tool surface:
        PreToolUse hook → PermissionEngine → dispatch → PostToolUse.

        ``run_python`` is intercepted here and handed to the code-mode session,
        whose in-kernel tool calls are gated host-side by ``HostBridge`` (the
        same hook + permission + doom-loop pipeline, including the
        ``code_mode_allowed=False`` escape hatch).
        """
        assert self.registry is not None

        # CE12 enforcement point 3: a call naming a tool outside this session's
        # surface never reaches the registry. The model should never emit one
        # (the schema was never sent), so this is the defense against
        # hallucinated and stale-transcript names — it answers in-context
        # instead of crashing the turn. Placed ahead of the hook and permission
        # gates so the same wall covers the code-mode bridge, which enters the
        # dispatch path here rather than through ``_dispatch_tool``.
        if not self._tool_permitted(name):
            return await self._finish_tool(
                sid, turn_id, name, tool_use_id,
                ToolOutcome(
                    text=f"tool not available in this session: {name}", is_error=True
                ),
            )
        spec = self.registry.get(name)

        # Plan mode's last wall. The JSON surface withholds write schemas while
        # planning, so a call arriving here names a tool the model was never
        # offered — but code mode reaches this path from a kernel, where the
        # model writes the call itself, and "the schema was withheld" enforces
        # nothing. Live flag: an approval that ends plan mode mid-turn lets the
        # very next call of the same turn through.
        if self.plan_mode and spec is not None and plan_mode_blocks(spec):
            return await self._finish_tool(
                sid, turn_id, name, tool_use_id,
                ToolOutcome(text=plan_mode_denial(name), is_error=True),
            )

        # PreToolUse hook: deny short-circuits with an in-context error result;
        # modify replaces the input before permissions/dispatch see it.
        if self.hooks is not None:
            decision = await self.hooks.emit(
                PRE_TOOL_USE,
                {"name": name, "input": input, "tool_use_id": tool_use_id,
                 "session_id": sid},
            )
            await self._report_hook_warnings(sid, turn_id, PRE_TOOL_USE, decision, name)
            if decision.action == "deny":
                reason = decision.note or "denied"
                await self._emit(HookEvent(
                    session_id=sid, turn_id=turn_id, level="deny",
                    message=reason, hook_event=PRE_TOOL_USE, tool_name=name,
                ))
                return await self._finish_tool(
                    sid, turn_id, name, tool_use_id,
                    ToolOutcome(text=f"blocked by hook: {reason}", is_error=True),
                    input=input,
                )
            if decision.action == "modify" and decision.replacement_input is not None:
                input = decision.replacement_input

        # Permission gate.
        if self.permissions is not None and spec is not None:
            decided = self.permissions.decide(spec, input)
            verdict = decided.decision
            # Default denial text: names the configured rule when one decided.
            deny_message: str | None = decided.deny_message()
            ask_reason: str | None = None
            if verdict == "allow" and self.permissions.note_execution(name, input):
                # Doom-loop checkpoint. Under never_ask there may be nobody
                # listening for permission prompts (headless embedders), so an
                # "ask" would park the turn forever — deny with in-context
                # feedback instead; that alone breaks the repeat pattern.
                if self.permissions.approval_policy == "never_ask":
                    return await self._finish_tool(
                        sid, turn_id, name, tool_use_id,
                        ToolOutcome(text=_DOOM_LOOP_DENIAL, is_error=True),
                    )
                verdict = "ask"
                ask_reason = _DOOM_LOOP_REASON
            if verdict == "ask":
                verdict, responder_message, updated_input = await self._await_permission(
                    sid, turn_id, name, tool_use_id, input, decided.risk,
                    reason=ask_reason,
                )
                # The responder's reason is what the model reads back; only
                # fall back to the generic prompt text when none was given.
                deny_message = responder_message or PROMPT_DENIAL
                if ask_reason and responder_message is None:
                    deny_message = f"{deny_message} ({ask_reason})"
                if verdict != "deny" and updated_input is not None:
                    input = updated_input
            if verdict == "deny":
                return await self._finish_tool(
                    sid, turn_id, name, tool_use_id,
                    ToolOutcome(text=deny_message or "permission denied", is_error=True),
                )

        # run_python is engine-driven: it opens the kernel; the kernel's in-sandbox
        # tool calls are gated by HostBridge (see CodeModeSession.run_python),
        # which runs its own MRTR round-trip through this loop's
        # ``_collect_input_responses`` — so an in-kernel AskUser reaches the user
        # exactly as a JSON-path one does.
        if self._codemode is not None and name == "run_python":
            outcome = await self._codemode.run_python(
                sid=sid, turn_id=turn_id, input=input, loop=self
            )
        else:
            outcome = await self.registry.dispatch(name, input, self.ctx)

        # MRTR (MCP spec 2026-07-28): a tools/call can pause on input_required.
        # Collect answers via QuestionRequest/RespondQuestion round-trips, then
        # retry the SAME call with inputResponses + the echoed request_state.
        outcome = await resolve_input_required(
            outcome,
            name=name,
            collect=lambda marker: self._collect_input_responses(
                sid, turn_id, name, tool_use_id, marker
            ),
            redispatch=lambda answers, request_state: self.registry.dispatch(
                name, input, self.ctx,
                input_responses=answers, request_state=request_state,
            ),
        )
        return await self._finish_tool(sid, turn_id, name, tool_use_id, outcome, input=input)

    async def _collect_input_responses(
        self, sid: str, turn_id: str, name: str, tool_use_id: str, marker: Any
    ) -> dict[str, Any] | None:
        """One QuestionRequest/RespondQuestion round-trip per MRTR request.

        Returns {server-request-id: answer} for the retry's ``inputResponses``,
        or None if the turn was interrupted while parked on a question.
        """
        answers: dict[str, Any] = {}
        for server_req_id, descriptor in (marker.requests or {}).items():
            request_id = new_id("q")
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self._pending_questions[request_id] = fut
            desc = descriptor if isinstance(descriptor, dict) else {}
            await self._emit(QuestionRequest(
                session_id=sid, turn_id=turn_id, request_id=request_id,
                tool_use_id=tool_use_id, name=name,
                message=str(desc.get("message", "") or f"{marker.server} needs input for {name}"),
                schema=desc.get("schema"),
            ))
            interrupt_wait = asyncio.create_task(self._interrupted.wait())
            try:
                done, _ = await asyncio.wait(
                    {fut, interrupt_wait}, return_when=asyncio.FIRST_COMPLETED
                )
                if fut not in done:
                    return None
                resp: RespondQuestion = fut.result()
            finally:
                interrupt_wait.cancel()
                self._pending_questions.pop(request_id, None)
            answers[server_req_id] = resp.answer
        return answers

    async def _await_permission(
        self, sid: str, turn_id: str, name: str, tool_use_id: str,
        input: dict[str, Any], risk: str, *, reason: str | None = None,
    ) -> tuple[str, str | None, dict[str, Any] | None]:
        """Publish a PermissionRequest and park the turn until RespondPermission.

        Returns ``(verdict, deny_message, updated_input)``: the responder's
        reason (surfaced as the denied tool's output) and any rewritten tool
        input it handed back on an allow.
        """
        request_id = new_id("perm")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending_permissions[request_id] = fut
        await self._emit(PermissionRequest(
            session_id=sid, turn_id=turn_id, request_id=request_id,
            tool_use_id=tool_use_id, name=name, input=input, risk=risk, reason=reason,
        ))
        interrupt_wait = asyncio.create_task(self._interrupted.wait())
        try:
            done, _ = await asyncio.wait(
                {fut, interrupt_wait}, return_when=asyncio.FIRST_COMPLETED
            )
            if fut not in done:  # Interrupt arrived while parked on the prompt
                return ("deny", "interrupted while awaiting permission", None)
            resp: RespondPermission = fut.result()
        finally:
            interrupt_wait.cancel()
            self._pending_permissions.pop(request_id, None)
        allowed = resp.decision in ("allow", "allow_always")
        if resp.decision == "allow_always" and self.permissions is not None:
            # Sticky for the rest of the session: future calls to this tool
            # skip the prompt (fix: allow_always used to be a one-shot allow).
            spec = self.registry.get(name) if self.registry is not None else None
            self.permissions.allow_always(
                name, spec.scope_of(input) if spec is not None else None
            )
        return (
            "allow" if allowed else "deny",
            resp.message,
            resp.updated_input if allowed else None,
        )

    async def _report_hook_warnings(
        self, sid: str, turn_id: str, event: str, decision: Any,
        tool_name: str | None = None,
    ) -> None:
        """Surface non-fatal hook diagnostics (timeouts, odd exit codes) as
        HookEvents. Never changes the decision."""
        for message in getattr(decision, "warnings", ()):
            await self._emit(HookEvent(
                session_id=sid, turn_id=turn_id, level="warning",
                message=message, hook_event=event, tool_name=tool_name,
            ))

    async def _finish_tool(
        self, sid: str, turn_id: str, name: str, tool_use_id: str, outcome: ToolOutcome,
        input: dict[str, Any] | None = None,
    ) -> ToolOutcome:
        if outcome.text:
            # Spoofing defense: neutralize fluid's own control markers
            # (system-reminders, steering markers) inside result text from ANY
            # source — a file the model Read, kernel stdout, an MCP payload.
            # Genuine instances are appended AFTER this point (steers below,
            # nested-instruction reminders in _dispatch_tool), so they are
            # never touched.
            from fluid.tools.hardening import defang_harness_markers

            defanged = defang_harness_markers(outcome.text)
            if defanged is not outcome.text:
                outcome = replace(outcome, text=defanged)
        if self.hooks is not None:
            decision = await self.hooks.emit(
                POST_TOOL_USE,
                {"name": name, "tool_use_id": tool_use_id, "input": input,
                 "output": outcome.text, "is_error": outcome.is_error,
                 "session_id": sid},
            )
            await self._report_hook_warnings(sid, turn_id, POST_TOOL_USE, decision, name)
        if self._journal is not None:
            # Continual journal, JSON-mode leg: one compact entry per call.
            self._journal.record_tool(
                name, input or {}, outcome.text, outcome.is_error
            )
        delivered: list[tuple[str | None, str]] = []
        if self._pending_steers:
            from fluid.context.steering import format_steer

            delivered = [_steer_parts(pending) for pending in self._pending_steers]
            self._pending_steers.clear()
            steers = "".join(format_steer(text) for _, text in delivered)
            # The steer is new information appended to the payload, so the
            # tool's content_id no longer describes this text. Dropping it
            # keeps code mode from collapsing this result into a pointer at an
            # older ref and losing the user's interjection with it.
            outcome = replace(
                outcome, text=outcome.text + steers, content_id=None, window_of=None
            )
        await self._emit(ToolResult(
            session_id=sid, turn_id=turn_id, tool_use_id=tool_use_id, name=name,
            output=outcome.text, is_error=outcome.is_error, ui=outcome.ui,
        ))
        if delivered:
            # Receipt, ordered after the result that carries the markers: the
            # steers are now part of immutable history, so a host can retire
            # its durable copy of every id listed here.
            await self._emit(SteerDelivered(
                session_id=sid, turn_id=turn_id,
                steers=[{"id": steer_id, "text": text} for steer_id, text in delivered],
            ))
        return outcome

    @staticmethod
    def _merge_usage(totals: dict[str, Any], usage: dict[str, Any]) -> None:
        """WITHIN one model call: usage chunks are cumulative snapshots
        (message_start carries input counts, the terminal chunk output counts),
        so token keys take max(); a provider-reported cost rides one terminal
        chunk and sums harmlessly."""
        for key, value in usage.items():
            if key in {"provider_requests", "provider_response"}:
                # Correlation/routing metadata is a snapshot, not a numeric
                # counter. Keep it with the logical call's durable Usage event.
                totals[key] = value
                continue
            if key == "cost_usd":
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    totals[key] = totals.get(key, 0.0) + float(value)
                continue
            if isinstance(value, int):
                totals[key] = max(totals.get(key, 0), value)

    def _fold_call_usage(self, totals: dict[str, Any], usage: dict[str, Any]) -> None:
        """Fold one model call's usage into the turn totals, filling in an
        estimated cost when the provider reported none."""
        if "cost_usd" not in usage:
            # No provider-reported cost — fall back to the provider's
            # optional pricing-table estimate for this call.
            est = getattr(self.provider, "estimate_cost", None)
            if est is not None:
                cost = est(self.model, usage)
                if cost is not None:
                    usage["cost_usd"] = cost
                    usage["cost_estimated"] = True
        self._accumulate_usage(totals, usage)

    @staticmethod
    def _partial_usage(totals: dict[str, Any]) -> tuple[dict[str, Any], float | None]:
        """Split turn totals into a token-shaped usage dict and its cost, for
        turn-ending emissions that are not TurnComplete. Non-destructive."""
        snapshot = dict(totals)
        return snapshot, snapshot.pop("cost_usd", None)

    @staticmethod
    def _accumulate_usage(totals: dict[str, Any], usage: dict[str, Any]) -> None:
        """ACROSS the agent loop's model calls: every call bills its own
        tokens, so token keys (and cost) SUM into the turn totals."""
        for key, value in usage.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            totals[key] = totals.get(key, 0) + value

    def _assemble_message(
        self, blocks: dict[int, dict[str, Any]], model: str
    ) -> Message:
        content: list[Any] = []
        for index in sorted(blocks):
            block = blocks[index]
            if block["type"] == "text":
                content.append(Text(block["text"]))
            elif block["type"] == "thinking":
                content.append(Thinking(block["text"]))
                content.append(self._reasoning_envelope(block, model))
            elif block["type"] == "tool_use":
                content.append(
                    ToolUseBlock(id=block["tool_use_id"] or "",
                                 name=block["tool_name"] or "",
                                 input=block.get("input", {}))
                )
        return Message(role="assistant", content=content)

    def _reasoning_envelope(self, block: dict[str, Any], model: str) -> Reasoning:
        """Build the provider-tagged Reasoning envelope for one thinking block.

        The origin MUST be the provider that produced the reasoning — each
        serializer round-trips only its own origin and drops foreign ones. For
        OpenAI Responses the authoritative reasoning item (encrypted_content)
        arrives as the block's done item; for Anthropic it's the accumulated
        thinking text + signature.

        ``model`` is the request's model, not ``self.model`` at assembly time:
        a hot-swap can land mid-stream, and the envelope must name the model
        that actually produced the blob — serializers drop mismatched ones
        (encrypted content and signatures are model-bound, see content.py).
        """
        origin = self.provider.name
        done = block.get("done_item")
        if origin == "openai_responses":
            return Reasoning(
                origin=origin,
                summary_text=block["text"] or None,
                encrypted_content=(done or {}).get("encrypted_content"),
                raw=done,
                model=model,
            )
        if origin == "anthropic":
            return Reasoning(
                origin=origin,
                summary_text=block["text"],
                signature=block["signature"] or None,
                raw={"type": "thinking", "thinking": block["text"],
                     "signature": block["signature"]},
                model=model,
            )
        # Other providers (openrouter, ...): no verbatim round-trip contract —
        # keep the summary text; serializers drop what they can't resend.
        return Reasoning(
            origin=origin, summary_text=block["text"] or None, raw=done,
            model=model,
        )
