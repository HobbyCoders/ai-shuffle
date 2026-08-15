"""Anthropic pricing table: rates, cache multipliers, prefix precedence."""

import pytest

from fluid.providers.anthropic.pricing import estimate_cost


def test_known_model_applies_cache_multipliers() -> None:
    usage = {
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
        "cache_creation_input_tokens": 1_000_000,
        "cache_read_input_tokens": 1_000_000,
    }
    # 5 (input) + 25 (output) + 6.25 (cache write @1.25x) + 0.5 (cache read @0.1x)
    assert estimate_cost("claude-opus-5", usage) == pytest.approx(36.75)


def test_unknown_model_returns_none() -> None:
    assert estimate_cost("gpt-5", {"input_tokens": 1_000_000}) is None


def test_longest_prefix_wins_for_legacy_opus() -> None:
    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
    # Opus 4.1 keeps legacy 15/75, not the 5/25 of the "claude-opus" fallback.
    assert estimate_cost("claude-opus-4-1-20250805", usage) == pytest.approx(90.0)
    assert estimate_cost("claude-opus-5", usage) == pytest.approx(30.0)
