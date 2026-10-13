"""OpenAI Responses API pricing table + per-call cost estimator.

Prices are OpenAI's published per-MTok USD list prices, retrieved
2026-08-16 from https://developers.openai.com/api/docs/pricing (legacy rows
marked below come from the per-model docs pages, which outlive the main
pricing table).

Two things differ from the Anthropic table in this package:

* OpenAI's cached-input discount is **not** a constant ratio across models, so
  every row carries explicit ``in`` / ``cached`` / ``write`` / ``out`` rates
  instead of global multipliers. ``write`` is ``None`` when OpenAI publishes no
  distinct cache-write price (only the gpt-5.6 family has one) — those bill
  cache writes at the plain input rate. ``cached`` is ``None`` on the pro-tier
  rows, where no cached price is published — those bill cached reads at the
  full input rate, which is the conservative reading (caching is unpriced
  there, not free).
* Some model lines have a **long-context** tier: OpenAI bills the higher rates
  when a request's total prompt exceeds 272K tokens. Those rows carry a
  ``long`` sub-dict with the same four keys.

An unknown model id yields ``None`` — no cost is reported rather than a wrong
one. Never guess a price.
"""

from __future__ import annotations

# OpenAI's pricing page bills the >272K tier per request: when the prompt for a
# single call exceeds this many tokens, the whole call prices at the long rates.
_LONG_CONTEXT_THRESHOLD = 272_000

# Longest-prefix-first: matched against the lowercased model id with
# ``str.startswith``, first match wins. Order matters — specific families must
# precede the shorter family-wide fallbacks.
_PRICES: list[tuple[str, dict]] = [
    # (prefix, {"in", "cached", "write", "out", optional "long"}) — $/MTok
    (
        "gpt-5.6-terra",
        {
            "in": 2.00,
            "cached": 0.20,
            "write": 2.50,
            "out": 12.00,
            "long": {"in": 4.00, "cached": 0.40, "write": 5.00, "out": 18.00},
        },
    ),
    (
        "gpt-5.6-luna",
        {
            "in": 0.20,
            "cached": 0.02,
            "write": 0.25,
            "out": 1.20,
            "long": {"in": 0.40, "cached": 0.04, "write": 0.50, "out": 1.80},
        },
    ),
    (
        "gpt-5.6-cyber",
        {"in": 12.50, "cached": 1.25, "write": 15.625, "out": 75.00},
    ),
    (
        # covers gpt-5.6-sol + the bare alias
        "gpt-5.6",
        {
            "in": 5.00,
            "cached": 0.50,
            "write": 6.25,
            "out": 30.00,
            "long": {"in": 10.00, "cached": 1.00, "write": 12.50, "out": 45.00},
        },
    ),
    (
        "gpt-5.5-pro",
        {"in": 30.00, "cached": None, "write": None, "out": 180.00},
    ),
    (
        "gpt-5.5",
        {
            "in": 5.00,
            "cached": 0.50,
            "write": None,
            "out": 30.00,
            "long": {"in": 10.00, "cached": 1.00, "write": None, "out": 45.00},
        },
    ),
    ("gpt-5.4-mini", {"in": 0.75, "cached": 0.075, "write": None, "out": 4.50}),
    ("gpt-5.4-nano", {"in": 0.20, "cached": 0.02, "write": None, "out": 1.25}),
    ("gpt-5.4-pro", {"in": 30.00, "cached": None, "write": None, "out": 180.00}),
    (
        "gpt-5.4",
        {
            "in": 2.50,
            "cached": 0.25,
            "write": None,
            "out": 15.00,
            "long": {"in": 5.00, "cached": 0.50, "write": None, "out": 22.50},
        },
    ),
    ("gpt-5.3-codex", {"in": 1.75, "cached": 0.175, "write": None, "out": 14.00}),
    ("gpt-5.2-pro", {"in": 21.00, "cached": None, "write": None, "out": 168.00}),
    ("gpt-5.2", {"in": 1.75, "cached": 0.175, "write": None, "out": 14.00}),
    # legacy, from the per-model docs (not on the main pricing page)
    ("gpt-5.1-codex-mini", {"in": 0.25, "cached": 0.025, "write": None, "out": 2.00}),
    ("gpt-5.1-codex", {"in": 1.25, "cached": 0.125, "write": None, "out": 10.00}),
    ("gpt-5.1", {"in": 1.25, "cached": 0.125, "write": None, "out": 10.00}),
    # gpt-5-chat-latest
    ("gpt-5-chat", {"in": 5.00, "cached": 0.50, "write": None, "out": 30.00}),
    ("gpt-5-mini", {"in": 0.25, "cached": 0.025, "write": None, "out": 2.00}),
    ("gpt-5-nano", {"in": 0.05, "cached": 0.005, "write": None, "out": 0.40}),
    ("gpt-5-pro", {"in": 15.00, "cached": None, "write": None, "out": 120.00}),
    # legacy, from the per-model docs
    ("gpt-5-codex", {"in": 1.25, "cached": 0.125, "write": None, "out": 10.00}),
    # also catches gpt-5-search-api (same rates)
    ("gpt-5", {"in": 1.25, "cached": 0.125, "write": None, "out": 10.00}),
    # codex-mini-latest, legacy
    ("codex-mini", {"in": 0.25, "cached": 0.025, "write": None, "out": 2.00}),
    ("gpt-4.1-mini", {"in": 0.40, "cached": 0.10, "write": None, "out": 1.60}),
    ("gpt-4.1-nano", {"in": 0.10, "cached": 0.025, "write": None, "out": 0.40}),
    ("gpt-4.1", {"in": 2.00, "cached": 0.50, "write": None, "out": 8.00}),
    ("gpt-4o-mini", {"in": 0.15, "cached": 0.075, "write": None, "out": 0.60}),
    ("gpt-4o", {"in": 2.50, "cached": 1.25, "write": None, "out": 10.00}),
    ("o1-pro", {"in": 150.00, "cached": None, "write": None, "out": 600.00}),
    ("o1", {"in": 15.00, "cached": 7.50, "write": None, "out": 60.00}),
    ("o3-pro", {"in": 20.00, "cached": None, "write": None, "out": 80.00}),
    ("o3-mini", {"in": 1.10, "cached": 0.55, "write": None, "out": 4.40}),
    ("o3", {"in": 2.00, "cached": 0.50, "write": None, "out": 8.00}),
    ("o4-mini", {"in": 1.10, "cached": 0.275, "write": None, "out": 4.40}),
]


def _row(model: str) -> dict | None:
    lowered = (model or "").lower()
    for prefix, rates in _PRICES:
        if lowered.startswith(prefix):
            return rates
    return None


def estimate_cost(model: str, usage: dict) -> float | None:
    """Estimated USD cost of one model call, or ``None`` for unknown models."""
    row = _row(model)
    if row is None:
        return None
    input_tokens = usage.get("input_tokens", 0) or 0
    cache_write = usage.get("cache_creation_input_tokens", 0) or 0
    cache_read = usage.get("cache_read_input_tokens", 0) or 0
    output_tokens = usage.get("output_tokens", 0) or 0
    # ``reasoning_tokens`` is already a subset of ``output_tokens`` in our
    # normalized usage (see openai_responses/serialize.py) — never add it.

    prompt_total = input_tokens + cache_read + cache_write
    rates = row
    long_rates = row.get("long")
    if long_rates is not None and prompt_total > _LONG_CONTEXT_THRESHOLD:
        rates = long_rates

    input_rate = rates["in"]
    # No published cached price → cached reads bill at the full input rate.
    cached_rate = rates["cached"] if rates["cached"] is not None else input_rate
    # No published cache-write price → writes bill at the plain input rate.
    write_rate = rates["write"] if rates["write"] is not None else input_rate

    total = (
        input_tokens * input_rate
        + cache_read * cached_rate
        + cache_write * write_rate
        + output_tokens * rates["out"]
    )
    return total / 1_000_000
