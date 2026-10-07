"""The parent tracks a Task child on another worker like a local background Task (S7).

Two in-process workers over the real signed control endpoint: Unraid runs the
child, Windows runs the parent chat. No provider, no second machine.
"""
import asyncio
import copy
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fluid.tools.registry import ToolCtx

from app.core import agent_messaging, provider_agent_supervisor, remote_task, startup_reconcile
from app.core import swarm, task_dispatch, task_persistence, turn_scheduler
from app.core.provider_agent_feed import get_feed_group, reset_feed_groups
from app.core.task_registry import task_registry
from app.db import background_tasks, database, remote_tasks, session_events
from app.db.base import get_db
from app.network import catalog, control
from tests.core.network.harness import converge
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
def _clean_tracking():
    yield
    provider_agent_supervisor.reset()
    reset_feed_groups()
    swarm.coordinators.clear()
    swarm.workers.clear()
    swarm.worker_jobs.clear()
    swarm._locks.clear()


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


def test_a_quiet_target_shows_no_stale_worker_state(workers, monkeypatch, kicks):
    """The mirror of a stopped worker still says "working"; while the worker is
    unreachable the record reports only that, not the stale state (#1808)."""
    a, b, router = network(workers, monkeypatch)
    child = started_child(a, b)
    monkeypatch.setattr(remote_task, "_mirrored_states", lambda ids: {i: "working" for i in ids})
    with b.active():
        assert asyncio.run(remote_task.check_targets()) == 0
        assert task_registry.get_task("caller", child)["workerState"] == "working"
    router.down.add(UNRAID_URL)
    with b.active():
        assert asyncio.run(remote_task.check_targets()) == 1
        task = task_registry.get_task("caller", child)
        assert task["unreachableSince"]
        assert "workerState" not in task
    router.down.clear()
    with b.active():
        assert asyncio.run(remote_task.check_targets()) == 0
        task = task_registry.get_task("caller", child)
        assert "unreachableSince" not in task and task["workerState"] == "working"


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
    monkeypatch.delitem(control._LAZY_HANDLERS, remote_task.TASK_STATUS_ACTION)
    monkeypatch.setattr(remote_task, "_tracking_started", None)
    now = datetime.now().astimezone()
    with b.active():
        assert asyncio.run(remote_task.check_targets(now=now)) == 0
        assert asyncio.run(remote_task.check_targets(
            now=now + timedelta(seconds=remote_task.OFFLINE_NOTICE_SECONDS + 1))) == 0
        dispatch = remote_tasks.get_dispatch(child)
        assert dispatch["unreachable_since"] is None
        assert dispatch["unconfirmed_start_at"] is None
        assert dispatch["offline_notice_at"] is None
        assert task_registry.get_task("caller", child)["status"] == "running"
        assert wakes() == []
    deliver(a, child)
    with b.active():
        assert task_registry.get_task("caller", child)["status"] == "completed"
        assert asyncio.run(remote_task.check_targets(
            now=now + timedelta(seconds=remote_task.OFFLINE_NOTICE_SECONDS + 2))) == 0
        assert len(wakes()) == 1


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


# --- remote Swarm: real dispatcher, signed control and report producer -----------

def swarm_network(workers, monkeypatch):
    a, b, router = network(workers, monkeypatch)
    for worker in (a, b):
        with worker.active():
            from app.db import mobile_tool_identity
            mobile_tool_identity.init_schema()
            database.create_profile("helper", "Helper", None, {"model": "sonnet"})
            from app.network import engine
            engine.publish_presence(capabilities={"remote_task": 1})
    converge([a, b])
    return a, b, router


async def start_swarm(monkeypatch, *, dependent=True, extra_tasks=()):
    published = asyncio.Queue()
    persist = swarm.persist

    async def publish(snapshot):
        await persist(snapshot)
        published.put_nowait(copy.deepcopy(snapshot))

    monkeypatch.setattr(swarm, "persist", publish)
    ctx = ToolCtx(extra={"task_dispatch": task_dispatch.make_fluid_dispatcher("caller")})
    outcome = await swarm.make_handler(lambda: "caller")({"action": "start", "max_parallel": 1,
        "tasks": [
            {"id": "first", "agent": "helper", "worker": "Unraid", "prompt": "Check the disk."},
            {"id": "second", "agent": "helper", "worker": "Unraid", "prompt": "Review findings.",
             "depends_on": ["first"] if dependent else []},
        ] + list(extra_tasks)}, ctx)
    assert not outcome.is_error, outcome.text
    key = outcome.background_task.task_id
    return swarm.coordinators["caller"], key, published


async def admitted(published, index):
    async def next_receipt():
        while True:
            snapshot = await published.get()
            row = snapshot["tasks"][index]
            if row["task_id"] or snapshot["status"] in swarm.TERMINAL_STATUSES:
                assert row["task_id"], str(snapshot)
                return snapshot
    return await asyncio.wait_for(next_receipt(), 5)


async def report_from_target(a, child, **kwargs):
    with a.active():
        finish_child(child, **kwargs)
        assert remote_task.owe_if_finished(child)
        body = remote_tasks.get_child(child)["report"]
        assert await remote_task.deliver_due() == 1
        return body


@pytest.mark.parametrize("dependent", [False, True])
def test_remote_swarm_holds_slot_routes_messages_and_uses_actual_report(workers, monkeypatch, kicks, dependent):
    a, b, router = swarm_network(workers, monkeypatch)
    local_resume = AsyncMock(side_effect=AssertionError("remote child must never resume locally"))
    monkeypatch.setattr(agent_messaging, "resume_unavailable", local_resume)

    async def scenario():
        with b.active():
            coordinator, key, published = await start_swarm(monkeypatch, dependent=dependent)
            first_snapshot = await admitted(published, 0)
            first, second = first_snapshot["tasks"]
            child = first["worker_session_id"]
            assert first["status"] == "running" and second["status"] == "queued"
            assert second["worker_session_id"] is None
            assert not coordinator.jobs[key].done() and wakes() == []
            assert sum(path.endswith("/task-start") for _, path in router.requests(UNRAID_URL)) == 1
            peers = await agent_messaging.make_roster(lambda: "caller")({"running_only": True}, None)
            [peer] = [row for row in peers if row["worker_session_id"] == child]
            assert peer["name"] == "first" and peer["worker"] == "Unraid" and peer["status"] == "running"
            for address in ("first", child):
                message = await agent_messaging.make_message(lambda: "caller")(
                    {"to": address, "message": "Also check the logs."}, None)
                assert not message.is_error, message.text
            assert ("POST", "/api/v1/network/peer/control/task-message") in router.requests(UNRAID_URL)
            with a.active():
                assert any("Also check the logs." in t["prompt"] for t in all_turns(child)
                           if t["source"] == "agent_message")
                # Model messages consumed by the mocked child's live turn, not
                # separate blank runs that could become its latest report turn.
                from app.db import turns
                for turn in all_turns(child):
                    if turn["source"] == "agent_message":
                        assert turns.absorb_queued_turn(turn["id"])
                assert all(t["stop_reason"] == "steered" for t in all_turns(child)
                           if t["source"] == "agent_message")
            body = await report_from_target(a, child, text="Actual disk findings, not a launch receipt.")
            assert body["report"] == "Actual disk findings, not a launch receipt."
            assert wakes() == []  # child's report belongs to the aggregate
            snapshot = await admitted(published, 1)
            assert snapshot["tasks"][0]["result"] == body["report"]
            second_child = snapshot["tasks"][1]["worker_session_id"]
            assert snapshot["tasks"][1]["status"] == "running"
            with a.active():
                prompt = remote_task.report_owed(second_child)
                assert prompt["owner_session_id"] == "caller"
                [turn] = [t for t in all_turns(second_child) if t["source"] == "provider_agent"]
                assert (body["report"] in turn["prompt"]) is dependent
            assert (await remote_task.handle_task_report(body, a.device_id))["duplicate"]
            assert wakes() == []
            second_body = await report_from_target(a, second_child, text="Actual review report.")
            assert second_body["report"] == "Actual review report."
            await asyncio.wait_for(coordinator.jobs[key], 5)
            snapshot = coordinator.snapshot(key)
            assert snapshot["status"] == "completed"
            assert [t["result"] for t in snapshot["tasks"]] == [body["report"], second_body["report"]]
            [wake] = wakes()
            assert key in wake["payload"]["task_ids"]
            assert "Actual disk findings" in wake["prompt"] and "Actual review report" in wake["prompt"]
            assert (await remote_task.handle_task_report(second_body, a.device_id))["duplicate"]
            assert len(wakes()) == 1
            local_resume.assert_not_awaited()
            await task_persistence.flush()

    asyncio.run(scenario())


@pytest.mark.parametrize("ending", ["failed", "cancelled"])
def test_remote_swarm_failure_and_cancel_block_successor_and_wake_once(workers, monkeypatch, kicks, ending):
    a, b, router = swarm_network(workers, monkeypatch)
    local_kill = AsyncMock(side_effect=AssertionError("remote child must never be killed locally"))
    monkeypatch.setattr(provider_agent_supervisor, "_kill_worker_session", local_kill)

    async def scenario():
        with b.active():
            coordinator, key, published = await start_swarm(monkeypatch)
            snapshot = await admitted(published, 0)
            child = snapshot["tasks"][0]["worker_session_id"]
            if ending == "failed":
                body = await report_from_target(a, child, text="Provider refused the request.", outcome="aborted")
            else:
                await coordinator.cancel(key)
                assert ("POST", "/api/v1/network/peer/control/task-stop") in router.requests(UNRAID_URL)
                with a.active():
                    assert remote_tasks.get_child(child)["stop_requested_at"]
                    assert remote_task.owe_if_finished(child)
                    body = remote_tasks.get_child(child)["report"]
                    assert body["status"] == "stopped"
                    assert await remote_task.deliver_due() == 1
            await asyncio.wait_for(coordinator.jobs[key], 5)
            snapshot = coordinator.snapshot(key)
            assert snapshot["status"] == ending
            assert snapshot["tasks"][0]["status"] == ending
            assert snapshot["tasks"][1]["status"] == ("blocked" if ending == "failed" else "cancelled")
            assert snapshot["tasks"][1]["worker_session_id"] is None
            assert sum(path.endswith("/task-start") for _, path in router.requests(UNRAID_URL)) == 1
            assert (await remote_task.handle_task_report(body, a.device_id))["duplicate"]
            assert len(wakes()) == 1
            local_kill.assert_not_awaited()
            await task_persistence.flush()

    asyncio.run(scenario())


@pytest.mark.parametrize("answer_kind", ["fast_report", "invalid", "timeout", "refused", "unreachable"])
def test_remote_swarm_start_races_keep_report_and_routing_ownership(workers, monkeypatch, kicks, answer_kind):
    a, b, _ = swarm_network(workers, monkeypatch)
    real_call = control.call
    starts, early_reports = [], []

    async def call(device_id, action, body, **kwargs):
        if action != remote_task.TASK_START_ACTION or starts:
            return await real_call(device_id, action, body, **kwargs)
        starts.append(body)
        if answer_kind == "refused":
            raise control.ControlRefused("Nothing was started")
        if answer_kind == "unreachable":
            raise control.RunnerUnreachable("Could not connect")
        answer = await real_call(device_id, action, body, **kwargs)
        if answer_kind == "fast_report":
            early_reports.append(await report_from_target(a, body["worker_session_id"], text="Finished before receipt."))
        elif answer_kind == "invalid":
            return {}
        elif answer_kind == "timeout":
            raise control.RunnerUnreachable(remote_task.MAY_HAVE_APPLIED)
        return answer

    monkeypatch.setattr(control, "call", call)

    async def scenario():
        with b.active():
            coordinator, key, published = await start_swarm(monkeypatch)
            if answer_kind in {"refused", "unreachable"}:
                await asyncio.wait_for(coordinator.jobs[key], 5)
                snapshot = coordinator.snapshot(key)
                assert snapshot["status"] == "failed"
                assert [t["status"] for t in snapshot["tasks"]] == ["failed", "blocked"]
                assert snapshot["tasks"][0]["worker_session_id"] is None
                assert not swarm.workers
                assert remote_tasks.get_dispatch(starts[0]["worker_session_id"]) is None
            else:
                if answer_kind != "fast_report":
                    snapshot = await admitted(published, 0)
                    assert snapshot["tasks"][0]["status"] == "running"
                    assert snapshot["tasks"][1]["status"] == "queued"
                    child = snapshot["tasks"][0]["worker_session_id"]
                    assert swarm.peer_rows("caller")[0]["worker"] == "Unraid"
                    assert task_registry.get_task("caller", child)["status"] == "running"
                    early_reports.append(await report_from_target(a, child, text="Report after uncertain start."))
                snapshot = await admitted(published, 1)
                assert snapshot["tasks"][0]["result"] == early_reports[0]["report"]
                assert wakes() == []
                child = snapshot["tasks"][1]["worker_session_id"]
                with a.active():
                    [turn] = [t for t in all_turns(child) if t["source"] == "provider_agent"]
                    assert early_reports[0]["report"] in turn["prompt"]
                await report_from_target(a, child, text="Successor finished.")
                await asyncio.wait_for(coordinator.jobs[key], 5)
                assert coordinator.snapshot(key)["status"] == "completed"
                assert (await remote_task.handle_task_report(early_reports[0], a.device_id))["duplicate"]
            assert len(wakes()) == 1
            await task_persistence.flush()

    asyncio.run(scenario())


@pytest.mark.parametrize("answer_kind", ["invalid", "timeout"])
@pytest.mark.parametrize("ending", ["expires", "late_report"])
@pytest.mark.parametrize("status_supported", [True, False], ids=["status-supported", "older-target"])
def test_uncertain_swarm_start_unknown_grace_is_bounded(
    workers, monkeypatch, kicks, answer_kind, ending, status_supported,
):
    a, b, router = swarm_network(workers, monkeypatch)
    real_call = control.call
    starts = []
    monkeypatch.setattr(remote_task, "_tracking_started", None)

    async def call(device_id, action, body, **kwargs):
        if action != remote_task.TASK_START_ACTION or starts:
            return await real_call(device_id, action, body, **kwargs)
        starts.append(body)
        # The request may have been sent, but no child exists on the target.
        if answer_kind == "invalid":
            return {}
        raise control.RunnerUnreachable(remote_task.MAY_HAVE_APPLIED)

    monkeypatch.setattr(control, "call", call)
    if not status_supported:
        # The signed endpoint of an older reachable peer refuses task-status.
        monkeypatch.setattr(control, "known_action", lambda action: action != remote_task.TASK_STATUS_ACTION)
        monkeypatch.delitem(control._LAZY_HANDLERS, remote_task.TASK_STATUS_ACTION)

    async def scenario():
        with b.active():
            independent = [{"id": "independent", "agent": "helper", "worker": "Unraid",
                            "prompt": "Unrelated work."}]
            coordinator, key, published = await start_swarm(monkeypatch, extra_tasks=independent)
            snapshot = await admitted(published, 0)
            child = snapshot["tasks"][0]["worker_session_id"]
            dispatch = remote_tasks.get_dispatch(child)
            started = datetime.fromisoformat(dispatch["unconfirmed_start_at"])
            deadline = started + timedelta(seconds=remote_task.OFFLINE_NOTICE_SECONDS)
            with a.active():
                assert remote_tasks.get_child(child) is None
            # Real signed task-status returns CHILD_UNKNOWN while reachable.
            assert await remote_task.check_targets(now=started) == 0
            assert await remote_task.check_targets(now=deadline - timedelta(seconds=1)) == 0
            dispatch = remote_tasks.get_dispatch(child)
            assert dispatch["last_contact_at"] and dispatch["unreachable_since"] is None
            assert dispatch["unconfirmed_start_at"] == started.isoformat()
            assert dispatch["offline_notice_at"] is None
            assert task_registry.get_task("caller", child)["status"] == "running"
            assert coordinator.snapshot(key)["tasks"][1]["status"] == "queued"
            assert wakes() == []
            assert ("POST", "/api/v1/network/peer/control/task-status") in router.requests(UNRAID_URL)

            if ending == "late_report":
                assert dispatch["target_state"] == remote_task.CHILD_UNKNOWN
                # A delayed start/report within grace still completes normally.
                assert (await real_call(a.device_id, remote_task.TASK_START_ACTION, starts[0]))["worker_session_id"] == child
                assert await remote_task.check_targets(now=deadline - timedelta(seconds=1)) == 0
                if status_supported:
                    assert remote_tasks.get_dispatch(child)["unconfirmed_start_at"] is None
                    # A stale unknown-status pass cannot fail a now-confirmed child.
                    assert not await remote_task.notice_offline(dispatch, started.isoformat(), unknown_start=True)
                else:
                    assert remote_tasks.get_dispatch(child)["unconfirmed_start_at"] == started.isoformat()
                body = await report_from_target(a, child, text="Delayed disk report within grace.")
                assert body["report"] == "Delayed disk report within grace."
                snapshot = await admitted(published, 1)
                assert snapshot["tasks"][0]["result"] == body["report"]
                second_child = snapshot["tasks"][1]["worker_session_id"]
                with a.active():
                    [turn] = [t for t in all_turns(second_child) if t["source"] == "provider_agent"]
                    assert body["report"] in turn["prompt"]
                await report_from_target(a, second_child, text="Dependent report.")
                expected_status = "completed"
            else:
                # Repeated unknown answers after grace end the item once.
                assert await remote_task.check_targets(now=deadline + timedelta(seconds=1)) == 0
                assert coordinator.snapshot(key)["tasks"][0]["status"] == "failed"
                assert remote_tasks.get_dispatch(child)["target_state"] == remote_task.CHILD_UNKNOWN
                snapshot = await admitted(published, 2)
                assert snapshot["tasks"][0]["status"] == "failed"
                assert "reachable but still knows no subagent" in snapshot["tasks"][0]["error"]
                assert snapshot["tasks"][1]["status"] == "blocked"
                assert snapshot["tasks"][1]["worker_session_id"] is None
                assert task_registry.get_task("caller", child)["status"] == "error"
                assert task_registry.get_task("caller", child)["reason"] == remote_task.UNCONFIRMED_START_REASON
                assert remote_tasks.get_dispatch(child)["offline_notice_at"]
                assert provider_agent_supervisor.get_worker("caller", child) is None
                assert await remote_task.check_targets(now=deadline + timedelta(seconds=60)) == 0
                expected_status = "failed"

            # In both cases the held max_parallel=1 slot is freed for unrelated work.
            if ending == "late_report":
                snapshot = await admitted(published, 2)
            assert snapshot["tasks"][2]["status"] == "running"
            assert wakes() == []
            await report_from_target(a, snapshot["tasks"][2]["worker_session_id"], text="Independent report.")
            await asyncio.wait_for(coordinator.jobs[key], 5)
            assert coordinator.snapshot(key)["status"] == expected_status
            [wake] = wakes()
            assert key in wake["payload"]["task_ids"]
            if ending == "expires":
                assert "reachable but still knows no subagent" in wake["prompt"]
            assert await remote_task.check_targets(now=deadline + timedelta(seconds=120)) == 0
            assert len(wakes()) == 1
            await task_persistence.flush()

    asyncio.run(scenario())



def test_existing_remote_task_table_adds_uncertain_start_marker(workers):
    worker = workers("schema-upgrade")
    with worker.active():
        from app.db.base import column_exists

        old_schema = remote_tasks._PARENT_DDL.replace(",\n    unconfirmed_start_at TEXT", "")
        assert old_schema != remote_tasks._PARENT_DDL
        with get_db() as conn:
            conn.cursor().execute(old_schema)
            assert not column_exists(conn.cursor(), "remote_tasks", "unconfirmed_start_at")
        remote_tasks.record_dispatch(worker_session_id="uncertain", target_device_id="remote",
            owner_session_id="caller", root_session_id="caller", tool_use_id=None,
            mode="background", label="Check disk", agent="helper")
        assert remote_tasks.get_dispatch("uncertain")["unconfirmed_start_at"] is None
        remote_tasks.mark_start_unconfirmed("uncertain")
        stamp = remote_tasks.get_dispatch("uncertain")["unconfirmed_start_at"]
        assert stamp
        remote_tasks.note_contact({"uncertain": remote_task.CHILD_UNKNOWN})
        assert remote_tasks.get_dispatch("uncertain")["unconfirmed_start_at"] == stamp
        remote_tasks.note_contact({"uncertain": None})
        assert remote_tasks.get_dispatch("uncertain")["unconfirmed_start_at"] == stamp
        remote_tasks.note_contact({"uncertain": remote_tasks.RUNNING})
        assert remote_tasks.get_dispatch("uncertain")["unconfirmed_start_at"] is None
