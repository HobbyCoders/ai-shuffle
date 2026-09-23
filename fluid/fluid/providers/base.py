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
            image_generation, image_generation_progress, provider_alive}

    ``provider_alive`` (payload ``{"signal": ...}``) is a content-free sign
    that the accepted response is still streaming, for provider frames that
    otherwise produce no chunk: a reasoning item opening, an SSE keepalive.
    Consumers that do not care ignore it.
    """

    kind: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class WireState:
    """Request identity and in-turn routing state for one loop and one role.

    Owned by the caller (a ``NativeLoop`` keeps one per session and role) and
    handed to the client on each ``ProviderRequest``. A client may be shared
    by several roles (the main loop and the memory learner use the same
    Responses client), so the client never keeps this state itself: a
    learner call would overwrite the main loop's routing token.

    ``turn_marker`` is the turn the state belongs to. ``turn_state`` is an
    opaque routing token the provider returned on the first response of that
    turn; it is replayed on later requests of the same turn and cleared when
    the marker changes.
    """

    session_id: str
    role: str = "main"
    turn_marker: str | None = None
    turn_state: str | None = None

    @property
    def thread_id(self) -> str:
        """The session id for the main role, ``"<role>:<session>"`` otherwise."""
        if self.role == "main":
            return self.session_id
        return f"{self.role}:{self.session_id}"

    def begin(self, marker: str | None) -> bool:
        """Enter the turn ``marker`` names; True when it is a new turn.

        A request with no marker cannot be placed in a turn, so it clears the
        token and never counts as a turn start.
        """
        if marker is None:
            self.turn_marker = None
            self.turn_state = None
            return False
        if marker == self.turn_marker:
            return False
        self.turn_marker = marker
        self.turn_state = None
        return True


@dataclass(frozen=True)
class ErrorClass:
    """How a client classifies one provider error.

    ``max_retries`` caps retries for this class below the client's policy
    (None means the policy's own budget). ``backoff_floor`` is the minimum
    wait in seconds before the retry. ``drop_sticky`` says whether the
    engine should stop sending the provider's cache-affinity key after this
    error: OpenRouter drops it (a dead replica stays dead), the Responses
    API never does (the key is what keeps its prompt cache warm).
    """

    retriable: bool = False
    code: str | None = None
    backoff_floor: float = 0.0
    max_retries: int | None = None
    drop_sticky: bool = False


# The key in ``ProviderRequest.extra`` that carries the turn a request belongs
# to. Keys with the ``_fluid_`` prefix are engine-to-client signals, and a
# serializer that merges ``extra`` into the body leaves them out.
TURN_MARKER_KEY = "_fluid_turn_id"
INTERNAL_EXTRA_PREFIX = "_fluid_"


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
    # Per-loop, per-role routing state (see WireState). None for one-off
    # callers; a client that uses it reads and updates it in place.
    wire_state: WireState | None = field(default=None, compare=False, repr=False)


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
