"""The app messaging roster must include ordinary Task workers, not just Swarm."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fluid.events.ops import AgentMessage
from fluid.swarm import Launch, SwarmCoordinator
from fluid.tools.registry import ToolCtx

from app.core import agent_messaging as messaging
from app.core import swarm
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
    return registry, engines


def worker(runtime, owner, worker_id, name="Helper"):
    registry, engines = runtime
    registry.start_task(owner, worker_id, "agent", "Investigate", agent=name)
    engine = SimpleNamespace(submit=AsyncMock())
    engines[worker_id] = SimpleNamespace(
        adapter=SimpleNamespace(engine=engine, provider_kind="openai_responses")
    )
    return engine


@pytest.mark.parametrize("address", ["Helper", "worker", "Helper [worker]"])
async def test_task_roster_and_delivery(runtime, address):
    engine = worker(runtime, "parent", "worker")
    rows = messaging.make_roster(lambda: "parent")({}, ToolCtx())
    assert [(r["name"], r["ref"]) for r in rows] == [("Helper", "worker")]
    result = await messaging.make_message(lambda: "parent")(
        {"to": address, "message": "Check the failure", "sender": "forged"}, ToolCtx()
    )
    assert not result.is_error, result.text
    op = engine.submit.await_args.args[0]
    assert isinstance(op, AgentMessage)
    assert op.sender == "parent" and op.text == "Check the failure"


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


@pytest.mark.parametrize("state", ["completed", "missing", "delegated", "closing"])
async def test_unavailable_targets_report_errors(runtime, state):
    registry, engines = runtime
    engine = worker(runtime, "parent", "worker")
    if state == "completed":
        registry.complete_task("parent", "worker", "completed")
    elif state == "missing":
        engines.clear()
    elif state == "delegated":
        engines["worker"].adapter.provider_kind = "delegated"
    else:
        engine.submit.side_effect = RuntimeError("engine closed")
    result = await messaging.make_message(lambda: "parent")(
        {"to": "Helper", "message": "hi"}, ToolCtx()
    )
    assert result.is_error
    if state != "closing":
        engine.submit.assert_not_awaited()
    if state == "completed":
        assert (
            messaging.make_roster(lambda: "parent")({"running_only": True}, ToolCtx())
            == []
        )
        assert messaging.peer_rows("parent")[0]["status"] == "completed"


def test_missing_caller_and_non_agent_tasks_do_not_grant_access(runtime):
    registry, _ = runtime
    registry.start_task("parent", "shell", "shell", "Run tests")
    assert messaging.peer_rows("parent") == []
    assert messaging.peer_rows(None) == []
