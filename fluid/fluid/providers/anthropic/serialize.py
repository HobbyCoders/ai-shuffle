"""Anthropic Messages API serialization.

Request side: normalized ``Message``s → Messages-API payload, with
``cache_control`` breakpoints placed on the trailing stable positions the
cache plan allows (system tail, last history block).

Response side: Messages-API SSE events → normalized ``StreamChunk``s.
Tool-call argument fragments are passed through byte-for-byte
(``tool_args_delta``); the engine accumulates and parses them only at
``block_stop``.
"""

from __future__ import annotations

from typing import Any

from fluid.messages import (
    Image,
    Reasoning,
    Text,
    Thinking,
    ToolResultBlock,
    ToolUseBlock,
)
from fluid.providers._transport import billing_summary, provider_error_summary
from fluid.providers.base import ProviderRequest, StreamChunk
from fluid.providers.anthropic.limits import resolve_max_tokens
from fluid.providers.anthropic.thinking import clamp_effort, resolve_thinking


def _block_to_api(block: Any, model: str | None = None) -> dict[str, Any] | None:
    if isinstance(block, Text):
        if not block.text:
            return None  # the API rejects an empty text block
        return {"type": "text", "text": block.text}
    if isinstance(block, Thinking):
        return None  # carried via its Reasoning envelope
    if isinstance(block, Reasoning):
        if block.origin != "anthropic":
            return None  # foreign reasoning is dropped for Anthropic calls
        if block.model is not None and model is not None and block.model != model:
            # Thinking signatures are model-bound: a signature another model
            # produced fails verification. Untagged legacy envelopes
            # (model=None) still resend as before.
            return None
        if block.raw is None:
            return None
        if block.raw.get("type") == "thinking" and not block.raw.get("signature"):
            # A thinking block the stream never signed (cut off, or the
            # stream died) fails verification on every later turn. Dropping
            # it costs the model its memory of that reasoning, nothing more.
            return None
        return block.raw  # verbatim resend: signed thinking, redacted thinking, server tool blocks
    if isinstance(block, ToolUseBlock):
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if isinstance(block, ToolResultBlock):
        content = [b for b in (_block_to_api(c, model) for c in block.content) if b]
        out: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": block.tool_use_id,
            "is_error": block.is_error,
        }
        if content:
            out["content"] = content
        return out
    if isinstance(block, Image):
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": block.media_type, "data": block.data},
        }
    return None


def build_request_body(request: ProviderRequest) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    for msg in request.messages:
        content = [
            b for b in (_block_to_api(blk, request.model) for blk in msg.content) if b
        ]
        if not content:
            continue  # nothing this dialect can carry (an unsigned thinking turn)
        if messages and messages[-1]["role"] == msg.role:
            # Roles must alternate. A dropped message leaves two neighbours
            # with the same role; folding the second into the first keeps
            # the order of every block and invents nothing.
            messages[-1]["content"].extend(content)
            continue
        messages.append({"role": msg.role, "content": content})

    body: dict[str, Any] = {
        "model": request.model,
        "max_tokens": resolve_max_tokens(request.model, request.max_tokens),
        "messages": messages,
        "stream": True,
    }

    breakpoints = request.cache_breakpoints
    if request.system:
        system = (
            [{"type": "text", "text": request.system}]
            if isinstance(request.system, str)
            else list(request.system)
        )
        if breakpoints > 0 and system:
            system[-1] = {**system[-1], "cache_control": {"type": "ephemeral"}}
            breakpoints -= 1
        body["system"] = system
    if request.tools:
        tools = [dict(t) for t in request.tools]
        # Tool defs are the stable second prefix segment: mark the last one so
        # the whole tool schema is cached and history churn never re-bills it.
        if breakpoints > 0 and tools:
            tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
            breakpoints -= 1
        body["tools"] = tools
    # Anthropic effort is GA and lives inside output_config, not top-level.
    # Both it and ``thinking`` are fitted to what this model accepts (xhigh on
    # a 4.6 model becomes high; a budget on 4.7+ becomes adaptive), since the
    # API rejects the whole request otherwise. See providers/anthropic/thinking.
    effort = clamp_effort(request.model, request.effort)
    thinking = resolve_thinking(request.model, request.thinking, effort)
    if thinking:
        budget = thinking.get("budget_tokens")
        if isinstance(budget, int) and budget >= body["max_tokens"]:
            # The budget must stay below max_tokens.
            thinking = {**thinking, "budget_tokens": max(1024, body["max_tokens"] - 1)}
        body["thinking"] = thinking
    if effort:
        body["output_config"] = {"effort": effort}
    if breakpoints > 0 and messages:
        last = messages[-1]["content"]
        if last:
            last[-1] = {**last[-1], "cache_control": {"type": "ephemeral"}}
    body.update(request.extra)
    return body


# Flat usage key carrying the 1-hour share of ``cache_creation_input_tokens``.
# The API reports the per-TTL split nested under ``usage.cache_creation``, and
# the engine's usage merge keeps only top-level integers, so the split would
# never reach the cost estimate without this copy. See ``pricing.py``.
CACHE_WRITE_1H_KEY = "cache_creation_1h_input_tokens"


def _with_ttl_split(usage: Any) -> Any:
    """``usage`` plus the flat 1-hour write count when the API reported one."""
    if not isinstance(usage, dict):
        return usage
    breakdown = usage.get("cache_creation")
    if not isinstance(breakdown, dict):
        return usage
    tokens_1h = breakdown.get("ephemeral_1h_input_tokens")
    if isinstance(tokens_1h, bool) or not isinstance(tokens_1h, int) or tokens_1h <= 0:
        return usage
    return {**usage, CACHE_WRITE_1H_KEY: tokens_1h}


def sse_to_chunk(event_name: str, data: dict[str, Any]) -> StreamChunk | None:
    t = data.get("type", event_name)
    if t == "message_start":
        usage = _with_ttl_split(data.get("message", {}).get("usage", {}))
        return StreamChunk("message_start", {"usage": usage, "message": data.get("message", {})})
    if t == "content_block_start":
        block = data.get("content_block", {})
        return StreamChunk(
            "block_start",
            {
                "index": data.get("index", 0),
                "block_type": block.get("type", "text"),
                "tool_name": block.get("name"),
                "tool_use_id": block.get("id"),
                "block": block,
            },
        )
    if t == "content_block_delta":
        delta = data.get("delta", {})
        dt = delta.get("type")
        index = data.get("index", 0)
        if dt == "text_delta":
            return StreamChunk("text_delta", {"index": index, "text": delta.get("text", "")})
        if dt == "thinking_delta":
            return StreamChunk("thinking_delta", {"index": index, "text": delta.get("thinking", "")})
        if dt == "input_json_delta":
            return StreamChunk(
                "tool_args_delta", {"index": index, "partial_json": delta.get("partial_json", "")}
            )
        if dt == "signature_delta":
            return StreamChunk(
                "signature_delta", {"index": index, "signature": delta.get("signature", "")}
            )
        return None
    if t == "content_block_stop":
        return StreamChunk("block_stop", {"index": data.get("index", 0)})
    if t == "message_delta":
        payload: dict[str, Any] = {"usage": _with_ttl_split(data.get("usage", {}))}
        payload["stop_reason"] = data.get("delta", {}).get("stop_reason")
        return StreamChunk("usage", payload)
    if t == "message_stop":
        return StreamChunk("message_stop", {})
    if t == "error":
        err = dict(data.get("error", {}) or {})
        # An overloaded or internal failure on an open stream is the same
        # blip a 529 or 500 status is; the client may retry it when nothing
        # has been delivered yet. Everything else names a fault in the
        # request and stays fatal.
        err.setdefault("retriable", err.get("type") in _RETRIABLE_STREAM_ERRORS)
        # The status the same failure carries as an HTTP response, so a host
        # reads an in-stream rate limit exactly like a 429.
        status = _STREAM_ERROR_STATUS.get(str(err.get("type") or ""))
        if status is not None:
            err.setdefault("status", status)
        summary = provider_error_summary(err.get("message"))
        if err.get("type") == "billing_error":
            summary = billing_summary("billing_error", summary)
        err.setdefault("provider_message", summary)
        return StreamChunk("error", err)
    return None


_RETRIABLE_STREAM_ERRORS = frozenset(
    ("overloaded_error", "api_error", "rate_limit_error")
)

# Anthropic's documented error type → HTTP status pairs.
_STREAM_ERROR_STATUS = {
    "invalid_request_error": 400,
    "authentication_error": 401,
    "billing_error": 402,
    "permission_error": 403,
    "not_found_error": 404,
    "request_too_large": 413,
    "rate_limit_error": 429,
    "api_error": 500,
    "timeout_error": 504,
    "overloaded_error": 529,
}
