import asyncio
import copy
from pathlib import Path
from typing import Any
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from tests.builtin_fixtures import create_unchanged_builtin
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
    swarm._orphan_free.clear()
    swarm._killed_swarms.clear()
    yield
    swarm.coordinators.clear()
    swarm.workers.clear()
    swarm.worker_jobs.clear()
    swarm._locks.clear()
    swarm._orphan_free.clear()
    swarm._killed_swarms.clear()

@pytest.mark.asyncio
@pytest.mark.parametrize("retired_id", ["Explore", "Plan", "general-purpose", "frontend-designer"])
async def test_swarm_naming_retired_agent_fails_normally(agents, monkeypatch, retired_id):
    from app.core import task_dispatch
    from app.core.builtin_subagents import seed_builtin_subagents

    create_unchanged_builtin(retired_id)
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
    # Without a pending receipt only an open (orphan-candidate) workflow may
    # cost evidence or lease reads; a settled one costs neither.
    status = "completed" if guard == "no-swarm" else "running"
    snapshot = dict(id=key, session_id=sid, status=status, tasks=[])
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


async def _start_blocked_swarm(
    agents: Any, monkeypatch: pytest.MonkeyPatch, sid: str,
    ids: tuple[str, ...] = ("a", "b"),
) -> tuple[str, dict[str, str]]:
    """Start a real Swarm whose workers are mid-flight; returns (key, worker ids)."""
    from app.core import task_dispatch, provider_agent, turn_scheduler
    from app.core.task_registry import task_registry
    _mk_session(agents, sid, "root-agent")
    monkeypatch.setattr(task_dispatch, "_await_worker_registration", AsyncMock(return_value=True))
    monkeypatch.setattr(turn_scheduler, "_on_task_terminal_locked", AsyncMock())
    gate = asyncio.Event()
    async def worker(**kwargs: Any) -> None:
        task_registry.start_task(sid, kwargs["worker_session_id"], "agent", "worker")
        await gate.wait()
    monkeypatch.setattr(provider_agent, "dispatch_worker", worker)
    ctx = ToolCtx(extra={"task_dispatch": task_dispatch.make_fluid_dispatcher(sid)})
    outcome = await swarm.make_handler(lambda: sid)({"action": "start", "tasks": [
        {"id": item, "agent": "explorer", "prompt": f"work {item}"} for item in ids]}, ctx)
    assert not outcome.is_error
    key = outcome.background_task.task_id
    for _ in range(200):
        rows = {t["id"]: t for t in (await swarm.snapshots(sid))[0]["tasks"]}
        if all(rows[item]["worker_session_id"] for item in ids):
            break
        await asyncio.sleep(0.01)
    return key, {item: rows[item]["worker_session_id"] for item in ids}


async def _retry(sid: str, first_worker: str, retry_worker: str) -> None:
    """The dispatch producer's automatic retry: it rebinds the item to a new worker."""
    from fluid.swarm import current_launch
    from app.core.task_registry import task_registry
    token = current_launch.set(swarm.workers[first_worker])
    try:
        await swarm.bind_worker(retry_worker, sid)
    finally:
        current_launch.reset(token)
    task_registry.complete_task(sid, first_worker, "error", summary="provider failed to start")
    task_registry.start_task(sid, retry_worker, "agent", "worker retry")


async def _restart(sid: str, *, hydrate: bool = True) -> None:
    """What an unclean exit plus boot reconcile leaves: no coordinator, reaped rows."""
    from app.core import task_persistence
    from app.core.task_registry import task_registry
    from app.db.background_tasks import mark_stale_background_tasks_killed
    coordinator = swarm.coordinators[sid]
    coordinator.publish = AsyncMock()  # a dead process publishes nothing more
    coordinator.finished = AsyncMock()
    coordinator.dispatch = AsyncMock()
    for job in coordinator.jobs.values():
        job.cancel()
    await asyncio.gather(*coordinator.jobs.values(), return_exceptions=True)
    for job in swarm.worker_jobs.values():
        job.cancel()
    await asyncio.gather(*swarm.worker_jobs.values(), return_exceptions=True)
    swarm.coordinators.clear()
    swarm.workers.clear()
    swarm.worker_jobs.clear()
    await task_persistence.flush()
    task_registry.clear_session(sid)
    task_registry._recent_terminal.pop(sid, None)
    task_registry._hydrated.discard(sid)
    assert mark_stale_background_tasks_killed()
    if hydrate:
        await task_registry.ensure_hydrated(sid)


async def _finish_resumed_worker(
    sid: str, worker_id: str, tmp_path: Path, *, report_back: bool = False,
) -> None:
    """A worker continued after the restart (SendMessage) finishes with a report.

    ``report_back`` also delivers its completion to the parent the way the
    resume path does (``turn_scheduler.on_task_terminal``).
    """
    from app.core import task_persistence, turn_scheduler
    from app.core.task_registry import task_registry
    report = tmp_path / f"{worker_id}.md"
    report.write_text(f"report of {worker_id}", encoding="utf-8")
    task_registry.resume_task(sid, worker_id)
    task_registry.complete_task(sid, worker_id, "completed", summary="done",
                                report_path=str(report), report_chars=21)
    await task_persistence.flush()
    if report_back:
        await turn_scheduler.on_task_terminal(sid, {
            "task_id": worker_id, "tool_use_id": worker_id, "status": "completed",
            "summary": "done", "agent_message_turn_id": f"turn-{worker_id}",
            "report_path": str(report)})


@pytest.mark.asyncio
async def test_restart_orphan_settles_from_worker_outcomes(
        agents: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from app.core.config import settings
    from app.db import runs
    sid = "orphan-parent"
    key, wids = await _start_blocked_swarm(agents, monkeypatch, sid)
    await _restart(sid)
    await _finish_resumed_worker(sid, wids["a"], tmp_path)
    assert (await swarm.snapshots(sid))[0]["status"] == "running"  # reads never recover
    # The coordinator's own next turn: its lease is live, so no "no active run".
    run = runs.claim_run(sid, settings.effective_instance_id, 60)
    await swarm.reconcile_owned_pending(sid, run["id"], run["fence_token"])
    snapshot = (await swarm.snapshots(sid))[0]
    assert snapshot["id"] == key and snapshot["status"] == "interrupted"
    rows = {t["id"]: t for t in snapshot["tasks"]}
    assert rows["a"]["status"] == "completed"
    assert rows["a"]["result"] == f"report of {wids['a']}"
    assert rows["b"]["status"] == "interrupted"


@pytest.mark.asyncio
async def test_boot_settles_orphan_completed_when_every_worker_finished(
        agents: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from app.core import startup_reconcile
    sid = "orphan-done"
    _, wids = await _start_blocked_swarm(agents, monkeypatch, sid)
    await _restart(sid)
    for wid in wids.values():
        await _finish_resumed_worker(sid, wid, tmp_path)
    assert await startup_reconcile._settle_orphaned_swarms() == 1
    snapshot = (await swarm.snapshots(sid))[0]
    assert snapshot["status"] == "completed"
    assert {t["status"] for t in snapshot["tasks"]} == {"completed"}


@pytest.mark.asyncio
async def test_boot_then_resumed_workers_write_back_and_complete(
        agents: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from app.core import startup_reconcile
    sid = "orphan-late"
    key, wids = await _start_blocked_swarm(agents, monkeypatch, sid)
    await _restart(sid)
    # Boot first: nothing is running yet, so the workflow settles interrupted.
    assert await startup_reconcile._settle_orphaned_swarms() == 1
    assert sid in swarm._orphan_free
    assert (await swarm.snapshots(sid))[0]["status"] == "interrupted"
    # Then each worker is continued with SendMessage and reports back.
    await _finish_resumed_worker(sid, wids["a"], tmp_path, report_back=True)
    snapshot = (await swarm.snapshots(sid))[0]
    rows = {t["id"]: t for t in snapshot["tasks"]}
    assert snapshot["status"] == "interrupted"
    assert rows["a"]["status"] == "completed"
    assert rows["a"]["result"] == f"report of {wids['a']}"
    assert rows["b"]["status"] == "interrupted"
    await _finish_resumed_worker(sid, wids["b"], tmp_path, report_back=True)
    snapshot = (await swarm.snapshots(sid))[0]
    assert snapshot["id"] == key and snapshot["status"] == "completed"
    assert {t["status"] for t in snapshot["tasks"]} == {"completed"}


@pytest.mark.asyncio
async def test_restart_after_retry_reads_the_retry_worker(
        agents: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from app.core import task_persistence
    from app.core.task_registry import task_registry
    sid = "orphan-retry"
    key, wids = await _start_blocked_swarm(agents, monkeypatch, sid)
    await _retry(sid, wids["a"], "retry-a")
    await task_persistence.flush()
    row = {t["id"]: t for t in (await swarm.snapshots(sid))[-1]["tasks"]}["a"]
    assert row["task_id"] == wids["a"] and row["worker_session_id"] == "retry-a"
    await _restart(sid)
    task_registry.resume_task(sid, "retry-a")  # the retry is still working
    stopped = []
    async def stop(worker_id: str, reason: str) -> None:
        stopped.append(worker_id)
    monkeypatch.setattr(swarm, "_stop_item", stop)
    assert await swarm.settle_orphans(sid) == []
    snapshot = await swarm.cancel(sid, key)
    assert stopped == ["retry-a"]
    assert {t["id"]: t["status"] for t in snapshot["tasks"]} == {"a": "cancelled", "b": "cancelled"}
    # And a retry that completed carries its own report.
    sid2 = "orphan-retry-done"
    _, wids2 = await _start_blocked_swarm(agents, monkeypatch, sid2)
    await _retry(sid2, wids2["a"], "retry-a2")
    await task_persistence.flush()
    await _restart(sid2)
    await _finish_resumed_worker(sid2, "retry-a2", tmp_path)
    settled = await swarm.settle_orphans(sid2)
    rows = {t["id"]: t for t in settled[0]["tasks"]}
    assert rows["a"]["status"] == "completed" and rows["a"]["result"] == "report of retry-a2"


@pytest.mark.asyncio
async def test_unreadable_worker_outcome_defers_settlement_and_cancel(
        agents: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.db import background_tasks
    from app.db.session_events import get_events
    sid = "orphan-unreadable"
    key, _ = await _start_blocked_swarm(agents, monkeypatch, sid)
    await _restart(sid, hydrate=False)  # nothing in memory: the row is the only truth
    original = background_tasks.get_background_task
    def unreadable(task_id: str, session_id: str) -> dict | None:
        if not task_id.startswith("swarm_"):
            raise RuntimeError("database unavailable")
        return original(task_id, session_id)
    monkeypatch.setattr(background_tasks, "get_background_task", unreadable)
    events = len(get_events(sid, 0, 500))
    stop = AsyncMock()
    monkeypatch.setattr(swarm, "_stop_item", stop)
    assert await swarm.settle_orphans(sid) == []
    assert sid not in swarm._orphan_free
    with pytest.raises(swarm.SwarmUnavailableError, match="Try again"):
        await swarm.cancel(sid, key)
    stop.assert_not_awaited()
    assert len(get_events(sid, 0, 500)) == events
    assert (await swarm.snapshots(sid))[0]["status"] == "running"
    monkeypatch.setattr(background_tasks, "get_background_task", original)
    assert [s["status"] for s in await swarm.settle_orphans(sid)] == ["interrupted"]


@pytest.mark.asyncio
async def test_orphan_with_a_running_worker_is_not_settled(
        agents: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from app.core.task_registry import task_registry
    sid = "orphan-busy"
    _, wids = await _start_blocked_swarm(agents, monkeypatch, sid)
    await _restart(sid)
    await _finish_resumed_worker(sid, wids["a"], tmp_path)
    task_registry.resume_task(sid, wids["b"])  # continued and still working
    assert await swarm.settle_orphans(sid) == []
    assert (await swarm.snapshots(sid))[0]["status"] == "running"
    # Once that worker ends, a later pass settles the workflow.
    task_registry.complete_task(sid, wids["b"], "error", summary="failed")
    settled = await swarm.settle_orphans(sid)
    assert [s["status"] for s in settled] == ["interrupted"]


@pytest.mark.asyncio
async def test_live_coordinator_is_never_settled_and_cancels_normally(
        agents: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import startup_reconcile
    from app.db.background_tasks import upsert_background_task
    sid = "orphan-live"
    key, wids = await _start_blocked_swarm(agents, monkeypatch, sid)
    # Even stale terminal evidence for the key cannot outrank a live coordinator.
    upsert_background_task(dict(task_id=key, session_id=sid, kind="agent", status="killed"))
    assert await swarm.settle_orphans(sid) == []
    assert await startup_reconcile._settle_orphaned_swarms() == 0
    assert await swarm.record_worker_completion(sid, wids["a"]) == []
    assert (await swarm.snapshots(sid))[0]["status"] == "running"
    stopped = []
    async def stop(worker_id: str, reason: str) -> None:
        stopped.append(worker_id)
        swarm.workers[worker_id].finish("cancelled", "stopped")
    monkeypatch.setattr(swarm, "_stop_item", stop)
    snapshot = await swarm.cancel(sid, key)
    assert sorted(stopped) == sorted(wids.values())
    await swarm.coordinators[sid].jobs[key]
    assert (await swarm.snapshots(sid))[-1]["status"] == "cancelled"
    assert snapshot["id"] == key


@pytest.mark.asyncio
async def test_cancel_orphan_returns_cancelled_snapshot_not_conflict(
        agents: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from app.api import swarms
    from app.core.task_registry import task_registry
    sid = "orphan-cancel"
    key, wids = await _start_blocked_swarm(agents, monkeypatch, sid)
    await _restart(sid)
    await _finish_resumed_worker(sid, wids["a"], tmp_path)
    task_registry.resume_task(sid, wids["b"])  # still live: stopped individually
    stopped = []
    async def stop(worker_id: str, reason: str) -> None:
        stopped.append((worker_id, reason))
    monkeypatch.setattr(swarm, "_stop_item", stop)
    monkeypatch.setattr(swarms, "owned_session", AsyncMock())
    result = await swarms.cancel_swarm(sid, key, SimpleNamespace())
    snapshot = result["swarm"]
    assert snapshot["status"] == "cancelled"
    rows = {t["id"]: t for t in snapshot["tasks"]}
    assert rows["a"]["status"] == "completed" and rows["b"]["status"] == "cancelled"
    assert stopped == [(wids["b"], "swarm_cancelled")]
    assert (await swarm.snapshots(sid))[0]["status"] == "cancelled"
    # Idempotent: a second cancel returns the same terminal snapshot.
    assert (await swarm.cancel(sid, key))["status"] == "cancelled"


@pytest.mark.asyncio
async def test_orphan_cancel_rechecks_ownership_after_its_reads(
        agents: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.task_registry import task_registry
    from app.db import runs
    from app.db.session_events import get_events
    sid = "orphan-claimed"
    key, wids = await _start_blocked_swarm(agents, monkeypatch, sid)
    await _restart(sid)
    task_registry.resume_task(sid, wids["a"])
    original = swarm._reconcile_items
    def claimed_meanwhile(*args: Any, **kwargs: Any) -> tuple[dict, list[dict], bool]:
        runs.claim_run(sid, "other-runner", 60)  # another runner takes the session
        return original(*args, **kwargs)
    monkeypatch.setattr(swarm, "_reconcile_items", claimed_meanwhile)
    stop = AsyncMock()
    monkeypatch.setattr(swarm, "_stop_item", stop)
    events = len(get_events(sid, 0, 500))
    with pytest.raises(swarm.SwarmUnavailableError):
        await swarm.cancel(sid, key)
    stop.assert_not_awaited()
    assert len(get_events(sid, 0, 500)) == events


@pytest.mark.asyncio
async def test_orphan_owned_by_another_runner_is_left_alone(
        agents: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from app.db import runs
    sid = "orphan-remote"
    key, wids = await _start_blocked_swarm(agents, monkeypatch, sid)
    await _restart(sid)
    runs.claim_run(sid, "other-runner", 60)  # handed off: may run there
    assert await swarm.settle_orphans(sid) == []
    await _finish_resumed_worker(sid, wids["a"], tmp_path, report_back=True)
    with pytest.raises(swarm.SwarmUnavailableError):
        await swarm.cancel(sid, key)
    assert (await swarm.snapshots(sid))[0]["status"] == "running"


async def _cancel_with_claim_during_stop(
        agents: Any, monkeypatch: pytest.MonkeyPatch, sid: str, *, takeover: bool,
) -> tuple[str, list[Any], int]:
    """Cancel an orphan while another runner tries to claim the session as its
    live worker is being stopped, after cancel's last authority read."""
    from app.core.task_registry import task_registry
    from app.db import runs
    from app.db.base import get_db
    from app.db.session_events import get_events
    key, wids = await _start_blocked_swarm(agents, monkeypatch, sid)
    await _restart(sid)
    task_registry.resume_task(sid, wids["a"])
    claims = []
    async def stop(worker_id: str, reason: str) -> None:
        if takeover:  # the repair's lease lapsed meanwhile: a takeover reaps it
            with get_db() as conn:
                conn.cursor().execute(
                    "UPDATE runs SET lease_expires_at = NOW() + (%s * INTERVAL '1 second') "
                    "WHERE session_id = %s AND state = 'active'", (-60, sid))
        claims.append(runs.claim_run(sid, "other-runner", 60))
    monkeypatch.setattr(swarm, "_stop_item", stop)
    return key, claims, len(get_events(sid, 0, 500))


@pytest.mark.asyncio
async def test_orphan_cancel_holds_its_lease_while_stopping_workers(
        agents: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.db import runs
    sid = "orphan-held"
    key, claims, _ = await _cancel_with_claim_during_stop(
        agents, monkeypatch, sid, takeover=False)
    snapshot = await swarm.cancel(sid, key)
    assert claims == [None]  # nobody can claim the session mid-cancel
    assert snapshot["status"] == "cancelled"
    assert (await swarm.snapshots(sid))[0]["status"] == "cancelled"
    assert runs.get_active_run(sid) is None  # the repair lease was released


@pytest.mark.asyncio
async def test_orphan_cancel_lands_nothing_after_a_takeover(
        agents: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.db import runs
    from app.db.session_events import get_events
    sid = "orphan-taken"
    key, claims, events = await _cancel_with_claim_during_stop(
        agents, monkeypatch, sid, takeover=True)
    with pytest.raises(swarm.SwarmUnavailableError):
        await swarm.cancel(sid, key)
    assert claims[0] is not None and claims[0]["runner_id"] == "other-runner"
    assert len(get_events(sid, 0, 500)) == events
    assert (await swarm.snapshots(sid))[0]["status"] == "running"
    assert runs.get_active_run(sid)["runner_id"] == "other-runner"


@pytest.mark.asyncio
async def test_settlement_lands_nothing_after_a_takeover(
        agents: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.db import runs
    from app.db.base import get_db
    from app.db.session_events import get_events
    sid = "orphan-settle-taken"
    _, _ = await _start_blocked_swarm(agents, monkeypatch, sid)
    await _restart(sid)
    original = swarm.persist
    async def taken_over(snapshot: dict, *, run_id: str | None = None,
                         fence_token: int | None = None) -> None:
        with get_db() as conn:
            conn.cursor().execute(
                "UPDATE runs SET lease_expires_at = NOW() + (%s * INTERVAL '1 second') "
                "WHERE session_id = %s AND state = 'active'", (-60, sid))
        assert runs.claim_run(sid, "other-runner", 60) is not None
        await original(snapshot, run_id=run_id, fence_token=fence_token)
    monkeypatch.setattr(swarm, "persist", taken_over)
    events = len(get_events(sid, 0, 500))
    assert await swarm.settle_orphans(sid) == []
    assert sid not in swarm._orphan_free
    assert len(get_events(sid, 0, 500)) == events


@pytest.mark.asyncio
async def test_completion_of_ordinary_work_skips_swarm_reads(
        agents: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.db import background_tasks
    _mk_session(agents, "plain-parent", "root-agent")
    calls = []
    original = background_tasks.list_sessions_with_killed_swarms
    def counted(*args: Any, **kwargs: Any) -> list[str]:
        calls.append(args)
        return original(*args, **kwargs)
    monkeypatch.setattr(background_tasks, "list_sessions_with_killed_swarms", counted)
    snapshots = AsyncMock()
    monkeypatch.setattr(swarm, "snapshots", snapshots)
    for worker in ("w1", "w2", "w3"):
        assert await swarm.record_worker_completion("plain-parent", worker) == []
    assert len(calls) == 1  # one gate read per session per process
    snapshots.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_during_lease_claim_releases_the_claimed_run(
        agents: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading
    from app.core.config import settings
    from app.core.task_registry import task_registry
    from app.db import runs
    sid = "orphan-claim-cancelled"
    key, wids = await _start_blocked_swarm(agents, monkeypatch, sid)
    await _restart(sid)
    task_registry.resume_task(sid, wids["a"])
    monkeypatch.setattr(swarm, "_stop_item", AsyncMock())
    entered, gate = threading.Event(), threading.Event()
    original = runs.claim_run
    def slow_claim(session_id: str, runner_id: str, ttl_seconds: int) -> dict | None:
        entered.set()
        assert gate.wait(10)
        return original(session_id, runner_id, ttl_seconds)  # commits after the cancel
    monkeypatch.setattr(runs, "claim_run", slow_claim)
    cancelling = asyncio.create_task(swarm.cancel(sid, key))
    assert await asyncio.to_thread(entered.wait, 10)
    cancelling.cancel()
    await asyncio.sleep(0)
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await cancelling
    assert runs.get_active_run(sid) is None  # the claim was released, not abandoned
    monkeypatch.setattr(runs, "claim_run", original)
    turn_run = runs.claim_run(sid, settings.effective_instance_id, 60)
    assert turn_run is not None  # the session's next turn can claim at once
    assert (await swarm.snapshots(sid))[0]["status"] == "running"  # nothing landed


@pytest.mark.asyncio
async def test_cancel_during_lease_release_still_releases_and_propagates(
        agents: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading
    from app.core.task_registry import task_registry
    from app.db import runs
    sid = "orphan-release-cancelled"
    key, wids = await _start_blocked_swarm(agents, monkeypatch, sid)
    await _restart(sid)
    task_registry.resume_task(sid, wids["a"])
    monkeypatch.setattr(swarm, "_stop_item", AsyncMock())
    entered, gate = threading.Event(), threading.Event()
    original = runs.release_run
    def slow_release(run_id: str, fence_token: int) -> bool:
        entered.set()
        assert gate.wait(10)
        return original(run_id, fence_token)
    monkeypatch.setattr(runs, "release_run", slow_release)
    cancelling = asyncio.create_task(swarm.cancel(sid, key))
    assert await asyncio.to_thread(entered.wait, 10)  # the cancelled snapshot landed
    cancelling.cancel()
    await asyncio.sleep(0)
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await cancelling  # the caller learns it was cancelled
    assert runs.get_active_run(sid) is None  # and the lease was still released
