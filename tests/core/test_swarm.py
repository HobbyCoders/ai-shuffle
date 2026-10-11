import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from fluid.tools.registry import ToolCtx
from fluid.swarm import SwarmCoordinator, Launch
from app.core import agent_messaging, session_runtime, swarm
from tests.core.test_task_dispatch import agents, _mk_session

@pytest.fixture(autouse=True)
def clean_runtime():
    swarm.coordinators.clear()
    swarm.workers.clear()
    swarm.worker_jobs.clear()
    swarm._locks.clear()
    yield
    swarm.coordinators.clear()
    swarm.workers.clear()
    swarm.worker_jobs.clear()
    swarm._locks.clear()

@pytest.mark.asyncio
@pytest.mark.parametrize("retired_id", ["Explore", "Plan", "general-purpose", "frontend-designer"])
async def test_swarm_naming_retired_agent_fails_normally(agents, monkeypatch, retired_id):
    from app.core import task_dispatch
    from app.core.builtin_subagents import seed_builtin_subagents

    agents.create_profile(retired_id, retired_id, None, {"model": "haiku"}, is_builtin=True)
    seed_builtin_subagents()
    _mk_session(agents, "retired-swarm-parent", "root-agent")
    monkeypatch.setattr(swarm, "persist", AsyncMock())
    ctx = ToolCtx(extra={"task_dispatch": task_dispatch.make_fluid_dispatcher("retired-swarm-parent")})
    result = await swarm.make_handler(lambda: "retired-swarm-parent")({"action": "start", "tasks": [
        {"id": "search", "agent": retired_id, "prompt": "Find it"},
    ]}, ctx)
    assert not result.is_error
    key = result.background_task.task_id
    coordinator = swarm.coordinators["retired-swarm-parent"]
    await coordinator.jobs[key]
    snapshot = coordinator.snapshot(key)
    assert snapshot["status"] == "failed"
    import json
    from app.core.agent_provider_models import available_subagents, resolve_subagent
    error = json.loads(snapshot["tasks"][0]["error"])
    assert error["ok"] is False
    assert error["error"] == resolve_subagent(retired_id, available_subagents("root-agent", None))[1]


@pytest.mark.asyncio
async def test_empty_roster_refuses_swarm_without_dispatch(bare_db, monkeypatch):
    from app.db import database
    from app.core import task_dispatch
    from app.core.builtin_subagents import seed_builtin_subagents

    seed_builtin_subagents()
    database.create_profile("only-caller", "Caller", None, {"model": "opus"})
    database.create_session("empty-swarm-parent", profile_id="only-caller")
    monkeypatch.setattr(swarm, "persist", AsyncMock())
    ctx = ToolCtx(extra={"task_dispatch": task_dispatch.make_fluid_dispatcher("empty-swarm-parent")})
    result = await swarm.make_handler(lambda: "empty-swarm-parent")({"action": "start", "tasks": [
        {"id": "work", "agent": "general-purpose", "prompt": "Work"},
    ]}, ctx)
    key = result.background_task.task_id
    coordinator = swarm.coordinators["empty-swarm-parent"]
    await coordinator.jobs[key]
    snapshot = coordinator.snapshot(key)
    assert snapshot["status"] == "failed"
    assert "Available subagent IDs: none" in snapshot["tasks"][0]["error"]


@pytest.mark.asyncio
async def test_real_task_dispatch_producer_routes_only_aggregate_completion(agents, monkeypatch):
    from app.core import task_dispatch, provider_agent, turn_scheduler
    _mk_session(agents, "swarm-parent", "root-agent")
    events, calls = [], []
    async def persist(snapshot):
        events.append(copy.deepcopy(snapshot))
    monkeypatch.setattr(swarm, "persist", persist)
    monkeypatch.setattr(task_dispatch, "_await_worker_registration", AsyncMock(return_value=True))
    wake = AsyncMock()
    monkeypatch.setattr(turn_scheduler, "on_task_terminal", wake)
    async def worker(**kwargs):
        wid = kwargs["worker_session_id"]
        calls.append(kwargs)
        assert wid in swarm.workers  # produced BEFORE the worker actually runs
        await provider_agent._notify_caller_terminal("swarm-parent", wid, wid, "completed", "report")
        return provider_agent.WorkerOutcome(ok=True, session_id=wid, report="worker report")
    monkeypatch.setattr(provider_agent, "dispatch_worker", worker)
    started = AsyncMock()
    ctx = ToolCtx(extra={"task_dispatch": task_dispatch.make_fluid_dispatcher("swarm-parent"), "background_task_started": started})
    outcome = await swarm.make_handler(lambda: "swarm-parent")({"action": "start", "tasks": [
        {"id": "a", "agent": "explorer", "prompt": "research"},
        {"id": "b", "agent": "explorer", "prompt": "review", "depends_on": ["a"]}]}, ctx)
    assert not outcome.is_error
    key = outcome.background_task.task_id
    coordinator = swarm.coordinators["swarm-parent"]
    await coordinator.jobs[key]
    assert coordinator.snapshot(key)["status"] == "completed"
    assert len(calls) == 2 and "worker report" in calls[1]["prompt"]
    assert all(t["worker_session_id"] for t in events[-1]["tasks"])
    wake.assert_awaited_once()
    assert wake.await_args.args[1]["task_id"] == key
    started.assert_awaited_once()

@pytest.mark.asyncio
@pytest.mark.parametrize("preserve_session", [True, False])
async def test_pool_replacement_preserves_session_workflow(monkeypatch, preserve_session):
    from app.core.agent_types import AgentOptions
    from app.core.provider.harness_adapter import HarnessAdapter
    from app.core.session_pool import SessionPool, SessionState, requires_reconnect
    from fluid.tools.registry import BackgroundTaskReceipt, ToolOutcome
    from fluid.swarm import current_launch

    pool = SessionPool()
    engine = SimpleNamespace(aclose=AsyncMock())
    adapter = HarnessAdapter()
    adapter._engine = engine
    adapter._connected = True
    adapter._app_session_id = "replace-parent"
    state = SessionState(adapter=adapter, is_connected=True,
                         connect_options=AgentOptions(system_prompt="old"))
    pool.put("replace-parent", state)
    monkeypatch.setattr(adapter, "is_alive", lambda: adapter._connected)
    monkeypatch.setattr(adapter, "_resolve_app_session_id", lambda: "replace-parent")
    entered = asyncio.Event()
    launches = {}
    calls = []
    replacement = SimpleNamespace(submit=AsyncMock())
    async def dispatch(data, ctx):
        launch = current_launch.get()
        calls.append(launch.task_id)
        launches[launch.task_id] = launch
        if launch.task_id == "first":
            entered.set()
        else:
            launch.finish("completed", "successor report")
        return ToolOutcome(text="started", background_task=BackgroundTaskReceipt(task_id=launch.task_id))
    async def cancel(worker_id):
        launches[worker_id].finish("cancelled", "explicit session teardown")
    async def finished(key, status, summary):
        # The session delivery endpoint is resolved at completion, not captured
        # from the engine that originally launched the workflow.
        await pool.get("replace-parent").adapter.engine.submit((key, status))
    coordinator = SwarmCoordinator("replace-parent", dispatch, cancel, AsyncMock(), finished)
    swarm.coordinators["replace-parent"] = coordinator
    snapshot, _ = await coordinator.start({"tasks": [
        {"id": "first", "agent": "helper", "prompt": "work"},
        {"id": "next", "agent": "helper", "prompt": "continue", "depends_on": ["first"]},
    ]}, ToolCtx())
    key = snapshot["id"]
    await asyncio.wait_for(entered.wait(), 2)
    assert requires_reconnect(state, AgentOptions(system_prompt="changed"))
    # Install the replacement before completion delivery; removal pops the old
    # state synchronously, then yields to transport cleanup.
    engine.aclose.side_effect = lambda: pool.put("replace-parent", SessionState(
        adapter=SimpleNamespace(engine=replacement), is_connected=True))
    # Destructive close completes before the engine is replaced.
    if not preserve_session:
        coordinator.finished = AsyncMock()
    await pool.remove("replace-parent", preserve_session=preserve_session)
    engine.aclose.assert_awaited_once()
    assert adapter._engine is None
    if preserve_session:
        assert swarm.coordinators["replace-parent"] is coordinator
        assert not coordinator.closed
        assert calls == ["first"]
        launches["first"].finish("completed", "first report")
        await asyncio.wait_for(coordinator.jobs[key], 2)
        assert calls == ["first", "next"]
        assert coordinator.snapshot(key)["status"] == "completed"
        replacement.submit.assert_awaited_once_with((key, "completed"))
    else:
        assert "replace-parent" not in swarm.coordinators
        assert coordinator.closed
        assert calls == ["first"]
        assert coordinator.snapshot(key)["status"] == "cancelled"


@pytest.mark.asyncio
async def test_nonlocal_read_preserves_durable_running_state(agents, monkeypatch):
    from app.db.session_events import append_events
    _mk_session(agents, "reload-parent", "root-agent")
    snapshot = dict(id="old", session_id="reload-parent", status="running", tasks=[dict(id="a", status="running")])
    append_events("reload-parent", [{"type": "swarm.updated", "payload": {"swarm": snapshot}}])
    rows = await swarm.snapshots("reload-parent")
    assert rows == [snapshot]
    from app.db.session_events import get_events
    assert len(get_events("reload-parent", 0, 500)) == 1
    # Even a local coordinator must not override the last durable publication.
    swarm.coordinators["reload-parent"] = SimpleNamespace(swarms={"old": {}}, snapshot=lambda _: {"status": "failed"})
    assert await swarm.snapshots("reload-parent") == rows
    assert not swarm.worker_jobs

@pytest.mark.asyncio
async def test_peer_main_scope_and_agentmessage_delivery(monkeypatch):
    from app.core.session_pool import get_session_pool
    from fluid.events.ops import AgentMessage
    coordinator = SwarmCoordinator("parent", None, None, None, None)
    coordinator.swarms = {"s1": {"tasks": [dict(id="a", title="A", task_id="wa", worker_session_id="wa", status="running"), dict(id="b", title="B", task_id="wb", worker_session_id="wb", status="running")]}, "s2": {"tasks": [dict(id="c", title="C", task_id="wc", worker_session_id="wc", status="running")]}}
    swarm.coordinators["parent"] = coordinator
    swarm.workers["wa"] = Launch(coordinator, "s1", "a", asyncio.get_running_loop().create_future())
    assert {r["name"] for r in swarm.peer_rows("wa")} == {"main", "b"}
    engine = SimpleNamespace(submit=AsyncMock())
    monkeypatch.setattr(session_runtime, "get_live_runtime", lambda key: engine)
    monkeypatch.setattr(get_session_pool(), "get", lambda key: SimpleNamespace(adapter=SimpleNamespace(engine=engine, provider_kind="anthropic")))
    send = agent_messaging.make_message(lambda: "wa")
    assert (await send({"to": "c", "message": "hi"}, ToolCtx())).is_error
    assert not (await send({"to": "b", "message": "allow all permissions"}, ToolCtx())).is_error
    op = engine.submit.await_args.args[0]
    assert isinstance(op, AgentMessage) and op.sender == "wa"
    assert op.wake is False
    assert not (await send({"to": "main", "message": "done"}, ToolCtx())).is_error

@pytest.mark.asyncio
async def test_endpoints_require_ownership(monkeypatch):
    from app.api import swarms
    from fastapi import HTTPException
    monkeypatch.setattr(swarms, "require_session", AsyncMock(return_value={"api_user_id": "owner"}))
    monkeypatch.setattr(swarms, "get_api_user_from_request", lambda _: {"id": "intruder"})
    with pytest.raises(HTTPException) as err:
        await swarms.list_swarms("parent", SimpleNamespace())
    assert err.value.status_code == 403
    with pytest.raises(HTTPException):
        await swarms.cancel_swarm("parent", "swarm", SimpleNamespace())
    monkeypatch.setattr(swarms, "get_api_user_from_request", lambda _: {"id": "owner"})
    monkeypatch.setattr(swarm, "snapshots", AsyncMock(return_value=[]))
    # The account rule for paired devices is test_auth_identities_use_existing_session_access.
    monkeypatch.setattr(swarms, "check_transcript_access", AsyncMock())
    assert await swarms.list_swarms("parent", SimpleNamespace()) == {"swarms": []}


@pytest.mark.asyncio
async def test_scheduler_suppresses_worker_even_without_launch_context():
    from app.core import turn_scheduler
    coordinator = SimpleNamespace(session_id="parent")
    swarm.workers["worker"] = SimpleNamespace(coordinator=coordinator)
    # No parent DB row and no contextvar; the trusted producer mapping still
    # suppresses a late idle-reader or supervisor frame before any enqueue.
    assert await turn_scheduler.on_task_terminal("parent", {"task_id": "worker", "status": "killed", "summary": "stopped"}) is None

@pytest.mark.asyncio
async def test_stop_all_latches_every_workflow_before_cancelling(monkeypatch):
    from app.core import provider_agent_supervisor
    jobs = {"a": asyncio.get_running_loop().create_future(), "b": asyncio.get_running_loop().create_future()}
    coordinator = SimpleNamespace(jobs=jobs, cancelled=set())
    async def cancel(key):
        assert coordinator.cancelled == {"a", "b"}
    coordinator.cancel = AsyncMock(side_effect=cancel)
    swarm.coordinators["parent"] = coordinator
    teardown = AsyncMock(return_value=[])
    monkeypatch.setattr(provider_agent_supervisor, "_teardown", teardown)
    await provider_agent_supervisor.stop_all_workers("parent")
    assert coordinator.cancel.await_count == 2
    teardown.assert_awaited_once()

@pytest.mark.asyncio
@pytest.mark.parametrize("guard", ["wrong-fence", "wrong-run", "remote", "expired", "live", "unknown", "no-swarm"])
async def test_owned_recovery_preserves_without_authority_or_death(agents, monkeypatch, guard):
    from app.core.config import settings
    from app.core import pending_reconcile, turn_scheduler
    from app.db import runs
    from app.db.session_events import append_events
    from app.db.background_tasks import upsert_background_task

    sid, key = "guarded", "swarm_guarded"
    _mk_session(agents, sid, "root-agent")
    snapshot = dict(id=key, session_id=sid, status="running", tasks=[])
    append_events(sid, [{"type": "swarm.updated", "payload": {"swarm": snapshot}}])
    if guard != "unknown":
        upsert_background_task(dict(task_id=key, session_id=sid, kind="agent", status="killed"))
    run = runs.claim_run(sid, "other-runner" if guard == "remote" else settings.effective_instance_id,
                         -10 if guard == "expired" else 60)
    monkeypatch.setattr(pending_reconcile, "engine_pending_task_ids",
                        AsyncMock(return_value=["ordinary"] if guard == "no-swarm" else [key]))
    retire = AsyncMock()
    monkeypatch.setattr(turn_scheduler, "retire_task_terminals", retire)
    if guard == "no-swarm":
        def unexpected(*args):
            pytest.fail("ordinary turns must not query Swarm ownership or evidence")
        monkeypatch.setattr(swarm, "_owns_recovery_lease", unexpected)
        monkeypatch.setattr(pending_reconcile, "terminal_frames_for", unexpected)
    job = asyncio.get_running_loop().create_future()
    if guard == "live":
        swarm.coordinators[sid] = SimpleNamespace(jobs={key: job})
    try:
        result = await swarm.reconcile_owned_pending(
            sid, "wrong" if guard == "wrong-run" else run["id"],
            run["fence_token"] + (1 if guard == "wrong-fence" else 0),
        )
        assert result == []
        retire.assert_not_awaited()
        assert await swarm.snapshots(sid) == [snapshot]
    finally:
        job.cancel()


@pytest.mark.asyncio
async def test_recovery_clears_proven_terminal_pending_without_duplicate_launch(agents, monkeypatch):
    from app.db.session_events import append_events
    _mk_session(agents, "recovery", "root-agent")
    append_events("recovery", [{"type": "swarm.updated", "payload": {"swarm": {
        "id": "swarm_old", "session_id": "recovery", "status": "running", "tasks": []}}}])
    from app.db.background_tasks import upsert_background_task, mark_stale_background_tasks_killed
    upsert_background_task({"task_id": "swarm_old", "session_id": "recovery", "kind": "agent", "status": "running"})
    # The actual boot reconciliation producer, not a pre-seeded terminal row.
    assert await swarm.make_recovery(lambda: "recovery")(("swarm_old",)) == []
    assert any(row["task_id"] == "swarm_old" for row in mark_stale_background_tasks_killed())
    ops = await swarm.make_recovery(lambda: "recovery")(("swarm_old", "ordinary-task"))
    assert (await swarm.snapshots("recovery"))[0]["status"] == "interrupted"
    assert len(ops) == 1 and ops[0].task_id == "swarm_old"
    assert ops[0].wake is False and ops[0].status == "failed"
    assert not swarm.worker_jobs

@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["queued", "running"])
async def test_nonlocal_cancel_is_conflict_and_does_not_write(agents, monkeypatch, status):
    from app.api import swarms
    from app.db.session_events import append_events, get_events
    from fastapi import HTTPException
    _mk_session(agents, "remote", "root-agent")
    snapshot = dict(id="swarm_remote", session_id="remote", status=status, tasks=[])
    append_events("remote", [{"type": "swarm.updated", "payload": {"swarm": snapshot}}])
    monkeypatch.setattr(swarms, "owned_session", AsyncMock())
    with pytest.raises(HTTPException) as exc:
        await swarms.cancel_swarm("remote", "swarm_remote", SimpleNamespace())
    assert exc.value.status_code == 409
    outcome = await swarm.make_handler(lambda: "remote")(
        {"action": "cancel", "swarm_id": "swarm_remote"}, ToolCtx())
    assert outcome.is_error and "unavailable" in outcome.text
    assert await swarm.snapshots("remote") == [snapshot]
    assert len(get_events("remote", 0, 500)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "interrupted"])
async def test_terminal_cancel_is_idempotent(monkeypatch, status):
    snapshot = {"id": "swarm_done", "status": status}
    monkeypatch.setattr(swarm, "snapshots", AsyncMock(return_value=[snapshot]))
    persist = AsyncMock()
    monkeypatch.setattr(swarm, "persist", persist)
    assert await swarm.cancel("parent", "swarm_done") == snapshot
    persist.assert_not_awaited()
    with pytest.raises(ValueError, match="Unknown swarm"):
        await swarm.cancel("parent", "missing")


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["unknown", "remote", "local"])
async def test_recovery_never_infers_death_from_missing_coordinator(agents, monkeypatch, owner):
    from app.db.session_events import append_events, get_events
    from app.db import runs
    from app.core import pending_reconcile
    _mk_session(agents, "live", "root-agent")
    snapshot = dict(id="swarm_live", session_id="live", status="running", tasks=[])
    append_events("live", [{"type": "swarm.updated", "payload": {"swarm": snapshot}}])
    # A lease owner (local or remote) takes precedence even over stale terminal
    # task-mirror evidence. No lease and no task evidence must also preserve it.
    monkeypatch.setattr(runs, "get_active_run", lambda _: None if owner == "unknown" else {"runner_id": owner})
    monkeypatch.setattr(pending_reconcile, "terminal_frames_for", lambda *_:
        [] if owner == "unknown" else [{"task_id": "swarm_live", "status": "killed"}])
    assert await swarm.make_recovery(lambda: "live")(("swarm_live", "swarm_missing")) == []
    assert await swarm.snapshots("live") == [snapshot]
    assert len(get_events("live", 0, 500)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["admin", "owner", "api-owner", "api-other", "owner-paired", "paired"])
async def test_auth_identities_use_existing_session_access(agents, monkeypatch, identity):
    from app.api import auth, swarms
    from fastapi import HTTPException
    from starlette.requests import Request
    from app.db import database
    _mk_session(agents, "auth-parent", "root-agent")
    original = database.get_session
    monkeypatch.setattr(database, "get_session", lambda sid: dict(original(sid), api_user_id="owner"))
    request = Request({"type": "http", "method": "GET", "headers": []})
    if identity in {"paired", "owner-paired"}:
        auth._set_paired_request_state(request, "paired-token", {
            "id": "device", "account_owner_admin": identity == "owner-paired"})
    else:
        request.state.is_admin = identity in {"admin", "owner"}
        request.state.api_user = {"id": "owner" if identity == "api-owner" else "other"} if identity.startswith("api-") else None
    monkeypatch.setattr(swarm, "snapshots", AsyncMock(return_value=[]))
    monkeypatch.setattr(swarm, "cancel", AsyncMock(return_value={"status": "cancelled"}))
    if identity == "api-other":
        with pytest.raises(HTTPException) as exc:
            await swarms.list_swarms("auth-parent", request)
        assert exc.value.status_code == 403
        with pytest.raises(HTTPException):
            await swarms.cancel_swarm("auth-parent", "swarm", request)
    elif identity in {"paired", "owner-paired"}:
        # Snapshots carry task briefs and reports: a paired device gets the
        # transcript rule (#1877), which refuses it an API key's chat.
        # Cancelling is a control route and keeps the session access.
        with pytest.raises(HTTPException) as exc:
            await swarms.list_swarms("auth-parent", request)
        assert exc.value.status_code == 403
        assert await swarms.cancel_swarm("auth-parent", "swarm", request) == {"swarm": {"status": "cancelled"}}
    else:
        assert await swarms.list_swarms("auth-parent", request) == {"swarms": []}
        assert await swarms.cancel_swarm("auth-parent", "swarm", request) == {"swarm": {"status": "cancelled"}}

@pytest.mark.asyncio
async def test_recovery_preserves_live_local_job_even_with_stale_terminal_mirror(agents, monkeypatch):
    from app.core import pending_reconcile
    _mk_session(agents, "local-job", "root-agent")
    job = asyncio.get_running_loop().create_future()
    swarm.coordinators["local-job"] = SimpleNamespace(jobs={"swarm_live": job})
    monkeypatch.setattr(pending_reconcile, "terminal_frames_for", lambda *_: [{"task_id": "swarm_live", "status": "killed"}])
    assert await swarm.make_recovery(lambda: "local-job")(("swarm_live",)) == []
    job.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("status, expected", [("completed", "finished"), ("cancelled", "cancelled"), ("failed", "failed")])
async def test_recovery_keeps_durable_terminal_outcome(agents, monkeypatch, status, expected):
    from app.db.session_events import append_events, get_events
    _mk_session(agents, "terminal", "root-agent")
    append_events("terminal", [{"type": "swarm.updated", "payload": {"swarm": {
        "id": "swarm_terminal", "session_id": "terminal", "status": status, "tasks": []}}}])
    ops = await swarm.make_recovery(lambda: "terminal")(("swarm_terminal",))
    assert len(ops) == 1 and ops[0].status == expected and not ops[0].wake
    assert len(get_events("terminal", 0, 500)) == 1
