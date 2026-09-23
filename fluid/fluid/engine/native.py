"""NativeLoop — the harness-owned agent loop.

Turn lifecycle:
  UserTurn → persist user node → build request (append-only prefix + cache
  plan, tool defs) → stream provider chunks → re-emit deltas
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
ErrorEvent saying so. Peer messages accepted during a failed call are retained
as a new unanswered user-role node and included in an explicit later resume.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import suppress
from dataclasses import dataclass, field, replace
from fnmatch import fnmatchcase
from typing import Any

from fluid.codemode.session import (
    CODE_MODE_SOURCE,
    SKILLS_SOURCE,
    CodeModeSession,
    register_code_mode_tools,
)
from fluid.context.cache_plan import plan_cache
from fluid.context.cache_probe import (
    CacheProbeConfig,
    cache_read_report,
    diagnose_cache_miss,
    fingerprint_prefix,
    is_unexplained_miss,
    message_count,
    system_change_detail,
    system_text_of,
)
from fluid.context.compaction import (
    CompactionConfig,
    Compactor,
    Promotion,
    describe_promotions,
)
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
    CacheMissAlert,
    ContextCompaction,
    ErrorEvent,
    Event,
    HandoffCreated,
    HookEvent,
    Interrupted,
    PermissionRequest,
    ProviderAlive,
    ProviderRetry,
    QuestionRequest,
    SteerDelivered,
    TextDelta,
    ThinkingDelta,
    ToolResult,
    ToolArgsDelta,
    ToolUse,
    SubagentEvent,
    TurnComplete,
    TurnStarted,
    Usage,
    WAKE_NOT_APPLICABLE,
)
from fluid.events.ops import (
    BackgroundTaskFinished,
    Fork,
    Handoff,
    AgentMessage,
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
    NOTIFICATION,
    POST_TOOL_USE,
    PRE_COMPACT,
    PRE_TOOL_USE,
    SESSION_END,
    SESSION_START,
    TURN_END,
    TURN_START,
    HookDecision,
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
)
from fluid.providers.base import ProviderClient, ProviderRequest
from fluid.context.environment import environment_note
from fluid.providers.pricing import PricingResolver
from fluid.tools import read_window
from fluid.providers.pricing import estimate_cost as estimate_cost_from_table
from fluid.providers.windows import context_window_for
from fluid.store.base import (
    RECONSTRUCTIBLE_EVENTS,
    SessionMeta,
    SessionNode,
    SessionStore,
    iter_events,
    new_id,
    persist_event,
    replay_source,
)
from fluid.tools.builtins._util import resolve as resolve_tool_path
from fluid.tools.builtins.tool_search import (
    parse_deferred_catalog,
    render_deferred_catalog,
)
from fluid.tools.images import condition_image
from fluid.tools.mrtr import resolve_input_required
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolRegistry

log = logging.getLogger(__name__)

# Seconds of silence (no emitted event) after which the turn watchdog ends
# the turn. Long enough for the slowest single tool call fluid allows (a
# Bash call caps at 600 s, an MCP call at 75 s) plus room; short enough that
# a hung turn is closed with its cost recorded within the hour.
DEFAULT_TURN_WATCHDOG_SECONDS = 900.0
# Header of the runtime-facts block (see _environment_reminder); the resume
# path finds the last one sent by this exact text.
_RUNTIME_FACTS_HEADER = (
    "Runtime facts as of this message (worker-side, read-only checks; "
    "shown on the first turn and again only when they change):\n"
)
_BACKGROUND_SPAWN_INTERRUPT_GRACE = 1.0

# Opening words of the reflection nudge — a stable handle for tests and for
# hosts that want to find or filter it. See NativeLoop._reflection_nudge.
REFLECTION_NUDGE_MARK = "This turn cost you some harness friction"
REFLECTION_NUDGE_DISPATCH_MARK = "This turn dispatched background work"


def _reflection_nudge_text(events: int, dispatches: int) -> str:
    """The one-per-turn reminder to turn a harness mistake into a memory."""
    if events > 0:
        opening = (
            f"{REFLECTION_NUDGE_MARK}: {events} "
            f"{'event' if events == 1 else 'events'} the harness recorded "
            "(a path that was not there, a call that kept failing, work "
            "repeated)."
        )
    else:
        opening = (
            f"{REFLECTION_NUDGE_DISPATCH_MARK}: {dispatches} "
            f"{'task' if dispatches == 1 else 'tasks'} whose results arrive "
            "outside the call that started them."
        )
    return (
        "<system-reminder>\n"
        f"{opening} If anything you learned there holds beyond this task — "
        "where a path or a file actually lives, an output shape you had to "
        "guess at, where a result really arrives — record it with Remember, "
        "one short line each. Having nothing worth recording is a normal "
        "answer; either way, do not mention this reminder in your reply.\n"
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


def _read_ledger_window(block: ToolUseBlock) -> tuple[int, int | None] | None:
    """The line window a replayed Read delivered; None for a Write (whole file).

    Replay cannot know whether an unbounded Read reached the end of the file,
    so it records an open window from the offset — the lenient reading, which
    is the right one for evidence this indirect.
    """
    if block.name != "Read" or not isinstance(block.input, dict):
        return None
    try:
        offset = max(1, int(block.input.get("offset") or 1))
        limit = block.input.get("limit")
        end = offset + int(limit) - 1 if limit else None
    except (TypeError, ValueError):
        return (1, None)
    return (offset, end)


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

# How many times the TurnEnd (Stop) hooks may send the model back to work in
# one turn. Past this a block is reported and the turn ends anyway.
MAX_STOP_HOOK_CONTINUATIONS = 3


def _hook_reminder(label: str, text: str) -> str:
    """Hook-supplied text as a system-reminder block on a user message."""
    from fluid.hooks.bus import hook_reminder

    return hook_reminder(label, text)


def _steer_parts(
    pending: Steer | AgentMessage | str,
) -> tuple[str | None, str, str | None, str]:
    """Split a queued interjection into ``(id, text, sender, origin)``.

    The queue normally holds Steer ops, but a bare string is accepted so
    callers that pushed text straight onto ``_pending_steers`` keep working;
    such a steer simply has no correlation id. ``sender`` is None for the
    user's own steers and the peer's name for an AgentMessage. ``origin``
    ("user" | "system" | "peer") is what picks the marker the text is rendered
    in — the user's steering marker, the harness-notice marker, or the peer
    marker (fluid.context.steering, fluid.context.peer).
    """
    if isinstance(pending, str):
        return (None, pending, None, "user")
    if isinstance(pending, AgentMessage):
        return (pending.id, pending.text, pending.sender, "peer")
    return (pending.id, pending.text, None, pending.origin)


def _render_interjection(text: str, sender: str | None, origin: str) -> str:
    """Wrap one queued interjection in the marker its origin earns.

    The marker IS the authority statement, so this is the one place that
    decides it. A harness-generated notice must never reach the model in the
    user's marker: the base prompt tells the model that text there carries the
    user's own authority, and a <task-notification> whose summary a sub-agent
    wrote plainly does not.
    """
    from fluid.messages.content import SteerDelivery

    return SteerDelivery(text=text, sender=sender, origin=origin).render()


def _steer_receipt(
    steer_id: str | None, text: str, sender: str | None, origin: str
) -> dict[str, Any]:
    """One entry of a ``SteerDelivered`` record."""
    entry: dict[str, Any] = {"id": steer_id, "text": text}
    if sender is not None:
        entry["from"] = sender  # a peer's message; ``from`` already says so
    elif origin != "user":
        entry["origin"] = origin  # a harness notice, not the user's own words
    return entry


@dataclass(frozen=True, kw_only=True)
class ToolArgStreamGuard:
    """Limits on one tool call's streamed argument JSON.

    A model can degenerate mid-``arguments`` and stream padding forever: the
    JSON never becomes callable, the per-read HTTP timeout never fires because
    data keeps arriving, and the only thing that ends the call is the user
    pressing Stop. Three sessions in one audited day burned 224 s, 495 s and
    839 s that way, the last of them on 423 KB of whitespace across 27,845
    delta events.

    Any limit set to ``None`` is off; all three off means no guard at all.
    """

    # Total accumulated argument bytes for one call. Above this the call cannot
    # plausibly still be an honest one.
    max_bytes: int | None = 256 * 1024
    # Consecutive whitespace-only argument bytes.
    stall_bytes: int | None = 8 * 1024
    # Seconds of nothing but whitespace, once the run is long enough to be more
    # than a slow provider drawing indentation (_WHITESPACE_MIN_RUN).
    stall_seconds: float | None = 20.0


@dataclass
class _CallResult:
    """Outcome of one streamed model call."""

    message: Message
    tool_uses: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)  # tokens (int) + optional cost_usd (float)
    stop_reason: str = "end_turn"
    interrupted: bool = False
    # Set when ToolArgStreamGuard aborted the stream: why, and the tool call it
    # abandoned (registered as a failed call only once the retry is spent).
    arg_stream_error: str | None = None
    arg_stream_tool: dict[str, Any] | None = None
    error_message: str | None = None  # set when stop_reason == "error"
    error_retriable: bool = False
    # HTTP status of a rejected request, when the provider reported one. A
    # 413 means "the prompt no longer fits" whatever the body says.
    error_status: int | None = None
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
    "too many tokens",
    "input token count",  # Gemini via OpenRouter: "The input token count (N) exceeds ..."
    "request too large",
)

# HTTP statuses that mean the request body itself was rejected for size.
_OVERFLOW_STATUSES = frozenset({413})

# Events whose loss costs a gap in the replay log and nothing else: they are
# progress frames, and the assistant node the turn stores carries the same
# content in full. A store failure on one of these is logged, not raised (see
# NativeLoop._emit). Everything else — ToolUse, ToolResult, Usage, every
# lifecycle event — is load-bearing, and a store that cannot take it must say
# so loudly.
_RECONSTRUCTIBLE_EVENTS = RECONSTRUCTIBLE_EVENTS

# Exception class-name suffixes that mean "the transport or the database was
# briefly unavailable", matched anywhere in an exception's inheritance chain.
# Named rather than imported: fluid must not depend on whichever driver the
# host wired its store to (psycopg2, psycopg3, asyncpg, aiosqlite, …).
_TRANSIENT_ERROR_SUFFIXES = (
    "OperationalError",     # psycopg2/psycopg3/sqlite3: connection lost, refused
    "InterfaceError",       # psycopg2: connection already closed
    "PoolError",            # psycopg pools: exhausted / closed
    "PoolTimeout",
    "TransportError",       # httpx
    "ConnectError",
    "ConnectTimeout",
    "ReadTimeout",
    "WriteTimeout",
    "RemoteProtocolError",
    "ConnectionDoesNotExistError",  # asyncpg
    "TooManyConnectionsError",
    "CannotConnectNowError",
)


def _is_transient_error(exc: BaseException) -> bool:
    """True when an exception that reached the agent loop looks retriable.

    Socket and OS errors (Windows' WSAEADDRINUSE from ephemeral-port
    exhaustion, a refused connection, a read timeout) and driver-level
    connection failures are blips: the same turn re-run normally succeeds. A
    bug in a tool handler is not. Wrapped causes count — drivers routinely
    re-raise an OSError as their own error type.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (OSError, TimeoutError, ConnectionError)):
            return True
        for cls in type(current).__mro__:
            if cls.__name__.endswith(_TRANSIENT_ERROR_SUFFIXES):
                return True
        current = current.__cause__ or current.__context__
    return False


def _describe_engine_failure(exc: BaseException, *, transient: bool) -> str:
    """A turn-ending exception as something a reader can act on.

    ``repr(exc)`` alone shipped bare ``OperationalError('connection to server
    … failed: Address already in use')`` to the user with no hint that it was
    infrastructure rather than their work, and no hint that retrying would
    work.
    """
    detail = str(exc).strip() or repr(exc)
    name = type(exc).__name__
    if transient:
        return (
            f"the turn stopped on a transient storage or transport failure "
            f"({name}: {detail}). Nothing in the conversation was lost — "
            "resume the turn to continue."
        )
    return (
        f"the turn stopped on an unexpected harness error ({name}: {detail})."
    )


# How long a turn parks on a QuestionRequest before giving up on an answer.
# A host that renders no questions and never interrupts used to hold the
# turn open for good. An engine instance may set ``question_timeout`` to
# another number of seconds, or None to wait without limit.
QUESTION_TIMEOUT_S = 30 * 60.0


def _matches_tool_pattern(name: str, patterns: Sequence[str]) -> bool:
    """Exact name match, or a case-sensitive fnmatch pattern (ruling CE12).

    ``mcp__chrome-devtools__*`` is the motivating shape; plain names carry no
    wildcards and fall out of ``fnmatchcase`` as exact comparisons anyway, but
    the equality check is kept so a name containing ``[`` (legal in MCP tool
    names, a character class to fnmatch) still matches itself.
    """
    return any(name == p or fnmatchcase(name, p) for p in patterns)


def _is_context_overflow(message: str, status: int | None = None) -> bool:
    """Did the provider reject the prompt for being too long?

    A 413 is an overflow whatever the body says (the wording varies by
    provider and gateway); otherwise the message has to carry one of the
    known markers.
    """
    if status in _OVERFLOW_STATUSES:
        return True
    lowered = (message or "").lower()
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
    if result.interrupted or result.tool_uses or result.arg_stream_error:
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


# A whitespace run shorter than this is a slow provider drawing indentation,
# not a degenerate one. Both stall limits wait for it.
_WHITESPACE_MIN_RUN = 256
# How much whitespace has to pile up before the accumulated arguments are
# tested for syntactic completeness, and how much more before re-testing. The
# answer only sharpens the wording of the abort, so the probe is cheap on
# purpose: a complete-JSON test over hundreds of KB, once per delta, would cost
# more than the runaway it is catching.
_WHITESPACE_PROBE_BYTES = 512
_WHITESPACE_REPROBE_BYTES = 64 * 1024
# Runaway argument streams retried before the failure is handed to the model.
_ARG_STREAM_MAX_RETRIES = 1


def _json_object_complete(raw_args: str) -> bool:
    """True when the accumulated argument text is already a whole JSON object."""
    try:
        return isinstance(json.loads(raw_args), dict)
    except (json.JSONDecodeError, ValueError):
        return False


def _explain_arg_runaway(
    tool_name: str, raw_args: str, cause: str, *, closed: bool
) -> str:
    """The tool error a caller gets for a call the guard killed.

    It has to say three things: the call did not run, why it did not run, and
    what to do — because the model's next move (reissue the same call) is
    usually right, and without this it sees only silence.
    """
    state = (
        "Your arguments were already complete before the padding started"
        if closed
        else "Your arguments were still unfinished when the padding started"
    )
    return (
        f"Tool arguments for {tool_name or 'this call'} were abandoned and the "
        f"call was not run: {cause}. {state}. This is a degenerate generation, "
        "not a problem with the payload — reissue the call, and if it carries "
        "a large body consider splitting it.\n"
        f"Received {len(raw_args)} argument bytes."
    )


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
        inproc_tools=True,
        fork=True, rewind=True, cache_metrics=True, context_usage=True,
    )

    #: After a model call's first ProviderAlive, how long the engine must have
    #: emitted nothing at all before it publishes another. A call that streams
    #: content therefore publishes one; a silent reasoning stream with
    #: heartbeats publishes one per interval.
    PROVIDER_ALIVE_INTERVAL_S = 15.0

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
        thinking: dict[str, Any] | None = None,
        request_extra: dict[str, Any] | None = None,
        registry: ToolRegistry | None = None,
        permissions: PermissionEngine | None = None,
        hooks: HookBus | None = None,
        ctx: ToolCtx | None = None,
        codemode: CodeModeSession | None = None,
        journal: Any | None = None,
        max_model_calls_per_turn: int | None = 200,
        max_continuations_per_turn: int = 3,
        turn_watchdog_seconds: float | None = DEFAULT_TURN_WATCHDOG_SECONDS,
        tool_arg_guard: ToolArgStreamGuard | None = None,
        trim: TrimConfig | None = None,
        compaction: CompactionConfig | None = None,
        cache_probe: CacheProbeConfig | None = None,
        cache_key: str | None = None,
        pricing: PricingResolver | None = None,
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
        # A subscriber asking for events older than the bus's in-memory ring
        # gets them from the store. A bus shared between engines keeps the
        # source the first engine attached.
        if self.bus.replay_source is None:
            self.bus.set_replay_source(replay_source(self.store))
        self.model = model
        self.system = system
        # The compiled prompt's labeled sections, when the caller compiled one
        # (fluid.context.compiler). Purely descriptive — the request always
        # sends ``system`` — but it is what lets get_context_usage price memory
        # files and skills separately instead of as one opaque blob.
        self.system_sections = system_sections
        # Model context window. An explicit value (host app, config, env) is
        # authoritative; None falls back to the model-id table in
        # fluid.providers.windows — see the ``context_window`` property.
        self._explicit_context_window = context_window
        # Agent-definition descriptors behind the "Custom agents" row of
        # get_context_usage() (see usage.agent_usage): only what the host
        # itself injected into the prompt. Native ``.agents/agents``
        # definitions are NOT added here — their bodies are worker prompts
        # that never enter this session's context, and the index line that
        # does is priced as part of the compiled prompt. Dispatch remains
        # host territory.
        self.agents = list(agents or ())
        self.max_tokens = max_tokens
        # Reasoning-depth knob, provider-mapped by each serializer (Anthropic
        # output_config.effort; OpenAI-shaped reasoning.effort, clamped).
        self.effort = effort
        # The host's explicit thinking setting, Anthropic-shaped
        # (``{"type": "adaptive", "display": "summarized"}``; a dict with only
        # ``display`` keeps the model's default mode). It applies to NEW
        # sessions only: a session keeps the setting its turns were recorded
        # with (``TurnStarted.thinking``), so a history that ran without
        # thinking never has it switched on mid-way, which would rebuild the
        # cached prefix and can 400 inside a tool loop. The serializer fits the
        # value to each model (providers/anthropic/thinking.py).
        self.thinking = dict(thinking) if thinking else None
        self._session_thinking: dict[str, dict[str, Any] | None] = {}
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
        # The ToolSearch builtin (and any other registry-reading tool) sees
        # this session's *view* of the shared registry through this predicate:
        # the CE12 surface AND the wired host capabilities. The registry itself
        # stays unfiltered — a host app may share one registry across sessions
        # with different restrictions. Stamped on every session, restricted or
        # not: a host that never wired agent messaging still had SendMessage
        # turn up in ToolSearch on a leg whose schemas withheld it.
        self.ctx.extra["tool_filter"] = self._tool_wired
        self._codemode = codemode
        # JSON-mode episodic journal (fluid.codemode.journal.ToolJournal).
        # Code-mode sessions journal through the CodeModeSession instead; a
        # host passing both gets the codemode path (never double-journaled).
        self._journal = journal if codemode is None else None
        if codemode is not None and registry is not None:
            # Ruling T9: code-mode discovery, the projected kernel VFS, and the
            # in-kernel host-call gate all see this session's tool surface, so
            # a filtered tool is never described and never callable from code.
            register_code_mode_tools(
                registry, codemode,
                tool_filter=self._tool_available,
            )
        # ``None`` means unlimited: the provider stays the source of truth for
        # when a turn has to stop. Every comparison below guards that case.
        self.max_model_calls_per_turn = max_model_calls_per_turn
        self.max_continuations_per_turn = max_continuations_per_turn
        # Runaway tool-argument streams. Pass
        # ``ToolArgStreamGuard(max_bytes=None, stall_bytes=None,
        # stall_seconds=None)`` to turn the guard off entirely.
        self.tool_arg_guard = tool_arg_guard or ToolArgStreamGuard()
        self._session: SessionMeta | None = None
        self._session_id = session_id
        self._interrupted = asyncio.Event()
        # Model calls the current turn has issued so far. Mirrors the agent
        # loop's local counter so terminal events raised OUTSIDE the loop (the
        # catch-all handler in _start_and_loop) can still report the count.
        self._turn_model_calls = 0
        # The live turn-totals dict the agent loop folds each call's usage
        # into, by reference, for the same reason: a turn that dies outside the
        # loop still bills what it spent. Four crashed turns in one audited day
        # carried ``cost_usd: null`` and lost real spend.
        self._turn_usage: dict[str, Any] = {}
        self._tool_deliveries = []  # Includes nested code-mode tool completions.
        # Streaming deltas the store refused (see _emit / _note_dropped_event).
        # Cumulative for the engine; the per-turn count drives the log's
        # once-per-turn rate limit and hosts can read the total as a metric.
        self.dropped_delta_events = 0
        self._dropped_events_turn: str | None = None
        self._dropped_events_in_turn = 0
        # Hook bookkeeping. ``_session_hook_started`` gates the one SessionStart
        # per engine and ``_session_start_context`` holds what those hooks
        # asked to add to the first user message. ``_turn_end_hook_fired``
        # lets _start_and_loop's finally fire TurnEnd on the paths the agent
        # loop left early; ``_turn_end_reason`` names why. ``_hook_stop_reason``
        # carries a hook's ``continue: false`` to the next point the loop can
        # end the turn.
        self._session_hook_started = False
        self._session_start_context: str | None = None
        self._turn_end_hook_fired = False
        self._turn_end_reason: str | None = None
        self._hook_stop_reason: str | None = None
        # End-of-turn reflection nudge (see _reflection_nudge): per-turn state,
        # reset by _agent_loop. ``_background_dispatches`` counts detached Task
        # workers started this turn; ``_reflection_nudged`` caps the reminder at
        # one per turn.
        self._background_dispatches = 0
        self._reflection_nudged = False
        self._turn_task: asyncio.Task | None = None
        self._pending_permissions: dict[str, asyncio.Future] = {}
        # Turn watchdog (round 9): a turn whose active tool call never
        # returned and whose stream made no progress sat until the host tore
        # the engine down, with no terminal event and its cost unrecorded.
        # ``_turn_progress_at`` moves on every emitted event; the watchdog
        # task cancels the turn after ``turn_watchdog_seconds`` of silence
        # that is not a human wait (a pending permission or question).
        self.turn_watchdog_seconds = turn_watchdog_seconds
        self._turn_progress_at = time.monotonic()
        self._turn_cancel_reason: str | None = None
        self._watchdog_task: asyncio.Task | None = None
        # Peer messages that arrived while this session was parked on
        # background tasks: delivered with the next wake (or user turn)
        # instead of each waking the parent on its own.
        self._queued_peer_messages: list[AgentMessage] = []
        self._queued_peer_rehydrated = False
        # Runtime-facts block bookkeeping (see _environment_reminder): the
        # facts text last sent to the model, and whether this engine has
        # already harvested them into the memory store.
        self._runtime_facts_sent: str | None = None
        self._runtime_facts_harvested = False
        self._runtime_facts_rehydrated = False
        # System text of the request in flight / the last completed one, for
        # the cache probe's system diff (see _cache_probe_fields).
        self._pending_system_text: str | None = None
        self._prev_system_text: str | None = None
        self._pending_questions: dict[str, asyncio.Future] = {}
        # Mid-turn user steers and peer messages awaiting the next tool
        # result (see the Steer and AgentMessage ops).
        self._pending_steers: list[Steer | AgentMessage | str] = []
        # An append can raise after advancing the store's cached head, or
        # after actually committing. A retry must keep the exact same node
        # and parent rather than build a duplicate under that uncertain head.
        self._pending_peer_write: tuple[SessionNode, list[AgentMessage]] | None = None
        # Once a terminal response is being published, this turn can no
        # longer consume peer traffic. Refuse it explicitly in that small
        # window instead of acknowledging a message which will be discarded.
        self._peer_delivery_sealed = False
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
        if compaction.recall_tools and not self._recall_tools_on_surface():
            # The placeholders and digests name HistoryExpand as the way
            # back; a session that cannot call it gets the recovery-free
            # wording instead of an instruction it cannot follow.
            compaction = replace(
                compaction, recall_tools=False,
                trim=replace(compaction.trim, recall_tools=False),
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
        # Sessions whose ``_last_call_usage`` entry was already looked for in
        # the persisted Usage events (a resume recovers it from there once).
        self._usage_rehydrated: set[str] = set()
        # Prompt-cache instrumentation (context/cache_probe.py). Every request
        # gets a short prefix fingerprint; when a call bills a big fresh prompt
        # with ~no cache read, the two fingerprints are bisected to name the
        # segment that moved. Purely diagnostic — it never changes a request.
        self._cache_probe = cache_probe or CacheProbeConfig()
        # Host-supplied prompt-cache affinity key. Default None keeps the
        # historical behavior (the fluid session id). A host whose users resume
        # a conversation onto a NEW fluid session passes its own durable
        # conversation id here, so the provider keeps serving the same cache
        # partition across the resume instead of re-billing the longest prefix
        # the session will ever have. Must be stable for the life of the
        # conversation and distinct per conversation.
        self.cache_key = cache_key or None
        self._prefix_fingerprint: dict[str, str] | None = None
        self._prev_prefix_fingerprint: dict[str, str] | None = None
        # Consecutive misses the probe could not pin on anything fluid did,
        # and whether this run has already been alerted on. Both reset by any
        # explained call and at the start of every turn.
        self._unexplained_misses = 0
        self._cache_miss_alerted = False
        # Sessions whose previous-request fingerprint was already looked for in
        # the persisted Usage events. The app rebuilds the engine per run, so
        # without this every turn's first call reported "no_previous_request"
        # and the one diagnosis that mattered — what moved across the turn
        # boundary — was never made.
        self._prefix_rehydrated: set[str] = set()
        # Compaction/trim rungs applied since the last request was sent, as
        # ``describe_promotions`` records. Consumed by the next cache-miss
        # diagnosis, which reports a rewrite instead of blaming the provider.
        self._history_rewrites: list[dict[str, Any]] = []
        # Host pricing hook: ``resolver(model) -> ModelPrice | mapping | None``,
        # consulted before fluid's own table so an app with its own model
        # catalog prices its own models. See fluid.providers.pricing.
        self._pricing_resolver = pricing
        # MCP servers from config, connected lazily on the first turn (the
        # constructor is sync; connecting needs the event loop).
        self._mcp_servers = dict(mcp_servers or {})
        self._mcp_connected = False
        # Which filesystem scopes native-agent discovery may draw from (the
        # Agent SDK's setting_sources vocabulary). None = no restriction.
        self._setting_sources = (
            tuple(setting_sources) if setting_sources is not None else None
        )
        # The Skill builtin's miss list walks the same scopes the index did.
        self.ctx.extra.setdefault("setting_sources", self._setting_sources)
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
    def _apply_native_agents(self) -> None:
        """Discover native agent definitions and publish them to the session.

        Two seams, both non-dispatching (task.py doctrine — fluid discovers and
        surfaces, the host spawns):

        * ``ctx.extra["agent_registry"]`` — ``{name: AgentDefinition}`` for the
          host's Task dispatcher. Host-provided entries WIN: an app that
          declared an agent under a name a file also uses keeps its own, with a
          warning, because its dispatcher is what actually runs the thing.
        * the compiled prompt's agents index (name + description per agent,
          built by the prompt compiler, not here) — the only part of a
          definition this session's context carries.

        ``self.agents`` (the "Custom agents" usage row) is left alone: the
        bodies are worker prompts, so pricing them here would report tokens
        the request never sends. The index line is priced with the prompt.
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

    async def _ensure_session(self) -> SessionMeta:
        if self._session is None:
            resumed = False
            if self._session_id is not None:
                session = await self.store.get_session(self._session_id)
                if session is not None:
                    await self._seed_bus_seq(session.session_id)
                    self._session = session
                    resumed = True
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
            await self._emit_session_start(
                self._session.session_id, "resume" if resumed else "startup"
            )
        return self._session

    # ─── Hook plumbing ───────────────────────────────────────────────────

    def _transcript_path(self, sid: str) -> str:
        """The session's transcript file, for hook payloads. Empty when the
        store keeps nothing on disk."""
        for attr in ("transcript_path", "_log_path"):
            fn = getattr(self.store, attr, None)
            if callable(fn):
                try:
                    return str(fn(sid))
                except Exception:  # noqa: BLE001 — a payload field, never fatal
                    return ""
        return ""

    def _hook_payload(self, sid: str, **fields: Any) -> dict[str, Any]:
        return {"session_id": sid, "transcript_path": self._transcript_path(sid), **fields}

    async def _emit_session_start(self, sid: str, source: str) -> None:
        """SessionStart hooks, once per engine. Their context is held for the
        first user message (see _turn_start_hook)."""
        if self._session_hook_started or self.hooks is None:
            return
        self._session_hook_started = True
        decision = await self.hooks.emit(
            SESSION_START, self._hook_payload(sid, source=source)
        )
        await self._report_hook_warnings(sid, None, SESSION_START, decision)
        if decision.additional_context:
            self._session_start_context = decision.additional_context

    async def _emit_session_end(self) -> None:
        """SessionEnd hooks on close, best effort: nothing here may keep the
        engine from closing, and the event bus is already on its way out, so
        diagnostics go to the log."""
        if self.hooks is None or self._session is None:
            return
        sid = self._session.session_id
        try:
            decision = await self.hooks.emit(
                SESSION_END, self._hook_payload(sid, reason="other")
            )
        except Exception:  # noqa: BLE001
            log.warning("SessionEnd hooks failed", exc_info=True)
            return
        for message in [*decision.warnings, decision.system_message or ""]:
            if message:
                log.warning("SessionEnd hook: %s", message)

    async def _emit_pre_compact(
        self, sid: str, turn_id: str, promotions: Sequence[Promotion]
    ) -> None:
        """PreCompact hooks, before a compaction batch is applied to the
        request. The batch is already decided; hooks observe, they cannot
        veto."""
        if self.hooks is None:
            return
        decision = await self.hooks.emit(
            PRE_COMPACT,
            self._hook_payload(
                sid, turn_id=turn_id, trigger="auto", custom_instructions="",
                tiers=sorted({p.tier for p in promotions}),
            ),
        )
        await self._report_hook_warnings(sid, turn_id, PRE_COMPACT, decision)

    async def _notify_hooks(
        self, sid: str, turn_id: str, notification_type: str, message: str,
        *, name: str | None = None, tool_use_id: str | None = None,
    ) -> None:
        """Notification hooks: the engine is waiting on the user."""
        if self.hooks is None:
            return
        decision = await self.hooks.emit(
            NOTIFICATION,
            self._hook_payload(
                sid, turn_id=turn_id, notification_type=notification_type,
                message=message, name=name, tool_use_id=tool_use_id,
            ),
        )
        await self._report_hook_warnings(sid, turn_id, NOTIFICATION, decision, name)

    async def _turn_start_hook(
        self, sid: str, turn_id: str, prompt: str, *, with_context: bool = True
    ) -> tuple[bool, str | None]:
        """TurnStart (UserPromptSubmit) hooks. Returns ``(blocked, context)``.

        A block ends the turn right here: a HookEvent names the reason, the
        TurnEnd hooks run and an ErrorEvent closes the turn. The blocked prompt
        never reaches the store; separately accepted peer messages can still
        be retained there for a later resume. Context
        is what the hooks asked to add to the user message, already wrapped
        as system-reminder blocks; SessionStart context rides the first turn
        the same way. ``with_context=False`` (Resume) drops it, since the
        prompt node already exists.
        """
        self._turn_end_hook_fired = False
        self._turn_end_reason = None
        self._hook_stop_reason = None
        parts: list[str] = []
        if with_context and self._session_start_context:
            parts.append(_hook_reminder("SessionStart hook context", self._session_start_context))
            self._session_start_context = None
        if self.hooks is None:
            return False, "\n".join(parts) or None
        decision = await self.hooks.emit(
            TURN_START, self._hook_payload(sid, turn_id=turn_id, prompt=prompt)
        )
        await self._report_hook_warnings(sid, turn_id, TURN_START, decision)
        if decision.action == "deny" or decision.stop:
            reason = decision.note or "blocked by hook"
            await self._emit(HookEvent(
                session_id=sid, turn_id=turn_id, level="deny",
                message=reason, hook_event=TURN_START,
            ))
            await self._emit_turn_end_hook(sid, turn_id, text="", stop_reason="blocked")
            await self._emit_terminal_with_park(ErrorEvent(
                session_id=sid, turn_id=turn_id,
                message=f"prompt blocked by hook: {reason}",
                retriable=False, num_model_calls=0,
            ))
            self._active_turn_id = None
            return True, None
        if with_context and decision.additional_context:
            parts.append(
                _hook_reminder("UserPromptSubmit hook context", decision.additional_context)
            )
        return False, "\n".join(parts) or None

    async def _emit_turn_end_hook(
        self, sid: str, turn_id: str, *, text: str, stop_reason: str,
        stop_hook_active: bool = False,
    ) -> HookDecision | None:
        """TurnEnd (Stop) hooks. Marks the turn's hooks as fired so the
        fallback in _start_and_loop does not run them twice."""
        self._turn_end_hook_fired = True
        if self.hooks is None:
            return None
        decision = await self.hooks.emit(
            TURN_END,
            self._hook_payload(
                sid, turn_id=turn_id, text=text, stop_reason=stop_reason,
                stop_hook_active=stop_hook_active,
            ),
        )
        await self._report_hook_warnings(sid, turn_id, TURN_END, decision)
        return decision

    async def _stop_hook_continuation(
        self, sid: str, turn_id: str, result: _CallResult, continuations: int
    ) -> str | None:
        """Run the TurnEnd hooks on a finished reply. Returns the reason the
        model should keep going, or None to end the turn. A block past
        MAX_STOP_HOOK_CONTINUATIONS is reported and ignored; a hook's
        ``continue: false`` always ends the turn."""
        decision = await self._emit_turn_end_hook(
            sid, turn_id, text=_plain_text(result.message.content),
            stop_reason=result.stop_reason, stop_hook_active=continuations > 0,
        )
        if decision is None:
            return None
        if decision.stop:
            await self._emit(HookEvent(
                session_id=sid, turn_id=turn_id, level="warning",
                message=f"stopped by hook: {decision.note or 'no reason given'}",
                hook_event=TURN_END,
            ))
            return None
        if decision.action != "deny":
            return None
        reason = decision.note or "a Stop hook asked the model to continue"
        if continuations >= MAX_STOP_HOOK_CONTINUATIONS:
            await self._emit(HookEvent(
                session_id=sid, turn_id=turn_id, level="warning",
                message=(
                    f"Stop hook asked to continue ({reason}) but the turn already "
                    f"continued {continuations} times; ending it"
                ),
                hook_event=TURN_END,
            ))
            return None
        await self._emit(HookEvent(
            session_id=sid, turn_id=turn_id, level="info",
            message=f"Stop hook asked to continue: {reason}", hook_event=TURN_END,
        ))
        # The hooks run again when the turn really ends.
        self._turn_end_hook_fired = False
        return reason

    async def _seed_bus_seq(self, sid: str) -> None:
        """Continue event numbering after the rows already persisted.

        A fresh bus counts from 1, so a resumed session would re-issue seqs
        the events file already holds and ``events_since`` would skip the
        resumed run's first events. A store may offer ``last_event_seq``;
        otherwise the maximum is taken from a full ``events_since`` read.
        """
        last_event_seq = getattr(self.store, "last_event_seq", None)
        # Failure must abort resume: starting at zero would silently collide
        # with durable events in stores that deduplicate by (session_id, seq).
        if callable(last_event_seq):
            last = int(await last_event_seq(sid))
        else:
            events = await self.store.events_since(sid, 0)
            last = max((e.seq for e in events), default=0)
        self.bus.seed(sid, last)

    @property
    def context_window(self) -> int | None:
        """The window the usage breakdown and the eviction ladder size against.

        An explicit value wins. Otherwise the model id is looked up in
        :mod:`fluid.providers.windows` on every read, so a mid-session model
        switch is reflected. None when neither knows.
        """
        if self._explicit_context_window is not None:
            return self._explicit_context_window
        return context_window_for(self.model)

    @context_window.setter
    def context_window(self, value: int | None) -> None:
        self._explicit_context_window = value

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
            self._peer_delivery_sealed = False
            self._turn_task = asyncio.create_task(self._run_turn(op))
        elif isinstance(op, Interrupt):
            self._interrupted.set()
        elif isinstance(op, Steer):
            # Mid-turn user text: queued here, appended to the NEXT tool
            # result inside the steering marker (fluid.context.steering) —
            # the one role-alternation-safe slot. Delivery is receipted with a
            # SteerDelivered event; if the turn ends first the steer is dropped
            # with a warning (the user can just send a message then).
            #
            # origin="system" is the same channel carrying a harness notice
            # instead: it renders in the harness-notice marker, and its body is
            # defanged on the way in because part of it can be text a sub-agent
            # wrote — which must not be able to close the notice and open the
            # user's marker.
            if op.origin != "user":
                from fluid.tools.hardening import defang_harness_markers

                op = replace(op, text=defang_harness_markers(op.text))
            if self._turn_task is not None and not self._turn_task.done():
                self._pending_steers.append(op)
            else:
                raise RuntimeError(
                    "no turn is running — send a normal message instead of a steer"
                )
        elif isinstance(op, AgentMessage):
            # A peer's text, delivered on whichever channel the receiver's
            # current state leaves open — the branch is taken here, without
            # awaiting, so a sender never has to check "is it busy?" and race
            # the answer. Busy: queue like a steer, rendered in the peer marker
            # so it cannot borrow the user's authority. Idle: an ordinary turn,
            # unless the host owns wake scheduling and disables it on the op.
            from fluid.tools.hardening import defang_harness_markers

            if self._background_closed or self._interrupted.is_set():
                raise RuntimeError("receiver is stopping; peer message was not delivered")
            text = defang_harness_markers(op.text)
            if self._turn_task is not None and not self._turn_task.done():
                if self._peer_delivery_sealed:
                    raise RuntimeError("receiver is finishing its turn; retry the peer message")
                self._pending_steers.append(replace(op, text=text))
            else:
                if not op.wake:
                    raise RuntimeError("no turn is running; peer message was not delivered")
                if not op.urgent and await self._queue_if_parked(replace(op, text=text)):
                    return
                from fluid.context.peer import format_peer_message

                self._interrupted.clear()
                self._peer_delivery_sealed = False
                self._turn_task = asyncio.create_task(self._run_turn(UserTurn(
                    content=format_peer_message(op.sender, text).strip(),
                )))
        elif isinstance(op, RespondPermission):
            fut = self._pending_permissions.get(op.request_id)
            if fut is not None and not fut.done():
                fut.set_result(op)
        elif isinstance(op, RespondQuestion):
            fut = self._pending_questions.get(op.request_id)
            if fut is not None:
                if not fut.done():
                    fut.set_result(op)
            elif not await self._answer_question(op):
                raise ValueError(f"unknown question: {op.request_id}")
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
            }.get(mode, "on_write")
            if self.permissions is not None:
                self.permissions.approval_policy = policy
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
        """Publish to the bus (assigns seq) and persist for durable replay.

        The bus is never optional — live clients render from it. Persistence
        is, for one class of event: the streaming deltas. They exist to make a
        reply visible while it is written, and every one of them is
        reconstructible from the assistant node the turn goes on to store, so
        a store that refuses a single ``ToolArgsDelta`` INSERT costs a gap in
        the replay log and nothing else. It used to cost the turn: the failure
        raised through the agent loop into the catch-all in
        :meth:`_start_and_loop`, which ended the turn on a non-retriable
        ErrorEvent. One transient database blip killed two turns and three
        workers that way. Load-bearing events (ToolUse, ToolResult, Usage, and
        every lifecycle event) still surface their failures — losing one of
        those really does corrupt the record.
        """
        self._turn_progress_at = time.monotonic()
        sequenced = await self.bus.publish(event)
        try:
            await persist_event(self.store, sequenced)
        except Exception as exc:
            if not isinstance(event, _RECONSTRUCTIBLE_EVENTS):
                raise
            self._note_dropped_event(event, exc)
        return sequenced

    def _note_dropped_event(self, event: Event, exc: BaseException) -> None:
        """Count a delta the store refused, logging once per turn.

        Rate-limited on purpose: the failure mode is a store that is down for
        seconds, and a degenerate stream emits thousands of deltas a second.
        One warning names the cause; the rest are counted and reported at the
        end of the turn.
        """
        turn = self._active_turn_id
        if turn != self._dropped_events_turn:
            self._dropped_events_turn = turn
            self._dropped_events_in_turn = 0
            log.warning(
                "store refused a %s event (%s: %s); the turn continues — "
                "streaming deltas are reconstructible from the stored "
                "assistant message",
                type(event).__name__, type(exc).__name__, exc,
            )
        self._dropped_events_in_turn += 1
        self.dropped_delta_events += 1

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
        close_task = getattr(self, "_close_task", None)
        if close_task is None:
            close_task = self._close_task = asyncio.create_task(self._aclose_inner())
        await asyncio.shield(close_task)

    async def _aclose_inner(self) -> None:
        await self._close_background_lifecycle()
        self._interrupted.set()
        if self._turn_task and not self._turn_task.done():
            self._turn_task.cancel()
            try:
                await self._turn_task
            except asyncio.CancelledError:
                pass
        await self._emit_session_end()
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

        The total is the last call's REAL prompt size plus the chars/4 estimate
        of everything appended since (the reply itself, user text, tool
        results) whenever a measured call is on the current path — recorded
        live in ``_last_call_usage``, or recovered from the persisted Usage
        events after a resume. With no call yet, or after a fork/rewind past
        it, the whole total is the estimate.

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
            measured = await self._measured_next_prompt(sid, nodes)
        return self._context_usage_snapshot(
            history=history,
            compaction=compaction_stats,
            measured_prompt_tokens=measured,
        )

    async def _measured_next_prompt(
        self, sid: str, nodes: list[SessionNode]
    ) -> int | None:
        """The last call's billed prompt size, carried forward to the NEXT request.

        The provider measured the prompt that produced the recorded assistant
        node. The next request also carries that node (the reply) and whatever
        was appended after it — user text, tool results — so those are added
        at the chars/4 estimate. None when no measured call is on the current
        path: a fresh session, or a fork/rewind past the measured node.
        """
        recorded = self._last_call_usage.get(sid)
        if recorded is None:
            recorded = await self._rehydrate_last_call_usage(sid, nodes)
        if recorded is None:
            return None
        node_id, usage = recorded
        index = next((i for i, n in enumerate(nodes) if n.node_id == node_id), None)
        if index is None:
            return None
        appended = [n.message for n in nodes[index:]]
        return _measured_prompt_tokens(usage) + estimate_tokens_of_messages(appended)

    async def _rehydrate_last_call_usage(
        self, sid: str, nodes: list[SessionNode]
    ) -> tuple[str, dict[str, Any]] | None:
        """Recover the last call's prompt accounting from the persisted events.

        ``_last_call_usage`` is process memory; the session outlives the
        process. Every model call left a Usage event carrying the billed
        prompt fields and the assistant node it produced, so a resumed engine
        reads the newest one whose node is still on the path instead of
        falling back to chars/4 until its first call. Looked for once per
        session per engine; a store that cannot replay contributes nothing.
        """
        if sid in self._usage_rehydrated:
            return None
        self._usage_rehydrated.add(sid)
        try:
            events = [event async for event in iter_events(self.store, sid, 0)
                      if isinstance(event, Usage)]
        except Exception:
            return None
        on_path = {n.node_id for n in nodes}
        for event in reversed(events):
            if not isinstance(event, Usage) or event.node_id not in on_path:
                continue
            usage = {
                "input_tokens": event.input_tokens,
                "cache_read_input_tokens": event.cache_read_tokens,
                "cache_creation_input_tokens": event.cache_creation_tokens,
            }
            if _measured_prompt_tokens(usage) > 0:
                self._last_call_usage[sid] = (event.node_id, usage)
                return self._last_call_usage[sid]
        return None

    def _deferred_tools_api(self) -> list[dict[str, Any]]:
        """Schemas of known-but-unloaded tools — the ToolSearch catalog.

        Code mode does not make this empty. The host's deferred set is what it
        always was: tools that are callable (they get a kernel stub) but whose
        documentation the prompt does not carry — reachable there with
        ``read_tool_docs`` rather than ToolSearch. Reporting [] hid the very
        saving deferral exists to make.
        """
        if self.registry is None:
            return []
        return [
            s.to_api() for s in self.registry.deferred_tools()
            if self._tool_wired(s.name)
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
            mounted_tools=self._mounted_tool_names(),
        )

    # ─── Turn lifecycle ──────────────────────────────────────────────────

    async def _emit_terminal_with_park(self, event: Event) -> Event:
        if isinstance(event, (TurnComplete, Interrupted, ErrorEvent)):
            # Every terminal path closes admission before its first await,
            # including provider failures and turns blocked before a prompt
            # is written. Acknowledging a peer during publication would let
            # turn cleanup discard it after the sender has reported success.
            self._peer_delivery_sealed = True
            while (
                not isinstance(event, Interrupted)
                and not self._interrupted.is_set()
                and not self._background_closed
                and any(isinstance(op, AgentMessage) for op in self._pending_steers)
            ):
                # Failure must not erase already accepted peers. Retain them
                # on the durable head for an explicit resume, without issuing
                # another provider call or changing the original outcome.
                meta = await self.store.get_session(event.session_id)
                peer_node = await self._finish_peer_messages(
                    event.session_id, event.turn_id,
                    meta.head_node_id if meta is not None else None,
                )
                if peer_node is not None:
                    event = replace(event, node_id=peer_node.node_id)
                else:
                    break  # interrupt/close arrived while reading the head
        # Keep background park/wake serialization and completion delivery in
        # the shared lifecycle. A subsequent explicit or wake turn reopens
        # peer admission normally.
        publication = asyncio.create_task(super()._emit_terminal_with_park(event))
        try:
            return await asyncio.shield(publication)
        except asyncio.CancelledError:
            # Cancellation between bus publication and durable append must not
            # publish a second terminal or close the bus before persistence.
            await publication
            raise

    async def _run_turn(self, op: UserTurn | Resume) -> None:
        self._turn_cancel_reason = None
        self._turn_progress_at = time.monotonic()
        self._start_turn_watchdog()
        try:
            await self._run_turn_inner(op)
        except asyncio.CancelledError:
            if self._active_turn_id and not self._background_turn_terminal:
                partial, cost = self._partial_usage(self._turn_usage)
                if self._background_closed:
                    await self._emit_terminal_with_park(Interrupted(
                        session_id=self.session_id, turn_id=self._active_turn_id,
                        message="engine closed",
                        usage=partial, cost_usd=cost,
                        num_model_calls=self._turn_model_calls,
                    ))
                else:
                    # Cancelled by something other than Engine.aclose (the
                    # watchdog, or a host that cancelled the task): say so
                    # as an error, then end the turn properly so the cost
                    # accumulated so far reaches the session row.
                    reason = self._turn_cancel_reason or "turn cancelled"
                    await self._emit(ErrorEvent(
                        session_id=self.session_id, turn_id=self._active_turn_id,
                        message=reason, retriable=False,
                        usage=partial, cost_usd=cost,
                        num_model_calls=self._turn_model_calls,
                    ))
                    await self._emit_terminal_with_park(TurnComplete(
                        session_id=self.session_id, turn_id=self._active_turn_id,
                        stop_reason="error", num_model_calls=self._turn_model_calls,
                        usage=partial, cost_usd=cost,
                    ))
                    if self._turn_cancel_reason is not None:
                        # The watchdog cancelled us and the turn is now
                        # properly terminated: finish the task normally so
                        # a host awaiting wait_turn() sees a finished turn,
                        # not a CancelledError it never asked for.
                        current = asyncio.current_task()
                        if current is not None:
                            current.uncancel()
                        self._active_turn_id = None
                        return
            raise
        finally:
            self._stop_turn_watchdog()
            # Covers early hook/Resume exits and cancellation before the
            # agent loop starts, as well as its normal terminal paths. Stop
            # must never leave queued peers or a failed write to a later turn.
            self._peer_delivery_sealed = True
            self._pending_steers.clear()
            self._pending_peer_write = None
            self._active_turn_id = None

    def _start_turn_watchdog(self) -> None:
        limit = self.turn_watchdog_seconds
        if not limit or limit <= 0:
            return
        self._stop_turn_watchdog()
        try:
            self._watchdog_task = asyncio.get_running_loop().create_task(
                self._turn_watchdog(float(limit)), name="fluid-turn-watchdog",
            )
        except RuntimeError:
            self._watchdog_task = None

    def _stop_turn_watchdog(self) -> None:
        task, self._watchdog_task = self._watchdog_task, None
        if task is not None and not task.done():
            task.cancel()

    def _human_wait_pending(self) -> bool:
        """A permission or question is out to the user: silence is not a hang."""
        for futures in (self._pending_permissions, getattr(self, "_pending_questions", {})):
            for fut in list(futures.values()):
                if not fut.done():
                    return True
        return False

    async def _turn_watchdog(self, limit: float) -> None:
        """Cancel the turn after ``limit`` seconds without any emitted event.

        Polls at a quarter of the limit (at least once a second). A pending
        permission or question resets the clock: waiting on a person is not
        a hang. When it fires, the cancel reason names the silence so the
        ErrorEvent the cancel path emits says why the turn ended.
        """
        interval = max(1.0, limit / 4)
        while True:
            await asyncio.sleep(interval)
            if self._human_wait_pending():
                self._turn_progress_at = time.monotonic()
                continue
            silent = time.monotonic() - self._turn_progress_at
            if silent < limit:
                continue
            task = self._turn_task
            if task is None or task.done() or task is asyncio.current_task():
                return
            self._turn_cancel_reason = (
                f"turn watchdog: no tool result or stream progress for "
                f"{silent:.0f}s (limit {limit:.0f}s); the turn was cancelled "
                "so its cost is recorded and the session can continue"
            )
            log.warning("%s (turn %s)", self._turn_cancel_reason, self._active_turn_id)
            task.cancel()
            return

    async def _run_turn_inner(self, op: UserTurn | Resume) -> None:
        self._peer_delivery_sealed = False
        meta = await self._ensure_session()
        sid = meta.session_id
        turn_id = new_id("turn")
        self._background_turn_terminal = False
        self._turn_model_calls = 0
        self._turn_usage = {}
        self._active_turn_id = turn_id

        # Connect config MCP servers before building the user node so newly
        # known deferred tools can be announced on this very turn.
        await self._connect_mcp_once(sid, turn_id)
        await self._rehydrate_deferred_announced(sid, meta.head_node_id)
        await self._rehydrate_read_files(sid, meta.head_node_id)
        await self._rehydrate_runtime_facts(sid, meta.head_node_id)
        await self._rehydrate_queued_peer_messages(sid)
        await self._rehydrate_compaction(sid)
        await self._rehydrate_prefix_fingerprint(sid)
        await self._resolve_session_thinking(sid)

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
                    thinking=self._session_thinking.get(sid),
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
            await self._emit(TurnStarted(
                session_id=sid, node_id=head.node_id, turn_id=turn_id,
                model=self.model, provider=self.provider.name,
                thinking=self._session_thinking.get(sid),
            ))
            blocked, _ = await self._turn_start_hook(
                sid, turn_id, head.message.text(), with_context=False
            )
            if blocked:
                return
            await self._start_and_loop(sid, turn_id, head)
            return

        op, question_ids = await self._question_turn_content(op)
        content: list[Any] = (
            [Text(op.content)] if isinstance(op.content, str) else list(op.content)
        )
        prompt_text = op.content if isinstance(op.content, str) else _plain_text(content)
        reminder = self._deferred_catalog_reminder()
        if reminder is not None:
            content.append(Text(reminder))
        ledger = self._ledger_reminder()
        if ledger is not None:
            content.append(Text(ledger))
        notes = self._notes_reminder()
        if notes is not None:
            content.append(Text(notes))
        env_note = await self._environment_reminder()
        if env_note is not None:
            content.append(Text(env_note))
        queued = self._queued_peer_reminder()
        if queued is not None:
            content.append(Text(queued))
        # The node id is fixed before the TurnStart hooks run so TurnStarted
        # can name it; the node itself is only written once they allow the
        # prompt, so a blocked prompt never enters the session.
        node_id = new_id("node")
        await self._emit(TurnStarted(
            session_id=sid, node_id=node_id, turn_id=turn_id,
            model=self.model, provider=self.provider.name,
            thinking=self._session_thinking.get(sid),
        ))
        blocked, hook_context = await self._turn_start_hook(sid, turn_id, prompt_text)
        if blocked:
            return
        if hook_context:
            content.append(Text(hook_context))
        user_node = SessionNode(
            node_id=node_id,
            parent_id=op.parent_node_id or meta.head_node_id,
            message=Message(role="user", content=content),
        )
        await self.store.append(sid, user_node)
        await self._mark_questions_delivered(question_ids)
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
        """The half of a turn that runs once the user node exists and the
        TurnStart hooks let the prompt through: run the agent loop and end on
        a terminal event. Shared by UserTurn (which just wrote the node) and
        Resume (which continues from the node already at the head). The
        TurnEnd hooks fire on every way out except cancellation, which is the
        engine closing."""
        cancelled = False
        try:
            await self._agent_loop(sid, turn_id, from_node)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception as exc:  # surface, never swallow
            transient = _is_transient_error(exc)
            log.warning(
                "turn %s ended on an unhandled %s", turn_id, type(exc).__name__,
                exc_info=True,
            )
            # Whatever the turn billed before it fell over is still billed.
            partial, partial_cost = self._partial_usage(self._turn_usage)
            await self._emit_terminal_with_park(
                ErrorEvent(
                    session_id=sid, turn_id=turn_id,
                    message=_describe_engine_failure(exc, transient=transient),
                    retriable=transient,
                    usage=partial, cost_usd=partial_cost,
                    num_model_calls=self._turn_model_calls,
                )
            )
        finally:
            self._peer_delivery_sealed = True
            if not cancelled and not self._turn_end_hook_fired:
                # The loop left early: a provider error, an interrupt, a
                # hook's stop, or an exception. The terminal event is already
                # out; the hooks still get their turn.
                reason = self._turn_end_reason or (
                    "interrupted" if self._interrupted.is_set() else "error"
                )
                try:
                    await self._emit_turn_end_hook(sid, turn_id, text="", stop_reason=reason)
                except Exception:  # noqa: BLE001 — never mask the turn's own outcome
                    log.warning("TurnEnd hooks failed", exc_info=True)
            # _run_turn owns the active id through cancellation settlement.
            # Interrupted turns discard their pending interjections. Other
            # terminal paths have already retained accepted peers in history;
            # nothing queued should leak into a later, unrelated user request.
            self._pending_steers.clear()

    async def _connect_mcp_once(self, sid: str, turn_id: str) -> None:
        """Connect configured MCP servers on the first turn (idempotent).

        Their tools become searchable/loadable via ToolSearch (progressive
        disclosure) without entering the prompt schema. A server that fails to
        connect surfaces as a non-fatal ErrorEvent; the turn proceeds.
        """
        if self._mcp_connected or not self._mcp_servers or self.registry is None:
            return
        self._mcp_connected = True
        registry = self.registry

        async def connect_one(name: str, cfg: Any) -> None:
            try:
                await registry.connect_mcp(name, dict(cfg))
            except Exception as exc:
                await self._emit(ErrorEvent(
                    session_id=sid, turn_id=turn_id,
                    message=f"mcp server {name!r} failed to connect: {exc!r}",
                    retriable=False,
                ))

        # Servers connect side by side, so the first turn waits for the
        # slowest one rather than the sum; a connect timeout is reported by
        # connect_one like any other failure.
        await asyncio.gather(
            *(connect_one(name, cfg) for name, cfg in self._mcp_servers.items()),
            return_exceptions=True,
        )

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

    async def _rehydrate_runtime_facts(self, sid: str, head_node_id: str | None) -> None:
        """Seed the last-sent runtime facts from the stored transcript (once).

        A fresh engine over an existing session would otherwise re-send the
        runtime-facts block on its first turn even when nothing changed —
        every park/wake is a fresh engine in the app, so that would be most
        turns. The block's body is recovered from the newest user node that
        carries it.
        """
        if self._runtime_facts_sent is not None or self._runtime_facts_rehydrated:
            return
        self._runtime_facts_rehydrated = True
        if head_node_id is None:
            return
        try:
            nodes = await self.store.load_path(sid, head_node_id)
        except Exception:  # a store that cannot replay just re-sends once
            return
        for node in reversed(nodes):
            if node.message.role != "user":
                continue
            for block in node.message.content:
                text = getattr(block, "text", None)
                if not isinstance(text, str) or _RUNTIME_FACTS_HEADER not in text:
                    continue
                start = text.index(_RUNTIME_FACTS_HEADER) + len(_RUNTIME_FACTS_HEADER)
                end = text.find("\n</system-reminder>", start)
                self._runtime_facts_sent = text[start:end if end >= 0 else None].strip("\n")
                return

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
        seen: dict[str, tuple[str, tuple[int, int | None] | None]] = {}
        for node in nodes:
            for block in node.message.content:
                if isinstance(block, ToolUseBlock):
                    path = _read_ledger_path(block, self.ctx)
                    if path is not None:
                        seen[block.id] = (path, _read_ledger_window(block))
                elif isinstance(block, ToolResultBlock) and block.is_error:
                    seen.pop(block.tool_use_id, None)
        for path, window in seen.values():
            self.ctx.read_files.add(path)
            if window is None:
                read_window.record_whole(self.ctx, path)
            else:
                read_window.record(self.ctx, path, *window)

    async def _environment_reminder(self) -> str | None:
        """The volatile half of the environment block, re-rendered per turn.

        The compiled system prompt carries the FIXED environment facts (cwd,
        platform, shell, project manifests); the date and the git state change
        under a session and used to be compiled in with them, so every new day
        and every commit rewrote the prompt prefix and cost the cache. Only a
        prompt compiled with an environment section gets the full note — a
        host that injects its own environment keeps its date and git lines.
        The RUNTIME facts (which Python has pytest, which packages lack
        node_modules) are independent of that section: for any compiled
        prompt they ride their own small block on the first turn and
        whenever they change, because a host that suppressed the environment
        section used to lose them too and its sessions re-discovered the same
        facts by hand. A bare system string (no sections) still gets nothing. Gathering runs
        git and stats the tree, so it runs off the loop. Either way the facts
        are also harvested into the memory store (see
        :func:`fluid.context.runtime_facts.harvest_environment_facts`).
        """
        sections = self.system_sections.sections if self.system_sections else ()
        has_env_section = any(s.kind == "environment" for s in sections)
        try:
            note, facts = await asyncio.to_thread(
                self._gather_environment, has_env_section
            )
        except Exception:  # never let a git hiccup block a turn
            log.debug("environment note failed", exc_info=True)
            return None
        if has_env_section:
            return note
        if self.system_sections is None:
            # A bare system string (no compiled sections): the host did not
            # go through compile_prompt and gets no reminders from fluid.
            return None
        if facts == self._runtime_facts_sent:
            return None
        self._runtime_facts_sent = facts
        return f"<system-reminder>\n{_RUNTIME_FACTS_HEADER}{facts}\n</system-reminder>"

    def _gather_environment(self, full: bool) -> tuple[str | None, str]:
        """Thread-side half of :meth:`_environment_reminder`: the full note
        when the prompt has an environment section, plus the runtime facts
        text on their own for the change check. Also harvests the facts into
        the memory store once per engine (file I/O, so it belongs here off
        the loop)."""
        from fluid.context.runtime_facts import runtime_facts

        facts = runtime_facts(self.ctx.cwd)
        note = environment_note(self.ctx.cwd) if full else None
        self._harvest_runtime_facts()
        return note, facts

    def _harvest_runtime_facts(self) -> None:
        """Write mechanically established environment facts to the memory
        store, once per engine instance. Best-effort and silent."""
        if self._runtime_facts_harvested:
            return
        self._runtime_facts_harvested = True
        store = self.ctx.extra.get("memory_store") or self.ctx.extra.get("recall_store")
        if store is None or not hasattr(store, "save_memory"):
            return
        from fluid.context.runtime_facts import (
            harvest_environment_facts,
            write_harvested_facts,
        )

        try:
            facts = harvest_environment_facts(self.ctx.cwd)
            write_harvested_facts(store, self.ctx.extra.get("memory_project"), facts)
        except Exception:  # noqa: BLE001 — never let bookkeeping touch a turn
            log.debug("runtime fact harvest failed", exc_info=True)

    async def _queue_if_parked(self, op: AgentMessage) -> bool:
        """Hold an idle-time peer message when the session is parked.

        Parked on background work (typically the sender itself): the message
        waits for the wake the completion brings instead of paying a turn
        now to read an interim note. It is persisted as a
        ``SubagentEvent(kind="progress")`` so a host that evicts the parked
        engine and rebuilds it for the wake still delivers it (see
        :meth:`_rehydrate_queued_peer_messages`). Returns False when the
        session is not parked, in which case the caller wakes as usual.
        """
        async with self._background_lock:
            await self._rehydrate_background_lifecycle_unlocked()
            parked = bool(self._park_pending_ids())
        if not parked:
            return False
        meta = await self._ensure_session()
        self._queued_peer_messages.append(op)
        await self._emit(SubagentEvent(
            session_id=meta.session_id, turn_id=None,
            task_id=f"peer:{op.sender}", kind="progress",
            description=op.sender, summary=op.text,
            wake=False, wake_disposition=WAKE_NOT_APPLICABLE,
        ))
        return True

    async def _rehydrate_queued_peer_messages(self, sid: str) -> None:
        """Recover peer messages queued while parked from the event log (once).

        A ``progress`` SubagentEvent with a ``peer:`` task id is a queued
        message; a later TurnStarted means it was delivered with that turn.
        """
        if self._queued_peer_rehydrated:
            return
        self._queued_peer_rehydrated = True
        try:
            events = [event async for event in iter_events(self.store, sid, 0)
                      if isinstance(event, (TurnStarted, SubagentEvent))]
        except Exception:  # noqa: BLE001 — stores without replay degrade in memory
            return
        queued: list[AgentMessage] = []
        for event in events:
            if isinstance(event, TurnStarted):
                queued.clear()
            elif (
                isinstance(event, SubagentEvent)
                and event.kind == "progress"
                and str(event.task_id).startswith("peer:")
                and event.summary
            ):
                queued.append(AgentMessage(
                    text=str(event.summary), sender=str(event.description or "peer"),
                ))
        if queued:
            self._queued_peer_messages = queued + self._queued_peer_messages

    def _queued_peer_reminder(self) -> str | None:
        """Peer messages held while parked, rendered once for this turn."""
        queued, self._queued_peer_messages = self._queued_peer_messages, []
        if not queued:
            return None
        from fluid.context.peer import format_peer_message

        parts = [
            "<system-reminder>",
            "Messages from other agents that arrived while this session was "
            "parked (they were not delivered separately; a subagent's final "
            "report is the completion below, if any):",
        ]
        for message in queued:
            parts.append(format_peer_message(message.sender, message.text).strip())
        parts.append("</system-reminder>")
        return "\n\n".join(parts)

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
            if s.name not in self._deferred_announced and self._tool_wired(s.name)
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
            events = [event async for event in iter_events(self.store, sid, 0)
                      if isinstance(event, ContextCompaction)]
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

    async def _resolve_session_thinking(self, sid: str) -> None:
        """Fix this session's thinking setting, once per session per engine.

        A session with no recorded turn is new and takes the host's setting.
        A session that already has turns keeps the setting its newest
        ``TurnStarted`` recorded, whatever the host passes now; turns recorded
        before the field existed ran without thinking, so they read as None.
        That is what keeps a resumed history from having thinking switched on
        (or off) under it.
        """
        if sid in self._session_thinking:
            return
        if self.thinking is None and not getattr(
            self.provider.caps, "signed_thinking", False
        ):
            # No setting to apply, and a provider that sends none: skip the
            # event scan.
            self._session_thinking[sid] = None
            return
        try:
            recorded = [event async for event in iter_events(self.store, sid, 0)
                        if isinstance(event, TurnStarted)]
        except Exception:  # noqa: BLE001 — unreadable history reads as resumed
            log.warning(
                "could not read session %s turns; not applying thinking", sid,
                exc_info=True,
            )
            self._session_thinking[sid] = None
            return
        if recorded:
            setting = recorded[-1].thinking
            self._session_thinking[sid] = dict(setting) if setting else None
        else:
            self._session_thinking[sid] = dict(self.thinking) if self.thinking else None

    async def _rehydrate_prefix_fingerprint(self, sid: str) -> None:
        """Recover the previous request's prefix fingerprint from the events.

        ``_prev_prefix_fingerprint`` is process memory; the session outlives
        the process, and a host that rebuilds the engine per run or per turn
        starts every turn with none. Every model call already persisted its
        fingerprint on the Usage event, so the newest one IS the previous
        request — read it back and the first call of a turn gets a real
        diagnosis instead of ``no_previous_request``.

        Looked for once per session per engine, and never overwrites a
        fingerprint this engine recorded itself. A store that cannot replay
        contributes nothing.
        """
        if not self._cache_probe.enabled or sid in self._prefix_rehydrated:
            return
        self._prefix_rehydrated.add(sid)
        if self._prev_prefix_fingerprint is not None:
            return
        try:
            events = [event async for event in iter_events(self.store, sid, 0)
                      if isinstance(event, Usage)]
        except Exception:  # noqa: BLE001 — instrumentation is never fatal
            return
        for event in reversed(events):
            if not isinstance(event, Usage):
                continue
            hashes = event.raw.get("prefix_hashes")
            if not isinstance(hashes, dict) or not hashes:
                continue
            # Dict order is prefix order and JSON round-trips preserve it, so
            # the rehydrated fingerprint bisects exactly like a live one.
            recovered = {
                key: value
                for key, value in hashes.items()
                if isinstance(key, str) and isinstance(value, str)
            }
            if recovered:
                self._prev_prefix_fingerprint = recovered
            return

    async def _emit_compaction(
        self, sid: str, turn_id: str, promo: Promotion
    ) -> None:
        await self._emit(ContextCompaction(
            session_id=sid, turn_id=turn_id,
            tier=promo.tier, reason=promo.reason,
            trimmed=[list(p) for p in promo.trimmed],
            spans=[list(s) for s in promo.spans],
            reclaimed_chars=promo.reclaimed_chars,
            before_tokens=promo.before_tokens, after_tokens=promo.after_tokens,
            decision_reason=promo.decision_reason,
            first_changed_message=promo.first_changed_message,
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


    # ─── Agent loop (model call → tool dispatch → repeat) ────────────────

    async def _agent_loop(self, sid: str, turn_id: str, user_node: SessionNode) -> None:
        parent = user_node
        num_calls = 0
        self._turn_model_calls = 0
        # A run of misses is only interesting inside one turn: the gap between
        # turns is user time, and a cache that expired over it is ordinary.
        self._unexplained_misses = 0
        self._cache_miss_alerted = False
        self._begin_friction_turn()
        # Shared by reference with _start_and_loop's catch-all, so a turn that
        # dies on an exception still reports what it spent.
        total_usage: dict[str, Any] = {}
        self._turn_usage = total_usage
        final_stop = "end_turn"
        overflow_retries = 0
        result: _CallResult | None = None
        empty_retries = 0  # CONSECUTIVE empty completions; any real call resets it
        continuations = 0
        stop_hook_continuations = 0
        arg_stream_retries = 0  # runaway tool-argument streams, retried once

        while True:
            nodes = await self.store.load_path(sid, parent.node_id)
            rendered, promotions = self._compactor.apply(
                nodes,
                overhead_tokens=self._context_overhead_tokens(),
                context_window=self.context_window,
                output_reserve_tokens=self.max_tokens or _DEFAULT_OUTPUT_RESERVE,
                # Only the FIRST call of a turn may rewrite already-sent
                # history for reasons other than size pressure. Between two
                # calls of one turn the provider is holding a prefix it built
                # seconds ago; rewriting it there re-bills the whole prompt.
                turn_boundary=num_calls == 0,
            )
            history = repair_dangling_tool_calls(rendered)
            if self._encrypted_reasoning_poisoned:
                # A prior call proved the history holds reasoning blobs the
                # current model+account cannot decrypt; render without them
                # rather than pay another rejected request (the stored tree
                # keeps them — a switch back could decrypt them again).
                history = strip_opaque_reasoning(history)
            if promotions:
                self._history_rewrites.extend(describe_promotions(promotions))
                await self._emit_pre_compact(sid, turn_id, promotions)
            for promo in promotions:
                await self._emit_compaction(sid, turn_id, promo)
            projected_prompt = self._estimate_request_input_tokens(history)
            call_was_sticky = not self._sticky_poisoned and empty_retries == 0
            num_calls += 1
            self._turn_model_calls = num_calls
            result = await self._stream_once(
                sid, turn_id, history, sticky=call_was_sticky
            )
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
                # Rotate the prefix fingerprint past the dud call too, so the
                # next cache-miss diagnosis diffs against the request that
                # was actually sent last, not the one before it.
                self._cache_probe_fields(result.usage)
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
            if result.arg_stream_error is not None:
                if (
                    arg_stream_retries < _ARG_STREAM_MAX_RETRIES
                    and not self._interrupted.is_set()
                ):
                    # A degenerate generation is usually a one-off: the audited
                    # sessions all re-issued the identical cell successfully on
                    # the next attempt. Re-send the same history — nothing is
                    # appended, so the prompt and its cache prefix are
                    # unchanged — and bill what the abandoned call produced.
                    arg_stream_retries += 1
                    self._fold_call_usage(total_usage, result.usage)
                    usage_raw = dict(result.usage)
                    usage_raw["model_call_index"] = num_calls
                    usage_raw["projected_prompt_tokens"] = projected_prompt
                    usage_raw["tool_arg_stream_aborted"] = result.arg_stream_error
                    usage_raw.update(self._cache_probe_fields(result.usage))
                    await self._emit(Usage(
                        session_id=sid, node_id=parent.node_id, turn_id=turn_id,
                        input_tokens=result.usage.get("input_tokens", 0),
                        output_tokens=result.usage.get("output_tokens", 0),
                        cache_read_tokens=result.usage.get(
                            "cache_read_input_tokens", 0
                        ),
                        cache_creation_tokens=result.usage.get(
                            "cache_creation_input_tokens", 0
                        ),
                        raw=usage_raw,
                    ))
                    await self._emit(ProviderRetry(
                        session_id=sid, turn_id=turn_id,
                        attempt=arg_stream_retries,
                        max_attempts=_ARG_STREAM_MAX_RETRIES,
                        reason=(
                            "abandoned a runaway tool-argument stream; "
                            "reissuing the call"
                        ),
                        delay_seconds=0.0, model=self.model,
                    ))
                    continue
                if self.registry is None or result.arg_stream_tool is None:
                    # No tool surface to answer on — end the turn instead.
                    result.stop_reason = "error"
                    result.error_message = result.arg_stream_error
                    result.error_retriable = True
                else:
                    # Retry spent. Keep the abandoned call in the transcript so
                    # the model reads why it failed and can reissue it — the
                    # same route a truncated argument payload takes.
                    result.tool_uses.append(result.arg_stream_tool)
                    await self._emit(ToolUse(
                        session_id=sid, turn_id=turn_id,
                        tool_use_id=result.arg_stream_tool["tool_use_id"],
                        name=result.arg_stream_tool["name"], input={},
                    ))
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
                await self._alert_unexplained_cache_misses(sid, turn_id, usage_raw)
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
                    and _is_context_overflow(
                        result.error_message or "", result.error_status
                    )
                ):
                    # The prompt outgrew the window: force the next rung of
                    # the eviction ladder and retry on the smaller view. Each
                    # rung is a batch (tier 1 trim → tier 2 digests → tier 3
                    # epoch folds, then both again with the recency guard
                    # off); when no rung can reclaim anything, fall through
                    # to the normal error path. With a known window the
                    # compactor climbs as many rungs as the estimate needs in
                    # one go.
                    escalations = self._compactor.escalate(
                        nodes,
                        overhead_tokens=self._context_overhead_tokens(),
                        context_window=self.context_window,
                        output_reserve_tokens=(
                            self.max_tokens or _DEFAULT_OUTPUT_RESERVE
                        ),
                    )
                    if escalations:
                        overflow_retries += 1
                        self._history_rewrites.extend(
                            describe_promotions(escalations)
                        )
                        await self._emit_pre_compact(sid, turn_id, escalations)
                        for promo in escalations:
                            await self._emit_compaction(sid, turn_id, promo)
                        continue
                # The failed call's usage was folded in above (before the
                # escalation decision) — snapshot it, don't re-fold.
                if result.usage.get("transport_failure"):
                    # Keep visible text and completed tool requests for a later
                    # user-directed continuation. Incomplete arguments and opaque
                    # reasoning envelopes are not safe provider history.
                    completed_ids = {tu["tool_use_id"] for tu in result.tool_uses}
                    content = [
                        block for block in result.message.content
                        if isinstance(block, Text) or (
                            isinstance(block, ToolUseBlock) and block.id in completed_ids
                        )
                    ]
                    if content:
                        node = SessionNode(
                            node_id=new_id("node"), parent_id=parent.node_id,
                            message=Message(role="assistant", content=content),
                        )
                        await self.store.append(sid, node)
                        parent = node
                    aborted = []
                    for tu in result.tool_uses:
                        text = "Tool not executed: the provider connection was interrupted before the response finished."
                        await self._emit(ToolResult(
                            session_id=sid, turn_id=turn_id,
                            tool_use_id=tu["tool_use_id"], name=tu["name"],
                            output=text, is_error=True,
                        ))
                        aborted.append(ToolResultBlock(
                            tool_use_id=tu["tool_use_id"], content=[Text(text)], is_error=True,
                        ))
                    if aborted:
                        node = SessionNode(
                            node_id=new_id("node"), parent_id=parent.node_id,
                            message=Message(role="user", content=aborted),
                        )
                        await self.store.append(sid, node)
                        parent = node
                partial, partial_cost = self._partial_usage(total_usage)
                await self._emit_terminal_with_park(ErrorEvent(
                    session_id=sid, node_id=parent.node_id, turn_id=turn_id,
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
            await self._alert_unexplained_cache_misses(sid, turn_id, usage_raw)
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
            # a registry a tool_use stop is terminal (M0 behavior). With one,
            # every emitted tool call is dispatched whatever the stop reason
            # says: llama.cpp, vLLM and Ollama builds report ``stop`` next to
            # tool_calls (mapped to ``end_turn``), and a stream that ran out of
            # output budget mid-call still emitted ToolUse events for the
            # blocks that closed. Leaving any of them unanswered puts a
            # ToolUse with no ToolResult in the transcript, which the next
            # turn can only repair as "its result was lost".
            dispatchable = self.registry is not None and bool(result.tool_uses)
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
                can_continue = (self.max_model_calls_per_turn is None
                                or num_calls < self.max_model_calls_per_turn)
                if not can_continue:
                    self._peer_delivery_sealed = True
                peer_node = await self._finish_peer_messages(sid, turn_id, parent.node_id)
                if peer_node is not None:
                    parent = peer_node
                    if can_continue:
                        continue
                    # Preserve delivery in history without bypassing the
                    # caller's model-call budget. A later turn can resume it.
                    final_stop = "max_calls"
                    break
                # No await between the empty-queue check and this seal:
                # submit either queues a message we consume or returns an
                # explicit retry error during terminal publication.
                self._peer_delivery_sealed = True
                final_stop = result.stop_reason
                reason = await self._stop_hook_continuation(
                    sid, turn_id, result, stop_hook_continuations
                )
                if reason is not None:
                    # A Stop hook blocked the end of the turn: the reason goes
                    # to the model as a user message and the loop runs again.
                    stop_hook_continuations += 1
                    nudge = SessionNode(
                        node_id=new_id("node"), parent_id=parent.node_id,
                        message=Message(role="user", content=[Text(
                            _hook_reminder("A Stop hook asked you to continue", reason)
                        )]),
                    )
                    await self.store.append(sid, nudge)
                    parent = nudge
                    self._peer_delivery_sealed = False
                    continue
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
            answers = await self.pending_question_answers()
            if answers and blocks and not self._interrupted.is_set():
                blocks[-1] = replace(
                    blocks[-1], content=[*blocks[-1].content, Text(self._question_text(answers))],
                )
            else:
                answers = []
            tr_node = SessionNode(
                node_id=new_id("node"), parent_id=parent.node_id,
                message=Message(role="user", content=blocks),
            )
            await self.store.append(sid, tr_node)
            await self._mark_questions_delivered(a.request_id for a in answers)
            parent = tr_node
            if call_cap_reached:
                final_stop = "max_calls"
                break
            if self._hook_stop_reason is not None:
                # A PreToolUse or PostToolUse hook answered ``continue: false``:
                # the tool results are in the transcript, no further model call.
                reason, self._hook_stop_reason = self._hook_stop_reason, None
                self._turn_end_reason = "stopped"
                partial, partial_cost = self._partial_usage(total_usage)
                await self._emit_terminal_with_park(ErrorEvent(
                    session_id=sid, node_id=parent.node_id, turn_id=turn_id,
                    message=f"stopped by hook: {reason}", retriable=False,
                    usage=partial, cost_usd=partial_cost,
                    num_model_calls=num_calls,
                ))
                return
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

        # A call-limit exit can follow a tool response instead of final text.
        # Preserve any peers accepted while that last response was written,
        # but never issue another model call beyond the configured limit.
        self._peer_delivery_sealed = True
        peer_node = await self._finish_peer_messages(sid, turn_id, parent.node_id)
        if peer_node is not None:
            parent = peer_node
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
        if not self._turn_end_hook_fired:
            # The max_calls path ends the turn without a final reply.
            await self._emit_turn_end_hook(
                sid, turn_id,
                text=_plain_text(result.message.content) if result is not None else "",
                stop_reason=final_stop or "end_turn",
            )
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

    async def _finish_peer_messages(
        self, sid: str, turn_id: str | None, parent_id: str | None,
    ) -> SessionNode | None:
        """Retain peers when no next tool result will carry them.

        Normal final text may continue the turn over the new node. Failure
        only persists it for a later resume; user steers retain their existing
        explicit turn-end fallback.
        """
        if self._interrupted.is_set() or self._background_closed:
            return None
        if self._pending_peer_write is None:
            peers = [op for op in self._pending_steers if isinstance(op, AgentMessage)]
            if not peers:
                return None
            delivered = [_steer_parts(op) for op in peers]
            node = SessionNode(
                node_id=new_id("node"), parent_id=parent_id,
                message=Message(role="user", content=[Text("".join(
                    _render_interjection(text, sender, origin)
                    for _, text, sender, origin in delivered
                ).strip())]),
            )
            self._pending_peer_write = (node, peers)
        else:
            node, peers = self._pending_peer_write
            delivered = [_steer_parts(op) for op in peers]
        await self.store.append(sid, node)
        self._pending_peer_write = None
        # Remove only the persisted batch, after its write succeeds. A peer
        # arriving during that await belongs to the next batch, even when its
        # fields equal an earlier message. Receipt failure must not reappend
        # an already durable batch either.
        persisted = {id(op) for op in peers}
        self._pending_steers[:] = [
            op for op in self._pending_steers if id(op) not in persisted
        ]
        await self._emit(SteerDelivered(
            session_id=sid, node_id=node.node_id, turn_id=turn_id,
            steers=[_steer_receipt(*parts) for parts in delivered],
        ))
        return node

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

    def _recall_tools_on_surface(self) -> bool:
        """Can this session call both recall builtins?"""
        if self.registry is None:
            return False
        return all(
            self.registry.get(name) is not None and self._tool_permitted(name)
            for name in ("HistoryExpand", "HistorySearch")
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
            if spec is not None and spec.source in (CODE_MODE_SOURCE, SKILLS_SOURCE):
                # Code mode's four tools ARE the schema surface
                # (``register_code_mode_tools``): the CE12 allowlist names
                # WORK tools, and the kernel enforces it through the stamped
                # ``tool_filter``. An allowlist of work tools must not strip
                # the surface that reaches them — with every schema filtered
                # the model can only narrate its calls as text. The skills
                # pair (save_skill/delete_skill) is part of that same
                # harness surface: an allowlist of work tools must not
                # silently amputate the learning loop either. Deny still
                # beats everything.
                return not _matches_tool_pattern(name, self.disallowed_tools)
        if self.allowed_tools is not None and not _matches_tool_pattern(
            name, self.allowed_tools
        ):
            return False
        return not _matches_tool_pattern(name, self.disallowed_tools)

    def _tool_wired(self, name: str) -> bool:
        """Permitted (:meth:`_tool_permitted`) AND backed by a wired capability.

        The predicate every catalog is read through — the deferred schemas,
        the deferred-catalog reminder, ToolSearch, the usage accounting — so a
        ``requires_extra`` tool whose host injection is missing is invisible
        there too, exactly as :meth:`_tools_api` withholds it from the request.
        Filtered on permission alone, an audited host that never wired agent
        messaging still listed SendMessage and ListAgents in ToolSearch and
        billed them in the context breakdown; models loaded them and every
        call failed.
        """
        if not self._tool_permitted(name):
            return False
        if self.registry is None:
            return True
        spec = self.registry.get(name)
        return spec is None or self._capability_present(spec)

    def _mounted_tool_names(self) -> frozenset[str] | None:
        """Every registered name this leg actually serves; None without a registry."""
        if self.registry is None:
            return None
        return frozenset(
            s.name for s in self.registry.list_tools(loaded_only=False)
            if self._tool_wired(s.name)
        )

    def _tool_available(self, name: str) -> bool:
        """Permitted and backed by a wired host capability.

        The JSON surface drops a ``requires_extra`` tool whose injection is
        missing (:meth:`_capability_present`), but code mode projected it
        anyway — so a kernel cell could import ``SessionSearch`` and get
        "unavailable: no session store directory injected" back as a tool
        error. Absent capability must be invisible on both surfaces.

        """
        if not self._tool_permitted(name):
            return False
        if self.registry is None:
            return True
        spec = self.registry.get(name)
        if spec is None:
            return True
        return self._capability_present(spec)

    def _tools_api(self) -> list[dict[str, Any]]:
        """The tool schemas the next request would carry (code-mode surface or
        the loaded registry, filtered by the session's tool restrictions).
        Pure — no request is built."""
        if self.registry is None:
            return []
        if self._codemode is not None:
            # Code-mode replaces N schemas with the four-tool surface; the
            # work-tools stay in the registry (reachable via host calls) but
            # never enter the prompt schema.
            specs = [
                s for s in self.registry.list_tools(loaded_only=True)
                if s.source == "codemode"
            ]
        else:
            specs = self.registry.list_tools(loaded_only=True)
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
        sid = self._session.session_id if self._session is not None else None
        return ProviderRequest(
            model=self.model,
            messages=history,
            system=self.system,
            tools=tools_api,
            max_tokens=self.max_tokens,
            effort=self.effort,
            thinking=self._session_thinking.get(sid) if sid else None,
            cache_breakpoints=plan.breakpoints,
            # Per-provider cache-affinity key first, host passthrough on top
            # so the host's keys win.
            extra={**self._affinity_keys(sticky), **self.request_extra},
        )

    def _affinity_key(self) -> str | None:
        """The value the provider's cache-affinity key carries.

        ``cache_key`` (host-supplied) wins over the fluid session id. The
        session id is the right default — one fluid session, one prefix — but
        it is not durable: a host that resumes a conversation on a NEW fluid
        session repartitions the provider cache at exactly the moment the
        prefix is longest. A host with its own durable conversation id passes
        it here and the partition survives the resume.
        """
        if self.cache_key:
            return self.cache_key
        return self._session.session_id if self._session is not None else None

    def _affinity_keys(self, sticky: bool) -> dict[str, Any]:
        """The provider's prompt-cache affinity key.

        OpenRouter documents ``session_id`` as its sticky-routing key; the
        OpenAI Responses API takes ``prompt_cache_key`` for the same job
        (routing requests that share a prefix to the same cache). Other
        providers get nothing. ``sticky=False`` drops the key so OpenRouter
        may pick a different upstream replica (see the empty-completion retry
        in _agent_loop). Empty before a session exists.
        """
        key = self._affinity_key()
        if not sticky or key is None:
            return {}
        if self.provider.name == "openrouter":
            return {"session_id": key}
        if self.provider.name == "openai_responses":
            return {"prompt_cache_key": key}
        return {}

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
                request_identity={
                    "provider": self.provider.name, "model": request.model,
                    "max_tokens": request.max_tokens, "effort": request.effort,
                    "thinking": request.thinking, "extra": request.extra,
                    "cache_breakpoints": request.cache_breakpoints,
                    "endpoint": getattr(self.provider, "_base_url", None),
                },
                # Snapshot the end of the previous request's history too, so
                # the two fingerprints share a checkpoint at the end of their
                # common prefix. Powers of two alone leave everything between
                # the last one and the shared count uncompared — with 63
                # messages that is 33..63, a third of the history.
                extra_checkpoints=(message_count(self._prev_prefix_fingerprint),),
            )
            if self.provider.name == "openai_responses":
                from fluid.context.cache_probe import fingerprint_responses_request
                fingerprint_responses_request(
                    request, self._prefix_fingerprint,
                    previous_count=message_count(self._prev_prefix_fingerprint),
                    endpoint=getattr(self.provider, "_base_url", None),
                )
            self._prefix_fingerprint["_requested_at"] = str(time.time())
            # The text behind the system hash, kept in-process so a change
            # can be diffed against the previous request's text (across a
            # park only the paragraph hashes survive, on the Usage event).
            self._pending_system_text = system_text_of(request.system)
        except Exception:  # noqa: BLE001 — instrumentation is never fatal
            log.debug("prefix fingerprint failed", exc_info=True)
            self._prefix_fingerprint = None

    def _cache_probe_fields(self, usage: dict[str, Any]) -> dict[str, Any]:
        """``prefix_hashes`` (+ ``cache_miss`` on a miss) for a Usage payload.

        Consumes the fingerprint of the call that just completed and rotates it
        into the "previous" slot, so the next call diffs against this one.
        """
        fingerprint = self._prefix_fingerprint
        rewrites = self._history_rewrites
        # Consumed either way: these rungs belong to the request just sent, and
        # must not be blamed for the miss after next.
        self._history_rewrites = []
        out: dict[str, Any] = {"compaction_decision": dict(self._compactor.last_decision)}
        try:
            # Provider-reported cache reads next to the verdict, every call,
            # so partial and full misses are told apart in the event log.
            out["cache_read_report"] = cache_read_report(
                usage,
                # Every provider normalizes input to exclude cache reads/writes.
                input_includes_cached=False,
            )
        except Exception:  # noqa: BLE001 — instrumentation is never fatal
            pass
        if fingerprint is None:
            return out
        self._prefix_fingerprint = None
        out["prefix_hashes"] = fingerprint
        system_detail = None
        try:
            system_detail = system_change_detail(
                self._prev_prefix_fingerprint, fingerprint,
                current_text=self._pending_system_text or "",
                previous_text=self._prev_system_text,
                sections=[
                    (s.kind, s.text) for s in self.system_sections.sections
                ] if self.system_sections else None,
            )
        except Exception:  # noqa: BLE001
            log.debug("system diff failed", exc_info=True)
        self._prev_system_text, self._pending_system_text = self._pending_system_text, None
        previous_time = (self._prev_prefix_fingerprint or {}).get("_requested_at")
        try:
            if previous_time and fingerprint.get("_requested_at"):
                out["request_interval_seconds"] = max(
                    0.0, float(fingerprint["_requested_at"]) - float(previous_time)
                )
        except (TypeError, ValueError):
            pass  # Old or malformed diagnostic metadata is not a turn failure.
        try:
            diagnosis = diagnose_cache_miss(
                previous=self._prev_prefix_fingerprint,
                current=fingerprint,
                input_tokens=int(usage.get("input_tokens", 0) or 0),
                cache_read_tokens=int(
                    usage.get("cache_read_input_tokens", 0) or 0
                ),
                config=self._cache_probe,
                history_rewrites=rewrites,
                explicit_breakpoints=self.provider.caps.explicit_cache_breakpoints,
                system_detail=system_detail,
            )
        except Exception:  # noqa: BLE001 — instrumentation is never fatal
            log.debug("cache-miss diagnosis failed", exc_info=True)
            diagnosis = None
        self._prev_prefix_fingerprint = fingerprint
        if diagnosis is None:
            self._unexplained_misses = 0
            return out
        out["cache_miss"] = diagnosis
        if not is_unexplained_miss(diagnosis):
            self._unexplained_misses = 0
            log.debug(
                "prompt cache miss: %s (%s); fresh=%s cache_read=%s",
                diagnosis.get("reason"),
                diagnosis.get("note", ""),
                diagnosis.get("input_tokens"),
                diagnosis.get("cache_read_tokens"),
            )
            return out
        # A shortfall against the fingerprinted reusable prefix. Keep every
        # call's evidence, but alert at most once per turn. This is not proof
        # of a provider fault or an account's cache capabilities.
        self._unexplained_misses += 1
        diagnosis["consecutive_unexplained"] = self._unexplained_misses
        key = self._affinity_key()
        log.debug(
            "prompt cache miss: %s (%s); fresh=%s cache_read=%s; "
            "provider=%s model=%s cache_key=%s provider_usage=%s",
            diagnosis.get("reason"),
            diagnosis.get("note", ""),
            diagnosis.get("input_tokens"),
            diagnosis.get("cache_read_tokens"),
            self.provider.name,
            self.model,
            key,
            {k: v for k, v in usage.items() if k != "provider_requests"},
        )
        return out

    async def _alert_unexplained_cache_misses(
        self, sid: str, turn_id: str, usage_raw: dict[str, Any]
    ) -> None:
        """Surface a run of misses fluid cannot explain, once per run.

        One unexplained miss is noise (a TTL expiry looks identical). Two in a
        row inside one turn is a pattern the host should be able to see and act
        on - audited sessions repeatedly paid full price with nothing
        in the transcript saying so. Emitted once when the streak reaches the
        threshold, not on every later call in the same run.
        """
        threshold = self._cache_probe.alert_after_misses
        if threshold <= 0 or self._cache_miss_alerted:
            return
        diagnosis = usage_raw.get("cache_miss")
        if not isinstance(diagnosis, dict):
            return
        streak = diagnosis.get("consecutive_unexplained")
        if not isinstance(streak, int) or streak < threshold:
            return
        self._cache_miss_alerted = True
        payload = {
            "session_id": sid,
            "turn_id": turn_id,
            "provider": self.provider.name,
            "model": self.model,
            "cache_key": self._affinity_key(),
            "consecutive": streak,
            "reason": diagnosis.get("reason"),
            "input_tokens": diagnosis.get("input_tokens"),
            "cache_read_tokens": diagnosis.get("cache_read_tokens"),
            "note": diagnosis.get("note"),
        }
        await self._emit(CacheMissAlert(
            session_id=sid, turn_id=turn_id,
            consecutive=int(streak),
            reason=str(diagnosis.get("reason") or ""),
            input_tokens=int(diagnosis.get("input_tokens") or 0),
            cache_read_tokens=int(diagnosis.get("cache_read_tokens") or 0),
            provider=self.provider.name,
            model=self.model,
            cache_key=payload["cache_key"],
            note=str(diagnosis.get("note") or ""),
        ))
        if self.hooks is not None:
            # Optional host hook, safe default: no subscriber, nothing happens.
            # A host that wants to trip a breaker (drop the affinity key, warn
            # the user, switch route) gets the whole diagnosis here.
            try:
                await self.hooks.emit("CacheMiss", payload)
            except Exception:  # noqa: BLE001 — a hook never ends a turn
                log.debug("CacheMiss hook failed", exc_info=True)

    async def _stream_once(
        self, sid: str, turn_id: str, history: list[Message], *, sticky: bool = True
    ) -> _CallResult:
        self._stream_usage: dict[str, Any] = {}
        try:
            return await self._stream_once_inner(sid, turn_id, history, sticky=sticky)
        except asyncio.CancelledError:
            # Only provider-observed counts: cancellation is not evidence of
            # unreported generation. Fold this logical call exactly once.
            usage = self._stream_usage
            self._fold_call_usage(self._turn_usage, usage)
            raw = dict(usage)
            raw.update(self._cache_probe_fields(usage))
            raw["model_call_index"] = self._turn_model_calls
            raw["cancelled"] = True
            await self._emit(Usage(
                session_id=sid, turn_id=turn_id,
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                cache_read_tokens=usage.get("cache_read_input_tokens", 0),
                cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
                raw=raw,
            ))
            raise
        finally:
            self._stream_usage = {}

    async def _stream_once_inner(
        self, sid: str, turn_id: str, history: list[Message], *, sticky: bool = True
    ) -> _CallResult:
        request = self._build_request(
            history, sticky=sticky
        )
        self._fingerprint_request(request)

        blocks: dict[int, dict[str, Any]] = {}
        usage_totals = self._stream_usage
        tool_uses: list[dict[str, Any]] = []
        stop_reason = "end_turn"
        native_stop_reason: str | None = None
        interrupted = False
        arg_abort: tuple[int, str] | None = None
        alive_published = False

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
            if chunk.kind in ("message_start", "provider_alive"):
                # Evidence the provider accepted this call and is streaming,
                # for a host that otherwise sees nothing until the first
                # content (encrypted-only reasoning). The first one per call
                # is always published; later ones only after a stretch with
                # no event at all, so a streaming call adds one event.
                if not alive_published or (
                    time.monotonic() - self._turn_progress_at
                    >= self.PROVIDER_ALIVE_INTERVAL_S
                ):
                    alive_published = True
                    await self._emit(ProviderAlive(
                        session_id=sid, turn_id=turn_id,
                        signal=str(payload.get("signal") or "accepted"),
                    ))
            if chunk.kind == "message_start":
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
                    # The provider's own block as it opened. Blocks the
                    # engine has no model for (redacted thinking, server
                    # tool use and results) are kept from this and resent
                    # verbatim, so the stored prefix stays what the
                    # provider produced.
                    "raw": payload.get("block") or None,
                }
                await self._emit(
                    BlockStart(
                        session_id=sid, turn_id=turn_id, index=index,
                        block_type=payload["block_type"],
                        subtype=payload.get("subtype"),
                        tool_name=payload.get("tool_name"),
                        tool_use_id=payload.get("tool_use_id"),
                    )
                )
            elif chunk.kind == "text_delta":
                await self._ensure_block(blocks, payload["index"], "text", sid, turn_id)
                blocks[payload["index"]]["text"] += payload["text"]
                await self._emit(
                    TextDelta(session_id=sid, turn_id=turn_id,
                              index=payload["index"], text=payload["text"])
                )
            elif chunk.kind == "thinking_delta":
                await self._ensure_block(
                    blocks, payload["index"], "thinking", sid, turn_id
                )
                blocks[payload["index"]]["text"] += payload["text"]
                await self._emit(
                    ThinkingDelta(session_id=sid, turn_id=turn_id,
                                  index=payload["index"], text=payload["text"])
                )
            elif chunk.kind == "tool_args_delta":
                index = payload["index"]
                fragment = payload["partial_json"]
                await self._ensure_block(blocks, index, "tool_use", sid, turn_id)
                block = blocks[index]
                block["args"] += fragment
                await self._emit(ToolArgsDelta(
                    session_id=sid, turn_id=turn_id, index=index,
                    tool_use_id=block["tool_use_id"] or "",
                    name=block["tool_name"] or "",
                    partial_json=fragment,
                ))
                cause = self._arg_stream_verdict(block, fragment)
                if cause is not None:
                    arg_abort = (index, cause)
                    break
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
                if payload.get("transport_failure"):
                    usage_totals["transport_failure"] = payload["transport_failure"]
                if payload.get("provider_requests"):
                    usage_totals["provider_requests"] = payload["provider_requests"]
                # No ErrorEvent here — _agent_loop decides whether this error
                # is terminal or recoverable (context overflow → forced trim
                # + one retry) before surfacing it.
                status = payload.get("status")
                transport_failure = payload.get("transport_failure")
                error_message = str(payload.get("message", payload))
                if transport_failure:
                    # Raw exception text stays out of user messaging and cannot
                    # accidentally activate a context-overflow retry. Transport
                    # clients already exhausted safe pre-stream retries.
                    error_message = (
                        "The provider connection was interrupted before the response finished. "
                        "The response may be incomplete. No automatic replay was attempted. "
                        "Review any completed actions before asking to continue."
                    )
                return _CallResult(
                    message=self._assemble_message(blocks, request.model),
                    tool_uses=tool_uses,
                    usage=usage_totals,
                    stop_reason="error",
                    error_message=error_message,
                    error_retriable=False if transport_failure else bool(payload.get("retriable")),
                    error_status=status if isinstance(status, int) else None,
                    native_stop_reason=native_stop_reason,
                )

        if self._interrupted.is_set():
            interrupted = True

        arg_stream_error: str | None = None
        arg_stream_tool: dict[str, Any] | None = None
        if arg_abort is not None and not interrupted:
            # The guard, not the provider, ended this stream: close the block
            # for live clients and describe the abandoned call. Whether it is
            # retried or handed to the model as a failed tool call is the agent
            # loop's decision — nothing is registered as a tool_use here, so a
            # retry leaves no ToolUse without a ToolResult behind it.
            index, cause = arg_abort
            block = blocks[index]
            block["input"] = {}
            stop_reason = "tool_use"
            await self._emit(BlockStop(session_id=sid, turn_id=turn_id, index=index))
            arg_stream_error = _explain_arg_runaway(
                block["tool_name"] or "", block["args"], cause,
                closed=bool(block.get("args_closed")),
            )
            arg_stream_tool = {
                "tool_use_id": block["tool_use_id"] or "",
                "name": block["tool_name"] or "",
                "input": {},
                "arg_error": arg_stream_error,
            }
            log.warning(
                "aborted a runaway tool-argument stream for %r: %s",
                block["tool_name"], cause,
            )

        if interrupted or arg_stream_error is not None:
            self._estimate_partial_usage(usage_totals, blocks, request)

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
            arg_stream_error=arg_stream_error,
            arg_stream_tool=arg_stream_tool,
        )

    def _arg_stream_verdict(
        self, block: dict[str, Any], fragment: str
    ) -> str | None:
        """Why this tool-argument stream has to stop, or None to keep going.

        Two shapes, both seen in production: arguments that simply never end,
        and arguments that reached a plausible payload and then streamed
        nothing but whitespace. The second is the common one and the harder to
        see, because every fragment still arrives on time.
        """
        guard = self.tool_arg_guard
        args: str = block["args"]
        if guard.max_bytes is not None and len(args) > guard.max_bytes:
            return (
                f"the arguments passed {guard.max_bytes} bytes and were still "
                "being written"
            )
        if guard.stall_bytes is None and guard.stall_seconds is None:
            return None
        if not fragment:
            return None
        if fragment.strip():
            # Real content: the whitespace run (if any) is over.
            block["ws_run"] = 0
            block["ws_since"] = None
            block["ws_probe_at"] = _WHITESPACE_PROBE_BYTES
            block["args_closed"] = False
            return None
        run = block.get("ws_run", 0) + len(fragment)
        block["ws_run"] = run
        since = block.get("ws_since")
        if since is None:
            since = block["ws_since"] = time.monotonic()
        if run < _WHITESPACE_MIN_RUN:
            return None
        if not block.get("args_closed"):
            probe_at = block.get("ws_probe_at") or _WHITESPACE_PROBE_BYTES
            if run >= probe_at:
                if _json_object_complete(args):
                    block["args_closed"] = True
                else:
                    block["ws_probe_at"] = run + _WHITESPACE_REPROBE_BYTES
        if guard.stall_bytes is not None and run >= guard.stall_bytes:
            return f"{run} consecutive bytes of whitespace arrived"
        if (
            guard.stall_seconds is not None
            and time.monotonic() - since >= guard.stall_seconds
        ):
            return (
                f"nothing but whitespace arrived for "
                f"{guard.stall_seconds:g}s ({run} bytes)"
            )
        return None

    def _estimate_partial_usage(
        self,
        usage_totals: dict[str, Any],
        blocks: dict[int, dict[str, Any]],
        request: ProviderRequest,
    ) -> None:
        """Fill in what an abandoned stream was billed but never reported.

        A call that ends mid-stream — the user pressed Stop, or the argument
        guard fired — normally never reaches the provider's terminal usage
        chunk, so its usage dict is empty and every downstream number reads
        zero: an interrupted turn used to record ``cost_usd: 0.0`` for a
        prompt and an output the provider certainly charged for. The tokens
        that were generated are sitting in the accumulated blocks, and the
        prompt is the request that was just sent, so both are estimated here
        and flagged as estimates.
        """
        if not usage_totals.get("output_tokens"):
            produced = "".join(
                str(block.get("text") or "") + str(block.get("args") or "")
                for block in blocks.values()
            )
            estimated = estimate_tokens_of_text(produced)
            if estimated > 0:
                usage_totals["output_tokens"] = estimated
                usage_totals["output_tokens_estimated"] = True
        if _measured_prompt_tokens(usage_totals) == 0:
            projected = self._estimate_request_input_tokens(request.messages)
            if projected > 0:
                usage_totals["input_tokens"] = projected
                usage_totals["input_tokens_estimated"] = True

    async def _ensure_block(
        self, blocks: dict[int, dict[str, Any]], index: int, block_type: str,
        sid: str, turn_id: str,
    ) -> None:
        """Open a block a delta refers to when no ``block_start`` announced it.

        A provider that skips the start frame (a proxy that drops it, a
        stream joined late) used to raise ``KeyError`` out of the consumer
        and lose the turn; the delta's own index and kind say enough to open
        the block.
        """
        if index in blocks:
            return
        blocks[index] = {
            "type": block_type, "text": "", "args": "", "signature": "",
            "tool_name": None, "tool_use_id": None, "raw": None,
        }
        await self._emit(BlockStart(
            session_id=sid, turn_id=turn_id, index=index, block_type=block_type,
        ))

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
                    # Direct aclose cancels this reader rather than winning
                    # the Interrupt race. Settle __anext__ before aclose.
                    next_chunk.cancel()
                    interrupted.cancel()
                    with suppress(asyncio.CancelledError, StopAsyncIteration):
                        await next_chunk
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
            except asyncio.CancelledError:
                dispatch.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await dispatch
                await self._emit(ToolResult(
                    session_id=sid, turn_id=turn_id, tool_use_id=tu["tool_use_id"],
                    name=tu["name"], output="Tool cancelled: engine closed.", is_error=True,
                ))
                raise
            finally:
                interrupt_wait.cancel()
                with suppress(asyncio.CancelledError):
                    await interrupt_wait
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
        self._tool_deliveries = []
        try:
            outcome = await self._gated_call(sid, turn_id, tu["name"], tool_use_id, tu["input"])
        finally:
            self.ctx.extra.pop("tool_use_id", None)
        if (
            outcome.background_task is not None
            and not outcome.background_task.serves
        ):
            # A detached worker: its result comes back somewhere other than
            # this tool's output, and getting that wrong is one of the most
            # expensive harness mistakes there is. Counts toward the
            # end-of-turn reflection nudge even on an otherwise clean turn.
            self._background_dispatches += 1
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
        # Reflection nudge: at most one per turn, appended to the first result
        # that follows the turn's first friction (or its first detached Task).
        nudge = self._reflection_nudge()
        if nudge:
            content.append(Text(nudge))
        return ToolResultBlock(
            tool_use_id=tool_use_id,
            content=content,
            is_error=outcome.is_error,
            delivered_steers=self._tool_deliveries,
            delivery_metadata_version=1,
        )

    # ── end-of-turn reflection nudge ─────────────────────────────────────
    def _friction_source(self) -> Any | None:
        """Whichever recorder is journaling this session's tool calls."""
        return self._codemode if self._codemode is not None else self._journal

    def _begin_friction_turn(self) -> None:
        """Reset the turn-scoped friction/nudge state. Never raises."""
        self._background_dispatches = 0
        self._reflection_nudged = False
        source = self._friction_source()
        if source is None:
            return
        try:
            source.begin_turn()
        except Exception:  # noqa: BLE001 — instrumentation is never fatal
            pass

    def _reflection_nudge(self) -> str | None:
        """One appended system-reminder asking the model to record a lesson.

        Small models never call Remember on their own after an expensive
        harness mistake — audited sessions show the model naming the problem
        in its own thinking, redoing a subagent's work by hand, and leaving
        nothing durable behind. The journal now records the mistake
        mechanically (``FrictionDetector``); this is the other half: a prompt
        to turn the mistake into a memory while the context is still hot.

        Three properties it must keep:

        - **Appended, never prefix-mutating.** It rides a tool result block on
          its way into immutable history, exactly like the nested-instruction
          reminder above; nothing already sent is rewritten, so the prompt
          cache survives.
        - **Absent on a clean turn.** No friction and no detached Task means
          no reminder at all — a turn that went well costs zero tokens here.
        - **Free to ignore.** It states that having nothing to record is a
          normal answer, so a model that skips it loses nothing.

        Returns ``None`` when it has already fired this turn, when nothing has
        gone wrong, or when Remember is not on this session's surface (with no
        continual learner wired there is nowhere for a lesson to go).
        """
        if self._reflection_nudged:
            return None
        source = self._friction_source()
        if source is None:
            return None
        if self.registry is not None and (
            self.registry.get("Remember") is None
            or not self._tool_available("Remember")
        ):
            return None
        try:
            events = int(source.friction_count)
        except Exception:  # noqa: BLE001
            return None
        dispatches = self._background_dispatches
        if events <= 0 and dispatches <= 0:
            return None
        self._reflection_nudged = True
        return _reflection_nudge_text(events, dispatches)

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
                input=input,
            )
        spec = self.registry.get(name)

        # PreToolUse hook: deny short-circuits with an in-context error result;
        # modify replaces the input before permissions/dispatch see it; a
        # permission decision of "allow" skips the permission gate and "ask"
        # forces a prompt; a stop denies the call and ends the turn after it.
        hook_permission: str | None = None
        hook_reason: str | None = None
        if self.hooks is not None:
            decision = await self.hooks.emit(
                PRE_TOOL_USE,
                self._hook_payload(
                    sid, name=name, input=input, tool_use_id=tool_use_id, turn_id=turn_id
                ),
            )
            await self._report_hook_warnings(sid, turn_id, PRE_TOOL_USE, decision, name)
            if decision.action == "deny" or decision.stop:
                reason = decision.note or ("stopped by hook" if decision.stop else "denied")
                if decision.stop:
                    self._hook_stop_reason = reason
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
            hook_permission = decision.permission_decision
            hook_reason = decision.note
            if hook_permission == "allow":
                await self._emit(HookEvent(
                    session_id=sid, turn_id=turn_id, level="info",
                    message=f"hook approved {name}; permission prompt skipped",
                    hook_event=PRE_TOOL_USE, tool_name=name,
                ))

        # Permission gate.
        if self.permissions is not None and spec is not None and hook_permission != "allow":
            decided = self.permissions.decide(spec, input, cwd=self.ctx.cwd)
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
                        input=input,
                    )
                verdict = "ask"
                ask_reason = _DOOM_LOOP_REASON
            if verdict == "allow" and hook_permission == "ask":
                verdict = "ask"
                ask_reason = hook_reason or "a PreToolUse hook asked for confirmation"
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
                    input=input,
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
        user_answered = False
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
            await self._notify_hooks(
                sid, turn_id, "elicitation_dialog", f"{name} is waiting for an answer",
                name=name, tool_use_id=tool_use_id,
            )
            interrupt_wait = asyncio.create_task(self._interrupted.wait())
            try:
                done, _ = await asyncio.wait(
                    {fut, interrupt_wait},
                    timeout=getattr(self, "question_timeout", QUESTION_TIMEOUT_S),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if fut not in done:
                    if interrupt_wait not in done:
                        log.warning(
                            "no answer to %s's question within the question "
                            "timeout; giving up on it", name,
                        )
                    return None
                resp: RespondQuestion = fut.result()
            finally:
                interrupt_wait.cancel()
                self._pending_questions.pop(request_id, None)
            answers[server_req_id] = resp.answer
            user_answered = user_answered or bool(getattr(resp, "user_answered", False))
        if user_answered:
            answers["__user_answered__"] = True
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
        await self._notify_hooks(
            sid, turn_id, "permission_prompt", f"{name} needs your permission to run",
            name=name, tool_use_id=tool_use_id,
        )
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
            scope = spec.scope_of(input) if spec is not None else None
            if spec is not None and spec.session_scope is not None and scope is None:
                # The tool scopes its allows but could not label this call
                # (a Bash line with command substitution): recording it
                # unscoped would cover every later call, so it stays a
                # one-shot allow.
                log.info(
                    "allow_always for %s applied to this call only: the "
                    "command could not be scoped", name,
                )
            else:
                self.permissions.allow_always(name, scope)
        return (
            "allow" if allowed else "deny",
            resp.message,
            resp.updated_input if allowed else None,
        )

    async def _report_hook_warnings(
        self, sid: str, turn_id: str | None, event: str, decision: Any,
        tool_name: str | None = None,
    ) -> None:
        """Surface non-fatal hook diagnostics (timeouts, odd exit codes) and
        a hook's ``systemMessage`` as HookEvents. Never changes the decision."""
        messages = [*getattr(decision, "warnings", ())]
        system_message = getattr(decision, "system_message", None)
        if system_message:
            messages.append(system_message)
        for message in messages:
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
                self._hook_payload(
                    sid, name=name, tool_use_id=tool_use_id, input=input,
                    output=outcome.text, is_error=outcome.is_error, turn_id=turn_id,
                ),
            )
            await self._report_hook_warnings(sid, turn_id, POST_TOOL_USE, decision, name)
            # A block appends its reason to the result (the call already ran,
            # so is_error stays as it was); additionalContext rides the same
            # way; a stop ends the turn once this dispatch round is recorded.
            additions: list[str] = []
            if decision.stop:
                reason = decision.note or "stopped by hook"
                self._hook_stop_reason = reason
                await self._emit(HookEvent(
                    session_id=sid, turn_id=turn_id, level="warning",
                    message=f"stopped by hook: {reason}",
                    hook_event=POST_TOOL_USE, tool_name=name,
                ))
            elif decision.action == "deny":
                reason = decision.note or "blocked by hook"
                await self._emit(HookEvent(
                    session_id=sid, turn_id=turn_id, level="info",
                    message=f"PostToolUse hook on {name}: {reason}",
                    hook_event=POST_TOOL_USE, tool_name=name,
                ))
                additions.append(_hook_reminder("PostToolUse hook feedback", reason))
            if decision.additional_context:
                additions.append(
                    _hook_reminder("PostToolUse hook context", decision.additional_context)
                )
            if additions:
                # New text the tool never produced: drop the content id so
                # code mode cannot collapse this result into an older ref.
                outcome = replace(
                    outcome, text="\n\n".join([outcome.text, *additions]).strip(),
                    content_id=None, window_of=None,
                )
        if self._journal is not None:
            # Continual journal, JSON-mode leg: one compact entry per call.
            self._journal.record_tool(
                name, input or {}, outcome.text, outcome.is_error
            )
        delivered: list[tuple[str | None, str, str | None, str]] = []
        if self._pending_steers:
            delivered = [_steer_parts(pending) for pending in self._pending_steers]
            self._pending_steers.clear()
            from fluid.messages.content import SteerDelivery
            self._tool_deliveries.extend(
                SteerDelivery(id=steer_id, text=text, sender=sender, origin=origin)
                for steer_id, text, sender, origin in delivered
            )
            steers = "".join(
                _render_interjection(text, sender, origin)
                for _, text, sender, origin in delivered
            )
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
            images=[{"media_type": image.media_type, "data": image.data} for image in outcome.images],
        ))
        if delivered:
            # Receipt, ordered after the result that carries the markers: the
            # steers are now part of immutable history, so a host can retire
            # its durable copy of every id listed here.
            await self._emit(SteerDelivered(
                session_id=sid, turn_id=turn_id,
                # ``from`` appears only on a peer's message, ``origin`` only on
                # a harness notice; the record for a user steer keeps the shape
                # hosts already parse, and the absence of both is what says
                # "this one was the user".
                steers=[
                    _steer_receipt(steer_id, text, sender, origin)
                    for steer_id, text, sender, origin in delivered
                ],
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

    async def record_auxiliary_usage(
        self, usage: dict[str, Any], *, model: str, turn_id: str | None, source: str,
    ) -> None:
        """Record provider-observed usage from a tool's call, including on cancel.

        Native media costs are not inferred from the orchestrator's text-token
        pricing. Retain observed counts and explicitly mark cost as incomplete.
        """
        if not usage:
            return
        observed = dict(usage)
        observed.update(model=model, auxiliary_source=source, cost_incomplete=True)
        self._accumulate_usage(self._turn_usage, usage)
        await self._emit(Usage(
            session_id=self.session_id, turn_id=turn_id,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_read_tokens=usage.get("cache_read_input_tokens", 0),
            cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
            raw=observed,
        ))

    def _fold_call_usage(self, totals: dict[str, Any], usage: dict[str, Any]) -> None:
        """Fold one model call's usage into the turn totals, filling in an
        estimated cost when the provider reported none.

        Three tiers, first answer wins: the provider's own ``estimate_cost``
        (Anthropic and the OpenAI dialect ship sourced tables; OpenRouter
        prices from injected rates), then the cross-provider table in
        ``fluid.providers.pricing`` — which also consults the host's pricing
        resolver, and which is what stops a Grok or GLM session from
        recording no spend at all. A model nobody can price leaves ``cost_usd``
        absent, so the field stays "unknown" rather than becoming a wrong
        number; every call here is guarded, and a missing price never raises.
        """
        if "cost_usd" not in usage:
            cost: float | None = None
            est = getattr(self.provider, "estimate_cost", None)
            if est is not None:
                try:
                    cost = est(self.model, usage)
                except Exception:  # noqa: BLE001 — accounting is never fatal
                    log.debug("provider cost estimate failed", exc_info=True)
                    cost = None
            if cost is None:
                cost = estimate_cost_from_table(
                    self.model, usage, resolver=self._pricing_resolver
                )
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
            else:
                opaque = self._opaque_block(block)
                if opaque is not None:
                    content.append(Reasoning(
                        origin=self.provider.name, raw=opaque, model=model,
                    ))
        return Message(role="assistant", content=content)

    @staticmethod
    def _opaque_block(block: dict[str, Any]) -> dict[str, Any] | None:
        """The provider's own item for a block the engine has no model for.

        Anthropic's redacted_thinking, server_tool_use and the server tool
        result blocks arrive this way. They are kept as an opaque envelope
        and resent verbatim by the serializer that produced them, since the
        API refuses a later turn whose prefix lost them. A server tool call's
        arguments stream after the block opens, so they are folded back in.
        """
        raw = block.get("raw")
        if not isinstance(raw, dict) or not raw.get("type"):
            return None
        item = dict(raw)
        if block.get("args"):
            parsed, error = _parse_tool_args(block["args"])
            if error is None:
                item["input"] = parsed
        return item

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
