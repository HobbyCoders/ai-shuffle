"""The Claude CLI's live thinking estimate reaches the host, and only live."""

from __future__ import annotations

from fluid.engine import delegated_proto
from fluid.engine.delegated_proto import StreamJsonMapper
from fluid.events.model import ThinkingProgress
from fluid.store.base import RECONSTRUCTIBLE_EVENTS


def _frame(estimate, **extra) -> dict:
    return {"type": "system", "subtype": "thinking_tokens", "uuid": "u", "estimated_tokens": estimate, **extra}


def _mapper() -> StreamJsonMapper:
    return StreamJsonMapper(session_id="s", turn_id="t", model="claude-opus-5-5", user_node_id="n")


def test_estimate_becomes_a_progress_event():
    events = _mapper().feed(_frame(12400)).events
    assert len(events) == 1
    assert isinstance(events[0], ThinkingProgress)
    assert events[0].estimated_tokens == 12400


def test_estimates_are_throttled(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(delegated_proto.time, "monotonic", lambda: clock[0])
    mapper = _mapper()
    assert mapper.feed(_frame(10)).events
    clock[0] += delegated_proto._THINKING_PROGRESS_SECONDS - 0.01
    assert not mapper.feed(_frame(20)).events
    clock[0] += 0.02
    assert mapper.feed(_frame(30)).events


def test_subagent_and_unusable_estimates_are_dropped():
    assert not _mapper().feed(_frame(50, parent_tool_use_id="tool-1")).events
    for bad in (0, -1, "12", True, None):
        assert not _mapper().feed(_frame(bad)).events


def test_progress_is_never_stored():
    assert ThinkingProgress in RECONSTRUCTIBLE_EVENTS
