"""Per-turn usage on the Kimi, MiniMax and OpenCode Messages legs.

The streams below copy the usage shapes each vendor documents for its
Anthropic-compatible endpoint; every value is a made-up test number.
"""
import json

import httpx
import pytest
from fluid.engine.native import NativeLoop
from fluid.events import TurnComplete, Usage, UserTurn
from fluid.providers.anthropic.client import AnthropicClient
from fluid.providers.pricing import compatible_cost
from fluid.store.base import MemoryStore


def sse(start_usage, *delta_usages):
    events = [
        {"type": "message_start", "message": {"id": "msg-test", "role": "assistant", "usage": start_usage}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi"}},
        {"type": "content_block_stop", "index": 0},
        *({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": u} for u in delta_usages),
        {"type": "message_stop"},
    ]
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)


async def run_turn(provider, model, text, *, plan_key=False):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = AnthropicClient(api_key="fake-test-key", compatible_provider=provider, plan_key=plan_key,
                                 user_agent="ai-shuffle/test", http_client=http)
        loop = NativeLoop(provider=client, store=MemoryStore(), model=model)
        try:
            await loop.submit(UserTurn(content="Hello"))
            await loop.wait_turn()
            events = loop.bus.log(loop.session_id)
        finally:
            await loop.aclose()
    assert len(calls) == 1
    usage = [e for e in events if isinstance(e, Usage)]
    done = [e for e in events if isinstance(e, TurnComplete)]
    assert len(usage) == 1 and len(done) == 1
    return usage[0], done[0]


# MiniMax's documented stream: message_start reports the whole prompt as a
# cache read, message_delta the real split (input excludes the cache read).
MINIMAX_START = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0,
                 "cache_read_input_tokens": 1366}
MINIMAX_DELTA = {"input_tokens": 1252, "output_tokens": 213, "cache_creation_input_tokens": 0,
                 "cache_read_input_tokens": 114}


@pytest.mark.parametrize("plan_key", [False, True])
async def test_minimax_final_delta_replaces_placeholder_start(plan_key):
    usage, done = await run_turn("minimax", "MiniMax-M3", sse(MINIMAX_START, MINIMAX_DELTA), plan_key=plan_key)
    assert (usage.input_tokens, usage.cache_read_tokens, usage.output_tokens) == (1252, 114, 213)
    assert usage.cache_creation_tokens == 0
    if plan_key:
        assert done.cost_usd is None or done.cost_usd == 0  # an allowance is not cash
    else:
        expected = (1252 * 0.30 + 114 * 0.06 + 213 * 1.20) / 1_000_000
        assert done.cost_usd == pytest.approx(expected)


# Kimi documents the full usage block on both events, with the TTL split.
KIMI_START = {"input_tokens": 900, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 0,
              "output_tokens": 1}
KIMI_DELTA = {"input_tokens": 900, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 0,
              "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0},
              "output_tokens": 50}


async def test_kimi_api_turn_records_tokens_and_list_price():
    usage, done = await run_turn("kimi", "kimi-k3.1-pro", sse(KIMI_START, KIMI_DELTA))
    assert (usage.input_tokens, usage.cache_read_tokens, usage.output_tokens) == (900, 100, 50)
    assert done.cost_usd == pytest.approx((900 * 2.00 + 100 * 0.20 + 50 * 10.00) / 1_000_000)


async def test_kimi_code_plan_turn_has_tokens_and_no_cash_cost():
    usage, done = await run_turn("kimi", "kimi-for-coding", sse(KIMI_START, KIMI_DELTA), plan_key=True)
    assert (usage.input_tokens, usage.output_tokens) == (900, 50)
    assert not done.cost_usd


def test_kimi_prices_one_hour_cache_writes_at_twice_input():
    five_minute = compatible_cost("kimi", "kimi-k3", {"cache_creation_input_tokens": 1_000_000})
    one_hour = compatible_cost("kimi", "kimi-k3", {"cache_creation_input_tokens": 1_000_000,
                                                   "cache_creation_1h_input_tokens": 1_000_000})
    assert five_minute == pytest.approx(3.00)
    assert one_hour == pytest.approx(6.00)
    assert compatible_cost("kimi", "kimi-k3.1-pro-highspeed", {"output_tokens": 1_000_000}) == pytest.approx(20.00)
    assert compatible_cost("kimi", "kimi-for-coding", {"output_tokens": 1_000_000}) is None


def opencode_delta(cost, **meta):
    return {"input_tokens": 1200, "cache_read_input_tokens": 8000, "cache_creation_input_tokens": 0,
            "output_tokens": 300, "cost": cost, "opencode": {"billing_source": "balance", **meta}}


async def test_opencode_api_turn_records_the_reported_charge():
    # kimi-k3.1-pro has no Console cash price, so only the reported cost can price it.
    usage, done = await run_turn("opencode", "kimi-k3.1-pro",
                                 sse({"input_tokens": 1200, "output_tokens": 1}, opencode_delta(0.00412)))
    assert (usage.input_tokens, usage.cache_read_tokens, usage.output_tokens) == (1200, 8000, 300)
    assert done.cost_usd == pytest.approx(0.00412)
    assert not usage.raw.get("cost_from_table")


async def test_opencode_estimated_cost_is_flagged_as_a_list_price():
    usage, done = await run_turn("opencode", "kimi-k3.1-pro", sse(
        {"input_tokens": 1200}, opencode_delta(0.002, billing_source="byok", estimated=True)))
    assert done.cost_usd == pytest.approx(0.002)
    assert usage.raw.get("cost_from_table")


@pytest.mark.parametrize("cost", [0.5, float("nan"), -1, "0.1", True])
async def test_opencode_go_or_malformed_cost_is_never_cash_spend(cost):
    plan = cost == 0.5
    text = sse({"input_tokens": 1200}, opencode_delta(cost))
    usage, done = await run_turn("opencode", "kimi-k3.1-pro", text, plan_key=plan)
    assert usage.input_tokens == 1200 and usage.output_tokens == 300
    assert done.cost_usd is None or done.cost_usd == 0


async def test_output_only_delta_keeps_the_start_counts():
    # A delta that reports no prompt side is not authoritative: max() merge as before.
    usage, _ = await run_turn("minimax", "MiniMax-M3", sse({"input_tokens": 640, "cache_read_input_tokens": 60},
                                                         {"output_tokens": 9}))
    assert (usage.input_tokens, usage.cache_read_tokens, usage.output_tokens) == (640, 60, 9)


# Kimi: message_start states the TTL split; whether the final delta repeats it varies.
KIMI_1H_START = {"input_tokens": 100, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 1000,
                 "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 1000},
                 "output_tokens": 1}
BASE = (100 * 3.00 + 50 * 15.00) / 1_000_000  # kimi-k3 input and output


@pytest.mark.parametrize("delta,expected", [
    # No breakdown: the start's 1-hour count stands (1000 writes at 2x input).
    ({"cache_creation_input_tokens": 1000}, BASE + 1000 * 6.00 / 1_000_000),
    # An explicit zero 1-hour share: all writes at the 5-minute rate.
    ({"cache_creation_input_tokens": 1000,
      "cache_creation": {"ephemeral_5m_input_tokens": 1000, "ephemeral_1h_input_tokens": 0}},
     BASE + 1000 * 3.00 / 1_000_000),
    # Zero writes in total: no write cost and no 1-hour surcharge.
    ({"cache_creation_input_tokens": 0}, BASE),
    # A smaller final total than the start's 1-hour share caps the surcharge.
    ({"cache_creation_input_tokens": 400}, BASE + 400 * 6.00 / 1_000_000),
])
async def test_kimi_final_delta_keeps_or_replaces_the_one_hour_share(delta, expected):
    delta = {"input_tokens": 100, "cache_read_input_tokens": 0, "output_tokens": 50, **delta}
    _, done = await run_turn("kimi", "kimi-k3", sse(KIMI_1H_START, delta))
    assert done.cost_usd == pytest.approx(expected)


@pytest.mark.parametrize("costs,expected", [
    ((0.002, 0.003), 0.003),  # cumulative: the later figure is the bill
    ((0.003, 0.003), 0.003),  # a repeated figure is not charged twice
    ((0.003, None), 0.003),   # a later delta without a cost adds nothing
])
async def test_opencode_cost_counts_once_per_stream(costs, expected):
    deltas = [opencode_delta(c) if c is not None else {"input_tokens": 1200, "output_tokens": 300}
              for c in costs]
    _, done = await run_turn("opencode", "kimi-k3.1-pro", sse({"input_tokens": 1200}, *deltas))
    assert done.cost_usd == pytest.approx(expected)
