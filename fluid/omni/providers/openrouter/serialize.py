"""OpenRouter (OpenAI-compatible chat/completions) serialization.

Request side: normalized ``Message``s → a chat/completions request body. Reasoning
blocks from ANY origin are dropped (chat/completions has no verbatim reasoning
round-trip — OpenRouter caps declare no stateful reasoning). ``stream_options.
include_usage`` is set so the terminal chunk carries a usage object.

Response side: chat/completions SSE chunks (``choices[].delta``; the ``[DONE]``
sentinel is already stripped by omni's sse reader) → normalized ``StreamChunk``s.
Blocks (text / reasoning / one per tool-call index) open lazily and are all closed
at the terminal usage chunk, since chat/completions has no per-block stop event.
"""

from __future__ import annotations

import json
from typing import Any

from omni.messages import (
    Image,
    Message,
    Reasoning,
    Text,
    Thinking,
    ToolResultBlock,
    ToolUseBlock,
)
from omni.providers.base import ProviderRequest, StreamChunk


# ─── request: Message[] → chat/completions body ──────────────────────────────


def _system_message(system: Any) -> dict[str, Any] | None:
    if system is None:
        return None
    if isinstance(system, str):
        text = system
    elif isinstance(system, list):
        text = "".join(
            str(b.get("text", "")) if isinstance(b, dict) else str(b) for b in system
        )
    else:
        text = str(system)
    return {"role": "system", "content": text}


def _tool_result_text(block: ToolResultBlock) -> str:
    parts: list[str] = []
    for c in block.content:
        if isinstance(c, Text):
            parts.append(c.text)
        else:
            parts.append(json.dumps(c.to_dict(), separators=(",", ":"), ensure_ascii=False))
    return "".join(parts)


def _image_part(block: Image) -> dict[str, Any]:
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{block.media_type};base64,{block.data}"},
    }


def _translate_message(msg: Message) -> list[dict[str, Any]]:
    """One omni ``Message`` → one or more chat/completions messages.

    Tool results become standalone ``role:"tool"`` messages (emitted first, in
    order); text/image blocks coalesce into one user/assistant message; an
    assistant's tool_use blocks attach as a ``tool_calls`` array. Reasoning and
    thinking blocks are dropped.
    """
    out: list[dict[str, Any]] = []
    text_parts: list[str] = []
    images: list[Image] = []
    tool_calls: list[dict[str, Any]] = []

    for block in msg.content:
        if isinstance(block, Text):
            text_parts.append(block.text)
        elif isinstance(block, Image):
            images.append(block)
        elif isinstance(block, ToolUseBlock):
            tool_calls.append(
                {
                    "id": block.id,
                    "type": "function",
                    "function": {
                        "name": block.name,
                        "arguments": json.dumps(
                            block.input, separators=(",", ":"), ensure_ascii=False
                        ),
                    },
                }
            )
        elif isinstance(block, ToolResultBlock):
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": block.tool_use_id,
                    "content": _tool_result_text(block),
                }
            )
        elif isinstance(block, (Reasoning, Thinking)):
            continue  # no verbatim reasoning round-trip on chat/completions

    if msg.role == "assistant":
        message: dict[str, Any] = {"role": "assistant"}
        message["content"] = "".join(text_parts) if text_parts else None
        if tool_calls:
            message["tool_calls"] = tool_calls
        # Skip a wholly-empty assistant message (e.g. a reasoning-only turn).
        if message["content"] or tool_calls:
            out.append(message)
    else:
        if images:
            content: list[dict[str, Any]] = []
            if text_parts:
                content.append({"type": "text", "text": "".join(text_parts)})
            content.extend(_image_part(i) for i in images)
            out.append({"role": msg.role, "content": content})
        elif text_parts:
            out.append({"role": msg.role, "content": "".join(text_parts)})
    return out


def _translate_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.get("name"),
                "description": tool.get("description", ""),
                "parameters": tool.get(
                    "input_schema", {"type": "object", "properties": {}}
                ),
            },
        }
        for tool in tools
    ]


def build_request_body(request: ProviderRequest) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    system = _system_message(request.system)
    if system is not None:
        messages.append(system)
    for msg in request.messages:
        messages.extend(_translate_message(msg))

    body: dict[str, Any] = {
        "model": request.model,
        "messages": messages,
        "max_tokens": request.max_tokens,
        "stream": True,
        # Ask for a usage object on the terminal chunk (finalize hook).
        "stream_options": {"include_usage": True},
        # OpenRouter usage accounting: adds `cost` (USD credits) to the
        # terminal chunk's usage object alongside the token counts.
        "usage": {"include": True},
    }
    if request.tools:
        body["tools"] = _translate_tools(request.tools)
    if request.effort:
        # Harness effort vocabulary is Anthropic-shaped (low..max); OpenRouter's
        # unified reasoning param knows low/medium/high — clamp the top tiers.
        clamped = {"xhigh": "high", "max": "high"}.get(request.effort, request.effort)
        if clamped in ("minimal", "low", "medium", "high"):
            body["reasoning"] = {"effort": clamped}
    elif request.thinking:
        effort = request.thinking.get("effort")
        if effort in ("minimal", "low", "medium", "high"):
            body["reasoning"] = {"effort": effort}
    body.update(request.extra)
    return body


# ─── usage / stop-reason mapping ─────────────────────────────────────────────

_STOP_REASON = {
    "tool_calls": "tool_use",
    "stop": "end_turn",
    "length": "max_tokens",
    "content_filter": "end_turn",
    "function_call": "tool_use",
}


def _map_stop_reason(finish_reason: str) -> str:
    return _STOP_REASON.get(finish_reason, "end_turn")


def _normalize_usage(usage: dict[str, Any]) -> dict[str, int]:
    prompt = int(usage.get("prompt_tokens", 0) or 0)
    completion = int(usage.get("completion_tokens", 0) or 0)
    cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0)
    return {
        "input_tokens": max(prompt - cached, 0),
        "output_tokens": completion,
        "cache_read_input_tokens": cached,
        "total_tokens": int(usage.get("total_tokens", 0) or 0),
    }


# ─── response: chat/completions SSE chunks → StreamChunks ────────────────────


class ChatCompletionsNormalizer:
    """Stateful normalizer: feed parsed chat/completions SSE chunks, get
    StreamChunks. One instance per stream."""

    def __init__(self) -> None:
        self._started = False
        self._index_of: dict[str, int] = {}  # logical key → omni block index
        self._order: list[str] = []
        self._next_index = 0
        self._stop_reason = "end_turn"

    def _open(self, key: str, block_type: str, **kw: Any) -> list[StreamChunk]:
        if key in self._index_of:
            return []
        index = self._next_index
        self._next_index += 1
        self._index_of[key] = index
        self._order.append(key)
        return [
            StreamChunk(
                "block_start",
                {
                    "index": index,
                    "block_type": block_type,
                    "tool_name": kw.get("tool_name"),
                    "tool_use_id": kw.get("tool_use_id"),
                    "block": kw.get("block", {}),
                },
            )
        ]

    def consume(self, name: str, data: dict[str, Any]) -> list[StreamChunk]:
        data = data or {}
        out: list[StreamChunk] = []
        if not self._started:
            self._started = True
            out.append(StreamChunk("message_start", {"usage": {}, "message": {}}))

        for choice in data.get("choices") or []:
            delta = choice.get("delta") or {}
            content = delta.get("content")
            if content:
                out += self._open("text", "text")
                out.append(
                    StreamChunk(
                        "text_delta", {"index": self._index_of["text"], "text": content}
                    )
                )
            reasoning = delta.get("reasoning")
            if reasoning:
                out += self._open("reasoning", "thinking")
                out.append(
                    StreamChunk(
                        "thinking_delta",
                        {"index": self._index_of["reasoning"], "text": reasoning},
                    )
                )
            for tc in delta.get("tool_calls") or []:
                key = f"tool:{tc.get('index', 0)}"
                fn = tc.get("function") or {}
                out += self._open(
                    key,
                    "tool_use",
                    tool_name=fn.get("name"),
                    tool_use_id=tc.get("id"),
                    block=tc,
                )
                args = fn.get("arguments")
                if args:
                    out.append(
                        StreamChunk(
                            "tool_args_delta",
                            {"index": self._index_of[key], "partial_json": args},
                        )
                    )
            if choice.get("finish_reason"):
                self._stop_reason = _map_stop_reason(choice["finish_reason"])

        # The terminal chunk (include_usage) carries usage with empty choices —
        # close every open block, then emit usage + message_stop.
        usage = data.get("usage")
        if usage is not None:
            for key in self._order:
                out.append(StreamChunk("block_stop", {"index": self._index_of[key]}))
            normalized: dict[str, Any] = dict(_normalize_usage(usage))
            cost = usage.get("cost")
            if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                # Provider-reported cost (USD credits) wins over any estimate.
                normalized["cost_usd"] = float(cost)
            out.append(
                StreamChunk(
                    "usage",
                    {"usage": normalized, "stop_reason": self._stop_reason},
                )
            )
            out.append(StreamChunk("message_stop", {}))
        return out
