"""SendMessage to a Task child on another worker, and from it to `main` (S9).

Two in-process workers over the real signed control endpoint: Unraid runs the
child, Windows runs the parent chat. No provider, no second machine.
"""
import asyncio
import uuid

import pytest

from app.core import agent_messaging, provider_agent_supervisor, remote_task, turn_scheduler
from app.core import wakeup_service
from app.core.provider_agent_feed import reset_feed_groups
from app.core.task_registry import USER_STOP_REASON, task_registry
from app.db import database, remote_tasks, session_events, turns
from app.db.base import get_db
from app.network import control
from tests.core.network.test_remote_task_control import (
    UNRAID_URL, WINDOWS_URL, all_turns, finish_child, network, started_child, wakes,
)

MESSAGE_PATH = ("POST", "/api/v1/network/peer/control/task-message")


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
    for timer in list(wakeup_service._timers.values()):
        timer.cancel()
    wakeup_service._timers.clear()


def send(b, to, text, *, sender="caller", urgent=False):
    with b.active():
        message = agent_messaging.make_message(lambda: sender)
        return asyncio.run(message({"to": to, "message": text, "urgent": urgent}, None))


def message_turns(session_id):
    return [t for t in all_turns(session_id) if t["source"] == "agent_message"]


def deliver(a, child, **kwargs):
    with a.active():
        turn = finish_child(child, **kwargs)
        asyncio.run(remote_task.on_child_settled(child))
        asyncio.run(remote_task.deliver_due())
    return turn


def finish_turn(a, child, turn_id, text):
    """Run a resumed turn of the child to its end and deliver its report."""
    with a.active():
        assert turns.start_turn(turn_id)
        session_events.append_events(child, [{"type": "assistant.text_delta", "turn_id": turn_id,
                                              "payload": {"text": text}}])
        assert turns.settle_turn(turn_id, "completed")
        asyncio.run(remote_task.on_child_settled(child))
        asyncio.run(remote_task.deliver_due())


def finish_wake(a, b, wake):
    """The caller ran its wake, so the next report needs a wake of its own.

    Turn times have one-second resolution in the test store; the first run
    is moved back so "latest turn" means what it does when a message comes
    some time after the report.
    """
    with b.active():
        assert turns.start_turn(wake["id"])
        assert turns.settle_turn(wake["id"], "completed")
    for worker in (a, b):
        with worker.active():
            with get_db() as conn:
                conn.cursor().execute("UPDATE turns SET created_at=%s", ("2000-01-01 00:00:00",))


def body(child, *, to=remote_task.TO_CHILD, sender="caller", text="Also check the logs.",
         message_id=None, known=None):
    return {"version": remote_task.WIRE_VERSION, "to": to, "worker_session_id": child,
            "message_id": message_id or str(uuid.uuid4()), "sender_session_id": sender,
            "text": text, "urgent": False, "known_completion_id": known}


def call(worker, device_id, payload):
    with worker.active():
        return asyncio.run(control.call(device_id, remote_task.TASK_MESSAGE_ACTION, payload,
                                        timeout=remote_task.MESSAGE_TIMEOUT))


# --- parent to a running child -------------------------------------------------------

def test_a_busy_child_gets_the_message_queued(workers, monkeypatch, kicks):
    a, b, router = network(workers, monkeypatch)
    child = started_child(a, b)
    outcome = send(b, child, "Also check the logs.")
    assert not outcome.is_error, outcome.text
    assert outcome.text == "Queued for Default on Unraid"
    assert MESSAGE_PATH in router.requests(UNRAID_URL)
    with a.active():
        [queued] = message_turns(child)
        assert "Also check the logs." in queued["prompt"]
        assert queued["payload"]["task_peer_sender"] == "caller"
        assert remote_tasks.get_child(child)["state"] == remote_tasks.RUNNING
    with b.active():
        assert message_turns(child) == []  # nothing queued on the read-only mirror
        assert task_registry.get_task("caller", child)["status"] == "running"


def test_a_running_child_takes_the_message_at_its_next_tool_result(workers, monkeypatch, kicks):
    """#1804: the parent lives on the other worker, so it has no roster on the
    child's; the live hand-over must still accept it as the child's `main`."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from app.core import session_runtime

    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)
    with a.active():
        [turn] = [t for t in all_turns(child) if t["source"] == "provider_agent"]
        assert turns.start_turn(turn["id"])
        runtime = session_runtime.SessionRuntime(turns.get_turn(turn["id"]))
    monkeypatch.setitem(session_runtime._live_runtimes, child, runtime)
    adapter = MagicMock(steer=AsyncMock(), steer_peer=AsyncMock(), provider_kind="delegated")
    state = SimpleNamespace(adapter=adapter)
    monkeypatch.setattr("app.core.session_pool.get_session_pool",
                        lambda: SimpleNamespace(get=lambda _: state, peek=lambda _: state))
    # Both test workers share one process-wide task registry, so "caller"'s
    # roster would be visible on the child's worker; in production it is not.
    on_target = []
    real_rows, real_message_child = agent_messaging.peer_rows, remote_task._message_child

    def peer_rows(session_id):
        return [] if on_target and session_id == "caller" else real_rows(session_id)

    async def message_child(msg, from_device):
        on_target.append(True)
        try:
            return await real_message_child(msg, from_device)
        finally:
            on_target.clear()

    monkeypatch.setattr(agent_messaging, "peer_rows", peer_rows)
    monkeypatch.setattr(remote_task, "_message_child", message_child)
    outcome = send(b, child, "Also check the logs.")
    assert not outcome.is_error, outcome.text
    with a.active():
        [queued] = message_turns(child)
    adapter.steer_peer.assert_awaited_once_with("Also check the logs.", sender="caller",
                                                id=queued["id"])
    assert queued["id"] in runtime._steer_attempted
    with a.active():
        on_target.append(True)
        assert agent_messaging.may_message("caller", child)
        assert not agent_messaging.may_message("someone-else", child)


def test_a_parked_child_holds_the_message_unless_urgent(workers, monkeypatch, kicks):
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)
    with a.active():
        [turn] = [t for t in all_turns(child) if t["source"] == "provider_agent"]
        assert turns.start_turn(turn["id"])
        turns.park_turn(turn["id"], ["bg-1"])
    held = send(b, child, "Ping when the scan is done.")
    assert not held.is_error and "Default on Unraid" in held.text
    assert held.text == agent_messaging.held_receipt("Default on Unraid")
    with a.active():
        assert message_turns(child) == []
    urgent = send(b, child, "Stop the scan now.", urgent=True)
    assert not urgent.is_error and urgent.text == "Queued for Default on Unraid"
    with a.active():
        [wake] = message_turns(child)
        # The urgent wake carries the message held before it.
        assert "Ping when the scan is done." in wake["prompt"]
        assert "Stop the scan now." in wake["prompt"]


# --- resume a finished child -----------------------------------------------------------

def test_a_finished_child_resumes_and_its_next_report_wakes_the_caller_again(
        workers, monkeypatch, kicks):
    a, b, router = network(workers, monkeypatch)
    child = started_child(a, b)
    first = deliver(a, child, text="Disk is 40% full.", deferred="permission for Bash")
    with b.active():
        [wake] = wakes()
        assert "permission for Bash" in wake["prompt"]
        assert task_registry.get_task("caller", child)["status"] == "completed"
    finish_wake(a, b, wake)

    outcome = send(b, child, "Now clean the cache.")
    assert not outcome.is_error, outcome.text
    assert outcome.text.startswith("Resume queued for Default on Unraid (turn ")
    assert "woken with its new report" in outcome.text
    with a.active():
        [resumed] = message_turns(child)
        assert "Now clean the cache." in resumed["prompt"]
        assert resumed["payload"].get("agent_message_reply_to") is None
        assert remote_tasks.get_child(child)["state"] == remote_tasks.RUNNING
    with b.active():
        record = remote_tasks.get_dispatch(child)
        assert record["completion_id"] is None
        assert remote_tasks.past_completions(child) == [first["id"]]
        assert task_registry.get_task("caller", child)["status"] == "running"
        assert task_registry.get_task("caller", child)["worker"]["name"] == "Unraid"
        assert provider_agent_supervisor.get_worker("caller", child) is not None

    finish_turn(a, child, resumed["id"], "Cache cleaned, 12 GB freed.")
    with a.active():
        assert remote_tasks.get_child(child)["state"] == remote_tasks.DELIVERED
    with b.active():
        assert len(wakes()) == 2
        second = wakes()[-1]
        assert "Cache cleaned, 12 GB freed." in second["prompt"]
        # Approvals held back during the first run are not repeated.
        assert "permission for Bash" not in second["prompt"]
        record = remote_tasks.get_dispatch(child)
        assert record["completion_id"] == resumed["id"]
        assert task_registry.get_task("caller", child)["status"] == "completed"
        # Retries of either report wake nobody.
        old = {"version": 1, "worker_session_id": child, "completion_id": first["id"],
               "status": "completed", "report": "Disk is 40% full.", "truncated": False,
               "needs_owner": []}
        for completion in (first["id"], resumed["id"]):
            again = asyncio.run(control.handle(remote_task.TASK_REPORT_ACTION,
                                               {**old, "completion_id": completion},
                                               from_device=a.device_id))
            assert again == {"delivered": True, "duplicate": True}
        assert len(wakes()) == 2


def test_a_resumed_report_that_beats_the_answer_still_wakes_the_caller(
        workers, monkeypatch, kicks):
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)
    first = deliver(a, child)
    with b.active():
        [wake] = wakes()
    finish_wake(a, b, wake)
    # Windows sent the message but has not handled the answer yet.
    answer = call(b, a.device_id, body(child, known=first["id"]))
    assert answer["status"] == remote_task.RESUMED and answer["duplicate"] is False
    finish_turn(a, child, answer["turn_id"], "Second pass done.")
    with b.active():
        assert len(wakes()) == 2 and "Second pass done." in wakes()[-1]["prompt"]
        assert remote_tasks.get_dispatch(child)["completion_id"] == answer["turn_id"]
        assert remote_tasks.past_completions(child) == [first["id"]]
        # The late answer finds the record already moved on: nothing reopens.
        assert asyncio.run(remote_task.reopen(remote_tasks.get_dispatch(child), first["id"])) is False
        assert task_registry.get_task("caller", child)["status"] == "completed"


def test_a_message_while_the_report_is_on_its_way_is_refused(workers, monkeypatch, kicks):
    a, b, router = network(workers, monkeypatch)
    child = started_child(a, b)
    router.down.add(WINDOWS_URL)
    with a.active():
        finish_child(child)
        asyncio.run(remote_task.sweep())
        assert remote_tasks.get_child(child)["state"] == remote_tasks.OWED
    outcome = send(b, child, "Anything else?")
    assert outcome.is_error and "its report is still on its way" in outcome.text
    with a.active():
        assert message_turns(child) == []


# --- refusals ------------------------------------------------------------------------

def test_a_stopped_child_refuses_at_the_parent(workers, monkeypatch, kicks):
    a, b, router = network(workers, monkeypatch)
    child = started_child(a, b)
    with b.active():
        task_registry.complete_task("caller", child, "killed", reason=USER_STOP_REASON)
    outcome = send(b, child, "Keep going.")
    assert outcome.is_error and "it was stopped" in outcome.text
    assert MESSAGE_PATH not in router.requests(UNRAID_URL)


def test_a_stopped_child_refuses_at_the_target(workers, monkeypatch, kicks):
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)
    with a.active():
        finish_child(child)
        remote_tasks.owe_report(child, "t-stopped", {"status": "stopped"})
    with pytest.raises(control.ControlRefused, match="stopped on this worker"):
        call(b, a.device_id, body(child, known="t-stopped"))
    with a.active():
        assert message_turns(child) == []


def test_a_child_whose_stop_is_not_answered_yet_refuses_at_the_parent(workers, monkeypatch, kicks):
    """S8 stop still pending (target away, record not settled): no message goes out."""
    a, b, router = network(workers, monkeypatch)
    child = started_child(a, b)
    router.down.add(UNRAID_URL)
    with b.active():
        provider_agent_supervisor.unregister_worker("caller", child)

        async def stop():
            answer = await remote_task.stop_remote_child(child, a.device_id, reason="swarm_cancelled")
            await remote_task.drain_stops()
            return answer
        assert asyncio.run(stop())["outcome"] == "remote_stop_pending"
        assert task_registry.get_task("caller", child)["status"] == "running"
    router.down.clear()
    outcome = send(b, child, "Keep going.")
    assert outcome.is_error and "it was stopped" in outcome.text
    assert MESSAGE_PATH not in router.requests(UNRAID_URL)
    with a.active():
        assert message_turns(child) == []


def test_a_stopped_finished_child_is_not_resumed_after_its_latch_is_lost(
        workers, monkeypatch, kicks):
    """The target's durable stop holds when its in-memory latch is gone (a restart)."""
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)
    first = deliver(a, child)
    with b.active():
        [wake] = wakes()
    finish_wake(a, b, wake)
    with b.active():
        answer = asyncio.run(remote_task.stop_remote_child(child, a.device_id, reason="user_stopped"))
        assert answer == {"outcome": "remote_finished"}
    with a.active():
        assert remote_tasks.get_child(child)["stop_requested_at"]
    task_registry.clear_user_stop(child)
    with pytest.raises(control.ControlRefused, match="stopped on this worker"):
        call(b, a.device_id, body(child, known=first["id"]))
    with a.active():
        assert message_turns(child) == []
        assert remote_tasks.get_child(child)["state"] == remote_tasks.DELIVERED
    with b.active():
        assert len(wakes()) == 1


def test_an_unreachable_target_is_named_and_nothing_is_claimed(workers, monkeypatch, kicks):
    a, b, router = network(workers, monkeypatch)
    child = started_child(a, b)
    router.down.add(UNRAID_URL)
    outcome = send(b, child, "Are you there?")
    assert outcome.is_error and "Unraid cannot be reached right now" in outcome.text
    assert "was not delivered" in outcome.text


def test_an_older_target_is_named_as_older(workers, monkeypatch, kicks):
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)
    monkeypatch.setattr(control, "known_action",
                        lambda action: action != remote_task.TASK_MESSAGE_ACTION)
    original = control._LAZY_HANDLERS.pop(remote_task.TASK_MESSAGE_ACTION)
    try:
        outcome = send(b, child, "Hello?")
    finally:
        control._LAZY_HANDLERS[remote_task.TASK_MESSAGE_ACTION] = original
    assert outcome.is_error and "runs an older release" in outcome.text


def test_a_message_id_is_applied_once(workers, monkeypatch, kicks):
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)
    payload = body(child)
    first = call(b, a.device_id, payload)
    again = call(b, a.device_id, payload)
    assert first["status"] == agent_messaging.QUEUED and first["duplicate"] is False
    assert again == {**first, "duplicate": True}
    with a.active():
        assert len(message_turns(child)) == 1
    other = started_child(a, b)
    with pytest.raises(control.ControlRefused, match="already used"):
        call(b, a.device_id, {**payload, "worker_session_id": other})


def test_only_the_dispatching_conversation_may_message_the_child(workers, monkeypatch, kicks):
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)
    with b.active():
        database.create_session(session_id="other-chat", profile_id="default", project_id=None,
                                title="Another chat", is_active=True)
    with pytest.raises(control.ControlRefused, match="Only the conversation that started"):
        call(b, a.device_id, body(child, sender="other-chat"))
    with a.active():
        # A worker of the account that did not start the child.
        msg = remote_task._parse_message(body(child))
        with pytest.raises(PermissionError, match="No subagent with this id"):
            asyncio.run(remote_task._message_child(msg, "dev-laptop"))
        with pytest.raises(PermissionError, match="Only active workers"):
            asyncio.run(control.handle(remote_task.TASK_MESSAGE_ACTION, body(child),
                                       from_device="dev-stranger"))
        assert message_turns(child) == []


# --- child to main ---------------------------------------------------------------------

def test_the_child_reaches_main_on_the_parent_worker(workers, monkeypatch, kicks):
    a, b, router = network(workers, monkeypatch)
    child = started_child(a, b)
    with a.active():
        [main] = [r for r in agent_messaging.peer_rows(child) if r["name"] == "main"]
        assert main["worker_session_id"] == "caller" and main["remote_parent"] == b.device_id
        assert "on Windows" in main["description"]
    outcome = send(a, "main", "Found a second disk; scan it too?", sender=child)
    assert not outcome.is_error, outcome.text
    assert outcome.text == "Queued for main on Windows"
    assert MESSAGE_PATH in router.requests(WINDOWS_URL)
    with b.active():
        [queued] = message_turns("caller")
        assert "Found a second disk; scan it too?" in queued["prompt"]
        assert queued["payload"]["task_peer_sender"] == child
    with a.active():
        assert message_turns("caller") == []


def test_main_refuses_a_child_it_did_not_send_there(workers, monkeypatch, kicks):
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)
    with pytest.raises(control.ControlRefused, match="No subagent with this id"):
        call(a, b.device_id, body(child, to=remote_task.TO_MAIN, sender="caller"))
    with b.active():
        assert message_turns("caller") == []


# --- a local-only session is unchanged ---------------------------------------------------

def test_local_messaging_is_unchanged(workers, monkeypatch, kicks):
    a, b, router = network(workers, monkeypatch)
    with b.active():
        database.create_session(session_id="local-worker", profile_id="default", project_id=None,
                                title="Local worker", is_active=True)
        task_registry.start_task("caller", "local-worker", "agent", "Local job",
                                 tool_use_id="local-worker")
        assert remote_task.remote_parent("local-worker") is None
        assert all("remote_parent" not in row for row in agent_messaging.peer_rows("local-worker"))
    outcome = send(b, "local-worker", "Local hello.")
    assert not outcome.is_error and outcome.text == "Queued for Local job"
    with b.active():
        [queued] = message_turns("local-worker")
        assert "Local hello." in queued["prompt"]
    assert MESSAGE_PATH not in router.requests(UNRAID_URL)
    assert MESSAGE_PATH not in router.requests(WINDOWS_URL)


# --- one completion, after the child's last turn (#1812) ----------------------------------

def backdate_turns(*workers, keep=None):
    """Earlier turns sort first: the test store stamps turns to the second."""
    for worker in workers:
        with worker.active():
            with get_db() as conn:
                conn.cursor().execute("UPDATE turns SET created_at=%s WHERE id != %s",
                                      ("2000-01-01 00:00:00", keep or ""))


def run_turn(child, turn_id, text, outcome="completed"):
    assert turns.start_turn(turn_id)
    session_events.append_events(child, [{"type": "assistant.text_delta", "turn_id": turn_id,
                                          "payload": {"text": text}}])
    assert turns.settle_turn(turn_id, outcome)


def settle_and_deliver(child):
    asyncio.run(remote_task.on_child_settled(child))
    return asyncio.run(remote_task.sweep())


def test_a_child_that_parks_on_its_monitor_reports_once_after_its_last_turn(
        workers, monkeypatch, kicks):
    """The turn that ends to wait on a Monitor is not the report; the turn its
    completion wakes is, and the parent is woken once, with that one."""
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)
    with a.active():
        [first] = [t for t in all_turns(child) if t["source"] == "provider_agent"]
        task_registry.start_task(child, "mon-tests", "shell", "npm test")
        task_registry.mark_monitor_shell_task(child, "mon-tests")
        run_turn(child, first["id"], "The test run is still going; ending the turn so it wakes me.")
        assert settle_and_deliver(child) == 0
        assert remote_tasks.get_child(child)["state"] == remote_tasks.RUNNING
        # The Monitor finishes and queues the child's wake: still not over.
        task_registry.complete_task(child, "mon-tests", "completed", summary="exit 0")
        wake = turns.enqueue_turn(child, "unpark", prompt="<task-notification>exit 0</task-notification>")
        assert settle_and_deliver(child) == 0
    with b.active():
        assert wakes() == []
        assert task_registry.get_task("caller", child)["status"] == "running"
    backdate_turns(a, keep=wake["id"])
    with a.active():
        run_turn(child, wake["id"], "All 120 tests pass. Pushed fix/chat-prompts.")
        assert settle_and_deliver(child) == 1
        assert remote_tasks.get_child(child)["completion_id"] == wake["id"]
    with b.active():
        [parent_wake] = wakes()
        assert "All 120 tests pass." in parent_wake["prompt"]
        assert "still going" not in parent_wake["prompt"]
        assert task_registry.get_task("caller", child)["status"] == "completed"
    with a.active():
        assert asyncio.run(remote_task.sweep()) == 0
    with b.active():
        assert len(wakes()) == 1


def test_a_message_steered_into_a_running_child_is_not_its_completion(workers, monkeypatch, kicks):
    """The message's queue row settles the moment the live turn reads it; the
    child stays running for the parent until that turn ends."""
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)
    with a.active():
        [first] = [t for t in all_turns(child) if t["source"] == "provider_agent"]
        assert turns.start_turn(first["id"])
    outcome = send(b, child, "How far along are you?")
    assert not outcome.is_error, outcome.text
    with a.active():
        [queued] = message_turns(child)
        assert turns.absorb_queued_turn(queued["id"])  # the live turn took it as a steer
        assert settle_and_deliver(child) == 0
        assert remote_tasks.get_child(child)["state"] == remote_tasks.RUNNING
    with b.active():
        assert wakes() == []
        assert task_registry.get_task("caller", child)["status"] == "running"
    with a.active():
        session_events.append_events(child, [{"type": "assistant.text_delta", "turn_id": first["id"],
                                              "payload": {"text": "Counted all six steps."}}])
        assert turns.settle_turn(first["id"], "completed")
        assert settle_and_deliver(child) == 1
        assert remote_tasks.get_child(child)["completion_id"] == first["id"]
    with b.active():
        [parent_wake] = wakes()
        assert "Counted all six steps." in parent_wake["prompt"]
        assert "without a written report" not in parent_wake["prompt"]


def test_a_child_that_runs_again_after_its_report_reports_again(workers, monkeypatch, kicks):
    """A later turn (a wake, the owner typing in its chat) replaces the stored
    report and reaches the parent once more; a stopped child never reopens."""
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)
    first = deliver(a, child, text="Parked on the batch.")
    with b.active():
        [first_wake] = wakes()
    finish_wake(a, b, first_wake)
    with a.active():
        later = turns.enqueue_turn(child, "unpark", prompt="<task-notification>done</task-notification>")
        run_turn(child, later["id"], "The batch passed; branch pushed.")
        assert settle_and_deliver(child) == 1
        row = remote_tasks.get_child(child)
        assert row["state"] == remote_tasks.DELIVERED and row["completion_id"] == later["id"]
    with b.active():
        assert len(wakes()) == 2 and "The batch passed" in wakes()[-1]["prompt"]
        record = remote_tasks.get_dispatch(child)
        assert record["completion_id"] == later["id"]
        assert remote_tasks.past_completions(child) == [first["id"]]
        assert task_registry.get_task("caller", child)["status"] == "completed"
        # TaskGet's report is the new one.
        from pathlib import Path
        assert Path(record["report_path"]).read_text(encoding="utf-8") == "The batch passed; branch pushed."
    # Stopped: a later turn changes nothing.
    with a.active():
        remote_tasks.mark_child_stopped(child)
    backdate_turns(a, b)
    with a.active():
        again = turns.enqueue_turn(child, "user", prompt="one more thing")
        run_turn(child, again["id"], "Did one more thing.")
        assert settle_and_deliver(child) == 0
        assert remote_tasks.get_child(child)["completion_id"] == later["id"]
    with b.active():
        assert len(wakes()) == 2


def test_a_report_not_sent_yet_is_replaced_by_the_newer_one(workers, monkeypatch, kicks):
    a, b, router = network(workers, monkeypatch)
    child = started_child(a, b)
    router.down.add(WINDOWS_URL)
    with a.active():
        first = finish_child(child, text="First pass.")
        assert settle_and_deliver(child) == 0
        assert remote_tasks.get_child(child)["completion_id"] == first["id"]
    backdate_turns(a)
    with a.active():
        later = turns.enqueue_turn(child, "unpark", prompt="<task-notification>done</task-notification>")
        run_turn(child, later["id"], "Second pass.")
        asyncio.run(remote_task.on_child_settled(child))
        row = remote_tasks.get_child(child)
        assert row["state"] == remote_tasks.OWED and row["completion_id"] == later["id"]
        assert row["report"]["report"] == "Second pass."
    router.down.clear()
    with a.active():
        with get_db() as conn:
            conn.cursor().execute("UPDATE remote_task_children SET next_attempt_at=%s",
                                  ("2000-01-01T00:00:00+00:00",))
        assert asyncio.run(remote_task.sweep()) == 1
    with b.active():
        [parent_wake] = wakes()
        assert "Second pass." in parent_wake["prompt"] and "First pass." not in parent_wake["prompt"]


def test_a_report_arriving_while_the_parent_turn_runs_is_delivered_once(workers, monkeypatch, kicks):
    """Mid-turn: the completion is steered into the live turn; a turn that ends
    without reading it enqueues the ordinary wake, so it is never dropped."""
    from app.core import session_runtime

    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)
    steered = []

    async def steer(session_id, prompt, *, notification_id, frames=None, task_ids=None, **_):
        steered.append({"session_id": session_id, "frames": frames, "task_ids": task_ids})
        return True
    monkeypatch.setattr(session_runtime, "try_steer_task_notification", steer)
    deliver(a, child, text="Done mid-turn.")
    with b.active():
        assert [s["session_id"] for s in steered] == ["caller"]
        assert wakes() == []
        assert steered[0]["task_ids"] == [child]
        # The parent's turn ended before fluid delivered it.
        asyncio.run(turn_scheduler.on_task_notification_stranded(
            "caller", steered[0]["frames"], steered[0]["task_ids"]))
        [parent_wake] = wakes()
        assert "Done mid-turn." in parent_wake["prompt"]
