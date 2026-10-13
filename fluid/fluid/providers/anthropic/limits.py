"""Per-model output ceilings for the Anthropic Messages API.

Unlike every other dialect fluid speaks, ``max_tokens`` is **required** here —
there is no "use the model's own maximum" wire value to fall back on. So when a
request carries no explicit cap, the number has to come from somewhere, and it
has to be a number the API will accept: Anthropic rejects the whole request when
``max_tokens`` exceeds the model's ceiling, rather than clamping it.

Longest-prefix-first against the lowercased model id, mirroring
:mod:`fluid.providers.anthropic.pricing` so the two tables read the same way and
drift the same way. Anything unrecognised falls back to
:data:`CONSERVATIVE_MAX_OUTPUT_TOKENS`, which every Claude model since Claude 3
accepts.
"""

from __future__ import annotations

# Accepted by every Claude model from Claude 3 onward — the safe answer for a
# model id this table has never heard of.
CONSERVATIVE_MAX_OUTPUT_TOKENS = 8192

_MAX_OUTPUT: list[tuple[str, int]] = [
    # (prefix, max output tokens) — specific/legacy families first.
    ("claude-opus-4-1", 32_000),
    ("claude-opus-4-2025", 32_000),
    ("claude-opus-4-0", 32_000),
    ("claude-3-opus", 4_096),
    ("claude-3-haiku", 4_096),
    ("claude-3-5-haiku", 8_192),
    ("claude-3-5-sonnet", 8_192),
    ("claude-3-7-sonnet", 64_000),
    ("claude-3-sonnet", 4_096),
    ("claude-fable-5", 64_000),
    ("claude-mythos", 64_000),
    ("claude-opus-5-5", 128_000),
    ("claude-sonnet-5-5", 128_000),
    ("claude-opus", 64_000),   # Opus 5 / 4.8 / 4.7 / 4.6 / 4.5
    ("claude-sonnet", 64_000),  # Sonnet 5 / 4.6 / 4.5 / 4.0
    ("claude-haiku-5-5", 128_000),
    ("claude-haiku", 64_000),   # Haiku 4.5
]


def max_output_tokens(model: str) -> int:
    """The largest ``max_tokens`` this model accepts, as far as we know."""
    lowered = (model or "").lower()
    for prefix, cap in _MAX_OUTPUT:
        if lowered.startswith(prefix):
            return cap
    return CONSERVATIVE_MAX_OUTPUT_TOKENS


def resolve_max_tokens(model: str, requested: int | None) -> int:
    """The value to put on the wire.

    ``None`` means the caller set no ceiling and wants the model's own; an
    explicit request is honoured but still clamped to what the API will accept,
    since a rejected request helps nobody.
    """
    ceiling = max_output_tokens(model)
    if requested is None:
        return ceiling
    return max(1, min(int(requested), ceiling))
