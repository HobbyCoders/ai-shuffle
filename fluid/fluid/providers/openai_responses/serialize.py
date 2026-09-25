"""OpenAI Responses API serialization.

Request side: normalized ``Message``s → a Responses ``input`` item list plus the
non-negotiable envelope the stateless-history mode requires (``store: false`` +
``include: ["reasoning.encrypted_content"]`` so encrypted reasoning items come
back with the blob we round-trip).

Response side: Responses SSE events → the normalized ``StreamChunk`` vocabulary.
The Responses stream is stateful (item ids, lazily-opened blocks, authoritative
final items delivered only on ``response.output_item.done``), so unlike the
Anthropic per-event ``sse_to_chunk`` this side is a small stateful normalizer.

Event-mapping knowledge is ported from a production translator gateway
(``responses_to_anthropic.py``) and ``docs/CODEX_SPIKE_FINDINGS.md`` — fluid's
chunk model is simpler, so this is a port of the *knowledge*, not the code:

  * ``response.completed.output`` is ALWAYS empty (spike surprise #2); the
    authoritative message/function_call/reasoning items arrive only via
    ``response.output_item.done``. Each ``block_stop`` therefore carries the
    complete done item under ``payload["block"]`` so the engine can rebuild the
    reasoning envelope (encrypted_content) and full function-call arguments.
  * ``function_call_arguments.delta`` fragments pass through byte-for-byte as
    ``tool_args_delta``; the ``obfuscation`` padding field is ignored (surprise
    #10 — only ``delta`` is load-bearing).
  * On stateless resend every input item strips ``id`` AND ``status`` (surprise
    #6); ``encrypted_content`` / ``call_id`` / ``arguments`` / ``summary`` /
    ``content`` are preserved verbatim.
  * Usage: ``input_tokens_details.cached_tokens`` → ``cache_read_input_tokens``
    (Anthropic-style keys the engine merges); Responses ``input_tokens``
    INCLUDES its cached subset, so we subtract it (clamped ≥ 0) to keep the
    engine's ``input + cache_read + cache_creation`` accounting correct.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

from fluid.messages import (
    Image,
    Message,
    Reasoning,
    Text,
    Thinking,
    ToolResultBlock,
    ToolUseBlock,
    UnknownBlock,
)
from fluid.providers._retry import is_retriable_status
from fluid.providers._sse import KEEPALIVE_EVENT
from fluid.providers.base import (
    INTERNAL_EXTRA_PREFIX,
    ErrorClass,
    ProviderRequest,
    StreamChunk,
)

# Fields echoed on output items that MUST be removed before a stateless
# (``store: false``) resend — the backend rejects resent items carrying them.
_RESEND_STRIP_KEYS = ("id", "status")

_EFFORT_LOW_CEIL = 4096
_EFFORT_MEDIUM_CEIL = 16384


# ─── request: Message[] → Responses input items ──────────────────────────────


def _strip_for_resend(item: dict[str, Any]) -> dict[str, Any]:
    """Copy an echoed output item minus the fields a stateless resend forbids."""
    return {k: v for k, v in item.items() if k not in _RESEND_STRIP_KEYS}


def _instructions_from_system(system: Any) -> str | None:
    if system is None:
        return None
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts = [
            str(b.get("text", "")) if isinstance(b, dict) else str(b) for b in system
        ]
        return "".join(parts)
    return str(system)


def _image_part(block: Image) -> dict[str, Any]:
    return {
        "type": "input_image",
        "image_url": f"data:{block.media_type};base64,{block.data}",
        "detail": "auto",
    }


def _tool_result_output(block: ToolResultBlock) -> str | list[dict[str, Any]]:
    """A ``function_call_output.output``: a plain string, or a content-part array
    when the result carries images (native-image form)."""
    has_image = any(isinstance(c, Image) for c in block.content)
    if not has_image:
        parts: list[str] = []
        for c in block.content:
            if isinstance(c, Text):
                parts.append(c.text)
            elif isinstance(c, UnknownBlock):
                continue  # a block kind from a newer release renders nothing
            else:
                parts.append(json.dumps(c.to_dict(), separators=(",", ":"), ensure_ascii=False))
        return "".join(parts)
    out: list[dict[str, Any]] = []
    for c in block.content:
        if isinstance(c, Text):
            out.append({"type": "input_text", "text": c.text})
        elif isinstance(c, Image):
            out.append(_image_part(c))
    return out


def _message_items(msg: Message, model: str) -> Iterator[dict[str, Any]]:
    """Flatten one fluid ``Message`` into Responses input items, in block order.

    Text/Image blocks coalesce into a single ``message`` item; a tool_use,
    tool_result or reasoning block flushes any open message item and emits its
    own item, so ordering (reasoning → text → function_call) is preserved.
    ``model`` is the request's model: reasoning envelopes another model
    produced are dropped, not resent.
    """
    # System/developer messages are remapped to "developer" (the Codex backend
    # 400s role:"system" input items but accepts "developer").
    role = "developer" if msg.role in ("system", "developer") else msg.role
    text_type = "output_text" if role == "assistant" else "input_text"

    content_parts: list[dict[str, Any]] = []

    def flush() -> Iterator[dict[str, Any]]:
        if content_parts:
            yield {"type": "message", "role": role, "content": list(content_parts)}
            content_parts.clear()

    for block in msg.content:
        if isinstance(block, Text):
            content_parts.append({"type": text_type, "text": block.text})
        elif isinstance(block, Image):
            content_parts.append(_image_part(block))
        elif isinstance(block, Thinking):
            continue  # visible thinking is carried by its Reasoning envelope
        elif isinstance(block, Reasoning):
            yield from flush()
            if block.origin != "openai_responses":
                continue  # foreign reasoning is dropped for Responses calls
            if block.model is not None and block.model != model:
                # Encrypted content is bound to the model that produced it;
                # xAI and OpenAI share this dialect, so after a model swap the
                # blob 400s the WHOLE request (invalid_encrypted_content).
                # Untagged legacy envelopes (model=None) still resend — the
                # engine's self-heal retry covers a stale one.
                continue
            if block.raw is not None:
                yield _strip_for_resend(block.raw)  # verbatim resend (id/status stripped)
            elif block.encrypted_content is not None:
                item: dict[str, Any] = {
                    "type": "reasoning",
                    "encrypted_content": block.encrypted_content,
                    "content": [],
                }
                if block.summary_text:
                    item["summary"] = [
                        {"type": "summary_text", "text": block.summary_text}
                    ]
                yield item
        elif isinstance(block, ToolUseBlock):
            yield from flush()
            yield {
                "type": "function_call",
                "call_id": block.id,
                "name": block.name,
                "arguments": json.dumps(
                    block.input, separators=(",", ":"), ensure_ascii=False
                ),
            }
        elif isinstance(block, ToolResultBlock):
            yield from flush()
            yield {
                "type": "function_call_output",
                "call_id": block.tool_use_id,
                "output": _tool_result_output(block),
            }
    yield from flush()


def _translate_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for tool in tools:
        if tool.get("type") == "image_generation":
            # Provider-native tools have no function name or input_schema.
            # Support is determined by the configured endpoint/account response,
            # not by recognizing a GPT-looking model name.
            out.append(dict(tool))
            continue
        out.append(
            {
                "type": "function",
                "name": tool.get("name"),
                "description": tool.get("description", ""),
                "parameters": tool.get(
                    "input_schema", {"type": "object", "properties": {}}
                ),
            }
        )
    return out


def _effort_from_budget(budget_tokens: int | None) -> str:
    if budget_tokens is None:
        return "medium"
    if budget_tokens < _EFFORT_LOW_CEIL:
        return "low"
    if budget_tokens < _EFFORT_MEDIUM_CEIL:
        return "medium"
    return "high"


# Model capability validation belongs to the caller; supported values must
# reach the API unchanged, including upper tiers and explicit reasoning off.
_EFFORT_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")


def _reasoning_config(
    thinking: dict[str, Any] | None, effort: str | None = None
) -> dict[str, Any] | None:
    if effort in _EFFORT_LEVELS:
        return {"effort": effort, "summary": "auto"}
    if not thinking:
        return None
    thinking_effort = thinking.get("effort")
    if thinking_effort not in _EFFORT_LEVELS:
        thinking_effort = _effort_from_budget(thinking.get("budget_tokens"))
    return {"effort": thinking_effort, "summary": "auto"}


def build_request_body(request: ProviderRequest) -> dict[str, Any]:
    input_items: list[dict[str, Any]] = []
    for msg in request.messages:
        input_items.extend(_message_items(msg, request.model))

    body: dict[str, Any] = {
        "model": request.model,
        "input": input_items,
        # Stateless-history mode invariants (spike surprises #3 / E4): always
        # stream, never persist server-side, always ask for the encrypted
        # reasoning blob so it round-trips.
        "store": False,
        "stream": True,
        "include": ["reasoning.encrypted_content"],
    }

    instructions = _instructions_from_system(request.system)
    if instructions is not None:
        body["instructions"] = instructions

    if request.tools:
        body["tools"] = _translate_tools(request.tools)

    if request.max_tokens is not None:
        body["max_output_tokens"] = request.max_tokens

    reasoning = _reasoning_config(request.thinking, request.effort)
    if reasoning is not None:
        body["reasoning"] = reasoning

    # extra passthrough enables stateful mode (previous_response_id, store:true),
    # tool_choice, temperature, etc. — and can override any default above.
    # ``_fluid_``-prefixed keys are engine-to-client signals (the turn
    # marker) and never reach the wire or the cache probe's body hash.
    body.update({
        key: value for key, value in request.extra.items()
        if not key.startswith(INTERNAL_EXTRA_PREFIX)
    })
    return body


# ─── in-stream error classification ──────────────────────────────────────────

# Codes and types the backend sends for a failure that says nothing about the
# request itself. Codex retries ``server_error``, ``rate_limit_exceeded`` and
# ``slow_down``; policy, invalid-request and context-length failures stay
# terminal because the same request fails the same way.
_RETRIABLE_ERROR_CODES = frozenset((
    "server_error", "rate_limit_exceeded", "slow_down", "server_is_overloaded",
))
_RETRIABLE_ERROR_TYPES = frozenset(("server_error", "service_unavailable_error"))

# Overload is the backend shedding load, and Codex ends the turn on it. Fluid
# retries it, but at most twice and never sooner than this many seconds, so
# the retry does not add to the load it is waiting out (experiment E11 tunes
# both numbers).
OVERLOAD_CODE = "server_is_overloaded"
OVERLOAD_MAX_RETRIES = 2
#: The Responses client's long backoff floor. Overload never retries sooner;
#: a rate-limit "try again in Xs" hint is never waited on for longer.
LONG_BACKOFF_FLOOR_S = 10.0
OVERLOAD_BACKOFF_FLOOR_S = LONG_BACKOFF_FLOOR_S

RATE_LIMIT_CODE = "rate_limit_exceeded"
# "Rate limit reached ... Please try again in 1.898s." (Codex parses the same
# phrase; the unit can also be ms.)
_TRY_AGAIN_RE = re.compile(
    r"try again in\s*([0-9]+(?:\.[0-9]+)?)\s*(ms|milliseconds?|s|secs?|seconds?)\b",
    re.IGNORECASE,
)


def retry_after_hint(err: Any) -> float | None:
    """Seconds from a "try again in Xs" phrase in an error, else None."""
    if isinstance(err, dict):
        text = " ".join(str(err.get(k) or "") for k in ("message", "detail"))
        if not text.strip():
            text = str(err)
    else:
        text = str(err or "")
    match = _TRY_AGAIN_RE.search(text)
    if match is None:
        return None
    value = float(match.group(1))
    if match.group(2).lower().startswith("m"):
        value /= 1000.0
    return value


def _error_fields(err: Any) -> tuple[str | None, str | None, Any]:
    """``(code, type, status)`` of a provider error object or error payload.

    The ``error`` SSE event can carry its fields at the top level next to
    ``"type": "error"``, which names the event, not the failure.
    """
    if not isinstance(err, dict):
        return None, None, None
    code = err.get("code")
    etype = err.get("type")
    return (
        code if isinstance(code, str) else None,
        etype if isinstance(etype, str) and etype != "error" else None,
        err.get("status"),
    )


def classify_error(err: Any) -> ErrorClass:
    """Classify a Responses in-stream error (``response.failed`` / ``error``).

    ``err`` is the provider's error object, or an ``error`` chunk payload
    built from one; both carry ``code``, ``type`` and ``status``. The
    Responses lane never drops its affinity key on an error, so
    ``drop_sticky`` is always False.
    """
    code, etype, status = _error_fields(err)
    if code == OVERLOAD_CODE:
        return ErrorClass(
            retriable=True, code=code,
            backoff_floor=OVERLOAD_BACKOFF_FLOOR_S,
            max_retries=OVERLOAD_MAX_RETRIES,
        )
    retriable = (
        code in _RETRIABLE_ERROR_CODES
        or etype in _RETRIABLE_ERROR_TYPES
        or is_retriable_status(status)
    )
    hint = retry_after_hint(err) if code == RATE_LIMIT_CODE else None
    return ErrorClass(retriable=retriable, code=code, retry_after=hint)


# ─── usage / stop-reason mapping ─────────────────────────────────────────────


def _normalize_usage(usage: dict[str, Any] | None) -> dict[str, int]:
    """Responses usage → Anthropic-style keys the engine merges.

    Responses ``input_tokens`` INCLUDES its cached / cache-write subsets; the
    engine treats ``input_tokens`` as exclusive of the cache fields (context =
    input + cache_read + cache_creation), so subtract the subsets (clamped ≥ 0)
    to avoid double-counting. ``reasoning_tokens`` stays a subset of
    ``output_tokens`` (carried in ``raw``, never added).
    """
    usage = usage or {}
    in_details = usage.get("input_tokens_details") or {}
    out_details = usage.get("output_tokens_details") or {}
    raw_input = int(usage.get("input_tokens", 0) or 0)
    cached = int(in_details.get("cached_tokens", 0) or 0)
    cache_write = int(in_details.get("cache_write_tokens", 0) or 0)
    return {
        "input_tokens": max(raw_input - cached - cache_write, 0),
        "output_tokens": int(usage.get("output_tokens", 0) or 0),
        "cache_read_input_tokens": cached,
        "cache_creation_input_tokens": cache_write,
        "reasoning_tokens": int(out_details.get("reasoning_tokens", 0) or 0),
        "total_tokens": int(usage.get("total_tokens", 0) or 0),
    }


def _incomplete_reason(response: dict[str, Any]) -> str | None:
    """The ``incomplete_details.reason`` of a truncated response, else None."""
    if response.get("status") != "incomplete":
        return None
    reason = (response.get("incomplete_details") or {}).get("reason")
    return str(reason) if reason else "unknown"


def _map_stop_reason(response: dict[str, Any], has_function_call: bool) -> str:
    # Truncation wins over an open function call: the call's arguments are
    # cut short, and the engine explains a decode failure by this reason.
    if _incomplete_reason(response) in ("max_output_tokens", "max_tokens"):
        return "max_tokens"
    if has_function_call:
        return "tool_use"
    return "end_turn"


# ─── response: Responses SSE events → StreamChunks ───────────────────────────

# Responses output-item type → fluid block type.
_ITEM_BLOCK_TYPE = {
    "message": "text",
    "reasoning": "thinking",
    "function_call": "tool_use",
}

# A reasoning item streams on two independent channels that share one item_id:
# ``reasoning_summary_text`` (the provider's own summary of its reasoning, asked
# for by ``summary: "auto"``) and ``reasoning_text`` (the reasoning draft
# itself). They are different kinds of text and xAI/grok sends both, so they get
# their own block indices — keyed here — and their own ``subtype`` on block_start
# so a host can label the summary as the provider's rather than the model's.
_SUMMARY_SUFFIX = "\x00summary"


def _summary_key(item_id: str) -> str:
    return f"{item_id}{_SUMMARY_SUFFIX}"


def _item_text(item: dict[str, Any]) -> str:
    """The visible text a finished ``message`` item carries, if any."""
    parts: list[str] = []
    for part in item.get("content") or ():
        if not isinstance(part, dict):
            continue
        if part.get("type") == "output_text":
            parts.append(part.get("text") or "")
        elif part.get("type") == "refusal":
            parts.append(part.get("refusal") or "")
    return "".join(parts)


class ResponsesStreamNormalizer:
    """Stateful normalizer: feed parsed Responses SSE events, get StreamChunks.

    One instance per stream. Blocks are opened lazily (on the ``output_item.added``
    skeleton or the first delta) and assigned monotonically-increasing integer
    indices so the engine's index-keyed accumulation works unchanged.
    """

    def __init__(self) -> None:
        self._started = False
        self._image_items: set[str] = set()
        self._index_of: dict[str, int] = {}
        self._next_index = 0
        self._has_function_call = False
        self._stopped: set[int] = set()
        # Trailing characters of the text emitted so far in the current run of
        # text blocks; "" once a block of another kind opens. See _open_text.
        self._text_tail = ""

    def _close_open_blocks(self) -> list[StreamChunk]:
        """``block_stop`` for every block still open, in index order.

        A truncated response never sends ``output_item.done`` for the item it
        was cut off in; the engine still needs the stop so it parses what
        arrived (and reports a cut-off tool call as such).
        """
        out: list[StreamChunk] = []
        for index in sorted(self._index_of.values()):
            if index not in self._stopped:
                self._stopped.add(index)
                out.append(StreamChunk("block_stop", {"index": index}))
        return out

    def _terminal(self, response: dict[str, Any]) -> list[StreamChunk]:
        """``usage`` plus ``message_stop`` for a finished (or cut-off) response."""
        payload: dict[str, Any] = {
            "usage": _normalize_usage(response.get("usage")),
            "stop_reason": _map_stop_reason(response, self._has_function_call),
        }
        reason = _incomplete_reason(response)
        if reason is not None:
            payload["native_stop_reason"] = f"incomplete:{reason}"
        return [StreamChunk("usage", payload), StreamChunk("message_stop", {})]

    def _open(
        self, key: str, block_type: str, **kw: Any
    ) -> list[StreamChunk]:
        """Open the block ``key`` names, once. ``key`` is the item_id for every
        channel except a reasoning summary, which gets its own (:func:`_summary_key`)."""
        if key in self._index_of:
            return []
        index = self._next_index
        self._next_index += 1
        self._index_of[key] = index
        if block_type != "text":
            self._text_tail = ""
        return [
            StreamChunk(
                "block_start",
                {
                    "index": index,
                    "block_type": block_type,
                    "subtype": kw.get("subtype"),
                    "tool_name": kw.get("tool_name"),
                    "tool_use_id": kw.get("tool_use_id"),
                    "block": kw.get("block", {}),
                },
            )
        ]

    def _text_chunk(self, index: int, text: str) -> StreamChunk:
        """A ``text_delta``, remembering how the block's text now ends."""
        if text:
            self._text_tail = (self._text_tail + text)[-2:]
        return StreamChunk("text_delta", {"index": index, "text": text})

    def _open_text(self, key: str) -> list[StreamChunk]:
        """Open a text block, separated from a text block that just closed.

        Each ``message`` item is its own block and a host renders their text
        end to end, so back-to-back items with nothing between them arrived
        as a run-on: a turn of short per-batch status lines read as
        "**Stopping local servers****Waiting for local test**". Start the new
        block on a blank line, minus whatever newlines the last one ended
        with, so text that already separated itself is left alone.
        """
        if key in self._index_of:
            return []
        tail = self._text_tail
        out = self._open(key, "text")
        missing = 2 - (len(tail) - len(tail.rstrip("\n")))
        if tail and missing > 0:
            out.append(self._text_chunk(self._index_of[key], "\n" * missing))
        return out

    def consume(self, name: str, data: dict[str, Any]) -> list[StreamChunk]:
        data = data or {}
        name = name or data.get("type", "")
        out: list[StreamChunk] = []

        if name == KEEPALIVE_EVENT:
            # An SSE comment heartbeat: the connection is open and the server
            # is still answering, nothing more. The engine rate-limits these.
            return [StreamChunk("provider_alive", {"signal": "keepalive"})]

        if name.startswith("response.image_generation_call."):
            # Partial previews are observations, never completed artifacts.
            return [StreamChunk("image_generation_progress", {
                "item_id": data.get("item_id"),
                "status": name.rsplit(".", 1)[-1],
                "partial_image_index": data.get("partial_image_index"),
                "partial_image_b64": data.get("partial_image_b64"),
            })]

        if name in ("response.created", "response.in_progress"):
            if not self._started:
                self._started = True
                out.append(
                    StreamChunk(
                        "message_start",
                        {"usage": {}, "message": data.get("response", {}) or {}},
                    )
                )
            return out

        if name == "response.output_item.added":
            item = data.get("item", {}) or {}
            itype = item.get("type", "")
            if itype == "function_call":
                self._has_function_call = True
                out += self._open(
                    item.get("id", ""),
                    "tool_use",
                    tool_name=item.get("name"),
                    tool_use_id=item.get("call_id"),
                    block=item,
                )
            # A message item is NOT opened here either: an item that streams
            # no text would leave an empty block behind (the one that preceded
            # the run-on status stubs), and the engine builds a text block from
            # its deltas alone, so the skeleton carries nothing it needs.
            # A reasoning item is NOT opened here: which of its two text
            # channels arrives first decides the block order, and a summary
            # that never streams must not leave an empty block behind. The
            # first delta opens it; ``output_item.done`` opens it if neither
            # channel produced one (encrypted content with no visible text).
            if itype == "reasoning":
                # Encrypted-only reasoning streams nothing else until the item
                # is done, which can take minutes. Say that the provider is
                # reasoning, so the silence reads as an open stream rather than
                # a request that never started. Not content: no block opens.
                out.append(StreamChunk("provider_alive", {"signal": "reasoning"}))
            return out

        if name in ("response.output_text.delta", "response.refusal.delta"):
            # A refusal is the model's visible answer in place of output text;
            # rendering it as text is what keeps the reply from coming back
            # blank.
            item_id = data.get("item_id", "")
            out += self._open_text(item_id)
            out.append(self._text_chunk(self._index_of[item_id], data.get("delta", "")))
            return out

        if name in (
            "response.reasoning_summary_text.delta",
            "response.reasoning_text.delta",
        ):
            # Two channels, two blocks. Concatenating them (which one shared
            # index did) produced a single "thinking" block on xAI/grok that ran
            # a clipped auto-summary straight into a second-person draft answer,
            # so the host rendered what read as a phantom collaborator.
            summary = name == "response.reasoning_summary_text.delta"
            item_id = data.get("item_id", "")
            key = _summary_key(item_id) if summary else item_id
            out += self._open(
                key,
                "thinking",
                subtype="reasoning_summary" if summary else "reasoning",
            )
            out.append(
                StreamChunk(
                    "thinking_delta",
                    {"index": self._index_of[key], "text": data.get("delta", "")},
                )
            )
            return out

        if name == "response.function_call_arguments.delta":
            # Byte-for-byte passthrough; the obfuscation padding field is ignored.
            item_id = data.get("item_id", "")
            out += self._open(item_id, "tool_use")
            out.append(
                StreamChunk(
                    "tool_args_delta",
                    {
                        "index": self._index_of[item_id],
                        "partial_json": data.get("delta", ""),
                    },
                )
            )
            return out

        if name == "response.output_item.done":
            # AUTHORITATIVE final item. Carry the complete payload on block_stop so
            # the engine gets encrypted_content / full arguments (response.completed
            # output is empty).
            item = data.get("item", {}) or {}
            itype = item.get("type", "")
            if itype == "image_generation_call":
                return self._image_result(item)
            if itype == "function_call":
                self._has_function_call = True
            block_type = _ITEM_BLOCK_TYPE.get(itype, "text")
            item_id = item.get("id", "")
            key = item_id
            subtype: str | None = None
            if block_type == "text" and key not in self._index_of:
                # Nothing streamed for this message item. Open it only if the
                # done payload has text to carry, and carry it as a delta —
                # the engine assembles a text block from deltas, so opening
                # one here regardless only ever produced an empty block.
                recovered = _item_text(item)
                if not recovered:
                    return out
                out += self._open_text(key)
                out.append(self._text_chunk(self._index_of[key], recovered))
            if itype == "reasoning":
                # The done item is the encrypted round-trip envelope and only
                # ONE block may carry it. It rides the reasoning-text block when
                # that channel streamed; otherwise the summary block, which is
                # what a summary-only item (the ordinary OpenAI shape) has. Both
                # absent: open the carrier now so the blob is not lost.
                summary_key = _summary_key(item_id)
                summary_index = self._index_of.get(summary_key)
                if item_id not in self._index_of and summary_index is not None:
                    key = summary_key
                    subtype = "reasoning_summary"
                else:
                    subtype = "reasoning"
                    if summary_index is not None and summary_index not in self._stopped:
                        # Close the sibling summary block first, without the
                        # done item: a second envelope would resend the same
                        # encrypted content twice.
                        self._stopped.add(summary_index)
                        out.append(StreamChunk("block_stop", {"index": summary_index}))
            out += self._open(
                key,
                block_type,
                subtype=subtype,
                tool_name=item.get("name"),
                tool_use_id=item.get("call_id"),
                block=item,
            )
            index = self._index_of.get(key)
            if index is not None and index not in self._stopped:
                self._stopped.add(index)
                out.append(StreamChunk("block_stop", {"index": index, "block": item}))
            return out

        if name == "response.completed":
            response = data.get("response", {}) or {}
            # Some Responses endpoints include final images only here; others
            # repeat output_item.done. Deliver each artifact once in either case.
            for item in response.get("output") or ():
                if isinstance(item, dict) and item.get("type") == "image_generation_call":
                    out += self._image_result(item)
            out += self._close_open_blocks()
            out += self._terminal(response)
            return out

        if name == "response.incomplete":
            # The response was cut off (output budget, content filter). Usage
            # is real and so is whatever streamed; close the block it stopped
            # in and end the message with the reason, instead of letting the
            # stream fall off the end as if nothing had been billed.
            response = data.get("response", {}) or {}
            if not self._index_of and _incomplete_reason(response) == "content_filter":
                out.append(
                    StreamChunk(
                        "error",
                        {
                            "message": (
                                "the provider's content filter stopped the "
                                "response before any output"
                            ),
                            "retriable": False,
                            "native_stop_reason": "incomplete:content_filter",
                        },
                    )
                )
                return out
            out += self._close_open_blocks()
            out += self._terminal(response)
            return out

        if name in ("response.failed", "error"):
            err = (
                (data.get("response", {}) or {}).get("error")
                or data.get("error")
                or data
            )
            payload: dict[str, Any] = {
                "message": str(err), "retriable": classify_error(err).retriable,
            }
            code, etype, _status = _error_fields(err)
            if code is not None:
                payload["code"] = code
            if etype is not None:
                payload["type"] = etype
            out.append(StreamChunk("error", payload))
            return out

        # content_part.added/done, reasoning_summary_part.*, in-progress skeletons
        # and unknown future events are not load-bearing — ignored.
        return out

    def _image_result(self, item: dict[str, Any]) -> list[StreamChunk]:
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id or item_id in self._image_items:
            return []
        if item.get("status") == "completed" and isinstance(item.get("result"), str) and item["result"]:
            self._image_items.add(item_id)
        return [StreamChunk("image_generation", {
            "item_id": item_id,
            "status": item.get("status"),
            "result": item.get("result"),
            "output_format": item.get("output_format", "png"),
            "revised_prompt": item.get("revised_prompt"),
            "usage": item.get("usage"),
        })]
