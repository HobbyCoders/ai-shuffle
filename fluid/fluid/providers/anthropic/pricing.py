"""Anthropic Messages API pricing table + per-call cost estimator.

Prices are Anthropic's published per-MTok USD list prices for the Messages API.
Derived rates follow the published multipliers:

* cache **write** = 1.25x the input rate (5m ephemeral TTL — the only TTL this
  harness plans to write),
* cache **read** = 0.1x the input rate.

The client in this package is API-key auth only (subscription OAuth is
deliberately unsupported), so every call here is metered spend and estimating
it unconditionally is correct. An unknown model id yields ``None`` — never
guess a price.
"""

from __future__ import annotations

# Longest-prefix-first: matched against the lowercased model id with
# ``str.startswith``, first match wins. Order matters — specific dated/legacy
# families must precede the shorter family-wide fallbacks.
_PRICES: list[tuple[str, float, float]] = [
    # (prefix, input $/MTok, output $/MTok)
    ("claude-opus-4-1", 15.0, 75.0),      # Opus 4.1 — legacy pricing
    ("claude-opus-4-2025", 15.0, 75.0),   # Opus 4.0 dated ids — legacy pricing
    ("claude-opus-4-0", 15.0, 75.0),      # Opus 4.0 — legacy pricing
    ("claude-3-5-haiku", 0.8, 4.0),
    ("claude-3-haiku", 0.25, 1.25),
    ("claude-3-7-sonnet", 3.0, 15.0),
    ("claude-3-5-sonnet", 3.0, 15.0),
    ("claude-3-sonnet", 3.0, 15.0),
    ("claude-3-opus", 15.0, 75.0),
    ("claude-fable-5", 10.0, 50.0),
    ("claude-mythos", 10.0, 50.0),
    ("claude-opus", 5.0, 25.0),   # Opus 5 / 4.8 / 4.7 / 4.6 / 4.5
    ("claude-sonnet", 3.0, 15.0), # Sonnet 5 / 4.6 / 4.5 / 4.0 (Sonnet 5 intro
                                  # discount through 2026-08-31 ignored; list price)
    ("claude-haiku", 1.0, 5.0),
]

_CACHE_WRITE_MULTIPLIER = 1.25  # 5m ephemeral
_CACHE_READ_MULTIPLIER = 0.1


def _rates(model: str) -> tuple[float, float] | None:
    lowered = (model or "").lower()
    for prefix, input_rate, output_rate in _PRICES:
        if lowered.startswith(prefix):
            return input_rate, output_rate
    return None


def estimate_cost(model: str, usage: dict) -> float | None:
    """Estimated USD cost of one model call, or ``None`` for unknown models."""
    rates = _rates(model)
    if rates is None:
        return None
    input_rate, output_rate = rates
    input_tokens = usage.get("input_tokens", 0) or 0
    cache_write = usage.get("cache_creation_input_tokens", 0) or 0
    cache_read = usage.get("cache_read_input_tokens", 0) or 0
    output_tokens = usage.get("output_tokens", 0) or 0
    total = (
        input_tokens * input_rate
        + cache_write * _CACHE_WRITE_MULTIPLIER * input_rate
        + cache_read * _CACHE_READ_MULTIPLIER * input_rate
        + output_tokens * output_rate
    )
    return total / 1_000_000
