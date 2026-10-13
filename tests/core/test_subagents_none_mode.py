"""Subagents "None" (``agent_invoke_mode: none``) is enforced on the server.

The roster is empty, Task and Swarm leave the tool surface on both legs (the
built-in agent and the Claude Code CLI), the prompt names no agents and no
delegation guidance, and a Task call that still arrives is refused.
"""
import dataclasses
from unittest.mock import AsyncMock

import pytest

from app.core import query_options as qo
from app.core.provider import harness_adapter as ha
from app.core.tool_access import (
    DENY_ALL,
    SUBAGENT_DELEGATION_TOOLS,
    resolve_tool_access,
    tool_permitted,
)
from fluid.tools.builtins import read, swarm, task
from fluid.tools.registry import ToolRegistry

NONE = {"agent_invoke_mode": "none", "enabled_agents": []}


def _caller(database, config=None, *, others=("helper",)):
    database.create_profile("caller", "Caller", None, {"model": "opus", **(config or NONE)})
    for agent_id in others:
        database.create_profile(agent_id, agent_id.title(), "Helps.", {"model": "sonnet"})


# --- tool access: the one seam both legs read ------------------------------

@pytest.mark.parametrize("selection", [
    {"tool_selection_mode": "all"},
    {"tool_selection_mode": "allow", "allowed_tools": ["Read", "Task", "Swarm"]},
    {"tool_selection_mode": "disallow", "disallowed_tools": ["Bash"]},
])
def test_none_blocks_the_delegation_tools_whatever_the_tool_selection(selection):
    access = resolve_tool_access({**selection, **NONE})
    for name in ("Task", "Swarm", "Agent", "mcp__builtins__Task"):
        assert not tool_permitted(name, access.allowed, access.disallowed), name
    assert tool_permitted("Read", access.allowed, access.disallowed)


def test_none_keeps_a_total_deny_as_it_was():
    access = resolve_tool_access({"tool_selection_mode": "none", **NONE})
    assert access.disallowed == [DENY_ALL]


@pytest.mark.parametrize("mode", ["all", "custom", None])
def test_other_modes_leave_the_surface_alone(mode):
    config = {"tool_selection_mode": "all", "agent_invoke_mode": mode, "enabled_agents": []}
    access = resolve_tool_access(config)
    assert access.disallowed is None
    assert tool_permitted("Task", access.allowed, access.disallowed)


# --- roster and Task contract -----------------------------------------------

def test_roster_is_empty_under_none(mock_db):
    from app.core.agent_provider_models import available_subagents, build_task_contract, file_subagent_targets
    from app.db import database

    _caller(database, others=("helper", "explorer"))
    assert available_subagents("caller", None) == ()
    files = file_subagent_targets({"writer": {"name": "writer", "description": "On disk."}}, "opus")
    contract = build_task_contract("caller", None, files)
    assert contract.targets == ()
    assert "No subagents are configured" in contract.description
    assert "enum" not in contract.input_schema["properties"]["agent"]


def test_selected_only_with_nothing_selected_still_offers_file_agents(mock_db):
    """Unchanged: an empty "Selected only" lists no DB agent, not no agent."""
    from app.core.agent_provider_models import available_subagents, build_task_contract, file_subagent_targets
    from app.db import database

    _caller(database, {"agent_invoke_mode": "custom", "enabled_agents": []})
    assert available_subagents("caller", None) == ()
    files = file_subagent_targets({"writer": {"name": "writer", "description": "On disk."}}, "opus")
    assert [t.id for t in build_task_contract("caller", None, files).targets] == ["writer"]


def test_switching_back_to_all_restores_the_roster(mock_db):
    from app.core.agent_provider_models import available_subagents
    from app.db import database

    _caller(database)
    assert available_subagents("caller", None) == ()
    database.update_profile("caller", config={"model": "opus", "agent_invoke_mode": "all"})
    assert "helper" in [t.id for t in available_subagents("caller", None)]


def test_no_caller_profile_identity_keeps_the_legacy_policy(monkeypatch):
    from app.core.agent_provider_models import caller_subagents_disabled, caller_subagents_refusal
    from app.db import database

    def unexpected_read(profile_id):
        pytest.fail("No profile identity must not trigger a profile lookup")

    monkeypatch.setattr(database, "get_profile", unexpected_read)
    assert caller_subagents_refusal(None) is None
    assert caller_subagents_disabled(None) is False


# --- Task dispatch refuses ----------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_refuses_under_none_even_for_a_file_agent(mock_db):
    from app.core import task_dispatch
    from app.db import database

    _caller(database)
    registry = {"writer": {"name": "writer", "description": "On disk.", "prompt": "Write."}}
    for agent in ("helper", "writer"):
        payload = await task_dispatch.dispatch_task_tool(
            {"agent": agent, "prompt": "Do it."},
            caller_session_id="parent-session",
            caller_profile_id="caller",
            caller_provider_id=None,
            agent_registry=registry,
        )
        assert payload["ok"] is False, agent
        assert payload["reason"] == "subagents_disabled"
        assert "Nothing was dispatched" in payload["error"]


@pytest.mark.asyncio
async def test_none_refuses_an_explicit_voice_roster(mock_db, monkeypatch):
    """The voice producer passes its own roster, not the chat allowlist."""
    from app.core import task_dispatch, provider_agent
    from app.core.agent_provider_models import available_voice_subagents
    from app.db import database

    _caller(database)
    database.update_profile("helper", config={"provider_models": {"__voice__": "sonnet"}})
    roster = available_voice_subagents()
    assert [target.id for target in roster] == ["helper"]
    worker = AsyncMock(return_value=provider_agent.WorkerOutcome(
        ok=True, session_id="voice-worker", report="done"))
    monkeypatch.setattr(provider_agent, "dispatch_worker", worker)
    payload = await task_dispatch.dispatch_task_tool(
        {"agent": "helper", "prompt": "Do it."},
        caller_session_id="voice-parent",
        caller_profile_id="caller",
        caller_provider_id=None,
        roster=roster,
    )
    assert payload["ok"] is False
    assert payload["reason"] == "subagents_disabled"
    assert "Nothing was dispatched" in payload["error"]
    worker.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_swarm_start_is_refused_before_scheduling(mock_db, monkeypatch):
    from types import SimpleNamespace
    from app.core import swarm
    from app.db import database
    from fluid.tools.registry import ToolCtx

    _caller(database)
    database.create_session(session_id="none-parent", profile_id="caller")
    start = AsyncMock(return_value=({"id": "must-not-start"}, None))
    monkeypatch.setitem(swarm.coordinators, "none-parent", SimpleNamespace(start=start))
    outcome = await swarm.make_handler(lambda: "none-parent")(
        {"action": "start", "tasks": [
            {"id": "a", "agent": "helper", "prompt": "Do it."}
        ]}, ToolCtx(),
    )
    assert outcome.is_error
    assert "Nothing was dispatched" in outcome.text
    start.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("profile_state", ["missing", "read_error"])
async def test_none_dispatch_fails_closed_when_caller_unavailable(mock_db, monkeypatch, profile_state):
    from app.core import task_dispatch, provider_agent
    from app.core.agent_provider_models import available_voice_subagents
    from app.db import database

    _caller(database)
    database.update_profile("helper", config={"provider_models": {"__voice__": "sonnet"}})
    roster = available_voice_subagents()
    get_profile = database.get_profile

    def unavailable_caller(profile_id):
        if profile_id == "caller":
            if profile_state == "missing":
                return None
            raise RuntimeError("Caller profile read unavailable")
        return get_profile(profile_id)

    monkeypatch.setattr(database, "get_profile", unavailable_caller)
    worker = AsyncMock(return_value=provider_agent.WorkerOutcome(
        ok=True, session_id="must-not-start", report="done"))
    monkeypatch.setattr(provider_agent, "dispatch_worker", worker)
    payload = await task_dispatch.dispatch_task_tool(
        {"agent": "helper", "prompt": "Do it."},
        caller_session_id="voice-parent", caller_profile_id="caller",
        caller_provider_id=None, roster=roster,
    )
    assert payload["ok"] is False
    assert payload["reason"] == "caller_profile_unavailable"
    assert "unavailable" in payload["error"]
    assert "Nothing was dispatched" in payload["error"]
    assert "Subagents: None" not in payload["error"]
    worker.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_swarm_reports_unavailable_caller_accurately(mock_db, monkeypatch):
    from types import SimpleNamespace
    from app.core import swarm
    from app.db import database
    from fluid.tools.registry import ToolCtx

    _caller(database)
    database.create_session(session_id="none-parent", profile_id="caller")
    start = AsyncMock(return_value=({"id": "must-not-start"}, None))
    monkeypatch.setitem(swarm.coordinators, "none-parent", SimpleNamespace(start=start))
    monkeypatch.setattr(database, "get_profile", lambda profile_id: None)
    outcome = await swarm.make_handler(lambda: "none-parent")(
        {"action": "start", "tasks": [
            {"id": "a", "agent": "helper", "prompt": "Do it."}
        ]}, ToolCtx(),
    )
    assert outcome.is_error
    assert "unavailable" in outcome.text
    assert "Nothing was dispatched" in outcome.text
    assert "Subagents: None" not in outcome.text
    start.assert_not_awaited()


# --- the built-in (native fluid) leg ---------------------------------------

async def _session_options(tmp_path, monkeypatch, config):
    full = {"cwd": str(tmp_path), "model": "claude-sonnet-4-6", "tool_selection_mode": "all",
            "system_prompt": "USER", **config}
    monkeypatch.setattr(qo, "get_security_instructions", lambda: "SECURITY")
    sdk, _ = qo.build_options_from_profile({"id": "caller", "config": full},
                                           overrides={"cwd": str(tmp_path)})
    adapter = ha.HarnessAdapter()
    capture = AsyncMock()
    monkeypatch.setattr(adapter, "connect", capture)
    await adapter.connect_with_sdk_options(sdk)
    return sdk, capture.call_args.args[0]


def _native_registry():
    registry = ToolRegistry()
    for module in (read, task, swarm):
        registry.register_builtin(module.SPEC, module.handler)
    return registry


def _wired():
    return frozenset(key for module in (task, swarm) for key in module.SPEC.requires_extra)


@pytest.mark.asyncio
async def test_native_leg_under_none_offers_no_delegation(mock_db, tmp_path, monkeypatch):
    from app.db import database

    _caller(database)
    sdk, options = await _session_options(tmp_path, monkeypatch, NONE)
    for name in ("Task", "Swarm"):
        assert name in sdk.disallowed_tools
    assert sdk.aih_declared_agents is None
    permit = ha._tool_predicate(options)
    assert permit is not None and not permit("Task") and not permit("Swarm") and permit("Read")

    registry = _native_registry()
    assert "Task" not in ha._permitted_tool_names(options, registry)
    system, append = ha._system_parts(options.system_prompt)
    compiled = ha.HarnessAdapter._compile_prompt_sections(
        system, append, options, native=True, registry=registry, wired=_wired())
    assert "USER" in compiled.text
    assert "# Subagent delegation" not in compiled.text
    assert "Helper" not in compiled.text
    assert ha._agents_header().splitlines()[0] not in compiled.text


@pytest.mark.asyncio
async def test_native_leg_under_all_still_delegates(mock_db, tmp_path, monkeypatch):
    """The control case: the same build with "All agents" keeps everything."""
    from app.db import database

    _caller(database, {"agent_invoke_mode": "all"})
    sdk, options = await _session_options(tmp_path, monkeypatch, {"agent_invoke_mode": "all"})
    assert "Task" not in (sdk.disallowed_tools or [])
    assert "Helper" in [a["name"] for a in sdk.aih_declared_agents]
    system, append = ha._system_parts(options.system_prompt)
    compiled = ha.HarnessAdapter._compile_prompt_sections(
        system, append, options, native=True, registry=_native_registry(), wired=_wired())
    assert "# Subagent delegation" in compiled.text
    assert "- Helper: Helps." in compiled.text


# --- the Claude Code CLI (delegated) leg -----------------------------------

def _delegated_registry():
    registry = ToolRegistry()
    registry.register_builtin(read.SPEC, read.handler)
    registry.register_builtin(
        dataclasses.replace(task.SPEC, name="mcp__builtins__Task"), task.handler)
    return registry


@pytest.mark.asyncio
async def test_delegated_leg_under_none_offers_no_delegation(mock_db, tmp_path, monkeypatch):
    from app.db import database

    _caller(database)
    _, options = await _session_options(tmp_path, monkeypatch, NONE)
    banned = set(ha._delegated_disallowed(options))
    assert {"Task", "Agent", "Swarm", "mcp__builtins__Task"} <= banned
    registry = _delegated_registry()
    assert ha._delegated_offered(options, registry)("Task") is False
    text = ha._delegated_append_text("USER", options, registry) or ""
    assert "# Subagent delegation" not in text
    assert "Helper" not in text


@pytest.mark.asyncio
async def test_delegated_leg_under_all_still_offers_task(mock_db, tmp_path, monkeypatch):
    from app.db import database

    _caller(database, {"agent_invoke_mode": "all"})
    _, options = await _session_options(tmp_path, monkeypatch, {"agent_invoke_mode": "all"})
    assert "mcp__builtins__Task" not in set(ha._delegated_disallowed(options))
    registry = _delegated_registry()
    assert ha._delegated_offered(options, registry)("Task") is True
    text = ha._delegated_append_text("USER", options, registry) or ""
    assert "# Subagent delegation" in text
    assert "- Helper: Helps." in text


def test_delegation_tool_names_cover_both_spellings():
    assert {"Task", "Swarm", "Agent", "mcp__builtins__Task"} == set(SUBAGENT_DELEGATION_TOOLS)
