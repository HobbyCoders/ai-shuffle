"""A tool call should be visible while it is being written, not only after.

BlockStart already told the client which tool had started, but the arguments
went dark until the whole call had been generated — for a Write carrying a large
file that is many silent seconds on exactly the call that most needs progress.
fluid now emits the argument fragments; these pin the rest of the path, which
already existed for the delegated CLI leg: adapter → input_json_delta → pump →
tool_input stream_delta → recorder → tool.input_delta contract events.
"""

import pytest
from fluid.events import model as lm

from app.core.provider.events import StreamEvent
from app.core.provider.types import SessionOptions
from tests.test_harness_adapter import SID, FakeEngine, _adapter_with, _collect


@pytest.mark.asyncio
async def test_arg_deltas_become_input_json_delta_stream_events():
    events = [
        lm.TurnStarted(session_id=SID, model="m", provider="fake"),
        lm.BlockStart(
            session_id=SID, index=0, block_type="tool_use",
            tool_name="Write", tool_use_id="tu1",
        ),
        lm.ToolArgsDelta(
            session_id=SID, index=0, tool_use_id="tu1", name="Write",
            partial_json='{"file_path":"/a.txt",',
        ),
        lm.ToolArgsDelta(
            session_id=SID, index=0, tool_use_id="tu1", name="Write",
            partial_json='"content":"hi"}',
        ),
        lm.BlockStop(session_id=SID, index=0),
        lm.ToolUse(
            session_id=SID, tool_use_id="tu1", name="Write",
            input={"file_path": "/a.txt", "content": "hi"},
        ),
        lm.TurnComplete(
            session_id=SID, stop_reason="end_turn", num_model_calls=1,
            usage={"input_tokens": 10}, cost_usd=0.01,
        ),
    ]
    adapter = _adapter_with(FakeEngine(events))
    adapter._options = SessionOptions(model="fake-model", include_partial_messages=True)
    got = await _collect(adapter)

    deltas = [
        m for m in got
        if isinstance(m, StreamEvent)
        and (m.event.get("delta") or {}).get("type") == "input_json_delta"
    ]
    assert [d.event["delta"]["partial_json"] for d in deltas] == [
        '{"file_path":"/a.txt",', '"content":"hi"}',
    ]
    # The name rides along so a card opened by the first fragment can label
    # itself rather than rendering an anonymous box.
    assert {d.event["delta"]["name"] for d in deltas} == {"Write"}
    assert {d.event["index"] for d in deltas} == {0}


@pytest.mark.asyncio
async def test_arg_deltas_are_withheld_when_partial_messages_are_off():
    """Non-partial mode gets complete messages only — the deltas would be
    duplicate render content."""
    events = [
        lm.TurnStarted(session_id=SID, model="m", provider="fake"),
        lm.BlockStart(
            session_id=SID, index=0, block_type="tool_use",
            tool_name="Write", tool_use_id="tu1",
        ),
        lm.ToolArgsDelta(
            session_id=SID, index=0, tool_use_id="tu1", name="Write",
            partial_json='{"a":1}',
        ),
        lm.BlockStop(session_id=SID, index=0),
        lm.ToolUse(session_id=SID, tool_use_id="tu1", name="Write", input={"a": 1}),
        lm.TurnComplete(
            session_id=SID, stop_reason="end_turn", num_model_calls=1,
            usage={"input_tokens": 10}, cost_usd=0.01,
        ),
    ]
    adapter = _adapter_with(FakeEngine(events))
    adapter._options = SessionOptions(model="fake-model", include_partial_messages=False)
    got = await _collect(adapter)

    assert not [m for m in got if isinstance(m, StreamEvent)]
