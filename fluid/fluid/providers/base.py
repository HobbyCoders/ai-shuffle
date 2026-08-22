"""ProviderClient protocol and the normalized StreamChunk vocabulary.

All provider quirks live in each provider's ``serialize.py``; the engine only
ever sees StreamChunks. The chunk vocabulary mirrors the proven normalized
event set from AI-Shuffle's translator gateway, including the discipline that
tool-call argument fragments are accumulated byte-for-byte and only parsed
once the tool block closes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from fluid.messages import Message


@dataclass(frozen=True)
class ProviderCaps:
    explicit_cache_breakpoints: bool = False  # Anthropic cache_control
    max_cache_breakpoints: int = 0
    stateful_reasoning: bool = False  # OpenAI Responses previous_response_id
    signed_thinking: bool = False  # Anthropic
    parallel_tool_calls: bool = True
    server_tools: bool = False


@dataclass(frozen=True)
class StreamChunk:
    """Normalized streaming event.

    kind ∈ {message_start, block_start, text_delta, thinking_delta,
            tool_args_delta, block_stop, usage, message_stop, rate_limit}
    """

    kind: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderRequest:
    model: str
    messages: list[Message]
    system: str | list[dict[str, Any]] | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    cache_breakpoints: int = 0  # how many trailing breakpoints the serializer may place
    max_tokens: int = 8192
    thinking: dict[str, Any] | None = None
    # Reasoning-depth knob ("low" | "medium" | "high" | "xhigh" | "max").
    # Each serializer maps it to its provider's shape: Anthropic
    # output_config.effort (verbatim), OpenAI-shaped APIs reasoning.effort
    # (clamped to their low/medium/high vocabulary).
    effort: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class ProviderClient(Protocol):
    # Optional cost hook (NOT part of the protocol — implementing it is opt-in):
    # a provider MAY expose ``estimate_cost(model, usage) -> float | None``. The
    # engine discovers it via ``getattr`` and calls it once per model call, only
    # when that call's usage dict carries no provider-reported ``"cost_usd"``.
    # Return ``None`` for models with no known price (never guess).
    name: str
    caps: ProviderCaps

    def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]: ...
