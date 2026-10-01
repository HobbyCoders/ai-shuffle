"""Per-call usage on the delegated leg, in the native loop's record shape."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

from fluid.engine import delegated_proto
from fluid.engine.delegated import DelegatedLoop
from fluid.engine.delegated_proto import StreamJsonMapper, per_call_usage
from fluid.events.model import ErrorEvent, RateLimit, TurnComplete, Usage
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
    result = {"type": "result", "subtype": "success", "num_turns": 2,
              "total_cost_usd": 0.01, "usage": {"output_tokens": 60}}
    # Each call's Usage is held until its counts are final: the first goes
    # out when the second call starts, the second with the result.
    usages = [
        e for frame in (_assistant("msg_1", first), _assistant("msg_2", second), result)
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


def test_a_rate_limit_frame_with_no_utilization_warns_once_per_shape(
    caplog, monkeypatch,
) -> None:
    """A CLI that moves utilization somewhere the mapper does not read must
    not drop every usage reading silently (the pre-2.1.285 mapper did)."""
    monkeypatch.setattr(delegated_proto, "_UNREADABLE_RATE_LIMIT_SHAPES", set())
    caplog.set_level(logging.WARNING, logger="fluid.engine.delegated_proto")
    mapper = StreamJsonMapper(session_id="s", turn_id="t", model="opus", user_node_id="n")
    frame = {"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed", "rateLimitType": "five_hour", "resetsAt": 1790754000,
        "newShapeWindows": [{"pct": 6}],
    }}
    got = [e for e in mapper.feed(frame).events if isinstance(e, RateLimit)]
    # Nothing is invented: the one reading carries no utilization.
    assert [(e.kind, e.utilization) for e in got] == [("five_hour", None)]
    (warning,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    message = warning.getMessage()
    for key in ("newShapeWindows", "rateLimitType", "resetsAt", "status"):
        assert key in message
    mapper.feed(frame)
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_readable_or_rejected_rate_limit_frames_do_not_warn(caplog, monkeypatch) -> None:
    monkeypatch.setattr(delegated_proto, "_UNREADABLE_RATE_LIMIT_SHAPES", set())
    caplog.set_level(logging.WARNING, logger="fluid.engine.delegated_proto")
    mapper = StreamJsonMapper(session_id="s", turn_id="t", model="opus", user_node_id="n")
    for info in (
        {"status": "allowed", "rateLimitType": "five_hour", "utilization": 0.2},
        {"status": "rejected", "rateLimitType": "five_hour", "resetsAt": 1900000000},
        {"status": "allowed", "rateLimitType": "five_hour", "unifiedWindows": {
            "five_hour": {"utilization": 0.06, "resetsAt": 1790754000}}},
    ):
        mapper.feed({"type": "rate_limit_event", "rate_limit_info": info})
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


# ─── Per-call output tokens ─────────────────────────────────────────────────


def _stream(event: dict) -> dict:
    return {"type": "stream_event", "session_id": "cli", "event": event}


def test_per_call_output_comes_from_the_message_delta() -> None:
    """The assistant frame repeats the message_start usage (output 4); the
    call's real output (812) arrives on the partial stream's message_delta."""
    mapper = StreamJsonMapper(session_id="s", turn_id="t", model="opus", user_node_id="n")
    start_usage = {"input_tokens": 30, "output_tokens": 4,
                   "cache_read_input_tokens": 9000, "cache_creation_input_tokens": 100}
    frames = [
        _stream({"type": "message_start", "message": {"id": "msg_1", "usage": start_usage}}),
        _stream({"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}),
        _stream({"type": "content_block_delta", "index": 0,
                 "delta": {"type": "text_delta", "text": "done"}}),
        _stream({"type": "content_block_stop", "index": 0}),
        _assistant("msg_1", start_usage, text="done"),
        _stream({"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                 "usage": {"output_tokens": 812}}),
        _stream({"type": "message_stop"}),
        {"type": "result", "subtype": "success", "num_turns": 1, "total_cost_usd": 0.05,
         "usage": {**start_usage, "output_tokens": 812}},
    ]
    events = [e for frame in frames for e in mapper.feed(frame).events]
    (usage,) = [e for e in events if isinstance(e, Usage)]
    assert usage.output_tokens == 812
    assert usage.input_tokens == 30 and usage.cache_read_tokens == 9000
    assert usage.raw["model_call_index"] == 1
    assert "output_reconciled" not in usage.raw
    assert usage.raw["cost_usd"] == pytest.approx(
        estimate_cost(MODEL, {**start_usage, "output_tokens": 812})
    )
    # The Usage precedes the turn's end.
    assert isinstance(events[-1], TurnComplete)


def test_a_message_delta_before_the_assistant_frame_still_lands() -> None:
    mapper = StreamJsonMapper(session_id="s", turn_id="t", model="opus", user_node_id="n")
    start_usage = {"input_tokens": 30, "output_tokens": 1}
    frames = [
        _stream({"type": "message_start", "message": {"id": "msg_1", "usage": start_usage}}),
        _stream({"type": "message_delta", "delta": {}, "usage": {"output_tokens": 300}}),
        _assistant("msg_1", start_usage),
    ]
    usages = [e for f in frames for e in mapper.feed(f).events if isinstance(e, Usage)]
    assert [(u.output_tokens, u.raw["model_call_index"]) for u in usages] == [(300, 1)]


def test_a_message_delta_completes_only_its_own_messages_call() -> None:
    """A delta belongs to the latest message_start. When a second stream
    starts before the first message's frame lands (parallel subagents), the
    second message's final count must not complete the first one's call."""
    mapper = StreamJsonMapper(session_id="s", turn_id="t", model="opus", user_node_id="n")
    a_usage = {"input_tokens": 30, "output_tokens": 1}
    b_usage = {"input_tokens": 50, "output_tokens": 2}
    frames = [
        _stream({"type": "message_start", "message": {"id": "msg_a", "usage": a_usage}}),
        _stream({"type": "message_start", "message": {"id": "msg_b", "usage": b_usage}}),
        _assistant("msg_a", a_usage),
        _stream({"type": "message_delta", "delta": {}, "usage": {"output_tokens": 300}}),
        _assistant("msg_b", b_usage),
    ]
    usages = [e for f in frames for e in mapper.feed(f).events if isinstance(e, Usage)]
    assert [(u.input_tokens, u.output_tokens) for u in usages] == [(30, 1), (50, 300)]


def test_a_subagents_model_does_not_price_the_turn() -> None:
    """With no cost baseline (first turn after --resume) the turn is priced
    from the table, at the main thread's model, even when the last frame
    came from a cheaper subagent."""
    mapper = StreamJsonMapper(session_id="s", turn_id="t", model="opus", user_node_id="n",
                              cost_baseline=None)
    usage = {"input_tokens": 1000, "output_tokens": 500}
    sub = _assistant("msg_2", {"input_tokens": 10, "output_tokens": 5})
    sub["parent_tool_use_id"] = "toolu_task"
    sub["message"]["model"] = "claude-haiku-4-5"
    frames = [
        _assistant("msg_1", {"input_tokens": 990, "output_tokens": 495}),
        sub,
        {"type": "result", "subtype": "success", "num_turns": 2, "total_cost_usd": 9.0,
         "usage": usage},
    ]
    events = [e for f in frames for e in mapper.feed(f).events]
    assert mapper.reported_model == MODEL
    (done,) = [e for e in events if isinstance(e, TurnComplete)]
    assert done.cost_usd == pytest.approx(estimate_cost(MODEL, usage))


def test_without_the_partial_stream_the_result_settles_the_last_call() -> None:
    """No message_delta: per-call outputs still add up to the turn's own."""
    mapper = StreamJsonMapper(session_id="s", turn_id="t", model="opus", user_node_id="n")
    frames = [
        _assistant("msg_1", {"input_tokens": 100, "output_tokens": 3}),
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
        _assistant("msg_2", {"input_tokens": 120, "output_tokens": 2}),
        {"type": "result", "subtype": "success", "num_turns": 2, "total_cost_usd": 0.02,
         "usage": {"input_tokens": 220, "output_tokens": 900}},
    ]
    usages = [e for f in frames for e in mapper.feed(f).events if isinstance(e, Usage)]
    assert [u.raw["model_call_index"] for u in usages] == [1, 2]
    assert sum(u.output_tokens for u in usages) == 900
    assert usages[0].output_tokens == 3  # its snapshot is all anyone knows
    assert usages[1].output_tokens == 897 and usages[1].raw["output_reconciled"] is True
    assert usages[1].raw["cost_usd"] == pytest.approx(
        estimate_cost(MODEL, {"input_tokens": 120, "output_tokens": 897})
    )


def test_a_result_never_lowers_a_calls_own_count() -> None:
    mapper = StreamJsonMapper(session_id="s", turn_id="t", model="opus", user_node_id="n")
    frames = [
        _assistant("msg_1", {"input_tokens": 10, "output_tokens": 50}),
        {"type": "result", "subtype": "success", "num_turns": 1, "total_cost_usd": 0.0,
         "usage": {"output_tokens": 20}},
    ]
    usages = [e for f in frames for e in mapper.feed(f).events if isinstance(e, Usage)]
    assert [u.output_tokens for u in usages] == [50]


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
        # The CLI's own running total, kept next to the turn's share of it.
        "cli_total_cost_usd": 0.00042,
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
