"""Unit tests for the background sub-agent inner-output helpers in query_engine.

Background sub-agents (Task/Agent launched with ``run_in_background``) used to
render "completed / no activity recorded": the SDK's immediate launch-ACK
ToolResult was treated as the subagent's FINAL result (firing ``subagent_done``),
and the real inner activity — which arrives later on the pooled client, after the
parent turn's ``receive_response`` has returned — was never re-dispatched.

These cover the two pure helpers behind the fix:
- ``_looks_like_subagent_launch_ack``: content-sniff fallback for the launch ACK.
- ``_subagent_events_from_message``: converts a drained inner message into the
  ``subagent_*`` events the live store / FE understand (keyed by the parent
  ``tool_use_id`` so they land in the right inner group).
"""

from claude_agent_sdk import (
    AssistantMessage,
    ToolResultBlock,
    ToolUseBlock,
    TextBlock,
    UserMessage,
)

from app.core.query_engine import (
    _looks_like_subagent_launch_ack,
    _subagent_events_from_message,
)


class TestLooksLikeLaunchAck:
    def test_matches_async_launch_ack_text(self):
        ack = (
            "Async agent launched successfully. agentId: a2c0deadbeef. "
            "The agent is working in the background. You will be notified "
            "automatically when it completes."
        )
        assert _looks_like_subagent_launch_ack(ack) is True

    def test_real_result_is_not_ack(self):
        assert (
            _looks_like_subagent_launch_ack(
                "All three commands ran successfully: STEP-1 then STEP-2."
            )
            is False
        )

    def test_handles_none_and_non_string(self):
        assert _looks_like_subagent_launch_ack(None) is False
        # str() of a list still gets sniffed (defensive)
        assert (
            _looks_like_subagent_launch_ack(
                [{"type": "text", "text": "running in the background"}]
            )
            is True
        )


class TestSubagentEventsFromMessage:
    def test_no_parent_tool_id_returns_empty(self):
        msg = AssistantMessage(
            content=[TextBlock("hi")], model="claude", parent_tool_use_id=None
        )
        assert _subagent_events_from_message(msg) == []

    def test_assistant_text_becomes_subagent_chunk(self):
        msg = AssistantMessage(
            content=[TextBlock("STEP-1 done")],
            model="claude",
            parent_tool_use_id="tu-task",
        )
        assert _subagent_events_from_message(msg) == [
            {"type": "subagent_chunk", "agent_id": "tu-task", "content": "STEP-1 done"}
        ]

    def test_assistant_tool_use_becomes_subagent_tool_use(self):
        msg = AssistantMessage(
            content=[ToolUseBlock(id="t1", name="Bash", input={"command": "echo hi"})],
            model="claude",
            parent_tool_use_id="tu-task",
        )
        assert _subagent_events_from_message(msg) == [
            {
                "type": "subagent_tool_use",
                "agent_id": "tu-task",
                "name": "Bash",
                "id": "t1",
                "input": {"command": "echo hi"},
            }
        ]

    def test_user_tool_result_becomes_subagent_tool_result(self):
        msg = UserMessage(
            content=[ToolResultBlock(tool_use_id="t1", content="hi\n", is_error=False)],
            parent_tool_use_id="tu-task",
        )
        evs = _subagent_events_from_message(msg)
        assert len(evs) == 1
        ev = evs[0]
        assert ev["type"] == "subagent_tool_result"
        assert ev["agent_id"] == "tu-task"
        assert ev["tool_use_id"] == "t1"
        assert ev["is_error"] is False
        assert "hi" in ev["output"]

    def test_mixed_blocks_preserve_order(self):
        msg = AssistantMessage(
            content=[TextBlock("running"), ToolUseBlock(id="t2", name="Read", input={})],
            model="claude",
            parent_tool_use_id="tu-task",
        )
        assert [e["type"] for e in _subagent_events_from_message(msg)] == [
            "subagent_chunk",
            "subagent_tool_use",
        ]

    def test_empty_text_skipped(self):
        msg = AssistantMessage(
            content=[TextBlock("")], model="claude", parent_tool_use_id="tu-task"
        )
        assert _subagent_events_from_message(msg) == []

    def test_string_content_user_message_returns_empty(self):
        # A backgrounded sub-agent inner message with string content has no blocks
        # to convert — must not raise.
        msg = UserMessage(content="plain string", parent_tool_use_id="tu-task")
        assert _subagent_events_from_message(msg) == []
