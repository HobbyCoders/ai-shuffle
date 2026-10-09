"""Model context-window table — the fallback when nothing names a window.

Longest-prefix-first against the lowercased model id, the same shape as
:mod:`fluid.providers.anthropic.pricing` and :mod:`fluid.providers.anthropic.limits`
so the three tables read and drift the same way. A vendor prefix an aggregator
adds (``anthropic/claude-opus-5`` on OpenRouter, ``anthropic.claude-opus-5`` on
Bedrock) is stripped before the lookup. Anything unrecognised — a local model,
an id this table has never heard of — yields ``None``: the usage breakdown then
shows no percentage rather than a wrong one, and the eviction ladder keeps its
flat trigger.

Consulted only when ``context_window`` is unset. A configured value or
``FLUID_CONTEXT_WINDOW`` always wins.

Sizes checked against the vendors' published docs on 2026-09-01. Every Claude
model from the 4.6 generation on has a 1M window by default (no beta header);
earlier Claude models have 200K. A ``[1m]`` suffix on a Claude id names the 1M
window whatever the family.
"""

from __future__ import annotations

_ONE_M_SUFFIX = "[1m]"

_WINDOWS: list[tuple[str, int]] = [
    # (prefix, context window in tokens) — 200K Claude families first, so they
    # beat the family-wide 1M rows below them.
    ("claude-3", 200_000),
    ("claude-haiku-5-5", 1_000_000),
    ("claude-haiku", 200_000),
    ("claude-opus-4-0", 200_000),
    ("claude-opus-4-1", 200_000),
    ("claude-opus-4-2025", 200_000),
    ("claude-opus-4-5", 200_000),
    ("claude-sonnet-4-0", 200_000),
    ("claude-sonnet-4-2025", 200_000),
    ("claude-sonnet-4-5", 200_000),
    ("claude-opus", 1_000_000),    # Opus 4.6 / 4.7 / 4.8 / 5
    ("claude-sonnet", 1_000_000),  # Sonnet 4.6 / 5
    ("claude-fable", 1_000_000),
    ("claude-mythos", 1_000_000),
    # OpenAI (Responses API ids, also the bare part of an OpenRouter id).
    ("gpt-4.1", 1_047_576),
    ("gpt-4o", 128_000),
    ("gpt-5", 400_000),
    ("codex-mini", 200_000),
    ("o1", 200_000),
    ("o3", 200_000),
    ("o4", 200_000),
]

_BEDROCK_PREFIX = "anthropic."


def bare_model_id(model: str | None) -> str:
    """The model id with any aggregator vendor prefix removed, lowercased.

    ``anthropic/claude-opus-5`` and ``openai/gpt-5-mini`` (OpenRouter) lose the
    part before the slash; ``anthropic.claude-opus-5`` (Bedrock) loses the
    dotted vendor. Variant suffixes (``:free``, ``[1m]``) are kept — the prefix
    match does not mind them and ``[1m]`` is meaningful.
    """
    lowered = (model or "").strip().lower()
    if "/" in lowered:
        lowered = lowered.rsplit("/", 1)[1]
    if lowered.startswith(_BEDROCK_PREFIX):
        lowered = lowered[len(_BEDROCK_PREFIX):]
    return lowered


def context_window_for(model: str | None) -> int | None:
    """The model's context window in tokens, or ``None`` when unknown."""
    bare = bare_model_id(model)
    if not bare:
        return None
    if bare.startswith("claude") and _ONE_M_SUFFIX in bare:
        return 1_000_000
    for prefix, window in _WINDOWS:
        if bare.startswith(prefix):
            return window
    return None
