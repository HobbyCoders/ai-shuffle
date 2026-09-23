"""Pins the pricing rows the fluid v2 review found wrong (caching-cost L9).

* gpt-6-luna fell through to the unpriced ``gpt-6`` row, so its calls
  recorded no cost and the host stored $0.
* Claude Opus 5.5 matched the family-wide ``claude-opus`` row and priced at
  Opus 5's $5/$25 instead of its own $4/$20, with cache reads at $0.20.
* Every Anthropic cache write billed at the 5-minute 1.25x rate, including
  1-hour writes, which bill at 2x.
"""

import pytest

from fluid.providers.anthropic import pricing as anthropic_pricing
from fluid.providers.anthropic.serialize import CACHE_WRITE_1H_KEY, sse_to_chunk
from fluid.providers.pricing import estimate_cost, resolve_price

_MTOK = 1_000_000


def _only(key: str) -> dict:
    return {key: _MTOK}


# ─── gpt-6-luna ──────────────────────────────────────────────────────────────


def test_gpt_6_luna_row_is_priced() -> None:
    price = resolve_price("gpt-6-luna")
    assert price is not None and price.priced
    assert (price.input, price.cached_input, price.cache_write, price.output) == (
        0.10, 0.01, 0.125, 0.50,
    )
    assert estimate_cost("gpt-6-luna", _only("input_tokens")) == pytest.approx(0.10)
    assert estimate_cost("gpt-6-luna", _only("cache_read_input_tokens")) == pytest.approx(0.01)
    assert estimate_cost("gpt-6-luna", _only("cache_creation_input_tokens")) == pytest.approx(0.125)
    assert estimate_cost("gpt-6-luna", _only("output_tokens")) == pytest.approx(0.50)


def test_gpt_6_luna_prices_through_an_aggregator_prefix() -> None:
    assert estimate_cost("openai/gpt-6-luna", _only("output_tokens")) == pytest.approx(0.50)


def test_the_luna_row_does_not_disturb_its_neighbours() -> None:
    assert estimate_cost("gpt-6-astra", _only("output_tokens")) == pytest.approx(50.00)
    # An unsourced gpt-6 id still reports no cost rather than luna's rate.
    assert estimate_cost("gpt-6-sol", _only("output_tokens")) is None


# ─── Claude Opus 5.5 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-opus-5-5[1m]", "anthropic/claude-opus-5-5"])
def test_opus_5_5_list_prices(model: str) -> None:
    assert estimate_cost(model, _only("input_tokens")) == pytest.approx(4.00)
    assert estimate_cost(model, _only("output_tokens")) == pytest.approx(20.00)
    assert estimate_cost(model, _only("cache_read_input_tokens")) == pytest.approx(0.20)
    assert estimate_cost(model, _only("cache_creation_input_tokens")) == pytest.approx(5.00)


def test_opus_5_keeps_its_own_price() -> None:
    assert estimate_cost("claude-opus-5", _only("input_tokens")) == pytest.approx(5.00)
    assert estimate_cost("claude-opus-5", _only("output_tokens")) == pytest.approx(25.00)
    assert estimate_cost("claude-opus-5", _only("cache_read_input_tokens")) == pytest.approx(0.50)


# ─── Anthropic cache-write TTL multipliers ───────────────────────────────────


def test_cache_write_multipliers_per_ttl() -> None:
    assert anthropic_pricing._CACHE_WRITE_MULTIPLIER == 1.25
    assert anthropic_pricing._CACHE_WRITE_1H_MULTIPLIER == 2.0


def test_a_write_with_no_ttl_breakdown_bills_at_the_5_minute_rate() -> None:
    usage = {"cache_creation_input_tokens": _MTOK}
    assert anthropic_pricing.estimate_cost("claude-opus-5", usage) == pytest.approx(6.25)


def test_the_api_breakdown_bills_each_ttl_at_its_own_rate() -> None:
    usage = {
        "cache_creation_input_tokens": 2 * _MTOK,
        "cache_creation": {
            "ephemeral_5m_input_tokens": _MTOK,
            "ephemeral_1h_input_tokens": _MTOK,
        },
    }
    # Opus 5 at $5 input: 1M x 1.25 x 5 + 1M x 2 x 5.
    assert anthropic_pricing.estimate_cost("claude-opus-5", usage) == pytest.approx(16.25)


def test_the_flat_1h_key_from_the_stream_parser_is_honoured() -> None:
    usage = {"cache_creation_input_tokens": _MTOK, CACHE_WRITE_1H_KEY: _MTOK}
    assert anthropic_pricing.estimate_cost("claude-opus-5-5", usage) == pytest.approx(8.00)


def test_an_oversized_1h_count_never_bills_more_than_the_reported_writes() -> None:
    usage = {
        "cache_creation_input_tokens": _MTOK,
        "cache_creation": {"ephemeral_1h_input_tokens": 3 * _MTOK},
    }
    assert anthropic_pricing.estimate_cost("claude-opus-5", usage) == pytest.approx(10.00)


def test_the_stream_parser_lifts_the_1h_split_to_a_top_level_integer() -> None:
    """The engine's per-call usage merge keeps only top-level integers."""
    start = sse_to_chunk("message_start", {
        "type": "message_start",
        "message": {"usage": {
            "input_tokens": 10,
            "cache_creation_input_tokens": 300,
            "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 200},
        }},
    })
    assert start is not None
    assert start.payload["usage"][CACHE_WRITE_1H_KEY] == 200

    delta = sse_to_chunk("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn"},
        "usage": {"output_tokens": 5, "cache_creation": {"ephemeral_1h_input_tokens": 200}},
    })
    assert delta is not None
    assert delta.payload["usage"][CACHE_WRITE_1H_KEY] == 200


def test_the_stream_parser_leaves_5_minute_only_usage_unchanged() -> None:
    usage = {
        "input_tokens": 10,
        "cache_creation_input_tokens": 100,
        "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 0},
    }
    chunk = sse_to_chunk("message_start", {"type": "message_start", "message": {"usage": usage}})
    assert chunk is not None
    assert chunk.payload["usage"] == usage
