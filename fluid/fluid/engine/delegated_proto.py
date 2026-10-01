"""claude CLI stream-json → fluid Event mapping.

The delegated backend spawns the official ``claude`` CLI in
``--input-format stream-json --output-format stream-json`` mode. Every line the
CLI writes is a JSON-serialized ``claude_agent_sdk`` message. This module turns
that vocabulary into the *same* ``Event`` model ``NativeLoop`` emits, so a client
cannot tell the two engines apart (modulo the documented capability matrix).

Frame vocabulary handled (see ``claude_agent_sdk`` message_parser + query.py):

  * ``{"type":"system","subtype":"init","session_id":...,"tools":[...],
    "mcp_servers":[...]}`` — captures ``cli_session_id`` (the ``--resume``
    handle) plus the announced tool surface (``cli_tools`` /
    ``cli_mcp_servers``), which is re-emitted as a ``DelegatedSurface`` event.
    TurnStarted is emitted by the loop at turn start (native parity: a
    persistent process only emits ``init`` once, so binding TurnStarted to it
    would miss turns 2+).
  * ``{"type":"system","subtype":"compact_boundary","compact_metadata":
    {"trigger":"auto"|"manual","pre_tokens":N,"post_tokens":N?}}`` — the CLI
    compacted its own context; mapped to ContextCompaction so a host sees the
    same boundary the native leg reports (see ``_on_compact_boundary``).
  * ``{"type":"assistant","message":{"content":[...],"usage":{...},...}}`` — each
    content block becomes BlockStart + a whole-block delta + BlockStop (+ ToolUse
    for ``tool_use``); ``usage`` becomes a per-model-call Usage event carrying
    what the native loop puts on its own (``per_call_usage``). The full
    message is assembled into a ``SessionNode`` for persistence. NOTE: the CLI
    splits a multi-block model message into ONE assistant frame PER BLOCK, each
    repeating the same message ``id`` and ``usage`` — hence the message-level
    block cursor (in-frame positions are always 0) and the usage-once guard.
    That repeated ``usage`` is the ``message_start`` snapshot, whose
    ``output_tokens`` is a placeholder of a few tokens, so the call's Usage is
    held until its final count is known: the partial stream's
    ``message_delta``; without the partial stream, the call's tool result or
    the next call; for the turn's last call, the result, whose per-turn
    output count settles what that call produced (``output_reconciled``).
  * ``{"type":"user","message":{"content":[{"type":"tool_result",...}]}}`` —
    ToolResult events (name resolved via the ``tool_use_id`` → name map).
  * ``{"type":"stream_event","event":{<raw Anthropic SSE>}}`` — real streaming
    deltas (only with ``--include-partial-messages``). Reuses the Anthropic
    ``sse_to_chunk`` serializer, then maps chunks exactly as ``NativeLoop`` does.
    When partial streaming is on, the block indices seen here suppress the
    whole-block re-emission from the ``assistant`` frames of the SAME message
    (dedupe); ``message_start`` is what clears that state, one message at a time.
  * ``{"type":"result","subtype":...,"total_cost_usd":...,"num_turns":...}`` —
    TurnComplete (+ a fallback Usage if no assistant frame carried usage).
    ``usage`` and ``num_turns`` are per turn, but ``total_cost_usd`` is the
    running total of the CLI process, so the turn's cost is its increase
    over ``cost_baseline`` (see ``_turn_cost``); the raw total rides along
    as ``usage["cli_total_cost_usd"]``, next to the turn's tool mix
    (``bash_file_ops`` / ``dedicated_file_ops``, see ``delegated_toolmix``).
    A result with ``is_error`` set is a failed turn: the mapper reports it
    as ``Mapped.error`` (with ``failed_status`` from its ``api_error_status``)
    and emits no TurnComplete, so the loop can decide
    between an ErrorEvent and a retry. The one exception is
    ``error_max_turns``: a turn cap is a stop reason, not a failure, and
    completes the turn the way the native loop's call cap does.
  * ``{"type":"rate_limit_event","rate_limit_info":{...}}`` — RateLimit. A
    frame with no utilization in any window (and not rejected) is logged
    once per key set, so a CLI shape change does not drop readings silently.
  * ``{"type":"control_request","request":{"subtype":"can_use_tool",...}}`` —
    a PermissionRequest event plus a ``PermissionBridge`` the loop uses to answer
    with the matching ``control_response`` frame.
  * ``{"type":"control_request","request":{"subtype":"hook_callback",
    "callback_id":...,"input":{...},"tool_use_id":...}}`` — one of the hooks
    the loop registered in its ``initialize`` request fired; surfaced as a
    ``HookCallbackRequest`` the loop answers through its HookBus. No event.
  * ``{"type":"control_cancel_request","request_id":...}`` — the CLI withdrew
    a request it sent. Not mapped here: the loop watches for it while it is
    parked on that request (``parse_control_cancel``).
  * ``{"type":"control_response","response":{...}}`` — the CLI's reply to a
    control_request the LOOP sent (``get_context_usage``, ``mcp_status``, …),
    surfaced as a ``ControlReply`` the loop correlates by request_id. Two
    subtypes, both keyed by ``request_id`` inside the inner ``response``::

        {"type":"control_response","response":{"subtype":"success",
         "request_id":"req_1_ab12cd34","response":{<payload>}}}
        {"type":"control_response","response":{"subtype":"error",
         "request_id":"req_1_ab12cd34","error":"..."}}

    (Shape verified against ``claude_agent_sdk._internal.query.Query`` —
    ``_read_messages`` routes on ``response.request_id`` and
    ``_send_control_request`` returns the inner ``response`` dict.)

Unknown frame types are ignored (forward-compat). The mapping helpers are pure;
the ``StreamJsonMapper`` is the small stateful driver (block indices, the
tool-name map, dedupe tracking, node-id generation) around them.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from fluid.events.model import (
    BlockStart,
    BlockStop,
    ContextCompaction,
    DelegatedSurface,
    Event,
    PermissionRequest,
    RateLimit,
    TextDelta,
    ThinkingDelta,
    ToolResult,
    ToolUse,
    TurnComplete,
    Usage,
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
from fluid.engine.delegated_toolmix import ToolMix
from fluid.providers.anthropic.serialize import sse_to_chunk
from fluid.store.base import new_id

logger = logging.getLogger(__name__)

# rate_limit_info key sets already reported as carrying no utilization, so a
# CLI that changes its frame shape warns once per process, not per frame.
_UNREADABLE_RATE_LIMIT_SHAPES: set[frozenset[str]] = set()

# Environment every delegated CLI child is started with, whatever the host
# environment, the caller's overlay or a settings file say. ``DelegatedLoop``
# applies it last in its child env and again in the ``--settings`` env block;
# the mapper copies it onto ``DelegatedSurface`` so a session records which
# overrides its CLI ran under. Treat it as read-only.
#
# * ``CLAUDE_CODE_DISABLE_BACKGROUND_TASKS``: the loop reads stdout only during
#   its own turns, so a background job's between-turn report would go unread.
# * ``CLAUDE_CODE_THRIFTY_SONIC``: turns off the CLI's "do your work through
#   Bash" steer (see ``DelegatedLoop._child_env``).
FORCED_CHILD_ENV: dict[str, str] = {
    "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
    "CLAUDE_CODE_THRIFTY_SONIC": "0",
}

# ─── Pure helpers ────────────────────────────────────────────────────────────


def stop_reason_from_subtype(subtype: str | None) -> str:
    """CLI ``result.subtype`` → fluid stop_reason (``success`` → ``end_turn``)."""
    if subtype in (None, "success"):
        return "end_turn"
    return subtype


def result_error_message(frame: dict[str, Any]) -> str:
    """What a failed ``result`` frame says went wrong, for the ErrorEvent."""
    subtype = str(frame.get("subtype") or "error")
    errors = frame.get("errors")
    if isinstance(errors, list) and errors:
        detail = "; ".join(str(e) for e in errors if e)
    else:
        result = frame.get("result")
        detail = result.strip() if isinstance(result, str) else ""
    return f"claude reported {subtype}: {detail}" if detail else f"claude reported {subtype}"


# The CLI reports a subscription limit as "<text>|<reset epoch seconds>".
_LIMIT_RESET_SUFFIX = re.compile(r"^(?P<text>.*?)\|(?P<epoch>\d{9,11})$", re.S)
_PROVIDER_MESSAGE_LIMIT = 300


def result_provider_message(frame: dict[str, Any]) -> str | None:
    """The short explanation a failed ``result`` frame gives, for a person.

    The result text when there is one, else the joined ``errors``. A
    usage-limit text's ``|<epoch>`` suffix becomes a readable reset time.
    None when the frame says nothing.
    """
    result = frame.get("result")
    detail = result.strip() if isinstance(result, str) else ""
    if not detail:
        errors = frame.get("errors")
        if isinstance(errors, list):
            detail = "; ".join(str(e) for e in errors if e).strip()
    if not detail:
        return None
    match = _LIMIT_RESET_SUFFIX.match(detail)
    if match is not None:
        try:
            resets = datetime.fromtimestamp(int(match["epoch"]), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            pass
        else:
            detail = f"{match['text'].strip()} (resets {resets:%Y-%m-%d %H:%M} UTC)"
    return detail[:_PROVIDER_MESSAGE_LIMIT]


def _api_error_status(frame: dict[str, Any]) -> int | None:
    """The HTTP status a failed ``result`` frame carries, when it is an int."""
    status = frame.get("api_error_status")
    if isinstance(status, bool) or not isinstance(status, int):
        return None
    return status


def _merge_counts(target: dict[str, Any], update: dict[str, Any]) -> None:
    """Fold a later usage snapshot of the SAME model call into ``target``.

    Token counters are cumulative within a call (``message_start`` carries
    the input counts, ``message_delta`` the final output count), so each
    integer takes the larger value, as the native loop merges them. Other
    fields are only filled in when missing.
    """
    for key, value in update.items():
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            current = target.get(key)
            if isinstance(current, bool) or not isinstance(current, int):
                current = 0
            target[key] = max(current, value)
        elif value is not None and key not in target:
            target[key] = value


def _dict_field(frame: dict[str, Any], key: str) -> dict[str, Any]:
    """``frame[key]`` when it is an object, else ``{}``: a frame whose body
    has the wrong shape maps to nothing instead of raising into the turn."""
    value = frame.get(key)
    return value if isinstance(value, dict) else {}


def flatten_tool_content(content: Any) -> str:
    """Reduce a tool_result ``content`` (str | list[block] | any) to model text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for c in content:
            if isinstance(c, dict):
                if c.get("type") == "text":
                    parts.append(c.get("text", ""))
                elif c.get("type") != "image":
                    parts.append(json.dumps(c, default=str))
            else:
                parts.append(str(c))
        return "".join(parts)
    return json.dumps(content, default=str)


def parse_control_response(frame: dict[str, Any]) -> ControlReply | None:
    """``control_response`` frame → :class:`ControlReply` (None if unattributable).

    Both subtypes key on ``request_id`` inside the inner ``response`` object;
    ``success`` nests the payload under a second ``response`` key, ``error``
    carries ``error`` instead. A frame with no usable request_id resolves
    nothing and is dropped.
    """
    response = frame.get("response", {}) or {}
    request_id = response.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        return None
    if response.get("subtype") == "error":
        return ControlReply(
            request_id=request_id, ok=False,
            error=str(response.get("error", "unknown control error")),
        )
    payload = response.get("response")
    return ControlReply(
        request_id=request_id, ok=True,
        payload=payload if isinstance(payload, dict) else None,
    )


def parse_hook_callback(frame: dict[str, Any]) -> HookCallbackRequest | None:
    """A ``hook_callback`` control_request as a record, or None for any
    other frame."""
    if not isinstance(frame, dict) or frame.get("type") != "control_request":
        return None
    req = frame.get("request")
    if not isinstance(req, dict) or req.get("subtype") != "hook_callback":
        return None
    data = req.get("input")
    return HookCallbackRequest(
        cli_request_id=str(frame.get("request_id") or ""),
        callback_id=str(req.get("callback_id") or ""),
        input=data if isinstance(data, dict) else {},
        tool_use_id=req.get("tool_use_id"),
    )


def parse_control_cancel(frame: Any) -> str | None:
    """The request_id a ``control_cancel_request`` withdraws, or None.

    The CLI sends ``{"type":"control_cancel_request","request_id":...}`` when
    it stops waiting on a request it sent (a ``can_use_tool`` another answer
    beat, such as a ``PermissionRequest`` hook in its own settings, or an
    aborted turn). No reply is expected.
    """
    if not isinstance(frame, dict) or frame.get("type") != "control_cancel_request":
        return None
    request_id = frame.get("request_id")
    return request_id if isinstance(request_id, str) and request_id else None


def per_call_usage(
    usage: dict[str, Any], *, model: str | None, index: int,
    cost_usd: float | None = None,
) -> dict[str, Any]:
    """``Usage.raw`` for one model call the CLI reported, in the native
    loop's shape.

    The CLI's own usage block (input, output, cache read and cache creation
    tokens) plus what the native loop adds to every per-call record: a
    ``model_call_index`` (hosts count and dedupe calls by it), the
    provider-reported ``cache_read_report`` and a ``cost_usd``. The CLI
    prices only the whole turn, so a call's cost is estimated from the
    pricing table, flagged ``cost_from_table`` (the dollars came from a
    price list, not the provider's bill; the token counts themselves are
    the CLI's own), unless the caller has the CLI's own figure. A model
    nobody can price leaves the cost out.
    """
    from fluid.context.cache_probe import cache_read_report
    from fluid.providers.pricing import estimate_cost

    raw = dict(usage)
    raw["model_call_index"] = index
    try:
        # The CLI's input_tokens exclude cache reads and writes, like every
        # usage block fluid normalizes.
        raw["cache_read_report"] = cache_read_report(usage, input_includes_cached=False)
    except Exception:  # noqa: BLE001 — instrumentation is never fatal
        pass
    if cost_usd is not None:
        raw["cost_usd"] = cost_usd
    elif "cost_usd" not in raw and model:
        cost = estimate_cost(model, usage)
        if cost is not None:
            raw["cost_usd"] = cost
            raw["cost_from_table"] = True
    return raw


def _int_or_zero(value: Any) -> int:
    """A reported token count, or 0 when the CLI omitted it (or sent junk)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def usage_fields(usage: dict[str, Any]) -> dict[str, Any]:
    """Extract the four canonical token counters (CLI key names == fluid's)."""
    return {
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
        "cache_creation_tokens": usage.get("cache_creation_input_tokens", 0),
    }


# ─── Bridge + result records ─────────────────────────────────────────────────


@dataclass(frozen=True)
class PermissionBridge:
    """A ``can_use_tool`` control_request awaiting a ``control_response`` reply."""

    cli_request_id: str
    fluid_request_id: str
    tool_name: str
    tool_use_id: str
    input: dict[str, Any]
    # The CLI's own ``permission_suggestions``: the rules it would add for an
    # "always allow" answer, echoed back as ``updatedPermissions``.
    suggestions: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class HookCallbackRequest:
    """A ``hook_callback`` control_request: the CLI ran a hook event the
    loop registered at ``initialize`` and waits for its output."""

    cli_request_id: str
    callback_id: str
    input: dict[str, Any]
    tool_use_id: str | None = None


@dataclass(frozen=True)
class ControlReply:
    """The CLI's answer to a control_request the loop sent.

    ``ok`` distinguishes the two subtypes: on success ``payload`` is the inner
    ``response`` dict (already in whatever shape that control call documents),
    on error ``error`` carries the CLI's message and ``payload`` is None.
    """

    request_id: str
    ok: bool
    payload: dict[str, Any] | None = None
    error: str | None = None


def _same_message(a: str | None, b: str | None) -> bool:
    """Whether two message ids name one message; an unknown id matches any."""
    return a is None or b is None or a == b


@dataclass
class _PendingCall:
    """A model call whose Usage waits for its final token counts.

    The first ``assistant`` frame of a message carries the ``message_start``
    usage, whose ``output_tokens`` is a placeholder of a few tokens. The
    record is held until the partial stream's ``message_delta`` brings the
    final count, or until nothing better can arrive (see
    ``StreamJsonMapper._flush_call``).
    """

    node_id: str
    msg_id: str | None
    model: str | None
    usage: dict[str, Any]
    index: int


@dataclass
class Mapped:
    """Everything one frame produces: events to emit, an optional node to
    persist, (for permissions) the bridge the loop must answer, and (for
    control replies) the answer the loop is awaiting."""

    events: list[Event] = field(default_factory=list)
    message: Message | None = None
    node_id: str | None = None
    permission: PermissionBridge | None = None
    control_reply: ControlReply | None = None
    hook_request: HookCallbackRequest | None = None
    # A control_request the loop has no handler for: (request_id, subtype).
    # The CLI blocks on every control_request, so it still gets an answer.
    unsupported_request: tuple[str, str] | None = None
    is_result: bool = False
    # Set with ``is_result`` when the CLI reported the turn as failed.
    error: str | None = None
    # (node_id, model_call_index) of a model call this frame started: the
    # loop asks the CLI for that call's context snapshot now, while its Usage
    # is still held for the final token counts.
    opened_call: tuple[str, int] | None = None


# ─── Stateful mapper ─────────────────────────────────────────────────────────


class StreamJsonMapper:
    def __init__(
        self,
        *,
        session_id: str,
        turn_id: str,
        model: str,
        provider: str = "claude",
        user_node_id: str,
        cost_baseline: float | None = 0.0,
    ) -> None:
        self.session_id = session_id
        self.turn_id = turn_id
        self.model = model
        self.provider = provider
        self.user_node_id = user_node_id
        self.cli_session_id: str | None = None
        # The tool surface the CLI announced in its init frame. None until an
        # init frame carries a `tools` list (see _on_system).
        self.cli_tools: tuple[str, ...] | None = None
        self.cli_mcp_servers: tuple[dict[str, Any], ...] | None = None
        # What the latest init frame said about the CLI itself: its version,
        # the permission mode and model it runs, and where its credentials
        # come from (``apiKeySource``). Empty until an init frame arrives.
        self.cli_init: dict[str, str | None] = {}

        self._tool_names: dict[str, str] = {}  # tool_use_id → tool name
        # tool_use_id → input, kept so a tool_result can be journaled with
        # the arguments that produced it.
        self.tool_inputs: dict[str, dict[str, Any]] = {}
        self._usage_emitted = False
        # Model calls reported this turn; the last one's number is the
        # ``model_call_index`` of its Usage.
        self.model_calls = 0
        # What a failed result reported for the whole turn (the CLI's usage
        # block and total cost), for the loop's ErrorEvent. Empty/None until
        # a failed result arrives.
        self.failed_usage: dict[str, Any] = {}
        self.failed_cost_usd: float | None = None
        # The HTTP status a failed result carried (its ``api_error_status``,
        # or 401 when an assistant frame reported ``authentication_failed``)
        # and its short explanation; None when it gave neither.
        self.failed_status: int | None = None
        self.failed_provider_message: str | None = None
        # The CLI's ``total_cost_usd`` is the running total of its process,
        # not the turn's cost. ``cost_baseline`` is that total after the last
        # turn the loop booked on this process (0.0 on a fresh process, None
        # when the process was spawned with --resume and the total it starts
        # from is unknown). The turn's cost is the difference; see
        # ``_turn_cost``. ``cli_total_cost_usd`` is the raw total the result
        # reported, for the loop's next baseline.
        self.cost_baseline = cost_baseline
        self.cli_total_cost_usd: float | None = None
        # The model the CLI said answered the turn (the first main-thread
        # assistant ``message.model``), used to price a turn from the table.
        # A subagent's frames (``parent_tool_use_id`` set) may name a cheaper
        # model and never replace it.
        self.reported_model: str | None = None
        # An assistant frame of this turn reported ``authentication_failed``.
        self._auth_failed = False
        # The model call whose Usage is held for its final token counts.
        self._pending_call: _PendingCall | None = None
        # The partial stream's final usage (``message_delta``) for the
        # current message, when it arrived before that message's first
        # assistant frame.
        self._stream_final_usage: dict[str, Any] | None = None
        # The id of the message now streaming (its message_start), and of the
        # message whose final counts are kept above: a message_delta carries
        # no id of its own, so it belongs to the latest message_start. Its
        # counts complete only a call of that message.
        self._stream_msg_id: str | None = None
        self._stream_final_msg_id: str | None = None
        # A stream_event arrived this turn: the CLI runs with
        # --include-partial-messages, so each call's message_delta will come.
        self._partial_seen = False
        # Output tokens of the per-call Usage events emitted this turn.
        self._emitted_output = 0
        # Shell file work against dedicated file tools this turn.
        self.tool_mix = ToolMix()
        self._last_node_id: str = user_node_id
        # Dedupe: block indices already streamed via stream_event for the CURRENT
        # model message (their whole-block re-emission is suppressed). Cleared on
        # the message_start chunk — NOT per assistant frame: the CLI splits one
        # model message into one `assistant` frame PER CONTENT BLOCK, so a
        # per-frame reset would drop the dedupe state before the sibling frames
        # arrive and every block after the first would be re-emitted whole.
        self._streamed: set[int] = set()
        # In-flight stream_event tool_use blocks: index → accumulated args/name.
        self._se_tool: dict[int, dict[str, Any]] = {}
        # Message-level index of the next block an `assistant` frame reports.
        # Those per-block frames each carry a one-element content list, so the
        # in-frame position is always 0 and cannot be matched against the
        # message-level indices in ``_streamed``; this running cursor supplies
        # the real index. Reset on message_start with ``_streamed``.
        self._block_cursor = 0
        # Message id whose `usage` already opened a model call (held or
        # emitted, see _open_call). Every
        # per-block frame of a message repeats that message's usage verbatim, so
        # without this a two-block message double-counts its tokens and cost.
        self._usage_msg_id: str | None = None

    @property
    def produced_nodes(self) -> bool:
        """True once an assistant or tool-result frame was mapped this turn."""
        return self._last_node_id != self.user_node_id

    @property
    def last_node_id(self) -> str:
        """The node the turn's terminal event points at: the last one mapped,
        else the user's."""
        return self._last_node_id

    # ─── frame dispatch ──────────────────────────────────────────────────

    def feed(self, frame: dict[str, Any]) -> Mapped:
        ftype = frame.get("type")
        if ftype == "system":
            return self._on_system(frame)
        if ftype == "assistant":
            return self._on_assistant(frame)
        if ftype == "user":
            return self._on_user(frame)
        if ftype == "stream_event":
            return self._on_stream_event(frame)
        if ftype == "result":
            return self._on_result(frame)
        if ftype == "rate_limit_event":
            return self._on_rate_limit(frame)
        if ftype == "control_request":
            return self._on_control_request(frame)
        if ftype == "control_response":
            return self._on_control_response(frame)
        # Unknown frame types: ignored (forward-compat).
        return Mapped()

    # ─── event builders (carry the fixed session/turn ids) ───────────────

    def _block_start(self, index: int, block_type: str, **kw: Any) -> BlockStart:
        return BlockStart(
            session_id=self.session_id, turn_id=self.turn_id,
            index=index, block_type=block_type, **kw,
        )

    def _block_stop(self, index: int) -> BlockStop:
        return BlockStop(session_id=self.session_id, turn_id=self.turn_id, index=index)

    def _text_delta(self, index: int, text: str) -> TextDelta:
        return TextDelta(session_id=self.session_id, turn_id=self.turn_id, index=index, text=text)

    def _thinking_delta(self, index: int, text: str) -> ThinkingDelta:
        return ThinkingDelta(session_id=self.session_id, turn_id=self.turn_id, index=index, text=text)

    def _tool_use(self, tool_use_id: str, name: str, input: dict[str, Any]) -> ToolUse:
        self.tool_mix.record(tool_use_id, name, input)
        return ToolUse(
            session_id=self.session_id, turn_id=self.turn_id,
            tool_use_id=tool_use_id, name=name, input=input,
        )

    def _usage_event(
        self, node_id: str, usage: dict[str, Any], *,
        model: str | None = None, cost_usd: float | None = None,
        index: int | None = None,
    ) -> Usage:
        if index is None:
            self.model_calls += 1
            index = self.model_calls
        return Usage(
            session_id=self.session_id, node_id=node_id, turn_id=self.turn_id,
            raw=per_call_usage(
                usage, model=model or self.model, index=index,
                cost_usd=cost_usd,
            ),
            **usage_fields(usage),
        )

    # ─── per-call usage (held until its final counts) ────────────────────

    def _open_call(
        self, node_id: str, msg_id: str | None, usage: dict[str, Any], model: Any,
    ) -> list[Event]:
        """Start holding the Usage of a new model call; what that releases.

        A call still held from an earlier message is complete by now and is
        emitted first. The call number is taken here, so indices follow the
        order the calls started in.
        """
        events = self._flush_call()
        self.model_calls += 1
        self._pending_call = _PendingCall(
            node_id=node_id, msg_id=msg_id,
            model=model if isinstance(model, str) and model else None,
            usage=dict(usage), index=self.model_calls,
        )
        self._usage_emitted = True
        self._usage_msg_id = msg_id
        if self._stream_final_usage is not None and _same_message(
            self._stream_final_msg_id, msg_id,
        ):
            # The message_delta came before this message's first frame.
            _merge_counts(self._pending_call.usage, self._stream_final_usage)
            self._stream_final_usage = None
            self._stream_final_msg_id = None
            events.extend(self._flush_call())
        return events

    def _flush_call(self, *, turn_output: int | None = None) -> list[Event]:
        """Emit the held call's Usage (priced from its counts), if any.

        ``turn_output`` is the result's per-turn ``output_tokens``, passed
        when the call never got a ``message_delta`` (the CLI runs without
        --include-partial-messages, or the message was cut short). The call
        then takes whatever output the turn reported beyond the calls
        already emitted, never less than its own snapshot, and is flagged
        ``output_reconciled``.
        """
        call = self._pending_call
        if call is None:
            return []
        self._pending_call = None
        usage = call.usage
        if turn_output is not None:
            snapshot = _int_or_zero(usage.get("output_tokens"))
            usage["output_tokens"] = max(snapshot, turn_output - self._emitted_output)
            usage["output_reconciled"] = True
        self._emitted_output += _int_or_zero(usage.get("output_tokens"))
        return [self._usage_event(call.node_id, usage, model=call.model, index=call.index)]

    def call_is_held(self, index: int | None) -> bool:
        """True while call ``index``'s Usage has not been emitted yet."""
        return self._pending_call is not None and self._pending_call.index == index

    def flush_usage(self) -> list[Event]:
        """Emit a call still held when the stream ended without a result.

        Its token counts are the last ones the CLI reported (the output count
        may be the ``message_start`` placeholder); recording them beats
        losing the call's input and cache tokens.
        """
        return self._flush_call()

    # ─── turn cost ───────────────────────────────────────────────────────

    def _priced_model(self, frame: dict[str, Any]) -> str:
        """The model a table price should use: the one the CLI reported."""
        if self.reported_model:
            return self.reported_model
        model_usage = frame.get("modelUsage")
        if isinstance(model_usage, dict):
            for name in model_usage:
                if isinstance(name, str) and name:
                    return name
        return self.model

    def _turn_cost(
        self, frame: dict[str, Any], usage: dict[str, Any],
    ) -> tuple[float | None, bool]:
        """This turn's cost from the result frame, and whether it is a table
        estimate.

        ``total_cost_usd`` is cumulative for the CLI process (its schema:
        read the latest result rather than summing), while the result's
        ``usage`` and ``num_turns`` are per turn. The turn's cost is the total
        minus ``cost_baseline``; a total below the baseline means the CLI's
        counter restarted, and the whole total is this turn's. With no
        baseline (the first result of a process spawned with --resume, which
        may or may not restore the saved total) the turn's own usage is
        priced from the table instead.
        """
        total = frame.get("total_cost_usd")
        if isinstance(total, bool) or not isinstance(total, (int, float)):
            return None, False
        total = float(total)
        self.cli_total_cost_usd = total
        baseline = self.cost_baseline
        if baseline is None:
            from fluid.providers.pricing import estimate_cost

            estimate = estimate_cost(self._priced_model(frame), usage) if usage else None
            return estimate, estimate is not None
        if total >= baseline:
            return round(total - baseline, 10), False
        return total, False

    def _log_tool_mix(self, frame: dict[str, Any]) -> None:
        mix = self.tool_mix
        model = self._priced_model(frame)
        logger.info(
            "delegated turn tool mix: bash_file_ops=%d dedicated_file_ops=%d model=%s",
            mix.bash_file_ops, mix.dedicated_file_ops, model,
        )
        if mix.bash_heavy:
            logger.warning(
                "delegated turn did most of its file work through the shell: "
                "bash_file_ops=%d dedicated_file_ops=%d model=%s. Claude Code's "
                "bash-first steer may be on (the child env should carry "
                "CLAUDE_CODE_THRIFTY_SONIC=0)",
                mix.bash_file_ops, mix.dedicated_file_ops, model,
            )

    # ─── per-frame handlers ──────────────────────────────────────────────

    def _on_system(self, frame: dict[str, Any]) -> Mapped:
        subtype = frame.get("subtype")
        if subtype == "compact_boundary":
            return self._on_compact_boundary(frame)
        if subtype != "init":
            return Mapped()
        self.cli_session_id = frame.get("session_id") or self.cli_session_id

        def text(key: str) -> str | None:
            value = frame.get(key)
            return str(value) if value not in (None, "") else None

        # What the CLI says about itself. This is the only record of which
        # build actually ran, in which mode and on which credentials: a stale
        # worker, a CLI on an API key instead of the subscription, or a mode
        # the CLI downgraded all show here and nowhere else.
        self.cli_init = {
            "cli_version": text("claude_code_version"),
            "permission_mode": text("permissionMode"),
            "model": text("model"),
            "api_key_source": text("apiKeySource"),
        }
        tools = frame.get("tools")
        if not isinstance(tools, list):
            # No tool list announced: leave the surface unknown (None) rather
            # than claiming the child can call nothing.
            return Mapped()
        self.cli_tools = tuple(str(t) for t in tools)
        servers = frame.get("mcp_servers")
        if not isinstance(servers, list):
            servers = []
        self.cli_mcp_servers = tuple(s for s in servers if isinstance(s, dict))
        return Mapped(events=[DelegatedSurface(
            session_id=self.session_id, turn_id=self.turn_id,
            tools=self.cli_tools, mcp_servers=self.cli_mcp_servers,
            cli_session_id=self.cli_session_id,
            child_env_overrides=dict(FORCED_CHILD_ENV),
            **self.cli_init,
        )])

    def _on_compact_boundary(self, frame: dict[str, Any]) -> Mapped:
        """The CLI compacted its own context — report it the way the native leg does.

        Without it the delegated leg would emit nothing when the prompt
        collapses mid-turn, and a host asking for the latest compaction
        boundary would conclude a Claude-lane session had never compacted.

        The CLI owns the compaction, so the event carries what it reports and
        nothing invented: its trigger as the reason, its own before/after token
        counts, and no digest text — ``trimmed`` and ``spans`` are the native
        ladder's decisions and stay empty. Tier 3 is the honest rung: a CLI
        compaction folds the whole prefix into a summary.
        """
        meta = frame.get("compact_metadata")
        if not isinstance(meta, dict):
            # A boundary with no metadata still says the context changed.
            meta = {}
        trigger = meta.get("trigger")
        reason = (
            f"cli_{trigger}_compact"
            if trigger in ("auto", "manual")
            else "cli_compact"
        )
        return Mapped(events=[ContextCompaction(
            session_id=self.session_id, turn_id=self.turn_id,
            tier=3, reason=reason,
            before_tokens=_int_or_zero(meta.get("pre_tokens")),
            after_tokens=_int_or_zero(meta.get("post_tokens")),
        )])

    def _on_assistant(self, frame: dict[str, Any]) -> Mapped:
        msg = _dict_field(frame, "message")
        if not msg:
            return Mapped()  # no message body: nothing to show or store
        blocks = msg.get("content", []) or []
        events: list[Event] = []
        content: list[Any] = []

        for index, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            # Suppression is decided on the MESSAGE-level index (the one the
            # stream events carry); emission keeps the in-frame index, which is
            # what the non-partial path has always used.
            streamed = (self._block_cursor + index) in self._streamed

            if btype == "text":
                text = block.get("text", "")
                content.append(Text(text))
                if not streamed:
                    events.append(self._block_start(index, "text"))
                    events.append(self._text_delta(index, text))
                    events.append(self._block_stop(index))
            elif btype == "thinking":
                thinking = block.get("thinking", "")
                signature = block.get("signature")
                content.append(Thinking(thinking))
                content.append(Reasoning(
                    origin="anthropic", summary_text=thinking, signature=signature,
                    raw={"type": "thinking", "thinking": thinking, "signature": signature},
                ))
                if not streamed:
                    events.append(self._block_start(index, "thinking"))
                    events.append(self._thinking_delta(index, thinking))
                    events.append(self._block_stop(index))
            elif btype in ("tool_use", "server_tool_use"):
                tid = block.get("id", "")
                name = block.get("name", "")
                inp = block.get("input", {}) or {}
                self._tool_names[tid] = name
                self.tool_inputs[tid] = inp
                content.append(ToolUseBlock(id=tid, name=name, input=inp))
                if not streamed:
                    # Order mirrors NativeLoop: BlockStart, BlockStop, then ToolUse.
                    events.append(self._block_start(index, "tool_use", tool_name=name, tool_use_id=tid))
                    events.append(self._block_stop(index))
                    events.append(self._tool_use(tid, name, inp))
            # tool_result / advisor blocks inside an assistant frame are rare and
            # not part of the native event shape; left out deliberately.

        node_id = new_id("node")
        self._last_node_id = node_id

        if frame.get("error") == "authentication_failed":
            self._auth_failed = True
        model = msg.get("model")
        if (
            isinstance(model, str) and model and self.reported_model is None
            and not frame.get("parent_tool_use_id")
        ):
            self.reported_model = model
        usage = msg.get("usage")
        msg_id = msg.get("id")
        pending = self._pending_call
        opened: tuple[str, int] | None = None
        if usage and isinstance(usage, dict):
            if msg_id is not None and pending is not None and pending.msg_id == msg_id:
                # A later per-block frame of the held call.
                _merge_counts(pending.usage, usage)
            elif msg_id is None or msg_id != self._usage_msg_id:
                # A new model call. Its Usage is held: this frame carries the
                # message_start usage, whose output count is a placeholder.
                events.extend(self._open_call(node_id, msg_id, usage, model))
                opened = (node_id, self.model_calls)

        # Advance past the blocks this frame reported: the next `assistant`
        # frame of the same message continues where this one stopped.
        self._block_cursor += len(blocks)

        return Mapped(
            events=events, message=Message(role="assistant", content=content),
            node_id=node_id, opened_call=opened,
        )

    def _on_user(self, frame: dict[str, Any]) -> Mapped:
        msg = _dict_field(frame, "message")
        raw_blocks = msg.get("content", [])
        events: list[Event] = []
        content: list[Any] = []

        blocks = raw_blocks if isinstance(raw_blocks, list) else []
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tid = block.get("tool_use_id", "")
            name = self._tool_names.get(tid, "unknown")
            raw_content = block.get("content")
            text = flatten_tool_content(raw_content)
            result_blocks: list[Any] = [Text(text)] if text else []
            for item in raw_content if isinstance(raw_content, list) else []:
                if not isinstance(item, dict) or item.get('type') != 'image':
                    continue
                source = item.get('source') or item
                if isinstance(source, dict) and source.get('data'):
                    result_blocks.append(Image(
                        media_type=source.get('media_type') or source.get('mimeType') or 'image/png',
                        data=source['data'],
                    ))
            is_error = bool(block.get("is_error"))
            content.append(ToolResultBlock(
                tool_use_id=tid, content=result_blocks, is_error=is_error,
            ))
            events.append(ToolResult(
                session_id=self.session_id, turn_id=self.turn_id,
                tool_use_id=tid, name=name, output=text, is_error=is_error, ui=raw_content,
            ))

        if not content:
            return Mapped()

        if not self._partial_seen:
            # Without the partial stream no message_delta will come, so the
            # held call's counts will not improve. Its tool result means the
            # call is over: emit it now rather than when the next call's
            # first frame arrives, so a turn killed while that call is still
            # generating keeps this one on record.
            events = self._flush_call() + events

        node_id = new_id("node")
        self._last_node_id = node_id
        return Mapped(events=events, message=Message(role="user", content=content), node_id=node_id)

    def _on_stream_event(self, frame: dict[str, Any]) -> Mapped:
        event = frame.get("event", {}) or {}
        chunk = sse_to_chunk(event.get("type", ""), event)
        if chunk is None:
            return Mapped()
        kind = chunk.kind
        p = chunk.payload
        events: list[Event] = []

        if kind == "message_start":
            # A new model message begins: the previous message's streamed-block
            # bookkeeping no longer applies, and its `assistant` frames have all
            # been consumed by now (they are emitted per block, before or right
            # after the block's own stream events, never after the NEXT
            # message_start). Deliberately NOT message_stop: the CLI may flush a
            # message's per-block assistant frames after it.
            self._streamed.clear()
            self._se_tool.clear()
            self._block_cursor = 0
            self._partial_seen = True
            # A call still held belongs to an earlier message, which is over.
            events.extend(self._flush_call())
            self._stream_final_usage = None
            self._stream_final_msg_id = None
            message = p.get("message")
            msg_id = message.get("id") if isinstance(message, dict) else None
            self._stream_msg_id = msg_id if isinstance(msg_id, str) and msg_id else None
        elif kind == "usage":
            # message_delta: the call's final usage (output_tokens is the
            # real count here; message_start and the assistant frames carry
            # a placeholder). Complete the held call and emit it, or keep the
            # counts for the message's first assistant frame.
            final = p.get("usage")
            if isinstance(final, dict):
                pending = self._pending_call
                if pending is not None and _same_message(self._stream_msg_id, pending.msg_id):
                    _merge_counts(pending.usage, final)
                    events.extend(self._flush_call())
                else:
                    # Its message has no assistant frame yet (or the held
                    # call is another message's, when streams interleave).
                    self._stream_final_usage = dict(final)
                    self._stream_final_msg_id = self._stream_msg_id
        elif kind == "block_start":
            index = p["index"]
            self._streamed.add(index)
            btype = p.get("block_type", "text")
            if btype == "tool_use":
                tid = p.get("tool_use_id")
                name = p.get("tool_name")
                self._se_tool[index] = {"args": "", "tool_use_id": tid, "name": name}
                if tid:
                    self._tool_names[tid] = name or ""
                events.append(self._block_start(index, "tool_use", tool_name=name, tool_use_id=tid))
            else:
                events.append(self._block_start(index, btype))
        elif kind == "text_delta":
            self._streamed.add(p["index"])
            events.append(self._text_delta(p["index"], p["text"]))
        elif kind == "thinking_delta":
            self._streamed.add(p["index"])
            events.append(self._thinking_delta(p["index"], p["text"]))
        elif kind == "tool_args_delta":
            block = self._se_tool.get(p["index"])
            if block is not None:
                block["args"] += p["partial_json"]
        elif kind == "signature_delta":
            pass  # captured verbatim from the assistant frame for the node
        elif kind == "block_stop":
            index = p["index"]
            events.append(self._block_stop(index))
            block = self._se_tool.get(index)
            if block is not None:
                # Same truncation risk as the native loop: a cut-off
                # arguments blob must not raise out of the stream consumer.
                try:
                    args = json.loads(block["args"]) if block["args"] else {}
                except (json.JSONDecodeError, ValueError):
                    args = {}
                if block["tool_use_id"]:
                    self.tool_inputs[block["tool_use_id"]] = args
                events.append(self._tool_use(block["tool_use_id"] or "", block["name"] or "", args))
        # message_stop from the partial stream is ignored: the call's Usage
        # went out with its message_delta.
        return Mapped(events=events)

    def _on_result(self, frame: dict[str, Any]) -> Mapped:
        subtype = frame.get("subtype", "success")
        usage = _dict_field(frame, "usage")
        events: list[Event] = []
        cost, cost_from_table = self._turn_cost(frame, usage)
        if self._pending_call is not None:
            # The last call never got a message_delta: the result's per-turn
            # output count settles what it produced.
            turn_output = usage.get("output_tokens")
            if isinstance(turn_output, bool) or not isinstance(turn_output, int):
                turn_output = None
            events.extend(self._flush_call(turn_output=turn_output))
        elif not self._usage_emitted and usage:
            # No assistant frame carried usage: the turn's totals stand in for
            # its one record, priced with the turn's own cost (or from the
            # table when that cost is itself a table estimate).
            events.append(self._usage_event(
                self._last_node_id, usage, model=self._priced_model(frame),
                cost_usd=None if cost_from_table else cost,
            ))
        self._log_tool_mix(frame)
        turn_usage = dict(usage)
        if self.cli_total_cost_usd is not None:
            # The CLI's own running total, kept for audits.
            turn_usage["cli_total_cost_usd"] = self.cli_total_cost_usd
        if cost_from_table:
            turn_usage["cost_from_table"] = True
        if frame.get("is_error") and subtype != "error_max_turns":
            # A failed turn: no TurnComplete. The loop turns this into an
            # ErrorEvent, or retries when a stale --resume handle caused it.
            # The CLI still billed the turn, so its totals ride along, with
            # the HTTP status the provider refused it with, when it said.
            self.failed_usage = turn_usage
            self.failed_cost_usd = cost
            status = _api_error_status(frame)
            if status is None and self._auth_failed:
                status = 401
            self.failed_status = status
            self.failed_provider_message = result_provider_message(frame)
            return Mapped(events=events, is_result=True, error=result_error_message(frame))
        turn_usage["bash_file_ops"] = self.tool_mix.bash_file_ops
        turn_usage["dedicated_file_ops"] = self.tool_mix.dedicated_file_ops
        num_turns = frame.get("num_turns") or 1
        events.append(TurnComplete(
            session_id=self.session_id, node_id=self._last_node_id, turn_id=self.turn_id,
            stop_reason=stop_reason_from_subtype(subtype),
            num_model_calls=int(num_turns),
            usage=turn_usage,
            cost_usd=cost,
        ))
        return Mapped(events=events, is_result=True)

    def _on_rate_limit(self, frame: dict[str, Any]) -> Mapped:
        info = frame.get("rate_limit_info", {}) or {}
        if not isinstance(info, dict):
            return Mapped()
        kind = info.get("rateLimitType")
        status = info.get("status")
        status = status if isinstance(status, str) and status else None
        events = []
        # Claude Code 2.1.285 reports all observed windows here, without a
        # top-level utilization. The verdict applies only to rateLimitType:
        # a rejected five-hour window must not exhaust the weekly allowance.
        windows = info.get("unifiedWindows")
        for window_kind, window in (windows.items() if isinstance(windows, dict) else ()):
            if not isinstance(window_kind, str) or not window_kind or not isinstance(window, dict):
                continue
            events.append(RateLimit(
                session_id=self.session_id, turn_id=self.turn_id,
                kind=window_kind,
                resets_at=window.get("resetsAt"),
                utilization=window.get("utilization"),
                status=status if window_kind == kind else None,
            ))
        # Older CLIs report just one window. Also preserve a primary verdict
        # omitted from unifiedWindows (rejected may have no utilization).
        if isinstance(kind, str) and kind and not any(e.kind == kind for e in events):
            events.append(RateLimit(
                session_id=self.session_id, turn_id=self.turn_id,
                kind=kind,
                resets_at=info.get("resetsAt"),
                utilization=info.get("utilization"),
                status=status,
            ))
        if status != "rejected" and not any(
            isinstance(e.utilization, (int, float)) and not isinstance(e.utilization, bool)
            for e in events
        ):
            # Nothing here moves a usage bar (a host drops a reading with no
            # utilization unless the window is rejected). When the CLI changes
            # this frame's shape again, say so once instead of dropping every
            # reading silently.
            shape = frozenset(str(key) for key in info)
            if shape not in _UNREADABLE_RATE_LIMIT_SHAPES:
                _UNREADABLE_RATE_LIMIT_SHAPES.add(shape)
                logger.warning(
                    "claude rate_limit_event carried no utilization in any window "
                    "(status=%s); rate_limit_info keys: %s. Usage readings from "
                    "this CLI are being dropped",
                    status, ", ".join(sorted(shape)) or "(none)",
                )
        return Mapped(events=events)

    def _on_control_request(self, frame: dict[str, Any]) -> Mapped:
        req = _dict_field(frame, "request")
        if req.get("subtype") == "hook_callback":
            return Mapped(hook_request=parse_hook_callback(frame))
        if req.get("subtype") != "can_use_tool":
            return Mapped(unsupported_request=(
                str(frame.get("request_id", "")), str(req.get("subtype") or ""),
            ))
        fluid_rid = new_id("perm")
        tool_name = req.get("tool_name", "")
        tid = req.get("tool_use_id") or ""
        inp = req.get("input", {}) or {}
        risk = req.get("risk", "safe")
        raw_suggestions = req.get("permission_suggestions")
        suggestions = tuple(
            s for s in raw_suggestions if isinstance(s, dict)
        ) if isinstance(raw_suggestions, list) else ()
        bridge = PermissionBridge(
            cli_request_id=frame.get("request_id", ""), fluid_request_id=fluid_rid,
            tool_name=tool_name, tool_use_id=tid, input=inp,
            suggestions=suggestions,
        )
        event = PermissionRequest(
            session_id=self.session_id, turn_id=self.turn_id, request_id=fluid_rid,
            tool_use_id=tid, name=tool_name, input=inp, risk=risk,
        )
        return Mapped(events=[event], permission=bridge)

    def _on_control_response(self, frame: dict[str, Any]) -> Mapped:
        """Correlate the CLI's reply to a loop-issued control_request.

        No event is emitted: control calls are request/response RPC riding the
        same stream, answered to the caller awaiting them, not part of the
        session's event history.
        """
        return Mapped(control_reply=parse_control_response(frame))
