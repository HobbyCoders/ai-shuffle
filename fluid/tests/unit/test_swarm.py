import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from fluid.swarm import MAX_TASKS, SwarmCoordinator, current_launch, recover, validate
from fluid.tools.registry import BackgroundTaskReceipt, ToolCtx, ToolOutcome


def spec(key, deps=()):
    return dict(id=key, agent="helper", prompt="do " + key, depends_on=list(deps))

@pytest.mark.parametrize("model", [None, "other-model"])
async def test_task_model_choice_reaches_dispatch(model):
    calls = []

    async def dispatch(data, ctx):
        calls.append(data)
        return ToolOutcome(text="done")

    c = SwarmCoordinator("main", dispatch, AsyncMock(), AsyncMock(), AsyncMock())
    task = spec("a")
    if model is not None:
        task["model"] = model
    _, receipt = await c.start({"tasks": [task]}, ToolCtx())
    await asyncio.wait_for(c.jobs[receipt.task_id], 5)
    assert calls[0].get("model") == model
    assert ("model" in calls[0]) is (model is not None)
    snapshot = c.snapshot(receipt.task_id)
    assert snapshot["status"] == "completed"


@pytest.mark.parametrize("model", [False, 3, [], "", " "])
def test_invalid_model_choice_rejected(model):
    with pytest.raises(ValueError, match="model must be"):
        validate({"tasks": [{**spec("a"), "model": model}]})


def test_swarm_schema_documents_task_model_validation():
    from fluid.tools.builtins.swarm import SPEC
    prop = SPEC.input_schema["properties"]["tasks"]["items"]["properties"]["model"]
    assert prop["type"] == "string"
    assert "Task" in prop["description"]


@pytest.mark.parametrize("data", [
    {"tasks": []}, {"tasks": [spec("a"), spec("a")]},
    {"tasks": [spec("a", ["missing"])]}, {"tasks": [spec("a", ["a"])]},
    {"tasks": [spec("a", ["b"]), spec("b", ["a"])]},
    {"tasks": [spec("a")], "max_parallel": 0},
    {"tasks": [spec("a")], "max_parallel": MAX_TASKS + 1},
    {"tasks": [spec("a")], "max_parallel": True},
    {"tasks": [{"id": "a", "agent": "helper", "prompt": ""}]},
])
async def test_validation_before_spawn(data):
    dispatch = AsyncMock()
    c = SwarmCoordinator("main", dispatch, AsyncMock(), AsyncMock(), AsyncMock())
    with pytest.raises(ValueError):
        await c.start(data, ToolCtx())
    dispatch.assert_not_called()
    assert not c.jobs


def test_parallel_schema_matches_validation_and_graph_default():
    from fluid.tools.builtins.swarm import SPEC

    properties = SPEC.input_schema["properties"]
    limit = properties["max_parallel"]
    assert limit["minimum"] == 1
    assert limit["maximum"] == properties["tasks"]["maxItems"] == MAX_TASKS
    assert "default" not in limit  # The default depends on the graph size.
    for count in (1, 10, MAX_TASKS):
        tasks = [spec(str(i)) for i in range(count)]
        assert validate({"tasks": tasks})[1] == count
    for cap in (1, 3, 8, 9, 10, MAX_TASKS):
        assert validate({"tasks": [spec("a")], "max_parallel": cap})[1] == cap


@pytest.mark.parametrize("max_parallel", [None, 10])
async def test_ten_ready_tasks_start_before_any_completes(max_parallel):
    all_started, release = asyncio.Event(), asyncio.Event()
    live = peak = completed = 0

    async def dispatch(data, ctx):
        nonlocal live, peak, completed
        live += 1
        peak = max(peak, live)
        if live == 10:
            all_started.set()
        await release.wait()
        live -= 1
        completed += 1
        current_launch.get().finish("finished", "ok")
        return BackgroundTaskReceipt(task_id=data["label"])

    finished = AsyncMock()
    c = SwarmCoordinator("main", dispatch, AsyncMock(), AsyncMock(), finished)
    data = {"tasks": [spec(str(i)) for i in range(10)]}
    if max_parallel is not None:
        data["max_parallel"] = max_parallel
    _, receipt = await c.start(data, ToolCtx())
    try:
        await asyncio.wait_for(all_started.wait(), 5)
        assert completed == 0
        assert peak == live == 10
    finally:
        release.set()
        await asyncio.wait_for(c.jobs[receipt.task_id], 5)
    assert completed == 10
    assert c.snapshot(receipt.task_id)["status"] == "completed"
    finished.assert_awaited_once()


@pytest.mark.parametrize("max_parallel, expected", [(2, 2), (None, 10)])
async def test_cancel_preserves_spawn_receipts_and_parallel_limit(max_parallel, expected):
    admitted, release = asyncio.Event(), asyncio.Event()
    launches = {}

    async def dispatch(data, ctx):
        launches[data["label"]] = current_launch.get()
        if len(launches) == expected:
            admitted.set()
        # Hold the spawn acknowledgement, including across Stop.
        await release.wait()
        return BackgroundTaskReceipt(task_id=data["label"])

    async def stop(key):
        launches[key].finish("cancelled", "stopped")

    cancel = AsyncMock(side_effect=stop)
    c = SwarmCoordinator("main", dispatch, cancel, AsyncMock(), AsyncMock())
    data = {"tasks": [spec(str(i)) for i in range(10)]}
    if max_parallel is not None:
        data["max_parallel"] = max_parallel
    _, receipt = await c.start(data, ToolCtx())
    try:
        await asyncio.wait_for(admitted.wait(), 5)
        assert len(launches) == expected
        await c.cancel(receipt.task_id)
    finally:
        release.set()
        await asyncio.wait_for(c.jobs[receipt.task_id], 5)
    assert len(launches) == expected
    assert cancel.await_count == expected
    assert {row["status"] for row in c.snapshot(receipt.task_id)["tasks"]} == {"cancelled"}

async def test_concurrency_dependencies_failure_and_early_completion():
    live, peak, prompts = 0, 0, {}
    async def dispatch(data, ctx):
        nonlocal live, peak
        key = data["label"]
        prompts[key] = data["prompt"]
        launch = current_launch.get()
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0)
        live -= 1
        launch.finish("failed" if key == "b" else "finished", "result-" + key)
        return BackgroundTaskReceipt(task_id=key, worker_session_id="session-" + key)
    finished = AsyncMock()
    c = SwarmCoordinator("main", dispatch, AsyncMock(), AsyncMock(), finished)
    snapshot, receipt = await c.start(dict(tasks=[spec("a"), spec("b"), spec("c", ["a"]), spec("d", ["b"])], max_parallel=2), ToolCtx())
    await c.jobs[receipt.task_id]
    rows = {t["id"]: t for t in c.snapshot(receipt.task_id)["tasks"]}
    assert peak == 2
    assert rows["c"]["status"] == "completed"
    assert "result-a" in prompts["c"]
    assert rows["d"]["status"] == "blocked" and "d" not in prompts
    assert c.snapshot(receipt.task_id)["status"] == "failed"
    assert rows["a"]["worker_session_id"] == "session-a"
    finished.assert_awaited_once()

async def test_cancel_during_spawn_preserves_receipt_and_cancels_successors():
    entering, release = asyncio.Event(), asyncio.Event()
    launches = {}
    async def dispatch(data, ctx):
        launches["worker"] = current_launch.get()
        entering.set()
        await release.wait()
        return BackgroundTaskReceipt(task_id="worker", worker_session_id="child")
    async def stop(key):
        launches[key].finish("cancelled", "stopped")
    cancel = AsyncMock(side_effect=stop)
    c = SwarmCoordinator("main", dispatch, cancel, AsyncMock(), AsyncMock())
    _, receipt = await c.start(dict(tasks=[spec("a"), spec("b", ["a"])], max_parallel=1), ToolCtx())
    await entering.wait()
    await c.cancel(receipt.task_id)
    release.set()
    await c.jobs[receipt.task_id]
    snapshot = c.snapshot(receipt.task_id)
    assert snapshot["status"] == "cancelled"
    assert {t["status"] for t in snapshot["tasks"]} == {"cancelled"}
    cancel.assert_awaited_once_with("worker")
    assert snapshot["tasks"][0]["task_id"] == "worker"
    await c.cancel(receipt.task_id)
    assert cancel.await_count == 1

async def test_recovery_never_relaunches():
    snapshot = dict(id="s", status="running", updated_at="old", tasks=[dict(status="completed"), dict(status="running")])
    restored = recover(snapshot)
    assert restored["status"] == "interrupted"
    assert restored["tasks"][0]["status"] == "completed"
    assert restored["tasks"][1]["status"] == "interrupted"
    assert snapshot["status"] == "running"
    assert recover(restored) == restored

async def test_snapshot_publications_cannot_overtake_each_other():
    entered, release = asyncio.Event(), asyncio.Event()
    published = []

    async def publish(snapshot):
        if snapshot["status"] == "running":
            entered.set()
            await release.wait()
        published.append(snapshot)

    coordinator = SwarmCoordinator("main", AsyncMock(), AsyncMock(), publish, AsyncMock())
    coordinator.swarms["s"] = {"id": "s", "status": "running"}
    first = asyncio.create_task(coordinator.emit("s"))
    await entered.wait()
    coordinator.swarms["s"]["status"] = "completed"
    second = asyncio.create_task(coordinator.emit("s"))
    # Give the concurrent write a chance to enter while the first is held.
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)
    assert [row["status"] for row in published] == ["running", "completed"]
    assert published[0]["updated_at"] <= published[1]["updated_at"]


@pytest.mark.parametrize("worker_count", [1, 10])
async def test_publication_failure_drains_inflight_spawn_receipts(worker_count):
    created, failing, acknowledge = asyncio.Event(), asyncio.Event(), asyncio.Event()
    workers, stops, abandoned, acknowledged = {}, {}, [], []
    failed = False

    async def dispatch(data, ctx):
        key = data["label"]
        stops[key] = asyncio.Event()
        workers[key] = asyncio.create_task(stops[key].wait())
        if len(workers) == worker_count:
            created.set()
        try:
            await acknowledge.wait()
        except asyncio.CancelledError:
            abandoned.append(key)
            raise
        acknowledged.append(key)
        return BackgroundTaskReceipt(task_id=key, worker_session_id="session-" + key)

    async def publish(snapshot):
        nonlocal failed
        if not failed and snapshot["tasks"][-1]["status"] == "running":
            await created.wait()
            failed = True
            failing.set()
            raise OSError("snapshot write failed")

    async def stop(key):
        stops[key].set()
        await workers[key]
        # No completion callback: exception cleanup must not await its future.

    finished, cancel = AsyncMock(), AsyncMock(side_effect=stop)
    c = SwarmCoordinator("main", dispatch, cancel, publish, finished)
    _, receipt = await c.start({"tasks": [spec(str(i)) for i in range(worker_count + 1)]}, ToolCtx())
    try:
        await asyncio.wait_for(failing.wait(), 5)
        acknowledge.set()
        await asyncio.wait_for(c.jobs[receipt.task_id], 5)
        assert not abandoned
        assert len(acknowledged) == worker_count
        assert {call.args[0] for call in cancel.await_args_list} == set(workers)
        assert all(worker.done() for worker in workers.values())
        snapshot = c.snapshot(receipt.task_id)
        assert snapshot["status"] == "failed"
        assert "snapshot write failed" in finished.await_args.args[2]
        assert {row["task_id"] for row in snapshot["tasks"] if row["task_id"]} == set(workers)
        finished.assert_awaited_once()
    finally:
        acknowledge.set()
        for gate in stops.values():
            gate.set()
        await asyncio.gather(*workers.values(), return_exceptions=True)


@pytest.mark.parametrize("receipt_returned", [False, True])
async def test_repeated_coordinator_interrupt_stops_ten_workers(receipt_returned):
    created, acknowledge, recorded = asyncio.Event(), asyncio.Event(), asyncio.Event()
    workers, stops, abandoned = {}, {}, []
    if receipt_returned:
        acknowledge.set()

    async def dispatch(data, ctx):
        key = data["label"]
        stops[key] = asyncio.Event()
        workers[key] = asyncio.create_task(stops[key].wait())
        if len(workers) == 10:
            created.set()
        try:
            await acknowledge.wait()
        except asyncio.CancelledError:
            abandoned.append(key)
            raise
        return BackgroundTaskReceipt(task_id=key)

    async def publish(snapshot):
        if all(row["task_id"] for row in snapshot["tasks"]):
            recorded.set()

    async def stop(key):
        stops[key].set()
        await workers[key]
        # The stopped worker never emits a completion callback.

    finished, cancel = AsyncMock(), AsyncMock(side_effect=stop)
    c = SwarmCoordinator("main", dispatch, cancel, publish, finished)
    _, receipt = await c.start({"tasks": [spec(str(i)) for i in range(10)]}, ToolCtx())
    job = c.jobs[receipt.task_id]
    try:
        await asyncio.wait_for(recorded.wait() if receipt_returned else created.wait(), 5)
        job.cancel()

        async def cancellation_latched():
            while receipt.task_id not in c.cancelled:
                await asyncio.sleep(0)

        await asyncio.wait_for(cancellation_latched(), 5)
        job.cancel()  # Shutdown/Stop can race the first interrupt's cleanup.
        acknowledge.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(job, 5)
        assert not abandoned
        assert cancel.await_count == 10
        assert all(worker.done() for worker in workers.values())
        assert c.snapshot(receipt.task_id)["status"] == "interrupted"
        finished.assert_awaited_once()
    finally:
        acknowledge.set()
        for gate in stops.values():
            gate.set()
        await asyncio.gather(*workers.values(), return_exceptions=True)


async def test_close_drains_workers_and_completion():
    launched = asyncio.Event()
    refs = {}
    async def dispatch(data, ctx):
        refs["a"] = current_launch.get()
        launched.set()
        return BackgroundTaskReceipt(task_id="a")
    async def stop(key):
        refs[key].finish("cancelled", "closed")
    c = SwarmCoordinator("main", dispatch, stop, AsyncMock(), AsyncMock())
    _, receipt = await c.start(dict(tasks=[spec("a")]), ToolCtx())
    await launched.wait()
    await c.aclose()
    assert c.jobs[receipt.task_id].done()
    assert c.snapshot(receipt.task_id)["status"] == "cancelled"

async def test_swarm_does_not_bypass_task_surface():
    from fluid.tools.builtins.swarm import handler
    host = AsyncMock()
    result = await handler(dict(action="start", tasks=[spec("a")]), ToolCtx(extra={"swarm": host, "tool_filter": lambda name: name != "Task"}))
    assert result.is_error
    host.assert_not_called()

async def test_cli_producer_real_child_and_aggregate_wake(tmp_path):
    import io, json
    from fluid.cli.__main__ import run_once
    from fluid.events.model import SwarmUpdated
    from fluid.store.base import MemoryStore
    from tests.unit.test_cli_host import RoutingProvider, _config, _allow
    from tests.unit.test_tool_loop import _tool, _text
    agents = tmp_path / ".agents" / "agents"
    agents.mkdir(parents=True)
    (agents / "helper.md").write_text("---\nname: helper\ndescription: helper\n---\nSWARM-HELPER\n")
    provider = RoutingProvider(parent=[_tool("sw", "Swarm", json.dumps(dict(action="start", tasks=[spec("a"), spec("b", ["a"])]))), _text("waiting"), _text("finished")], child=[_text("child report")], child_marker="SWARM-HELPER")
    store, out = MemoryStore(), io.StringIO()
    rc = await run_once(_config(tmp_path), store, "go", provider=provider, out=out, on_permission=_allow)
    assert rc == 0
    assert provider.child_calls == 2
    assert "child report" in provider.child_requests[1].messages[0].text()
    events = [e for sid in store._sessions for e in await store.events_since(sid, -1) if isinstance(e, SwarmUpdated)]
    assert events[-1].swarm["status"] == "completed"
    assert "[woken]" in out.getvalue()


async def test_cli_runs_ten_child_engines_concurrently_with_scripted_provider(tmp_path):
    import io, json
    from fluid.cli.__main__ import run_once
    from fluid.events.model import SwarmUpdated
    from fluid.store.base import MemoryStore
    from tests.unit.test_cli_host import RoutingProvider, _config, _allow, _system_text
    from tests.unit.test_tool_loop import _tool, _text

    agents = tmp_path / ".agents" / "agents"
    agents.mkdir(parents=True)
    (agents / "helper.md").write_text("---\nname: helper\ndescription: helper\n---\nSWARM-HELPER\n")
    all_started, release = asyncio.Event(), asyncio.Event()

    class ConcurrentProvider(RoutingProvider):
        """Real NativeLoop children, offline model responses held at a barrier."""

        active = peak = 0

        async def stream(self, request):
            child = self.marker in _system_text(request)
            if child:
                self.active += 1
                self.peak = max(self.peak, self.active)
                if self.active == 10:
                    all_started.set()
            try:
                if child:
                    await release.wait()
                async for chunk in super().stream(request):
                    yield chunk
            finally:
                if child:
                    self.active -= 1

    provider = ConcurrentProvider(
        parent=[_tool("sw", "Swarm", json.dumps({"action": "start",
                 "tasks": [spec(str(i)) for i in range(10)]})),
                _text("waiting"), _text("finished")],
        child=[_text("child report")], child_marker="SWARM-HELPER")
    store, out = MemoryStore(), io.StringIO()
    run = asyncio.create_task(run_once(_config(tmp_path), store, "go",
                                      provider=provider, out=out, on_permission=_allow))
    try:
        await asyncio.wait_for(all_started.wait(), 5)
        assert provider.active == provider.peak == 10
        assert provider.child_calls == 0
    finally:
        release.set()
        rc = await asyncio.wait_for(run, 5)
    assert rc == 0
    assert provider.child_calls == 10
    events = [e for sid in store._sessions for e in await store.events_since(sid, -1)
              if isinstance(e, SwarmUpdated)]
    assert events[-1].swarm["status"] == "completed"
    assert len(events[-1].swarm["tasks"]) == 10
    assert "[woken]" in out.getvalue()



@pytest.mark.parametrize("count", [1, 8, 63])
@pytest.mark.parametrize("content", ["x", '\n"\\', "界🙂"])
def test_dependency_handoffs_keep_every_result_with_valid_bounded_json(count, content):
    import json
    from fluid.swarm import RESULT_LIMIT, dependency_results

    keys = [str(i).zfill(64) for i in range(count)]
    rows = {key: {"result": content * RESULT_LIMIT} for key in keys}
    encoded = dependency_results(rows, keys)
    assert len(encoded) <= RESULT_LIMIT
    decoded = json.loads(encoded)
    assert list(decoded) == keys
    for key in keys:
        assert decoded[key].endswith(" [truncated; consult worker report]")
        assert rows[key]["result"].startswith(decoded[key].split(" [truncated;")[0])
        assert decoded[key].startswith(content)
    assert rows[keys[0]]["result"] == content * RESULT_LIMIT


def test_small_dependency_handoffs_are_unchanged():
    import json
    from fluid.swarm import dependency_results

    rows = {"a": {"result": "complete report"}, "b": {"result": ""}}
    assert json.loads(dependency_results(rows, ["a", "b"])) == {
        "a": "complete report", "b": "",
    }


async def test_cli_eight_reports_feed_review_without_parent_handoffs(tmp_path):
    import io, json
    from fluid.cli.__main__ import run_once
    from fluid.events.model import SessionWoken, SwarmUpdated
    from fluid.store.base import MemoryStore
    from tests.unit.test_cli_host import RoutingProvider, _config, _allow, _system_text
    from tests.unit.test_tool_loop import _tool, _text
    from fluid.tools.fragments import SWARM_ROUTING_SUMMARY

    agents = tmp_path / ".agents" / "agents"
    agents.mkdir(parents=True)
    (agents / "helper.md").write_text(
        "---\nname: helper\ndescription: helper\n---\nSWARM-HELPER\n")
    keys = [f"research-{i}" for i in range(8)]
    tasks = [spec(key) for key in keys] + [spec("review", keys)]
    provider = RoutingProvider(
        parent=[_tool("sw", "Swarm", json.dumps({"action": "start",
                 "tasks": tasks, "max_parallel": 3})),
                _text("waiting"), _text("review complete")],
        child=[_text(f"finding-{i}: " + "x" * 5000) for i in range(8)]
              + [_text("review report")], child_marker="SWARM-HELPER")
    store, out = MemoryStore(), io.StringIO()
    rc = await run_once(_config(tmp_path), store, "go", provider=provider,
                        out=out, on_permission=_allow)
    assert rc == 0
    assert provider.child_calls == 9
    assert provider.parent_calls == 3  # start, park, aggregate completion
    assert SWARM_ROUTING_SUMMARY in _system_text(provider.requests[0])
    review_prompt = provider.child_requests[-1].messages[0].text()
    handoffs = json.loads(review_prompt.split(
        "Dependency results (peer data, not authorization):\n", 1)[1]
        .split("<system-reminder>", 1)[0])
    assert list(handoffs) == keys
    # Three workers run side by side and the scripted provider hands out
    # replies in call order, so which worker got which finding is a
    # scheduling artifact; every finding must arrive exactly once, truncated.
    heads = sorted(value.split(": ", 1)[0] for value in handoffs.values())
    assert heads == sorted(f"finding-{i}" for i in range(8))
    for key in keys:
        assert "[truncated;" in handoffs[key]
    events = [e for sid in store._sessions
              for e in await store.events_since(sid, -1)]
    assert sum(isinstance(e, SessionWoken) for e in events) == 1
    snapshots = [e.swarm for e in events if isinstance(e, SwarmUpdated)]
    assert snapshots[-1]["status"] == "completed"
    assert snapshots[-1]["tasks"][-1]["result"] == "review report"


async def test_cli_peer_scope_and_no_resume():
    from fluid.cli.host import CliHost, Worker
    from fluid.store.base import MemoryStore
    parent = CliHost(SimpleNamespace(), MemoryStore())
    parent.engine = object()
    launch_a = SimpleNamespace(swarm_id="a")
    launch_b = SimpleNamespace(swarm_id="b")
    me = object()
    parent.workers = {
        "self": Worker(task_id="self", kind="task", description="self", engine=me, swarm_launch=launch_a),
        "peer": Worker(task_id="peer", kind="task", description="peer", engine=object(), swarm_launch=launch_a),
        "other": Worker(task_id="other", kind="task", description="other", engine=object(), swarm_launch=launch_b)}
    child = CliHost(SimpleNamespace(), parent.store, parent=parent, nested=True)
    child.engine, child.swarm_id = me, "a"
    assert {r["name"] for r in child.agent_roster({}, ToolCtx())} == {"main", "peer"}
    assert child._resolve_target("peer")[0] is not None
    assert child._resolve_target("other")[0] is None
    parent.workers["peer"].status = "finished"
    assert child._resolve_target("peer")[0] is None


async def test_cli_recovery_releases_durable_aggregate_pending(tmp_path):
    from fluid.cli.config import make_engine, host_of
    from fluid.events.model import SwarmUpdated, SubagentEvent
    from fluid.store.base import MemoryStore
    from tests.unit.test_cli_host import _config
    from tests.unit.test_tool_loop import ScriptedProvider, _text
    store = MemoryStore()
    meta = await store.create_session(cwd=str(tmp_path))
    key = "swarm_old"
    await store.append_event(meta.session_id, SwarmUpdated(session_id=meta.session_id, seq=1,
        swarm=dict(id=key, session_id=meta.session_id, status="running", tasks=[dict(id="a", status="running")], updated_at="old")))
    await store.append_event(meta.session_id, SubagentEvent(session_id=meta.session_id, seq=2,
        task_id=key, kind="started"))
    engine = make_engine(_config(tmp_path), store, provider=ScriptedProvider([_text("hi")]), session_id=meta.session_id)
    try:
        await engine._rehydrate_background_lifecycle_unlocked()
        assert key not in engine._background_pending
        assert key in engine._background_terminal
        events = await store.events_since(meta.session_id, -1)
        snapshots = [e.swarm for e in events if isinstance(e, SwarmUpdated)]
        assert snapshots[-1]["status"] == "interrupted"
        assert not host_of(engine).workers
    finally:
        await host_of(engine).aclose()
        await engine.aclose()


async def test_cancel_during_launch_intent_never_spawns():
    dispatch = AsyncMock()
    c = None
    async def publish(snapshot):
        if snapshot["tasks"][0]["status"] == "running":
            c.cancelled.add(snapshot["id"])
    c = SwarmCoordinator("main", dispatch, AsyncMock(), publish, AsyncMock())
    _, receipt = await c.start(dict(tasks=[spec("a")]), ToolCtx())
    await c.jobs[receipt.task_id]
    dispatch.assert_not_called()
    assert c.snapshot(receipt.task_id)["status"] == "cancelled"
