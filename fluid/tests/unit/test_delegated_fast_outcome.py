"""Fast mode on the delegated lane: what a turn asked for and how it really ran.

Claude Code reads the Fast setting once, when a turn starts. A toggle during a
turn is acknowledged at once, and every remaining request keeps the speed the
turn began with (measured on 2.1.316). The frames here are shaped like that
CLI's: ``usage.speed`` on the assistant records, and ``fast_mode_state`` /
``fast_mode_disabled_reason`` on the result.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from fluid.engine.delegated import DelegatedLoop
from fluid.engine.delegated_proto import StreamJsonMapper
from fluid.events import ErrorEvent, FastModeOutcome, TurnComplete, UserTurn
from fluid.events.model import Interrupted
from fluid.store.base import MemoryStore

FAKE = Path(__file__).parent.parent / "fixtures" / "fake_claude_script.py"
_TERMINALS = (TurnComplete, Interrupted, ErrorEvent)
MISSING = object()


def _usage(speed=MISSING) -> dict:
    usage = {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": 0, "service_tier": "standard"}
    return usage if speed is MISSING else {**usage, "speed": speed}


def _assistant(msg_id: str, speed=MISSING, *, parent=None, model="claude-opus-4-7") -> dict:
    return {"type": "assistant", "parent_tool_use_id": parent, "session_id": "cli-sess-1",
            "message": {"id": msg_id, "type": "message", "role": "assistant", "model": model,
                        "content": [{"type": "text", "text": "ok"}], "stop_reason": None,
                        "usage": _usage(speed)}}


def _result(*, state="off", reason=None, num_turns=1, usage=None) -> dict:
    frame = {"type": "result", "subtype": "success", "is_error": False, "duration_ms": 12,
             "duration_api_ms": 9, "num_turns": num_turns, "result": "ok",
             "session_id": "cli-sess-1", "total_cost_usd": 0.001,
             "usage": usage if usage is not None else _usage(), "fast_mode_state": state}
    return {**frame, "fast_mode_disabled_reason": reason} if reason else frame


def _outcomes(frames: list[dict], *, fast=False, toggled_to=None) -> list[FastModeOutcome]:
    mapper = StreamJsonMapper(session_id="s", turn_id="t", model="claude-opus-4-7",
                              user_node_id="n", fast_requested=fast)
    if toggled_to is not None:
        mapper.fast_now = toggled_to
    return [e for frame in frames for e in mapper.feed(frame).events if isinstance(e, FastModeOutcome)]


def test_an_ordinary_standard_turn_with_fast_off_reports_nothing():
    # Claude Code tells a host with Fast off a reason on every turn: no refusal.
    assert _outcomes([_assistant("m1", "standard"), _result(reason="sdk_opt_in_required")]) == []


def test_a_request_that_ran_fast_is_reported_even_when_fast_was_not_asked_for():
    [outcome] = _outcomes([_assistant("m1", "fast"), _result(state="on")])
    assert (outcome.requested, outcome.requested_at_end) == (False, False)
    assert (outcome.fast_calls, outcome.standard_calls, outcome.unknown_calls) == (1, 0, 0)
    assert outcome.state == "on"


def test_requested_fast_that_ran_standard_says_so_with_the_cli_reason():
    [outcome] = _outcomes(
        [_assistant("m1", "standard"), _result(reason="extra_usage_disabled")], fast=True)
    assert outcome.requested is True
    assert (outcome.fast_calls, outcome.standard_calls) == (0, 1)
    assert outcome.disabled_reason == "extra_usage_disabled"


def test_an_unknown_reason_is_kept_verbatim():
    [outcome] = _outcomes([_assistant("m1", "standard"), _result(reason="brand_new_reason")], fast=True)
    assert outcome.disabled_reason == "brand_new_reason"


def test_cooldown_state_comes_through():
    [outcome] = _outcomes(
        [_assistant("m1", "fast"), _assistant("m2", "standard"), _result(state="cooldown")], fast=True)
    assert outcome.state == "cooldown"
    assert (outcome.fast_calls, outcome.standard_calls) == (1, 1)


@pytest.mark.parametrize("silent", [None, MISSING, "turbo", 7])
def test_a_null_missing_or_unrecognised_speed_counts_as_unknown(silent):
    [outcome] = _outcomes([_assistant("m1", silent), _assistant("m2", "fast"), _result(state="on")], fast=True)
    assert (outcome.fast_calls, outcome.standard_calls, outcome.unknown_calls) == (1, 0, 1)


def test_an_unknown_speed_alone_with_fast_off_stays_quiet():
    assert _outcomes([_assistant("m1", None), _result()]) == []


@pytest.mark.parametrize("order", [(None, "fast"), ("fast", None), (MISSING, "fast")])
def test_one_call_with_several_records_is_one_call_and_any_speed_settles_it(order):
    frames = [_assistant("m1", speed) for speed in order]
    [outcome] = _outcomes([*frames, _result(state="on")], fast=True)
    assert (outcome.fast_calls, outcome.standard_calls, outcome.unknown_calls) == (1, 0, 0)


def test_subagent_and_synthetic_records_are_not_this_conversations_calls():
    [outcome] = _outcomes([
        _assistant("sub", "standard", parent="toolu_1"),
        _assistant("syn", None, model="<synthetic>"),
        _assistant("m1", "fast"),
        _result(state="on"),
    ], fast=True)
    assert (outcome.fast_calls, outcome.standard_calls, outcome.unknown_calls) == (1, 0, 0)


def test_a_result_that_ran_no_model_says_nothing_about_speed():
    assert _outcomes([_result(num_turns=0)], fast=True) == []


def test_usage_only_on_the_result_stands_in_for_the_turns_one_call():
    [outcome] = _outcomes([_result(state="on", usage=_usage("fast"))], fast=True)
    assert outcome.fast_calls == 1


def test_a_toggle_during_the_turn_is_kept_apart_from_what_the_turn_started_with():
    [outcome] = _outcomes([_assistant("m1", "standard"), _result()], toggled_to=True)
    assert (outcome.requested, outcome.requested_at_end) == (False, True)
    assert outcome.standard_calls == 1


def test_a_second_result_on_one_mapper_starts_from_the_setting_the_first_ended_with():
    mapper = StreamJsonMapper(session_id="s", turn_id="t", model="m", user_node_id="n", fast_requested=False)
    mapper.fast_now = True
    feed = lambda frame: [e for e in mapper.feed(frame).events if isinstance(e, FastModeOutcome)]
    assert feed(_assistant("m1", "standard")) == []
    [first] = feed(_result())
    assert (first.requested, first.requested_at_end, first.standard_calls) == (False, True, 1)
    assert feed(_assistant("m2", "fast")) == []
    [second] = feed(_result(state="on"))
    assert (second.requested, second.requested_at_end, second.fast_calls, second.standard_calls) == (True, True, 1, 0)


# ── Through the real loop and a spawned CLI ──────────────────────────────────


def _step(msg_id: str, speed: str) -> dict:
    return {"op": "frame", "frame": _assistant(msg_id, speed)}


def _loop(tmp_path: Path, turns: list[list[dict]], *, fast=False) -> DelegatedLoop:
    script = tmp_path / "script.json"
    script.write_text(json.dumps({"turns": turns}), encoding="utf-8")
    return DelegatedLoop(
        store=MemoryStore(), model="claude-opus-4-7", permission_mode="default",
        claude_cmd=[sys.executable, str(FAKE)], env={"FAKE_CLAUDE_SCRIPT": str(script)},
        settings=json.dumps({"fastMode": fast}),
    )


async def _send(loop: DelegatedLoop, sid: str, *, during=None) -> list:
    """One turn; ``during`` runs when its first assistant record has arrived."""
    seen: list = []
    start = loop.bus.last_seq(sid)
    first_record = asyncio.Event()

    async def watch() -> None:
        async for event in loop.bus.subscribe(sid, since_seq=start):
            seen.append(event)
            if type(event).__name__ in ("TextDelta", "AssistantText", "ModelServed", "UsageEvent", "Usage"):
                first_record.set()
            if isinstance(event, _TERMINALS):
                first_record.set()
                return

    watcher = asyncio.create_task(watch())
    await asyncio.sleep(0)
    await loop.submit(UserTurn(content="go"))
    if during is not None:
        await asyncio.wait_for(first_record.wait(), 20)
        await during()
    await asyncio.wait_for(watcher, 20)
    await asyncio.wait_for(loop.wait_turn(), 20)
    return seen


async def test_a_toggle_during_a_running_turn_waits_for_the_next_turn(tmp_path):
    # The fake holds the turn open until the second setting, so the first is
    # read back while the turn is certainly still running.
    serve = {"op": "serve", "subtype": "apply_flag_settings", "count": 2, "timeout": 15}
    loop = _loop(tmp_path, [
        [_step("m1", "standard"), serve, _step("m2", "standard"), {"op": "frame", "frame": _result()}],
        [_step("m3", "fast"), {"op": "frame", "frame": _result(state="on")}],
    ])
    waiting: list[bool] = []

    async def toggle() -> None:
        assert loop.fast_waits_for_next_turn() is False
        await loop.set_run_settings(effort=None, fast=True)
        waiting.append(loop.fast_waits_for_next_turn())
        await loop.set_run_settings(effort=None, fast=True)

    try:
        sid = (await loop.store.create_session()).session_id
        loop._session_id = sid
        first = await _send(loop, sid, during=toggle)
        assert waiting == [True]
        # The turn is over: the setting is what the next one starts with.
        assert loop.fast_waits_for_next_turn() is False
        [ran] = [e for e in first if isinstance(e, FastModeOutcome)]
        assert (ran.requested, ran.requested_at_end) == (False, True)
        assert (ran.fast_calls, ran.standard_calls) == (0, 2)

        second = await _send(loop, sid)
        [next_turn] = [e for e in second if isinstance(e, FastModeOutcome)]
        assert (next_turn.requested, next_turn.requested_at_end) == (True, True)
        assert (next_turn.fast_calls, next_turn.standard_calls) == (1, 0)
    finally:
        await loop.aclose()


async def test_toggling_back_during_the_turn_clears_the_wait(tmp_path):
    serve = {"op": "serve", "subtype": "apply_flag_settings", "count": 2, "timeout": 15}
    loop = _loop(tmp_path, [[_step("m1", "standard"), serve, {"op": "frame", "frame": _result()}]])
    waits: list[bool] = []

    async def toggle() -> None:
        await loop.set_run_settings(effort=None, fast=True)
        waits.append(loop.fast_waits_for_next_turn())
        await loop.set_run_settings(effort=None, fast=False)
        waits.append(loop.fast_waits_for_next_turn())

    try:
        sid = (await loop.store.create_session()).session_id
        loop._session_id = sid
        seen = await _send(loop, sid, during=toggle)
        assert waits == [True, False]
        assert [e for e in seen if isinstance(e, FastModeOutcome)] == []
    finally:
        await loop.aclose()


async def test_a_toggle_between_turns_does_not_wait(tmp_path):
    loop = _loop(tmp_path, [
        [_step("m1", "standard"), {"op": "frame", "frame": _result()}],
        [_step("m2", "fast"), {"op": "frame", "frame": _result(state="on")}],
    ])
    try:
        sid = (await loop.store.create_session()).session_id
        loop._session_id = sid
        await _send(loop, sid)
        await loop.set_run_settings(effort=None, fast=True)
        assert loop.fast_waits_for_next_turn() is False
        seen = await _send(loop, sid)
        [ran] = [e for e in seen if isinstance(e, FastModeOutcome)]
        assert (ran.requested, ran.requested_at_end, ran.fast_calls) == (True, True, 1)
    finally:
        await loop.aclose()


async def test_a_loop_launched_with_fast_on_knows_its_turn_asked_for_it(tmp_path):
    loop = _loop(tmp_path, [[_step("m1", "standard"), {"op": "frame", "frame": _result(reason="extra_usage_disabled")}]], fast=True)
    try:
        sid = (await loop.store.create_session()).session_id
        loop._session_id = sid
        [ran] = [e for e in await _send(loop, sid) if isinstance(e, FastModeOutcome)]
        assert (ran.requested, ran.standard_calls, ran.disabled_reason) == (True, 1, "extra_usage_disabled")
    finally:
        await loop.aclose()


async def test_a_refused_toggle_leaves_the_turn_as_it_was(tmp_path):
    script = tmp_path / "script.json"
    script.write_text(json.dumps({
        "controls": {"apply_flag_settings": "error"},
        "turns": [[_step("m1", "standard"), {"op": "serve", "subtype": "apply_flag_settings", "count": 1, "timeout": 15},
                   {"op": "sleep", "s": 0.3}, {"op": "frame", "frame": _result()}]],
    }), encoding="utf-8")
    loop = DelegatedLoop(
        store=MemoryStore(), model="claude-opus-4-7", permission_mode="default",
        claude_cmd=[sys.executable, str(FAKE)], env={"FAKE_CLAUDE_SCRIPT": str(script)},
        settings=json.dumps({"fastMode": False}),
    )

    async def toggle() -> None:
        with pytest.raises(Exception):
            await loop.set_run_settings(effort=None, fast=True)
        assert loop.fast_waits_for_next_turn() is False

    try:
        sid = (await loop.store.create_session()).session_id
        loop._session_id = sid
        seen = await _send(loop, sid, during=toggle)
        assert [e for e in seen if isinstance(e, FastModeOutcome)] == []
    finally:
        await loop.aclose()
