"""Per-call usage on the delegated leg, in the native loop's record shape."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from fluid.engine.delegated import DelegatedLoop
from fluid.engine.delegated_proto import StreamJsonMapper, per_call_usage
from fluid.events.model import ErrorEvent, Usage
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
    assert usages[0].raw["cost_estimated"] is True
    # The CLI's own fields survive untouched.
    assert usages[1].raw["input_tokens"] == 50
    assert mapper.model_calls == 2


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
