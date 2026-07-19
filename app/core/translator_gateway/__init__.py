"""
Translator gateway — Anthropic ⇄ OpenAI-Responses translation core (Chunk A).

STAGE 1 (this package): the pure, synchronous, network-free translation core.
  * ``events``                 — the internal normalized stream model + the §D4
                                 reasoning round-trip signature envelope codec.
  * ``anthropic_to_responses`` — Anthropic Messages request → Responses request.
  * ``responses_to_anthropic`` — Responses SSE stream → Anthropic SSE stream
                                 (+ buffered final assembly).
  * ``token_estimate``         — deterministic count_tokens estimator.

Stage 2 adds the HTTP router, routing table, upstream client, credentials, and
``app/main.py`` registration — none of which live here.

See docs/CODEX_SPIKE_FINDINGS.md (binding contract) and
docs/MULTI_PROVIDER_PHASE2_PLAN.md §A / gate rulings G3–G5.
"""

from __future__ import annotations

from app.core.translator_gateway.anthropic_to_responses import translate_request
from app.core.translator_gateway.events import (
    ENVELOPE_VERSION,
    MessageEnd,
    MessageStart,
    NormalizedResponse,
    NormalizedUsage,
    TextDelta,
    ThinkingDelta,
    ThinkingEnd,
    ToolCallArgsDelta,
    ToolCallEnd,
    ToolCallStart,
    UsageUpdate,
    decode_reasoning_signature,
    encode_reasoning_signature,
)
from app.core.translator_gateway.responses_to_anthropic import (
    AnthropicSSESerializer,
    ResponsesNormalizer,
    ResponsesToAnthropicTranslator,
    translate_responses_stream,
)
from app.core.translator_gateway.token_estimate import count_tokens

__all__ = [
    "ENVELOPE_VERSION",
    "encode_reasoning_signature",
    "decode_reasoning_signature",
    "MessageStart",
    "TextDelta",
    "ToolCallStart",
    "ToolCallArgsDelta",
    "ToolCallEnd",
    "ThinkingDelta",
    "ThinkingEnd",
    "UsageUpdate",
    "MessageEnd",
    "NormalizedUsage",
    "NormalizedResponse",
    "translate_request",
    "ResponsesNormalizer",
    "AnthropicSSESerializer",
    "ResponsesToAnthropicTranslator",
    "translate_responses_stream",
    "count_tokens",
]
