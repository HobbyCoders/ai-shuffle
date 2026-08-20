"""Unified Task tool dispatch (P2c — rulings A6/A8/A9/A10)."""

import asyncio
from unittest.mock import patch

import pytest

import app.core.task_dispatch as td
from app.core.provider_agent import WorkerOutcome


@pytest.fixture()
def agents(mock_db):
    from app.db import database

    database.create_profile("root-agent", "Root Agent", None, {"model": "opus"})
    database.create_profile("helper", "Helper", None, {"model": "sonnet"})
    database.create_profile(
        "locked", "Locked Caller", None,
        {"model": "opus", "enabled_agents": ["helper"]},
    )
    database.create_profile(
        "gpt-bound", "GPT Bound", None,
        {"model": "gpt-5", "provider": "prov-x"},
    )
    return database


def _mk_session(database, session_id, profile_id, parent=None):
    database.create_session(
        session_id=session_id, profile_id=profile_id,
        title=session_id, parent_session_id=parent,
    )


# ─── dispatch guards ─────────────────────────────────────────────────────────


async def _dispatch(args, session_id, **kw):
    return await td.dispatch_task_tool(args, caller_session_id=session_id, **kw)


def test_missing_prompt(agents):
    payload = asyncio.run(_dispatch({"agent": "helper", "prompt": ""}, "s1"))
    assert payload["ok"] is False and "prompt" in payload["error"]


def test_allowlist_open_by_default(agents):
    """No enabled_agents on the caller -> any target passes the grant check
    (dispatch itself is stubbed)."""
    _mk_session(agents, "s-root", "root-agent")

    async def fake_worker(**kwargs):
        return WorkerOutcome(ok=True, session_id="w1", report="done")

    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        payload = asyncio.run(_dispatch({"agent": "helper", "prompt": "go"}, "s-root"))
    assert payload["ok"] is True
    assert payload["report"] == "done"
    assert payload["agent"] == "helper"


def test_allowlist_restricts(agents):
    _mk_session(agents, "s-locked", "locked")
    payload = asyncio.run(_dispatch({"agent": "root-agent", "prompt": "go"}, "s-locked"))
    assert payload["ok"] is False
    assert "not available" in payload["error"]

    async def fake_worker(**kwargs):
        return WorkerOutcome(ok=True, session_id="w1", report="ok")

    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        allowed = asyncio.run(_dispatch({"agent": "helper", "prompt": "go"}, "s-locked"))
    assert allowed["ok"] is True


def test_depth_limit(agents, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "task_tool_max_depth", 5)
    monkeypatch.setattr(td, "caller_chain", lambda sid: ([], "root", 5))
    payload = asyncio.run(_dispatch(
        {"agent": "helper", "prompt": "go"},
        "deep",
        caller_profile_id="root-agent",
    ))
    assert payload["ok"] is False and "Nesting limit" in payload["error"]


def test_cycle_refused(agents, monkeypatch):
    monkeypatch.setattr(td, "caller_chain", lambda sid: ([], "root", 1))
    monkeypatch.setattr(td, "_chain_agent_ids", lambda sid: ["helper", "root-agent"])
    payload = asyncio.run(_dispatch(
        {"agent": "helper", "prompt": "go"},
        "s",
        caller_profile_id="root-agent",
    ))
    assert payload["ok"] is False and "Cycle" in payload["error"]


def test_worker_budget(agents, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "task_tool_max_workers", 2)
    monkeypatch.setattr(td, "caller_chain", lambda sid: ([], "root", 0))
    monkeypatch.setattr(td, "_live_worker_count_for_root", lambda root: 2)
    payload = asyncio.run(_dispatch(
        {"agent": "helper", "prompt": "go"},
        "s",
        caller_profile_id="root-agent",
    ))
    assert payload["ok"] is False and "budget" in payload["error"].lower()


def test_provider_bound_target_admission(agents):
    """A target bound to an unknown provider row is refused up front."""
    _mk_session(agents, "s-root2", "root-agent")
    payload = asyncio.run(_dispatch({"agent": "gpt-bound", "prompt": "go"}, "s-root2"))
    assert payload["ok"] is False
    assert "provider" in payload["error"]


def test_model_override_is_refused(agents):
    _mk_session(agents, "s-model-override", "root-agent")
    payload = asyncio.run(_dispatch(
        {"agent": "helper", "prompt": "go", "model": "opus"},
        "s-model-override",
    ))
    assert payload["ok"] is False
    assert "does not accept a model override" in payload["error"]


def test_dispatch_stays_on_caller_provider_and_uses_target_assignment(
    agents, monkeypatch
):
    from app.core import agent_provider_models as apm

    _mk_session(agents, "s-provider-local", "root-agent")
    seen = {}
    monkeypatch.setattr(
        apm,
        "available_subagents",
        lambda caller, provider: (
            apm.SubagentTarget("helper", "Helper", "", "gpt-5"),
        ),
    )

    async def fake_worker(**kwargs):
        seen.update(kwargs)
        return WorkerOutcome(ok=True, session_id="w-provider", report="done")

    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        payload = asyncio.run(td.dispatch_task_tool(
            {"agent": "helper", "prompt": "go"},
            caller_session_id="s-provider-local",
            caller_provider_id="openai-row",
        ))

    assert payload["ok"] is True
    assert seen["provider_id"] == "openai-row"
    assert seen["model_id"] == "gpt-5"


def test_worker_system_prompt_uses_subagent_role_and_real_depth(monkeypatch):
    monkeypatch.setattr(td, "_is_worker_session", lambda session_id: True)
    monkeypatch.setattr(td, "caller_chain", lambda session_id: ([], "root", 2))

    prompt = td.subagent_system_prompt("worker-2")

    assert prompt.startswith("# Subagent context")
    assert "You are a subagent, not the main agent" in prompt
    assert "depth 2" in prompt
    assert "returned to the parent" in prompt


# ─── modes ───────────────────────────────────────────────────────────────────


def test_sync_failure_surfaces_error(agents):
    _mk_session(agents, "s-root3", "root-agent")

    async def fake_worker(**kwargs):
        return WorkerOutcome(ok=False, session_id="w2", error="worker blew up")

    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        payload = asyncio.run(_dispatch({"agent": "helper", "prompt": "go"}, "s-root3"))
    assert payload["ok"] is False and payload["error"] == "worker blew up"


def test_background_mode_returns_immediately(agents):
    _mk_session(agents, "s-root4", "root-agent")
    started = {}

    async def fake_worker(**kwargs):
        started.update(kwargs)
        await asyncio.sleep(0.2)
        return WorkerOutcome(ok=True, session_id="w-bg", report="later")

    async def run():
        with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker), \
             patch.object(td, "_await_worker_registration", return_value=None) as _:
            return await td.dispatch_task_tool(
                {"agent": "helper", "prompt": "go", "mode": "background"},
                caller_session_id="s-root4",
            )

    payload = asyncio.run(run())
    assert payload["ok"] is True and payload["mode"] == "background"
    assert started.get("background") is True


def test_launch_tool_use_id_threads_to_the_worker(agents):
    """The omni dispatcher reads the engine-stamped ctx.extra['tool_use_id'] and
    it reaches dispatch_worker as launch_tool_use_id — what lets the worker
    card absorb the launch tool_use block instead of the UI rendering both."""
    from types import SimpleNamespace

    _mk_session(agents, "s-tuid", "root-agent")
    seen = {}

    async def fake_worker(**kwargs):
        seen.update(kwargs)
        return WorkerOutcome(ok=True, session_id="w-tuid", report="r")

    dispatcher = td.make_omni_dispatcher("s-tuid")
    ctx = SimpleNamespace(extra={"tool_use_id": "tu-99"})
    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        asyncio.run(dispatcher({"agent": "helper", "prompt": "go"}, ctx))
    assert seen["launch_tool_use_id"] == "tu-99"

    # Without the stamp (delegated leg's bridged tool) it degrades to None.
    seen.clear()
    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        asyncio.run(dispatcher({"agent": "helper", "prompt": "go"}, SimpleNamespace(extra={})))
    assert seen["launch_tool_use_id"] is None


def test_root_routing_passes_root_as_parent(agents, monkeypatch):
    """Nested dispatch registers the worker under the ROOT session while
    inheriting context from the direct caller."""
    seen = {}

    async def fake_worker(**kwargs):
        seen.update(kwargs)
        return WorkerOutcome(ok=True, session_id="w3", report="r")

    monkeypatch.setattr(td, "caller_chain", lambda sid: ([], "the-root", 1))
    monkeypatch.setattr(td, "_chain_agent_ids", lambda sid: [])
    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        asyncio.run(_dispatch(
            {"agent": "helper", "prompt": "go"},
            "nested-caller",
            caller_profile_id="root-agent",
        ))
    assert seen["parent_session_id"] == "the-root"
    assert seen["context_session_id"] == "nested-caller"
    assert seen["profile_id"] == "helper"
