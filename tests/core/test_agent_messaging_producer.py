"""Messaging consumes the registry rows real Task worker dispatches produce.

Reuse the provider-agent transport/turn doubles, not a fabricated task registry:
workers stay inside dispatch_worker while ListAgents and SendMessage execute.
Only the native receiver's submit method is mocked on the messaging path.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fluid.events.ops import AgentMessage
from fluid.tools.builtins import list_agents, send_message
from fluid.tools.registry import ToolCtx

from app.core import agent_messaging as messaging
from app.core import (
    provider_agent,
    provider_agent_approvals,
    provider_agent_supervisor,
    swarm,
    turn_manager,
)
from app.core.session_pool import SessionState, get_session_pool
from app.core.task_registry import task_registry
from app.core.turn_scheduler import on_task_terminal as _real_task_terminal
from tests.core import test_provider_agent

dispatch_env = test_provider_agent.dispatch_env


class _RunningWorkers:
    def __init__(self):
        self.started = {}
        self.release = {}
        self.jobs = {}
        self.engines = {}

    def receiver(self, session_id):
        engine = SimpleNamespace(submit=AsyncMock())
        get_session_pool().put(
            session_id,
            SessionState(
                adapter=SimpleNamespace(
                    engine=engine, provider_kind="openai_responses"
                ),
                is_connected=True,
                is_streaming=True,
            ),
        )
        self.engines[session_id] = engine
        return engine

    async def wait_session_idle(self, session_id, timeout=300.0):
        self.started[session_id].set()
        await self.release[session_id].wait()
        return True

    async def start(self, worker_id, *, root="root", owner=None, name="Helper"):
        self.started[worker_id] = asyncio.Event()
        self.release[worker_id] = asyncio.Event()
        engine = self.receiver(worker_id)
        self.jobs[worker_id] = asyncio.create_task(
            provider_agent.dispatch_worker(
                parent_session_id=root,
                context_session_id=owner,
                worker_session_id=worker_id,
                profile_id="default",
                provider_id="prov-openai",
                model_id="gpt-5.5",
                prompt="Investigate the messaging regression",
                label=f"Investigate with {name}",
                agent_name=name,
                background=True,
            )
        )
        await asyncio.wait_for(self.started[worker_id].wait(), timeout=10)
        assert not self.jobs[worker_id].done(), self.jobs[worker_id].result()
        # No registry rows are seeded by this fixture. These must be produced
        # before the real dispatch starts waiting for the worker turn.
        row = next(r for r in task_registry.get_all(root) if r["id"] == worker_id)
        assert row["kind"] == "agent" and row["status"] == "running"
        assert row["agent"] == name
        return engine

    async def finish(self, worker_id):
        self.release[worker_id].set()
        outcome = await asyncio.wait_for(self.jobs[worker_id], timeout=10)
        assert outcome.ok, outcome.error
        return outcome


@pytest_asyncio.fixture
async def workers(dispatch_env, monkeypatch):
    provider_agent.reset_worker_sessions()
    provider_agent_supervisor.reset()
    provider_agent_approvals.reset_routes()
    monkeypatch.setattr(swarm, "workers", {})
    monkeypatch.setattr(swarm, "coordinators", {})
    monkeypatch.setattr(swarm, "worker_jobs", {})
    runtime = _RunningWorkers()
    from app.core import session_runtime

    monkeypatch.setattr(session_runtime, "get_live_runtime", runtime.engines.get)
    monkeypatch.setattr(turn_manager, "wait_session_idle", runtime.wait_session_idle)
    try:
        yield runtime
    finally:
        # Finish descendants before their owners so the real background-work
        # drain has nothing outstanding. Never leave worker coroutines running.
        try:
            for worker_id in reversed(runtime.jobs):
                await runtime.finish(worker_id)
        finally:
            for job in runtime.jobs.values():
                if not job.done():
                    job.cancel()
            await asyncio.gather(*runtime.jobs.values(), return_exceptions=True)
            provider_agent.reset_worker_sessions()
            provider_agent_supervisor.reset()
            provider_agent_approvals.reset_routes()


def _ctx(session_id):
    return ToolCtx(
        session_id=session_id,
        extra={
            "agent_roster": messaging.make_roster(lambda: session_id),
            "agent_message": messaging.make_message(lambda: session_id),
        },
    )


async def _rows(session_id, *, running_only=False):
    result = await list_agents.handler({"running_only": running_only}, _ctx(session_id))
    assert not result.is_error, result.text
    return result.ui["agents"]


async def _send(ctx, address, receiver, *, sender):
    before = receiver.submit.await_count
    result = await send_message.handler(
        {
            "to": address,
            "message": "Check the failing path",
            "sender": "forged",
        },
        ctx,
    )
    assert not result.is_error, result.text
    assert receiver.submit.await_count == before + 1
    op = receiver.submit.await_args.args[0]
    assert isinstance(op, AgentMessage)
    assert op.sender == sender
    assert op.text == "Check the failing path"
    assert op.wake is False


@pytest.mark.asyncio
@pytest.mark.parametrize("address_kind", ["name", "id", "displayed_ref"])
async def test_real_task_worker_is_listed_and_messageable_while_running(
    workers, address_kind
):
    parent = workers.receiver("root")
    receiver = await workers.start("ordinary-worker", name="Research")
    rows = await _rows("root", running_only=True)
    assert [(r["name"], r["ref"], r["worker_session_id"]) for r in rows] == [
        ("Research", "ordinary-worker", "ordinary-worker"),
    ]
    row = rows[0]
    address = {
        "name": row["name"],
        "id": row["worker_session_id"],
        "displayed_ref": f"{row['name']} [{row['ref']}]",
    }[address_kind]
    await _send(_ctx("root"), address, receiver, sender="root")
    child_rows = await _rows("ordinary-worker", running_only=True)
    assert [(r["name"], r["worker_session_id"]) for r in child_rows] == [
        ("main", "root")
    ]
    await _send(_ctx("ordinary-worker"), "main", parent, sender="ordinary-worker")
    assert not workers.jobs["ordinary-worker"].done()


@pytest.mark.asyncio
async def test_real_nested_dispatch_resolves_main_to_owner_not_routing_root(workers):
    root_receiver = workers.receiver("root")
    owner = await workers.start("owner-worker", name="Owner")
    nested = await workers.start("nested-worker", owner="owner-worker", name="Nested")
    stranger = await workers.start(
        "unrelated-worker", root="elsewhere", name="Stranger"
    )
    # dispatch_worker writes the UI-routing copy first, and the owning copy
    # second. The first owner in this index is NOT the parent to message.
    assert task_registry.sessions_with_task("nested-worker") == ["root", "owner-worker"]
    assert {r["worker_session_id"] for r in await _rows("root")} == {
        "owner-worker",
        "nested-worker",
    }
    assert {r["worker_session_id"] for r in await _rows("owner-worker")} == {
        "root",
        "nested-worker",
    }
    assert [
        (r["name"], r["worker_session_id"]) for r in await _rows("nested-worker")
    ] == [
        ("main", "owner-worker"),
    ]
    await _send(_ctx("nested-worker"), "main", owner, sender="nested-worker")
    await _send(_ctx("owner-worker"), "Nested", nested, sender="owner-worker")
    await _send(_ctx("root"), "Nested [nested-worker]", nested, sender="root")
    for address in ["root", "unrelated-worker", "Stranger [unrelated-worker]"]:
        denied = await send_message.handler(
            {"to": address, "message": "No access"}, _ctx("nested-worker")
        )
        assert denied.is_error
    root_receiver.submit.assert_not_awaited()
    stranger.submit.assert_not_awaited()
    assert all(not job.done() for job in workers.jobs.values())


@pytest.mark.asyncio
async def test_real_same_named_dispatches_require_the_listed_ref(workers):
    first = await workers.start("worker-first", name="Explore")
    second = await workers.start("worker-second", name="Explore")
    rows = await _rows("root")
    assert len(rows) == 2 and {r["name"] for r in rows} == {"Explore"}
    denied = await send_message.handler(
        {"to": "Explore", "message": "Ambiguous"}, _ctx("root")
    )
    assert denied.is_error
    first.submit.assert_not_awaited()
    second.submit.assert_not_awaited()
    for row in rows:
        receiver = workers.engines[row["worker_session_id"]]
        await _send(
            _ctx("root"), f"{row['name']} [{row['ref']}]", receiver, sender="root"
        )
        await _send(_ctx("root"), row["worker_session_id"], receiver, sender="root")


@pytest.mark.asyncio
async def test_real_completion_remains_listed_but_is_not_running(workers):
    receiver = await workers.start("finished-worker")
    await workers.finish("finished-worker")
    rows = await _rows("root")
    assert [(r["ref"], r["status"]) for r in rows] == [("finished-worker", "completed")]
    assert await _rows("root", running_only=True) == []
    result = await send_message.handler(
        {"to": "Helper", "message": "Too late"}, _ctx("root")
    )
    assert result.is_error
    receiver.submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_adapter_injection_resolves_app_id_after_connect(
    workers, monkeypatch, tmp_path
):
    from app.core.provider import harness_adapter
    from app.core.provider.types import SessionOptions
    from app.db import database
    from tests.core.test_harness_adapter_session_ctx import (
        _CtxLoop,
        _fake_provider_client,
    )

    monkeypatch.setattr(harness_adapter, "NativeLoop", _CtxLoop)
    monkeypatch.setattr(
        harness_adapter, "_build_provider_client", _fake_provider_client
    )
    mapped = {}
    monkeypatch.setattr(database, "get_session_by_sdk_id", mapped.get)
    adapter = harness_adapter.HarnessAdapter()
    await adapter.connect(
        SessionOptions(
            model="gpt-4o",
            cwd=str(tmp_path),
            extra={"fluid_provider": "openai_responses"},
        )
    )
    ctx = _CtxLoop.captured["ctx"]
    assert ctx.session_id != "late-root"
    assert adapter._resolve_app_session_id() is None
    assert callable(ctx.extra.get("agent_roster"))
    assert callable(ctx.extra.get("agent_message"))
    before = await list_agents.handler({}, ctx)
    assert not before.is_error and before.ui["agents"] == []

    # The real worker registration belongs to the app ID, not the fluid ID.
    receiver = await workers.start("late-worker", root="late-root", name="Late helper")
    mapped[adapter._sdk_session_id] = {"id": "late-root"}
    listed = await list_agents.handler({}, ctx)
    assert not listed.is_error, listed.text
    assert [(r["name"], r["ref"]) for r in listed.ui["agents"]] == [
        ("Late helper", "late-worker"),
    ]
    await _send(ctx, "Late helper [late-worker]", receiver, sender="late-root")
    assert not workers.jobs["late-worker"].done()


@pytest.mark.asyncio
async def test_real_swarm_binding_keeps_worker_scope_and_deduplicates_parent(workers):
    from fluid.swarm import SwarmCoordinator, current_launch
    from fluid.tools.registry import BackgroundTaskReceipt, ToolOutcome

    workers.receiver("root")
    ordinary = await workers.start("ordinary-worker", name="a")
    ready = {name: asyncio.Event() for name in ("a", "b", "other")}

    async def discard(*args):
        pass

    async def dispatch(data, ctx):
        # Let the coordinator produce Launch, then use the same host binding
        # and real worker registration as Task's admitted dispatch attempt.
        launch = current_launch.get()
        worker_id = f"swarm-{launch.task_id}"
        await swarm.bind_worker(worker_id, "root")
        await workers.start(worker_id, name="Swarm agent")
        swarm.register_job(worker_id, workers.jobs[worker_id], "root")
        ready[launch.task_id].set()
        return ToolOutcome(
            text="Started",
            background_task=BackgroundTaskReceipt(
                task_id=worker_id,
                worker_session_id=worker_id,
            ),
        )

    coordinator = SwarmCoordinator(
        "root", dispatch, swarm.cancel_worker, discard, discard
    )
    swarm.coordinators["root"] = coordinator
    try:
        for names in [("a", "b"), ("other",)]:
            await coordinator.start(
                {
                    "tasks": [
                        {"id": name, "agent": "helper", "prompt": "Investigate"}
                        for name in names
                    ]
                },
                ToolCtx(),
            )
        await asyncio.wait_for(
            asyncio.gather(*(event.wait() for event in ready.values())), timeout=10
        )
        # An ordinary child of a swarm worker is genuinely registered under
        # that worker, but this must not widen its explicit workflow roster.
        nested = await workers.start("nested-worker", owner="swarm-a", name="Nested")
        rows = await _rows("root")
        assert {r["worker_session_id"] for r in rows} == {
            "ordinary-worker",
            "swarm-a",
            "swarm-b",
            "swarm-other",
            "nested-worker",
        }
        assert len(rows) == 5
        assert [(r["name"], r["worker_session_id"]) for r in rows[:3]] == [
            ("a", "swarm-a"),
            ("b", "swarm-b"),
            ("other", "swarm-other"),
        ]
        assert await _rows("swarm-a") == swarm.peer_rows("swarm-a")
        assert {r["worker_session_id"] for r in await _rows("swarm-a")} == {
            "root",
            "swarm-b",
        }
        denied = await send_message.handler(
            {"to": "a", "message": "Ambiguous"}, _ctx("root")
        )
        assert denied.is_error
        await _send(
            _ctx("root"), "a [swarm-a]", workers.engines["swarm-a"], sender="root"
        )
        await _send(_ctx("swarm-a"), "b", workers.engines["swarm-b"], sender="swarm-a")
        await _send(_ctx("swarm-a"), "main", workers.engines["root"], sender="swarm-a")
        for address in ["ordinary-worker", "swarm-other", "nested-worker"]:
            denied = await send_message.handler(
                {"to": address, "message": "No access"}, _ctx("swarm-a")
            )
            assert denied.is_error
        ordinary.submit.assert_not_awaited()
        nested.submit.assert_not_awaited()
        workers.engines["swarm-other"].submit.assert_not_awaited()
    finally:
        # Resolve actual worker outcomes and the coordinator's completion
        # futures; a successful test must leave no background dispatch behind.
        for worker_id in reversed(workers.jobs):
            await workers.finish(worker_id)
        await asyncio.wait_for(asyncio.gather(*coordinator.jobs.values()), timeout=10)


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_present", [False, True])
async def test_worker_message_to_idle_parent_uses_owned_turn_queue(
    workers, dispatch_env, monkeypatch, runtime_present
):
    """A real Task sends main a message, then completes: neither may bypass
    app admission, including the setup window where a runtime has no engine turn.
    """
    from fluid.engine.native import NativeLoop
    from fluid.events.ops import UserTurn
    from fluid.providers.base import ProviderCaps, StreamChunk
    from fluid.store.base import MemoryStore

    from app.core import session_runtime, turn_scheduler
    from app.db import turns as turns_db

    class Provider:
        name = "fake"
        caps = ProviderCaps()

        def __init__(self):
            self.calls = 0

        async def stream(self, request):
            self.calls += 1
            yield StreamChunk("text_delta", text="Acknowledged")
            yield StreamChunk("done", stop_reason="end_turn")

    provider = Provider()
    engine = NativeLoop(provider=provider, store=MemoryStore(), model="fake")
    workers.receiver("root")
    get_session_pool().get("root").adapter.engine = engine
    await workers.start("reporting-worker")
    monkeypatch.setattr(
        session_runtime, "get_live_runtime",
        lambda sid: object() if runtime_present else None,
    )
    kicks = []
    monkeypatch.setattr(turn_scheduler, "kick", kicks.append)
    context = turns_db.enqueue_turn(
        "root", "user", prompt="Delegate and wait", profile_id="profile",
        project_id="project", worktree_id="worktree",
        overrides={"model": "fake", "permission_mode": "default"},
        payload={"api_user_id": "owner"},
    )
    turns_db.start_turn(context["id"], None, "runner")
    turns_db.park_turn(context["id"], "background_tasks")
    try:
        result = await send_message.handler(
            {"to": "main", "message": "Progress before completion"},
            _ctx("reporting-worker"),
        )
        assert not result.is_error, result.text
        assert engine._turn_task is None, "SendMessage started an unowned engine turn"
        (message_turn,) = turns_db.get_queued_turns("root")
        assert message_turn["source"] == "agent_message"
        assert "Progress before completion" in message_turn["prompt"]
        assert "from='reporting-worker'" in message_turn["prompt"]
        assert message_turn["profile_id"] == "profile"
        assert message_turn["project_id"] == "project"
        assert message_turn["worktree_id"] == "worktree"
        assert message_turn["overrides"] == context["overrides"]
        assert message_turn["payload"]["api_user_id"] == "owner"
        assert message_turn["payload"]["task_ids"] == []
        assert kicks == ["root"]

        # dispatch_worker produces the real terminal frame. Feed that to the
        # real scheduler (dispatch_env captured it rather than auto-running it).
        await workers.finish("reporting-worker")
        # The fixture replaces the module attribute; use the import-time alias.
        await _real_task_terminal(*dispatch_env.wakes[-1])
        queued = turns_db.get_queued_turns("root")
        assert [row["source"] for row in queued] == ["agent_message", "unpark"]
        assert queued[1]["payload"]["task_ids"] == ["reporting-worker"]
        for row in queued:
            assert turns_db.start_turn(row["id"], None, "runner")
            await engine.submit(UserTurn(content=row["prompt"]))
            await engine.wait_turn()
            turns_db.settle_turn(row["id"], "completed")
        assert provider.calls == 2
        assert turns_db.get_queued_turns("root") == []
    finally:
        await engine.aclose()
