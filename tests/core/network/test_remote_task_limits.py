"""The assistant's limits count and stop its jobs on another worker (S11).

Two in-process workers over the real signed control endpoint: ServerWorker runs the
jobs, Windows runs the assistant's home ("caller"). No provider.
"""
import asyncio
import json
from datetime import datetime, timezone

import pytest

from app.core import assistant_limits as limits, assistant_service as service
from app.core import provider_agent_supervisor, remote_task, session_orchestration as orch
from app.core import turn_manager
from app.core.provider_agent_feed import reset_feed_groups
from app.core.task_registry import task_registry
from app.db import assistant as store, assistant_limits as limits_db, database, remote_tasks, turns
from app.db.base import get_db
from app.network import control
from tests.core.network.test_remote_task_control import (
    UNRAID_URL, all_turns, dispatch, finish_child, kicks, network, started_child,  # noqa: F401 - kicks is a fixture
)


@pytest.fixture(autouse=True)
def _clean_tracking():
    yield
    provider_agent_supervisor.reset()
    reset_feed_groups()


@pytest.fixture
def interrupts(monkeypatch):
    """Stop requests as the worker that runs the job receives them."""
    seen = []

    async def interrupt(session_id, mode, *, stop_background=False, turn_id=None, source=None):
        seen.append(dict(session_id=session_id, mode=mode, stop_background=stop_background, turn_id=turn_id))
        return {"id": turn_id or "live", "state": "interrupting"}

    async def no_background(*args, **kwargs):
        return None

    from app.core import query_session_lifecycle
    monkeypatch.setattr(turn_manager, "interrupt_async", interrupt)
    monkeypatch.setattr(query_session_lifecycle, "stop_all_background_tasks", no_background)
    return seen


@pytest.fixture
def notices(monkeypatch):
    posted = []

    async def record(session_id, text, extra_payload=None):
        posted.append((session_id, text))
        return "brief-" + str(len(posted))

    from app.core import turn_recorder
    monkeypatch.setattr(turn_recorder, "record_brief", record)
    return posted


def make_home(worker, home):
    """``home`` is this worker's assistant home (both workers share one id in the harness)."""
    with worker.active():
        aid = service.assistant_id()
        with get_db() as conn:
            conn.cursor().execute("INSERT INTO assistant_state (id, runner_id, home_session_id) VALUES (%s,%s,%s)",
                                  (aid, aid, home))


def assistant_network(workers, monkeypatch):
    a, b, router = network(workers, monkeypatch)
    make_home(b, "caller")
    return a, b, router


def book(session_id, cost):
    with get_db() as conn:
        conn.cursor().execute(
            "INSERT INTO usage_log (session_id, model, tokens_in, tokens_out, cost_usd, duration_ms, created_at) "
            "VALUES (%s,'m',1,1,%s,1,%s)", (session_id, cost, datetime.now(timezone.utc)))


def grandchild(child, session_id="grandchild"):
    """A Task worker the remote child started on its own worker."""
    database.create_session(session_id=session_id, profile_id="default", title="Nested")
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET is_worker=TRUE, parent_session_id=%s WHERE id=%s",
                              (child, session_id))
    return session_id


def start_child_turn(child):
    [turn] = [t for t in all_turns(child) if t["source"] == "provider_agent"]
    assert turns.start_turn(turn["id"])
    return turn


def refresh():
    return asyncio.run(limits.refresh_remote())


def spent():
    return limits.status()["spent_today_usd"]


def set_limits(**changes):
    return limits_db.update(service.assistant_id(), changes)


def requests_for(router, action):
    return [r for r in router.requests(UNRAID_URL) if r[1].endswith("/" + action)]


def send(b, child, text):
    from app.core import agent_messaging
    with b.active():
        message = agent_messaging.make_message(lambda: "caller")
        return asyncio.run(message({"to": child, "message": text, "urgent": False}, None))


def deliver(a, child):
    with a.active():
        finish_child(child)
        asyncio.run(remote_task.on_child_settled(child))
        asyncio.run(remote_task.deliver_due())


# --- counting ---------------------------------------------------------------------

def test_a_remote_childs_spend_counts_once_against_the_assistants_limit(workers, monkeypatch, kicks):
    a, b, _ = assistant_network(workers, monkeypatch)
    child = started_child(a, b)
    with b.active():
        job = store.get_remote_job(service.assistant_id(), child)
        assert job["device_id"] == a.device_id and job["kind"] == "task" and job["label"] == "Task · Default"
        assert spent() == 0
    with a.active():
        book(child, 2.0)
        book(grandchild(child), 0.5)
        book("unrelated-chat", 40.0)
    with b.active():
        [seen] = refresh()
        assert seen["session_id"] == child and seen["cost_usd"] == pytest.approx(2.5)
        assert spent() == pytest.approx(2.5)
        refresh()  # The same total again books nothing.
        assert spent() == pytest.approx(2.5)
    with a.active():
        book(child, 1.0)
    with b.active():
        refresh()
        assert spent() == pytest.approx(3.5)
        assert "another one" in limits.session_usage("caller")["note"]


def test_running_turns_count_before_they_are_booked_and_settling_lower_books_nothing(workers, monkeypatch, kicks):
    from app.db.session_events import append_events
    a, b, _ = assistant_network(workers, monkeypatch)
    child = started_child(a, b)
    with a.active():
        turn = start_child_turn(child)
        append_events(child, [{"type": "turn.usage", "turn_id": turn["id"],
                               "payload": {"usage": {"model_call_index": 0, "cost_usd": 1.25}}}])
    with b.active():
        refresh()
        assert spent() == pytest.approx(1.25)
    with a.active():
        turns.settle_turn(turn["id"], "completed")
        book(child, 1.0)  # Its settled price is lower than the in-flight estimate.
    with b.active():
        refresh()
        assert spent() == pytest.approx(1.25)


def test_an_unreachable_worker_keeps_its_cost_and_is_collected_when_it_is_back(workers, monkeypatch, kicks):
    a, b, router = assistant_network(workers, monkeypatch)
    child = started_child(a, b)
    with a.active():
        book(child, 2.0)
    with b.active():
        refresh()
    router.down.add(UNRAID_URL)
    with a.active():
        book(child, 1.0)
    with b.active():
        assert refresh() == []
        assert spent() == pytest.approx(2.0)
        assert [j["session_id"] for j in store.remote_jobs_to_check(
            service.assistant_id(), limits.REMOTE_IDLE_FOR)] == [child]
    router.down.clear()
    with b.active():
        refresh()
        assert spent() == pytest.approx(3.0)


def test_an_older_worker_is_read_from_the_mirrored_session_cost(workers, monkeypatch, kicks):
    a, b, _ = assistant_network(workers, monkeypatch)
    child = started_child(a, b)
    original = control._LAZY_HANDLERS.pop(limits.REMOTE_USAGE_ACTION)
    monkeypatch.setattr(control, "known_action", lambda action: action != limits.REMOTE_USAGE_ACTION)
    try:
        with b.active():
            database.create_session(session_id=child, profile_id="default", title="Mirror")
            with get_db() as conn:
                conn.cursor().execute("UPDATE sessions SET total_cost_usd=%s WHERE id=%s", (4.0, child))
            assert refresh() == []
            assert spent() == pytest.approx(4.0)
            refresh()
            assert spent() == pytest.approx(4.0)
    finally:
        control._LAZY_HANDLERS[limits.REMOTE_USAGE_ACTION] = original


def test_remote_usage_answers_only_for_jobs_the_asking_worker_started(workers, monkeypatch, kicks):
    a, b, _ = assistant_network(workers, monkeypatch)
    child = started_child(a, b)
    with a.active():
        database.create_session(session_id="own-chat", profile_id="default", title="ServerWorker's own chat")
        turns.enqueue_turn("own-chat", "user", prompt="hi", profile_id="default")
        book("own-chat", 9.0)
        body = {"version": limits.REMOTE_WIRE_VERSION, "session_ids": [child, "own-chat", "missing"]}
        answer = asyncio.run(control.handle(limits.REMOTE_USAGE_ACTION, body, from_device=b.device_id))
        jobs = answer["jobs"]
        assert jobs[child]["known"] is True and jobs[child]["cost_usd"] == 0
        assert jobs["own-chat"] == {"known": False} and jobs["missing"] == {"known": False}
        with pytest.raises(PermissionError):
            limits.answer_remote_usage(body, "not-a-member")
        with pytest.raises(ValueError):
            limits.answer_remote_usage({"version": limits.REMOTE_WIRE_VERSION, "session_ids": []}, b.device_id)


def test_the_target_never_links_a_child_another_worker_started_to_its_own_assistant(workers, monkeypatch, kicks):
    a, b, _ = assistant_network(workers, monkeypatch)
    child = started_child(a, b)
    with a.active():
        database.create_session(session_id="serverworker-home", profile_id="default", title="ServerWorker home")
    make_home(a, "serverworker-home")
    with a.active():
        assert service.link_job("serverworker-home", child) is None
        assert store.get_session_job(service.assistant_id(), child) is None


# --- linking ----------------------------------------------------------------------

def test_a_child_that_never_started_is_not_counted(workers, monkeypatch, kicks):
    a, b, _ = assistant_network(workers, monkeypatch)
    with b.active():
        result = dispatch(a, b, profile_id="no-such-agent")
        assert result["ok"] is False and remote_task.NOT_DISPATCHED in result["error"]
        assert store.remote_jobs_to_check(service.assistant_id(), limits.REMOTE_IDLE_FOR) == []


def test_a_task_from_a_chat_that_is_not_the_assistants_is_not_linked(workers, monkeypatch, kicks):
    a, b, _ = network(workers, monkeypatch)
    make_home(b, None)
    child = started_child(a, b)
    with b.active():
        assert store.get_remote_job(service.assistant_id(), child) is None


def test_the_spend_limit_refuses_a_remote_task_before_anything_is_sent(workers, monkeypatch, kicks):
    a, b, router = assistant_network(workers, monkeypatch)
    with b.active():
        set_limits(daily_spend_limit_usd=1)
        book("caller", 2.0)
        before = list(router.requests(UNRAID_URL))
        result = dispatch(a, b)
        assert result["ok"] is False and "spending limit is reached" in result["error"]
        assert remote_task.NOT_DISPATCHED not in result["error"]
        assert router.requests(UNRAID_URL) == before
        assert remote_tasks.tracked_dispatches() == []


async def test_a_remote_start_session_chat_is_linked_to_the_assistant(workers, monkeypatch):
    a, b, _ = assistant_network(workers, monkeypatch)

    async def call(device_id, action, body, timeout=None):
        assert action == orch.REMOTE_ACTION
        return {"session_id": body["session_id"], "turn_id": "t1", "created": True, "owner_device_id": device_id,
                "report_back": True}
    monkeypatch.setattr(control, "call", call)
    plan = {"remote": {"device_id": a.device_id, "device_name": "ServerWorker"}, "reserved": False, "receipt_id": None,
            "body": {"session_id": "remote-chat", "title": "Check the disks", "report_back": True}}
    with b.active():
        task_registry.reset_all()
        result = await orch._start_remote(plan, "caller", task_registry.stop_generation("caller"))
        assert result["session_id"] == "remote-chat"
        job = store.get_remote_job(service.assistant_id(), "remote-chat")
        assert job["kind"] == "chat" and job["device_id"] == a.device_id and job["label"] == "Check the disks"


# --- enforcement ------------------------------------------------------------------

def test_at_the_limit_the_remote_child_is_stopped_on_its_worker_once(workers, monkeypatch, kicks, interrupts,
                                                                     notices):
    a, b, router = assistant_network(workers, monkeypatch)
    child = started_child(a, b)
    with a.active():
        turn = start_child_turn(child)
        book(child, 1.5)
    with b.active():
        set_limits(daily_spend_limit_usd=1)
        asyncio.run(limits.enforce())
        asyncio.run(remote_task.drain_stops())
        assert interrupts == [dict(session_id=child, mode="stop", stop_background=True, turn_id=None)]
        # Through the one remote stop path (S8), never a parallel interrupt.
        assert len(requests_for(router, remote_task.TASK_STOP_ACTION)) == 1
        assert requests_for(router, "interrupt") == []
        record = remote_tasks.get_dispatch(child)
        assert record["stop_requested_at"] and record["stop_reason"] == "assistant_daily_limit"
        assert record["stop_state"] == remote_tasks.STOP_SENT
        # The parent's record settles like any stopped worker.
        assert task_registry.get_task("caller", child)["status"] == "killed"
        assert provider_agent_supervisor.get_worker("caller", child) is None
        assert limits_db.stopped(turn["id"])  # Keyed by the target's turn id.
        assert len(notices) == 1 and 'stopped 1 background job ("Task · Default")' in notices[0][1]
        asyncio.run(limits.enforce())  # Stopped and reported once.
        asyncio.run(remote_task.drain_stops())
        assert len(interrupts) == 1 and len(notices) == 1
        assert len(requests_for(router, remote_task.TASK_STOP_ACTION)) == 1
    with a.active():
        assert turns.get_turn(turn["id"])["state"] == "running"  # The fake stop left it running.
        assert remote_tasks.get_child(child)["stop_requested_at"]  # Latched stopped there.


def test_a_remote_chat_past_its_job_time_is_stopped_turn_by_turn_and_owner_turns_are_left(
        workers, monkeypatch, kicks, interrupts, notices):
    a, b, _ = assistant_network(workers, monkeypatch)
    with a.active():
        database.create_session(session_id="remote-chat", profile_id="default", title="Long chat")
        first = turns.enqueue_turn("remote-chat", "agent_message", prompt="go", profile_id="default",
                                   payload={"orchestration_remote_origin": b.device_id,
                                            "orchestration_report_device": b.device_id})
        assert turns.start_turn(first["id"])
        with get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET work_ms=%s WHERE id='remote-chat'", (31 * 60000,))
    with b.active():
        store.link_remote_job(service.assistant_id(), "remote-chat", a.device_id, "chat", "Long chat")
        set_limits(max_job_minutes=30)
        asyncio.run(limits.enforce())
        assert interrupts == [dict(session_id="remote-chat", mode="stop", stop_background=False,
                                   turn_id=first["id"])]
        assert notices[-1][1].startswith('The background job "Long chat" ran past its 30-minute limit')
    # The owner types into it there: their turn runs.
    with a.active():
        turns.settle_turn(first["id"], "aborted")
        mine = turns.enqueue_turn("remote-chat", "user", prompt="carry on", profile_id="default")
        assert turns.start_turn(mine["id"])
    with b.active():
        asyncio.run(limits.enforce())
        assert len(interrupts) == 1


def test_an_unreachable_worker_is_not_stopped_until_it_answers(workers, monkeypatch, kicks, interrupts, notices):
    a, b, router = assistant_network(workers, monkeypatch)
    child = started_child(a, b)
    with a.active():
        start_child_turn(child)
    with b.active():
        set_limits(daily_spend_limit_usd=1)
        book("caller", 2.0)
    router.down.add(UNRAID_URL)
    with b.active():
        asyncio.run(limits.enforce())
        assert interrupts == []
        assert task_registry.get_task("caller", child)["status"] == "running"
    router.down.clear()
    with b.active():
        asyncio.run(limits.enforce())
        asyncio.run(remote_task.drain_stops())
        assert [i["session_id"] for i in interrupts] == [child]


# --- with stop (S8) and messages (S9) ---------------------------------------------

def test_a_child_with_no_record_here_is_stopped_through_the_same_stop_path(workers, monkeypatch, kicks,
                                                                          interrupts, notices):
    a, b, router = assistant_network(workers, monkeypatch)
    child = started_child(a, b)
    with a.active():
        turn = start_child_turn(child)
    with b.active():
        provider_agent_supervisor.unregister_worker("caller", child)
        set_limits(daily_spend_limit_usd=1)
        book("caller", 2.0)
        asyncio.run(limits.enforce())
        asyncio.run(remote_task.drain_stops())
        assert [i["session_id"] for i in interrupts] == [child]
        assert len(requests_for(router, remote_task.TASK_STOP_ACTION)) == 1
        assert requests_for(router, "interrupt") == []
        assert remote_tasks.get_dispatch(child)["stop_reason"] == "assistant_daily_limit"
        assert limits_db.stopped(turn["id"])


def test_a_limit_stop_refuses_later_messages_on_both_workers(workers, monkeypatch, kicks, notices):
    a, b, router = assistant_network(workers, monkeypatch)
    child = started_child(a, b)
    with a.active():
        start_child_turn(child)
    with b.active():
        set_limits(daily_spend_limit_usd=1)
        book("caller", 2.0)
        asyncio.run(limits.enforce())
        asyncio.run(remote_task.drain_stops())
        set_limits(daily_spend_limit_usd=100)  # Raised again: the stop still holds.
    outcome = send(b, child, "Carry on.")
    assert outcome.is_error and "it was stopped" in outcome.text
    assert requests_for(router, remote_task.TASK_MESSAGE_ACTION) == []
    # The worker that runs it refuses too, even if the parent did not.
    with b.active():
        body = {"version": remote_task.WIRE_VERSION, "to": remote_task.TO_CHILD, "worker_session_id": child,
                "message_id": "m-1", "sender_session_id": "caller", "text": "Carry on.", "urgent": False,
                "known_completion_id": None}
        with pytest.raises(control.ControlRefused, match="stopped"):
            asyncio.run(control.call(a.device_id, remote_task.TASK_MESSAGE_ACTION, body, timeout=10))


def test_a_stop_an_older_worker_refused_is_neither_resent_nor_announced(workers, monkeypatch, kicks, notices):
    a, b, router = assistant_network(workers, monkeypatch)
    child = started_child(a, b)
    with a.active():
        turn = start_child_turn(child)
    monkeypatch.setattr(control, "known_action", lambda action: action != remote_task.TASK_STOP_ACTION)
    original = control._LAZY_HANDLERS.pop(remote_task.TASK_STOP_ACTION)
    try:
        with b.active():
            set_limits(daily_spend_limit_usd=1)
            book("caller", 2.0)
            asyncio.run(limits.enforce())
            asyncio.run(remote_task.drain_stops())
            asyncio.run(limits.enforce())
            asyncio.run(remote_task.drain_stops())
            assert len(requests_for(router, remote_task.TASK_STOP_ACTION)) == 1
            assert remote_tasks.get_dispatch(child)["stop_state"] == remote_tasks.STOP_REFUSED
            assert not limits_db.stopped(turn["id"])
            assert not any("background job" in text for _, text in notices)
    finally:
        control._LAZY_HANDLERS[remote_task.TASK_STOP_ACTION] = original


def test_a_message_to_a_remote_child_is_refused_at_the_limit(workers, monkeypatch, kicks):
    a, b, router = assistant_network(workers, monkeypatch)
    child = started_child(a, b)
    deliver(a, child)
    with b.active():
        set_limits(daily_spend_limit_usd=1)
        book("caller", 2.0)
    outcome = send(b, child, "Now clean the cache.")
    assert outcome.is_error and "spending limit is reached" in outcome.text
    assert requests_for(router, remote_task.TASK_MESSAGE_ACTION) == []
    with a.active():
        assert [t for t in all_turns(child) if t["source"] == "agent_message"] == []


def test_a_message_to_a_child_the_assistant_did_not_start_ignores_its_limit(workers, monkeypatch, kicks):
    a, b, router = network(workers, monkeypatch)
    make_home(b, None)
    child = started_child(a, b)
    deliver(a, child)
    with b.active():
        set_limits(daily_spend_limit_usd=1)
    outcome = send(b, child, "Now clean the cache.")
    assert not outcome.is_error, outcome.text


def test_a_resumed_child_is_asked_about_again_after_it_aged_out(workers, monkeypatch, kicks):
    from datetime import timedelta
    a, b, _ = assistant_network(workers, monkeypatch)
    child = started_child(a, b)
    deliver(a, child)
    with b.active():
        aid = service.assistant_id()
        old = datetime.now(timezone.utc) - limits.REMOTE_IDLE_FOR - timedelta(hours=1)
        with get_db() as conn:
            conn.cursor().execute("UPDATE assistant_remote_jobs SET idle_since=%s WHERE session_id=%s", (old, child))
        assert store.remote_jobs_to_check(aid, limits.REMOTE_IDLE_FOR) == []
    outcome = send(b, child, "Now clean the cache.")
    assert not outcome.is_error and outcome.text.startswith("Resume queued"), outcome.text
    with b.active():
        assert [j["session_id"] for j in store.remote_jobs_to_check(aid, limits.REMOTE_IDLE_FOR)] == [child]


def test_a_re_attached_child_keeps_its_provider_and_model_from_the_mirror(workers, monkeypatch, kicks):
    from app.core import task_persistence
    a, b, _ = assistant_network(workers, monkeypatch)
    child = started_child(a, b)
    with b.active():
        asyncio.run(task_persistence.flush())
        task_registry.reset_all()
        provider_agent_supervisor.reset()
        reset_feed_groups()
        task_persistence.reset()
        database.create_session(session_id=child, profile_id="default", title="Mirror", model="sonnet")
        assert asyncio.run(remote_task.reattach_all()) == 1
        assert provider_agent_supervisor.get_worker("caller", child).model_id == "sonnet"
