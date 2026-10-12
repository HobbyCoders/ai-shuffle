"""Messaging consumes the registry rows real Task worker dispatches produce.

Reuse the provider-agent transport/turn doubles, not a fabricated task registry:
workers stay inside dispatch_worker while ListAgents and SendMessage execute.
Only the native receiver's submit method is mocked on the messaging path.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

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
from app.core.turn_manager import interrupt_async as _real_interrupt_async
from app.core.session_runtime import get_live_runtime as _real_get_live_runtime
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


class _RecordingProvider:
    """A one-reply provider that keeps each request's final user text."""

    name = "fake"

    def __init__(self):
        from fluid.providers.base import ProviderCaps

        self.caps = ProviderCaps()
        self.calls = 0
        self.prompts = []

    async def stream(self, request):
        from fluid.providers.base import StreamChunk

        self.calls += 1
        self.prompts.append(request.messages[-1].text())
        yield StreamChunk("text_delta", text="Acknowledged")
        yield StreamChunk("done", stop_reason="end_turn")


async def _parked_root(workers, monkeypatch, runtime_present):
    """``root`` delegated to a real Task worker and parked on it."""
    from fluid.engine.native import NativeLoop
    from fluid.store.base import MemoryStore

    from app.core import session_runtime, turn_scheduler
    from app.db import turns as turns_db

    provider = _RecordingProvider()
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
    return engine, provider, kicks, context


def _held_rows(session_id):
    from app.core import wakeup_service
    from app.db import wakeups as wakeups_db

    return wakeups_db.get_session_wakeups(session_id, wakeup_service.HELD_PEER_SOURCE)


async def _message_main(text, *, urgent=False):
    payload = {"to": "main", "message": text}
    if urgent:
        payload["urgent"] = True
    result = await send_message.handler(payload, _ctx("reporting-worker"))
    assert not result.is_error, result.text
    return result


async def _run_queued(engine, session_id):
    from fluid.events.ops import UserTurn

    from app.db import turns as turns_db

    ran = []
    for row in turns_db.get_queued_turns(session_id):
        assert turns_db.start_turn(row["id"], None, "runner")
        await engine.submit(UserTurn(content=row["prompt"]))
        await engine.wait_turn()
        turns_db.settle_turn(row["id"], "completed")
        ran.append(row)
    return ran


@pytest_asyncio.fixture
async def _no_wake_timers():
    from app.core import turn_scheduler, wakeup_service

    yield
    # Terminal delivery also starts a scheduler batch-window timer. Leaving it
    # alive lets it call the real kick after monkeypatch restores the mock, or
    # leaves a closed-loop task for a later runtime test to gather.
    tasks = set(wakeup_service._timers.values()) | set(turn_scheduler._kick_tasks)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    wakeup_service._timers.clear()
    turn_scheduler._batch_kick_tasks.clear()


async def test_wake_timer_fixture_drains_scheduler_batch_tasks(monkeypatch):
    from app.core import turn_scheduler, wakeup_service

    monkeypatch.setattr(turn_scheduler, "TERMINAL_BATCH_WINDOW_SECONDS", 60)
    kick = MagicMock()
    monkeypatch.setattr(turn_scheduler, "kick", kick)
    cleanup = _no_wake_timers.__wrapped__()
    await anext(cleanup)
    turn_scheduler._kick_after_batch_window("fixture-cleanup")
    batch_task = turn_scheduler._batch_kick_tasks["fixture-cleanup"]
    wake_task = asyncio.create_task(asyncio.sleep(60))
    wakeup_service._timers["fixture-cleanup"] = wake_task
    # Enter the tasks so cancellation must unwind their awaited work/finally.
    await asyncio.sleep(0)
    try:
        with pytest.raises(StopAsyncIteration):
            await anext(cleanup)
        assert batch_task.cancelled()
        assert wake_task.cancelled()
        assert batch_task not in turn_scheduler._kick_tasks
        assert "fixture-cleanup" not in turn_scheduler._batch_kick_tasks
        assert "fixture-cleanup" not in wakeup_service._timers
        kick.assert_not_called()
    finally:
        batch_task.cancel()
        wake_task.cancel()
        await asyncio.gather(batch_task, wake_task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_present", [False, True])
async def test_worker_message_to_idle_parent_uses_owned_turn_queue(
    workers, dispatch_env, monkeypatch, runtime_present, _no_wake_timers,
):
    """A real Task sends its parked parent a progress message, then completes.

    The message must not wake the parent: it is held, the sender is told so,
    and the parent reads it once, in the unpark wake the completion brings.
    ``runtime_present=False`` covers the scheduler fallback; True covers a
    live engine, which the app does not offer an idle-time message to.
    """
    from app.db import turns as turns_db

    engine, provider, kicks, _context = await _parked_root(
        workers, monkeypatch, runtime_present,
    )
    try:
        result = await _message_main("Progress before completion")
        assert result.text == messaging.held_receipt("main")
        assert engine._turn_task is None, "SendMessage started an unowned engine turn"
        assert turns_db.get_queued_turns("root") == []
        assert kicks == []
        (held,) = _held_rows("root")
        assert "Progress before completion" in held["prompt"]
        assert "from='reporting-worker'" in held["prompt"]
        assert held["profile_id"] == "profile"

        # dispatch_worker produces the real terminal frame. Feed that to the
        # real scheduler (dispatch_env captured it rather than auto-running it).
        await workers.finish("reporting-worker")
        # The fixture replaces the module attribute; use the import-time alias.
        await _real_task_terminal(*dispatch_env.wakes[-1])
        (wake,) = turns_db.get_queued_turns("root")
        assert wake["source"] == "unpark"
        assert wake["payload"]["task_ids"] == ["reporting-worker"]
        assert "Progress before completion" in wake["payload"]["held_peer_messages"]
        assert _held_rows("root") == []

        await _run_queued(engine, "root")
        assert provider.calls == 1
        (request_text,) = provider.prompts
        reminder = request_text[request_text.index("<system-reminder>"):]
        assert "Progress before completion" in reminder
        assert request_text.index("<task-notification>") < request_text.index(
            "Progress before completion"
        )
        assert turns_db.get_queued_turns("root") == []
    finally:
        await engine.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_present", [False, True])
async def test_urgent_worker_message_wakes_a_parked_parent_now(
    workers, monkeypatch, runtime_present, _no_wake_timers,
):
    """Urgent keeps the wake, and takes any held message along ahead of it."""
    from app.db import turns as turns_db

    engine, provider, kicks, context = await _parked_root(
        workers, monkeypatch, runtime_present,
    )
    try:
        await _message_main("Interim note")
        assert kicks == []
        result = await _message_main("Blocked: may I edit control.py?", urgent=True)
        assert result.text == "Queued for main"
        assert kicks == ["root"]
        (turn,) = turns_db.get_queued_turns("root")
        assert turn["source"] == "agent_message"
        assert turn["overrides"] == context["overrides"]
        assert turn["prompt"].index("Interim note") < turn["prompt"].index("Blocked:")
        assert _held_rows("root") == []

        await _run_queued(engine, "root")
        assert provider.calls == 1
        assert "Interim note" in provider.prompts[0]
    finally:
        await engine.aclose()


def test_held_receipt_tells_the_sender_not_to_wait():
    text = messaging.held_receipt("main")
    assert text.startswith("Held for main until it wakes")
    assert "Do not wait for a reply" in text
    assert "urgent=true" in text
    assert "Delivered" not in text


@pytest.mark.asyncio
async def test_held_messages_are_promoted_durably_across_a_restart(
    workers, monkeypatch, _no_wake_timers,
):
    """The promotion deadline is a pending_wakeups row: it outlives the
    process, keeps the first message's deadline, and fires as one
    ``promotion`` turn carrying every held message."""
    from app.core import turn_scheduler, wakeup_service
    from app.db import turns as turns_db

    engine, provider, kicks, context = await _parked_root(workers, monkeypatch, False)
    try:
        await _message_main("First interim note")
        (first,) = _held_rows("root")
        await _message_main("Second interim note")
        (held,) = _held_rows("root")
        assert held["fire_at"] == pytest.approx(first["fire_at"], abs=1.0)
        assert held["fire_at"] - first["fire_at"] < 1.0
        assert held["prompt"].index("First") < held["prompt"].index("Second")

        # Restart: every in-process timer is gone; boot rehydrate re-arms
        # the row from the database.
        for task in list(wakeup_service._timers.values()):
            task.cancel()
        wakeup_service._timers.clear()
        armed = []
        monkeypatch.setattr(wakeup_service, "_arm", lambda row, delay: armed.append(row))
        assert await wakeup_service.rehydrate_pending() == 1
        (row,) = armed
        assert row["source"] == wakeup_service.HELD_PEER_SOURCE

        await wakeup_service._fire(row)
        (turn,) = turns_db.get_queued_turns("root")
        assert turn["source"] == "promotion"
        assert turn["overrides"] == context["overrides"]
        assert turn["payload"]["api_user_id"] == "owner"
        assert "First interim note" in turn["prompt"]
        assert "Second interim note" in turn["prompt"]
        assert turn["prompt"].startswith("<system-reminder>")
        assert kicks == ["root"]
        assert _held_rows("root") == []
        assert turn_scheduler.HELD_PEER_PROMOTION_SECONDS > 0

        await _run_queued(engine, "root")
        assert provider.calls == 1
    finally:
        await engine.aclose()


@pytest.mark.asyncio
async def test_original_dispatch_cleanup_cannot_drop_new_continuation_owner(
    workers, monkeypatch,
):
    from app.core import turn_scheduler
    from app.core.query_session_lifecycle import stop_session_task
    from app.db import turns as turns_db

    await workers.start("racing-child")
    context = turns_db.enqueue_turn("racing-child", "user", profile_id="profile")
    turns_db.start_turn(context["id"], None, "runner")
    turns_db.settle_turn(context["id"], "completed")
    monkeypatch.setattr(turn_scheduler, "kick", lambda sid: None)

    async def resume_before_dispatch_finally(owner, frame):
        await turn_scheduler.on_agent_message(
            "racing-child", owner, "Continue", reply_to=owner,
        )

    monkeypatch.setattr(turn_scheduler, "on_task_terminal", resume_before_dispatch_finally)
    await workers.finish("racing-child")
    record = provider_agent_supervisor.get_worker("root", "racing-child")
    assert record is not None and record.continuation_turn_id
    assert task_registry.get_task("root", "racing-child")["status"] == "running"
    await stop_session_task("root", "racing-child")
    assert provider_agent_supervisor.get_worker("root", "racing-child") is None
    assert turns_db.get_queued_turns("racing-child") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", [
    "completed", "error", "stop_task", "stop_all", "queued_stop", "parked_report",
    *[f"overlap_{first}_{stop}" for first in ("completed", "error", "parked")
      for stop in ("stop_task", "stop_all", "queued_stop", "completed", "error", "parked_report", "enqueue_race")],
])
async def test_dispatched_worker_native_continuation_lifecycle(
    workers, dispatch_env, monkeypatch, ending,
):
    """Real dispatch registration -> SendMessage -> runtime/recorder -> native
    adapter -> stop/report. Only provider I/O and the query setup pump are doubles.
    """
    from app.core import query_engine, session_runtime, turn_scheduler
    from app.core.provider.harness_adapter import HarnessAdapter
    from app.core.provider.types import SessionOptions
    from app.core.query_session_lifecycle import stop_session_task, stop_all_background_tasks
    from app.db import turns as turns_db
    from fluid.engine.native import NativeLoop
    from fluid.providers.base import ProviderCaps, StreamChunk
    from fluid.store.base import MemoryStore
    from app.core.provider.events import AssistantMessage, TextBlock, ResultMessage

    overlap = ending.startswith("overlap_")
    first_ending, stop_kind = ending.split("_", 2)[1:] if overlap else (None, ending)
    await workers.start("native-child")
    await workers.finish("native-child")
    assert provider_agent_supervisor.get_worker("root", "native-child") is None
    monkeypatch.setattr(turn_scheduler, "on_task_terminal", _real_task_terminal)
    monkeypatch.setattr(turn_scheduler, "kick", lambda sid: None)
    monkeypatch.setattr(turn_scheduler, "_kick_after_batch_window", lambda sid: None)
    monkeypatch.setattr(turn_manager, "interrupt_async", _real_interrupt_async)
    monkeypatch.setattr(session_runtime, "get_live_runtime", _real_get_live_runtime)
    monkeypatch.setattr(session_runtime, "start_idle_reader", lambda sid: None)
    monkeypatch.setattr("app.core.turn_finalize.persist_turn_end_state", AsyncMock())
    from app.db import database, sessions
    sessions.create_session(session_id="native-child", profile_id=None)
    sessions.create_session(session_id="root", profile_id=None)
    get_session = database.get_session
    monkeypatch.setattr(database, "get_session", lambda sid: {
        **get_session(sid), "instance_id": None,
    })

    class Provider:
        name = "fake"
        caps = ProviderCaps()

        def __init__(self):
            self.requests = []
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def stream(self, request):
            self.requests.append(request)
            if len(self.requests) == 2:
                self.started.set()
                if overlap and first_ending == "error":
                    raise RuntimeError("first overlapping continuation failed")
                if overlap and first_ending == "parked":
                    await workers.start("native-grandchild", root="native-child")
                if ending in {"stop_task", "stop_all"}:
                    try:
                        await asyncio.Event().wait()
                    finally:
                        self.cancelled.set()
                if ending == "error":
                    raise RuntimeError("continuation transport failed")
                if ending == "parked_report":
                    await workers.start("native-grandchild", root="native-child")
            if overlap and len(self.requests) == 3 and stop_kind in {"stop_task", "stop_all", "enqueue_race"}:
                self.started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    self.cancelled.set()
            if overlap and len(self.requests) == 4:
                if stop_kind == "error":
                    raise RuntimeError("last overlapping continuation failed")
                if stop_kind == "parked_report":
                    await workers.start("last-grandchild", root="native-child")
            yield StreamChunk("block_start", {"index": 0, "block_type": "text"})
            yield StreamChunk("text_delta", {"index": 0, "text": (
                "original native report" if len(self.requests) == 1 else "new native report"
            )})
            yield StreamChunk("block_stop", {"index": 0})
            yield StreamChunk("usage", {"usage": {}, "stop_reason": "end_turn"})

    provider = Provider()
    engine = NativeLoop(provider=provider, store=MemoryStore(), model="fake")
    adapter = HarnessAdapter()
    adapter._engine = engine
    adapter._options = SessionOptions(model="fake")
    await engine._ensure_session()
    adapter._connected = True
    state = SessionState(adapter=adapter, is_connected=True)
    get_session_pool().put("native-child", state)

    async def pump(prompt, **kwargs):
        await adapter.send_query(prompt)
        async for message in adapter.receive_events():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        yield {"type": "chunk", "content": block.text}
            elif isinstance(message, ResultMessage):
                if state.interrupt_requested or message.terminal_reason == "aborted_streaming":
                    yield {"type": "interrupted", "message": "Stopped"}
                elif message.is_error:
                    yield {"type": "error", "message": message.result or "native error"}
                else:
                    yield {"type": "done", "metadata": {"stop_reason": "end_turn"}}

    monkeypatch.setattr(query_engine, "_stream_to_websocket_impl", pump)
    session_runtime.register_scheduler(turn_scheduler.on_turn_settled)
    executor_jobs = []

    async def interrupt(sid, mode, turn_id=None):
        runtime = _real_get_live_runtime(sid)
        if runtime:
            await runtime.interrupt(mode)

    def executor(*args):
        job = asyncio.create_task(interrupt(*args))
        executor_jobs.append(job)
        return job

    turn_manager.register_interrupt_executor(executor)
    job = None
    try:
        original = turns_db.enqueue_turn("native-child", "user", prompt="Remember original work", profile_id="profile")
        assert await session_runtime.execute(original) == "completed"
        if overlap:
            # Both callers must finish REAL admission against the completed row
            # before either enqueue rearms it. No consumer-only registry seeds.
            admission = messaging.resume_unavailable
            admitted = 0
            barrier = asyncio.Event()

            async def admit(*args):
                nonlocal admitted
                error = await admission(*args)
                assert error is None
                admitted += 1
                if admitted == 2:
                    barrier.set()
                await asyncio.wait_for(barrier.wait(), 10)
                return error

            monkeypatch.setattr(messaging, "resume_unavailable", admit)
            results = await asyncio.gather(*[
                send_message.handler({"to": "native-child", "message": f"Follow-up {i}"}, _ctx("root"))
                for i in range(2)
            ])
            assert all(not result.is_error for result in results)
            # This caller sees RUNNING with no stream yet: it must retain the
            # continuation owner even though the last executed context is old.
            first_job = None
            first_turn = turns_db.get_queued_turns("native-child")[0]
            if stop_kind == "enqueue_race":
                enqueued, release_enqueue, settling = (asyncio.Event() for _ in range(3))
                enqueue = turn_scheduler._enqueue_context_wake
                finish = messaging.finish_message_resume

                async def held_enqueue(*args, **kwargs):
                    turn = await enqueue(*args, **kwargs)
                    enqueued.set()
                    await asyncio.wait_for(release_enqueue.wait(), 10)
                    return turn

                async def entering_finish(*args):
                    settling.set()
                    return await finish(*args)

                monkeypatch.setattr(turn_scheduler, "_enqueue_context_wake", held_enqueue)
                monkeypatch.setattr(messaging, "finish_message_resume", entering_finish)
                send_job = asyncio.create_task(send_message.handler(
                    {"to": "native-child", "message": "Follow-up 2"}, _ctx("root"),
                ))
                executor_jobs.append(send_job)
                await asyncio.wait_for(enqueued.wait(), 10)
                first_job = asyncio.create_task(session_runtime.execute(first_turn))
                executor_jobs.append(first_job)
                await asyncio.wait_for(settling.wait(), 10)
                if first_ending != "parked":
                    assert not first_job.done()
                release_enqueue.set()
                result = await asyncio.wait_for(send_job, 10)
                monkeypatch.setattr(turn_scheduler, "_enqueue_context_wake", enqueue)
            else:
                result = await send_message.handler(
                    {"to": "native-child", "message": "Follow-up 2"}, _ctx("root"),
                )
            assert not result.is_error, result.text
            pending = turns_db.get_queued_turns("native-child")
            if first_job:
                pending.insert(0, first_turn)
            assert len(pending) == 3
            assert all(f"Follow-up {i}" in " ".join(t["prompt"] for t in pending) for i in range(3))
            first, second, third = pending
            assert all(t["payload"]["agent_message_reply_to"] == "root" for t in pending)
            outcome = await asyncio.wait_for(first_job, 10) if first_job else await session_runtime.execute(first)
            assert outcome == {"completed": "completed", "error": "aborted", "parked": "parked"}[first_ending]
            assert task_registry.get_task("root", "native-child")["status"] == "running"
            assert provider_agent_supervisor.get_worker("root", "native-child") is not None
            if stop_kind in {"completed", "error", "parked_report"}:
                await session_runtime.execute(second)
                assert task_registry.get_task("root", "native-child")["status"] == "running"
                final_outcome = await session_runtime.execute(third)
                if first_ending == "parked" or stop_kind == "parked_report":
                    assert task_registry.get_task("root", "native-child")["status"] == "running"
                    for child in ("native-grandchild", "last-grandchild"):
                        if child in workers.jobs:
                            await workers.finish(child)
                    (wake,) = turns_db.get_queued_turns("native-child")
                    assert wake["payload"]["agent_message_reply_to"] == "root"
                    final_outcome = await session_runtime.execute(wake)
                expected = "error" if final_outcome == "aborted" else "completed"
                assert task_registry.get_task("root", "native-child")["status"] == expected
                assert provider_agent_supervisor.get_worker("root", "native-child") is None
                await messaging.finish_message_resume("native-child", first, outcome)
                assert task_registry.get_task("root", "native-child")["status"] == expected
                assert turns_db.get_queued_turns("native-child") == []
                if first_ending != "parked" and stop_kind != "parked_report":
                    (parent_wake,) = turns_db.get_queued_turns("root")
                    frames = parent_wake["payload"]["pending_frames"]
                    assert [f["agent_message_turn_id"] for f in frames] == [t["id"] for t in pending]
                    assert frames[-1]["status"] == expected
                assert all(f"Follow-up {i}" in str(provider.requests) for i in range(3))
                return
            if stop_kind != "queued_stop":
                provider.started.clear()
                job = asyncio.create_task(session_runtime.execute(second))
                await asyncio.wait_for(provider.started.wait(), 10)
                # A delayed/repeated old callback must not retire the new stream.
                await messaging.finish_message_resume("native-child", first, outcome)
                assert task_registry.get_task("root", "native-child")["status"] == "running"
            if stop_kind == "stop_all":
                stopped = await stop_all_background_tasks("root", user_stop=True)
                assert any(row["task_id"] == "native-child" for row in stopped)
            else:
                stopped = await stop_session_task("root", "native-child")
                assert stopped["ok"] and stopped.get("detail") != "already finished"
            if job:
                await asyncio.wait_for(provider.cancelled.wait(), 10)
                assert await asyncio.wait_for(job, 10) == "aborted"
            assert turns_db.get_queued_turns("native-child") == []
            assert turns_db.get_turn(third["id"])["state"] == "aborted"
            assert len(provider.requests) == (2 if stop_kind == "queued_stop" else 3)
            assert task_registry.get_task("root", "native-child")["status"] == "killed"
            assert provider_agent_supervisor.get_worker("root", "native-child") is None
            if first_ending == "parked":
                workers.release["native-grandchild"].set()
                descendant = await asyncio.wait_for(workers.jobs.pop("native-grandchild"), 10)
                assert descendant.killed
            return
        result = await send_message.handler(
            {"to": "native-child", "message": "Continue using the original work"}, _ctx("root")
        )
        assert not result.is_error, result.text
        (continuation,) = turns_db.get_queued_turns("native-child")
        assert provider_agent_supervisor.get_worker("root", "native-child") is not None
        if ending == "queued_stop":
            stopped = await stop_session_task("root", "native-child")
            assert stopped["ok"] and "reaped" not in stopped.get("detail", "")
            assert turns_db.get_queued_turns("native-child") == []
            assert len(provider.requests) == 1
        else:
            job = asyncio.create_task(session_runtime.execute(continuation))
            await asyncio.wait_for(provider.started.wait(), 10)
            if ending in {"stop_task", "stop_all"}:
                if ending == "stop_task":
                    stopped = await stop_session_task("root", "native-child")
                    assert stopped["ok"] and "reaped" not in stopped.get("detail", "")
                else:
                    await stop_all_background_tasks("root", user_stop=True)
                await asyncio.wait_for(provider.cancelled.wait(), 10)
            outcome = await asyncio.wait_for(job, 10)
            if ending == "parked_report":
                assert outcome == "parked"
                assert provider_agent_supervisor.get_worker("root", "native-child") is not None
                assert task_registry.get_task("root", "native-child")["status"] == "running"
                assert turns_db.get_queued_turns("root") == []
                await workers.finish("native-grandchild")
                (continuation,) = turns_db.get_queued_turns("native-child")
                assert continuation["source"] == "unpark"
                assert continuation["payload"]["agent_message_reply_to"] == "root"
                runtime = session_runtime.SessionRuntime(continuation)
                outcome = await runtime.execute()
                continuation = runtime.turn
            assert outcome == ("completed" if ending in {"completed", "parked_report"} else "aborted")
            assert len(provider.requests) == (3 if ending == "parked_report" else 2)
            # The native engine resumed its own stored conversation, not a fresh loop.
            assert "original native report" in str(provider.requests[1].messages)
        assert provider_agent_supervisor.get_worker("root", "native-child") is None
        # The panel replays task.updated, not the registry or completion card.
        from app.db.session_events import get_events
        updates = [e["payload"] for e in get_events("root", limit=1000)
                   if e["event_type"] == "task.updated"
                   and e["payload"]["task_id"] == "native-child"]
        expected_status = ("killed" if ending in {"stop_task", "stop_all", "queued_stop"}
                           else "error" if ending == "error" else "completed")
        assert updates[-1]["state"] == expected_status
        starts = [e for e in updates if e["state"] == "running"]
        assert starts[-1]["started_at"] != starts[0]["started_at"]
        row = task_registry.get_task("root", "native-child")
        if ending in {"stop_task", "stop_all", "queued_stop"}:
            assert row["status"] == "killed"
            assert turns_db.get_queued_turns("root") == []
        else:
            assert row["status"] == ("completed" if ending in {"completed", "parked_report"} else "error")
            (wake,) = turns_db.get_queued_turns("root")
            assert "original native report" not in wake["prompt"]
            if ending in {"completed", "parked_report"}:
                assert "new native report" in wake["prompt"]
            assert wake["payload"]["pending_frames"][0]["agent_message_turn_id"] == continuation["id"]
    finally:
        if job and not job.done():
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        await asyncio.gather(*executor_jobs, return_exceptions=True)
        turn_manager.register_interrupt_executor(None)
        session_runtime.register_scheduler(None)
        await adapter.disconnect()
        await asyncio.gather(*get_session_pool()._background_teardowns)
