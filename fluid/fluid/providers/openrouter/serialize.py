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
* ``reasoning: {effort}`` from the harness effort knob, clamped to
  OpenRouter's low/medium/high vocabulary.
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


def build_request_body(request: ProviderRequest) -> dict[str, Any]:
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
        "max_tokens": request.max_tokens,
        "stream": True,
        # Ask for a usage object on the terminal chunk (finalize hook).
        "stream_options": {"include_usage": True},
        # OpenRouter usage accounting: adds `cost` (USD credits) to the
        # terminal chunk's usage object alongside the token counts.
        "usage": {"include": True},
    }
    if request.tools:
        body["tools"] = translate_tools(request.tools)
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
