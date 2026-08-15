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
    Message,
    Reasoning,
    Text,
    Thinking,
    ToolResultBlock,
    ToolUseBlock,
)
from fluid.providers.base import ProviderRequest, StreamChunk


def _block_to_api(block: Any) -> dict[str, Any] | None:
    if isinstance(block, Text):
        return {"type": "text", "text": block.text}
    if isinstance(block, Thinking):
        return None  # carried via its Reasoning envelope
    if isinstance(block, Reasoning):
        if block.origin != "anthropic":
            return None  # foreign reasoning is dropped for Anthropic calls
        if block.raw is not None:
            return block.raw  # verbatim resend (signed thinking block)
        return None
    if isinstance(block, ToolUseBlock):
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if isinstance(block, ToolResultBlock):
        content = [b for b in (_block_to_api(c) for c in block.content) if b]
        return {
            "type": "tool_result",
            "tool_use_id": block.tool_use_id,
            "content": content,
            "is_error": block.is_error,
        }
    if isinstance(block, Image):
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": block.media_type, "data": block.data},
        }
    return None


def build_request_body(request: ProviderRequest) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    for msg in request.messages:
        content = [b for b in (_block_to_api(blk) for blk in msg.content) if b]
        if content:
            messages.append({"role": msg.role, "content": content})

    body: dict[str, Any] = {
        "model": request.model,
        "max_tokens": request.max_tokens,
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
    if request.thinking:
        body["thinking"] = request.thinking
    if request.effort:
        # Anthropic effort is GA and lives inside output_config, not top-level.
        # Values pass through verbatim (low/medium/high/xhigh/max).
        body["output_config"] = {"effort": request.effort}
    if breakpoints > 0 and messages:
        last = messages[-1]["content"]
        if last:
            last[-1] = {**last[-1], "cache_control": {"type": "ephemeral"}}
    body.update(request.extra)
    return body


def sse_to_chunk(event_name: str, data: dict[str, Any]) -> StreamChunk | None:
    t = data.get("type", event_name)
    if t == "message_start":
        usage = data.get("message", {}).get("usage", {})
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
        payload: dict[str, Any] = {"usage": data.get("usage", {})}
        payload["stop_reason"] = data.get("delta", {}).get("stop_reason")
        return StreamChunk("usage", payload)
    if t == "message_stop":
        return StreamChunk("message_stop", {})
    if t == "error":
        return StreamChunk("error", data.get("error", {}))
    return None
