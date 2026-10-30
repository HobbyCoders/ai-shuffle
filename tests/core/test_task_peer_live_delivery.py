"""#1618: a Task worker's SendMessage reaches a RUNNING parent mid-turn.

Drives the real producer (``agent_messaging.make_message`` -> the scheduler's
queue -> ``SessionRuntime.steer_pending`` -> ``HarnessAdapter.steer_peer``)
into a real ``DelegatedLoop`` over the fake stream-json CLI. Before the fix
the worker's row was never steer-eligible, so a delegated parent read the
message only after its turn ended; a parent that was also parked held it and
then woke into a separate "promotion" turn.
"""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import agent_messaging as messaging
from app.core import session_runtime, turn_scheduler, wakeup_service
from app.core import task_registry as registry_module
from app.db import database, sessions, turns
from app.db import wakeups as wakeups_db


@pytest.fixture
def parent(mock_db, monkeypatch):
    database.create_profile("agent", "Agent", None, {"model": "sonnet"})
    sessions.create_session("parent", "agent", title="parent", is_active=True,
                            model="sonnet", permission_mode="default")
    registry = registry_module.TaskRegistry()
    monkeypatch.setattr(registry_module, "task_registry", registry)
    registry.start_task("parent", "worker", "agent", "Investigate", agent="Helper")
    monkeypatch.setattr(turn_scheduler, "kick", lambda _: None)
    monkeypatch.setattr(turn_scheduler, "note_wake_pending", lambda _: None)
    yield registry
    for task in list(wakeup_service._timers.values()):
        task.cancel()
    wakeup_service._timers.clear()


@pytest.mark.parametrize("parked", [False, True])
async def test_worker_message_joins_a_running_delegated_parent_at_its_next_tool_boundary(
    parent, monkeypatch, parked,
):
    from fluid.context.steering import STEER_OPEN
    from fluid.engine.delegated import DelegatedLoop
    from fluid.events import (
        PermissionRequest,
        RespondPermission,
        SteerDelivered,
        TextDelta,
        TurnComplete,
        TurnStarted,
    )
    from fluid.events.ops import UserTurn
    from fluid.hooks.bus import HookBus
    from fluid.store.base import MemoryStore
    from fluid.tools.registry import ToolCtx

    from app.core.provider.harness_adapter import HarnessAdapter
    from app.core.turn_recorder import TurnRecorder
    from app.db import session_events

    if parked:
        # The parent parked on background work earlier and the user has since
        # started another turn: running, with the park not yet settled.
        earlier = turns.enqueue_turn("parent", "user", prompt="delegate", profile_id="agent")
        turns.start_turn(earlier["id"], None, "runner")
        turns.park_turn(earlier["id"], ["worker"])

    fake = Path(__file__).resolve().parents[2] / "fluid/tests/fixtures/fake_claude.py"
    store = MemoryStore()
    sdk = (await store.create_session()).session_id
    loop = DelegatedLoop(store=store, session_id=sdk, model="claude-fake", hooks=HookBus(),
                         claude_cmd=[sys.executable, "-u", str(fake)],
                         env={"FAKE_CLAUDE_SCENARIO": "w"})
    frames = []
    original_write = loop._write_frame

    async def spy(frame):
        frames.append(frame)
        await original_write(frame)

    loop._write_frame = spy
    live = turns.enqueue_turn("parent", "user", prompt="original", profile_id="agent")
    assert turns.start_turn(live["id"])
    runtime = session_runtime.SessionRuntime(live)
    monkeypatch.setitem(session_runtime._live_runtimes, "parent", runtime)
    adapter = HarnessAdapter()
    adapter._engine = loop
    adapter._provider_kind = "delegated"
    state = SimpleNamespace(adapter=adapter)
    monkeypatch.setattr("app.core.session_pool.get_session_pool",
                        lambda: SimpleNamespace(get=lambda _: state, peek=lambda _: state))
    send = messaging.make_message(lambda: "worker")
    sent = {}

    async def at_the_prompt():
        async for event in loop.bus.subscribe(sdk):
            if isinstance(event, PermissionRequest):
                try:
                    outcome = await send({"to": "main", "message": "Found the bug"}, ToolCtx())
                    sent["text"] = outcome.text
                    assert not outcome.is_error, outcome.text
                    (row,) = turns.get_queued_turns("parent")
                    sent["row"] = row["id"]
                    # Admitted, not yet delivered: queued, nothing receipted.
                    assert row["source"] == "agent_message"
                    assert row["id"] in runtime._steer_attempted
                    assert not any(isinstance(e, SteerDelivered) for e in loop.bus.log(sdk))
                finally:
                    await loop.submit(RespondPermission(request_id=event.request_id, decision="allow"))
                return

    watcher = asyncio.create_task(at_the_prompt())
    try:
        await loop.submit(UserTurn(content="original admitted turn"))
        await loop.wait_turn()
        await asyncio.wait_for(watcher, 10)
        await loop._discard_stale_stdout()
        assert sent["text"] == "Queued for main"
        assert wakeups_db.get_session_wakeups("parent", wakeup_service.HELD_PEER_SOURCE) == []

        events = loop.bus.log(sdk)
        [receipt] = [e for e in events if isinstance(e, SteerDelivered)]
        complete = next(e for e in events if isinstance(e, TurnComplete))
        assert receipt.seq < complete.seq
        assert receipt.steers == [{"id": sent["row"], "text": "Found the bug", "from": "worker"}]
        seen = json.loads([e.text for e in events if isinstance(e, TextDelta)][-1])
        assert "[agent message from='worker'" in seen["main"] and STEER_OPEN not in seen["main"]
        assert "<system-reminder>" not in seen["main"]
        assert seen["main"].count("Found the bug") == 1
        assert len([f for f in frames if f.get("type") == "user"]) == 1
        assert len([e for e in events if isinstance(e, TurnStarted)]) == 1

        recorder = TurnRecorder("parent", "original-human", None, None, None)
        recorder.turn_id = live["id"]
        steer_event = {"type": "steer_delivered", "steers": receipt.steers}
        await runtime._on_steer_delivered(recorder, steer_event)
        await runtime._on_steer_delivered(recorder, steer_event)
        row = turns.get_turn(sent["row"])
        assert row["state"] == "completed" and row["stop_reason"] == "steered"
        assert turns.get_queued_turns("parent") == []
        delivered = [e for e in session_events.get_events("parent")
                     if e["event_type"] == "message.user"]
        assert len(delivered) == 1 and delivered[0]["turn_id"] == live["id"]
        content = delivered[0]["payload"]["content"]
        # One complete peer envelope, the shape the chat renders as an
        # attributed "Agent message" (frontend agent-delivery.ts).
        assert content.startswith("[agent message from='worker' — a message from another agent,")
        assert content.endswith("\nFound the bug\n[/agent message]")
        assert "<system-reminder>" not in content
    finally:
        watcher.cancel()
        await loop.aclose()


async def test_revoked_worker_message_is_not_steered(parent, monkeypatch):
    """The live hand-over rechecks the roster; a sender that can no longer
    address the parent leaves its row to the ordinary queue."""
    from unittest.mock import AsyncMock, MagicMock

    from fluid.tools.registry import ToolCtx

    live = turns.enqueue_turn("parent", "user", prompt="ongoing", profile_id="agent")
    assert turns.start_turn(live["id"])
    runtime = session_runtime.SessionRuntime(live)
    monkeypatch.setitem(session_runtime._live_runtimes, "parent", runtime)
    adapter = MagicMock(steer=AsyncMock(), steer_peer=AsyncMock(), provider_kind="delegated")
    state = SimpleNamespace(adapter=adapter)
    monkeypatch.setattr("app.core.session_pool.get_session_pool",
                        lambda: SimpleNamespace(get=lambda _: state, peek=lambda _: state))
    real = messaging.may_message
    checks = []

    def may_message(sender, target, **kwargs):
        checks.append((sender, target))
        return False

    monkeypatch.setattr(messaging, "may_message", may_message)
    outcome = await messaging.make_message(lambda: "worker")(
        {"to": "main", "message": "Found the bug"}, ToolCtx(),
    )
    assert not outcome.is_error and outcome.text == "Queued for main"
    assert checks == [("worker", "parent")]
    (row,) = turns.get_queued_turns("parent")
    assert row["state"] == "queued"
    adapter.steer_peer.assert_not_awaited()
    adapter.steer.assert_not_awaited()
    assert not runtime._steer_attempted
    assert real("worker", "parent")  # the roster rule itself, unpatched


@pytest.fixture
async def resumed_child(parent):
    """Resume a completed task through SendMessage, not fabricated payloads."""
    from app.core import provider_agent_supervisor as supervisor

    sessions.create_session("worker", "agent", is_active=True,
                            parent_session_id="parent", is_worker=True)
    previous = turns.enqueue_turn("worker", "user", prompt="original task", profile_id="agent")
    assert turns.start_turn(previous["id"])
    turns.settle_turn(previous["id"], "completed")
    parent.complete_task("parent", "worker", "completed")
    result = await messaging.make_message(lambda: "parent")(
        {"to": "worker", "message": "Resume the investigation"}, None,
    )
    assert not result.is_error, result.text
    (resume,) = turns.get_queued_turns("worker")
    assert resume["payload"]["agent_message_reply_to"] == "parent"
    assert messaging.live_continuation_owner("worker") == "parent"
    try:
        yield resume
    finally:
        supervisor.reset()


def _live_child(monkeypatch, resume, loop, kind):
    from app.core.provider.harness_adapter import HarnessAdapter

    assert turns.start_turn(resume["id"])
    runtime = session_runtime.SessionRuntime(resume)
    monkeypatch.setitem(session_runtime._live_runtimes, "worker", runtime)
    adapter = HarnessAdapter()
    adapter._engine = loop
    adapter._provider_kind = kind
    state = SimpleNamespace(adapter=adapter)
    monkeypatch.setattr("app.core.session_pool.get_session_pool",
                        lambda: SimpleNamespace(get=lambda _: state, peek=lambda _: state))
    return runtime


async def _admit_parent_messages(runtime):
    rows = []
    for i in range(3):
        # The queue producer must infer the live reporting owner and still
        # attribute this individual message for the peer receipt channel.
        if runtime._live_adapter().provider_kind == "delegated":
            result = await messaging.make_message(lambda: "parent")(
                {"to": "worker", "message": f"Parent update {i}"}, None,
            )
            assert not result.is_error and result.text == "Queued for Helper"
            row = turns.get_queued_turns("worker")[-1]
            row_id = row["id"]
            assert row_id in runtime._steer_attempted  # producer triggered the sweep
        else:
            # Exercise the native durable-queue fallback, not its direct-send
            # fast path (whose queue-first durability is a separate change).
            row_id = await turn_scheduler.on_agent_message("worker", "parent", f"Parent update {i}")
            row = turns.get_turn(row_id)
            assert await runtime.steer_pending() == 1
        assert row["payload"]["agent_message_reply_to"] == "parent"
        rows.append(row)
        assert await runtime.steer_pending() == 0  # retries never duplicate it
        assert turns.get_turn(row_id)["state"] == "queued"
    return rows


@pytest.mark.parametrize("kind", ["native", "delegated"])
async def test_resumed_busy_child_receives_three_parent_messages_at_boundaries(
    resumed_child, monkeypatch, kind,
):
    from fluid.context.steering import STEER_OPEN
    from fluid.engine.delegated import DelegatedLoop
    from fluid.engine.native import NativeLoop
    from fluid.events import PermissionRequest, RespondPermission, SteerDelivered, TextDelta, TurnComplete, TurnStarted
    from fluid.events.ops import UserTurn
    from fluid.hooks.bus import HookBus
    from fluid.providers.base import ProviderCaps, StreamChunk
    from fluid.store.base import MemoryStore
    from fluid.tools.registry import ToolRegistry, ToolSpec
    from app.core.turn_recorder import TurnRecorder
    from app.db import session_events

    rows = []
    requests = []
    failures = []
    registry = ToolRegistry()

    async def boundary(data, ctx):
        try:
            rows.extend(await _admit_parent_messages(runtime))
        except AssertionError as exc:
            failures.append(exc)
            raise
        return "tool finished"

    registry.register_builtin(ToolSpec(name="Boundary", description="test boundary",
                                       input_schema={"type": "object"}, risk="safe"), boundary)

    class Provider:
        name = "fake"
        caps = ProviderCaps()

        async def stream(self, request):
            requests.append(request)
            if len(requests) == 1:
                yield StreamChunk("block_start", {"index": 0, "block_type": "tool_use",
                                                  "tool_name": "Boundary", "tool_use_id": "boundary-1"})
                yield StreamChunk("tool_args_delta", {"index": 0, "partial_json": "{}"})
                yield StreamChunk("block_stop", {"index": 0})
                yield StreamChunk("usage", {"usage": {}, "stop_reason": "tool_use"})
            else:
                yield StreamChunk("block_start", {"index": 0, "block_type": "text"})
                yield StreamChunk("text_delta", {"index": 0, "text": "done"})
                yield StreamChunk("block_stop", {"index": 0})
                yield StreamChunk("usage", {"usage": {}, "stop_reason": "end_turn"})

    store = MemoryStore()
    if kind == "native":
        loop = NativeLoop(provider=Provider(), store=store, model="fake", registry=registry)
    else:
        sdk = (await store.create_session()).session_id
        fake = Path(__file__).resolve().parents[2] / "fluid/tests/fixtures/fake_claude.py"
        loop = DelegatedLoop(store=store, session_id=sdk, model="claude-fake", hooks=HookBus(),
                             claude_cmd=[sys.executable, "-u", str(fake)],
                             env={"FAKE_CLAUDE_SCENARIO": "w"})
    runtime = _live_child(monkeypatch, resumed_child, loop, kind)

    async def permissions():
        async for event in loop.bus.subscribe(sdk):
            if isinstance(event, PermissionRequest):
                try:
                    rows.extend(await _admit_parent_messages(runtime))
                finally:
                    await loop.submit(RespondPermission(request_id=event.request_id, decision="allow"))
                return

    watcher = asyncio.create_task(permissions()) if kind == "delegated" else None
    try:
        await loop.submit(UserTurn(content=resumed_child["prompt"]))
        await asyncio.wait_for(loop.wait_turn(), 15)
        if watcher:
            await asyncio.wait_for(watcher, 5)
        events = loop.bus.log(loop.session_id)
        receipts = [e for e in events if isinstance(e, SteerDelivered)]
        complete = next(e for e in events if isinstance(e, TurnComplete))
        assert not failures, failures
        assert len(rows) == 3
        delivered = [s for event in receipts for s in event.steers]
        assert delivered == [{"id": r["id"], "text": f"Parent update {i}", "from": "parent", "origin": "parent"}
                             for i, r in enumerate(rows)]
        assert all(e.seq < complete.seq for e in receipts)
        assert len([e for e in events if isinstance(e, TurnStarted)]) == 1
        if requests:
            from fluid.messages import Text, ToolResultBlock

            seen = "\n".join(part.text for message in requests[-1].messages
                             for block in message.content if isinstance(block, ToolResultBlock)
                             for part in block.content if isinstance(part, Text))
        else:
            seen = json.loads([e.text for e in events if isinstance(e, TextDelta)][-1])["main"]
        from fluid.context.peer import PEER_OPEN_PREFIX

        assert STEER_OPEN not in seen
        assert all(seen.count(f"Parent update {i}") == 1 for i in range(3))
        assert PEER_OPEN_PREFIX in seen
        assert "host-verified parent assignment update" in seen
        recorder = TurnRecorder("worker", "resumed", None, None, None)
        recorder.turn_id = resumed_child["id"]
        for receipt in receipts:
            event = {"type": "steer_delivered", "steers": receipt.steers}
            await runtime._on_steer_delivered(recorder, event)
            await runtime._on_steer_delivered(recorder, event)
        assert turns.get_queued_turns("worker") == []
        assert all(turns.get_turn(r["id"])["stop_reason"] == "steered" for r in rows)
        assert turns.get_queued_turns("parent") == []  # no automatic replies/reports
        messages = [e for e in session_events.get_events("worker") if e["event_type"] == "message.user"]
        assert len(messages) == 3
        assert all(e["turn_id"] == resumed_child["id"] for e in messages)
        assert all(STEER_OPEN not in e["payload"]["content"] for e in messages)
    finally:
        if watcher:
            watcher.cancel()
        await loop.aclose()


async def test_resumed_child_ending_after_admission_keeps_unreceipted_messages_durable(
    resumed_child, monkeypatch,
):
    from fluid.engine.delegated import DelegatedLoop
    from fluid.events import PermissionRequest, RespondPermission, SteerDelivered, TurnStarted
    from fluid.events.ops import UserTurn
    from fluid.hooks.bus import HookBus
    from fluid.store.base import MemoryStore

    store = MemoryStore()
    sdk = (await store.create_session()).session_id
    fake = Path(__file__).resolve().parents[2] / "fluid/tests/fixtures/fake_claude.py"
    loop = DelegatedLoop(store=store, session_id=sdk, model="claude-fake", hooks=HookBus(),
                         claude_cmd=[sys.executable, "-u", str(fake)],
                         env={"FAKE_CLAUDE_SCENARIO": "w", "FAKE_CLAUDE_PEER_NO_BOUNDARY": "1"})
    runtime = _live_child(monkeypatch, resumed_child, loop, "delegated")
    write = loop._write_frame
    frames, rows = [], []

    async def capture(frame):
        frames.append(frame)
        await write(frame)

    async def end_without_boundary(admit, since_seq):
        async for event in loop.bus.subscribe(sdk, since_seq=since_seq):
            if isinstance(event, PermissionRequest):
                try:
                    if admit:
                        rows.extend(await _admit_parent_messages(runtime))
                finally:
                    # The fake CLI ends after permission admission, without
                    # another tool result to carry or receipt these peers.
                    await loop.submit(RespondPermission(request_id=event.request_id, decision="allow"))
                return

    loop._write_frame = capture
    watcher = asyncio.create_task(end_without_boundary(True, loop.bus.last_seq(sdk)))
    try:
        await loop.submit(UserTurn(content=resumed_child["prompt"]))
        await asyncio.wait_for(loop.wait_turn(), 15)
        await asyncio.wait_for(watcher, 5)
        assert len(rows) == 3
        assert not any(isinstance(e, SteerDelivered) for e in loop.bus.log(sdk))
        assert [r["id"] for r in turns.get_queued_turns("worker")] == [r["id"] for r in rows]
        turns.settle_turn(resumed_child["id"], "completed")
        # The ordinary drain owns each surviving row; never run alongside the
        # original turn or manufacture a second copy on turn-end cleanup.
        for row in rows:
            assert loop._turn_task.done()
            assert turns.start_turn(row["id"])
            watcher = asyncio.create_task(end_without_boundary(False, loop.bus.last_seq(sdk)))
            await loop.submit(UserTurn(content=row["prompt"]))
            await asyncio.wait_for(loop.wait_turn(), 15)
            await asyncio.wait_for(watcher, 5)
            turns.settle_turn(row["id"], "completed")
        prompts = [f["message"]["content"] for f in frames if f.get("type") == "user"]
        assert len(prompts) == 4
        assert all(sum(str(p).count(f"Parent update {i}") for p in prompts) == 1 for i in range(3))
        assert len([e for e in loop.bus.log(sdk) if isinstance(e, TurnStarted)]) == 4
        assert turns.get_queued_turns("worker") == []
    finally:
        await loop.aclose()
