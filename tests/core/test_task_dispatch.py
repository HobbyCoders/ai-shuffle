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


# ─── target resolution ───────────────────────────────────────────────────────


def test_resolve_by_id(agents):
    row, err = td.resolve_target_agent("helper")
    assert err is None and row["id"] == "helper"


def test_resolve_by_unambiguous_name(agents):
    row, err = td.resolve_target_agent("Helper")
    assert err is None and row["id"] == "helper"


def test_resolve_unknown(agents):
    row, err = td.resolve_target_agent("nope")
    assert row is None and "Unknown agent" in err


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
    assert "allowlist" in payload["error"]

    async def fake_worker(**kwargs):
        return WorkerOutcome(ok=True, session_id="w1", report="ok")

    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        allowed = asyncio.run(_dispatch({"agent": "helper", "prompt": "go"}, "s-locked"))
    assert allowed["ok"] is True


def test_depth_limit(agents, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "task_tool_max_depth", 5)
    monkeypatch.setattr(td, "caller_chain", lambda sid: ([], "root", 5))
    payload = asyncio.run(_dispatch({"agent": "helper", "prompt": "go"}, "deep"))
    assert payload["ok"] is False and "Nesting limit" in payload["error"]


def test_cycle_refused(agents, monkeypatch):
    monkeypatch.setattr(td, "caller_chain", lambda sid: ([], "root", 1))
    monkeypatch.setattr(td, "_chain_agent_ids", lambda sid: ["helper", "root-agent"])
    payload = asyncio.run(_dispatch({"agent": "helper", "prompt": "go"}, "s"))
    assert payload["ok"] is False and "Cycle" in payload["error"]


def test_worker_budget(agents, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "task_tool_max_workers", 2)
    monkeypatch.setattr(td, "caller_chain", lambda sid: ([], "root", 0))
    monkeypatch.setattr(td, "_live_worker_count_for_root", lambda root: 2)
    payload = asyncio.run(_dispatch({"agent": "helper", "prompt": "go"}, "s"))
    assert payload["ok"] is False and "budget" in payload["error"].lower()


def test_provider_bound_target_admission(agents):
    """A target bound to an unknown provider row is refused up front."""
    _mk_session(agents, "s-root2", "root-agent")
    payload = asyncio.run(_dispatch({"agent": "gpt-bound", "prompt": "go"}, "s-root2"))
    assert payload["ok"] is False
    assert "provider" in payload["error"]


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
        asyncio.run(_dispatch({"agent": "helper", "prompt": "go"}, "nested-caller"))
    assert seen["parent_session_id"] == "the-root"
    assert seen["context_session_id"] == "nested-caller"
    assert seen["profile_id"] == "helper"


# ─── file/plugin agent definitions (CE6 registry seam) ───────────────────────


def _definition(name="researcher", **kw):
    """A stand-in for fluid's AgentDefinition (duck-typed on purpose)."""
    from types import SimpleNamespace

    fields = dict(
        name=name,
        description="Researches things",
        path=f"/proj/.claude/agents/{name}.md",
        scope="project",
        model=None,
        tools=(),
        body="You are a careful researcher. Read before you write.",
    )
    fields.update(kw)
    return SimpleNamespace(**fields)


def test_file_agent_dispatched_on_the_callers_profile(agents):
    """A name only fluid knows resolves through the registry and spawns on the
    caller's profile with the definition body as a system-prompt override."""
    _mk_session(agents, "s-file", "root-agent")
    seen = {}

    async def fake_worker(**kwargs):
        seen.update(kwargs)
        return WorkerOutcome(ok=True, session_id="w-file", report="found it")

    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        payload = asyncio.run(_dispatch(
            {"agent": "researcher", "prompt": "go"}, "s-file",
            agent_registry={"researcher": _definition()},
        ))

    assert payload["ok"] is True and payload["agent"] == "researcher"
    assert seen["profile_id"] == "root-agent"  # borrowed, not invented
    assert "careful researcher" in seen["system_prompt_append"]
    assert "researcher" in seen["label"]


def test_app_agent_wins_a_name_collision_with_a_file_agent(agents):
    """Host wins (CE6): an app agent row shadows a same-named file agent."""
    _mk_session(agents, "s-collide", "root-agent")
    seen = {}

    async def fake_worker(**kwargs):
        seen.update(kwargs)
        return WorkerOutcome(ok=True, session_id="w-c", report="r")

    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        payload = asyncio.run(_dispatch(
            {"agent": "helper", "prompt": "go"}, "s-collide",
            agent_registry={"helper": _definition(name="helper", body="FILE BODY")},
        ))

    assert payload["ok"] is True
    assert seen["profile_id"] == "helper"
    assert seen.get("system_prompt_append") is None


def test_file_agent_unknown_name_still_errors(agents):
    _mk_session(agents, "s-unknown", "root-agent")
    payload = asyncio.run(_dispatch(
        {"agent": "ghost", "prompt": "go"}, "s-unknown",
        agent_registry={"researcher": _definition()},
    ))
    assert payload["ok"] is False and "Unknown agent" in payload["error"]


def test_file_agent_model_and_tools(agents):
    """``model:`` rides through on an unbound (delegated) caller; ``tools:`` is
    advisory and says so in the result note and the injected prompt."""
    _mk_session(agents, "s-model", "root-agent")
    seen = {}

    async def fake_worker(**kwargs):
        seen.update(kwargs)
        return WorkerOutcome(ok=True, session_id="w-m", report="r")

    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        payload = asyncio.run(_dispatch(
            {"agent": "researcher", "prompt": "go"}, "s-model",
            agent_registry={
                "researcher": _definition(model="sonnet", tools=("Read", "Grep")),
            },
        ))

    assert seen["model_id"] == "sonnet"
    assert "Read, Grep" in seen["system_prompt_append"]
    assert "advisory" in payload["note"]


def test_file_agent_respects_the_invoke_allowlist(agents):
    """enabled_agents is host policy — it gates file agents by name too."""
    _mk_session(agents, "s-locked-file", "locked")
    payload = asyncio.run(_dispatch(
        {"agent": "researcher", "prompt": "go"}, "s-locked-file",
        agent_registry={"researcher": _definition()},
    ))
    assert payload["ok"] is False and "allowlist" in payload["error"]


def test_dispatcher_reads_the_agent_registry_off_the_ctx(agents):
    """NativeLoop seeds ctx.extra['agent_registry'] AFTER this dispatcher is
    built into the same ctx, so the read must happen at call time."""
    from types import SimpleNamespace

    _mk_session(agents, "s-ctx", "root-agent")
    seen = {}

    async def fake_worker(**kwargs):
        seen.update(kwargs)
        return WorkerOutcome(ok=True, session_id="w-ctx", report="r")

    dispatcher = td.make_omni_dispatcher("s-ctx")
    ctx = SimpleNamespace(extra={})  # empty at injection time
    ctx.extra["agent_registry"] = {"researcher": _definition()}  # seeded later
    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        asyncio.run(dispatcher({"agent": "researcher", "prompt": "go"}, ctx))

    assert seen["profile_id"] == "root-agent"
    assert "careful researcher" in seen["system_prompt_append"]
