import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from fluid.swarm import SwarmCoordinator, current_launch, recover, validate
from fluid.tools.registry import BackgroundTaskReceipt, ToolCtx, ToolOutcome


def spec(key, deps=()):
    return dict(id=key, agent="helper", prompt="do " + key, depends_on=list(deps))

@pytest.mark.parametrize("data", [
    {"tasks": []}, {"tasks": [spec("a"), spec("a")]},
    {"tasks": [spec("a", ["missing"])]}, {"tasks": [spec("a", ["a"])]},
    {"tasks": [spec("a", ["b"]), spec("b", ["a"])]},
    {"tasks": [spec("a")], "max_parallel": 0},
    {"tasks": [spec("a")], "max_parallel": 9},
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
