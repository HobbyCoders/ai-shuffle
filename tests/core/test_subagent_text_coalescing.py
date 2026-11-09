"""Current provider messages do not enter the legacy parent-tagged child lane."""
from fluid.events import BlockStop, TextDelta

from app.core.provider.events import AssistantMessage
from app.core.provider.factory import make_adapter
from app.core.provider.types import SessionOptions


def test_current_producer_does_not_emit_legacy_child_messages():
    adapter = make_adapter()
    adapter._options = SessionOptions(model="fake", include_partial_messages=True)
    delta = adapter._translate(TextDelta(session_id="worker", index=1, text="hello"))
    completed = adapter._translate(BlockStop(session_id="worker", index=1))
    final = next(event for event in completed if isinstance(event, AssistantMessage))
    assert delta[0].event["index"] == 1
    assert final.content[0].text == "hello"
    # Even worker-session events have no parent tool id. Real worker progress
    # uses WorkerFeed -> record_drained_event, not this legacy query-engine lane.
    assert all(event.parent_tool_use_id is None for event in delta + completed)
