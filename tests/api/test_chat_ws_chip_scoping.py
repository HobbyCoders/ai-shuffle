"""Chip flood scoping + coalescing (lifecycle-notification batch, item 3).

Two chip-flood bugs in the between-turns drain pump:

1. SCOPING: the pump's chip converter and subagent-inner re-dispatch were
   UNGATED — a grandchild task (launched BY a sub-agent, never tracked by the
   parent) that patched its status while the parent was parked rendered "Task
   Updated" chips (and inner activity) in the PARENT's chat. Only the parent's
   OWN background work may chip: ``task_registry.is_own_background_task``
   mirrors the unpark admission set (``is_sdk_background_task`` OR the record's
   launch ``tool_use_id`` ∈ ``was_background_launch``).

2. COALESCING: every TaskUpdated patch chipped, so a task that reported
   running/running/running/completed rendered FOUR chips. New registry-level
   ``mark_chipped(session_id, task_id, phase)`` one-shot latches cap a task at
   max TWO chips for its whole lifetime: first sighting + first terminal;
   intermediate running/paused patches render nothing.

All tests drive the REAL drain pump reader (``_drain_notifications_impl`` via
the handler) with real SDK message types and the real task registry — only
``dispatch_event`` is captured so the dispatched chip events can be counted.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.api.chat_ws_handler as cwh
import app.api.ws_cleanup as ws_cleanup
from app.core.task_registry import task_registry
from claude_agent_sdk import (
    AssistantMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    TextBlock,
)


SESSION = "sess-chips"


def _started(task_id, *, tool_use_id, description="bg work", task_type=None):
    kwargs = dict(
        subtype="task_started",
        data={},
        task_id=task_id,
        description=description,
        uuid=f"s-{task_id}",
        session_id=SESSION,
        tool_use_id=tool_use_id,
    )
    if task_type is not None:
        kwargs["task_type"] = task_type
    return TaskStartedMessage(**kwargs)


def _updated(task_id, *, status, uuid=None):
    # ``data`` mirrors what the real SDK frame carries (the chip converter
    # forwards it verbatim as the system event's ``data``).
    return TaskUpdatedMessage(
        subtype="task_updated",
        data={"task_id": task_id, "status": status, "patch": {"status": status}},
        task_id=task_id,
        patch={"status": status},
        status=status,
        session_id=SESSION,
        uuid=uuid or f"uu-{task_id}-{status}",
    )


class _FakeClient:
    def __init__(self, messages, *, hang=False):
        self._messages = list(messages)
        self._hang = hang

    async def receive_messages(self):
        for msg in self._messages:
            yield msg
        if self._hang:
            await asyncio.Event().wait()


def _fake_state(client, *, is_streaming=False, alive=True):
    state = MagicMock()
    state.is_streaming = is_streaming
    state.stale_turn_undrained = False
    state.client = client
    adapter = MagicMock()
    adapter.is_alive = MagicMock(return_value=alive)
    state.adapter = adapter
    return state


def _make_handler():
    handler = cwh.ChatWebSocketHandler(
        websocket=MagicMock(),
        device_id="dev-1",
        user_id="admin",
        api_user_id=None,
    )
    handler.send_json = AsyncMock(return_value=True)
    handler._deliver_unpark = AsyncMock(return_value=True)
    return handler


def _patch_pool(state):
    pool = MagicMock()
    pool.peek = MagicMock(return_value=state)
    return patch("app.core.session_pool.get_session_pool", return_value=pool)


@pytest.fixture(autouse=True)
def _isolate():
    task_registry.reset_all()
    ws_cleanup._drain_pumps.clear()
    _saved_window = cwh._UNPARK_BATCH_WINDOW_SECONDS
    cwh._UNPARK_BATCH_WINDOW_SECONDS = 0.05
    yield
    cwh._UNPARK_BATCH_WINDOW_SECONDS = _saved_window
    ws_cleanup._drain_pumps.clear()
    task_registry.reset_all()


async def _run_pump(handler, messages):
    """Run the REAL pump over scripted messages, capturing dispatched events."""
    captured = []

    async def _capture(event, ectx, st):
        captured.append(event)

    client = _FakeClient(messages, hang=False)
    state = _fake_state(client)
    with _patch_pool(state), patch(
        "app.api.ws_event_handlers.dispatch_event",
        new=AsyncMock(side_effect=_capture),
    ):
        await handler._drain_notifications(SESSION, "claude-code", None, None)
    return captured


def _chips(events):
    return [e for e in events if e.get("type") == "system"]


# ---------------------------------------------------------------------------
# Registry primitives
# ---------------------------------------------------------------------------

def test_is_own_background_task_mirrors_unpark_admission_set():
    # SDK-backgrounded shell: owned via is_sdk_background_task.
    task_registry.start_task(SESSION, "sh-1", "shell", "bg shell")
    task_registry.mark_sdk_background_task(SESSION, "sh-1")
    assert task_registry.is_own_background_task(SESSION, "sh-1") is True

    # Background agent: owned via its record's launch tool_use_id being a
    # recorded run_in_background launch.
    task_registry.start_task(SESSION, "ag-1", "agent", "bg agent",
                             tool_use_id="tu-own")
    task_registry.mark_background_launch(SESSION, "tu-own")
    assert task_registry.is_own_background_task(SESSION, "ag-1") is True

    # Grandchild: has a registry record (the pump feeds every TaskStarted) but
    # its launch tool_use_id was issued by the SUB-agent, never the parent.
    task_registry.start_task(SESSION, "gc-1", "agent", "grandchild",
                             tool_use_id="tu-grandchild")
    assert task_registry.is_own_background_task(SESSION, "gc-1") is False

    # Unknown / missing ids.
    assert task_registry.is_own_background_task(SESSION, "nope") is False
    assert task_registry.is_own_background_task(SESSION, "") is False
    assert task_registry.is_own_background_task("", "sh-1") is False


def test_mark_chipped_one_shot_per_phase():
    assert task_registry.mark_chipped(SESSION, "t1", "start") is True
    assert task_registry.mark_chipped(SESSION, "t1", "start") is False
    assert task_registry.mark_chipped(SESSION, "t1", "terminal") is True
    assert task_registry.mark_chipped(SESSION, "t1", "terminal") is False
    # Distinct task: independent latches.
    assert task_registry.mark_chipped(SESSION, "t2", "terminal") is True
    # Defensive: missing ids never latch.
    assert task_registry.mark_chipped("", "t1", "start") is False
    assert task_registry.mark_chipped(SESSION, "", "start") is False
    assert task_registry.mark_chipped(SESSION, "t1", "") is False
    # Cleared with the session.
    task_registry.clear_session(SESSION)
    assert task_registry.mark_chipped(SESSION, "t1", "start") is True


# ---------------------------------------------------------------------------
# Scoping: a grandchild's TaskUpdated must NOT chip in the parent chat
# ---------------------------------------------------------------------------

async def test_grandchild_task_updated_renders_no_chip():
    handler = _make_handler()
    # The grandchild's TaskStarted also reaches the pump (it feeds the registry
    # — that part must keep working); its launch tool_use_id belongs to the
    # SUB-agent, so the parent has no background-launch record for it.
    captured = await _run_pump(handler, [
        _started("gc-task", tool_use_id="tu-grandchild"),
        _updated("gc-task", status="running"),
        _updated("gc-task", status="completed"),
    ])

    assert _chips(captured) == [], (
        "a grandchild task's status patches must not render chips in the "
        f"parent chat (got {_chips(captured)})"
    )
    # Registry side-effects unaffected by the chip gate: the task completed.
    rec = task_registry.get_task(SESSION, "gc-task")
    assert rec is not None and rec["status"] == "completed"


# ---------------------------------------------------------------------------
# Scoping: a grandchild's inner activity must NOT re-dispatch into the parent
# ---------------------------------------------------------------------------

async def test_unowned_subagent_inner_activity_not_redispatched():
    handler = _make_handler()
    inner = AssistantMessage(
        content=[TextBlock("grandchild step")], model="claude",
        parent_tool_use_id="tu-grandchild",
    )
    captured = await _run_pump(handler, [inner])
    assert [e for e in captured if e.get("type", "").startswith("subagent_")] == []


async def test_owned_subagent_inner_activity_still_redispatched():
    task_registry.mark_background_launch(SESSION, "tu-own")
    handler = _make_handler()
    inner = AssistantMessage(
        content=[TextBlock("own step")], model="claude",
        parent_tool_use_id="tu-own",
    )
    captured = await _run_pump(handler, [inner])
    chunks = [e for e in captured if e.get("type") == "subagent_chunk"]
    assert len(chunks) == 1 and chunks[0]["agent_id"] == "tu-own"


# ---------------------------------------------------------------------------
# Coalescing: 3×running + terminal → exactly TWO chips (start + terminal)
# ---------------------------------------------------------------------------

async def test_owned_task_running_x3_plus_terminal_yields_two_chips():
    task_registry.mark_background_launch(SESSION, "tu-own")
    handler = _make_handler()
    captured = await _run_pump(handler, [
        _started("t-own", tool_use_id="tu-own"),
        _updated("t-own", status="running", uuid="u1"),
        _updated("t-own", status="running", uuid="u2"),
        _updated("t-own", status="running", uuid="u3"),
        _updated("t-own", status="completed", uuid="u4"),
    ])

    chips = _chips(captured)
    assert len(chips) == 2, (
        f"expected exactly 2 chips (first sighting + terminal), got "
        f"{len(chips)}: {chips}"
    )
    # First sighting chip carries the first running patch; terminal chip the
    # completed patch.
    assert (chips[0].get("data") or {}).get("patch", {}).get("status") == "running"
    assert (chips[1].get("data") or {}).get("patch", {}).get("status") == "completed"


async def test_intermediate_patches_after_first_sighting_render_nothing():
    task_registry.mark_background_launch(SESSION, "tu-own")
    handler = _make_handler()
    captured = await _run_pump(handler, [
        _started("t-own", tool_use_id="tu-own"),
        _updated("t-own", status="running", uuid="u1"),
        _updated("t-own", status="paused", uuid="u2"),
        _updated("t-own", status="running", uuid="u3"),
    ])
    chips = _chips(captured)
    assert len(chips) == 1  # first sighting only — no terminal yet


async def test_chip_latches_hold_across_pump_runs():
    """The latch lives in the registry (not the per-run set), so a SECOND pump
    run re-reading a duplicate terminal patch renders no duplicate chip."""
    task_registry.mark_background_launch(SESSION, "tu-own")
    handler = _make_handler()
    first = await _run_pump(handler, [
        _started("t-own", tool_use_id="tu-own"),
        _updated("t-own", status="completed", uuid="u1"),
    ])
    # Second pump run (fresh _chipped_task_ids) replays the same terminal patch.
    second = await _run_pump(handler, [
        _updated("t-own", status="completed", uuid="u2"),
    ])
    assert len(_chips(first)) == 1
    assert _chips(second) == []
