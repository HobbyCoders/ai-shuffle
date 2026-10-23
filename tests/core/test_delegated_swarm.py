"""Swarm on the delegated (Claude Code CLI) leg.

The CLI reaches the app's builtins through fluid's bundled bridge, so Swarm
rides the ``builtins`` server as ``mcp__builtins__Swarm``: fluid's Swarm
handler over the same ``app.core.swarm`` host and Task dispatcher the native
leg uses. These tests pin the surface (offered when the profile allows Swarm
and Task, withheld otherwise, shadowed on native legs) and that a bridged start
launches its items, publishes ``swarm.updated`` snapshots and wakes the parent
once.
"""
from __future__ import annotations

import copy
import json
from unittest.mock import AsyncMock

import pytest

import app.core.provider.harness_adapter as ha
from app.core import swarm
from app.core.provider.types import SessionOptions
from tests.core.test_task_dispatch import _mk_session, agents  # noqa: F401 — fixture

PARENT = "delegated-swarm-parent"


@pytest.fixture(autouse=True)
def clean_runtime():
    for table in (swarm.coordinators, swarm.workers, swarm.worker_jobs, swarm._locks):
        table.clear()
    yield
    for table in (swarm.coordinators, swarm.workers, swarm.worker_jobs, swarm._locks):
        table.clear()


@pytest.fixture(autouse=True)
def _cli_search(monkeypatch):
    monkeypatch.setattr(ha, "_cli_ships_search_tools", lambda: True)


def _server(session_id=PARENT, profile_id="root-agent", cwd=None):
    from app.mcp.builtin_tools import create_builtin_tools_server

    server = create_builtin_tools_server(
        session_id=session_id, profile_id=profile_id, is_admin=True,
        model="opus", cwd=cwd, workflow_guidance=True,
    )
    assert server is not None
    return server


def _tools(server):
    from app.core.provider.sdk_tool_records import recorded_sdk_server_tools

    return {tool.name: tool for tool in recorded_sdk_server_tools(server) or ()}


def _options(tmp_path, server, **kw):
    options = SessionOptions(
        model="opus", cwd=str(tmp_path), mcp_servers={"builtins": server}, **kw,
    )
    options.extra["session_mode"] = "code"
    options.extra["automatic_workflows"] = True
    return options


# ─── The bridged tool ────────────────────────────────────────────────────────


def test_builtins_server_serves_swarm_with_the_native_spec(agents):  # noqa: F811
    from fluid.tools.builtins import swarm as fluid_swarm
    from fluid.tools.fragments import SWARM_SCOPE_RULES

    tools = _tools(_server())
    bridged = tools["Swarm"]
    task = tools["Task"]
    # The native leg's text: fluid's description plus the scope rules its
    # prompt leaves to Swarm's docs, and Task's ``worker`` on every item.
    assert bridged.description == f"{fluid_swarm.describe()} {SWARM_SCOPE_RULES}"
    worker = (task.input_schema.get("properties") or {}).get("worker")
    assert bridged.input_schema == fluid_swarm.input_schema(worker)


def test_swarm_risk_matches_fluid():
    from app.core.provider.fluid_tool_bridge import (
        BUILTINS_RISK_CLASSIFIERS,
        BUILTINS_RISK_MAP,
    )

    assert BUILTINS_RISK_MAP["Swarm"] == "write"
    assert BUILTINS_RISK_CLASSIFIERS["Swarm"]({"action": "status"}) == "safe"
    assert BUILTINS_RISK_CLASSIFIERS["Swarm"]({"action": "start"}) == "write"


# ─── The delegated surface ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delegated_surface_offers_swarm_when_allowed(agents, tmp_path):  # noqa: F811
    server = _server(cwd=str(tmp_path))
    for kw in ({}, {"tool_allowlist": ["Read", "Bash", "Task", "Swarm"]}):
        options = _options(tmp_path, server, **kw)
        registry = await ha.HarnessAdapter()._build_registry(options, native=False)
        assert registry.get("mcp__builtins__Swarm") is not None
        assert ha._delegated_offered(options, registry)("Swarm") is True, kw
        assert "mcp__builtins__Swarm" not in ha._delegated_disallowed(options)


@pytest.mark.asyncio
@pytest.mark.parametrize("kw", [
    {"disallowed_tools": ["Swarm"]},
    {"disallowed_tools": ["mcp__builtins__Swarm"]},
    # Every item runs through Task: without it Swarm could only refuse.
    {"disallowed_tools": ["Task"]},
    {"tool_allowlist": ["Read", "Bash", "Task"]},
    {"tool_allowlist": ["Read", "Bash", "Swarm"]},
])
async def test_delegated_surface_withholds_swarm_when_denied(agents, tmp_path, kw):  # noqa: F811
    options = _options(tmp_path, _server(cwd=str(tmp_path)), **kw)
    registry = await ha.HarnessAdapter()._build_registry(options, native=False)
    offered = ha._delegated_offered(options, registry)
    assert offered("Swarm") is False
    permit = ha._delegated_tool_predicate(options)
    assert permit is not None and not permit("mcp__builtins__Swarm")
    text = ha._delegated_append_text("APPEND", options, registry) or ""
    from fluid.tools.fragments import SWARM_ROUTING_BRIEF

    assert SWARM_ROUTING_BRIEF not in text


@pytest.mark.asyncio
async def test_native_leg_keeps_fluids_own_swarm(agents, tmp_path):  # noqa: F811
    options = _options(tmp_path, _server(cwd=str(tmp_path)))
    registry = await ha.HarnessAdapter()._build_registry(options, native=True)
    assert registry.get("Swarm") is not None
    assert registry.get("mcp__builtins__Swarm") is None


@pytest.mark.asyncio
async def test_delegated_append_routes_to_the_bridged_swarm(agents, tmp_path):  # noqa: F811
    from fluid.tools.fragments import SWARM_ROUTING_BRIEF

    options = _options(tmp_path, _server(cwd=str(tmp_path)))
    registry = await ha.HarnessAdapter()._build_registry(options, native=False)
    text = ha._delegated_append_text("APPEND", options, registry) or ""
    assert SWARM_ROUTING_BRIEF in text
    assert "__Swarm" in text  # the app-tools note names the bridged tool
    assert text == ha._delegated_append_text("APPEND", options, registry)


# ─── A bridged start ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delegated_swarm_start_launches_items_and_wakes_once(agents, monkeypatch):  # noqa: F811
    from app.core import provider_agent, task_dispatch, turn_scheduler
    from fluid.tools.registry import current_tool_use_id

    _mk_session(agents, PARENT, "root-agent")
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
        assert wid in swarm.workers  # bound to its workflow before it runs
        await provider_agent._notify_caller_terminal(PARENT, wid, wid, "completed", "report")
        return provider_agent.WorkerOutcome(ok=True, session_id=wid, report=f"report {len(calls)}")

    monkeypatch.setattr(provider_agent, "dispatch_worker", worker)
    dispatched = []
    real_dispatch = task_dispatch.dispatch_task_tool

    async def spy(args, **kwargs):
        dispatched.append((dict(args), kwargs))
        return await real_dispatch(args, **kwargs)

    monkeypatch.setattr(task_dispatch, "dispatch_task_tool", spy)

    handler = _tools(_server())["Swarm"].handler
    token = current_tool_use_id.set("toolu_swarm_call")
    try:
        result = await handler({"action": "start", "title": "Review", "tasks": [
            {"id": "a", "agent": "helper", "prompt": "research", "model": "sonnet"},
            {"id": "b", "agent": "helper", "prompt": "review", "depends_on": ["a"],
             "effort": "low"},
        ]})
    finally:
        current_tool_use_id.reset(token)
    assert not result.get("isError"), result
    started = json.loads(result["content"][0]["text"])["swarm"]
    key = started["id"]
    assert started["title"] == "Review"
    coordinator = swarm.coordinators[PARENT]
    await coordinator.jobs[key]

    snapshot = coordinator.snapshot(key)
    assert snapshot["status"] == "completed"
    # Both items ran, in dependency order, with their own model and effort.
    assert len(calls) == 2 and "report 1" in calls[1]["prompt"]
    assert dispatched[0][0]["model"] == "sonnet"
    assert dispatched[1][0]["effort"] == "low"
    # Every item's worker card joins the Swarm call, as on the native leg.
    assert {kw["launch_tool_use_id"] for _, kw in dispatched} == {"toolu_swarm_call"}
    # swarm.updated snapshots: queued first, completed last, every item bound.
    assert events[0]["status"] == "queued" and events[-1]["status"] == "completed"
    assert all(t["worker_session_id"] for t in events[-1]["tasks"])
    # One completion wake for the whole workflow, not one per item.
    wake.assert_awaited_once()
    assert wake.await_args.args[0] == PARENT
    assert wake.await_args.args[1]["task_id"] == key

    monkeypatch.setattr(swarm, "snapshots", AsyncMock(return_value=[snapshot]))
    status = await _tools(_server())["Swarm"].handler({"action": "status", "swarm_id": key})
    assert json.loads(status["content"][0]["text"])["swarm"]["status"] == "completed"


@pytest.mark.asyncio
async def test_delegated_swarm_cancel_and_close_stop_running_items(agents, monkeypatch):  # noqa: F811
    import asyncio

    from app.core import provider_agent, task_dispatch, turn_scheduler

    _mk_session(agents, PARENT, "root-agent")
    monkeypatch.setattr(swarm, "persist", AsyncMock())
    monkeypatch.setattr(task_dispatch, "_await_worker_registration", AsyncMock(return_value=True))
    monkeypatch.setattr(turn_scheduler, "on_task_terminal", AsyncMock())
    running = asyncio.Event()

    async def worker(**kwargs):
        running.set()
        await asyncio.Event().wait()  # until stopped

    monkeypatch.setattr(provider_agent, "dispatch_worker", worker)
    stopped = []

    async def stop(worker_id, reason):
        stopped.append(reason)
        job = swarm.worker_jobs.get(worker_id)
        if job is not None:
            job.cancel()

    monkeypatch.setattr(swarm, "_stop_item", stop)
    handler = _tools(_server())["Swarm"].handler

    first = json.loads((await handler({"action": "start", "tasks": [
        {"id": "a", "agent": "helper", "prompt": "work"},
        {"id": "b", "agent": "helper", "prompt": "after", "depends_on": ["a"]},
    ]}))["content"][0]["text"])["swarm"]["id"]
    await asyncio.wait_for(running.wait(), 5)
    cancelled = await handler({"action": "cancel", "swarm_id": first})
    assert not cancelled.get("isError"), cancelled
    await asyncio.wait_for(swarm.coordinators[PARENT].jobs[first], 5)
    snapshot = swarm.coordinators[PARENT].snapshot(first)
    assert snapshot["status"] == "cancelled"
    assert {t["id"]: t["status"] for t in snapshot["tasks"]}["b"] == "cancelled"
    assert stopped == ["swarm_cancelled"]

    # Closing the session (HarnessAdapter.disconnect) stops what still runs.
    running.clear()
    second = json.loads((await handler({"action": "start", "tasks": [
        {"id": "c", "agent": "helper", "prompt": "work"},
    ]}))["content"][0]["text"])["swarm"]["id"]
    await asyncio.wait_for(running.wait(), 5)
    coordinator = swarm.coordinators[PARENT]
    await asyncio.wait_for(swarm.close(PARENT), 5)
    assert coordinator.jobs[second].done()
    assert stopped[-1] == "session_closed"
    assert PARENT not in swarm.coordinators


@pytest.mark.asyncio
async def test_delegated_swarm_without_a_session_refuses():
    from app.mcp.builtin_tools import create_builtin_tools_server

    handler = _tools(create_builtin_tools_server(is_admin=True))["Swarm"].handler
    result = await handler({"action": "start", "tasks": [
        {"id": "a", "agent": "helper", "prompt": "work"},
    ]})
    assert result.get("isError") is True


# ─── The bridge itself withholds Swarm without Task ─────────────────────────


def _bridge(options, registry):
    """The DelegatedLoop bridge filter as connect wires it, and what it serves."""
    from fluid.engine.delegated import DelegatedLoop

    loop = DelegatedLoop.__new__(DelegatedLoop)
    loop.allowed_tools = ha._delegated_allowed(options)
    loop.disallowed_tools = ha._delegated_disallowed(options)
    served = {
        s.name for s in registry.list_tools(loaded_only=True)
        if s.source in ("inproc", "builtin") and loop._bridge_tool_permitted(s.name)
    }
    return loop._bridge_tool_permitted, served


@pytest.mark.asyncio
@pytest.mark.parametrize("kw", [
    {"disallowed_tools": ["Task"]},
    {"tool_allowlist": ["Read", "Swarm"]},
])
async def test_bridge_never_serves_swarm_without_task(agents, tmp_path, kw):  # noqa: F811
    """The bridge serves its tools without PreToolUse, so the Task rule must
    reach its own filter, not only the prompt and grant predicate."""
    options = _options(tmp_path, _server(cwd=str(tmp_path)), **kw)
    registry = await ha.HarnessAdapter()._build_registry(options, native=False)
    permitted, served = _bridge(options, registry)
    assert permitted("mcp__builtins__Swarm") is False
    assert "mcp__builtins__Swarm" not in served
    assert "mcp__builtins__Swarm" in ha._delegated_disallowed(options)


@pytest.mark.asyncio
@pytest.mark.parametrize("kw", [{}, {"tool_allowlist": ["Read", "Task", "Swarm"]}])
async def test_bridge_serves_swarm_with_task(agents, tmp_path, kw):  # noqa: F811
    options = _options(tmp_path, _server(cwd=str(tmp_path)), **kw)
    registry = await ha.HarnessAdapter()._build_registry(options, native=False)
    permitted, served = _bridge(options, registry)
    assert permitted("mcp__builtins__Swarm") is True
    assert {"mcp__builtins__Swarm", "mcp__builtins__Task"} <= served


def test_chat_lane_withholds_the_bridged_swarm(tmp_path):
    from app.mcp.builtin_tools import create_builtin_tools_server

    options = _options(tmp_path, create_builtin_tools_server(is_admin=True))
    options.extra["session_mode"] = "chat"
    assert "mcp__builtins__Swarm" in ha._delegated_disallowed(options)


# ─── The handler-side Task check (defence in depth) ─────────────────────────


@pytest.mark.parametrize("kw, task_ok", [
    ({}, True),
    ({"tool_allowlist": ["Read", "Task", "Swarm"]}, True),
    ({"disallowed_tools": ["Task"]}, False),
    ({"tool_allowlist": ["Read", "Swarm"]}, False),
])
def test_connect_installs_the_sessions_tool_filter(agents, tmp_path, kw, task_ok):  # noqa: F811
    from app.mcp.builtin_tools import BRIDGE_TOOL_FILTER_KEY

    server = _server(cwd=str(tmp_path))
    options = _options(tmp_path, server, **kw)
    ha._install_bridge_tool_filter(options)
    assert server[BRIDGE_TOOL_FILTER_KEY]["tool_filter"]("Task") is task_ok


@pytest.mark.asyncio
async def test_bridged_swarm_start_refuses_when_task_is_not_permitted(agents, tmp_path, monkeypatch):  # noqa: F811
    """Even if the bridge served it, fluid's handler refuses the start, as on
    the native leg, and nothing is dispatched."""
    from app.core import task_dispatch

    _mk_session(agents, PARENT, "root-agent")
    dispatch = AsyncMock()
    monkeypatch.setattr(task_dispatch, "dispatch_task_tool", dispatch)
    monkeypatch.setattr(swarm, "persist", AsyncMock())
    server = _server(cwd=str(tmp_path))
    ha._install_bridge_tool_filter(_options(tmp_path, server, disallowed_tools=["Task"]))
    result = await _tools(server)["Swarm"].handler({"action": "start", "tasks": [
        {"id": "a", "agent": "helper", "prompt": "work"},
    ]})
    assert result.get("isError") is True
    assert "Task delegation is not permitted" in result["content"][0]["text"]
    dispatch.assert_not_awaited()
    assert PARENT not in swarm.coordinators
