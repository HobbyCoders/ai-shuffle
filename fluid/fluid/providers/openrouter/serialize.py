"""OpenRouter (OpenAI-compatible chat/completions) serialization.

The provider-neutral chat/completions machinery (message translation, tool
translation, usage normalization, the SSE normalizer) lives in
``fluid.providers._chat_completions``; this module adds the OpenRouter
flavor on top of it:

* ``usage: {include: true}`` — OpenRouter's usage accounting; adds ``cost``
  (USD credits) to the terminal chunk's usage object.
* Cache breakpoints: OpenRouter forwards Anthropic-style ``cache_control``
  markers placed on text content parts. Placement mirrors the native Anthropic
  serializer's trailing-stable-position strategy (system tail, newest history
  block), gated to models that honor the markers (``anthropic/`` prefix) —
  other models ignore or reject the field, so the breakpoint budget is dropped
  for them.
* ``reasoning: {effort}`` from the harness effort knob, passed through when
  it is one of none/minimal/low/medium/high/xhigh/max and dropped otherwise.
"""

from __future__ import annotations

from typing import Any

from fluid.providers._chat_completions import (
    ChatCompletionsNormalizer,
    system_message,
    translate_message,
    translate_tools,
)
from fluid.providers.base import ProviderRequest

__all__ = ["ChatCompletionsNormalizer", "build_request_body"]


# Model families that honor Anthropic-style ``cache_control`` breakpoints when
# routed through OpenRouter's chat/completions endpoint.
#
# Audited 2026-08 against OpenRouter's prompt-caching docs, after glm sessions
# showed ``cache_creation_input_tokens`` pinned at 0 on every call:
#
# * Z.AI/GLM, OpenAI, DeepSeek, Grok, Moonshot and Groq cache AUTOMATICALLY —
#   markers are unnecessary there, and the zero cache-write count is not a
#   harness bug: OpenRouter returns ``prompt_tokens_details.cache_write_tokens``
#   only for models with explicit caching AND cache-write pricing, so an
#   automatic-caching leg reports reads (``cached_tokens``) and nothing else.
# * Alibaba Qwen and Google Gemini DO honor explicit markers. Qwen is a clean
#   candidate for this list; Gemini is deliberately left off, because its
#   explicit cache carries a write premium and a short TTL while its implicit
#   cache is free — adding a marker there can cost more than it saves. Neither
#   is added blind: both want a live cost comparison first.
_CACHE_CONTROL_MODEL_PREFIXES = ("anthropic/",)


def _supports_cache_control(model: str) -> bool:
    return model.startswith(_CACHE_CONTROL_MODEL_PREFIXES)


def _mark_trailing_breakpoint(messages: list[dict[str, Any]]) -> None:
    """Attach ``cache_control`` to the newest markable content — the same
    last-history breakpoint the native Anthropic serializer places.

    chat/completions carries ``cache_control`` only on text content parts, so
    string content is upgraded to a one-part list; assistant ``tool_calls``
    arrays and image parts can't hold the marker, so the walk skips past them.
    """
    for msg in reversed(messages):
        if msg["role"] == "system":
            return  # nothing markable after system; its tail has its own marker
        content = msg.get("content")
        if isinstance(content, str) and content:
            msg["content"] = [
                {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
            ]
            return
        if isinstance(content, list):
            for part in reversed(content):
                if part.get("type") == "text":
                    part["cache_control"] = {"type": "ephemeral"}
                    return


def build_request_body(
    request: ProviderRequest, *, allow_fallbacks: bool = False
) -> dict[str, Any]:
    """Serialize ``request``; ``allow_fallbacks`` is the client's standing knob."""
    breakpoints = (
        request.cache_breakpoints if _supports_cache_control(request.model) else 0
    )
    messages: list[dict[str, Any]] = []
    system = system_message(request.system)
    if system is not None:
        if breakpoints > 0 and system["content"]:
            # System tail is the most stable prefix position. OpenRouter's
            # Anthropic transform orders tool defs before system, so this one
            # marker also covers the tool schemas (chat/completions has no
            # tool-defs cache slot of its own).
            system["content"] = [
                {
                    "type": "text",
                    "text": system["content"],
                    "cache_control": {"type": "ephemeral"},
                }
            ]
            breakpoints -= 1
        messages.append(system)
    for msg in request.messages:
        messages.extend(translate_message(msg))
    if breakpoints > 0:
        _mark_trailing_breakpoint(messages)

    body: dict[str, Any] = {
        "model": request.model,
        "messages": messages,
        "stream": True,
        # Ask for a usage object on the terminal chunk (finalize hook).
        "stream_options": {"include_usage": True},
        # OpenRouter usage accounting: adds `cost` (USD credits) to the
        # terminal chunk's usage object alongside the token counts.
        "usage": {"include": True},
        # OpenRouter otherwise retries another upstream provider invisibly
        # before the first token. The harness owns retry visibility/budgets, so
        # keep one logical call bound to one upstream generation. Embedders that
        # would rather have the silent failover set allow_fallbacks on the client.
        "provider": {"allow_fallbacks": allow_fallbacks},
    }
    # Omitted when uncapped: OpenRouter then applies the model's own output
    # maximum. Sending a fixed ceiling truncated any tool call whose arguments
    # carried a large payload — a Write of a file bigger than the cap lost its
    # content mid-JSON and the call could not be run.
    if request.max_tokens is not None:
        body["max_tokens"] = request.max_tokens
    if request.tools:
        body["tools"] = translate_tools(request.tools)
    effort = request.effort
    if effort is None and request.thinking:
        effort = request.thinking.get("effort")
    if effort in ("none", "minimal", "low", "medium", "high", "xhigh", "max"):
        body["reasoning"] = {"effort": effort}
    body.update(request.extra)
    return body
