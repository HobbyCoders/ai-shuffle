"""Native checkpoint projections drive the warning without changing delegated costs."""
import pytest
from app.core import send_cost


@pytest.mark.parametrize("leg,projection,warn,retained", [
    ("openai_responses", 100_000, True, 100_000),
    ("openai_responses", 150_000, True, 150_000),
    ("openai_responses", 150_001, False, None),
    ("openai_responses", 210_000, False, None),
    ("delegated", 190_000, True, 40_000),
])
def test_projection(monkeypatch, leg, projection, warn, retained):
    from app.core import limit_resume
    from app.core.provider import harness_adapter
    from app.db import database
    monkeypatch.setattr(limit_resume, "session_leg", lambda s: leg)
    monkeypatch.setattr(send_cost, "_last_call", lambda sid: {"at": 1, "usage": {}})
    monkeypatch.setattr(harness_adapter, "_catalog_pricing_resolver",
                        lambda options: lambda model: {"input": 2, "output": 10})
    monkeypatch.setattr(database, "get_session_context_usage_snapshot", lambda sid: {
        "data": {"totalTokens": 200_000, "compaction": {"projectedAfterCompactTokens": projection}}})
    state = send_cost.cache_state({"id": "fixture", "model": "fixture"}, now=10_000)
    assert state["warn"] is warn
    if warn:
        assert state["cost_comparison"]["compacted_context_tokens"] == retained
        assert state["cost_comparison"]["compaction_usd"] > 0


@pytest.mark.parametrize("value", [None, True, -1, float("nan"), float("inf"), "12"])
def test_invalid_projection_falls_back(value):
    assert send_cost._native_projection("openrouter", {
        "compaction": {"projectedAfterCompactTokens": value}}) is None
