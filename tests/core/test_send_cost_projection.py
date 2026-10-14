"""Native warnings are relative to a fresh, achievable Compact projection."""
from dataclasses import replace

import pytest

from app.core import send_cost


def setup_state(monkeypatch, *, leg="openai_responses", tokens=200_000,
                projection=100_000, captured_at=2, model="fixture"):
    from app.core import limit_resume
    from app.core.provider import harness_adapter
    from app.db import database

    snapshot = {"captured_at": captured_at, "data": {
        "model": model, "totalTokens": tokens,
        "compaction": {"projectedAfterCompactTokens": projection},
    }}
    monkeypatch.setattr(limit_resume, "session_leg", lambda s: leg)
    monkeypatch.setattr(send_cost, "_last_call", lambda sid: {"at": 1, "usage": {}})
    monkeypatch.setattr(harness_adapter, "_catalog_pricing_resolver",
                        lambda options: lambda model: {"input": 2, "output": 10})
    monkeypatch.setattr(database, "get_session_context_usage_snapshot", lambda sid: snapshot)
    return snapshot


@pytest.mark.parametrize("leg", ["openai_responses", "anthropic", "openrouter"])
@pytest.mark.parametrize("projection,warn,threshold", [
    (100_000, True, 150_000),
    (150_000, True, 200_000),  # exactly 25% savings, but not below 150k
    (150_001, False, 200_002),
    (210_000, False, 280_000),
])
def test_projection(monkeypatch, leg, projection, warn, threshold):
    setup_state(monkeypatch, leg=leg, projection=projection)
    state = send_cost.cache_state({"id": "fixture", "model": "fixture"}, now=10_000)
    assert state["warn"] is warn
    assert state["large"] is warn
    assert state["threshold_tokens"] == threshold
    assert projection < threshold
    if warn:
        assert state["cost_comparison"]["compacted_context_tokens"] == projection
        assert state["cost_comparison"]["compaction_usd"] > 0
    else:
        assert state["cost_comparison"] is None


@pytest.mark.parametrize("value", [None, True, -1, float("nan"), float("inf"), "12"])
def test_invalid_projection_preserves_fixed_threshold_warning(monkeypatch, value):
    setup_state(monkeypatch, projection=value)
    state = send_cost.cache_state({"id": "fixture"}, now=10_000)
    assert state["cold"] and state["large"] and state["warn"]
    assert state["threshold_tokens"] == 150_000
    assert state["cost_comparison"]["compacted_context_tokens"] == 40_000
    assert send_cost._native_projection("openrouter", {
        "compaction": {"projectedAfterCompactTokens": value}}) is None


@pytest.mark.parametrize("captured_at", [None, 0, "invalid", float("nan"), float("inf")])
def test_missing_or_stale_capture_preserves_fixed_threshold_warning(monkeypatch, captured_at):
    setup_state(monkeypatch, projection=169_080, tokens=510_310, captured_at=captured_at)
    state = send_cost.cache_state({"id": "fixture"}, now=10_000)
    assert state["cold"] and state["large"] and state["warn"]
    assert state["threshold_tokens"] == 150_000
    assert state["cost_comparison"]["compacted_context_tokens"] == 102_062


def test_new_call_invalidates_previously_current_projection(monkeypatch):
    setup_state(monkeypatch, projection=169_080, tokens=510_310)
    fresh = send_cost.cache_state({"id": "fixture"}, now=10_000)
    assert fresh["warn"] and fresh["threshold_tokens"] == 225_440
    assert fresh["cost_comparison"]["compacted_context_tokens"] == 169_080
    monkeypatch.setattr(send_cost, "_last_call", lambda sid: {"at": 3, "usage": {}})
    stale = send_cost.cache_state({"id": "fixture"}, now=10_000)
    assert stale["warn"] and stale["threshold_tokens"] == 150_000
    assert stale["cost_comparison"]["compacted_context_tokens"] == 102_062


def test_changed_model_invalidates_projection_but_composite_id_matches(monkeypatch):
    setup_state(monkeypatch)
    changed = send_cost.cache_state({"id": "fixture", "model": "other"}, now=10_000)
    matching = send_cost.cache_state({"id": "fixture", "model": "prov:p1:fixture"}, now=10_000)
    assert changed["warn"] and matching["warn"]
    assert changed["cost_comparison"]["compacted_context_tokens"] == 40_000
    assert matching["cost_comparison"]["compacted_context_tokens"] == 100_000


def test_missing_snapshot_preserves_fixed_threshold_warning(monkeypatch):
    from app.db import database

    setup_state(monkeypatch)
    monkeypatch.setattr(database, "get_session_context_usage_snapshot", lambda sid: None)
    monkeypatch.setattr(send_cost, "_last_call", lambda sid: {
        "at": 1, "usage": {"input_tokens": 510_310}})
    state = send_cost.cache_state({"id": "fixture"}, now=10_000)
    assert state["context_tokens"] == 510_310
    assert state["cold"] and state["large"] and state["warn"]
    assert state["threshold_tokens"] == 150_000
    assert state["cost_comparison"]["compacted_context_tokens"] == 102_062


@pytest.mark.parametrize("captured_at", [None, 0, 2])
def test_delegated_still_uses_fixed_threshold_and_cli_ratio(monkeypatch, captured_at):
    setup_state(monkeypatch, leg="delegated", projection=190_000, captured_at=captured_at)
    state = send_cost.cache_state({"id": "fixture", "model": "fixture"}, now=10_000)
    assert state["warn"] and state["threshold_tokens"] == 150_000
    assert state["cost_comparison"]["compacted_context_tokens"] == 40_000


@pytest.mark.parametrize("overhead,window,expected_before,expected_projection", [
    (20_000, 400_000, 370_310, 29_080),
    (160_000, 1_000_000, 510_310, 169_080),
])
def test_real_projector_with_irreducible_overhead(
    monkeypatch, mock_db, overhead, window, expected_before, expected_projection,
):
    """Reproduce the validator with the real compactor, then persist its output."""
    from app.db import database
    from fluid.context.compaction import CompactionConfig, Compactor
    from fluid.messages import Message, Text
    from fluid.store.base import SessionNode

    path = []
    for i in range(70):
        for role in ("user", "assistant"):
            path.append(SessionNode(
                node_id=f"node_{i}_{role}", parent_id=path[-1].node_id if path else None,
                message=Message(role=role, content=[Text(text=f"turn {i}: " + "word " * 2000)]),
            ))
    compactor = Compactor(config=replace(
        CompactionConfig(), summary_enabled=True, keep_recent_turns=1,
        recency_tail_tokens=0, max_resident_span_digests=1000,
        user_turn_text_cap=8000, assistant_text_cap=4000,
    ))
    before = compactor.estimate_view_tokens(path, overhead)
    projection = compactor.projected_after_compact(
        path, overhead_tokens=overhead, context_window=window,
    )
    assert (before, projection) == (expected_before, expected_projection)
    # Keep the real DB reader: the snapshot wrapper is written by the producer.
    from app.core import limit_resume
    from app.core.provider import harness_adapter

    monkeypatch.setattr(limit_resume, "session_leg", lambda s: "openai_responses")
    monkeypatch.setattr(send_cost, "_last_call", lambda sid: {"at": 1, "usage": {}})
    monkeypatch.setattr(harness_adapter, "_catalog_pricing_resolver",
                        lambda options: lambda model: {"input": 2, "output": 10})
    database.create_session("projection-fixture", "default", model="fixture")
    database.set_session_context_usage("projection-fixture", {
        "model": "fixture", "totalTokens": before,
        "compaction": {"projectedAfterCompactTokens": projection},
    })
    state = send_cost.cache_state({"id": "projection-fixture", "model": "fixture"}, now=10_000)
    assert state["warn"]
    assert state["cost_comparison"]["compacted_context_tokens"] == projection
    assert projection < state["threshold_tokens"] <= before
    # Apply the same guarded manual path; no recency rule is weakened to fit 150k.
    compactor.compact_now(path, overhead_tokens=overhead, context_window=window)
    region = compactor.summary_region(path, context_window=window)
    assert region
    applied = compactor.apply_summary(path, (*region[:2], "s" * 16000), overhead_tokens=overhead)
    assert applied and applied.after_tokens < state["threshold_tokens"]
    database.set_session_context_usage("projection-fixture", {
        "model": "fixture", "totalTokens": applied.after_tokens,
        "compaction": {"projectedAfterCompactTokens": compactor.projected_after_compact(
            path, overhead_tokens=overhead, context_window=window,
        )},
    })
    after = send_cost.cache_state({"id": "projection-fixture", "model": "fixture"}, now=10_000)
    assert not after["warn"]
