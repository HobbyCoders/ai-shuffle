"""Shared OpenAI-compatible chat/completions machinery.

Everything here is provider-neutral: message translation (normalized
``Message``s → chat/completions ``messages``), tool-spec translation, usage
normalization, the SSE-chunk → StreamChunk normalizer, and the streaming POST
loop with retry. Provider-specific request-body flavoring (OpenRouter's
``usage: {include}``, cache_control placement, ``reasoning``) stays in each
provider's ``serialize.py``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from fluid.providers._trace import trace_request

from fluid.messages import (
    Image,
    Message,
    Reasoning,
    Text,
    Thinking,
    ToolResultBlock,
    ToolUseBlock,
)
from fluid.providers._retry import RETRIABLE_STATUSES, RetryPolicy
from fluid.providers._sse import sse_events
from fluid.providers.base import StreamChunk

# ─── request: Message[] → chat/completions messages ──────────────────────────


def system_message(system: Any) -> dict[str, Any] | None:
    """Flatten fluid's ``system`` (str or block list) into one system message."""
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


def tool_result_text(block: ToolResultBlock) -> str:
    parts: list[str] = []
    for c in block.content:
        if isinstance(c, Text):
            parts.append(c.text)
        elif isinstance(c, Image):
            # Never dump block dicts — a full base64 payload as text is worse
            # than useless. Images ride the content array (see
            # tool_result_content) when the leg carries them at all.
            parts.append("[image attached]")
        else:
            parts.append(json.dumps(c.to_dict(), separators=(",", ":"), ensure_ascii=False))
    return "".join(parts)


def tool_result_content(block: ToolResultBlock) -> str | list[dict[str, Any]]:
    """Content for a role:"tool" message.

    Plain string when the result is text-only (maximum compatibility); a
    multimodal content-part array — text plus image_url parts — when it
    carries images. Providers that cannot take images in tool results will
    reject or ignore the image parts but still see the text; the previous
    behavior (json.dumps of the Image block) shipped hundreds of KB of
    base64 as TEXT on every subsequent request.
    """
    has_image = any(isinstance(c, Image) for c in block.content)
    if not has_image:
        return tool_result_text(block)
    out: list[dict[str, Any]] = []
    for c in block.content:
        if isinstance(c, Text):
            out.append({"type": "text", "text": c.text})
        elif isinstance(c, Image):
            out.append(image_part(c))
    return out


def image_part(block: Image) -> dict[str, Any]:
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{block.media_type};base64,{block.data}"},
    }


def translate_message(msg: Message) -> list[dict[str, Any]]:
    """One fluid ``Message`` → one or more chat/completions messages.

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
                    "content": tool_result_content(block),
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
            content.extend(image_part(i) for i in images)
            out.append({"role": msg.role, "content": content})
        elif text_parts:
            out.append({"role": msg.role, "content": "".join(text_parts)})
    return out


def translate_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fluid tool specs → OpenAI function specs."""
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


# ─── usage / stop-reason mapping ─────────────────────────────────────────────

_STOP_REASON = {
    "tool_calls": "tool_use",
    "stop": "end_turn",
    "length": "max_tokens",
    "content_filter": "end_turn",
    "function_call": "tool_use",
}


def map_stop_reason(finish_reason: str) -> str:
    return _STOP_REASON.get(finish_reason, "end_turn")


def normalize_usage(usage: dict[str, Any]) -> dict[str, int]:
    """prompt/completion/cached token counts, each degrading to 0."""
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
        self._index_of: dict[str, int] = {}  # logical key → fluid block index
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
                self._stop_reason = map_stop_reason(choice["finish_reason"])

        # The terminal chunk (include_usage) carries usage with empty choices —
        # close every open block, then emit usage + message_stop.
        usage = data.get("usage")
        if usage is not None:
            for key in self._order:
                out.append(StreamChunk("block_stop", {"index": self._index_of[key]}))
            normalized: dict[str, Any] = dict(normalize_usage(usage))
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


# ─── streaming POST loop (shared by OpenRouter + local clients) ──────────────


async def stream_chat_completions(
    url: str,
    body: dict[str, Any],
    headers: dict[str, str],
    *,
    http_client: httpx.AsyncClient | None,
    timeout: float,
    retry: RetryPolicy,
) -> AsyncIterator[StreamChunk]:
    """POST ``body`` to ``url`` and normalize the SSE response into StreamChunks.

    Retries retriable statuses and pre-stream transport failures per ``retry``;
    mid-stream failures surface as an ``error`` chunk (chunks were already
    delivered downstream). When ``http_client`` is None a client is created and
    closed per call.
    """
    client = http_client or httpx.AsyncClient(timeout=timeout)
    owns_client = http_client is None
    attempt = 0
    try:
        while True:
            yielded = False
            try:
                trace_request("chat_completions", body)
                async with client.stream("POST", url, json=body, headers=headers) as resp:
                    if resp.status_code != 200:
                        text = (await resp.aread()).decode(errors="replace")
                        retriable = resp.status_code in RETRIABLE_STATUSES
                        if retriable and attempt < retry.max_retries:
                            await asyncio.sleep(
                                retry.delay(attempt, resp.headers.get("retry-after"))
                            )
                            attempt += 1
                            continue
                        yield StreamChunk(
                            "error",
                            {
                                "status": resp.status_code,
                                "message": text[:2000],
                                "retriable": retriable,
                            },
                        )
                        return
                    normalizer = ChatCompletionsNormalizer()
                    async for event_name, data in sse_events(resp.aiter_lines()):
                        for chunk in normalizer.consume(event_name, data):
                            yielded = True
                            yield chunk
                    return
            except httpx.TransportError as exc:
                # Pre-stream connection failures retry; mid-stream ones
                # don't (chunks were already delivered downstream).
                if not yielded and attempt < retry.max_retries:
                    await asyncio.sleep(retry.delay(attempt))
                    attempt += 1
                    continue
                yield StreamChunk(
                    "error",
                    {"message": f"{type(exc).__name__}: {exc}", "retriable": False},
                )
                return
    finally:
        if owns_client:
            await client.aclose()
