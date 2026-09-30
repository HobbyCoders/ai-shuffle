"""Per-call usage on the delegated leg, in the native loop's record shape."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from fluid.engine.delegated import DelegatedLoop
from fluid.engine.delegated_proto import StreamJsonMapper, per_call_usage
from fluid.events.model import ErrorEvent, RateLimit, Usage
from fluid.events.ops import UserTurn
from fluid.providers.pricing import estimate_cost
from fluid.store.base import MemoryStore

FAKE = str(Path(__file__).parent.parent / "fixtures" / "fake_claude.py")
MODEL = "claude-opus-4-6"


def _assistant(msg_id: str, usage: dict, text: str = "ok") -> dict:
    return {"type": "assistant", "session_id": "cli", "message": {
        "id": msg_id, "model": MODEL, "stop_reason": "end_turn",
        "content": [{"type": "text", "text": text}], "usage": usage,
    }}


def test_each_call_carries_index_cache_report_and_cost() -> None:
    mapper = StreamJsonMapper(session_id="s", turn_id="t", model="opus", user_node_id="n")
    first = {"input_tokens": 100, "output_tokens": 20,
             "cache_read_input_tokens": 9000, "cache_creation_input_tokens": 900}
    second = {"input_tokens": 50, "output_tokens": 40,
              "cache_read_input_tokens": 9900, "cache_creation_input_tokens": 50}
    usages = [
        e for frame in (_assistant("msg_1", first), _assistant("msg_2", second))
        for e in mapper.feed(frame).events if isinstance(e, Usage)
    ]
    assert [u.raw["model_call_index"] for u in usages] == [1, 2]
    assert usages[0].raw["cache_read_report"]["cached_fraction"] == pytest.approx(0.9, abs=1e-3)
    assert usages[0].raw["cache_read_report"]["verdict"] == "full_hit"
    # The CLI prices only the turn, so the call is estimated from the table,
    # priced by the model the CLI reported for the message.
    assert usages[0].raw["cost_usd"] == pytest.approx(estimate_cost(MODEL, first))
    assert usages[0].raw["cost_from_table"] is True
    # The CLI's own fields survive untouched.
    assert usages[1].raw["input_tokens"] == 50
    assert mapper.model_calls == 2


def test_rate_limit_without_window_type_is_dropped() -> None:
    mapper = StreamJsonMapper(session_id="s", turn_id="t", model="opus", user_node_id="n")
    typed = {"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed", "rateLimitType": "five_hour", "utilization": 0.4}}
    untyped = {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "utilization": 0.4}}
    assert [e.kind for e in mapper.feed(typed).events if isinstance(e, RateLimit)] == ["five_hour"]
    assert not [e for e in mapper.feed(untyped).events if isinstance(e, RateLimit)]


def test_rate_limit_carries_the_cli_verdict() -> None:
    """``status`` survives the mapper; a ``rejected`` window with no utilization
    is still a reading, and a missing or non-string verdict reads as None."""
    mapper = StreamJsonMapper(session_id="s", turn_id="t", model="opus", user_node_id="n")
    frames = [
        {"status": "rejected", "rateLimitType": "five_hour", "resetsAt": 1900000000},
        {"status": "allowed_warning", "rateLimitType": "seven_day", "utilization": 0.81},
        {"rateLimitType": "seven_day_opus", "utilization": 0.1},
        {"status": 3, "rateLimitType": "overage", "utilization": 0.1},
    ]
    got = [
        e for info in frames
        for e in mapper.feed({"type": "rate_limit_event", "rate_limit_info": info}).events
        if isinstance(e, RateLimit)
    ]
    assert [(e.kind, e.status, e.utilization, e.resets_at) for e in got] == [
        ("five_hour", "rejected", None, 1900000000),
        ("seven_day", "allowed_warning", 0.81, None),
        ("seven_day_opus", None, 0.1, None),
        ("overage", None, 0.1, None),
    ]


def test_rate_limit_unified_windows_from_claude_2_1_285() -> None:
    """Shape captured from a live signed-in CLI; no top-level utilization."""
    mapper = StreamJsonMapper(session_id="s", turn_id="t", model="opus", user_node_id="n")
    frame = {"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed", "resetsAt": 1790754000, "rateLimitType": "five_hour",
        "overageStatus": "rejected", "overageDisabledReason": "org_level_disabled",
        "isUsingOverage": False,
        "unifiedWindows": {
            "five_hour": {"utilization": 0.06, "resetsAt": 1790754000},
            "seven_day": {"utilization": 0.4, "resetsAt": 1790978400},
        },
    }}
    got = [e for e in mapper.feed(frame).events if isinstance(e, RateLimit)]
    assert [(e.kind, e.utilization, e.resets_at, e.status) for e in got] == [
        ("five_hour", 0.06, 1790754000, "allowed"),
        ("seven_day", 0.4, 1790978400, None),
    ]


@pytest.mark.parametrize("include_primary", [True, False])
def test_unified_windows_rejection_only_applies_to_primary(include_primary: bool) -> None:
    mapper = StreamJsonMapper(session_id="s", turn_id="t", model="opus", user_node_id="n")
    windows = {"seven_day": {"utilization": 0.4, "resetsAt": 1900000000}}
    if include_primary:
        windows["five_hour"] = {"resetsAt": 1800000000}
    got = mapper.feed({"type": "rate_limit_event", "rate_limit_info": {
        "rateLimitType": "five_hour", "status": "rejected",
        "resetsAt": 1800000000, "unifiedWindows": windows,
    }}).events
    assert {e.kind: e.status for e in got} == {"five_hour": "rejected", "seven_day": None}
    assert len(got) == 2


@pytest.mark.parametrize("windows", [None, [], "bad", {"five_hour": None}])
def test_malformed_unified_windows_preserves_legacy_reading(windows: object) -> None:
    mapper = StreamJsonMapper(session_id="s", turn_id="t", model="opus", user_node_id="n")
    got = mapper.feed({"type": "rate_limit_event", "rate_limit_info": {
        "rateLimitType": "five_hour", "utilization": 0.3, "unifiedWindows": windows,
    }}).events
    assert [(e.kind, e.utilization) for e in got] == [("five_hour", 0.3)]


def test_unpriceable_model_leaves_cost_out() -> None:
    raw = per_call_usage({"input_tokens": 5}, model="no-such-model", index=1)
    assert "cost_usd" not in raw and raw["model_call_index"] == 1


async def test_failed_result_bills_the_turn_on_the_error_event() -> None:
    """The CLI reports usage and cost on a failed result too; the ErrorEvent
    the loop ends the turn with used to carry neither."""
    loop = DelegatedLoop(
        store=MemoryStore(), model="claude-fake", claude_cmd=[sys.executable, FAKE],
        env={"FAKE_CLAUDE_SCENARIO": "n"},
    )
    try:
        await loop.submit(UserTurn(content="hi"))
        await loop.wait_turn()
        events = loop.bus.log(loop.session_id)
    finally:
        await loop.aclose()
    error = events[-1]
    assert isinstance(error, ErrorEvent)
    assert error.cost_usd == pytest.approx(0.00042)
    assert error.usage == {
        "input_tokens": 12, "output_tokens": 5,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
    }
    assert error.num_model_calls == 1


async def test_context_snapshot_carries_its_calls_index() -> None:
    """The snapshot record describes a call already billed; it carries that
    call's index so a host dedupes it instead of counting a second call."""
    loop = DelegatedLoop(
        store=MemoryStore(), model="claude-fake", claude_cmd=[sys.executable, FAKE],
        env={"FAKE_CLAUDE_SCENARIO": "j"},
    )
    try:
        await loop.submit(UserTurn(content="hi"))
        await loop.wait_turn()
        events = loop.bus.log(loop.session_id)
    finally:
        await loop.aclose()
    billed, snapshot = [e for e in events if isinstance(e, Usage)]
    assert billed.raw["model_call_index"] == 1
    assert snapshot.raw == {"model_call_index": 1}
    assert snapshot.context_usage is not None
    assert not loop._snapshot_call_index
