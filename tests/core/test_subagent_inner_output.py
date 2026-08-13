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

from typing import Optional

import pytest

from app.core.provider.events import (
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
# and discriminates them only via ``task_type``: "local_bash" is a shell command,
# while "local_agent" / "remote_agent" / "local_workflow" are sub-agents /
# workflows. The registry kind must follow suit so the task bar renders the SHELL
# badge for shell commands (not the agent badge).
#
# CRITICAL (2026-07-02 incident): the CLI emits a local_bash TaskStartedMessage
# for FOREGROUND shell commands too, not just run_in_background ones. Only a shell
# whose launch tool_use_id the parent recorded with run_in_background=true
# (mark_background_launch, set when the tool_use block streamed) may arm the pool
# pin (mark_sdk_background_task); a foreground shell's result returns inline, and
# arming it made its at-rest notification pass the unpark gates and fire a stale
# "<task-notification>" follow-up turn after the turn ended.
# ``has_active_background_agent`` stays agent-kind only; ``has_active_background_work``
# covers agents + genuinely backgrounded shells.

def _started(task_id, *, task_type=None, uuid="s-1", tool_use_id: Optional[str] = "tu-1"):
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

    def test_foreground_local_bash_records_shell_kind_but_never_arms_unpark(self):
        # THE 2026-07-02 incident shape: a plain synchronous Bash/PowerShell call.
        # The CLI still emits TaskStarted(task_type="local_bash") for it, but the
        # parent's tool_use had NO run_in_background → mark_background_launch was
        # never recorded for "tu-1". It must get the SHELL badge only — arming the
        # pool pin here is what made its at-rest notification pass the unpark
        # gates and fire the stale post-turn <task-notification> follow-up.
        _record_task_message("sess-1", _started("t-bash", task_type="local_bash"))
        rec = task_registry.get_task("sess-1", "t-bash")
        assert rec["kind"] == "shell"
        assert task_registry.has_active_background_agent("sess-1") is False
        assert task_registry.is_sdk_background_task("sess-1", "t-bash") is False
        assert task_registry.has_active_background_work("sess-1") is False
        # _record_task_message no longer arms the launch gate itself.
        assert task_registry.was_background_launch("sess-1", "tu-1") is False

    def test_backgrounded_local_bash_pins_via_recorded_launch(self):
        # A genuine run_in_background shell: the streaming loop recorded the
        # parent's launch tool_use_id BEFORE the TaskStarted arrived (the tool_use
        # block always streams first). The shell then pins the parked session
        # (mark_sdk_background_task) so idle eviction can't drop its completion,
        # and get_active_background_work gates the drain pump on.
        task_registry.mark_background_launch("sess-1", "tu-1")
        _record_task_message("sess-1", _started("t-bash", task_type="local_bash"))
        assert task_registry.get_task("sess-1", "t-bash")["kind"] == "shell"
        assert task_registry.is_sdk_background_task("sess-1", "t-bash") is True
        assert task_registry.has_active_background_work("sess-1") is True

    def test_local_bash_without_tool_use_id_is_treated_as_foreground(self):
        # Without a tool_use_id on the TaskStarted there is no way to confirm the
        # parent backgrounded the launch — and the CLI emits local_bash TaskStarted
        # for foreground shells too, so defaulting to "background" is exactly the
        # incident bug. Unconfirmed → no pin, no unpark arming.
        task_registry.mark_background_launch("sess-1", "tu-1")
        _record_task_message(
            "sess-1", _started("t-bash2", task_type="local_bash", tool_use_id=None)
        )
        assert task_registry.is_sdk_background_task("sess-1", "t-bash2") is False
        assert task_registry.has_active_background_work("sess-1") is False

    def test_local_agent_does_not_arm_sdk_background(self):
        # A sub-agent must NOT be marked as an SDK-background SHELL (it is already
        # covered as kind=="agent"); arming sdk_background for agents would be
        # redundant and muddy the shell-only fallback.
        _record_task_message("sess-1", _started("t-agent", task_type="local_agent"))
        assert task_registry.is_sdk_background_task("sess-1", "t-agent") is False

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
        # started-this-turn is recorded uniformly across kinds — it feeds the
        # in-stream latch's orphan guard (and the drain-pump diagnostics), so even
        # a foreground shell's notification is classified against THIS turn.
        _record_task_message("sess-1", _started("t-bash", task_type="local_bash"))
        assert task_registry.was_started_this_turn("sess-1", "t-bash") is True

    # --- RC1 belt-and-braces: agent TaskStarted with a recorded background launch
    #     also arms mark_sdk_background_task (test E). The CLI now launches Agent
    #     tasks async WITHOUT the run_in_background input flag, so the ONLY signal
    #     that an agent is genuinely backgrounded is that its launch tool_use_id was
    #     armed from the launch ACK (mark_background_launch). This fallback covers a
    #     terminal notification/update that arrives WITHOUT a tool_use_id (the
    #     always-present task_id then carries the unpark gate via is_sdk_background).
    def test_agent_started_with_recorded_launch_arms_sdk_background(self):
        # The launch ACK (or run_in_background flag) recorded the parent's launch
        # tool_use_id "tu-1" — the TaskStarted for this agent carries the same id.
        task_registry.mark_background_launch("sess-1", "tu-1")
        _record_task_message(
            "sess-1", _started("t-agent", task_type="local_agent", tool_use_id="tu-1")
        )
        assert task_registry.get_task("sess-1", "t-agent")["kind"] == "agent"
        assert task_registry.is_sdk_background_task("sess-1", "t-agent") is True

    def test_agent_started_without_recorded_launch_does_not_arm_sdk_background(self):
        # No mark_background_launch for this agent's tool_use_id — a synchronous
        # sub-agent (result inline). Arming it would resurrect the #470 foreground
        # spurious-unpark bug, so is_sdk_background_task must stay False.
        _record_task_message(
            "sess-1", _started("t-agent", task_type="local_agent", tool_use_id="tu-1")
        )
        assert task_registry.is_sdk_background_task("sess-1", "t-agent") is False

    def test_agent_started_without_tool_use_id_does_not_arm_sdk_background(self):
        # Even with a recorded launch, an agent TaskStarted lacking a tool_use_id
        # can't be confirmed against it → no arming.
        task_registry.mark_background_launch("sess-1", "tu-1")
        _record_task_message(
            "sess-1", _started("t-agent", task_type="local_agent", tool_use_id=None)
        )
        assert task_registry.is_sdk_background_task("sess-1", "t-agent") is False

    # --- Subagent-lane background tasks (2026-07-10 stranded-subagent incident):
    #     a launch recorded via mark_subagent_background_launch arms the DEDICATED
    #     task marker — never mark_sdk_background_task (no pool pin, no chip
    #     scope), and never for an unrecorded launch (the #470 foreground
    #     semantics stay pinned).
    def test_shell_started_with_subagent_lane_launch_arms_subagent_marker(self):
        task_registry.mark_subagent_background_launch("sess-1", "tu-1")
        _record_task_message("sess-1", _started("t-bash", task_type="local_bash"))
        assert task_registry.is_subagent_background_task("sess-1", "t-bash") is True
        # The parent's own gates stay unarmed: no pool pin, no chip eligibility.
        assert task_registry.is_sdk_background_task("sess-1", "t-bash") is False
        assert task_registry.has_active_background_work("sess-1") is False
        assert task_registry.is_own_background_task("sess-1", "t-bash") is False

    def test_agent_started_with_subagent_lane_launch_arms_subagent_marker(self):
        task_registry.mark_subagent_background_launch("sess-1", "tu-1")
        _record_task_message(
            "sess-1", _started("t-agent", task_type="local_agent", tool_use_id="tu-1")
        )
        assert task_registry.is_subagent_background_task("sess-1", "t-agent") is True
        assert task_registry.is_sdk_background_task("sess-1", "t-agent") is False

    def test_unrecorded_launch_does_not_arm_subagent_marker(self):
        _record_task_message("sess-1", _started("t-bash", task_type="local_bash"))
        assert task_registry.is_subagent_background_task("sess-1", "t-bash") is False


# ---------------------------------------------------------------------------
# RC1: arm the unpark gate from the launch ACK (tests A, B, D).
#
# The bundled CLI (2.1.195+) launches Agent/Task sub-agents ASYNC by DEFAULT with
# NO run_in_background/runInBackground key in the tool input — the launch
# tool_result is just an ACK ("Async agent launched successfully … working in the
# background … notified automatically"). Because mark_background_launch used to
# fire ONLY on the input flag, _background_launches stayed empty and the drain
# pump suppressed every genuine agent completion as suppress-foreground.
#
# The fix arms the gate from the ACK: at the ToolResult branch, a TOP-LEVEL Task
# tool_use whose result reads like a launch ACK records mark_background_launch.
# A SYNCHRONOUS agent's tool_result carries the FULL result text (the ACK sniffer
# does not match) so it must NOT arm the gate — this pins the #470/e83551e6
# foreground-suppression semantics. A NESTED (grandchild) launch must not arm the
# PARENT gate.
#
# Harness mirrors tests/core/test_cross_turn_background_subagent_routing.py:
# drive stream_to_websocket over a scripted SDK message sequence, no Postgres /
# real SDK, then assert the registry side-effect (was_background_launch).
# ---------------------------------------------------------------------------

from contextlib import ExitStack as _ExitStack  # noqa: E402
from unittest.mock import AsyncMock as _AsyncMock, MagicMock as _MagicMock, patch as _patch  # noqa: E402

import app.core.query_engine as _qe  # noqa: E402
from app.core.session_pool import SessionState as _SessionState  # noqa: E402


_ACK_TEXT = (
    "Async agent launched successfully. agentId: a2c0deadbeef. The agent is "
    "working in the background. You will be notified automatically when it "
    "completes."
)


def _ack_state(messages, **overrides) -> _SessionState:
    adapter = _MagicMock()
    adapter.is_alive = _MagicMock(return_value=True)
    adapter.sdk_client = _MagicMock()
    adapter.send_query = _AsyncMock()

    async def _response():
        for m in messages:
            yield m

    adapter.receive_events = _MagicMock(side_effect=lambda: _response())
    state = _SessionState(adapter=adapter, is_connected=True)
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


class _AckStubPool:
    def __init__(self, state=None):
        self.state = state

    def get(self, session_id):
        return self.state

    def peek(self, session_id):
        return self.state

    def put(self, session_id, state):
        self.state = state

    def admission_check(self):
        return None

    async def evict_if_needed(self):
        return None

    async def remove(self, session_id):
        self.state = None

    async def remove_dead(self, session_id):
        self.state = None

    async def remove_broken(self, session_id):
        self.state = None


def _ack_chat_patches(stack, pool):
    stack.enter_context(_patch.object(
        _qe, "get_profile", return_value={"id": "claude-code", "config": {}}
    ))

    async def _fake_run_sync(fn, *a, **k):
        return {"id": "row-1"}

    stack.enter_context(_patch.object(_qe, "run_sync", new=_AsyncMock(side_effect=_fake_run_sync)))
    oauth = _MagicMock()
    oauth.ensure_fresh_credentials = _AsyncMock()
    stack.enter_context(_patch.object(_qe, "direct_oauth", new=oauth))
    options = _MagicMock()
    options.include_partial_messages = True
    options.cwd = None
    stack.enter_context(_patch("app.core.sdk_options.build_options_from_profile", return_value=(options, {})
    ))
    stack.enter_context(_patch.object(_qe, "get_session_pool", return_value=pool))
    stack.enter_context(_patch.object(_qe, "is_subprocess_alive", return_value=True))
    stack.enter_context(_patch.object(_qe, "requires_reconnect", return_value=False))
    stack.enter_context(_patch.object(_qe, "mark_subprocess_oom_killable", new=_MagicMock()))
    stack.enter_context(_patch.object(_qe, "resolve_note_references", side_effect=lambda p: p))
    stack.enter_context(_patch.object(_qe, "_persist_turn_results", new=_AsyncMock()))
    stack.enter_context(_patch.object(_qe, "push_memory", new=_AsyncMock()))
    sync = _MagicMock()
    sync.broadcast_message_added = _AsyncMock()
    sync.broadcast_stream_start = _AsyncMock()
    sync.broadcast_stream_chunk = _AsyncMock()
    sync.broadcast_stream_end = _AsyncMock()
    stack.enter_context(_patch.object(_qe, "sync_engine", new=sync))
    return options


async def _drain(agen):
    async for _ in agen:
        pass


def _agent_launch_msg(tool_id, *, parent_tool_use_id=None, run_in_background=False):
    tool_input = {"subagent_type": "Explore", "description": "bg work", "prompt": "go"}
    if run_in_background:
        tool_input["run_in_background"] = True
    return AssistantMessage(
        content=[ToolUseBlock(id=tool_id, name="Agent", input=tool_input)],
        model="claude",
        parent_tool_use_id=parent_tool_use_id,
    )


def _tool_result_user_msg(tool_use_id, text, *, parent_tool_use_id=None):
    return UserMessage(
        content=[ToolResultBlock(tool_use_id=tool_use_id, content=text, is_error=False)],
        parent_tool_use_id=parent_tool_use_id,
    )


# The CLI-native builtin `Monitor` tool's REAL input shape (live 2026-07-10,
# session 07df5136 / sidechain agent-a529395258548df1c, tool_use
# toolu_01JuRn7NoPKiYAPB36ikmrxk → task b5qiqqf1h): command / description /
# timeout_ms / persistent — NO run_in_background flag, ever, despite ALWAYS
# spawning a background task.
_MONITOR_INPUT = {
    "command": 'i=0; until [ $i -ge 90 ]; do sleep 1; i=$((i+1)); done; echo "90s-elapsed"',
    "description": "90s until-loop repro timer",
    "timeout_ms": 120000,
    "persistent": False,
}


def _monitor_launch_msg(tool_id, *, parent_tool_use_id=None):
    return AssistantMessage(
        content=[ToolUseBlock(id=tool_id, name="Monitor", input=dict(_MONITOR_INPUT))],
        model="claude",
        parent_tool_use_id=parent_tool_use_id,
    )


class TestLaunchAckArmsBackgroundGate:
    def setup_method(self):
        task_registry.reset_all()

    def teardown_method(self):
        task_registry.reset_all()

    @pytest.mark.asyncio
    async def test_ack_tool_result_arms_background_launch(self):
        """Test A: an Agent tool_use launched WITHOUT run_in_background in its input,
        whose tool_result is the async launch ACK, ARMS mark_background_launch for
        the launch tool_use_id — so its later terminal notification unparks."""
        session_id = "sess-ack-a"
        messages = [
            _agent_launch_msg("toolu_bg"),
            _tool_result_user_msg("toolu_bg", _ACK_TEXT),
        ]
        state = _ack_state(messages)
        pool = _AckStubPool(state)

        with _ExitStack() as stack:
            _ack_chat_patches(stack, pool)
            await _drain(_qe.stream_to_websocket("prompt", session_id, "claude-code"))

        assert task_registry.was_background_launch(session_id, "toolu_bg") is True

    @pytest.mark.asyncio
    async def test_real_synchronous_result_does_not_arm_gate(self):
        """Test B: the SAME Agent launch but whose tool_result is a REAL synchronous
        result (not ACK text) must NOT arm the gate — pins the #470/e83551e6
        foreground-suppression semantics so a synchronous sub-agent never unparks."""
        session_id = "sess-ack-b"
        real_result = "All three commands ran successfully: STEP-1 then STEP-2 then STEP-3."
        messages = [
            _agent_launch_msg("toolu_sync"),
            _tool_result_user_msg("toolu_sync", real_result),
        ]
        state = _ack_state(messages)
        pool = _AckStubPool(state)

        with _ExitStack() as stack:
            _ack_chat_patches(stack, pool)
            await _drain(_qe.stream_to_websocket("prompt", session_id, "claude-code"))

        assert task_registry.was_background_launch(session_id, "toolu_sync") is False

    @pytest.mark.asyncio
    async def test_nested_grandchild_ack_does_not_arm_parent_gate(self):
        """Test D: a grandchild Agent launch (parent_tool_use_id set) whose result is
        an ACK must NOT arm the PARENT gate — only TOP-LEVEL launches pin/unpark."""
        session_id = "sess-ack-d"
        # A real top-level background parent so its inner grandchild launch has a
        # tracked parent to nest under (parent_tool_use_id == the parent's tool_id).
        parent_id = "toolu_parent"
        grandchild_id = "toolu_grandchild"
        messages = [
            _agent_launch_msg(parent_id, run_in_background=True),
            _tool_result_user_msg(parent_id, _ACK_TEXT),
            # The grandchild launch + its ACK arrive nested under the parent.
            _agent_launch_msg(grandchild_id, parent_tool_use_id=parent_id),
            _tool_result_user_msg(grandchild_id, _ACK_TEXT, parent_tool_use_id=parent_id),
        ]
        state = _ack_state(messages)
        pool = _AckStubPool(state)

        with _ExitStack() as stack:
            _ack_chat_patches(stack, pool)
            await _drain(_qe.stream_to_websocket("prompt", session_id, "claude-code"))

        # The top-level parent IS armed (run_in_background flag path).
        assert task_registry.was_background_launch(session_id, parent_id) is True
        # The nested grandchild is NOT armed on the parent gate.
        assert task_registry.was_background_launch(session_id, grandchild_id) is False

    @pytest.mark.asyncio
    async def test_nested_grandchild_ack_arms_subagent_lane_gate(self):
        """The SAME nested-ACK sequence as test D, asserted on the NEW gate: the
        grandchild's async-launch ACK records the DEDICATED subagent-lane mark
        (2026-07-10 incident fix) while the parent gate stays clear (test D)."""
        session_id = "sess-ack-d2"
        parent_id = "toolu_parent"
        grandchild_id = "toolu_grandchild"
        messages = [
            _agent_launch_msg(parent_id, run_in_background=True),
            _tool_result_user_msg(parent_id, _ACK_TEXT),
            _agent_launch_msg(grandchild_id, parent_tool_use_id=parent_id),
            _tool_result_user_msg(grandchild_id, _ACK_TEXT, parent_tool_use_id=parent_id),
        ]
        state = _ack_state(messages)
        pool = _AckStubPool(state)

        with _ExitStack() as stack:
            _ack_chat_patches(stack, pool)
            await _drain(_qe.stream_to_websocket("prompt", session_id, "claude-code"))

        assert task_registry.was_subagent_background_launch(session_id, grandchild_id) is True
        assert task_registry.was_background_launch(session_id, grandchild_id) is False

    @pytest.mark.asyncio
    async def test_nested_grandchild_background_flag_arms_subagent_lane_gate(self):
        """2026-07-10 incident (in-stream half): a grandchild Bash tool_use with an
        explicit run_in_background flag (parent_tool_use_id set) records the
        DEDICATED subagent-lane launch mark — never the parent gate."""
        session_id = "sess-ack-e"
        parent_id = "toolu_parent"
        grandchild_id = "toolu_gc_bash"
        gc_launch = AssistantMessage(
            content=[ToolUseBlock(
                id=grandchild_id, name="Bash",
                input={"command": "python monitor.py", "run_in_background": True},
            )],
            model="claude",
            parent_tool_use_id=parent_id,
        )
        messages = [
            _agent_launch_msg(parent_id, run_in_background=True),
            _tool_result_user_msg(parent_id, _ACK_TEXT),
            gc_launch,
        ]
        state = _ack_state(messages)
        pool = _AckStubPool(state)

        with _ExitStack() as stack:
            _ack_chat_patches(stack, pool)
            await _drain(_qe.stream_to_websocket("prompt", session_id, "claude-code"))

        assert task_registry.was_subagent_background_launch(session_id, grandchild_id) is True
        # Parent gate untouched — no pin, no chip scope for the grandchild.
        assert task_registry.was_background_launch(session_id, grandchild_id) is False

    @pytest.mark.asyncio
    async def test_nested_monitor_builtin_arms_subagent_lane_gate(self):
        """2026-07-10 Monitor-builtin gap (in-stream half): the CLI-native
        `Monitor` tool ALWAYS spawns a background task but carries NO
        run_in_background flag in its input, so the flag sniffs never
        classified it. Live repro (session 07df5136, task b5qiqqf1h): the
        sidechain Monitor's terminal notification hit the pump gate unarmed →
        decision=suppress-foreground → the Monitor-parked sub-agent stranded
        forever (the CLI enqueued the <task-notification> into the parked
        parent's queue and never self-dequeued a subagent-owned one). A
        sidechain Monitor tool_use must arm the DEDICATED subagent-lane mark
        by TOOL NAME — never the parent gate."""
        session_id = "sess-ack-mon"
        parent_id = "toolu_parent"
        monitor_id = "toolu_monitor"
        messages = [
            _agent_launch_msg(parent_id, run_in_background=True),
            _tool_result_user_msg(parent_id, _ACK_TEXT),
            _monitor_launch_msg(monitor_id, parent_tool_use_id=parent_id),
        ]
        state = _ack_state(messages)
        pool = _AckStubPool(state)

        with _ExitStack() as stack:
            _ack_chat_patches(stack, pool)
            await _drain(_qe.stream_to_websocket("prompt", session_id, "claude-code"))

        assert task_registry.was_subagent_background_launch(session_id, monitor_id) is True
        # Parent gate untouched — no pin, no chip scope (#470 preserved).
        assert task_registry.was_background_launch(session_id, monitor_id) is False

    @pytest.mark.asyncio
    async def test_top_level_monitor_builtin_arms_nothing(self):
        """Top-level Monitor behavior must be COMPLETELY unchanged by the
        sidechain fix: a top-level Monitor's wake is CLI-OWNED (the CLI
        enqueues its own <task-notification> into its queue and self-dequeues
        it, injecting the wake turn itself), so the app keeps suppressing its
        completion — NEITHER launch gate may arm."""
        session_id = "sess-ack-mon-top"
        monitor_id = "toolu_monitor_top"
        messages = [_monitor_launch_msg(monitor_id)]
        state = _ack_state(messages)
        pool = _AckStubPool(state)

        with _ExitStack() as stack:
            _ack_chat_patches(stack, pool)
            await _drain(_qe.stream_to_websocket("prompt", session_id, "claude-code"))

        assert task_registry.was_background_launch(session_id, monitor_id) is False
        assert task_registry.was_subagent_background_launch(session_id, monitor_id) is False


class TestRecordSubagentLaneLaunchesMonitor:
    """Pump-side half of the 2026-07-10 Monitor-builtin gap: in the live repro
    the sub-agent armed its Monitor BETWEEN turns (parent turn ended 21:19:51,
    the Monitor tool_use streamed ~21:19:58 with only the drain pump reading),
    so ``_record_subagent_lane_launches`` — not the in-stream branch — is the
    recorder that must sniff the flag-less builtin by tool name."""

    def setup_method(self):
        task_registry.reset_all()

    def teardown_method(self):
        task_registry.reset_all()

    def test_sidechain_monitor_marks_subagent_launch(self):
        msg = _monitor_launch_msg("tu-mon", parent_tool_use_id="tu-parent")
        _qe._record_subagent_lane_launches("sess-rec-mon", msg)
        assert task_registry.was_subagent_background_launch("sess-rec-mon", "tu-mon") is True
        assert task_registry.was_background_launch("sess-rec-mon", "tu-mon") is False

    def test_top_level_monitor_is_ignored_by_recorder(self):
        # A top-level between-turns launch belongs to _cli_turn_events_from_message
        # (which must keep NOT arming for Monitor — its wake is CLI-owned).
        msg = _monitor_launch_msg("tu-mon-top")
        _qe._record_subagent_lane_launches("sess-rec-mon", msg)
        assert task_registry.was_subagent_background_launch("sess-rec-mon", "tu-mon-top") is False
        assert task_registry.was_background_launch("sess-rec-mon", "tu-mon-top") is False


# ---------------------------------------------------------------------------
# #660: defer-don't-terminate gate — end-to-end over stream_to_websocket.
#
# The pre-#660 guard fired subagent_done at the launch ToolResult UNLESS the
# result matched the 5-substring ACK sniffer or a run_in_background flag was set.
# If a future CLI REWORDS the launch ACK, the sniffer misses it and a premature
# subagent_done was persisted at launch (card checkmarks while the agent runs).
#
# The gate defers an ambiguous launch result instead of terminating it:
#  - a genuine async launch emits a later in-stream TaskStarted → the parked
#    entry is DROPPED (it settles on its terminal TaskNotificationMessage), so a
#    REWORDED ACK no longer fires a false subagent_done;
#  - a genuine SYNCHRONOUS foreground agent (no TaskStarted, no background
#    evidence) is flushed as subagent_done at a clean end-of-turn drain;
#  - registry background evidence SUPPRESSES with no phrase match at all;
#  - an ABORTED turn (RULING R1) drops the parked entry WITHOUT emitting.
# ---------------------------------------------------------------------------

import asyncio as _asyncio  # noqa: E402


async def _collect(agen):
    out = []
    async for ev in agen:
        out.append(ev)
    return out


def _raising_state(messages, exc) -> _SessionState:
    """A SessionState whose receive_response yields ``messages`` then RAISES —
    models a mid-stream interrupt/crash after the launch result was parked."""
    adapter = _MagicMock()
    adapter.is_alive = _MagicMock(return_value=True)
    adapter.sdk_client = _MagicMock()
    adapter.send_query = _AsyncMock()

    async def _response():
        for m in messages:
            yield m
        raise exc

    adapter.receive_events = _MagicMock(side_effect=lambda: _response())
    return _SessionState(adapter=adapter, is_connected=True)


# A launch ACK the 5-substring sniffer does NOT match — the exact regression the
# gate defends against (a future CLI reword of the async-launch placeholder).
_REWORDED_ACK = "Spawned the explorer agent; it will report back once finished."
_REAL_SYNC_RESULT = "All three commands ran successfully: STEP-1 then STEP-2 then STEP-3."


class TestDeferDontTerminateStream:
    def setup_method(self):
        task_registry.reset_all()

    def teardown_method(self):
        task_registry.reset_all()

    @pytest.mark.asyncio
    async def test_reworded_ack_then_taskstarted_emits_no_subagent_done(self):
        """The #660 win: a launch result the sniffer MISSES is DEFERRED, then a
        later in-stream TaskStarted drops the parked entry — so NO premature
        subagent_done is ever emitted (the node stays running, settling on its
        terminal notification instead)."""
        session_id = "sess-defer-a"
        messages = [
            _agent_launch_msg("toolu_bg"),
            _tool_result_user_msg("toolu_bg", _REWORDED_ACK),
            _started("task-bg", task_type="local_agent", tool_use_id="toolu_bg"),
        ]
        state = _ack_state(messages)
        pool = _AckStubPool(state)
        with _ExitStack() as stack:
            _ack_chat_patches(stack, pool)
            events = await _collect(
                _qe.stream_to_websocket("prompt", session_id, "claude-code")
            )
        assert [e for e in events if e.get("type") == "subagent_done"] == []
        # The TaskStarted frame still flows to the FE (settles via #617 join).
        assert any(e.get("type") == "task_started" for e in events)

    @pytest.mark.asyncio
    async def test_synchronous_foreground_flushes_at_turn_end(self):
        """A genuine synchronous sub-agent (real result, NO TaskStarted, no
        background evidence) is flushed as subagent_done at the clean end-of-turn
        drain — AND (RULING R3) lands deterministically AFTER later same-turn
        events, not at the launch ToolResult position."""
        session_id = "sess-defer-b"
        messages = [
            _agent_launch_msg("toolu_sync"),
            _tool_result_user_msg("toolu_sync", _REAL_SYNC_RESULT),
            # A later same-turn top-level tool: proves the flushed subagent_done
            # deterministically trails it (the accepted R3 cosmetic reorder).
            AssistantMessage(
                content=[ToolUseBlock(id="t_read", name="Read", input={"file_path": "/x"})],
                model="claude",
            ),
            _tool_result_user_msg("t_read", "file contents"),
        ]
        state = _ack_state(messages)
        pool = _AckStubPool(state)
        with _ExitStack() as stack:
            _ack_chat_patches(stack, pool)
            events = await _collect(
                _qe.stream_to_websocket("prompt", session_id, "claude-code")
            )
        dones = [e for e in events if e.get("type") == "subagent_done"]
        assert len(dones) == 1
        assert dones[0]["tool_id"] == "toolu_sync"
        assert dones[0]["result"] == _REAL_SYNC_RESULT
        assert dones[0]["is_error"] is False
        # Deterministic ordering: the flushed done trails the later Read result.
        done_idx = events.index(dones[0])
        read_result_idx = next(
            i for i, e in enumerate(events)
            if e.get("type") == "tool_result" and e.get("tool_use_id") == "t_read"
        )
        assert done_idx > read_result_idx

    @pytest.mark.asyncio
    async def test_registry_background_evidence_suppresses_without_phrase_match(self):
        """A launch pre-armed in the registry (was_background_launch) SUPPRESSES
        at the ToolResult with NO ACK phrase match — the gate no longer depends
        on the sniffer when positive background evidence exists."""
        session_id = "sess-defer-c"
        task_registry.mark_background_launch(session_id, "toolu_bg")
        messages = [
            _agent_launch_msg("toolu_bg"),
            _tool_result_user_msg("toolu_bg", _REWORDED_ACK),
        ]
        state = _ack_state(messages)
        pool = _AckStubPool(state)
        with _ExitStack() as stack:
            _ack_chat_patches(stack, pool)
            events = await _collect(
                _qe.stream_to_websocket("prompt", session_id, "claude-code")
            )
        assert [e for e in events if e.get("type") == "subagent_done"] == []

    @pytest.mark.asyncio
    async def test_aborted_turn_drops_parked_entry_without_emitting(self):
        """RULING R1 (abort-safe): a parked launch result whose turn ends via an
        abnormal teardown (CancelledError) is DROPPED — the end-of-turn flush
        lives inside the streaming try and is skipped, so NO false terminal is
        emitted. A stuck-running node is recoverable (#659 re-open belt); a false
        terminal is not."""
        session_id = "sess-defer-d"
        messages = [
            _agent_launch_msg("toolu_sync"),
            _tool_result_user_msg("toolu_sync", _REAL_SYNC_RESULT),
        ]
        state = _raising_state(messages, _asyncio.CancelledError())
        pool = _AckStubPool(state)
        with _ExitStack() as stack:
            _ack_chat_patches(stack, pool)
            events = await _collect(
                _qe.stream_to_websocket("prompt", session_id, "claude-code")
            )
        assert [e for e in events if e.get("type") == "subagent_done"] == []
