"""Exercise Swarm through the real app dispatcher and admission gates.

Only provider execution is replaced by bounded event-controlled workers; these
tests make no provider requests and use the suite's isolated database shim.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core import config, provider_agent, provider_agent_supervisor as supervisor
from app.core import session_pool, swarm, task_dispatch, turn_scheduler
from app.core.session_pool import AdmissionSnapshot
from fluid.swarm import Launch, current_launch
from fluid.tools.registry import ToolCtx


class WorkerBarrier:
    def __init__(self):
        self.active = {}
        self.started = []
        self.finished = []
        self.peak = 0
        self.changed = asyncio.Event()
        self.snapshot_calls = 0
        self.owners = {}

    def peek(self, key):
        return self.active.get(key)

    def admission_snapshot(self, lane="delegated"):
        self.snapshot_calls += 1
        # A native parent plus its children; zero CLI processes. Pool/resource
        # guards have their own tests, while this exercises real app admission.
        return AdmissionSnapshot(
            refusal=None, in_pool=1 + len(self.active), hard_cap=8,
            delegated_in_pool=0, native_in_pool=1 + len(self.active), lane=lane,
        )

    async def worker(self, **kwargs):
        key = kwargs["worker_session_id"]
        parent = kwargs["parent_session_id"]
        assert task_dispatch._worker_lane(kwargs["provider_id"], kwargs["model_id"]) == "native"
        assert key in swarm.workers
        self.active[key] = asyncio.Event()
        supervisor.register_worker(supervisor.WorkerRecord(
            parent_session_id=parent, worker_session_id=key, task_id=key,
            tool_use_id=key, provider_id=kwargs["provider_id"], background=True,
        ))
        self.started.append(key)
        self.owners[key] = kwargs["context_session_id"]
        assert parent == "swarm-parent"  # nested workers retain root supervision
        self.peak = max(self.peak, len(self.active))
        self.changed.set()
        try:
            await self.active[key].wait()
            self.finished.append(key)
            return provider_agent.WorkerOutcome(ok=True, session_id=key, report="done")
        finally:
            self.active.pop(key, None)
            supervisor.unregister_worker(parent, key)
            self.changed.set()

    async def wait_for_starts(self, count):
        async def wait():
            while len(self.started) < count:
                self.changed.clear()
                await self.changed.wait()
        await asyncio.wait_for(wait(), 5)

    def release(self):
        for event in list(self.active.values()):
            event.set()


@pytest.fixture
async def harness(mock_db, monkeypatch):
    from app.db import database

    for name in ("task_tool_max_workers", "subagent_max_concurrent_per_session", "task_worker_max_per_provider"):
        monkeypatch.setattr(config.settings, name, config.Settings.model_fields[name].default)
    database.create_model_provider(
        provider_id="test-provider", name="Offline provider", provider_type="openai",
        base_url="https://provider.invalid/v1", models=[{"id": "gpt-5", "label": "Test"}],
        default_model="gpt-5", api_dialect="responses", enabled=True,
    )
    database.create_profile("swarm-root", "Root", None, {"agent_invoke_mode": "all"})
    database.create_profile("swarm-helper", "Helper", None, {
        "provider_models": {"test-provider": "gpt-5"},
        "allowed_tools": ["Read", "Grep"],
    })
    database.create_session(session_id="swarm-parent", profile_id="swarm-root", title="Offline Swarm")
    for session_id in ("nested-owner", "nested-sibling"):
        database.create_session(
            session_id=session_id, profile_id="swarm-root", title=session_id,
            parent_session_id="swarm-parent",
        )
        provider_agent.register_worker_session(session_id)
    barrier = WorkerBarrier()
    supervisor.reset()
    task_dispatch._PENDING_ADMISSIONS.clear()
    task_dispatch._ADMISSION_LOCK = None
    swarm.coordinators.clear()
    swarm.workers.clear()
    swarm.worker_jobs.clear()
    monkeypatch.setattr(session_pool, "_session_pool", barrier)
    monkeypatch.setattr(task_dispatch, "_credential_gap", lambda _: None)
    monkeypatch.setattr(task_dispatch, "_ADMISSION_POLL_SECONDS", 0.005)
    monkeypatch.setattr(provider_agent, "dispatch_worker", barrier.worker)
    monkeypatch.setattr(supervisor, "_kill_worker_session", AsyncMock())
    monkeypatch.setattr(swarm, "persist", AsyncMock())
    monkeypatch.setattr(turn_scheduler, "on_task_terminal", AsyncMock())
    async def start(count, *, caller_session_id="swarm-parent", **options):
        ctx = ToolCtx(extra={"task_dispatch": task_dispatch.make_fluid_dispatcher(
            caller_session_id, provider_id="test-provider"), "background_task_started": AsyncMock()})
        result = await swarm.make_handler(lambda: caller_session_id)({
            "action": "start", "tasks": [
                {"id": str(i), "agent": "swarm-helper", "prompt": "Wait at the offline barrier"}
                for i in range(count)
            ], **options,
        }, ctx)
        assert not result.is_error, result.text
        key = result.background_task.task_id
        return swarm.coordinators[caller_session_id], key

    yield barrier, start
    for coordinator in list(swarm.coordinators.values()):
        await asyncio.wait_for(coordinator.aclose(), 5)
    for task in list(task_dispatch._BACKGROUND_TASKS):
        if not task.done():
            task.cancel()
    await asyncio.gather(*task_dispatch._BACKGROUND_TASKS, return_exceptions=True)
    task_dispatch._PENDING_ADMISSIONS.clear()
    task_dispatch._ADMISSION_LOCK = None
    supervisor.reset()
    provider_agent.reset_worker_sessions()
    swarm.coordinators.clear()
    swarm.workers.clear()
    swarm.worker_jobs.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [{}, {"max_parallel": 10}])
async def test_ten_native_children_start_before_any_finishes(harness, options):
    barrier, start = harness
    coordinator, key = await start(10, **options)
    await barrier.wait_for_starts(10)
    assert barrier.peak == 10
    assert not barrier.finished
    barrier.release()
    await asyncio.wait_for(coordinator.jobs[key], 5)
    assert coordinator.snapshot(key)["status"] == "completed"
    assert len(barrier.finished) == 10
    assert task_dispatch._pending_admission_count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("setting", [
    "task_worker_max_per_provider", "subagent_max_concurrent_per_session", "task_tool_max_workers",
])
async def test_explicit_host_limit_queues_then_starts_remaining_child(harness, monkeypatch, setting):
    barrier, start = harness
    monkeypatch.setattr(config.settings, setting, 2)
    coordinator, key = await start(3)
    await barrier.wait_for_starts(2)
    await asyncio.sleep(0.08)
    assert len(barrier.started) == 2 and not barrier.finished
    barrier.active[barrier.started[0]].set()
    await barrier.wait_for_starts(3)
    assert barrier.peak == 2
    barrier.release()
    await asyncio.wait_for(coordinator.jobs[key], 5)
    assert coordinator.snapshot(key)["status"] == "completed"
    assert len(barrier.finished) == 3
    assert task_dispatch._pending_admission_count() == 0


@pytest.mark.asyncio
async def test_stop_settles_waiting_admission_without_starting_more_workers(harness, monkeypatch):
    barrier, start = harness
    monkeypatch.setattr(config.settings, "task_worker_max_per_provider", 1)
    coordinator, key = await start(10)
    await barrier.wait_for_starts(1)
    # Let the coordinator enter admission for the remaining ready children.
    await asyncio.sleep(0.08)
    assert barrier.snapshot_calls >= 10
    await swarm.cancel_all("swarm-parent")
    await asyncio.wait_for(coordinator.jobs[key], 1)
    assert len(barrier.started) == 1
    assert not barrier.active
    assert coordinator.snapshot(key)["status"] == "cancelled"
    assert all(t["status"] == "cancelled" for t in coordinator.snapshot(key)["tasks"])
    assert task_dispatch._pending_admission_count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("closed", [False, True])
async def test_stop_during_resource_measurement_cannot_reserve(harness, monkeypatch, closed):
    barrier, _ = harness
    coordinator = SimpleNamespace(session_id="swarm-parent", closed=False, cancelled=set())
    launch = Launch(coordinator, "swarm-test", "child", asyncio.get_running_loop().create_future())
    def snapshot(lane):
        if closed:
            coordinator.closed = True
        else:
            coordinator.cancelled.add("swarm-test")
        return barrier.admission_snapshot(lane)
    monkeypatch.setattr(session_pool, "admission_snapshot", snapshot)
    token = current_launch.set(launch)
    try:
        refusal, _ = await task_dispatch._reserve_admission(
            "never-started", lane="native", parent_session_id="swarm-parent",
            provider_id="test-provider",
        )
    finally:
        current_launch.reset(token)
    assert "cancelled before admission" in refusal
    assert not task_dispatch._PENDING_ADMISSIONS


@pytest.mark.asyncio
async def test_cancelled_launch_does_not_block_a_different_root(harness):
    coordinator = SimpleNamespace(session_id="different-root", closed=True, cancelled={"old-swarm"})
    launch = Launch(coordinator, "old-swarm", "child", asyncio.get_running_loop().create_future())
    token = current_launch.set(launch)
    try:
        refusal, _ = await task_dispatch._reserve_admission(
            "unrelated-worker", lane="native", parent_session_id="swarm-parent",
            provider_id="test-provider",
        )
    finally:
        current_launch.reset(token)
    assert refusal is None
    assert "unrelated-worker" in task_dispatch._PENDING_ADMISSIONS
    task_dispatch._release_admission("unrelated-worker")


@pytest.mark.asyncio
async def test_nested_owner_stop_preserves_same_root_sibling_swarm(harness, monkeypatch):
    barrier, start = harness
    monkeypatch.setattr(config.settings, "task_worker_max_per_provider", 1)
    owner, owner_key = await start(3, caller_session_id="nested-owner")
    await barrier.wait_for_starts(1)
    sibling, sibling_key = await start(1, caller_session_id="nested-sibling")
    await asyncio.sleep(0.08)
    assert set(barrier.owners.values()) == {"nested-owner"}
    await swarm.cancel_all("nested-owner")
    await asyncio.wait_for(owner.jobs[owner_key], 1)
    await barrier.wait_for_starts(2)
    assert list(barrier.owners.values()).count("nested-owner") == 1
    assert list(barrier.owners.values()).count("nested-sibling") == 1
    assert owner.snapshot(owner_key)["status"] == "cancelled"
    barrier.release()
    await asyncio.wait_for(sibling.jobs[sibling_key], 5)
    assert sibling.snapshot(sibling_key)["status"] == "completed"
    assert task_dispatch._pending_admission_count() == 0


@pytest.mark.asyncio
async def test_cancelled_sibling_launch_does_not_authorize_cancelling_actual_caller(harness, monkeypatch):
    coordinator = SimpleNamespace(session_id="nested-owner", closed=True, cancelled={"old-swarm"})
    launch = Launch(coordinator, "old-swarm", "child", asyncio.get_running_loop().create_future())
    worker = AsyncMock(return_value=provider_agent.WorkerOutcome(ok=True, session_id="test-sibling", report="done"))
    monkeypatch.setattr(provider_agent, "dispatch_worker", worker)
    # Both caller chains resolve to swarm-parent. An inherited launch from the
    # other branch must not cancel this caller simply because roots match.
    assert task_dispatch.caller_chain("nested-owner")[1] == task_dispatch.caller_chain("nested-sibling")[1]
    token = current_launch.set(launch)
    try:
        result = await task_dispatch.make_fluid_dispatcher(
            "nested-sibling", provider_id="test-provider",
        )({"agent": "swarm-helper", "prompt": "Benign sibling work"}, ToolCtx())
    finally:
        current_launch.reset(token)
    assert not result.is_error, result.text
    worker.assert_awaited_once()
    assert worker.await_args.kwargs["parent_session_id"] == "swarm-parent"
    assert worker.await_args.kwargs["context_session_id"] == "nested-sibling"
    assert task_dispatch._pending_admission_count() == 0


@pytest.mark.asyncio
async def test_stopped_nested_swarm_cannot_reenter_admission_for_retry(harness, monkeypatch):
    _, start = harness
    calls = []
    async def fails_before_first_frame(**kwargs):
        calls.append(kwargs)
        assert kwargs["parent_session_id"] == "swarm-parent"
        assert kwargs["context_session_id"] == "nested-owner"
        launch = current_launch.get()
        launch.coordinator.cancelled.add(launch.swarm_id)
        return provider_agent.WorkerOutcome(
            ok=False, session_id=kwargs["worker_session_id"],
            error="worker failed to start: peer closed connection without sending complete message body",
            frames_forwarded=0,
        )
    monkeypatch.setattr(provider_agent, "dispatch_worker", fails_before_first_frame)
    monkeypatch.setattr(provider_agent, "announce_abandoned_retry", AsyncMock())
    monkeypatch.setattr(task_dispatch, "_retry_backoff", lambda *args: 0)
    coordinator, key = await start(1, caller_session_id="nested-owner")
    await asyncio.wait_for(coordinator.jobs[key], 1)
    assert len(calls) == 1
    assert coordinator.snapshot(key)["status"] == "cancelled"
    assert task_dispatch._pending_admission_count() == 0


@pytest.mark.asyncio
async def test_dependent_swarm_workers_keep_the_originating_effort(harness, monkeypatch):
    from app.db import turns

    barrier, _ = harness
    latest = {"overrides": {"effort": "xhigh"}}
    monkeypatch.setattr(turns, "get_latest_turn", lambda _: latest)
    captured = []

    async def worker(**kwargs):
        captured.append(kwargs["inherited_effort"])
        return await barrier.worker(**kwargs)

    monkeypatch.setattr(provider_agent, "dispatch_worker", worker)
    ctx = ToolCtx(extra={
        "app_caller_effort": "xhigh",
        "task_dispatch": task_dispatch.make_fluid_dispatcher("swarm-parent", provider_id="test-provider"),
        "background_task_started": AsyncMock(),
    })
    result = await swarm.make_handler(lambda: "swarm-parent")({
        "action": "start", "tasks": [
            {"id": "first", "agent": "swarm-helper", "prompt": "first"},
            {"id": "dependent", "agent": "swarm-helper", "prompt": "second", "depends_on": ["first"]},
        ],
    }, ctx)
    assert not result.is_error
    await barrier.wait_for_starts(1)
    latest["overrides"]["effort"] = "low"
    barrier.release()
    await barrier.wait_for_starts(2)
    assert captured == ["xhigh", "xhigh"]
    barrier.release()
    coordinator = swarm.coordinators["swarm-parent"]
    key = result.background_task.task_id
    await asyncio.wait_for(coordinator.jobs[key], 5)
    assert coordinator.snapshot(key)["status"] == "completed"
