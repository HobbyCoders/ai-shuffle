"""Approvals of a Task child on another worker, over the real signed control endpoint.

A ("Unraid") runs the child; B ("Windows") runs the parent chat "caller". Both
workers share this process, and which one is "current" is process-wide, so
each side runs under its own ``with x.active()``:

* the child's tool call (``route_permission`` on A) starts as a task; its
  request to B shows the proxy prompt there, whose waiter is collected through
  the ``_spawn`` seam instead of being detached on the request's short loop;
* the proxy then runs under ``b.active()`` until the owner answers on B and
  the answer is accepted on A;
* answers reach the waiting tool call through ``_settle``, which these tests
  hold until A is current again, so the child's side never touches B's data.
"""
import asyncio

import pytest

from app.core import (
    assistant_confirm,
    assistant_service,
    permission_handler as permission_module,
    provider_agent,
    provider_agent_approvals as approvals,
    provider_agent_supervisor,
    remote_task,
    remote_task_approvals as relay,
    turn_scheduler,
)
from app.core.agent_types import PermissionResultAllow, PermissionResultDeny
from app.core.permission_handler import permission_handler
from app.db import interactions, remote_tasks, session_events, turns
from app.network import control
from tests.core.network.test_remote_task_control import (
    UNRAID_URL,
    WINDOWS_URL,
    all_turns,
    finish_child,
    network,
    started_child,
)

TOOL_INPUT = {"command": "df -h /mnt/user", "description": "Check the disk"}


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    seen = []
    monkeypatch.setattr(turn_scheduler, "kick", seen.append)
    monkeypatch.setattr(turn_scheduler, "_kick_after_batch_window", lambda sid: None)
    relay.reset()
    approvals.reset_routes()
    provider_agent.reset_worker_sessions()
    assistant_confirm.reset()
    permission_handler.clear_session_rules("caller")  # the handler is process-wide
    yield
    relay.reset()
    approvals.reset_routes()
    provider_agent.reset_worker_sessions()
    assistant_confirm.reset()
    permission_handler.clear_session_rules("caller")


@pytest.fixture
def proxies(monkeypatch):
    collected = []
    monkeypatch.setattr(relay, "_spawn", lambda coro, name: collected.append(coro))
    yield collected
    for coro in collected:
        coro.close()  # never-run proxies, so no "never awaited" warning


@pytest.fixture
def held(monkeypatch):
    """Answers accepted on A, held until the test releases them under ``a.active()``."""
    settled = []
    monkeypatch.setattr(relay, "_settle", lambda future, answer: settled.append((future, answer)))
    return settled


def release(settled):
    for future, answer in settled:
        if not future.done():
            future.set_result(answer)
    settled.clear()


async def not_local(**kwargs):
    raise AssertionError("a remote child's prompt must not be raised on its own worker")


def ask_kwargs(session_id, **changes):
    kwargs = dict(request_id=f"perm-{session_id}-0a1b2c3d", session_id=session_id, profile_id="default",
                  tool_name="Bash", tool_input=dict(TOOL_INPUT), broadcast_func=None,
                  title=None, display_name="Bash", description="Run df", decision_reason=None)
    kwargs.update(changes)
    return kwargs


async def until(predicate, timeout=10.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if loop.time() > deadline:
            raise AssertionError("timed out waiting")
        await asyncio.sleep(0.01)


async def pending_proxy_row(session_id="caller"):
    def read():
        rows = [r for r in interactions.get_pending_interactions(session_id)
                if (r.get("payload") or {}).get("provider_agent", {}).get("remote_approval_id")]
        return rows[0] if rows else None
    return await until(lambda: read())


async def start_ask(a, proxies, kwargs):
    """The child's tool call on A, until B has accepted its request."""
    with a.active():
        ask = asyncio.create_task(approvals.route_permission(kwargs, not_local))
        await until(lambda: proxies or ask.done())
    return ask


async def answer_on_b(b, proxies, answer):
    """Run B's proxy prompt, let ``answer(row)`` decide, and send the result to A."""
    with b.active():
        proxy = asyncio.create_task(proxies.pop(0))
        row = await pending_proxy_row()
        await answer(row)
        await asyncio.wait_for(proxy, 10)
        return row


def responder(decision):
    async def answer(row):
        result = await permission_handler.respond(row["payload"]["request_id"], "caller", decision)
        assert result.get("resolved") is not False, result
    return answer


async def finish_ask(a, ask, held):
    with a.active():
        release(held)
        return await asyncio.wait_for(ask, 10)


def round_trip(a, b, proxies, held, kwargs, answer):
    async def scenario():
        ask = await start_ask(a, proxies, kwargs)
        assert proxies, await ask  # the request was refused: show why
        row = await answer_on_b(b, proxies, answer)
        return row, await finish_ask(a, ask, held)
    return asyncio.run(scenario())


def start_child_turn(a, child):
    with a.active():
        [turn] = [t for t in all_turns(child) if t["source"] == "provider_agent"]
        assert turns.start_turn(turn["id"])
        session_events.append_events(child, [{"type": "message.user", "turn_id": turn["id"],
                                              "payload": {"content": "Check the disk."}}])
    return turn


def report_needs(a, child, turn):
    with a.active():
        session_events.append_events(child, [{"type": "assistant.text_delta", "turn_id": turn["id"],
                                              "payload": {"text": "Could not check."}}])
        assert turns.settle_turn(turn["id"], "completed")
        return remote_task.build_child_report(child)["needs_owner"]


# --- the round trip -------------------------------------------------------------

def test_an_allow_on_the_parent_runs_the_childs_own_input(workers, monkeypatch, proxies, held):
    a, b, router = network(workers, monkeypatch)
    child = started_child(a, b)
    kwargs = ask_kwargs(child, tool_input={**TOOL_INPUT, "env": {"API_TOKEN": "x" * 3000}})
    row, result = round_trip(a, b, proxies, held, kwargs, responder("allow"))

    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input == kwargs["tool_input"]  # the child's own input, unchanged
    assert ("POST", "/api/v1/network/peer/control/task-approval") in router.requests(WINDOWS_URL)
    payload = row["payload"]
    assert row["session_id"] == "caller"
    assert payload["title"].startswith("Task · Default on Unraid")
    assert "a subagent on another worker" in payload["description"]
    tag = payload["provider_agent"]
    assert tag["worker_session_id"] == child and tag["worker_name"] == "Unraid"
    assert tag["remote_device_id"] == a.device_id
    # Only a bounded summary of the input travelled.
    assert payload["tool_input"]["command"] == TOOL_INPUT["command"]
    assert len(payload["tool_input"]["env"]) <= relay.MAX_VALUE_CHARS
    with a.active():
        assert relay._WAITING == {}


def test_a_deny_on_the_parent_denies_the_tool_call(workers, monkeypatch, proxies, held):
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)
    turn = start_child_turn(a, child)
    _, result = round_trip(a, b, proxies, held, ask_kwargs(child), responder("deny"))
    assert isinstance(result, PermissionResultDeny)
    assert result.message == "The owner denied permission (asked on Windows)."
    # A person decided, so there is nothing to list for the owner.
    assert report_needs(a, child, turn) == []


def test_saved_rules_on_the_parent_never_decide_for_another_worker(workers, monkeypatch, proxies, held):
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)
    with b.active():
        permission_handler._add_session_rule_idempotent("caller", "Bash", None, "allow")
    row, result = round_trip(a, b, proxies, held, ask_kwargs(child), responder("deny"))
    assert row is not None  # the prompt was shown despite the rule
    assert isinstance(result, PermissionResultDeny)


def test_always_allow_on_the_card_travels_as_allow_once(workers, monkeypatch, proxies, held):
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)

    async def always(row):
        result = await permission_handler.respond(row["payload"]["request_id"], "caller", "allow",
                                                  remember="session", pattern="df *")
        assert result.get("resolved") is True, result
    _, result = round_trip(a, b, proxies, held, ask_kwargs(child), always)
    assert isinstance(result, PermissionResultAllow)
    assert [r["tool_pattern"] for r in permission_handler.get_session_rules("caller")] == ["df *"]

    # The next call asks again: the rule stayed on the parent.
    _, second = round_trip(a, b, proxies, held, ask_kwargs(child), responder("deny"))
    assert isinstance(second, PermissionResultDeny)


def test_a_local_worker_under_the_remote_child_asks_the_parent_too(workers, monkeypatch, proxies, held):
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)
    with a.active():
        provider_agent.register_worker_session("grandchild")
        approvals.register_route(worker_session_id="grandchild", parent_session_id=child, label="Researcher")
    row, result = round_trip(a, b, proxies, held, ask_kwargs("grandchild"), responder("allow"))
    assert isinstance(result, PermissionResultAllow)
    assert row["payload"]["title"].startswith("Researcher under Task · Default on Unraid")
    assert row["payload"]["provider_agent"]["worker_session_id"] == child


def test_the_assistant_home_answers_in_conversation(workers, monkeypatch, proxies, held):
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)
    monkeypatch.setattr(assistant_service, "is_primary_session", lambda sid: sid == "caller")

    async def say_yes(row):
        confirm = row["payload"]["assistant_confirm"]
        assert "df -h /mnt/user" in confirm["question"] and "on Unraid" in confirm["question"]
        assert await assistant_confirm.on_owner_message("caller", "yes") == "approved"
    _, result = round_trip(a, b, proxies, held, ask_kwargs(child), say_yes)
    assert isinstance(result, PermissionResultAllow)


def test_an_ordinary_chat_is_not_looked_up(monkeypatch):
    calls = []
    monkeypatch.setattr(relay, "remote_child", lambda sid: calls.append(sid))

    async def local(**kwargs):
        return PermissionResultAllow(updated_input=kwargs["tool_input"])
    result = asyncio.run(approvals.route_permission(ask_kwargs("plain-chat"), local))
    assert isinstance(result, PermissionResultAllow) and calls == []


# --- nobody decides: deny, and list it for the owner ------------------------------

def test_a_lapsed_prompt_on_the_parent_denies_and_is_listed(workers, monkeypatch, proxies, held):
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)
    turn = start_child_turn(a, child)
    monkeypatch.setattr(permission_module, "PERMISSION_TTL_SECONDS", 1)
    monkeypatch.setattr(relay, "MIN_TTL_SECONDS", 1)

    async def nobody(row):
        return None
    _, result = round_trip(a, b, proxies, held, ask_kwargs(child), nobody)
    assert isinstance(result, PermissionResultDeny)
    assert result.message.startswith("Not approved: Nobody answered the approval in time on Windows.")
    assert relay.NO_ANSWER_TAIL in result.message
    assert report_needs(a, child, turn) == ["permission for Bash"]


def test_an_unreachable_parent_denies_and_is_listed(workers, monkeypatch, proxies, held):
    a, b, router = network(workers, monkeypatch)
    child = started_child(a, b)
    turn = start_child_turn(a, child)
    router.down.add(WINDOWS_URL)
    with a.active():
        result = asyncio.run(approvals.route_permission(ask_kwargs(child), not_local))
    assert isinstance(result, PermissionResultDeny)
    assert "Windows could not be reached to ask the owner" in result.message
    assert proxies == []
    assert report_needs(a, child, turn) == ["permission for Bash"]


def test_an_older_parent_denies_by_name(workers, monkeypatch, proxies, held):
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)
    monkeypatch.setattr(control, "known_action", lambda action: action != relay.APPROVAL_ACTION)
    with a.active():
        result = asyncio.run(approvals.route_permission(ask_kwargs(child), not_local))
    assert isinstance(result, PermissionResultDeny)
    assert "Windows runs an older release that cannot show approvals" in result.message


def test_no_answer_within_the_deadline_denies_and_withdraws(workers, monkeypatch, proxies, held):
    a, b, router = network(workers, monkeypatch)
    child = started_child(a, b)
    turn = start_child_turn(a, child)
    monkeypatch.setattr(relay, "MIN_TTL_SECONDS", 0.2)
    monkeypatch.setattr(relay, "GRACE_SECONDS", 0.0)
    monkeypatch.setattr(relay, "_ttl", lambda value: 0.2)
    withdrawals = []
    monkeypatch.setattr(relay, "_withdraw_later", lambda *args: withdrawals.append(args))

    async def scenario():
        ask = await start_ask(a, proxies, ask_kwargs(child))
        with a.active():
            return await asyncio.wait_for(ask, 10)
    result = asyncio.run(scenario())
    assert isinstance(result, PermissionResultDeny)
    assert "nobody answered on Windows" in result.message
    assert withdrawals and withdrawals[0][0] == b.device_id
    assert report_needs(a, child, turn) == ["permission for Bash"]


# --- stop, withdraw and restart -------------------------------------------------------

def test_stopping_the_child_on_the_parent_denies_the_tool_call(workers, monkeypatch, proxies, held):
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)

    async def stop(row):
        record = provider_agent_supervisor.get_worker("caller", child)
        await provider_agent_supervisor.teardown_worker(
            record, reason="user_stopped_all", summary="Stopped by user", preview="Stopped by the user.")
        assert interactions.get_interaction(row["id"])["state"] == "cancelled"
    _, result = round_trip(a, b, proxies, held, ask_kwargs(child), stop)
    assert isinstance(result, PermissionResultDeny)
    assert result.message.startswith(relay.CANCELLED_MESSAGE)


def test_stopping_the_child_on_its_worker_withdraws_the_parent_prompt(workers, monkeypatch, proxies, held):
    a, b, router = network(workers, monkeypatch)
    child = started_child(a, b)

    async def scenario():
        ask = await start_ask(a, proxies, ask_kwargs(child))
        with b.active():
            proxy = asyncio.create_task(proxies.pop(0))
            await pending_proxy_row()
        with a.active():
            ask.cancel()
            with pytest.raises(asyncio.CancelledError):
                await ask
            # The withdraw is detached; let it reach B.
            await until(lambda: ("POST", "/api/v1/network/peer/control/task-approval")
                        in router.requests(WINDOWS_URL)[1:])
        with b.active():
            await asyncio.wait_for(proxy, 10)
            return interactions.get_pending_interactions("caller")
    sent_before = len(router.requests(UNRAID_URL))
    assert asyncio.run(scenario()) == []
    # Withdrawn: the parent's proxy sent no answer back.
    answers = [r for r in router.requests(UNRAID_URL)[sent_before:]
               if r[1].endswith("/task-approval-answer")]
    assert answers == []


def test_a_parent_restart_restores_the_open_prompt(workers, monkeypatch, proxies, held):
    a, b, router = network(workers, monkeypatch)
    child = started_child(a, b)

    async def scenario():
        ask = await start_ask(a, proxies, ask_kwargs(child))
        with b.active():
            proxy = asyncio.create_task(proxies.pop(0))
            row = await pending_proxy_row()
            # The parent shuts down: its waiter is cancelled, the row stays.
            proxy.cancel()
            with pytest.raises(asyncio.CancelledError):
                await proxy
            assert interactions.get_interaction(row["id"])["state"] == "pending"
            relay.reset()  # a new process remembers nothing
            assert await relay.restore_proxies() == 1
            assert await relay.restore_proxies() == 0  # once per process
            follower = asyncio.create_task(proxies.pop(0))
            await responder("allow")(row)
            await asyncio.wait_for(follower, 10)
        return await finish_ask(a, ask, held)
    # The target keeps its own waiter; only the parent "restarted".
    monkeypatch.setattr(relay, "reset", lambda: (relay._SEEN.clear(), relay._WITHDRAWN.clear()))
    result = asyncio.run(scenario())
    assert isinstance(result, PermissionResultAllow)


def test_a_report_drops_leftover_prompts(workers, monkeypatch, proxies, held):
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)

    async def scenario():
        ask = await start_ask(a, proxies, ask_kwargs(child))
        with b.active():
            proxy = asyncio.create_task(proxies.pop(0))
            await pending_proxy_row()
        with a.active():  # no awaits here, so nothing of B's runs meanwhile
            finish_child(child, text="Gave up on the disk.")
            report = remote_task.build_child_report(child)
        with b.active():
            assert await control.handle(remote_task.TASK_REPORT_ACTION, report,
                                        from_device=a.device_id) == {"delivered": True, "duplicate": False}
            await asyncio.wait_for(proxy, 10)
            assert interactions.get_pending_interactions("caller") == []
        return await finish_ask(a, ask, held)
    result = asyncio.run(scenario())
    assert isinstance(result, PermissionResultDeny)


# --- who may ask and who may answer ---------------------------------------------------

def request_body(child, **changes):
    body = {"version": 1, "op": "request", "approval_id": "a" * 32, "worker_session_id": child,
            "asker": None, "tool_name": "Bash", "title": None, "display_name": None,
            "description": None, "decision_reason": None, "input": {"command": "ls"}}
    body.update(changes)
    return body


def test_requests_are_taken_only_from_the_worker_the_child_went_to(workers, monkeypatch, proxies):
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)
    with b.active():
        with pytest.raises(PermissionError, match="Only active workers"):
            relay.accept_request(request_body(child), "dev-stranger")
        with pytest.raises(PermissionError, match="No subagent with this id"):
            relay.accept_request(request_body("someone-else"), a.device_id)
        remote_tasks.record_dispatch(worker_session_id="sent-elsewhere", target_device_id="dev-laptop",
                                     owner_session_id="caller", root_session_id="caller", tool_use_id=None,
                                     mode="background", label="x", agent="default")
        with pytest.raises(PermissionError, match="No subagent with this id"):
            relay.accept_request(request_body("sent-elsewhere"), a.device_id)
        with pytest.raises(ValueError, match="Unknown approval request fields"):
            relay.accept_request(request_body(child, updated_input={"command": "rm -rf /"}), a.device_id)
        with pytest.raises(ValueError, match="approval_id"):
            relay.accept_request(request_body(child, approval_id="../x"), a.device_id)
        assert relay.accept_request(request_body(child), a.device_id).root_session_id == "caller"
        # A finished child's late request shows nothing.
        remote_tasks.mark_delivered(child, completion_id="t1", status="completed", report_path=None)
        with pytest.raises(PermissionError, match="no longer running"):
            asyncio.run(relay.handle_task_approval(request_body(child), a.device_id))
    assert proxies == []


def test_a_replayed_request_shows_one_prompt(workers, monkeypatch, proxies):
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)
    with b.active():
        first = asyncio.run(relay.handle_task_approval(request_body(child), a.device_id))
        again = asyncio.run(relay.handle_task_approval(request_body(child), a.device_id))
    assert first["accepted"] and "duplicate" not in first
    assert again["duplicate"] is True and len(proxies) == 1


def answer_body(child, **changes):
    body = {"version": 1, "approval_id": "b" * 32, "worker_session_id": child, "decision": "allow",
            "message": None}
    body.update(changes)
    return body


def test_answers_are_taken_once_and_only_from_the_dispatching_worker(workers, monkeypatch):
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)

    async def scenario():
        loop = asyncio.get_running_loop()
        waiter = relay._Waiter("b" * 32, child, b.device_id, loop.create_future(), loop)
        relay._WAITING[waiter.approval_id] = waiter
        with a.active():
            with pytest.raises(PermissionError, match="Only active workers"):
                relay.accept_answer(answer_body(child), "dev-stranger")
            with pytest.raises(PermissionError, match="different subagent"):
                relay.accept_answer(answer_body("other-child"), b.device_id)
            with pytest.raises(ValueError, match="allow or deny"):
                relay.accept_answer(answer_body(child, decision="always"), b.device_id)
            with pytest.raises(ValueError, match="Unknown approval answer fields"):
                relay.accept_answer(answer_body(child, remember="profile"), b.device_id)
            waiter.parent_device_id = "dev-other-parent"
            with pytest.raises(PermissionError, match="Only the worker that started"):
                relay.accept_answer(answer_body(child), b.device_id)
            waiter.parent_device_id = b.device_id
            claimed, answer = relay.accept_answer(answer_body(child), b.device_id)
            assert claimed is waiter and answer == {"decision": "allow", "message": None}
            with pytest.raises(LookupError, match="no longer waiting"):
                relay.accept_answer(answer_body(child), b.device_id)
    asyncio.run(scenario())


def test_a_late_answer_over_the_wire_is_refused(workers, monkeypatch):
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)
    with b.active():
        with pytest.raises(control.ControlRefused, match="no longer waiting"):
            asyncio.run(control.call(a.device_id, relay.ANSWER_ACTION, answer_body(child)))


def test_the_input_summary_is_bounded_and_redacted():
    summary = relay.summarize_input({
        "command": "curl -H 'Authorization: Bearer sk-ant-api03-" + "A" * 40 + "' https://x",
        "nested": {"a": [1, 2, 3]},
        **{f"k{i}": "v" for i in range(30)},
    })
    assert "sk-ant-api03-" + "A" * 40 not in summary["command"]
    assert summary["nested"] == '{"a": [1, 2, 3]}'
    assert len(summary) == relay.MAX_INPUT_KEYS + 1 and "more fields not shown" in summary["…"]
    long = relay.summarize_input({"a": "x" * 5000, "b": "y" * 5000, "c": "z" * 5000, "d": "w" * 5000,
                                  "e": "q" * 5000})
    assert sum(len(v) for v in long.values()) <= relay.MAX_INPUT_CHARS + 40


# --- with stop (S8) and messages (S9) ------------------------------------------------

def test_a_stop_with_no_record_here_still_withdraws_the_prompt(workers, monkeypatch, proxies, held):
    """Swarm cancel without a supervisor record goes straight to ``stop_remote_child``."""
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)

    async def stop(row):
        provider_agent_supervisor.unregister_worker("caller", child)
        await remote_task.stop_remote_child(child, a.device_id, reason="swarm_cancelled")
        await remote_task.drain_stops()
        assert interactions.get_interaction(row["id"])["state"] == "cancelled"
    _, result = round_trip(a, b, proxies, held, ask_kwargs(child), stop)
    assert isinstance(result, PermissionResultDeny)
    assert result.message.startswith(relay.CANCELLED_MESSAGE)


def test_a_child_stopped_here_has_its_next_request_refused(workers, monkeypatch, proxies, held):
    """A stop not answered yet, with the record here still running: nothing is shown."""
    a, b, router = network(workers, monkeypatch)
    child = started_child(a, b)
    turn = start_child_turn(a, child)
    router.down.add(UNRAID_URL)
    with b.active():
        provider_agent_supervisor.unregister_worker("caller", child)

        async def stop():
            answer = await remote_task.stop_remote_child(child, a.device_id, reason="swarm_cancelled")
            await remote_task.drain_stops()
            return answer
        assert asyncio.run(stop())["outcome"] == "remote_stop_pending"
    router.down.clear()
    with a.active():
        result = asyncio.run(approvals.route_permission(ask_kwargs(child), not_local))
    assert isinstance(result, PermissionResultDeny)
    assert "Windows refused the approval request" in result.message
    assert proxies == []
    assert report_needs(a, child, turn) == ["permission for Bash"]


def test_a_child_resumed_by_a_message_asks_the_parent_and_reports_only_its_own(
        workers, monkeypatch, proxies, held):
    from app.core.task_registry import task_registry
    from tests.core.network.test_remote_task_control import wakes
    from tests.core.network.test_remote_task_messaging import finish_wake, message_turns, send

    a, b, router = network(workers, monkeypatch)
    child = started_child(a, b)
    turn = start_child_turn(a, child)
    # First run: nobody can answer on the parent, so the call is listed.
    router.down.add(WINDOWS_URL)
    with a.active():
        first = asyncio.run(approvals.route_permission(ask_kwargs(child), not_local))
    router.down.clear()
    assert isinstance(first, PermissionResultDeny)
    with a.active():
        session_events.append_events(child, [{"type": "assistant.text_delta", "turn_id": turn["id"],
                                              "payload": {"text": "Could not check."}}])
        assert turns.settle_turn(turn["id"], "completed")
        asyncio.run(remote_task.on_child_settled(child))
        asyncio.run(remote_task.deliver_due())
    with b.active():
        [wake] = wakes()
        assert "permission for Bash" in wake["prompt"]
    finish_wake(a, b, wake)

    outcome = send(b, child, "Try again, I am here now.")
    assert not outcome.is_error, outcome.text
    with b.active():
        assert task_registry.get_task("caller", child)["status"] == "running"
    with a.active():
        [resumed] = message_turns(child)
        assert turns.start_turn(resumed["id"])
        session_events.append_events(child, [{"type": "message.user", "turn_id": resumed["id"],
                                              "payload": {"content": "Try again, I am here now."}}])
    # The resumed run's prompt reaches the owner on the parent again.
    _, allowed = round_trip(a, b, proxies, held, ask_kwargs(child), responder("allow"))
    assert isinstance(allowed, PermissionResultAllow)
    # A second one nobody can answer is listed in the resumed run's report.
    router.down.add(WINDOWS_URL)
    with a.active():
        denied = asyncio.run(approvals.route_permission(ask_kwargs(child, tool_name="Write"), not_local))
    router.down.clear()
    assert isinstance(denied, PermissionResultDeny)
    with a.active():
        session_events.append_events(child, [{"type": "assistant.text_delta", "turn_id": resumed["id"],
                                              "payload": {"text": "Disk checked."}}])
        assert turns.settle_turn(resumed["id"], "completed")
        asyncio.run(remote_task.on_child_settled(child))
        asyncio.run(remote_task.deliver_due())
    with b.active():
        assert len(wakes()) == 2
        second = wakes()[-1]["prompt"]
        assert "Disk checked." in second and "permission for Write" in second
        # Held back during the first run: not repeated.
        assert "permission for Bash" not in second


# --- with the assistant's limits (S11) ------------------------------------------------

def test_a_limit_stop_withdraws_the_childs_open_prompt(workers, monkeypatch, proxies, held):
    """The daily limit stops the child through the S8 path, so its prompt goes too."""
    from app.core import assistant_limits as limits, turn_recorder
    from app.db import assistant_limits as limits_db
    from tests.core.network.test_remote_task_limits import book, make_home, set_limits

    async def no_brief(*args, **kwargs):
        return "brief"
    monkeypatch.setattr(turn_recorder, "record_brief", no_brief)
    a, b, _ = network(workers, monkeypatch)
    make_home(b, "caller")
    child = started_child(a, b)
    turn = start_child_turn(a, child)
    with b.active():
        set_limits(daily_spend_limit_usd=1)
        book("caller", 2.0)

    async def stop(row):
        await limits.enforce()
        await remote_task.drain_stops()
        assert interactions.get_interaction(row["id"])["state"] == "cancelled"
        assert remote_tasks.get_dispatch(child)["stop_reason"] == "assistant_daily_limit"
        assert limits_db.stopped(turn["id"])
    _, result = round_trip(a, b, proxies, held, ask_kwargs(child), stop)
    assert isinstance(result, PermissionResultDeny)
    assert result.message.startswith(relay.CANCELLED_MESSAGE)
