"""SQL may leave the loop; mutable host identity/roster state must not."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fluid.swarm import Launch
from fluid.tools.registry import ToolCtx

from app.core import agent_messaging as messaging, session_runtime, turn_scheduler, swarm
from tests.core.test_owner_delegation import peers, runtime, worker, lineage_threads


async def test_scheduler_keeps_host_state_on_loop(peers, runtime, lineage_threads, monkeypatch):
    worker(runtime, "caller", "peer")
    enqueue = AsyncMock(return_value={"id": "continuation"})
    monkeypatch.setattr(turn_scheduler, "_enqueue_context_wake", enqueue)
    monkeypatch.setattr(turn_scheduler, "kick", lambda _: None)
    result = await turn_scheduler.on_agent_message("peer", "caller", "Revised scope", reply_to="caller")
    assert result == "continuation"
    assert "host-verified parent assignment update" in enqueue.await_args.args[4]
    assert "peer" in lineage_threads
    assert runtime[0].get_task("caller", "peer").get("assignmentOwner", True)


async def test_steer_keeps_host_state_on_loop(peers, runtime, lineage_threads, monkeypatch):
    from app.db import turns
    worker(runtime, "caller", "peer")
    live = turns.enqueue_turn("peer", "user", prompt="ongoing", profile_id="agent")
    assert turns.start_turn(live["id"])
    current = session_runtime.SessionRuntime(live)
    monkeypatch.setitem(session_runtime._live_runtimes, "peer", current)
    adapter = SimpleNamespace(steer=AsyncMock(), steer_peer=AsyncMock(), provider_kind="delegated")
    runtime[1]["peer"].adapter = adapter
    monkeypatch.setattr(turn_scheduler, "kick", lambda _: None)
    queued = await turn_scheduler.on_agent_message("peer", "caller", "Revised scope")
    assert queued
    lineage_threads.clear()
    assert await current.steer_pending() == 1
    assert adapter.steer_peer.await_args.kwargs["provenance"] == "parent"
    assert "peer" in lineage_threads and "caller" in lineage_threads
    # Receipt attribution must use the same loop/thread split as steering.
    assert (await current._peer_note_data("caller", "Revised scope"))["sender_label"] == "main"


async def test_sibling_resume_is_reportable_but_never_a_parent(peers, runtime, monkeypatch):
    from app.core import task_persistence, task_registry as registries
    worker(runtime, "root", "caller")
    worker(runtime, "root", "peer")
    registry = runtime[0]
    registry.complete_task("root", "peer", "completed")
    coordinator = SimpleNamespace(session_id="root", swarms={"s": {"tasks": [
        {"id": "A", "title": "A", "task_id": "caller", "worker_session_id": "caller", "status": "running"},
        {"id": "B", "title": "B", "task_id": "peer", "worker_session_id": "peer", "status": "completed"},
    ]}})
    for sid, name in (("caller", "A"), ("peer", "B")):
        swarm.workers[sid] = Launch(coordinator, "s", name, asyncio.get_running_loop().create_future())
    monkeypatch.setattr(messaging, "resume_unavailable", AsyncMock(return_value=None))
    monkeypatch.setattr(turn_scheduler, "_enqueue_context_wake", AsyncMock(return_value={"id": "resumed"}))
    monkeypatch.setattr(turn_scheduler, "kick", lambda _: None)
    outcome = await messaging.make_message(lambda: "caller")({"to": "B", "message": "Please continue"}, ToolCtx())
    assert not outcome.is_error, outcome.text
    assert "Resume queued" in outcome.text
    assert registry.get_task("caller", "peer")["assignmentOwner"] is False
    assert messaging.live_continuation_owner("peer") == "caller"  # reporting lifecycle retained
    swarm.workers.clear()
    assert await messaging.message_provenance_async("peer", "caller") == "peer"
    assert messaging.peer_rows("peer")[0]["worker_session_id"] == "root"
    registry.complete_task("caller", "peer", "completed")
    assert messaging.message_provenance("peer", "caller") == "peer"  # retained snapshot too
    await task_persistence.flush()
    assert await asyncio.to_thread(messaging._persisted_parents, "peer") == ["root"]
    monkeypatch.setattr(registries, "task_registry", registries.TaskRegistry())
    assert await messaging.message_provenance_async("peer", "caller") == "peer"
    [main] = await messaging.peer_rows_async("peer")
    assert main["worker_session_id"] == "root"
