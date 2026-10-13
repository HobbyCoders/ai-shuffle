"""Anthropic pricing table: rates, cache multipliers, prefix precedence."""

import pytest

from fluid.providers.anthropic.pricing import (
    _LONG_CONTEXT_INPUT_MULTIPLIER,
    _LONG_CONTEXT_OUTPUT_MULTIPLIER,
    estimate_cost,
)


def test_known_model_applies_cache_multipliers() -> None:
    usage = {
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
        "cache_creation_input_tokens": 1_000_000,
        "cache_read_input_tokens": 1_000_000,
    }
    # 5 (input) + 25 (output) + 6.25 (cache write @1.25x) + 0.5 (cache read @0.1x)
    assert estimate_cost("claude-opus-5", usage) == pytest.approx(36.75)


@pytest.mark.parametrize("prompt,rate", [(99_999, 0.10), (100_000, 0.10), (100_001, 0.50)])
def test_haiku_5_5_native_long_context_pricing(prompt, rate) -> None:
    usage = {"input_tokens": prompt, "output_tokens": 1000}
    expected = (prompt * rate + 1000 * rate * 5) / 1_000_000
    assert estimate_cost("claude-haiku-5-5", usage) == pytest.approx(expected)
    assert estimate_cost("claude-haiku-5-5[1m]", usage) == pytest.approx(expected)
    assert estimate_cost("claude-haiku-4-5", usage) == pytest.approx((prompt + 5000) / 1_000_000)


def test_haiku_5_5_long_tier_counts_cached_prompt_tokens() -> None:
    usage = {"input_tokens": 1, "cache_read_input_tokens": 50_000,
             "cache_creation_input_tokens": 50_000, "output_tokens": 1000}
    expected = (0.50 + 50_000 * 0.05 + 50_000 * 0.625 + 1000 * 2.50) / 1_000_000
    assert estimate_cost("claude-haiku-5-5", usage) == pytest.approx(expected)


def test_unknown_model_returns_none() -> None:
    assert estimate_cost("gpt-5", {"input_tokens": 1_000_000}) is None


def test_longest_prefix_wins_for_legacy_opus() -> None:
    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
    # Opus 4.1 keeps legacy 15/75, not the 5/25 of the "claude-opus" fallback.
    assert estimate_cost("claude-opus-4-1-20250805", usage) == pytest.approx(90.0)
    assert estimate_cost("claude-opus-5", usage) == pytest.approx(30.0)


def test_sonnet_5_has_its_own_list_price() -> None:
    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
    assert estimate_cost("claude-sonnet-5", usage) == pytest.approx(12.0)
    assert estimate_cost("claude-sonnet-4-6", usage) == pytest.approx(18.0)


# ─── long context: the 200K families' 1M beta tier ───────────────────────────

_OVER_200K = {
    "input_tokens": 150_000,
    "cache_read_input_tokens": 100_000,
    "cache_creation_input_tokens": 0,
    "output_tokens": 10_000,
}


def test_long_context_tier_applies_past_200k_on_a_1m_beta_id() -> None:
    base = estimate_cost("claude-sonnet-4-5", _OVER_200K)
    long = estimate_cost("claude-sonnet-4-5[1m]", _OVER_200K)
    # 150k @ 3*2 + 100k cache reads @ 0.1*3*2 + 10k out @ 15*1.5
    assert base == pytest.approx(0.63)
    assert long == pytest.approx(1.185)
    # Shape: input-side rates scale by the input multiplier, output by its own.
    input_side = (150_000 * 3.0 + 100_000 * 0.3) / 1_000_000
    output_side = 10_000 * 15.0 / 1_000_000
    assert long == pytest.approx(
        input_side * _LONG_CONTEXT_INPUT_MULTIPLIER
        + output_side * _LONG_CONTEXT_OUTPUT_MULTIPLIER
    )


def test_long_context_tier_needs_the_suffix_and_the_threshold() -> None:
    at_threshold = {"input_tokens": 200_000, "output_tokens": 1_000}
    assert estimate_cost("claude-sonnet-4-5[1m]", at_threshold) == pytest.approx(
        estimate_cost("claude-sonnet-4-5", at_threshold)
    )
    # Past the threshold without the suffix: the plain 200K model, base rates.
    assert estimate_cost("claude-sonnet-4-5", _OVER_200K) == pytest.approx(0.63)


def test_native_1m_models_price_the_same_across_the_window() -> None:
    """The 4.6 generation on ships 1M at standard pricing; ``[1m]`` is a no-op."""
    for model in ("claude-opus-5", "claude-sonnet-5", "claude-sonnet-4-6"):
        assert estimate_cost(f"{model}[1m]", _OVER_200K) == pytest.approx(
            estimate_cost(model, _OVER_200K)
        )


# ─── cache-read rates that differ from the 0.1x default ──────────────────────


def test_fable_5_1_cache_reads_bill_at_a_quarter_rate() -> None:
    usage = {"cache_read_input_tokens": 1_000_000}
    # $10 input, cache reads listed at $0.25/MTok.
    assert estimate_cost("claude-fable-5-1", usage) == pytest.approx(0.25)
    # Fable 5 and Mythos keep the published 0.1x default.
    assert estimate_cost("claude-fable-5", usage) == pytest.approx(1.0)
    assert estimate_cost("claude-mythos-5-1", usage) == pytest.approx(1.0)
