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
