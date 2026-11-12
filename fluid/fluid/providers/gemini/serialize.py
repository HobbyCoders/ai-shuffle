"""Gemini Interactions API serialization (API-key leg, stateless).

Surface: ``POST /v1beta/interactions`` with ``store: false`` and
``stream: true``. Google's Interactions overview calls it "the recommended
interface for all new projects" (GA June 2026) and ``generateContent``
"legacy"; ``store: false`` is its documented stateless mode, where the client
sends the whole history every call. fluid owns history, so every request
carries every turn and no ``previous_interaction_id`` is ever sent.

Request side: normalized ``Message``s → an ``input`` list of steps:

  * user text and images → one ``user_input`` step per run of content,
  * tool results → ``function_result`` steps (``call_id``, ``name``,
    ``result``, ``is_error``),
  * assistant text → ``model_output`` steps, tool calls → ``function_call``
    steps, and the model's ``thought`` steps are resent VERBATIM from their
    ``Reasoning`` envelope (origin ``gemini``). Google: "You MUST always resend
    all thought blocks exactly as they were received"; a function call whose
    thought signature is missing is refused. Reasoning from any other
    provider is dropped. Within Gemini the envelope is resent across model
    switches too ("The backend manages compatibility").
  * a history that ends in a model turn is cut back to its last user step:
    recent Gemini models refuse a request that ends in a model turn.

``temperature``, ``top_p``, ``top_k`` and ``candidate_count`` are never sent
(ignored with a deprecation warning on Gemini 3.6+ and 4, a 400 "in a future
release"; candidate count is unsupported on Gemini 3 and 4), and an ``extra``
passthrough cannot reintroduce them.

Response side: the step-based SSE stream (``interaction.created``,
``step.start`` / ``step.delta`` / ``step.stop``, ``interaction.completed``,
``error``) → the normalized ``StreamChunk`` vocabulary. Each ``thought`` step
is reassembled (summary text plus its ``signature``, which arrives as the last
delta before ``step.stop``) and handed to the engine on its ``block_stop`` as
``payload["block"]``, which becomes the round-tripped ``Reasoning.raw``.

Usage: ``total_output_tokens`` excludes thinking, which Google bills as
output ("response pricing is the sum of output tokens and thinking tokens"),
so ``output_tokens`` = output + thought and ``reasoning_tokens`` reports the
thought subset. ``total_cached_tokens`` (implicit caching) is reported as
``cache_read_input_tokens`` and subtracted from ``total_input_tokens``, which
counts the whole prompt.
"""

from __future__ import annotations

import json
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
from fluid.providers._retry import (
    BILLING_STATUS,
    coerce_status,
    is_retriable_status,
)
from fluid.providers._transport import (
    billing_summary,
    provider_error_summary,
    usage_limit_summary,
)
from fluid.providers.base import (
    INTERNAL_EXTRA_PREFIX,
    ErrorClass,
    ProviderRequest,
    StreamChunk,
    flatten_system,
)
from fluid.tools.images import condition_image

ORIGIN = "gemini"

#: Sampling knobs recent Gemini models reject or ignore (see module docstring).
FORBIDDEN_FIELDS = frozenset((
    "temperature", "top_p", "top_k", "candidate_count",
    "topP", "topK", "candidateCount",
))

#: ``thinking_level`` values per model, from Google's thinking guide (table
#: "Controlling thinking", 2026-11-11) and the OpenAI-compatibility mapping
#: table for 3.1 Flash-Lite. ``gemini-3.8-flash``'s model page: "``minimal``
#: is not supported and returns an error."
THINKING_LEVELS: dict[str, tuple[str, ...]] = {
    "gemini-4-carbon": ("low", "medium", "high"),
    "gemini-4-argon": ("low", "medium", "high"),
    "gemini-3.8-flash": ("low", "medium", "high"),
    "gemini-3.6-flash": ("minimal", "low", "medium", "high"),
    "gemini-3.5-flash-lite": ("minimal", "low", "medium", "high"),
    "gemini-3.1-flash-lite": ("minimal", "low", "medium", "high"),
    "gemini-3.1-pro-preview": ("low", "medium", "high"),
    "gemini-3-flash-preview": ("minimal", "low", "medium", "high"),
    "gemini-3-pro-preview": ("low", "high"),
}
#: The level a model thinks at when none is sent (same table).
DEFAULT_LEVELS: dict[str, str] = {
    "gemini-4-carbon": "medium",
    "gemini-4-argon": "medium",
    "gemini-3.8-flash": "medium",
    "gemini-3.6-flash": "medium",
    "gemini-3.5-flash-lite": "minimal",
    "gemini-3.1-pro-preview": "high",
    "gemini-3-flash-preview": "high",
}
_ALL_LEVELS = ("minimal", "low", "medium", "high")

_EFFORT_LOW_CEIL = 4096
_EFFORT_MEDIUM_CEIL = 16384


def effort_levels(model: str) -> tuple[str, ...] | None:
    """The documented ``thinking_level`` values for ``model``, or None."""
    return THINKING_LEVELS.get((model or "").strip().lower())


def default_level(model: str) -> str | None:
    """The documented default ``thinking_level`` for ``model``, or None."""
    return DEFAULT_LEVELS.get((model or "").strip().lower())


def thinking_level(model: str, effort: str | None, thinking: dict[str, Any] | None = None) -> str | None:
    """The ``thinking_level`` for one call, or None to keep the model default.

    An effort the model supports passes unchanged. Above ``high`` (xhigh, max,
    ultra) clamps to ``high``; below the model's lowest level clamps up to it
    (Google's own mapping sends ``minimal`` to ``low`` on Gemini 4). Reasoning
    cannot be turned off on Gemini 3 or 4, so ``none`` means the lowest level.
    """
    if effort is None and thinking:
        effort = thinking.get("effort") if isinstance(thinking.get("effort"), str) else None
        if effort is None:
            budget = thinking.get("budget_tokens")
            if isinstance(budget, int) and not isinstance(budget, bool):
                effort = (
                    "low" if budget < _EFFORT_LOW_CEIL
                    else "medium" if budget < _EFFORT_MEDIUM_CEIL else "high"
                )
    if not isinstance(effort, str) or not effort:
        return None
    effort = effort.strip().lower()
    levels = effort_levels(model)
    if effort in ("xhigh", "max", "ultra"):
        effort = "high"
    elif effort == "none":
        effort = "minimal"
    if levels is None:
        # An uncurated model: send only a level the API defines, never invent.
        return effort if effort in _ALL_LEVELS else None
    if effort in levels:
        return effort
    if effort not in _ALL_LEVELS:
        return None
    rank = _ALL_LEVELS.index(effort)
    for level in levels:
        if _ALL_LEVELS.index(level) >= rank:
            return level
    return levels[-1]


# ─── request: Message[] → Interactions steps ─────────────────────────────────


def _image_content(block: Image) -> dict[str, Any]:
    if not block.data.strip():
        return {"type": "text", "text": "[image omitted: empty data]"}
    media_type, data = condition_image(block.media_type, block.data)
    return {"type": "image", "data": data, "mime_type": media_type}


def _tool_result(block: ToolResultBlock) -> str | list[dict[str, Any]]:
    """A ``function_result.result``: a string, or text/image items with images."""
    if not any(isinstance(c, Image) for c in block.content):
        parts: list[str] = []
        for c in block.content:
            if isinstance(c, Text):
                parts.append(c.text)
            elif isinstance(c, UnknownBlock):
                continue
            else:
                parts.append(json.dumps(c.to_dict(), separators=(",", ":"), ensure_ascii=False))
        return "".join(parts)
    out: list[dict[str, Any]] = []
    for c in block.content:
        if isinstance(c, Text):
            out.append({"type": "text", "text": c.text})
        elif isinstance(c, Image):
            out.append(_image_content(c))
    return out


def _reasoning_step(block: Reasoning) -> dict[str, Any] | None:
    """The Gemini step a Reasoning envelope carries, or None when foreign."""
    if block.origin != ORIGIN:
        return None
    if isinstance(block.raw, dict) and block.raw.get("type"):
        return block.raw  # verbatim: the signature must round-trip unchanged
    if block.signature:
        step: dict[str, Any] = {"type": "thought", "signature": block.signature}
        if block.summary_text:
            step["summary"] = [{"type": "text", "text": block.summary_text}]
        return step
    return None


_MODEL_STEPS = frozenset(("model_output", "thought", "function_call"))


def build_input(messages: list[Message]) -> list[dict[str, Any]]:
    """History → the ``input`` step list for a stateless call."""
    names: dict[str, str] = {}
    steps: list[dict[str, Any]] = []

    def user_content(content: list[dict[str, Any]]) -> None:
        if not content:
            return
        if steps and steps[-1].get("type") == "user_input":
            steps[-1]["content"].extend(content)
        else:
            steps.append({"type": "user_input", "content": list(content)})

    for msg in messages:
        if msg.role == "assistant":
            text: list[dict[str, Any]] = []

            def flush_text(text: list[dict[str, Any]] = text) -> None:
                if text:
                    steps.append({"type": "model_output", "content": list(text)})
                    text.clear()

            for block in msg.content:
                if isinstance(block, Text):
                    if block.text:
                        text.append({"type": "text", "text": block.text})
                elif isinstance(block, Thinking):
                    continue  # carried by its Reasoning envelope
                elif isinstance(block, Reasoning):
                    step = _reasoning_step(block)
                    if step is not None:
                        flush_text()
                        steps.append(step)
                elif isinstance(block, ToolUseBlock):
                    flush_text()
                    names[block.id] = block.name
                    steps.append({
                        "type": "function_call", "id": block.id,
                        "name": block.name, "arguments": block.input or {},
                    })
            flush_text()
            continue
        # user and system turns: Gemini has one system_instruction per call, so
        # an in-history system message reaches the model as user content.
        pending: list[dict[str, Any]] = []
        for block in msg.content:
            if isinstance(block, Text):
                if block.text:
                    pending.append({"type": "text", "text": block.text})
            elif isinstance(block, Image):
                pending.append(_image_content(block))
            elif isinstance(block, ToolResultBlock):
                user_content(pending)
                pending = []
                step = {
                    "type": "function_result", "call_id": block.tool_use_id,
                    "result": _tool_result(block),
                }
                name = names.get(block.tool_use_id)
                if name:
                    step["name"] = name
                if block.is_error:
                    step["is_error"] = True
                steps.append(step)
        user_content(pending)

    # Recent models refuse a request that ends in a model turn.
    while steps and steps[-1].get("type") in _MODEL_STEPS:
        steps.pop()
    return steps


def _translate_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for tool in tools:
        name = tool.get("name")
        if not name:
            continue  # provider-native tools of other dialects have no name
        out.append({
            "type": "function",
            "name": name,
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
        })
    return out


API_REVISION = "2026-05-20"


def build_request_body(request: ProviderRequest) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": request.model,
        "input": build_input(request.messages),
        # Stateless invariants: fluid owns history, nothing is stored.
        "store": False,
        "stream": True,
    }
    system = flatten_system(request.system)
    if system:
        body["system_instruction"] = system
    if request.tools:
        tools = _translate_tools(request.tools)
        if tools:
            body["tools"] = tools
    config: dict[str, Any] = {"thinking_summaries": "auto"}
    level = thinking_level(request.model, request.effort, request.thinking)
    if level is not None:
        config["thinking_level"] = level
    if request.max_tokens is not None:
        config["max_output_tokens"] = request.max_tokens
    body["generation_config"] = config
    for key, value in request.extra.items():
        if key.startswith(INTERNAL_EXTRA_PREFIX) or key in FORBIDDEN_FIELDS:
            continue
        if key == "generation_config" and isinstance(value, dict):
            config.update({k: v for k, v in value.items() if k not in FORBIDDEN_FIELDS})
            continue
        body[key] = value
    return body


# ─── errors ──────────────────────────────────────────────────────────────────

# Interactions API error codes (docs/api-errors, 2026-11-11). Retried: the
# documented transient ones ("Retry on specific errors: ... 429, 408, or 5xx").
_CODE_STATUS = {
    "invalid_request": 400, "failed_precondition": 400, "parameter_unknown": 400,
    "authentication": 401, "payment_required": 402, "permission_denied": 403,
    "not_found": 404, "model_not_found": 404, "out_of_range": 416,
    "rate_limit_exceeded": 429, "quota_exceeded": 429, "too_many_requests": 429,
    "api_error": 500, "unimplemented": 501, "service_unavailable": 503,
    "deadline_exceeded": 504, "gateway_timeout": 504,
}
_RETRIABLE_CODES = frozenset((
    "rate_limit_exceeded", "too_many_requests", "api_error",
    "service_unavailable", "deadline_exceeded", "gateway_timeout",
))
#: "You have exceeded your daily quota. Wait until the quota resets": a spent
#: allowance, not a blip, so never retried.
QUOTA_CODE = "quota_exceeded"


def error_fields(err: Any) -> tuple[str | None, int | None, str]:
    """``(code, status, message)`` of an Interactions or legacy Google error."""
    if isinstance(err, dict) and isinstance(err.get("error"), dict):
        err = err["error"]
    if not isinstance(err, dict):
        return None, None, str(err or "")
    raw_code = err.get("code")
    message = str(err.get("message") or "")
    if isinstance(raw_code, str) and not raw_code.isdigit():
        code = raw_code
        status = _CODE_STATUS.get(code)
    else:
        # Legacy google.rpc shape: {"code": 429, "status": "RESOURCE_EXHAUSTED"}
        status = coerce_status(raw_code)
        legacy = err.get("status")
        code = legacy.lower() if isinstance(legacy, str) else None
    return code, status, message


def classify_error(err: Any) -> ErrorClass:
    code, status, _message = error_fields(err)
    if code == QUOTA_CODE:
        return ErrorClass(retriable=False, code=code)
    retriable = code in _RETRIABLE_CODES or (
        code not in _CODE_STATUS and is_retriable_status(status)
    )
    return ErrorClass(retriable=retriable, code=code)


def error_payload(err: Any) -> dict[str, Any]:
    """The ``error`` chunk payload for an in-stream or HTTP Gemini error."""
    code, status, message = error_fields(err)
    verdict = classify_error(err)
    summary = provider_error_summary({"error": {"message": message}} if message else err)
    payload: dict[str, Any] = {
        "message": message or str(err),
        "retriable": verdict.retriable,
    }
    if code is not None:
        payload["code"] = code
    if code == QUOTA_CODE:
        payload["usage_limit"] = True
        summary = usage_limit_summary(summary)
    elif status == BILLING_STATUS:
        summary = billing_summary(code, summary)
    if status is not None:
        payload["status"] = status
    payload["provider_message"] = summary
    return payload


# ─── response: Interactions SSE → StreamChunks ───────────────────────────────


def normalize_usage(usage: dict[str, Any] | None) -> dict[str, int]:
    usage = usage or {}

    def count(key: str) -> int:
        value = usage.get(key, 0) or 0
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0

    cached = count("total_cached_tokens")
    thought = count("total_thought_tokens")
    out = {
        "input_tokens": max(count("total_input_tokens") - cached, 0),
        "output_tokens": count("total_output_tokens") + thought,
        "cache_read_input_tokens": cached,
        "cache_creation_input_tokens": 0,
        "reasoning_tokens": thought,
    }
    if count("total_tokens"):
        out["total_tokens"] = count("total_tokens")
    if count("total_tool_use_tokens"):
        out["tool_use_tokens"] = count("total_tool_use_tokens")
    return out


_BLOCK_TYPE = {"model_output": "text", "thought": "thinking", "function_call": "tool_use"}


class StreamNormalizer:
    """Stateful translator for one Interactions stream."""

    def __init__(self) -> None:
        self._open: dict[int, dict[str, Any]] = {}  # index → step being built
        self._has_function_call = False
        self._served_model: str | None = None
        self.terminal = False

    def _start(self, index: int, step: dict[str, Any]) -> list[StreamChunk]:
        stype = step.get("type")
        block_type = _BLOCK_TYPE.get(stype)
        if block_type is None:
            # A server-side tool step: none is requested, so nothing to keep.
            return [StreamChunk("provider_alive", {"signal": f"step:{stype}"})]
        state: dict[str, Any] = {"type": stype, "summary": "", "signature": None}
        payload: dict[str, Any] = {"index": index, "block_type": block_type}
        out: list[StreamChunk] = []
        if stype == "function_call":
            self._has_function_call = True
            payload["tool_use_id"] = step.get("id") or None
            payload["tool_name"] = step.get("name")
            out.append(StreamChunk("block_start", payload))
            args = step.get("arguments")
            if isinstance(args, dict) and args:
                out.append(StreamChunk("tool_args_delta", {
                    "index": index,
                    "partial_json": json.dumps(args, separators=(",", ":"), ensure_ascii=False),
                }))
        else:
            out.append(StreamChunk("block_start", payload))
            if stype == "thought":
                if isinstance(step.get("signature"), str) and step["signature"]:
                    state["signature"] = step["signature"]
                for item in step.get("summary") or []:
                    if isinstance(item, dict) and isinstance(item.get("text"), str):
                        state["summary"] += item["text"]
                        out.append(StreamChunk("thinking_delta", {"index": index, "text": item["text"]}))
            elif stype == "model_output":
                for item in step.get("content") or []:
                    if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
                        out.append(StreamChunk("text_delta", {"index": index, "text": item["text"]}))
        self._open[index] = state
        return out

    def _delta(self, index: int, delta: dict[str, Any]) -> list[StreamChunk]:
        state = self._open.get(index)
        if state is None:
            return []
        dtype = delta.get("type")
        if dtype == "text" and isinstance(delta.get("text"), str):
            return [StreamChunk("text_delta", {"index": index, "text": delta["text"]})]
        if dtype == "thought_summary":
            content = delta.get("content") or {}
            text = content.get("text") if isinstance(content, dict) else None
            if isinstance(text, str) and text:
                state["summary"] += text
                return [StreamChunk("thinking_delta", {"index": index, "text": text})]
            return []
        if dtype == "thought_signature" and isinstance(delta.get("signature"), str):
            state["signature"] = (state["signature"] or "") + delta["signature"]
            return [StreamChunk("signature_delta", {"index": index, "signature": delta["signature"]})]
        if dtype == "arguments_delta" and isinstance(delta.get("arguments"), str):
            return [StreamChunk("tool_args_delta", {"index": index, "partial_json": delta["arguments"]})]
        return [StreamChunk("provider_alive", {"signal": f"delta:{dtype}"})]

    def _stop(self, index: int) -> list[StreamChunk]:
        state = self._open.pop(index, None)
        if state is None:
            return []
        payload: dict[str, Any] = {"index": index}
        if state["type"] == "thought":
            step: dict[str, Any] = {"type": "thought"}
            if state["signature"] is not None:
                step["signature"] = state["signature"]
            if state["summary"]:
                step["summary"] = [{"type": "text", "text": state["summary"]}]
            payload["block"] = step
        return [StreamChunk("block_stop", payload)]

    def _close_all(self) -> list[StreamChunk]:
        out: list[StreamChunk] = []
        for index in sorted(self._open):
            out.extend(self._stop(index))
        return out

    def consume(self, name: str, data: dict[str, Any]) -> list[StreamChunk]:
        event = data.get("event_type") or name
        if event == "interaction.created":
            interaction = data.get("interaction") or {}
            model = interaction.get("model")
            if isinstance(model, str) and model:
                self._served_model = model
            return [StreamChunk("message_start", {"usage": {}, "message": {
                "id": interaction.get("id"), "model": model,
            }})]
        if event == "interaction.status_update":
            return [StreamChunk("provider_alive", {"signal": "status_update"})]
        index = data.get("index")
        if event == "step.start" and isinstance(index, int):
            return self._start(index, data.get("step") or {})
        if event == "step.delta" and isinstance(index, int):
            return self._delta(index, data.get("delta") or {})
        if event == "step.stop" and isinstance(index, int):
            return self._stop(index)
        if event == "interaction.completed":
            return self._completed(data.get("interaction") or {})
        if event == "error":
            self.terminal = True
            return [*self._close_all(), StreamChunk("error", error_payload(data.get("error") or data))]
        return []

    def _completed(self, interaction: dict[str, Any]) -> list[StreamChunk]:
        out = self._close_all()
        self.terminal = True
        status = interaction.get("status")
        if status in ("failed", "cancelled"):
            err = interaction.get("error") or {"code": status, "message": f"The interaction {status}."}
            out.append(StreamChunk("error", error_payload(err)))
            return out
        if status == "incomplete":
            stop_reason = "max_tokens"
        elif self._has_function_call or status == "requires_action":
            stop_reason = "tool_use"
        else:
            stop_reason = "end_turn"
        payload: dict[str, Any] = {
            "usage": normalize_usage(interaction.get("usage")),
            "stop_reason": stop_reason,
        }
        if status == "incomplete":
            payload["native_stop_reason"] = "incomplete"
        served = interaction.get("model")
        if isinstance(served, str) and served:
            payload["served_model"] = served
        out.append(StreamChunk("usage", payload))
        out.append(StreamChunk("message_stop", {}))
        return out
