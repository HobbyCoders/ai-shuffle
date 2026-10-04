"""Stopping a Task child on the parent reaches it on the worker that runs it (S8).

Two in-process workers over the real signed control endpoint: Unraid runs the
child, Windows runs the parent chat. No provider, no second machine.
"""
import asyncio

import pytest

from app.core import provider_agent_supervisor, remote_task, swarm, turn_scheduler
from app.core.provider_agent_feed import reset_feed_groups
from app.core.query_session_lifecycle import stop_all_background_tasks, stop_session_task
from app.core.task_registry import task_registry
from app.db import database, remote_tasks, session_events, turns
from app.network import control
from tests.core.network.harness import converge, join
from tests.core.network.test_remote_task_control import (
    UNRAID_URL, all_turns, finish_child, network, started_child, wakes,
)


@pytest.fixture
def kicks(monkeypatch):
    seen = []
    monkeypatch.setattr(turn_scheduler, "kick", seen.append)
    monkeypatch.setattr(turn_scheduler, "_kick_after_batch_window", lambda sid: None)
    return seen


@pytest.fixture(autouse=True)
def _clean_tracking(monkeypatch):
    async def no_local_kill(*args, **kwargs):
        raise AssertionError("a remote child has nothing to kill on this worker")
    monkeypatch.setattr(provider_agent_supervisor, "_kill_worker_session", no_local_kill)
    monkeypatch.setattr(provider_agent_supervisor, "settle_killed_worker", no_local_kill)
    yield
    provider_agent_supervisor.reset()
    reset_feed_groups()
    swarm.coordinators.clear()
    swarm.workers.clear()
    swarm.worker_jobs.clear()


def stop_requests(router):
    return [r for r in router.requests(UNRAID_URL) if r[1].endswith("/" + remote_task.TASK_STOP_ACTION)]


def child_turn(child):
    [turn] = [t for t in all_turns(child) if t["source"] == "provider_agent"]
    return turn


def assert_stopped_on_target(a, b, child):
    """The child's work ended on its worker; its report says stopped and wakes nobody."""
    with a.active():
        assert remote_tasks.get_child(child)["stop_requested_at"]
        assert task_registry.is_user_stopped(child)
        assert child_turn(child)["state"] == "aborted"
        assert asyncio.run(remote_task.sweep()) == 1
        assert remote_tasks.get_child(child)["report"]["status"] == "stopped"
    with b.active():
        assert task_registry.get_task("caller", child)["status"] == "killed"
        assert wakes() == []
        dispatch = remote_tasks.get_dispatch(child)
        assert dispatch["completion_id"] and dispatch["status"] == "stopped"
        assert dispatch["stop_state"] == remote_tasks.STOP_SENT and dispatch["stop_error"] is None


# --- every stop path ------------------------------------------------------------

def test_the_single_task_stop_reaches_the_child_on_its_worker(workers, monkeypatch, kicks):
    a, b, router = network(workers, monkeypatch)
    child = started_child(a, b)
    with b.active():
        result = asyncio.run(stop_session_task("caller", child))
        assert result == {"ok": True, "kind": "agent", "task_id": child}
        assert remote_tasks.get_dispatch(child)["stop_reason"] == "user_stopped"
        assert provider_agent_supervisor.get_worker("caller", child) is None
    assert len(stop_requests(router)) == 1
    assert_stopped_on_target(a, b, child)


def test_stop_on_the_parent_chat_stops_the_remote_child(workers, monkeypatch, kicks):
    a, b, router = network(workers, monkeypatch)
    child = started_child(a, b)
    with b.active():
        results = asyncio.run(stop_all_background_tasks("caller", user_stop=True))
        assert [r.get("task_id") for r in results] == [child]
    assert len(stop_requests(router)) == 1
    assert_stopped_on_target(a, b, child)


def test_stop_all_workers_stops_the_remote_child(workers, monkeypatch, kicks):
    """Also the path session deletion takes."""
    a, b, router = network(workers, monkeypatch)
    child = started_child(a, b)
    with b.active():
        result = asyncio.run(provider_agent_supervisor.stop_all_workers("caller"))
        assert result["stopped"] == 1
        assert result["workers"][0]["outcome"] == "remote_stopped"
    assert len(stop_requests(router)) == 1
    assert_stopped_on_target(a, b, child)


def test_a_running_child_is_interrupted_and_reports_stopped(workers, monkeypatch, kicks):
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)
    with a.active():
        turn = child_turn(child)
        assert turns.start_turn(turn["id"])
        session_events.append_events(child, [{"type": "message.user", "turn_id": turn["id"],
                                              "payload": {"content": "Check the disk."}}])
        session_events.append_events(child, [{"type": "assistant.text_delta", "turn_id": turn["id"],
                                              "payload": {"text": "Looking at the disk."}}])
    with b.active():
        assert asyncio.run(stop_session_task("caller", child))["ok"] is True
    with a.active():
        assert child_turn(child)["state"] == "aborted"
        aborted = [e for e in session_events.get_events(child, 0, 1000)
                   if e["event_type"] == "turn.aborted"]
        assert aborted and aborted[-1]["turn_id"] == turn["id"]
    assert_stopped_on_target(a, b, child)


def test_swarm_cancel_stops_a_remote_item(workers, monkeypatch, kicks):
    from fluid.swarm import SwarmCoordinator
    from fluid.tools.registry import BackgroundTaskReceipt, ToolCtx, ToolOutcome

    a, b, router = network(workers, monkeypatch)

    async def run():
        async def dispatch(data, ctx):
            result = await remote_task.dispatch_remote(
                worker={"device_id": a.device_id, "device_name": "Unraid"},
                caller_row=database.get_session("caller"), caller_session_id="caller",
                root_session_id="caller", depth=1, profile_id="default", agent_name="Default",
                result_agent="default", provider_id=None, model_id="sonnet", effort=None,
                prompt=data["prompt"], label=data["label"], roster_name=None, mode="background",
                launch_tool_use_id="toolu_swarm")
            assert result["ok"] is True, result
            worker_id = result["worker_session_id"]
            return ToolOutcome(text="Started", background_task=BackgroundTaskReceipt(
                task_id=worker_id, worker_session_id=worker_id))

        async def discard(*args):
            pass

        coordinator = SwarmCoordinator("caller", dispatch, swarm.cancel_worker, discard, discard)
        swarm.coordinators["caller"] = coordinator
        snapshot, _ = await coordinator.start(
            {"tasks": [{"id": "disk", "agent": "default", "prompt": "Check the disk."}]}, ToolCtx())
        key = snapshot["id"]
        for _ in range(500):
            if coordinator.swarms[key]["tasks"][0]["task_id"]:
                break
            await asyncio.sleep(0.01)
        item = coordinator.swarms[key]["tasks"][0]
        worker_id = item["task_id"]
        assert worker_id and item["worker_session_id"] == worker_id
        assert swarm.workers[worker_id].task_id == "disk"  # bound by dispatch_remote
        await coordinator.cancel(key)
        await asyncio.wait_for(coordinator.jobs[key], 10)
        return worker_id, coordinator.snapshot(key)

    with b.active():
        child, snapshot = asyncio.run(run())
        assert snapshot["status"] == "cancelled"
        assert snapshot["tasks"][0]["status"] == "cancelled"
        assert task_registry.get_task("caller", child)["status"] == "killed"
        assert remote_tasks.get_dispatch(child)["stop_reason"] == "swarm_cancelled"
    assert len(stop_requests(router)) == 1
    assert_stopped_on_target(a, b, child)


# --- a target that cannot be reached, or cannot stop it ---------------------------

def test_a_stop_the_target_misses_is_sent_when_it_is_back(workers, monkeypatch, kicks):
    a, b, router = network(workers, monkeypatch)
    child = started_child(a, b)
    router.down.add(UNRAID_URL)
    with b.active():
        result = asyncio.run(stop_session_task("caller", child))
        assert result["ok"] is True
        assert "Unraid could not be reached" in result["detail"]
        assert "sent again when it answers" in result["detail"]
        dispatch = remote_tasks.get_dispatch(child)
        assert dispatch["stop_state"] == remote_tasks.STOP_PENDING and dispatch["stop_error"]
        # Still away: a retry leaves it pending.
        assert asyncio.run(remote_task.deliver_stops()) == 0
        assert [r["worker_session_id"] for r in remote_tasks.pending_stops()] == [child]
        # A second stop keeps the first time and reason, and does not wait on
        # a target already known to be away.
        first = remote_tasks.get_dispatch(child)["stop_requested_at"]
        assert asyncio.run(remote_task.check_targets()) == 1

        async def again():
            answer = await remote_task.stop_remote_child(child, a.device_id, reason="session_closed")
            await remote_task.drain_stops()
            return answer
        assert asyncio.run(again())["outcome"] == "remote_stop_pending"
        dispatch = remote_tasks.get_dispatch(child)
        assert dispatch["stop_requested_at"] == first and dispatch["stop_reason"] == "user_stopped"
    with a.active():
        assert remote_tasks.get_child(child)["stop_requested_at"] is None
        assert child_turn(child)["state"] == "queued"
    router.down.clear()
    with b.active():
        assert asyncio.run(remote_task.deliver_stops()) == 1
        assert remote_tasks.pending_stops() == []
        assert asyncio.run(remote_task.deliver_stops()) == 0  # answered once
    assert_stopped_on_target(a, b, child)


def test_an_already_finished_child_keeps_its_report(workers, monkeypatch, kicks):
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)
    with a.active():
        finish_child(child, text="Done before the stop.")  # its report is not stored yet
    with b.active():
        record = provider_agent_supervisor.get_worker("caller", child)
        result = asyncio.run(provider_agent_supervisor.teardown_worker(
            record, reason="user_stopped", summary="Stopped by user", preview="Stopped by the user."))
        assert result["outcome"] == "remote_finished"
        assert remote_tasks.get_dispatch(child)["stop_state"] == remote_tasks.STOP_SENT
    with a.active():
        row = remote_tasks.get_child(child)
        assert row["state"] == remote_tasks.OWED and row["stop_requested_at"]
        assert row["report"]["status"] == "completed"
        # Latched, so a later message cannot resume it.
        assert task_registry.is_user_stopped(child)
        assert asyncio.run(remote_task.sweep()) == 1
    with b.active():
        assert task_registry.get_task("caller", child)["status"] == "killed"
        assert wakes() == []
        assert remote_tasks.get_dispatch(child)["status"] == "completed"
        # A delivered report means nothing more is owed to the target.
        assert remote_tasks.pending_stops() == []


def test_an_unknown_child_answers_unknown(workers, monkeypatch, kicks):
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)
    with a.active():
        answer = asyncio.run(control.handle(
            remote_task.TASK_STOP_ACTION,
            {"version": remote_task.WIRE_VERSION, "worker_session_ids": ["someone-else"]},
            from_device=b.device_id))
        assert answer == {"children": {"someone-else": {"state": remote_task.CHILD_UNKNOWN}}}
        assert child_turn(child)["state"] == "queued"


def test_only_the_worker_that_started_a_child_may_stop_it(workers, monkeypatch, kicks):
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)
    laptop = workers("laptop")
    join(a, laptop, name="Laptop")
    converge([a, b, laptop])
    body = {"version": remote_task.WIRE_VERSION, "worker_session_ids": ["someone-else", child]}
    with a.active():
        with pytest.raises(PermissionError, match="Only the worker that started"):
            asyncio.run(control.handle(remote_task.TASK_STOP_ACTION, body,
                                       from_device=laptop.device_id))
        with pytest.raises(PermissionError):
            remote_task._stop_targets(body, "not-a-member")
        with pytest.raises(ValueError):
            remote_task._stop_targets({**body, "worker_session_ids": []}, b.device_id)
        # Nothing was stopped or latched.
        assert remote_tasks.get_child(child)["stop_requested_at"] is None
        assert child_turn(child)["state"] == "queued"
        assert not task_registry.is_user_stopped(child)


def test_an_older_target_is_named_and_the_stop_is_not_retried(workers, monkeypatch, kicks):
    a, b, router = network(workers, monkeypatch)
    child = started_child(a, b)
    monkeypatch.setattr(control, "known_action", lambda action: action != remote_task.TASK_STOP_ACTION)
    original = control._LAZY_HANDLERS.pop(remote_task.TASK_STOP_ACTION)
    try:
        with b.active():
            result = asyncio.run(stop_session_task("caller", child))
            assert result["ok"] is True
            assert "Unraid runs an older release" in result["detail"]
            assert "stop the subagent in its chat there" in result["detail"]
            dispatch = remote_tasks.get_dispatch(child)
            assert dispatch["stop_state"] == remote_tasks.STOP_REFUSED
            assert "older release" in dispatch["stop_error"]
            assert task_registry.get_task("caller", child)["status"] == "killed"
            assert asyncio.run(remote_task.deliver_stops()) == 0
            assert len(stop_requests(router)) == 1
    finally:
        control._LAZY_HANDLERS[remote_task.TASK_STOP_ACTION] = original
    # It keeps running there; its report, when it comes, wakes nobody.
    with a.active():
        assert child_turn(child)["state"] == "queued"
        finish_child(child, text="Finished on the older worker.")
        assert asyncio.run(remote_task.sweep()) == 1
    with b.active():
        assert task_registry.get_task("caller", child)["status"] == "killed"
        assert wakes() == []


# --- a local-only session sends nothing ------------------------------------------------

def test_stopping_a_local_only_session_sends_nothing(workers, monkeypatch, kicks):
    a, b, router = network(workers, monkeypatch)
    with b.active():
        task_registry.start_task("caller", "local-1", "agent", "Local job", tool_use_id="local-1")
        before = list(router.requests(UNRAID_URL))
        asyncio.run(stop_all_background_tasks("caller", user_stop=True))
        assert task_registry.get_task("caller", "local-1")["status"] != "running"
        assert asyncio.run(remote_task.deliver_stops()) == 0
        assert remote_tasks.pending_stops() == []
    assert router.requests(UNRAID_URL) == before


def test_a_parent_restart_keeps_a_stopped_child_stopped(workers, monkeypatch, kicks):
    from tests.core.network.test_remote_task_tracking import restart_parent

    a, b, router = network(workers, monkeypatch)
    child = started_child(a, b)
    router.down.add(UNRAID_URL)
    with b.active():
        assert asyncio.run(stop_session_task("caller", child))["ok"] is True
        restart_parent()  # before the killed row reached the database
        assert asyncio.run(remote_task.reattach_all()) == 0
        assert provider_agent_supervisor.get_worker("caller", child) is None
        assert task_registry.get_task("caller", child)["status"] != "running"
        assert task_registry.get_active_background_work("caller") == []
    router.down.clear()
    with b.active():
        assert asyncio.run(remote_task.deliver_stops()) == 1
    with a.active():
        assert asyncio.run(remote_task.sweep()) == 1
    with b.active():
        assert wakes() == []
        assert remote_tasks.get_dispatch(child)["status"] == "stopped"


def test_a_stop_does_not_wait_on_a_target_known_to_be_away(workers, monkeypatch, kicks):
    import time

    a, b, router = network(workers, monkeypatch)
    child = started_child(a, b)
    router.down.add(UNRAID_URL)
    with b.active():
        assert asyncio.run(remote_task.check_targets()) == 1
    monkeypatch.setattr(remote_task, "STOP_WAIT", 30.0)

    async def slow_call(*args, **kwargs):
        await asyncio.sleep(0.5)
        raise ConnectionError("connection refused")
    monkeypatch.setattr(control, "call", slow_call)

    async def stop():
        started = time.monotonic()
        answer = await remote_task.stop_remote_child(child, a.device_id, reason="user_stopped")
        elapsed = time.monotonic() - started
        await remote_task.drain_stops()  # the send goes on in the background
        return answer, elapsed
    with b.active():
        answer, elapsed = asyncio.run(stop())
        assert answer["outcome"] == "remote_stop_pending" and "could not be reached" in answer["detail"]
        assert elapsed < 0.4
        dispatch = remote_tasks.get_dispatch(child)
        assert dispatch["stop_state"] == remote_tasks.STOP_PENDING
        assert dispatch["stop_error"] == "connection refused"


def test_a_report_after_a_stop_wakes_nobody_even_without_a_settled_record(workers, monkeypatch, kicks):
    """A Swarm item stopped with no supervisor record (stop_remote_child alone)."""
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)
    with b.active():
        provider_agent_supervisor.unregister_worker("caller", child)
        answer = asyncio.run(remote_task.stop_remote_child(child, a.device_id, reason="swarm_cancelled"))
        assert answer == {"outcome": "remote_stopped"}
        assert task_registry.get_task("caller", child)["status"] == "running"
    with a.active():
        assert asyncio.run(remote_task.sweep()) == 1
    with b.active():
        assert wakes() == []
        assert task_registry.get_task("caller", child)["status"] == "killed"
        assert remote_tasks.get_dispatch(child)["status"] == "stopped"
