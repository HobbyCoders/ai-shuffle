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
        assert content.startswith("[agent message from='worker' — a message from another agent in this session")
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

    def may_message(sender, target):
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
