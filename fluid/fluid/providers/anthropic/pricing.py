"""Anthropic Messages API pricing table + per-call cost estimator.

Prices are Anthropic's published per-MTok USD list prices for the Messages API.
Derived rates follow the published multipliers:

* cache **write** = 1.25x the input rate for the 5-minute TTL and 2x for the
  1-hour TTL. The API splits ``cache_creation_input_tokens`` by TTL in
  ``usage.cache_creation`` (``ephemeral_5m_input_tokens`` /
  ``ephemeral_1h_input_tokens``); the 1-hour part bills at 2x and the rest at
  1.25x,
* cache **read** = 0.1x the input rate, except the families listed in
  ``_CACHE_READ_OVERRIDES``.

Long context: models from the 4.6 generation on ship the 1M window at
standard pricing, except Haiku 5.5 ($0.50/$2.50 above 100K prompt tokens).
A ``[1m]`` suffix changes nothing for these native-1M models. The
earlier 200K families reached 1M only through the ``context-1m`` beta, which
billed the whole call at a higher tier once the prompt passed 200K tokens;
that tier applies here when such an id carries ``[1m]``.

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
    ("claude-opus-5-5", 4.0, 20.0),  # Opus 5.5 lists below Opus 5
    ("claude-opus", 5.0, 25.0),   # Opus 5 / 4.8 / 4.7 / 4.6 / 4.5
    ("claude-sonnet-5", 2.0, 10.0),  # the launch price became the list price
    ("claude-sonnet", 3.0, 15.0), # Sonnet 4.6 / 4.5 / 4.0
    ("claude-haiku-5-5", 0.10, 0.50),
    ("claude-haiku", 1.0, 5.0),
]

# Published as multipliers of the base input rate for every model, with no
# per-model 1-hour rate listed (prompt-caching docs: "1.25x for 5-minute TTL,
# 2x for 1-hour TTL"). So Opus 5.5's 1-hour write is $8/MTok by that rule;
# the per-model table lists only input, output and cache-read overrides.
_CACHE_WRITE_MULTIPLIER = 1.25  # 5m ephemeral
_CACHE_WRITE_1H_MULTIPLIER = 2.0  # 1h ephemeral
_CACHE_READ_MULTIPLIER = 0.1
# Families whose published cache-read rate is not the 0.1x default, as a
# fraction of their input rate. Fable 5.1 lists cache reads at $0.25/MTok
# against a $10 input rate, and Opus 5.5 at $0.20/MTok against $4. Whether
# Mythos 5.1 shares Fable 5.1's rate was open at launch, so it keeps the
# default. Longest prefix first.
_CACHE_READ_OVERRIDES: list[tuple[str, float]] = [
    ("claude-fable-5-1", 0.025),
    ("claude-opus-5-5", 0.05),
]

# Families whose native window is 200K and whose 1M window was the
# ``context-1m`` beta. Prefix-matched like ``_PRICES``; a ``[1m]`` id from any
# other family (4.6 onward) prices the same across the whole window.
_LONG_CONTEXT_BETA_FAMILIES = (
    "claude-opus-4-0",
    "claude-opus-4-1",
    "claude-opus-4-2025",
    "claude-opus-4-5",
    "claude-sonnet-4-0",
    "claude-sonnet-4-2025",
    "claude-sonnet-4-5",
)
_LONG_CONTEXT_SUFFIX = "[1m]"
# The beta bills the whole call at the long tier once the prompt (input +
# cache read + cache write) exceeds this many tokens.
_LONG_CONTEXT_THRESHOLD = 200_000
# The beta's published multipliers over the base rates. Cache multipliers
# stack on the raised input rate.
_LONG_CONTEXT_INPUT_MULTIPLIER = 2.0
_LONG_CONTEXT_OUTPUT_MULTIPLIER = 1.5


def _rates(model: str) -> tuple[float, float] | None:
    lowered = (model or "").lower()
    for prefix, input_rate, output_rate in _PRICES:
        if lowered.startswith(prefix):
            return input_rate, output_rate
    return None


def _long_context_tier(model: str, prompt_tokens: int) -> bool:
    """True when this call bills at the ``context-1m`` beta's long tier."""
    lowered = (model or "").lower()
    if _LONG_CONTEXT_SUFFIX not in lowered or prompt_tokens <= _LONG_CONTEXT_THRESHOLD:
        return False
    return lowered.startswith(_LONG_CONTEXT_BETA_FAMILIES)


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
    prompt_tokens = input_tokens + cache_write + cache_read
    if (model or "").lower().startswith("claude-haiku-5-5") and prompt_tokens > 100_000:
        input_rate, output_rate = 0.50, 2.50
    elif _long_context_tier(model, prompt_tokens):
        input_rate *= _LONG_CONTEXT_INPUT_MULTIPLIER
        output_rate *= _LONG_CONTEXT_OUTPUT_MULTIPLIER
    write_5m, write_1h = _cache_write_split(usage, cache_write)
    total = (
        input_tokens * input_rate
        + write_5m * _CACHE_WRITE_MULTIPLIER * input_rate
        + write_1h * _CACHE_WRITE_1H_MULTIPLIER * input_rate
        + cache_read * _cache_read_multiplier(model) * input_rate
        + output_tokens * output_rate
    )
    return total / 1_000_000


def _cache_write_split(usage: dict, cache_write: int) -> tuple[int, int]:
    """Split this call's cache-write tokens into (5-minute, 1-hour) parts.

    The 1-hour count comes from the flat ``cache_creation_1h_input_tokens``
    key the stream parser adds (``serialize.CACHE_WRITE_1H_KEY``), or else
    from the API's nested ``usage["cache_creation"]`` breakdown. Without
    either, every write token is a 5-minute write. The 1-hour part is capped
    at the reported total, and whatever it leaves over bills at the 5-minute
    rate, so an inconsistent breakdown never drops tokens and never bills more
    than ``cache_write`` tokens in all.
    """
    raw_1h = usage.get("cache_creation_1h_input_tokens")
    if raw_1h is None:
        breakdown = usage.get("cache_creation")
        if not isinstance(breakdown, dict):
            return cache_write, 0
        raw_1h = breakdown.get("ephemeral_1h_input_tokens", 0)
    raw_1h = raw_1h or 0
    if isinstance(raw_1h, bool) or not isinstance(raw_1h, (int, float)):
        raw_1h = 0
    write_1h = min(max(raw_1h, 0), cache_write)
    return cache_write - write_1h, write_1h


def _cache_read_multiplier(model: str) -> float:
    lowered = (model or "").lower()
    for prefix, multiplier in _CACHE_READ_OVERRIDES:
        if lowered.startswith(prefix):
            return multiplier
    return _CACHE_READ_MULTIPLIER
