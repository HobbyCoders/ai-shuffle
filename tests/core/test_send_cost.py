"""The cold-send verdict the composer warns on (#1831): a large context and a
provider cache older than its lifetime for the chat's leg."""

import pytest

from app.core import send_cost

NOW = 1_800_000_000.0
LARGE = send_cost.LARGE_CONTEXT_TOKENS


@pytest.mark.parametrize("leg,idle,tokens,warn", [
    # Claude Code keeps 1-hour cache entries.
    ("delegated", 59 * 60, LARGE * 3, False),
    ("delegated", 61 * 60, LARGE * 3, True),
    ("delegated", 61 * 60, LARGE - 1, False),
    # Native legs follow fluid's five-minute cold gap.
    ("openai_responses", 4 * 60, LARGE, False),
    ("openai_responses", 6 * 60, LARGE, True),
    ("anthropic", 6 * 60, LARGE, True),
    ("openrouter", 6 * 60, LARGE - 1, False),
    # No cache lifetime known: never a warning.
    ("voice", 99999, LARGE * 5, False),
    (None, 99999, LARGE * 5, False),
])
def test_thresholds(leg, idle, tokens, warn):
    verdict = send_cost.assess(leg=leg, context_tokens=tokens, last_call_at=NOW - idle, now=NOW)
    assert verdict["warn"] is warn
    assert verdict["threshold_tokens"] == LARGE


def test_no_recorded_call_is_not_cold():
    verdict = send_cost.assess(leg="delegated", context_tokens=LARGE * 2, last_call_at=None, now=NOW)
    assert verdict["cold"] is False and verdict["warn"] is False


def test_cache_state_reads_the_snapshot_then_the_last_call(monkeypatch):
    from app.core import limit_resume
    from app.db import database

    monkeypatch.setattr(limit_resume, "session_leg", lambda s: "delegated")
    monkeypatch.setattr(send_cost, "_last_call", lambda sid: {
        "at": NOW - 7200,
        "usage": {"input_tokens": 4, "cache_read_input_tokens": 300_000,
                  "cache_creation_input_tokens": 5_000},
    })
    monkeypatch.setattr(database, "get_session_context_usage_snapshot",
                        lambda sid: {"data": {"totalTokens": 410_000}})
    state = send_cost.cache_state({"id": "s1"}, now=NOW)
    assert state["context_tokens"] == 410_000 and state["warn"] is True

    monkeypatch.setattr(database, "get_session_context_usage_snapshot", lambda sid: None)
    state = send_cost.cache_state({"id": "s1"}, now=NOW)
    assert state["context_tokens"] == 305_004 and state["idle_seconds"] == 7200
    assert state["cost_comparison"]["model"] == "claude-sonnet-5"
    assert state["cost_comparison"]["compacted_context_tokens"] == 61_001

@pytest.mark.parametrize("leg,expected_compaction,recommendation", [
    ("delegated", 1.68, "send"),
    ("openai_responses", 0.0, "compact"),
])
def test_totals_include_compaction_and_same_reply_budget(leg, expected_compaction, recommendation):
    from fluid.providers.pricing import ModelPrice

    # A cheap cache read must not be used for either cold path.
    price = ModelPrice(input=2, cached_input=0.01, cache_write=4, output=10)
    result = send_cost.compare_costs(
        model="fixture", leg=leg, context_tokens=400_000,
        compacted_context_tokens=80_000, reply_tokens=4_000, summary_tokens=8_000,
        resolver=lambda model: price,
    )
    input_rate = 4 if leg == "delegated" else 2
    assert result["cold_resume_usd"] == pytest.approx(0.4 * input_rate + 0.04)
    assert result["compaction_usd"] == pytest.approx(expected_compaction)
    assert result["continued_usd"] == pytest.approx(0.08 * input_rate + 0.04)
    assert result["compact_continue_usd"] == pytest.approx(
        result["compaction_usd"] + result["continued_usd"])
    assert result["recommendation"] == recommendation


def test_model_without_cache_pricing_uses_full_input_not_free_cache():
    from fluid.providers.pricing import ModelPrice

    result = send_cost.compare_costs(
        model="fixture", leg="delegated", context_tokens=400_000,
        compacted_context_tokens=80_000, reply_tokens=4_000, summary_tokens=8_000,
        resolver=lambda model: ModelPrice(input=2, output=10),
    )
    assert result["cold_resume_usd"] == pytest.approx(0.84)
    assert result["compaction_usd"] == pytest.approx(0.88)
    assert result["compact_continue_usd"] == pytest.approx(1.08)


def test_delegated_alias_uses_sourced_one_hour_cache_write_pricing(monkeypatch):
    from app.core.provider import harness_adapter

    monkeypatch.setattr(harness_adapter, "_catalog_pricing_resolver", lambda options: None)
    result = send_cost._comparison(
        {"model": "sonnet"}, "delegated", 400_000,
        {"totalTokens": 400_000, "categories": [{"name": "Messages", "tokens": 300_000}]},
        {"usage": {"output_tokens": 2_000}},
    )
    assert result["model"] == "claude-sonnet-5"
    assert result["compacted_context_tokens"] == 108_000  # fixed overhead + summary
    assert result["cold_resume_usd"] == pytest.approx(1.62)
    assert result["compaction_usd"] == pytest.approx(1.68)
    assert result["continued_usd"] == pytest.approx(0.452)
    assert result["compact_continue_usd"] == pytest.approx(2.132)


def test_composite_model_uses_the_bound_catalog_rates(monkeypatch):
    from app.core.provider import harness_adapter

    def catalog(options):
        assert options.extra["provider_id"] == "p1"
        return lambda model: {"input": 7, "output": 13}

    monkeypatch.setattr(harness_adapter, "_catalog_pricing_resolver", catalog)
    result = send_cost._comparison(
        {"model": "prov:p1:custom-model"}, "openrouter", 400_000, {}, None,
    )
    assert result["model"] == "custom-model"
    assert result["cold_resume_usd"] == pytest.approx(2.852)
    assert result["compaction_usd"] == 0
    assert result["compact_continue_usd"] == pytest.approx(0.612)


@pytest.mark.parametrize("rates", [None, {"input": 2}, {"output": 10}])
def test_missing_or_partial_prices_leave_comparison_unavailable(rates):
    assert send_cost.compare_costs(
        model="unpriced-fixture", leg="delegated", context_tokens=400_000,
        compacted_context_tokens=80_000, reply_tokens=4_000, summary_tokens=8_000,
        resolver=lambda model: rates,
    ) is None


def test_cache_state_prices_persisted_context_and_usage(mock_db):
    from datetime import datetime, timezone

    from app.db import database, session_events

    database.create_session("cost-session", "default", model="sonnet")
    database.set_session_context_usage("cost-session", {
        "totalTokens": 400_000,
        "categories": [{"name": "Messages", "tokens": 300_000}],
    })
    session_events.append_events("cost-session", [{
        "type": "turn.usage", "payload": {"usage": {
            "input_tokens": 20, "cache_read_input_tokens": 399_980,
            "output_tokens": 2_000,
        }},
    }])
    from app.db.base import get_db

    with get_db() as conn:
        conn.cursor().execute("UPDATE session_events SET created_at=%s WHERE session_id=%s", (
            datetime.fromtimestamp(NOW - 7200, timezone.utc).isoformat(), "cost-session"))
    state = send_cost.cache_state(database.get_session("cost-session"), now=NOW)
    assert state["warn"] is True
    result = state["cost_comparison"]
    assert result["model"] == "claude-sonnet-5"
    assert result["reply_tokens"] == 2_000
    assert result["cold_resume_usd"] == pytest.approx(1.62)
    assert result["compact_continue_usd"] == pytest.approx(2.132)


def test_long_context_tier_prices_reply_with_its_prompt():
    result = send_cost.compare_costs(
        model="claude-sonnet-4-5[1m]", leg="delegated", context_tokens=400_000,
        compacted_context_tokens=80_000, reply_tokens=4_000, summary_tokens=8_000,
    )
    assert result["cold_resume_usd"] == pytest.approx(4.89)
    assert result["compaction_usd"] == pytest.approx(4.98)
    assert result["continued_usd"] == pytest.approx(0.54)
