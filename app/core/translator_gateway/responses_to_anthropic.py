"""
OpenAI Responses stream  →  Anthropic Messages event stream translation
(§A.3 / §D3).

Two layers, per the boundary rule (§D1 / F5):

  1. ``ResponsesNormalizer`` — consumes parsed Responses SSE events (name + JSON
     payload) and yields provider-agnostic normalized events (events.py). It is
     the only place that knows the Responses wire vocabulary.
  2. ``AnthropicSSESerializer`` — consumes normalized events and yields Anthropic
     SSE frames (as dicts) in the canonical sequence:
        message_start
          → (content_block_start / content_block_delta* / content_block_stop)*
          → message_delta   (stop_reason + usage)
          → message_stop

``ResponsesToAnthropicTranslator`` wires the two together for the incremental
streaming path and also exposes ``final_response()`` for the buffered / non-
streaming Anthropic path (stage 2).

Binding contract points (docs/CODEX_SPIKE_FINDINGS.md):
  * Final items are accumulated from ``response.output_item.done`` ONLY;
    ``response.completed.output`` is always empty and is ignored (surprise #2).
  * ``function_call_arguments.delta`` fragments are re-emitted byte-for-byte as
    Anthropic ``input_json_delta`` (surprise #10: the ``obfuscation`` padding is
    ignored — only ``delta`` is load-bearing). This is THE fidelity surface
    (LiteLLM #25321).
  * Reasoning summary text streams as Anthropic ``thinking_delta``; the reasoning
    round-trip envelope (§D4) is emitted as a ``signature_delta`` at block close.
  * Usage: input/output/total + cache fields; ``reasoning_tokens`` ⊂
    ``output_tokens`` (never double-counted, surprise E5 / §D3c).

No network, no I/O, no credentials.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Iterator, List, Optional

from app.core.translator_gateway.events import (
    FinalReasoningItem,
    FinalTextItem,
    FinalToolCallItem,
    MessageEnd,
    MessageStart,
    NormalizedEvent,
    NormalizedResponse,
    NormalizedUsage,
    TextDelta,
    ThinkingDelta,
    ThinkingEnd,
    ToolCallArgsDelta,
    ToolCallEnd,
    ToolCallStart,
    UsageUpdate,
    encode_reasoning_signature,
)


# ─── Layer 1: Responses SSE → normalized events ──────────────────────────────

class ResponsesNormalizer:
    """Stateful, incremental parser: Responses SSE events → normalized events.

    Feed events with :meth:`consume(name, payload)` as they arrive off the wire.
    Accumulates authoritative final items from ``output_item.done`` so
    :meth:`final_response` can rebuild the whole turn without replaying the SSE.
    """

    def __init__(self) -> None:
        self._started = False
        self._response_id: Optional[str] = None
        self._model: Optional[str] = None
        # item_id → type, so late deltas can be attributed.
        self._item_types: Dict[str, str] = {}
        self._order: List[str] = []
        # item_id → final item dict (from output_item.done), authoritative.
        self._final_items: Dict[str, Dict[str, Any]] = {}
        self._has_function_call = False
        self._usage = NormalizedUsage()
        self._stop_reason = "end_turn"

    # -- helpers --------------------------------------------------------------

    def _record_item(self, item_id: str, item_type: str) -> None:
        if item_id not in self._item_types:
            self._item_types[item_id] = item_type
            self._order.append(item_id)

    def _message_id(self) -> str:
        # Reuse the upstream response id so the Anthropic frame is stable and
        # traceable; fall back to a fixed prefix if the backend omitted it.
        base = self._response_id or "unknown"
        return base if str(base).startswith("msg_") else f"msg_{base}"

    # -- main entry -----------------------------------------------------------

    def consume(self, name: str, payload: Dict[str, Any]) -> Iterator[NormalizedEvent]:
        """Translate one Responses SSE event into zero or more normalized events."""
        payload = payload or {}

        if name in ("response.created", "response.in_progress"):
            response = payload.get("response", {}) or {}
            if not self._started:
                self._response_id = response.get("id") or self._response_id
                self._model = response.get("model") or self._model
                self._started = True
                yield MessageStart(message_id=self._message_id(), model=self._model)
            return

        if name == "response.output_item.added":
            item = payload.get("item", {}) or {}
            item_id = item.get("id", "")
            item_type = item.get("type", "")
            self._record_item(item_id, item_type)
            if item_type == "function_call":
                self._has_function_call = True
                yield ToolCallStart(
                    item_id=item_id,
                    call_id=item.get("call_id", ""),
                    name=item.get("name", ""),
                )
            # message / reasoning: block opens implicitly on first delta.
            return

        if name == "response.output_text.delta":
            yield TextDelta(item_id=payload.get("item_id", ""), text=payload.get("delta", ""))
            return

        if name == "response.function_call_arguments.delta":
            # Ignore the obfuscation padding field entirely (surprise #10).
            yield ToolCallArgsDelta(
                item_id=payload.get("item_id", ""), delta=payload.get("delta", "")
            )
            return

        if name == "response.function_call_arguments.done":
            yield ToolCallEnd(
                item_id=payload.get("item_id", ""), arguments=payload.get("arguments", "")
            )
            return

        if name == "response.reasoning_summary_text.delta":
            yield ThinkingDelta(
                item_id=payload.get("item_id", ""), text=payload.get("delta", "")
            )
            return

        if name == "response.output_item.done":
            # AUTHORITATIVE final item. Everything downstream of a completed turn
            # is rebuilt from these, never from response.completed.output.
            item = payload.get("item", {}) or {}
            item_id = item.get("id", "")
            item_type = item.get("type", "")
            self._record_item(item_id, item_type)
            self._final_items[item_id] = item
            if item_type == "function_call":
                self._has_function_call = True
            if item_type == "reasoning":
                signature = encode_reasoning_signature(
                    item.get("encrypted_content"),
                    item.get("summary", []),
                    item.get("content", []),
                )
                yield ThinkingEnd(item_id=item_id, signature=signature)
            return

        if name == "response.completed":
            response = payload.get("response", {}) or {}
            self._usage = _normalize_usage(response.get("usage"))
            self._stop_reason = _map_stop_reason(response, self._has_function_call)
            yield UsageUpdate(
                input_tokens=self._usage.input_tokens,
                output_tokens=self._usage.output_tokens,
                cache_read_input_tokens=self._usage.cache_read_input_tokens,
                cache_creation_input_tokens=self._usage.cache_creation_input_tokens,
                reasoning_tokens=self._usage.reasoning_tokens,
                total_tokens=self._usage.total_tokens,
            )
            yield MessageEnd(stop_reason=self._stop_reason)
            return

        # Everything else (content_part.added/done, reasoning_summary_part.*,
        # in-progress skeletons, unknown future events) is not load-bearing for
        # translation and is intentionally ignored.
        return

    # -- final assembly (§D3b) ------------------------------------------------

    def final_response(self) -> NormalizedResponse:
        """Rebuild the whole turn from accumulated ``output_item.done`` items."""
        items: List[Any] = []
        for item_id in self._order:
            item = self._final_items.get(item_id)
            if item is None:
                continue
            item_type = item.get("type")
            if item_type == "message":
                text = "".join(
                    str(part.get("text", ""))
                    for part in item.get("content", []) or []
                    if isinstance(part, dict) and part.get("type") == "output_text"
                )
                items.append(FinalTextItem(text=text))
            elif item_type == "function_call":
                items.append(
                    FinalToolCallItem(
                        call_id=item.get("call_id", ""),
                        name=item.get("name", ""),
                        arguments=item.get("arguments", ""),
                    )
                )
            elif item_type == "reasoning":
                items.append(
                    FinalReasoningItem(
                        encrypted_content=item.get("encrypted_content"),
                        summary=item.get("summary", []) or [],
                        content=item.get("content", []) or [],
                        signature=encode_reasoning_signature(
                            item.get("encrypted_content"),
                            item.get("summary", []),
                            item.get("content", []),
                        ),
                    )
                )
        return NormalizedResponse(
            items=items,
            usage=self._usage,
            stop_reason=self._stop_reason,
            model=self._model,
            message_id=self._message_id() if self._started else None,
        )


def _normalize_usage(usage: Optional[Dict[str, Any]]) -> NormalizedUsage:
    """Map the Responses usage object to Anthropic-shaped fields (§D3c / E5)."""
    usage = usage or {}
    input_details = usage.get("input_tokens_details", {}) or {}
    output_details = usage.get("output_tokens_details", {}) or {}
    return NormalizedUsage(
        input_tokens=int(usage.get("input_tokens", 0) or 0),
        output_tokens=int(usage.get("output_tokens", 0) or 0),
        cache_read_input_tokens=int(input_details.get("cached_tokens", 0) or 0),
        cache_creation_input_tokens=int(input_details.get("cache_write_tokens", 0) or 0),
        # Subset of output_tokens — carried for internal accounting, NOT added.
        reasoning_tokens=int(output_details.get("reasoning_tokens", 0) or 0),
        total_tokens=int(usage.get("total_tokens", 0) or 0),
    )


def _map_stop_reason(response: Dict[str, Any], has_function_call: bool) -> str:
    """Responses stop conditions → Anthropic stop_reason (§D3e)."""
    if has_function_call:
        return "tool_use"
    # Truncation: the backend signals an incomplete response with a reason.
    if response.get("status") == "incomplete":
        reason = (response.get("incomplete_details", {}) or {}).get("reason")
        if reason in ("max_output_tokens", "max_tokens"):
            return "max_tokens"
    return "end_turn"


# ─── Layer 2: normalized events → Anthropic SSE frames ───────────────────────

class AnthropicSSESerializer:
    """Render normalized events into Anthropic SSE frames (dicts).

    Manages the Anthropic content-block index space and block open/close
    bookkeeping. Text and thinking blocks open lazily on their first delta and
    close on transition to a different item or at message end; tool blocks are
    opened/closed by explicit ToolCallStart/End.
    """

    def __init__(self, model: Optional[str] = None) -> None:
        self._fallback_model = model
        self._message_id = "msg_unknown"
        self._model: Optional[str] = model
        self._block_index = -1
        # currently open block: {"kind", "item_id", "index"}
        self._open: Optional[Dict[str, Any]] = None
        # per-item flag: did we emit any input_json_delta for this tool block?
        self._tool_emitted_args: Dict[str, bool] = {}
        self._usage = NormalizedUsage()

    # -- block bookkeeping ----------------------------------------------------

    def _close_open(self) -> List[Dict[str, Any]]:
        if self._open is None:
            return []
        idx = self._open["index"]
        self._open = None
        return [{"type": "content_block_stop", "index": idx}]

    def _open_block(self, kind: str, item_id: str, content_block: Dict[str, Any]) -> List[Dict[str, Any]]:
        self._block_index += 1
        self._open = {"kind": kind, "item_id": item_id, "index": self._block_index}
        return [
            {
                "type": "content_block_start",
                "index": self._block_index,
                "content_block": content_block,
            }
        ]

    def _ensure_text_like_block(self, kind: str, item_id: str) -> List[Dict[str, Any]]:
        """Open a text/thinking block if the current open block is not it."""
        if self._open and self._open["kind"] == kind and self._open["item_id"] == item_id:
            return []
        frames = self._close_open()
        if kind == "text":
            frames += self._open_block(kind, item_id, {"type": "text", "text": ""})
        else:  # thinking
            frames += self._open_block(kind, item_id, {"type": "thinking", "thinking": ""})
        return frames

    # -- main entry -----------------------------------------------------------

    def render(self, event: NormalizedEvent) -> List[Dict[str, Any]]:
        """Render one normalized event into zero or more Anthropic SSE frames."""
        if isinstance(event, MessageStart):
            self._message_id = event.message_id
            self._model = event.model or self._fallback_model
            return [
                {
                    "type": "message_start",
                    "message": {
                        "id": self._message_id,
                        "type": "message",
                        "role": event.role,
                        "model": self._model,
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        # input usage is not known until response.completed; the
                        # authoritative counts land in message_delta.usage.
                        "usage": {"input_tokens": 0, "output_tokens": 0},
                    },
                }
            ]

        if isinstance(event, TextDelta):
            frames = self._ensure_text_like_block("text", event.item_id)
            frames.append(
                {
                    "type": "content_block_delta",
                    "index": self._open["index"],
                    "delta": {"type": "text_delta", "text": event.text},
                }
            )
            return frames

        if isinstance(event, ThinkingDelta):
            frames = self._ensure_text_like_block("thinking", event.item_id)
            frames.append(
                {
                    "type": "content_block_delta",
                    "index": self._open["index"],
                    "delta": {"type": "thinking_delta", "thinking": event.text},
                }
            )
            return frames

        if isinstance(event, ThinkingEnd):
            # Ensure a thinking block exists (in case there were no summary
            # deltas), emit the round-trip signature, then close.
            frames = self._ensure_text_like_block("thinking", event.item_id)
            frames.append(
                {
                    "type": "content_block_delta",
                    "index": self._open["index"],
                    "delta": {"type": "signature_delta", "signature": event.signature},
                }
            )
            frames += self._close_open()
            return frames

        if isinstance(event, ToolCallStart):
            frames = self._close_open()
            frames += self._open_block(
                "tool_use",
                event.item_id,
                {"type": "tool_use", "id": event.call_id, "name": event.name, "input": {}},
            )
            self._tool_emitted_args[event.item_id] = False
            return frames

        if isinstance(event, ToolCallArgsDelta):
            # Byte-exact re-emit of the raw fragment — the critical fidelity path.
            self._tool_emitted_args[event.item_id] = True
            return [
                {
                    "type": "content_block_delta",
                    "index": self._open["index"] if self._open else self._block_index,
                    "delta": {"type": "input_json_delta", "partial_json": event.delta},
                }
            ]

        if isinstance(event, ToolCallEnd):
            frames: List[Dict[str, Any]] = []
            # If the backend emitted no argument deltas (e.g. empty-args call),
            # emit the authoritative arguments as a single input_json_delta so the
            # tool_use block's input is never empty when it should not be.
            if not self._tool_emitted_args.get(event.item_id) and event.arguments:
                frames.append(
                    {
                        "type": "content_block_delta",
                        "index": self._open["index"] if self._open else self._block_index,
                        "delta": {"type": "input_json_delta", "partial_json": event.arguments},
                    }
                )
            frames += self._close_open()
            return frames

        if isinstance(event, UsageUpdate):
            self._usage = NormalizedUsage(
                input_tokens=event.input_tokens,
                output_tokens=event.output_tokens,
                cache_read_input_tokens=event.cache_read_input_tokens,
                cache_creation_input_tokens=event.cache_creation_input_tokens,
                reasoning_tokens=event.reasoning_tokens,
                total_tokens=event.total_tokens,
            )
            return []

        if isinstance(event, MessageEnd):
            frames = self._close_open()
            frames.append(
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": event.stop_reason, "stop_sequence": None},
                    # Anthropic usage fields only. reasoning_tokens is a subset of
                    # output_tokens and is deliberately NOT surfaced here.
                    "usage": {
                        "input_tokens": self._usage.input_tokens,
                        "output_tokens": self._usage.output_tokens,
                        "cache_read_input_tokens": self._usage.cache_read_input_tokens,
                        "cache_creation_input_tokens": self._usage.cache_creation_input_tokens,
                    },
                }
            )
            frames.append({"type": "message_stop"})
            return frames

        return []


# ─── Combined incremental translator ─────────────────────────────────────────

class ResponsesToAnthropicTranslator:
    """Drop-in incremental translator: push Responses SSE events, get Anthropic
    SSE frames. Also exposes the buffered final assembly (§D3b)."""

    def __init__(self, *, request_model: Optional[str] = None) -> None:
        self._normalizer = ResponsesNormalizer()
        self._serializer = AnthropicSSESerializer(model=request_model)

    def push(self, name: str, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Feed one parsed Responses SSE event; return Anthropic SSE frames."""
        frames: List[Dict[str, Any]] = []
        for norm_event in self._normalizer.consume(name, payload):
            frames.extend(self._serializer.render(norm_event))
        return frames

    def final_response(self) -> NormalizedResponse:
        return self._normalizer.final_response()


def translate_responses_stream(
    events: Iterable[tuple],
) -> Iterator[Dict[str, Any]]:
    """Convenience generator: iterate ``(name, payload)`` Responses SSE events,
    yield Anthropic SSE frames in order. Deterministic, no I/O."""
    translator = ResponsesToAnthropicTranslator()
    for name, payload in events:
        for frame in translator.push(name, payload):
            yield frame
