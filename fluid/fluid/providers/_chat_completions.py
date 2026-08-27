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
import uuid
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
from fluid.providers._retry import (
    RETRIABLE_STATUSES,
    RetryPolicy,
    coerce_status,
    is_retriable_status,
)
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


def tool_result_content(block: ToolResultBlock) -> str:
    """Content for a role:"tool" message — always a plain string.

    The OpenAI chat/completions schema defines tool content as text. Sending
    image_url parts inside a role:"tool" message is accepted by some proxies
    and forwarded upstream, where it makes providers answer HTTP 200 with an
    empty stream for the rest of the conversation. So each image leaves a
    marker in its position here and rides a following role:"user" message
    instead (see translate_message). Dumping the Image block as JSON is not an
    option either — that shipped hundreds of KB of base64 as TEXT on every
    subsequent request.
    """
    parts: list[str] = []
    for c in block.content:
        if isinstance(c, Text):
            parts.append(c.text)
        elif isinstance(c, Image):
            parts.append("[image attached — see the user message that follows]")
        else:
            parts.append(json.dumps(c.to_dict(), separators=(",", ":"), ensure_ascii=False))
    return "".join(parts)


def image_part(block: Image) -> dict[str, Any]:
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{block.media_type};base64,{block.data}"},
    }


def translate_message(msg: Message) -> list[dict[str, Any]]:
    """One fluid ``Message`` → one or more chat/completions messages.

    Tool results become standalone ``role:"tool"`` messages (emitted first, in
    order) whose content is always text; images carried by a tool result move
    into the single user message that follows them. Text/image blocks coalesce
    into that same one user/assistant message; an assistant's tool_use blocks
    attach as a ``tool_calls`` array. Reasoning and thinking blocks are dropped.
    """
    out: list[dict[str, Any]] = []
    text_parts: list[str] = []
    images: list[Image] = []
    tool_calls: list[dict[str, Any]] = []
    tool_result_images: list[tuple[str, list[Image]]] = []

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
            found = [c for c in block.content if isinstance(c, Image)]
            if found:
                tool_result_images.append((block.tool_use_id, found))
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
        # Images pulled out of tool results lead the user message that follows
        # them, each labelled with the tool call it answers.
        lead_parts: list[dict[str, Any]] = []
        for tool_use_id, tool_images in tool_result_images:
            lead_parts.append(
                {"type": "text", "text": f"Image from tool result {tool_use_id}:"}
            )
            lead_parts.extend(image_part(i) for i in tool_images)

        if lead_parts or images:
            content: list[dict[str, Any]] = list(lead_parts)
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
    cache_write = int(
        (usage.get("prompt_tokens_details") or {}).get("cache_write_tokens", 0) or 0
    )
    return {
        "input_tokens": max(prompt - cached - cache_write, 0),
        "output_tokens": completion,
        "cache_read_input_tokens": cached,
        "cache_creation_input_tokens": cache_write,
        "total_tokens": int(usage.get("total_tokens", 0) or 0),
    }


# ─── response: chat/completions SSE chunks → StreamChunks ────────────────────


class ChatCompletionsNormalizer:
    """Stateful normalizer: feed parsed chat/completions SSE chunks, get
    StreamChunks. One instance per stream."""

    def __init__(self) -> None:
        self.errored = False
        self.finished = False
        # OpenRouter's own verdict on the upstream call, e.g. "network_error"
        # behind a normalized finish_reason of "stop". The engine surfaces it
        # when a completion comes back empty.
        self.native_stop_reason: str | None = None
        self._started = False
        self._index_of: dict[str, int] = {}  # logical key → fluid block index
        self._order: list[str] = []
        self._next_index = 0
        self._stop_reason = "end_turn"
        self._response_meta: dict[str, Any] = {}

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
        for source, target in (
            ("id", "generation_id"), ("model", "effective_model"),
            ("provider", "effective_provider"),
        ):
            if isinstance(data.get(source), str) and data[source]:
                self._response_meta[target] = data[source]
        out: list[StreamChunk] = []
        if not self._started:
            self._started = True
            out.append(StreamChunk("message_start", {"usage": {}, "message": {}}))

        # OpenRouter (and some proxies) answer upstream failures and rate
        # limits with HTTP 200 and a chunk carrying only ``{"error": ...}``.
        # Without this the turn would end silently with zero usage.
        err = data.get("error")
        if err and not data.get("choices"):
            if isinstance(err, dict):
                message = str(err.get("message") or err)
                code = err.get("code")
                if coerce_status(code) is None and err.get("status") is not None:
                    code = err["status"]
            else:
                message, code = str(err), None
            self.errored = True
            # The embedded code decides retriability, exactly as a non-200
            # status would: a Cloudflare 520 from the upstream provider is a
            # blip worth another attempt, an auth or validation failure is not.
            out.append(
                StreamChunk(
                    "error",
                    {
                        "status": code,
                        "message": message[:2000],
                        "retriable": is_retriable_status(code),
                    },
                )
            )
            return out

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
            if choice.get("native_finish_reason"):
                self.native_stop_reason = str(choice["native_finish_reason"])

        # The terminal chunk (include_usage) carries usage with empty choices —
        # close every open block, then emit usage + message_stop.
        usage = data.get("usage")
        if usage is not None:
            for key in self._order:
                out.append(StreamChunk("block_stop", {"index": self._index_of[key]}))
            normalized: dict[str, Any] = dict(normalize_usage(usage))
            if self._response_meta:
                normalized["provider_response"] = dict(self._response_meta)
            cost = usage.get("cost")
            if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                # Provider-reported cost (USD credits) wins over any estimate.
                normalized["cost_usd"] = float(cost)
            terminal: dict[str, Any] = {}
            if self.native_stop_reason:
                terminal["native_stop_reason"] = self.native_stop_reason
            out.append(
                StreamChunk(
                    "usage",
                    {
                        "usage": normalized,
                        "stop_reason": self._stop_reason,
                        **terminal,
                    },
                )
            )
            out.append(StreamChunk("message_stop", dict(terminal)))
            self.finished = True
        return out


# ─── streaming POST loop (shared by OpenRouter + local clients) ──────────────


# A dead upstream replica can answer 200 and then emit nothing but SSE keepalive
# comments (": OPENROUTER PROCESSING") indefinitely. Deadline the FIRST data
# event so the engine's empty-completion retry gets a chance to route around it
# instead of the caller staring at dead air.
FIRST_DATA_TIMEOUT = 30.0


async def stream_chat_completions(
    url: str,
    body: dict[str, Any],
    headers: dict[str, str],
    *,
    http_client: httpx.AsyncClient | None,
    timeout: float,
    retry: RetryPolicy,
    track_provider_requests: bool = False,
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
    logical_call_id = f"fluid-{uuid.uuid4().hex}"
    request_headers = dict(headers)
    if track_provider_requests:
        # Correlation only: OpenRouter does not document this as an idempotency key.
        request_headers["X-Fluid-Call-ID"] = logical_call_id
    attempts: list[dict[str, Any]] = []

    def with_request_evidence(payload: dict[str, Any]) -> dict[str, Any]:
        if track_provider_requests:
            payload["provider_requests"] = {
                "logical_call_id": logical_call_id,
                "attempts": list(attempts),
            }
        return payload
    try:
        while True:
            yielded = False
            try:
                trace_request("chat_completions", body)
                async with client.stream(
                    "POST", url, json=body, headers=request_headers
                ) as resp:
                    attempt_meta = {
                        "attempt": attempt + 1,
                        "status": resp.status_code,
                    }
                    for header, field in (
                        ("x-generation-id", "generation_id"),
                        ("x-request-id", "request_id"),
                    ):
                        value = resp.headers.get(header)
                        if value:
                            attempt_meta[field] = value
                    attempts.append(attempt_meta)
                    # Surface the physical provider request as soon as its
                    # response headers arrive.  A stream may be interrupted
                    # before OpenRouter emits its terminal usage chunk; this
                    # early correlation record keeps that billable request
                    # visible to the engine even in that case.  Do not set the
                    # local ``yielded`` flag here: it deliberately means
                    # provider *content* was delivered and controls whether a
                    # pre-content transport failure may be retried.
                    if track_provider_requests:
                        yield StreamChunk(
                            "provider_request",
                            with_request_evidence({}),
                        )
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
                            with_request_evidence({
                                "status": resp.status_code,
                                "message": text[:2000],
                                "retriable": retriable,
                            }),
                        )
                        return
                    normalizer = ChatCompletionsNormalizer()
                    events = sse_events(resp.aiter_lines()).__aiter__()
                    starved = False
                    awaiting_first = True
                    while True:
                        try:
                            if awaiting_first:
                                # Only the first data event is deadlined; once
                                # the provider says anything the watchdog is
                                # done (mid-stream stalls are out of scope).
                                # sse_events drops comment lines, so a keepalive
                                # heartbeat can never satisfy this.
                                event_name, data = await asyncio.wait_for(
                                    events.__anext__(), FIRST_DATA_TIMEOUT
                                )
                            else:
                                event_name, data = await events.__anext__()
                        except StopAsyncIteration:
                            break
                        except (asyncio.TimeoutError, TimeoutError):
                            starved = True
                            break
                        awaiting_first = False
                        for chunk in normalizer.consume(event_name, data):
                            if chunk.kind == "usage" and track_provider_requests:
                                payload = dict(chunk.payload)
                                usage = dict(payload.get("usage") or {})
                                usage["provider_requests"] = {
                                    "logical_call_id": logical_call_id,
                                    "attempts": list(attempts),
                                }
                                payload["usage"] = usage
                                chunk = StreamChunk("usage", payload)
                            yielded = True
                            yield chunk
                    if starved or (not normalizer.finished and not normalizer.errored):
                        # Stream closed before the usage/terminal chunk, or the
                        # watchdog gave up waiting for its first byte of data:
                        # the provider dropped the connection, sent nothing, or
                        # is hanging. This is the shape that carries
                        # native_finish_reason without ever reaching
                        # usage/message_stop, so the upstream verdict rides the
                        # error chunk instead.
                        message = (
                            "stream ended before the provider sent "
                            "a terminal usage chunk (empty or truncated response)"
                        )
                        if starved:
                            message += f" (no data within {FIRST_DATA_TIMEOUT:g}s)"
                        truncated: dict[str, Any] = {
                            "message": message,
                            "retriable": False,
                        }
                        if normalizer.native_stop_reason:
                            truncated["native_stop_reason"] = (
                                normalizer.native_stop_reason
                            )
                        yield StreamChunk("error", truncated)
                    return
            except httpx.TransportError as exc:
                # Pre-stream connection failures retry; mid-stream ones
                # don't (chunks were already delivered downstream).
                if attempts and attempts[-1].get("attempt") == attempt + 1:
                    attempts[-1]["transport_error"] = type(exc).__name__
                else:
                    attempts.append({
                        "attempt": attempt + 1,
                        "transport_error": type(exc).__name__,
                    })
                if not yielded and attempt < retry.max_retries:
                    await asyncio.sleep(retry.delay(attempt))
                    attempt += 1
                    continue
                yield StreamChunk(
                    "error",
                    with_request_evidence({
                        "message": f"{type(exc).__name__}: {exc}",
                        "retriable": False,
                    }),
                )
                return
    finally:
        if owns_client:
            await client.aclose()
