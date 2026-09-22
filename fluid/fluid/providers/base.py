"""ProviderClient protocol and the normalized StreamChunk vocabulary.

All provider quirks live in each provider's ``serialize.py``; the engine only
ever sees StreamChunks. The chunk vocabulary mirrors the proven normalized
event set from a production translator gateway, including the discipline that
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

    kind ∈ {provider_request, message_start, block_start, text_delta, thinking_delta,
            tool_args_delta, block_stop, usage, message_stop, rate_limit,
            image_generation, image_generation_progress}
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
    # None = no explicit ceiling: the model's own maximum. Every dialect
    # but Anthropic expresses that by omitting the field; Anthropic
    # requires a number, which its serializer resolves per model.
    max_tokens: int | None = None
    thinking: dict[str, Any] | None = None
    # Model-specific reasoning effort, validated by the caller. Serializers
    # preserve it in output_config.effort (Anthropic) or reasoning.effort
    # (OpenAI-shaped APIs). Literal "none" disables reasoning; None omits it.
    effort: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class ProviderClient(Protocol):
    # Optional cost hook (NOT part of the protocol — implementing it is opt-in):
    # a provider MAY expose ``estimate_cost(model, usage) -> float | None``. The
    # engine discovers it via ``getattr`` and calls it once per model call, only
    # when that call's usage dict carries no provider-reported ``"cost_usd"``.
    # Return ``None`` for models with no known price (never guess) — the engine
    # then falls through to ``fluid.providers.pricing``, which consults the
    # host's pricing resolver and fluid's own cross-provider table.
    name: str
    caps: ProviderCaps

    def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]: ...
