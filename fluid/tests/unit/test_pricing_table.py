"""Cross-provider pricing: id normalization, the host hook, and never raising.

The finding behind this module: 14 of 15 audited sessions recorded no spend at
all, because a Grok id on the OpenAI dialect and a GLM id on OpenRouter matched
no pricing table and the host stores 0.0 for a missing cost.
"""

import pytest

from fluid.providers.pricing import (
    ModelPrice,
    estimate_cost,
    normalize_model_id,
    resolve_price,
)

_MTOK = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}


# ─── Id normalization ────────────────────────────────────────────────────────


def test_vendor_prefixes_and_routing_tags_are_stripped() -> None:
    assert normalize_model_id("z-ai/glm-5.3-flash") == "glm-5.3-flash"
    assert normalize_model_id("Anthropic/Claude-Opus-5") == "claude-opus-5"
    assert normalize_model_id("openai/gpt-5.6-sol:nitro") == "gpt-5.6-sol"
    assert normalize_model_id("x-ai/grok-4.6") == "grok-4.6"
    assert normalize_model_id(None) == ""


def test_normalization_leaves_load_bearing_suffixes_alone() -> None:
    """Anthropic's long-context tag decides a rate; it must survive."""
    assert normalize_model_id("claude-sonnet-4-5[1m]") == "claude-sonnet-4-5[1m]"


def test_only_one_vendor_segment_is_stripped() -> None:
    """A slash inside the model name itself is not a vendor prefix."""
    assert normalize_model_id("openai/gpt-5/preview") == "gpt-5/preview"


# ─── Delegation to the sourced per-provider tables ───────────────────────────


def test_a_prefixed_claude_id_prices_like_the_bare_one() -> None:
    assert estimate_cost("anthropic/claude-opus-5", _MTOK) == pytest.approx(
        estimate_cost("claude-opus-5", _MTOK)
    )
    assert estimate_cost("claude-opus-5", _MTOK) == pytest.approx(30.0)


def test_a_prefixed_openai_id_prices_like_the_bare_one() -> None:
    usage = {"input_tokens": 1_000, "output_tokens": 1_000}
    assert estimate_cost("openai/gpt-5-mini", usage) == pytest.approx(
        estimate_cost("gpt-5-mini", usage)
    )


# ─── Known-but-unpriced vs unknown ───────────────────────────────────────────


def test_a_recognized_model_with_no_sourced_rate_reports_no_cost() -> None:
    """Never guess: the row exists so the model is recognized, and the absent
    rate keeps the cost absent rather than fabricated."""
    for model in ("grok-3", "z-ai/glm-4-32b", "gpt-6-mini"):
        price = resolve_price(model)
        assert price is not None and not price.priced, model
        assert "not sourced" in price.source
        assert estimate_cost(model, _MTOK) is None, model


def test_the_audited_models_now_carry_sourced_rates() -> None:
    """The three families a whole audit round saw priced at $0: sourced from
    the vendors' pages, short-context tier, source note on every row."""
    for model, expected in (  # _MTOK is one MTok in and one out
        ("grok-4.6", 2.00 + 6.00),
        ("x-ai/grok-4.5", 2.00 + 6.00),
        ("z-ai/glm-5.3-flash", 0.075 + 0.25),
        ("glm-5.3", 1.40 + 4.40),
        ("gpt-6-astra", 10.00 + 50.00),
    ):
        price = resolve_price(model)
        assert price is not None and price.priced, model
        assert "2026-09-04" in price.source
        assert estimate_cost(model, _MTOK) == pytest.approx(expected), model
    # Longest prefix wins: the flash variant is not billed at the 5.3 rate.
    assert resolve_price("glm-5.3-flash") is not resolve_price("glm-5.3")


def test_an_unknown_model_resolves_to_nothing() -> None:
    assert resolve_price("wholly-made-up-9000") is None
    assert estimate_cost("wholly-made-up-9000", _MTOK) is None


# ─── The host override hook ──────────────────────────────────────────────────


def test_the_host_resolver_prices_a_model_fluid_cannot() -> None:
    def resolver(model: str):
        return {"input": 0.30, "cached_input": 0.03, "output": 1.20}

    cost = estimate_cost("z-ai/glm-5.3-flash", _MTOK, resolver=resolver)
    assert cost == pytest.approx(0.30 + 1.20)


def test_the_host_resolver_accepts_a_model_price_and_wins_over_the_table() -> None:
    price = ModelPrice(input=1.0, output=2.0, source="host catalog")
    assert estimate_cost(
        "claude-opus-5", _MTOK, resolver=lambda _m: price
    ) == pytest.approx(3.0)


def test_the_host_resolver_sees_the_id_the_engine_uses() -> None:
    seen: list[str] = []

    def resolver(model: str):
        seen.append(model)
        return None

    estimate_cost("z-ai/glm-5.3-flash", _MTOK, resolver=resolver)
    assert seen == ["z-ai/glm-5.3-flash"]


def test_declining_to_answer_falls_through_to_the_table() -> None:
    assert estimate_cost(
        "claude-opus-5", _MTOK, resolver=lambda _m: None
    ) == pytest.approx(30.0)


# ─── Never raise ─────────────────────────────────────────────────────────────


def test_a_broken_host_resolver_costs_a_price_never_a_turn() -> None:
    def resolver(model: str):
        raise RuntimeError("catalog is down")

    assert estimate_cost(
        "claude-opus-5", _MTOK, resolver=resolver
    ) == pytest.approx(30.0)


def test_junk_from_a_host_resolver_is_ignored() -> None:
    for junk in (42, "cheap", [], {"input": "three dollars"}, True):
        assert estimate_cost("grok-3", _MTOK, resolver=lambda _m, j=junk: j) is None


def test_junk_usage_values_never_raise() -> None:
    usage = {
        "input_tokens": None,
        "output_tokens": "many",
        "cache_read_input_tokens": True,
    }
    # Own arithmetic: unusable counts read as zero.
    assert estimate_cost(
        "x", usage, resolver=lambda _m: ModelPrice(input=1.0, output=2.0)
    ) == pytest.approx(0.0)
    # Delegated arithmetic: a provider table that chokes yields no cost, not
    # an exception out of the turn.
    assert estimate_cost("claude-opus-5", usage) is None


# ─── Rate semantics ──────────────────────────────────────────────────────────


def test_unpriced_cache_tiers_bill_at_the_input_rate_not_free() -> None:
    price = ModelPrice(input=2.0, output=10.0)
    usage = {"cache_read_input_tokens": 1_000_000, "cache_creation_input_tokens": 1_000_000}
    assert estimate_cost("x", usage, resolver=lambda _m: price) == pytest.approx(4.0)


def test_a_published_cached_rate_is_used_when_given() -> None:
    price = ModelPrice(input=2.0, cached_input=0.2, cache_write=2.5, output=10.0)
    usage = {"cache_read_input_tokens": 1_000_000, "cache_creation_input_tokens": 1_000_000}
    assert estimate_cost("x", usage, resolver=lambda _m: price) == pytest.approx(2.7)


def test_reasoning_is_not_priced_unless_a_row_asks_for_it() -> None:
    """Normalized usage folds reasoning into output; pricing it again would
    double-count, so the field is opt-in."""
    usage = {"output_tokens": 1_000_000, "reasoning_tokens": 500_000}
    folded = ModelPrice(input=1.0, output=10.0)
    assert estimate_cost("x", usage, resolver=lambda _m: folded) == pytest.approx(10.0)
    separate = ModelPrice(input=1.0, output=10.0, reasoning=4.0)
    assert estimate_cost("x", usage, resolver=lambda _m: separate) == pytest.approx(12.0)


# ─── The engine seam ─────────────────────────────────────────────────────────


class _NoPriceProvider:
    """A provider with no ``estimate_cost`` at all — the xAI/GLM situation."""

    name = "openrouter"

    def __init__(self) -> None:
        from fluid.providers.base import ProviderCaps

        self.caps = ProviderCaps()

    async def stream(self, request):
        from fluid.providers.base import StreamChunk

        yield StreamChunk("message_start", {"usage": {"input_tokens": 1_000_000}})
        yield StreamChunk("block_start", {"index": 0, "block_type": "text"})
        yield StreamChunk("text_delta", {"index": 0, "text": "ok"})
        yield StreamChunk("block_stop", {"index": 0})
        yield StreamChunk(
            "usage",
            {"usage": {"output_tokens": 1_000_000}, "stop_reason": "end_turn"},
        )
        yield StreamChunk("message_stop", {})


async def _run(model: str, **kwargs):
    import asyncio

    from fluid.engine.native import NativeLoop
    from fluid.events import TurnComplete, UserTurn
    from fluid.store.base import MemoryStore

    loop = NativeLoop(
        provider=_NoPriceProvider(), store=MemoryStore(), model=model, **kwargs
    )
    await loop.submit(UserTurn(content="hello"))
    await asyncio.wait_for(loop.wait_turn(), timeout=5)
    return next(
        e for e in loop.bus.log(loop.session_id) if isinstance(e, TurnComplete)
    )


async def test_the_engine_prices_an_unpriced_provider_from_the_host_resolver() -> None:
    done = await _run(
        "z-ai/glm-5.3-flash",
        pricing=lambda _m: {"input": 0.30, "output": 1.20},
    )
    assert done.cost_usd == pytest.approx(1.50)


async def test_the_engine_reports_no_cost_when_nobody_can_price_the_model() -> None:
    done = await _run("z-ai/glm-4-32b")
    assert done.cost_usd is None


async def test_the_per_call_usage_event_carries_the_estimate_too() -> None:
    """Not just the turn total: each call's own Usage row is what the host
    aggregates, and it must say the number was estimated, not billed."""
    import asyncio

    from fluid.engine.native import NativeLoop
    from fluid.events import Usage, UserTurn
    from fluid.store.base import MemoryStore

    loop = NativeLoop(
        provider=_NoPriceProvider(), store=MemoryStore(),
        model="grok-4.6", pricing=lambda _m: {"input": 0.20, "output": 1.00},
    )
    await loop.submit(UserTurn(content="hello"))
    await asyncio.wait_for(loop.wait_turn(), timeout=5)
    usage = next(e for e in loop.bus.log(loop.session_id) if isinstance(e, Usage))
    assert usage.raw["cost_usd"] == pytest.approx(1.20)
    assert usage.raw["cost_estimated"] is True
