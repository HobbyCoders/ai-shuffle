"""No parent waits forever on a worker that went away (S12).

Two in-process workers over the real signed control endpoint: ServerWorker runs the
child, Windows runs the parent chat. ServerWorker going away is the test router
refusing its address; time is passed to ``check_targets``, never slept.
"""
import asyncio
from datetime import datetime, timedelta

import pytest

from app.core import provider_agent, provider_agent_supervisor, remote_task, swarm, turn_scheduler
from app.core import remote_task_approvals as relay
from app.core.provider_agent_feed import reset_feed_groups
from app.core.query_session_lifecycle import stop_all_background_tasks
from app.core.task_registry import task_registry
from app.db import database, interactions, remote_tasks, session_events, turns
from app.db.base import get_db
from app.network import control
from tests.core.network.test_remote_task_approvals import pending_proxy_row, request_body
from tests.core.network.test_remote_task_control import (
    UNRAID_URL, all_turns, finish_child, network, started_child, wakes,
)
from tests.core.network.test_remote_task_messaging import (
    deliver, finish_turn, finish_wake, message_turns, send,
)

NOTICE = "has not answered for 30 minutes"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(turn_scheduler, "kick", lambda sid: None)
    monkeypatch.setattr(turn_scheduler, "_kick_after_batch_window", lambda sid: None)
    # Each test is a fresh process for the "silence this process saw" floor.
    monkeypatch.setattr(remote_task, "_tracking_started", None)
    relay.reset()
    yield
    relay.reset()
    provider_agent_supervisor.reset()
    reset_feed_groups()
    swarm.coordinators.clear()
    swarm.workers.clear()
    swarm.worker_jobs.clear()


@pytest.fixture
def proxies(monkeypatch):
    collected = []
    monkeypatch.setattr(relay, "_spawn", lambda coro, name: collected.append(coro))
    yield collected
    for coro in collected:
        coro.close()


def ran(wake):
    """The caller ran this wake (on the current worker), so the next one is new."""
    assert turns.start_turn(wake["id"])
    assert turns.settle_turn(wake["id"], "completed")


def after(since, minutes):
    return datetime.fromisoformat(since) + timedelta(minutes=minutes)


def go_quiet(b, router, child):
    """ServerWorker stops answering; the first check records since when. Returns that time."""
    router.down.add(UNRAID_URL)
    with b.active():
        assert asyncio.run(remote_task.check_targets()) == 1
        return remote_tasks.get_dispatch(child)["unreachable_since"]


def notice(b, since, minutes=31):
    with b.active():
        return asyncio.run(remote_task.check_targets(now=after(since, minutes)))


# --- the notice ------------------------------------------------------------------

def test_a_quiet_worker_ends_the_wait_with_one_notice_after_30_minutes(workers, monkeypatch):
    a, b, router = network(workers, monkeypatch)
    child = started_child(a, b)
    since = go_quiet(b, router, child)
    notice(b, since, minutes=29)
    with b.active():
        assert wakes() == []
        assert task_registry.get_task("caller", child)["status"] == "running"
    notice(b, since)
    with b.active():
        [wake] = wakes()
        prompt = wake["prompt"]
        assert f"<task-id>{child}</task-id>" in prompt and "<status>error</status>" in prompt
        assert f"ServerWorker {NOTICE}" in prompt and "may still be running there" in prompt
        assert "its report still arrives here" in prompt
        # The wait is over: the record settled, nothing parks on it, no card spins.
        task = task_registry.get_task("caller", child)
        assert task["status"] == "error" and task["reason"] == remote_task.OFFLINE_REASON
        assert task_registry.get_active_background_work("caller") == []
        assert not task_registry.has_unresolved_background_launches("caller")
        assert provider_agent_supervisor.get_worker("caller", child) is None
        assert len([e for e in session_events.get_events("caller", 0, 1000)
                    if e["event_type"] == "subagent.completed"]) == 1
        dispatch = remote_tasks.get_dispatch(child)
        assert dispatch["offline_notice_at"] and dispatch["completion_id"] is None
        # Once per run, however long it stays away.
        notice(b, since, minutes=90)
        assert len(wakes()) == 1
    # Nothing was sent to ServerWorker for it: the child may still be running there.
    assert not [r for r in router.requests(UNRAID_URL) if r[1].endswith("/task-stop")]


def test_this_workers_own_downtime_does_not_count(workers, monkeypatch):
    """After a restart here, an old ``unreachable_since`` does not notice at once."""
    a, b, router = network(workers, monkeypatch)
    child = started_child(a, b)
    router.down.add(UNRAID_URL)
    with b.active():
        with get_db() as conn:
            conn.cursor().execute("UPDATE remote_tasks SET unreachable_since=%s",
                                  ("2000-01-01T00:00:00+00:00",))
        asyncio.run(remote_task.check_targets())
        assert wakes() == []
        assert remote_tasks.get_dispatch(child)["unreachable_since"].startswith("2000-01-01")
        first_check = remote_task._tracking_started
        asyncio.run(remote_task.check_targets(now=first_check + timedelta(minutes=29)))
        assert wakes() == []
        asyncio.run(remote_task.check_targets(now=first_check + timedelta(minutes=31)))
        assert len(wakes()) == 1


def test_a_worker_that_answers_in_time_gets_no_notice(workers, monkeypatch):
    a, b, router = network(workers, monkeypatch)
    child = started_child(a, b)
    since = go_quiet(b, router, child)
    router.down.clear()
    notice(b, since)  # it answers: contact clears the silence
    with b.active():
        assert wakes() == []
        assert remote_tasks.get_dispatch(child)["unreachable_since"] is None
        assert task_registry.get_task("caller", child)["status"] == "running"


# --- a report after the notice -------------------------------------------------------

def test_a_late_report_is_accepted_and_wakes_the_caller_once_more(workers, monkeypatch):
    a, b, router = network(workers, monkeypatch)
    child = started_child(a, b)
    notice(b, go_quiet(b, router, child))
    with b.active():
        [first] = wakes()
    finish_wake(a, b, first)
    router.down.clear()
    deliver(a, child, text="Disk is 40% full.")
    with b.active():
        assert len(wakes()) == 2
        prompt = wakes()[-1]["prompt"]
        assert "<status>completed</status>" in prompt and "Disk is 40% full." in prompt
        assert "ServerWorker answered again after the notice" in prompt
        task = task_registry.get_task("caller", child)
        assert task["status"] == "completed" and task["reportPath"]
        assert remote_tasks.get_dispatch(child)["completion_id"]
        # A retry of the same report is a duplicate and wakes nobody.
        with a.active():
            report = remote_tasks.get_child(child)["report"]
        assert asyncio.run(control.handle(remote_task.TASK_REPORT_ACTION, report,
                                          from_device=a.device_id)) == {"delivered": True,
                                                                        "duplicate": True}
        assert len(wakes()) == 2
        # Done: nothing is tracked any more, so no further notice or wake.
        assert remote_tasks.tracked_dispatches() == []
        assert len(wakes()) == 2


def test_a_report_while_the_notice_still_waits_joins_that_wake(workers, monkeypatch):
    """Not run yet: the report folds into the same wake, so the caller runs once."""
    a, b, router = network(workers, monkeypatch)
    child = started_child(a, b)
    notice(b, go_quiet(b, router, child))
    router.down.clear()
    deliver(a, child, text="Disk is 40% full.")
    with b.active():
        [wake] = wakes()
        assert NOTICE in wake["prompt"] and "Disk is 40% full." in wake["prompt"]
        assert task_registry.get_task("caller", child)["status"] == "completed"


def test_a_child_stopped_after_the_notice_stays_stopped_and_its_report_wakes_nobody(
        workers, monkeypatch):
    a, b, router = network(workers, monkeypatch)
    child = started_child(a, b)
    notice(b, go_quiet(b, router, child))
    with b.active():
        [first] = wakes()
        # Swarm cancel or the assistant's limits stop it through the S8 path.
        result = asyncio.run(remote_task.stop_remote_child(child, a.device_id, reason="swarm_cancelled"))
        assert result["outcome"] == "remote_stop_pending"
    finish_wake(a, b, first)
    router.down.clear()
    with b.active():
        assert asyncio.run(remote_task.deliver_stops()) == 1
    with a.active():
        assert asyncio.run(remote_task.sweep()) == 1
        assert remote_tasks.get_child(child)["report"]["status"] == "stopped"
    with b.active():
        assert len(wakes()) == 1
        assert remote_tasks.get_dispatch(child)["completion_id"]


def press_stop(b):
    """The owner presses Stop on the parent chat; stops being sent finish first."""
    async def stop():
        results = await stop_all_background_tasks("caller", user_stop=True)
        await remote_task.drain_stops()
        return results
    with b.active():
        return asyncio.run(stop())


def stop_requests(router):
    return [r for r in router.requests(UNRAID_URL) if r[1].endswith("/" + remote_task.TASK_STOP_ACTION)]


def test_stop_on_the_parent_after_the_notice_reaches_the_child_when_its_worker_returns(
        workers, monkeypatch):
    """#1780: notice, Stop on the parent, worker back, child stopped, report wakes nobody."""
    a, b, router = network(workers, monkeypatch)
    child = started_child(a, b)
    notice(b, go_quiet(b, router, child))
    with b.active():
        [first] = wakes()
    finish_wake(a, b, first)

    [result] = press_stop(b)
    assert result["task_id"] == child and result["ok"] is True
    assert "sent again when it answers" in result["detail"]
    with b.active():
        dispatch = remote_tasks.get_dispatch(child)
        assert dispatch["stop_requested_at"] and dispatch["stop_reason"] == "user_stopped"
        assert dispatch["stop_state"] == remote_tasks.STOP_PENDING
    with a.active():
        assert not remote_tasks.get_child(child)["stop_requested_at"]  # ServerWorker is still away
    # Stop again: idempotent, nothing new is asked for.
    assert press_stop(b) == []

    router.down.clear()
    with b.active():
        assert asyncio.run(remote_task.deliver_stops()) == 1
        assert remote_tasks.get_dispatch(child)["stop_state"] == remote_tasks.STOP_SENT
    with a.active():
        assert remote_tasks.get_child(child)["stop_requested_at"]
        assert task_registry.is_user_stopped(child)
        [turn] = [t for t in all_turns(child) if t["source"] == "provider_agent"]
        assert turn["state"] == "aborted"
        assert asyncio.run(remote_task.sweep()) == 1
        assert remote_tasks.get_child(child)["report"]["status"] == "stopped"
    with b.active():
        assert len(wakes()) == 1
        dispatch = remote_tasks.get_dispatch(child)
        assert dispatch["completion_id"] and dispatch["status"] == "stopped"
        assert asyncio.run(remote_task.deliver_stops()) == 0


def test_a_report_that_beats_the_stop_after_the_notice_wakes_nobody(workers, monkeypatch):
    """The child finished while its worker was away; Stop pressed after the notice still holds.

    The owner wrote to the chat since, so the session's in-memory Stop latch is
    gone: only the durable stop keeps the report from waking it.
    """
    a, b, router = network(workers, monkeypatch)
    child = started_child(a, b)
    notice(b, go_quiet(b, router, child))
    with b.active():
        [first] = wakes()
    finish_wake(a, b, first)
    press_stop(b)
    with b.active():
        assert task_registry.clear_user_stop("caller")
    router.down.clear()
    deliver(a, child, text="Disk is 40% full.")
    with b.active():
        assert len(wakes()) == 1
        assert remote_tasks.get_dispatch(child)["completion_id"]
        assert asyncio.run(remote_task.deliver_stops()) == 0  # at rest there: not owed
    assert stop_requests(router) == []


def test_stop_leaves_a_reachable_or_finished_remote_child_to_the_usual_path(workers, monkeypatch):
    """Only children past the notice are picked up here; others are not asked twice."""
    a, b, router = network(workers, monkeypatch)
    child = started_child(a, b)
    deliver(a, child, text="Disk is 40% full.")
    with b.active():
        assert remote_tasks.noticed_unstopped("caller") == []
    assert press_stop(b) == []
    assert stop_requests(router) == []


def test_a_resumed_run_that_goes_quiet_gets_its_own_notice(workers, monkeypatch):
    a, b, router = network(workers, monkeypatch)
    child = started_child(a, b)
    deliver(a, child, text="Disk is 40% full.")
    with b.active():
        [first] = wakes()
    finish_wake(a, b, first)
    outcome = send(b, child, "Now clean the cache.")
    assert not outcome.is_error, outcome.text
    with b.active():
        assert remote_tasks.get_dispatch(child)["offline_notice_at"] is None
    notice(b, go_quiet(b, router, child))
    with b.active():
        assert len(wakes()) == 2 and NOTICE in wakes()[-1]["prompt"]
        ran(wakes()[-1])
    router.down.clear()
    with a.active():
        [resumed] = message_turns(child)
    finish_turn(a, child, resumed["id"], "Cache cleaned.")
    with b.active():
        assert len(wakes()) == 3 and "Cache cleaned." in wakes()[-1]["prompt"]


# --- approvals, messages and spend around the notice ---------------------------------------

def test_the_notice_withdraws_open_prompts_and_a_returned_child_asks_again(workers, monkeypatch, proxies):
    a, b, router = network(workers, monkeypatch)
    child = started_child(a, b)

    async def scenario():
        with b.active():
            await relay.handle_task_approval(request_body(child), a.device_id)
            proxy = asyncio.create_task(proxies.pop(0))
            row = await pending_proxy_row()
            router.down.add(UNRAID_URL)
            await remote_task.check_targets()
            since = remote_tasks.get_dispatch(child)["unreachable_since"]
            await remote_task.check_targets(now=after(since, 31))
            assert interactions.get_interaction(row["id"])["state"] == "cancelled"
            # The withdrawn prompt resolves as a deny it cannot deliver (ServerWorker is away).
            await asyncio.wait_for(proxy, 10)
            assert interactions.get_pending_interactions("caller") == []
            router.down.clear()
            # ServerWorker is back and the child still runs: its next prompt is shown.
            answer = await relay.handle_task_approval(request_body(child, approval_id="c" * 32),
                                                      a.device_id)
            assert answer["accepted"] is True and "duplicate" not in answer
    asyncio.run(scenario())
    assert len(proxies) == 1


def test_a_message_to_a_child_declared_offline_says_so(workers, monkeypatch):
    a, b, router = network(workers, monkeypatch)
    child = started_child(a, b)
    notice(b, go_quiet(b, router, child))
    outcome = send(b, child, "Also check the logs.")
    assert outcome.is_error
    assert outcome.text.startswith("Could not deliver to Default on ServerWorker: ServerWorker has not answered since ")
    assert "stopped waiting for it" in outcome.text and "may still be running there" in outcome.text
    assert "Send the message again when ServerWorker is back" in outcome.text
    router.down.clear()
    # Back, and the child still runs there: the message is delivered as usual.
    outcome = send(b, child, "Also check the logs.")
    assert not outcome.is_error, outcome.text
    with a.active():
        assert [t["prompt"] for t in message_turns(child)] and "Also check the logs." in \
            message_turns(child)[0]["prompt"]


def test_spend_after_the_notice_is_still_counted_when_the_worker_returns(workers, monkeypatch):
    from app.core import assistant_limits as limits, assistant_service as service
    from app.db import assistant as store
    from tests.core.network.test_remote_task_limits import book, make_home, refresh, spent

    a, b, router = network(workers, monkeypatch)
    make_home(b, "caller")
    child = started_child(a, b)
    with a.active():
        book(child, 2.0)
    with b.active():
        refresh()
        assert spent() == pytest.approx(2.0)
    notice(b, go_quiet(b, router, child))
    with a.active():
        book(child, 1.5)  # it kept working while ServerWorker could not be reached
    with b.active():
        assert len(wakes()) == 1
        assert refresh() == [] and spent() == pytest.approx(2.0)
        # The notice does not age the job out of the spend checks.
        assert [j["session_id"] for j in store.remote_jobs_to_check(
            service.assistant_id(), limits.REMOTE_IDLE_FOR)] == [child]
    router.down.clear()
    with b.active():
        refresh()
        assert spent() == pytest.approx(3.5)


# --- Swarm --------------------------------------------------------------------------------

def run_swarm(a, b, router, finish):
    """One remote Swarm item; ``finish(child)`` ends it. Returns (child, snapshot)."""
    from fluid.swarm import SwarmCoordinator
    from fluid.tools.registry import BackgroundTaskReceipt, ToolCtx, ToolOutcome

    async def run():
        async def dispatch(data, ctx):
            result = await remote_task.dispatch_remote(
                worker={"device_id": a.device_id, "device_name": "ServerWorker"},
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
        child = coordinator.swarms[key]["tasks"][0]["task_id"]
        assert child
        await finish(child)
        await asyncio.wait_for(coordinator.jobs[key], 10)
        return child, coordinator.snapshot(key)

    with b.active():
        return asyncio.run(run())


def test_a_remote_swarm_item_ends_with_its_report(workers, monkeypatch):
    a, b, router = network(workers, monkeypatch)

    async def report(child):
        with a.active():  # no awaits inside: B's code never runs on A's data
            finish_child(child, text="Disk is 40% full.")
            body = remote_task.build_child_report(child)
        assert await control.handle(remote_task.TASK_REPORT_ACTION, body, from_device=a.device_id) == {
            "delivered": True, "duplicate": False}
    child, snapshot = run_swarm(a, b, router, report)
    assert snapshot["status"] == "completed"
    assert snapshot["tasks"][0]["status"] == "completed"
    assert snapshot["tasks"][0]["result"] == "Disk is 40% full."


def test_a_quiet_worker_ends_its_swarm_item(workers, monkeypatch):
    a, b, router = network(workers, monkeypatch)

    async def go_away(child):
        router.down.add(UNRAID_URL)
        await remote_task.check_targets()
        since = remote_tasks.get_dispatch(child)["unreachable_since"]
        await remote_task.check_targets(now=after(since, 31))
    child, snapshot = run_swarm(a, b, router, go_away)
    assert snapshot["status"] == "failed"
    item = snapshot["tasks"][0]
    assert item["status"] == "failed" and NOTICE in item["error"]
    with b.active():
        assert task_registry.get_task("caller", child)["status"] == "error"


# --- the worker that runs the child restarts -----------------------------------------------

def test_a_target_restart_reports_the_child_failed_and_it_can_be_continued(workers, monkeypatch):
    a, b, router = network(workers, monkeypatch)
    child = started_child(a, b)
    with a.active():
        [turn] = [t for t in all_turns(child) if t["source"] == "provider_agent"]
        assert turns.start_turn(turn["id"])
        session_events.append_events(child, [{"type": "assistant.text_delta", "turn_id": turn["id"],
                                              "payload": {"text": "Half way through the disk."}}])
        # The process dies: nothing in memory, and the boot reap ends the dead run.
        provider_agent.reset_worker_sessions()
        reaped = turns.reap_expired()
        assert [r["id"] for r in reaped] == [turn["id"]]
        assert turns.get_turn(turn["id"])["stop_reason"] == remote_task.LEASE_EXPIRED
        assert asyncio.run(remote_task.sweep()) == 1
    with b.active():
        [wake] = wakes()
        prompt = wake["prompt"]
        assert "<status>error</status>" in prompt
        assert "restarted (or lost its run) before the subagent finished" in prompt
        assert "Half way through the disk." in prompt and "(unfinished)" in prompt
        assert task_registry.get_task("caller", child)["status"] == "error"
        assert remote_tasks.get_dispatch(child)["status"] == "failed"
    finish_wake(a, b, wake)
    outcome = send(b, child, "Carry on from where you were.")
    assert not outcome.is_error, outcome.text
    assert outcome.text.startswith("Resume queued for Default on ServerWorker")


def test_a_child_whose_session_is_gone_after_a_restart_reports_failed(workers, monkeypatch):
    a, b, router = network(workers, monkeypatch)
    child = started_child(a, b)
    with a.active():
        with get_db() as conn:
            conn.cursor().execute("DELETE FROM turns WHERE session_id=%s", (child,))
            conn.cursor().execute("DELETE FROM sessions WHERE id=%s", (child,))
        assert asyncio.run(remote_task.sweep()) == 1
    with b.active():
        [wake] = wakes()
        assert "<status>error</status>" in wake["prompt"]
        assert "no longer exists on the worker that ran it" in wake["prompt"]
