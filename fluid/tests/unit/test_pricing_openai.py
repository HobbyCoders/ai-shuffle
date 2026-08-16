"""OpenAI pricing table: per-row rates, long-context tier, prefix precedence."""

import pytest

from fluid.providers.openai_responses.pricing import estimate_cost


def test_known_model_all_four_buckets() -> None:
    usage = {
        "input_tokens": 100_000,
        "cache_read_input_tokens": 50_000,
        "cache_creation_input_tokens": 10_000,
        "output_tokens": 20_000,
    }
    # 0.1*2.00 + 0.05*0.20 + 0.01*2.50 + 0.02*12.00
    assert estimate_cost("gpt-5.6-terra", usage) == pytest.approx(0.475)


def test_long_context_tier_applies_above_threshold() -> None:
    long_usage = {"input_tokens": 300_000, "output_tokens": 10_000}
    # prompt_total 300k > 272k → long rates: 0.3*5.00 + 0.01*22.50
    assert estimate_cost("gpt-5.4", long_usage) == pytest.approx(1.725)

    short_usage = {"input_tokens": 100_000, "output_tokens": 10_000}
    # short rates: 0.1*2.50 + 0.01*15.00
    assert estimate_cost("gpt-5.4", short_usage) == pytest.approx(0.40)


def test_prefix_precedence() -> None:
    # 200K keeps every row on its short-context rates (see the 272K threshold).
    usage = {"input_tokens": 200_000}
    # gpt-5.6-luna must not fall through to the gpt-5.6 row (0.2*5.00 = 1.00).
    assert estimate_cost("gpt-5.6-luna", usage) == pytest.approx(0.04)
    # gpt-5-mini must not fall through to the gpt-5 row (0.2*1.25 = 0.25).
    assert estimate_cost("gpt-5-mini", usage) == pytest.approx(0.05)


def test_missing_cache_write_price_bills_at_input_rate() -> None:
    # 200K stays on gpt-5.4's short-context rates: 0.2 * 2.50 (the input rate).
    usage = {"cache_creation_input_tokens": 200_000}
    assert estimate_cost("gpt-5.4", usage) == pytest.approx(0.50)


def test_missing_cached_price_bills_at_input_rate() -> None:
    usage = {"cache_read_input_tokens": 1_000_000}
    assert estimate_cost("gpt-5.5-pro", usage) == pytest.approx(30.00)


def test_unknown_model_returns_none() -> None:
    assert estimate_cost("claude-opus-5", {"input_tokens": 1_000_000}) is None
    assert estimate_cost("gemini-2.5-pro", {"input_tokens": 1_000_000}) is None


def test_match_is_case_insensitive() -> None:
    # 200K stays on gpt-5.6-terra's short-context input rate: 0.2 * 2.00.
    usage = {"input_tokens": 200_000}
    assert estimate_cost("GPT-5.6-TERRA", usage) == pytest.approx(0.40)
