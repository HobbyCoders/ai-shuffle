"""Background Task/Agent launches across a parked turn and its wake turn.

A turn that launches a background sub-agent or shell ends (``done``) with the
work still running: the task registry must say so, because that is what parks
the parent and pins its pooled client. The completion arrives on the NEXT turn
over the same pooled adapter as a ``TaskNotificationMessage``; the sub-agent's
own output in that turn routes to the subagent lane via the cross-turn seed.
Foreground launches settle as ``subagent_done`` exactly once: at their result
when it is conclusive, or at the end-of-turn drain when it is ambiguous — and
never on an interrupted turn.
"""

from contextlib import ExitStack
from types import SimpleNamespace

import pytest

import app.core.query_engine as qe
from app.core.provider.events import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from app.core.task_registry import task_registry
from tests.core.engine_harness import collect, stream_response
from tests.core.query_engine_flow_harness import (
    engine_patches,
    frames_of,
    fresh_adapter,
    scripted_events,
)

SID = "qe-bg-tasks"


@pytest.fixture(autouse=True)
def _clean_registry():
    task_registry.clear_session(SID)
    yield
    task_registry.clear_session(SID)


def _launch(tool_id, name="Agent", parent=None, **inp):
    return AssistantMessage(
        content=[ToolUseBlock(id=tool_id, name=name, input=inp)], model="m",
        parent_tool_use_id=parent,
    )


def _user_result(tool_id, content, is_error=False, parent=None):
    return UserMessage(
        content=[ToolResultBlock(tool_use_id=tool_id, content=content, is_error=is_error)],
        parent_tool_use_id=parent,
    )


def _asst_result(tool_id, content, is_error=False):
    return AssistantMessage(
        content=[ToolResultBlock(tool_use_id=tool_id, content=content, is_error=is_error)],
        model="m",
    )


def _started(task_id, tool_id, task_type="local_agent"):
    return TaskStartedMessage(subtype="task_started", data={}, task_id=task_id,
                              description="dig", tool_use_id=tool_id, task_type=task_type)


def _done():
    return ResultMessage(subtype="success")


async def _run(stack_bounds, prompt="go"):
    return await collect(qe._stream_to_websocket_impl(prompt, SID, "claude-code"))


# ─── park, then wake on the next turn ───────────────────────────────────────


async def test_background_agent_parks_the_turn_and_completes_on_the_wake_turn(tmp_path):
    with ExitStack() as stack:
        adapter = fresh_adapter([
            _launch("tu-bg", subagent_type="researcher", description="dig",
                    prompt="find it", run_in_background=True),
            _user_result("tu-bg", "Async agent launched successfully. agentId: a1"),
            _started("task-1", "tu-bg"),
            _done(),
        ])
        b = engine_patches(stack, cwd=str(tmp_path), adapter=adapter)
        first = await _run(b)

        # Turn 1: launched, not finished; the registry holds the parked work.
        assert frames_of(first, "subagent_start") == [{
            "type": "subagent_start", "tool_id": "tu-bg", "agent_type": "researcher",
            "description": "dig", "prompt": "find it", "agent_id": "tu-bg",
        }]
        assert not frames_of(first, "subagent_done")
        assert frames_of(first, "task_started")[0]["tool_use_id"] == "tu-bg"
        assert first[-1]["type"] == "done"
        assert task_registry.was_background_launch(SID, "tu-bg")
        assert task_registry.is_sdk_background_task(SID, "task-1")
        assert [w["id"] for w in task_registry.get_active_background_work(SID)] == ["task-1"]

        # Turn 2 on the same pooled client: the sub-agent's late output routes
        # to its lane and its terminal notification completes it.
        adapter.receive_events = stream_response([
            AssistantMessage(content=[TextBlock(text="still digging")], model="m-sub",
                             parent_tool_use_id="tu-bg"),
            SystemMessage(subtype="skill_note", data={"n": 1}),  # sub-agent context: hidden
            TaskProgressMessage(subtype="task_progress", data={}, task_id="task-1",
                                description="digging", tool_use_id="tu-bg",
                                usage=SimpleNamespace(total_tokens=5, tool_uses=1, duration_ms=9)),
            TaskNotificationMessage(subtype="task_notification", data={}, task_id="task-1",
                                    status="completed", summary="found it",
                                    tool_use_id="tu-bg", output_file="/tmp/out.txt",
                                    usage={"total_tokens": 10, "tool_uses": 2, "duration_ms": 50}),
            _done(),
        ])
        second = await _run(b, prompt="<task-notification>")

    b.make_adapter.assert_called_once()  # the wake reused the parked client
    assert frames_of(second, "subagent_chunk") == [
        {"type": "subagent_chunk", "agent_id": "tu-bg", "content": "still digging"}]
    assert not frames_of(second, "chunk")
    assert not frames_of(second, "system")
    assert frames_of(second, "task_progress") == [{
        "type": "task_progress", "task_id": "task-1", "description": "digging",
        "tool_use_id": "tu-bg",
        "usage": {"total_tokens": 5, "tool_uses": 1, "duration_ms": 9},
    }]
    assert frames_of(second, "task_notification") == [{
        "type": "task_notification", "task_id": "task-1", "status": "completed",
        "summary": "found it", "tool_use_id": "tu-bg", "output_file": "/tmp/out.txt",
        "uuid": "", "usage": {"total_tokens": 10, "tool_uses": 2, "duration_ms": 50},
    }]
    assert second[-1]["type"] == "done"
    assert task_registry.get_active_background_work(SID) == []
    assert task_registry.get_task(SID, "task-1")["status"] == "completed"


async def test_background_shell_parks_only_when_launched_in_background(tmp_path):
    with ExitStack() as stack:
        adapter = fresh_adapter([
            _launch("tu-sh", name="Bash", command="npm run dev", run_in_background=True),
            _started("task-sh", "tu-sh", task_type="local_bash"),
            _launch("tu-fg", name="Bash", command="ls"),
            _started("task-fg", "tu-fg", task_type="local_bash"),
            _done(),
        ])
        b = engine_patches(stack, cwd=str(tmp_path), adapter=adapter)
        await _run(b)
    assert [w["id"] for w in task_registry.get_active_background_work(SID)] == ["task-sh"]


@pytest.mark.parametrize("status,terminal", [("killed", "killed"), ("failed", "error"),
                                             ("running", None)])
async def test_task_update_settles_a_task_only_on_a_terminal_status(tmp_path, status, terminal):
    with ExitStack() as stack:
        adapter = fresh_adapter([
            _launch("tu-bg", run_in_background=True),
            _started("task-1", "tu-bg"),
            TaskUpdatedMessage(subtype="task_updated", data={}, task_id="task-1",
                               patch={"status": status}),
            _done(),
        ])
        b = engine_patches(stack, cwd=str(tmp_path), adapter=adapter)
        await _run(b)
    assert task_registry.get_task(SID, "task-1")["status"] == (terminal or "running")


async def test_launch_ack_without_the_flag_is_classified_as_background(tmp_path):
    """The CLI launches agents async without run_in_background; the ACK text
    is what marks them, so the ACK must not settle the card."""
    with ExitStack() as stack:
        adapter = fresh_adapter([
            _launch("tu-ack", subagent_type="helper"),
            _asst_result("tu-ack", "Agent is running in the background with id x"),
            _done(),
        ])
        b = engine_patches(stack, cwd=str(tmp_path), adapter=adapter)
        events = await _run(b)
    assert frames_of(events, "subagent_start")
    assert not frames_of(events, "subagent_done")
    assert task_registry.was_background_launch(SID, "tu-ack")


# ─── foreground settlement ─────────────────────────────────────────────────


@pytest.mark.parametrize("lane", ["assistant", "user"])
async def test_ambiguous_foreground_result_settles_at_end_of_turn(tmp_path, lane):
    result = (_asst_result if lane == "assistant" else _user_result)("tu-fg", "Here is the answer: 42")
    with ExitStack() as stack:
        adapter = fresh_adapter([
            _launch("tu-fg", subagent_type="calc"),
            result,
            AssistantMessage(content=[TextBlock(text="after")], model="m"),
            _done(),
        ])
        b = engine_patches(stack, cwd=str(tmp_path), adapter=adapter)
        events = await _run(b)
    types = [e["type"] for e in events]
    # Deferred past the text that followed it, flushed before `done`.
    assert types.index("chunk") < types.index("subagent_done") < types.index("done")
    assert frames_of(events, "subagent_done") == [{
        "type": "subagent_done", "tool_id": "tu-fg", "agent_id": "tu-fg",
        "agent_type": "calc", "result": "Here is the answer: 42", "is_error": False,
    }]


async def test_parked_foreground_result_is_dropped_on_an_interrupted_turn(tmp_path):
    box = {}

    async def _stop():
        box["b"].pool.state.interrupt_requested = True

    with ExitStack() as stack:
        adapter = fresh_adapter()
        adapter.receive_events = scripted_events([
            _launch("tu-fg", subagent_type="calc"),
            _user_result("tu-fg", "Here is the answer: 42"),
            _stop,
        ])
        box["b"] = engine_patches(stack, cwd=str(tmp_path), adapter=adapter)
        events = await _run(box["b"])
    assert events[-1]["type"] == "interrupted"
    assert not frames_of(events, "subagent_done")


async def test_parked_result_whose_task_started_is_not_flushed(tmp_path):
    with ExitStack() as stack:
        adapter = fresh_adapter([
            _launch("tu-x", subagent_type="calc"),
            _user_result("tu-x", "Here is the answer: 42"),
            _started("task-x", "tu-x"),
            _done(),
        ])
        b = engine_patches(stack, cwd=str(tmp_path), adapter=adapter)
        events = await _run(b)
    assert not frames_of(events, "subagent_done")


@pytest.mark.parametrize("content,is_error,expected", [
    ("model not in this provider's list", True, "model not in this provider's list"),
    ('{"ok": false, "error": "unknown model"}', False, "unknown model"),
])
async def test_launch_failure_settles_immediately_as_an_error(tmp_path, content, is_error, expected):
    with ExitStack() as stack:
        adapter = fresh_adapter([
            _launch("tu-bad", subagent_type="calc", run_in_background=True),
            _user_result("tu-bad", content, is_error=is_error),
            _done(),
        ])
        b = engine_patches(stack, cwd=str(tmp_path), adapter=adapter)
        events = await _run(b)
    [done] = frames_of(events, "subagent_done")
    assert done["result"] == expected
    assert done["is_error"] is True


async def test_app_dispatched_task_leaves_announcements_to_the_worker_feed(tmp_path):
    with ExitStack() as stack:
        adapter = fresh_adapter([
            _launch("tu-app", name="Task", agent="coder", prompt="p"),
            _user_result("tu-app", '{"ok": false, "error": "no such agent"}'),
            _launch("tu-app2", name="Task", agent="coder", prompt="p"),
            _user_result("tu-app2", "worker finished: all good"),
            _done(),
        ])
        b = engine_patches(stack, cwd=str(tmp_path), adapter=adapter)
        events = await _run(b)
    assert not frames_of(events, "subagent_start")
    assert not frames_of(events, "subagent_done")


# ─── the sub-agent lane ────────────────────────────────────────────────────


async def test_subagent_tool_calls_route_to_the_subagent_lane(tmp_path):
    png = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}
    with ExitStack() as stack:
        adapter = fresh_adapter([
            _launch("tu-p", subagent_type="helper", run_in_background=True),
            _launch("tu-child", name="Bash", parent="tu-p", command="sleep 9", run_in_background=True),
            _launch("tu-mon", name="Monitor", parent="tu-p", command="tail -f log"),
            _user_result("tu-child", "started", parent="tu-p"),
            UserMessage(content=[ToolResultBlock(tool_use_id="tu-shot", content=[png])],
                        parent_tool_use_id="tu-p"),
            _done(),
        ])
        b = engine_patches(stack, cwd=str(tmp_path), adapter=adapter)
        events = await _run(b)
    uses = frames_of(events, "subagent_tool_use")
    assert [(u["agent_id"], u["name"], u["id"]) for u in uses] == [
        ("tu-p", "Bash", "tu-child"), ("tu-p", "Monitor", "tu-mon")]
    results = frames_of(events, "subagent_tool_result")
    assert results[0]["tool_use_id"] == "tu-child" and results[0]["name"] == "Bash"
    assert results[1]["screenshot"].startswith("data:image/png;base64,")
    assert not frames_of(events, "tool_use") and not frames_of(events, "tool_result")
    # Grandchild background work is marked on the dedicated lane only.
    assert task_registry.was_subagent_background_launch(SID, "tu-child")
    assert task_registry.was_subagent_background_launch(SID, "tu-mon")
    assert not task_registry.was_background_launch(SID, "tu-child")


async def test_assistant_lane_launch_failure_settles_immediately(tmp_path):
    with ExitStack() as stack:
        adapter = fresh_adapter([
            _launch("tu-bad", subagent_type="calc"),
            _asst_result("tu-bad", "dispatch rejected", is_error=True),
            _done(),
        ])
        b = engine_patches(stack, cwd=str(tmp_path), adapter=adapter)
        events = await _run(b)
    assert frames_of(events, "subagent_done") == [{
        "type": "subagent_done", "tool_id": "tu-bad", "agent_id": "tu-bad",
        "agent_type": "calc", "result": "dispatch rejected", "is_error": True,
    }]


@pytest.mark.parametrize("reveal", ["started_first", "marked_later"])
async def test_parked_result_later_shown_to_be_background_is_not_flushed(tmp_path, reveal):
    async def _mark_background():
        task_registry.mark_background_launch(SID, "tu-y")

    steps = [_launch("tu-y", subagent_type="calc")]
    if reveal == "started_first":
        steps += [_started("task-y", "tu-y"), _user_result("tu-y", "partial words")]
    else:
        steps += [_user_result("tu-y", "partial words"), _mark_background]
    with ExitStack() as stack:
        adapter = fresh_adapter()
        adapter.receive_events = scripted_events(steps + [_done()])
        b = engine_patches(stack, cwd=str(tmp_path), adapter=adapter)
        events = await _run(b)
    assert not frames_of(events, "subagent_done")
    assert events[-1]["type"] == "done"
