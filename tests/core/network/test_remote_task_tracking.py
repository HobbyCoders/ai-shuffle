"""The parent tracks a Task child on another worker like a local background Task (S7).

Two in-process workers over the real signed control endpoint: Unraid runs the
child, Windows runs the parent chat. No provider, no second machine.
"""
import asyncio

import pytest

from app.core import agent_messaging, provider_agent_supervisor, remote_task, startup_reconcile
from app.core import task_persistence, turn_scheduler
from app.core.provider_agent_feed import get_feed_group, reset_feed_groups
from app.core.task_registry import task_registry
from app.db import background_tasks, database, remote_tasks, session_events
from app.db.base import get_db
from app.network import catalog, control
from tests.core.network.harness import converge
from tests.core.network.test_remote_task_control import (
    UNRAID_URL, finish_child, network, started_child, wakes,
)


@pytest.fixture
def kicks(monkeypatch):
    seen = []
    monkeypatch.setattr(turn_scheduler, "kick", seen.append)
    monkeypatch.setattr(turn_scheduler, "_kick_after_batch_window", lambda sid: None)
    return seen


@pytest.fixture(autouse=True)
def _clean_tracking():
    yield
    provider_agent_supervisor.reset()
    reset_feed_groups()


def caller_events(event_type=None):
    rows = session_events.get_events("caller", 0, 1000)
    return [r for r in rows if event_type is None or r["event_type"] == event_type]


def flush():
    asyncio.run(task_persistence.flush())


def deliver(a, child, **kwargs):
    with a.active():
        finish_child(child, **kwargs)
        asyncio.run(remote_task.on_child_settled(child))
        asyncio.run(remote_task.deliver_due())


def restart_parent():
    """Everything in memory goes; the database stays."""
    task_registry.reset_all()
    provider_agent_supervisor.reset()
    reset_feed_groups()
    task_persistence.reset()


# --- listing --------------------------------------------------------------------

def test_a_remote_child_is_listed_like_a_local_background_task(workers, monkeypatch, kicks):
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)
    with b.active():
        [task] = task_registry.get_all("caller")
        assert task["id"] == child and task["status"] == "running" and task["kind"] == "agent"
        assert task["subject"] == "Task · Default" and task["toolUseId"] == child
        assert task["worker"] == {"deviceId": a.device_id, "name": "Unraid"}
        # The park gate and the pool's busy signal count it, and its
        # completion is the caller's own background work (the wake lane).
        assert [t["id"] for t in task_registry.get_active_background_work("caller")] == [child]
        assert task_registry.is_own_background_task("caller", child)
        record = provider_agent_supervisor.get_worker("caller", child)
        assert record.remote_device_id == a.device_id and record.background
        # ListAgents names the worker.
        [row] = agent_messaging.peer_rows("caller")
        assert row["worker_session_id"] == child and row["worker"] == "Unraid"
        assert row["status"] == "running"
        # The card exists, with a running chip, like a local worker's.
        assert len(caller_events("subagent.started")) == 1
        flush()
        durable = background_tasks.get_background_task(child, "caller")
        assert durable["status"] == "running" and durable["remote_device_id"] == a.device_id
        # The session's running-task count includes it.
        presence = background_tasks.get_running_task_presence(["caller"])
        assert presence["caller"]["has_running_tasks"] is True


def test_the_card_shows_progress_from_the_mirrored_child(workers, monkeypatch, kicks):
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)
    with a.active():
        session_events.append_events(child, [
            {"type": "assistant.text_delta", "payload": {"text": "Looking at the disk."}},
            {"type": "tool.use", "payload": {"tool_use_id": "t1", "name": "Bash", "input": {"command": "df"}}},
        ])
        catalog.publish_owned()
    converge([a, b])
    with b.active():
        assert session_events.get_max_seq(child) >= 2  # the mirror arrived
        emitted = asyncio.run(get_feed_group("caller").tick())
        assert emitted == 2
        progress = caller_events("subagent.progress")
        assert len(progress) == 2
        assert "Looking at the disk." in str(progress[0]["payload"])
        assert "Bash" in str(progress[1]["payload"])
        # Nothing new: nothing forwarded twice.
        assert asyncio.run(get_feed_group("caller").tick()) == 0


# --- status transitions -----------------------------------------------------------

def test_the_report_settles_the_record_card_and_supervisor_then_wakes_once(workers, monkeypatch, kicks):
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)
    deliver(a, child, text="Disk is 40% full.\nDetails follow.")
    with b.active():
        task = task_registry.get_task("caller", child)
        assert task["status"] == "completed" and task["summary"] == "Disk is 40% full."
        assert task["reportPath"] and task["reportChars"] == len("Disk is 40% full.\nDetails follow.")
        assert task["worker"]["name"] == "Unraid"
        assert provider_agent_supervisor.get_worker("caller", child) is None
        assert task_registry.get_active_background_work("caller") == []
        assert not task_registry.has_unresolved_background_launches("caller")
        [done] = caller_events("subagent.completed")
        assert "Disk is 40% full." in str(done["payload"])
        assert len(wakes()) == 1
        flush()
        assert background_tasks.get_background_task(child, "caller")["status"] == "completed"
        assert background_tasks.get_running_task_presence(["caller"])["caller"]["has_running_tasks"] is False


def test_a_failed_child_settles_as_an_error(workers, monkeypatch, kicks):
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)
    deliver(a, child, text="The provider refused the request.", outcome="aborted")
    with b.active():
        assert task_registry.get_task("caller", child)["status"] == "error"
        assert "<status>error</status>" in wakes()[0]["prompt"]


def test_a_child_stopped_here_keeps_its_status_and_its_report_wakes_nobody(workers, monkeypatch, kicks):
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)

    async def no_local_kill(*args, **kwargs):
        raise AssertionError("a remote child has nothing to kill on this worker")
    monkeypatch.setattr(provider_agent_supervisor, "_kill_worker_session", no_local_kill)
    monkeypatch.setattr(provider_agent_supervisor, "settle_killed_worker", no_local_kill)
    with b.active():
        record = provider_agent_supervisor.get_worker("caller", child)
        result = asyncio.run(provider_agent_supervisor.teardown_worker(
            record, reason="user_stopped_all", summary="Stopped by user (stop all workers)",
            preview="Stopped by the user."))
        assert result["outcome"] == "remote_stopped" and "detail" not in result
        assert task_registry.get_task("caller", child)["status"] == "killed"
        assert remote_tasks.get_dispatch(child)["stop_state"] == remote_tasks.STOP_SENT
    with a.active():
        # Stopped on its worker: its queued turn is aborted and it reports so.
        assert remote_tasks.get_child(child)["stop_requested_at"]
        assert asyncio.run(remote_task.sweep()) == 1
        assert remote_tasks.get_child(child)["report"]["status"] == "stopped"
    with b.active():
        assert task_registry.get_task("caller", child)["status"] == "killed"
        assert wakes() == []
        # The report is still stored and its delivery recorded.
        assert remote_tasks.get_dispatch(child)["completion_id"]


# --- restart recovery ----------------------------------------------------------------

def test_a_parent_restart_keeps_the_child_running_and_re_attaches_it(workers, monkeypatch, kicks):
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)
    with a.active():
        session_events.append_events(child, [
            {"type": "assistant.text_delta", "payload": {"text": "Before the restart."}}])
        catalog.publish_owned()
    converge([a, b])
    with b.active():
        flush()
        # A local worker of the same session, also running when the process died.
        task_registry.start_task("caller", "local-1", "agent", "Local job")
        flush()
        with get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET parked_task_kind='agent' WHERE id='caller'")
        progress_before = len(caller_events("subagent.progress"))
        restart_parent()

        # Boot reconcile: the local row dies with the process, the remote one
        # does not, and the park it holds stays.
        startup_reconcile._clear_stale_parked_markers()
        assert database.get_session("caller")["parked_task_kind"] == "agent"
        asyncio.run(startup_reconcile._mark_stale_background_task_rows())
        assert background_tasks.get_background_task("local-1", "caller")["status"] == "killed"
        assert background_tasks.get_background_task(child, "caller")["status"] == "running"

        assert asyncio.run(remote_task.reattach_all()) == 1
        assert asyncio.run(remote_task.reattach_all()) == 0  # idempotent
        task = task_registry.get_task("caller", child)
        assert task["status"] == "running" and task["worker"]["name"] == "Unraid"
        assert task_registry.get_task("caller", "local-1")["status"] == "interrupted"
        assert [t["id"] for t in task_registry.get_active_background_work("caller")] == [child]
        assert provider_agent_supervisor.get_worker("caller", child).remote_device_id == a.device_id
        # The card is resumed, not restarted, and nothing is forwarded twice.
        assert len(caller_events("subagent.started")) == 1
        assert asyncio.run(get_feed_group("caller").tick()) == 0
        assert len(caller_events("subagent.progress")) == progress_before
    deliver(a, child, text="Done after the restart.")
    with b.active():
        assert task_registry.get_task("caller", child)["status"] == "completed"
        [wake] = wakes()
        assert "Done after the restart." in wake["prompt"]
        assert len(caller_events("subagent.completed")) == 1


def test_a_report_that_arrives_before_re_attach_still_settles_the_record(workers, monkeypatch, kicks):
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)
    with b.active():
        flush()
        restart_parent()
    deliver(a, child, text="Arrived first.")
    with b.active():
        task = task_registry.get_task("caller", child)
        assert task["status"] == "completed" and task["worker"]["name"] == "Unraid"
        assert len(wakes()) == 1
        assert asyncio.run(remote_task.reattach_all()) == 0


# --- quiet target ------------------------------------------------------------------

def test_a_quiet_target_is_marked_unreachable_and_cleared_when_it_answers(workers, monkeypatch, kicks):
    a, b, router = network(workers, monkeypatch)
    child = started_child(a, b)
    router.down.add(UNRAID_URL)
    with b.active():
        assert asyncio.run(remote_task.check_targets()) == 1
        since = remote_tasks.get_dispatch(child)["unreachable_since"]
        assert since
        task = task_registry.get_task("caller", child)
        assert task["unreachableSince"] == since
        # Still running: the park holds and the report is still accepted.
        assert task["status"] == "running"
        assert task_registry.get_active_background_work("caller")
        [row] = agent_messaging.peer_rows("caller")
        assert row["unreachable_since"] == since
        # A second silent check keeps the first time it went quiet.
        assert asyncio.run(remote_task.check_targets()) == 1
        assert remote_tasks.get_dispatch(child)["unreachable_since"] == since
    router.down.clear()
    with b.active():
        assert asyncio.run(remote_task.check_targets()) == 0
        dispatch = remote_tasks.get_dispatch(child)
        assert dispatch["unreachable_since"] is None and dispatch["last_contact_at"]
        assert dispatch["target_state"] == remote_tasks.RUNNING
        assert "unreachableSince" not in task_registry.get_task("caller", child)


def test_task_status_answers_only_for_the_asking_workers_children(workers, monkeypatch, kicks):
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)
    with a.active():
        answer = asyncio.run(control.handle(
            remote_task.TASK_STATUS_ACTION,
            {"version": remote_task.WIRE_VERSION, "worker_session_ids": [child, "someone-else"]},
            from_device=b.device_id))
        assert answer == {"children": {child: {"state": "running"},
                                       "someone-else": {"state": remote_task.CHILD_UNKNOWN}}}
        with pytest.raises(PermissionError):
            remote_task.answer_task_status(
                {"version": remote_task.WIRE_VERSION, "worker_session_ids": [child]},
                from_device="not-a-member")
        with pytest.raises(ValueError):
            remote_task.answer_task_status(
                {"version": remote_task.WIRE_VERSION, "worker_session_ids": []},
                from_device=b.device_id)


def test_an_older_target_that_refuses_task_status_counts_as_reachable(workers, monkeypatch, kicks):
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)
    monkeypatch.setattr(control, "known_action", lambda action: action != remote_task.TASK_STATUS_ACTION)
    original = control._LAZY_HANDLERS.pop(remote_task.TASK_STATUS_ACTION)
    try:
        with b.active():
            assert asyncio.run(remote_task.check_targets()) == 0
            assert remote_tasks.get_dispatch(child)["unreachable_since"] is None
    finally:
        control._LAZY_HANDLERS[remote_task.TASK_STATUS_ACTION] = original


# --- a local-only session is unaffected ---------------------------------------------------

def test_a_local_task_keeps_its_shape_and_nothing_is_checked(workers, monkeypatch, kicks):
    a, b, router = network(workers, monkeypatch)
    with b.active():
        task_registry.start_task("caller", "local-1", "agent", "Local job", tool_use_id="local-1")
        wire = task_registry.get_task("caller", "local-1")
        assert not {"worker", "workerState", "unreachableSince"} & set(wire)
        assert task_registry.update_remote_status("caller", "local-1", unreachable_since="x") is False
        assert "unreachableSince" not in task_registry.get_task("caller", "local-1")
        before = list(router.requests(UNRAID_URL))
        assert asyncio.run(remote_task.check_targets()) == 0
        assert asyncio.run(remote_task.reattach_all()) == 0
        assert router.requests(UNRAID_URL) == before
        # A running local row is still not brought back after a restart.
        flush()
        restart_parent()
        assert asyncio.run(task_registry.ensure_hydrated("caller")) == 0
        assert task_registry.get_task("caller", "local-1") is None
