"""The app messaging roster must include ordinary Task workers, not just Swarm."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fluid.events.ops import AgentMessage
from fluid.swarm import Launch, SwarmCoordinator
from fluid.tools.registry import ToolCtx

from app.core import agent_messaging as messaging
from app.core import session_runtime, swarm, turn_scheduler
from app.core import task_registry as registry_module
from app.core.session_pool import get_session_pool


@pytest.fixture
def runtime(monkeypatch):
    registry = registry_module.TaskRegistry()
    monkeypatch.setattr(registry_module, "task_registry", registry)
    monkeypatch.setattr(swarm, "workers", {})
    monkeypatch.setattr(swarm, "coordinators", {})
    engines = {}
    monkeypatch.setattr(get_session_pool(), "get", engines.get)
    monkeypatch.setattr(session_runtime, "get_live_runtime", engines.get)
    return registry, engines


def worker(runtime, owner, worker_id, name="Helper", roster_name=None):
    registry, engines = runtime
    registry.start_task(owner, worker_id, "agent", "Investigate", agent=name, name=roster_name)
    engine = SimpleNamespace(submit=AsyncMock())
    engines[worker_id] = SimpleNamespace(
        adapter=SimpleNamespace(engine=engine, provider_kind="openai_responses")
    )
    return engine


@pytest.mark.parametrize("address", ["Helper", "worker", "Helper [worker]"])
async def test_task_roster_and_delivery(runtime, address):
    engine = worker(runtime, "parent", "worker")
    rows = await messaging.make_roster(lambda: "parent")({}, ToolCtx())
    assert [(r["name"], r["ref"]) for r in rows] == [("Helper", "worker")]
    result = await messaging.make_message(lambda: "parent")(
        {"to": address, "message": "Check the failure", "sender": "forged"}, ToolCtx()
    )
    assert not result.is_error, result.text
    op = engine.submit.await_args.args[0]
    assert isinstance(op, AgentMessage)
    assert op.sender == "parent" and op.text == "Check the failure"
    assert op.wake is False


async def test_duplicate_names_require_exact_ref_even_beyond_list_limit(runtime):
    engines = [worker(runtime, "parent", f"worker-{i}") for i in range(60)]
    send = messaging.make_message(lambda: "parent")
    assert (await send({"to": "Helper", "message": "hi"}, ToolCtx())).is_error
    result = await send({"to": "Helper [worker-0]", "message": "hi"}, ToolCtx())
    assert not result.is_error, result.text
    engines[0].submit.assert_awaited_once()
    assert len(messaging.peer_rows("parent")) == 60


async def test_nested_main_is_direct_owner_and_unrelated_targets_are_rejected(runtime):
    parent = worker(runtime, "root", "parent", "Parent")
    worker(runtime, "root", "child")  # root's UI-routing copy
    worker(runtime, "parent", "child")  # actual dispatch owner
    stranger = worker(runtime, "elsewhere", "stranger")
    rows = messaging.peer_rows("child")
    assert len(rows) == 1 and rows[0]["worker_session_id"] == "parent"
    send = messaging.make_message(lambda: "child")
    assert not (await send({"to": "main", "message": "report"}, ToolCtx())).is_error
    parent.submit.assert_awaited_once()
    assert (await send({"to": "stranger", "message": "hi"}, ToolCtx())).is_error
    assert (await send({"to": "root", "message": "hi"}, ToolCtx())).is_error
    stranger.submit.assert_not_awaited()
    assert messaging.peer_rows("elsewhere-unrelated") == []


async def test_swarm_scope_and_mixed_roster_deduplicate_worker_identity(runtime):
    worker(runtime, "parent", "ordinary", "a")
    worker(runtime, "parent", "wa", "Swarm agent")
    worker(runtime, "parent", "wb", "Swarm agent")
    worker(runtime, "wa", "nested", "Nested")
    coordinator = SwarmCoordinator("parent", None, None, None, None)

    def row(name, wid):
        return {
            "id": name,
            "title": name,
            "task_id": wid,
            "worker_session_id": wid,
            "status": "running",
        }

    coordinator.swarms = {
        "s1": {"tasks": [row("a", "wa"), row("b", "wb")]},
        "s2": {"tasks": [row("c", "wc")]},
    }
    swarm.coordinators["parent"] = coordinator
    swarm.workers["wa"] = Launch(
        coordinator, "s1", "a", asyncio.get_running_loop().create_future()
    )
    assert {r["name"] for r in messaging.peer_rows("wa")} == {"main", "b"}
    rows = messaging.peer_rows("parent")
    assert len(rows) == 4
    assert {r["worker_session_id"] for r in rows} == {"ordinary", "wa", "wb", "wc"}
    send = messaging.make_message(lambda: "parent")
    assert (await send({"to": "a", "message": "hi"}, ToolCtx())).is_error
    assert not (await send({"to": "a [wa]", "message": "hi"}, ToolCtx())).is_error
    restricted = messaging.make_message(lambda: "wa")
    for address in ["ordinary", "wc", "nested"]:
        assert (await restricted({"to": address, "message": "hi"}, ToolCtx())).is_error


@pytest.mark.parametrize("state", ["completed", "stopped"])
async def test_unavailable_targets_report_errors(runtime, state):
    registry, engines = runtime
    engine = worker(runtime, "parent", "worker")
    if state == "completed":
        registry.complete_task("parent", "worker", "completed")
    else:
        registry.mark_user_stopped("worker")
    result = await messaging.make_message(lambda: "parent")(
        {"to": "Helper", "message": "hi"}, ToolCtx()
    )
    assert result.is_error
    engine.submit.assert_not_awaited()
    if state == "completed":
        assert (
            await messaging.make_roster(lambda: "parent")({"running_only": True}, ToolCtx())
            == []
        )
        assert messaging.peer_rows("parent")[0]["status"] == "completed"


@pytest.mark.parametrize("state", ["completed", "error", "interrupted"])
@pytest.mark.parametrize("kind", ["openai_responses", "delegated"])
async def test_finished_worker_queues_one_message_over_saved_context(runtime, monkeypatch, state, kind):
    from app.core import provider_agent
    from app.db import sessions
    registry, engines = runtime
    engine = worker(runtime, "parent", "worker")
    engines["worker"].adapter.provider_kind = kind
    registry.complete_task("parent", "worker", state)
    monkeypatch.setattr(sessions, "get_session", lambda sid: {"id": sid})
    monkeypatch.setattr(turn_scheduler, "_latest_context_turn", lambda sid: {"id": "previous", "profile_id": "helper"})
    monkeypatch.setattr(provider_agent, "final_turn_outcome", lambda sid: {"error": None})
    monkeypatch.setattr(turn_scheduler, "_turn_terminal_payload", lambda *args: {})
    queue = AsyncMock(return_value="resumed-turn")
    monkeypatch.setattr(turn_scheduler, "on_agent_message", queue)
    result = await messaging.make_message(lambda: "parent")({"to": "Helper", "message": "Continue from the saved result"}, ToolCtx())
    assert not result.is_error, result.text
    assert "Resume queued" in result.text
    queue.assert_awaited_once_with("worker", "parent", "Continue from the saved result", reply_to="parent")
    engine.submit.assert_not_awaited()


async def test_running_delegated_worker_queues_instead_of_using_native_engine(runtime, monkeypatch):
    _, engines = runtime
    engine = worker(runtime, "parent", "worker")
    engines["worker"].adapter.provider_kind = "delegated"
    queue = AsyncMock(return_value="message-turn")
    monkeypatch.setattr(turn_scheduler, "on_agent_message", queue)
    steer = AsyncMock(return_value=1)
    monkeypatch.setattr(session_runtime, "try_steer_pending", steer)
    result = await messaging.make_message(lambda: "parent")({"to": "Helper", "message": "progress?"}, ToolCtx())
    assert not result.is_error
    queue.assert_awaited_once_with("worker", "parent", "progress?")
    engine.submit.assert_not_awaited()
    # The queued row is offered to the running receiver's next tool boundary.
    steer.assert_awaited_once_with("worker")


async def test_held_message_is_not_offered_to_a_live_turn(runtime, monkeypatch):
    worker(runtime, "parent", "worker")
    monkeypatch.setattr(session_runtime, "get_live_runtime", lambda sid: None)
    monkeypatch.setattr(turn_scheduler, "on_agent_message", AsyncMock(return_value=turn_scheduler.HELD))
    steer = AsyncMock()
    monkeypatch.setattr(session_runtime, "try_steer_pending", steer)
    result = await messaging.make_message(lambda: "parent")({"to": "Helper", "message": "hi"}, ToolCtx())
    assert result.text == messaging.held_receipt("Helper")
    steer.assert_not_awaited()


async def test_policy_rejected_worker_is_not_resumed(runtime, monkeypatch):
    from app.core import provider_agent
    from app.db import sessions
    registry, _ = runtime
    worker(runtime, "parent", "worker")
    registry.complete_task("parent", "worker", "error")
    monkeypatch.setattr(sessions, "get_session", lambda sid: {"id": sid})
    monkeypatch.setattr(turn_scheduler, "_latest_context_turn", lambda sid: {"id": "previous", "profile_id": "helper"})
    monkeypatch.setattr(provider_agent, "final_turn_outcome", lambda sid: {"error": "Provider rejected: cyber_policy"})
    monkeypatch.setattr(turn_scheduler, "_turn_terminal_payload", lambda *args: {})
    queue = AsyncMock()
    monkeypatch.setattr(turn_scheduler, "on_agent_message", queue)
    result = await messaging.make_message(lambda: "parent")({"to": "Helper", "message": "try again"}, ToolCtx())
    assert result.is_error and "policy" in result.text
    queue.assert_not_awaited()


@pytest.mark.parametrize("state", ["missing", "closing", "parked"])
async def test_unavailable_live_delivery_queues_without_claiming_delivered(
    runtime, monkeypatch, state
):
    _, engines = runtime
    engine = worker(runtime, "parent", "worker")
    enqueue = AsyncMock(return_value="queued-turn")
    monkeypatch.setattr(turn_scheduler, "on_agent_message", enqueue)
    if state == "missing":
        engines.clear()
    elif state == "parked":
        monkeypatch.setattr(session_runtime, "get_live_runtime", lambda sid: None)
    else:
        engine.submit.side_effect = RuntimeError("receiver is finishing its turn")
    result = await messaging.make_message(lambda: "parent")(
        {"to": "Helper", "message": "hi", "sender": "forged"}, ToolCtx()
    )
    assert not result.is_error
    assert result.text == "Queued for Helper"
    enqueue.assert_awaited_once_with("worker", "parent", "hi")
    if state != "closing":
        engine.submit.assert_not_awaited()


async def test_queue_stop_refusal_is_not_reported_as_delivery(runtime, monkeypatch):
    worker(runtime, "parent", "worker")
    monkeypatch.setattr(session_runtime, "get_live_runtime", lambda sid: None)
    monkeypatch.setattr(turn_scheduler, "on_agent_message", AsyncMock(return_value=None))
    result = await messaging.make_message(lambda: "parent")(
        {"to": "Helper", "message": "hi"}, ToolCtx()
    )
    assert result.is_error
    assert "stopped" in result.text


def test_missing_caller_and_non_agent_tasks_do_not_grant_access(runtime):
    registry, _ = runtime
    registry.start_task("parent", "shell", "shell", "Run tests")
    assert messaging.peer_rows("parent") == []
    assert messaging.peer_rows(None) == []


# ─── round 9: fluid's Task passes a per-task name; SendMessage resolves it ───


@pytest.mark.parametrize("address", ["fix-flaky-login", "Fix-Flaky-Login", "worker-a", "fix-flaky-login [worker-a]"])
async def test_worker_is_listed_and_addressed_by_its_task_name_or_task_id(runtime, address):
    """Every worker used to be listed as its agent TYPE ("General Purpose"),
    so a parent addressing one by what it was doing got "Unknown agent". The
    roster now carries fluid's derived name; the task id always resolves."""
    engine = worker(runtime, "parent", "worker-a", name="General Purpose", roster_name="fix-flaky-login")
    rows = await messaging.make_roster(lambda: "parent")({}, ToolCtx())
    assert [(r["name"], r["task_id"]) for r in rows] == [("fix-flaky-login", "worker-a")]
    result = await messaging.make_message(lambda: "parent")(
        {"to": address, "message": "status?"}, ToolCtx()
    )
    assert not result.is_error, result.text
    assert engine.submit.await_args.args[0].text == "status?"


async def test_two_workers_with_one_name_are_told_apart_by_task_id(runtime):
    worker(runtime, "parent", "worker-a", name="General Purpose", roster_name="review-diff")
    worker(runtime, "parent", "worker-b", name="General Purpose", roster_name="review-diff")
    send = messaging.make_message(lambda: "parent")
    ambiguous = await send({"to": "review-diff", "message": "hi"}, ToolCtx())
    assert ambiguous.is_error
    assert "worker-a" in ambiguous.text and "worker-b" in ambiguous.text
    assert not (await send({"to": "worker-b", "message": "hi"}, ToolCtx())).is_error


def test_dispatch_forwards_the_name_to_the_registry(monkeypatch):
    """The slug rides ``args["name"]`` from fluid, through the dispatcher's
    ``roster_name`` kwarg, into the registry row's ``name`` (and its wire)."""
    from app.core import task_registry as registry_module

    registry = registry_module.TaskRegistry()
    registry.start_task("parent", "w1", "agent", "Task · General Purpose",
                        agent="General Purpose", name="fix-flaky-login")
    wire = registry_module.to_wire(registry.get_task("parent", "w1"))
    assert wire["agent"] == "General Purpose" and wire["name"] == "fix-flaky-login"
    # Re-registration never clobbers a name already recorded.
    registry.start_task("parent", "w1", "agent", "Task · General Purpose", name="other")
    assert registry.get_task("parent", "w1")["name"] == "fix-flaky-login"


# ─── owner decision 2026-10-01: a delivery names the worker by its task ───


def test_sender_label_is_the_workers_task_description(runtime):
    registry, _ = runtime
    registry.start_task("parent", "worker-a", "agent", "Fix flaky login",
                        agent="General Purpose", name="fix-flaky-login")
    registry.start_task("parent", "worker-b", "agent", "", agent="Reviewer")
    assert messaging.sender_label("parent", "worker-a") == "Fix flaky login"
    # No description: the roster name the worker is addressed by.
    assert messaging.sender_label("parent", "worker-b") == "Reviewer"
    # The worker's view of its parent.
    assert messaging.sender_label("worker-a", "parent") == "main"
    # Unknown to the receiver: no label, so the chat shows the session id.
    assert messaging.sender_label("parent", "stranger") is None
    assert messaging.sender_label(None, "worker-a") is None
