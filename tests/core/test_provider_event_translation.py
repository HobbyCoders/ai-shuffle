"""Provider-event compatibility seam preserves all host message fields.

Actual CLI frames are mapped by Fluid and exercised in delegated producer and
harness-adapter tests. The retired SDK is not a second message vocabulary.
"""

from __future__ import annotations

import app.core.provider.events as sdk
from app.core.provider.events import (
    HookEventMessage,
    RateLimitEvent,
    RateLimitInfo,
    StreamEvent,
    TaskNotificationMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
)

from app.core.provider import events as pe
from app.core.provider.sdk_event_translation import to_provider_event


def test_assistant_message_round_trip():
    m = sdk.AssistantMessage(
        content=[
            sdk.TextBlock(text="hi"),
            sdk.ThinkingBlock(thinking="hmm", signature="sig"),
            sdk.ToolUseBlock(id="t1", name="Bash", input={"command": "ls"}),
            sdk.ToolResultBlock(tool_use_id="t1", content="out", is_error=False),
        ],
        model="claude-x",
        parent_tool_use_id="parent-1",
        usage={"input_tokens": 5},
    )
    e = to_provider_event(m)
    assert type(e) is pe.AssistantMessage
    assert e.model == "claude-x" and e.parent_tool_use_id == "parent-1"
    assert e.usage == {"input_tokens": 5}
    text, think, tu, tr = e.content
    assert type(text) is pe.TextBlock and text.text == "hi"
    assert type(think) is pe.ThinkingBlock and think.signature == "sig"
    assert type(tu) is pe.ToolUseBlock and tu.id == "t1" and tu.input == {"command": "ls"}
    assert type(tr) is pe.ToolResultBlock and tr.tool_use_id == "t1" and tr.is_error is False


def test_user_message_round_trip_and_string_content():
    m = sdk.UserMessage(
        content=[sdk.ToolResultBlock(tool_use_id="t2", content=[{"type": "text"}])],
        parent_tool_use_id="p",
    )
    e = to_provider_event(m)
    assert type(e) is pe.UserMessage and e.parent_tool_use_id == "p"
    assert type(e.content[0]) is pe.ToolResultBlock

    s = to_provider_event(sdk.UserMessage(content="plain"))
    assert type(s) is pe.UserMessage and s.content == "plain"


def test_system_and_result_round_trip():
    e = to_provider_event(sdk.SystemMessage(subtype="init", data={"session_id": "abc"}))
    assert type(e) is pe.SystemMessage and e.data["session_id"] == "abc"

    r = to_provider_event(
        sdk.ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=2, is_error=False,
            num_turns=3, session_id="s", total_cost_usd=0.5,
            usage={"input_tokens": 9}, result="ok", terminal_reason="completed",
        )
    )
    assert type(r) is pe.ResultMessage
    assert r.num_turns == 3 and r.total_cost_usd == 0.5
    assert r.terminal_reason == "completed" and r.usage == {"input_tokens": 9}


def test_stream_event_round_trip():
    raw = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "x"}}
    e = to_provider_event(
        StreamEvent(uuid="u", session_id="s", event=raw, parent_tool_use_id="pt")
    )
    assert type(e) is pe.StreamEvent
    assert e.event is raw  # by reference, not copied
    assert e.parent_tool_use_id == "pt"


def test_task_and_hook_messages_preserve_system_subclassing():
    tn = to_provider_event(
        TaskNotificationMessage(
            subtype="task_notification", data={"x": 1}, task_id="t", status="completed",
            output_file="", summary="done", uuid="u", session_id="s", tool_use_id="tu",
        )
    )
    assert type(tn) is pe.TaskNotificationMessage
    assert isinstance(tn, pe.SystemMessage)  # dispatch-order dependency
    assert tn.status == "completed" and tn.tool_use_id == "tu" and tn.data == {"x": 1}

    ts = to_provider_event(
        TaskStartedMessage(
            subtype="task_started", data={}, task_id="t", description="d",
            uuid="u", session_id="s",
        )
    )
    assert type(ts) is pe.TaskStartedMessage and isinstance(ts, pe.SystemMessage)

    tu = to_provider_event(
        TaskUpdatedMessage(
            subtype="task_updated", data={}, task_id="t", patch={"status": "killed"},
        )
    )
    assert type(tu) is pe.TaskUpdatedMessage and tu.patch == {"status": "killed"}

    he = to_provider_event(
        HookEventMessage(
            subtype="hook_started", data={"k": 1}, hook_event_name="PreToolUse",
        )
    )
    assert type(he) is pe.HookEventMessage and isinstance(he, pe.SystemMessage)
    assert he.hook_event_name == "PreToolUse"


def test_rate_limit_round_trip():
    e = to_provider_event(
        RateLimitEvent(
            rate_limit_info=RateLimitInfo(
                status="allowed_warning", resets_at=123,
                rate_limit_type="five_hour", utilization=0.9,
            ),
            uuid="u", session_id="s",
        )
    )
    assert type(e) is pe.RateLimitEvent
    info = e.rate_limit_info
    assert type(info) is pe.RateLimitInfo
    assert info.status == "allowed_warning" and info.resets_at == 123
    assert info.rate_limit_type == "five_hour" and info.utilization == 0.9


def test_unknown_message_passes_through():
    class Weird:
        pass

    w = Weird()
    assert to_provider_event(w) is w


def test_provider_event_passthrough_preserves_future_fields():
    event = pe.ResultMessage(subtype="success", session_id="s", usage={"future": 7})
    event.future_field = {"opaque": [1, 2]}
    assert to_provider_event(event) is event
    assert to_provider_event(event).future_field == {"opaque": [1, 2]}
