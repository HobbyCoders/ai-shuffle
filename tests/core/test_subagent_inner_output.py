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

import pytest

from claude_agent_sdk import (
    AssistantMessage,
    TaskNotificationMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    ToolResultBlock,
    ToolUseBlock,
    TextBlock,
    UserMessage,
)

from app.core.query_engine import (
    _looks_like_subagent_launch_ack,
    _record_task_message,
    _subagent_events_from_message,
    _task_update_terminal_status,
)
from app.core.task_registry import task_registry


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


# ---------------------------------------------------------------------------
# TaskUpdatedMessage terminal handling (the killed/stopped-ALONE gap)
# ---------------------------------------------------------------------------
#
# Per the SDK docstring, a background task's terminal state can arrive ONLY as a
# TaskUpdatedMessage (no following TaskNotificationMessage) — notably a TaskStop
# reporting status="killed". Before this fix, _record_task_message ignored
# TaskUpdatedMessage entirely, so a killed/stopped background task left the live
# "Waiting for N background task(s)…" indicator stuck (registry task never
# completed). These cover the pure status reader and the registry side-effect.

def _updated(task_id, *, status=None, patch_status=None, uuid="uu-1"):
    """Build a real SDK TaskUpdatedMessage (so isinstance checks match).

    ``status`` sets the top-level attr; ``patch_status`` sets patch["status"].
    The SDK populates changed fields in ``patch`` and may also surface the status
    on the top-level attr, so we exercise both.
    """
    patch = {"end_time": 123}
    if patch_status is not None:
        patch["status"] = patch_status
    return TaskUpdatedMessage(
        subtype="task_updated",
        data={},
        task_id=task_id,
        patch=patch,
        status=status,
        session_id="sess-1",
        uuid=uuid,
    )


def _notif(task_id, *, status="completed", uuid="u-1", summary="s"):
    return TaskNotificationMessage(
        subtype="task_notification",
        data={},
        task_id=task_id,
        status=status,
        output_file="",
        summary=summary,
        uuid=uuid,
        session_id="sess-1",
        tool_use_id="tu-1",
    )


class TestTaskUpdateTerminalStatus:
    def test_reads_top_level_status(self):
        assert _task_update_terminal_status(_updated("t", status="killed")) == "killed"

    def test_falls_back_to_patch_status(self):
        assert (
            _task_update_terminal_status(_updated("t", status=None, patch_status="completed"))
            == "completed"
        )

    def test_non_terminal_returns_none(self):
        assert _task_update_terminal_status(_updated("t", status="running")) is None
        assert _task_update_terminal_status(_updated("t", status="pending")) is None
        assert _task_update_terminal_status(_updated("t", status="paused")) is None

    def test_no_status_anywhere_returns_none(self):
        msg = _updated("t", status=None, patch_status=None)
        assert _task_update_terminal_status(msg) is None


class TestRecordTaskMessageUpdate:
    @pytest.fixture(autouse=True)
    def _clean_registry(self):
        task_registry.reset_all()
        yield
        task_registry.reset_all()

    def test_killed_alone_completes_registry_task(self):
        # Task is running (started in the parent turn); a TaskStop kill arrives as
        # a TaskUpdatedMessage ALONE — no notification will follow.
        task_registry.start_task("sess-1", "task-1", "agent", "bg work")
        assert any(t["id"] == "task-1" for t in task_registry.get_active("sess-1"))

        _record_task_message("sess-1", _updated("task-1", status="killed"))

        # Indicator clears: the task is no longer "running".
        assert task_registry.get_active("sess-1") == []
        rec = task_registry.get_task("sess-1", "task-1")
        assert rec["status"] == "killed"

    def test_stopped_alone_completes_registry_task(self):
        task_registry.start_task("sess-1", "task-1", "agent", "bg work")
        _record_task_message("sess-1", _updated("task-1", status="stopped"))
        assert task_registry.get_active("sess-1") == []
        assert task_registry.get_task("sess-1", "task-1")["status"] == "killed"

    def test_non_terminal_update_leaves_task_running(self):
        # A 'running' update must NOT clobber the task to terminal.
        task_registry.start_task("sess-1", "task-1", "agent", "bg work")
        _record_task_message("sess-1", _updated("task-1", status="running"))
        active_ids = {t["id"] for t in task_registry.get_active("sess-1")}
        assert active_ids == {"task-1"}

    def test_completed_update_then_notification_completes_once_cleanly(self):
        # Normal completion: a TaskUpdatedMessage(completed) is followed by a
        # TaskNotificationMessage(completed). Completing twice must be a harmless
        # no-op (idempotent) — not an error, the final status is still terminal.
        task_registry.start_task("sess-1", "task-1", "agent", "bg work")

        _record_task_message("sess-1", _updated("task-1", status="completed"))
        assert task_registry.get_active("sess-1") == []
        assert task_registry.get_task("sess-1", "task-1")["status"] == "completed"

        # The notification still arrives — must not raise, status stays completed.
        _record_task_message("sess-1", _notif("task-1", status="completed", summary="done"))
        assert task_registry.get_active("sess-1") == []
        rec = task_registry.get_task("sess-1", "task-1")
        assert rec["status"] == "completed"
        assert rec["summary"] == "done"

    def test_update_for_unknown_task_is_noop(self):
        # complete_task no-ops on an unknown task — defensive, never raises.
        _record_task_message("sess-1", _updated("ghost", status="killed"))
        assert task_registry.get_active("sess-1") == []


# ---------------------------------------------------------------------------
# TaskStarted → registry kind discrimination (the agent-vs-shell badge fix)
# ---------------------------------------------------------------------------
#
# The SDK emits the same TaskStartedMessage for every flavour of background work
# and discriminates them only via ``task_type``: "local_bash" is a backgrounded
# shell command (Bash/PowerShell run_in_background), while "local_agent" /
# "remote_agent" / "local_workflow" are sub-agents / workflows. The registry kind
# must follow suit so the task bar renders the SHELL badge for shell commands (not
# the agent badge) and so a backgrounded shell command does not pin a parked
# session in the pool (``has_active_background_agent`` counts kind=="agent" only).

def _started(task_id, *, task_type=None, uuid="s-1", tool_use_id="tu-1"):
    """Build a real SDK TaskStartedMessage (so isinstance checks match)."""
    return TaskStartedMessage(
        subtype="task_started",
        data={},
        task_id=task_id,
        description="bg work",
        uuid=uuid,
        session_id="sess-1",
        tool_use_id=tool_use_id,
        task_type=task_type,
    )


class TestRecordTaskStartedKind:
    @pytest.fixture(autouse=True)
    def _clean_registry(self):
        task_registry.reset_all()
        yield
        task_registry.reset_all()

    def test_local_bash_records_shell_kind(self):
        # A backgrounded Bash/PowerShell command → "shell" badge, no pool pin.
        _record_task_message("sess-1", _started("t-bash", task_type="local_bash"))
        rec = task_registry.get_task("sess-1", "t-bash")
        assert rec["kind"] == "shell"
        assert task_registry.has_active_background_agent("sess-1") is False

    def test_local_agent_records_agent_kind(self):
        _record_task_message("sess-1", _started("t-agent", task_type="local_agent"))
        rec = task_registry.get_task("sess-1", "t-agent")
        assert rec["kind"] == "agent"
        assert task_registry.has_active_background_agent("sess-1") is True

    def test_remote_agent_and_workflow_record_agent_kind(self):
        _record_task_message("sess-1", _started("t-remote", task_type="remote_agent"))
        _record_task_message("sess-1", _started("t-wf", task_type="local_workflow"))
        assert task_registry.get_task("sess-1", "t-remote")["kind"] == "agent"
        assert task_registry.get_task("sess-1", "t-wf")["kind"] == "agent"

    def test_missing_task_type_defaults_to_agent(self):
        # Older CLI with no task_type field → historical behaviour (sub-agent).
        _record_task_message("sess-1", _started("t-none", task_type=None))
        assert task_registry.get_task("sess-1", "t-none")["kind"] == "agent"

    def test_started_this_turn_marked_for_shell_too(self):
        # The in-stream unpark stash is gated separately by was_background_launch
        # (False for shell, since only Task/Agent launches are recorded), so marking
        # started-this-turn for a shell task is harmless — assert it still happens
        # so the recording side-effect stays uniform across kinds.
        _record_task_message("sess-1", _started("t-bash", task_type="local_bash"))
        assert task_registry.was_started_this_turn("sess-1", "t-bash") is True
