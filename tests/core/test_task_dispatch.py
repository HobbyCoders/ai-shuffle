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
    # Explicit invoke-grant modes (agent_invoke_mode).
    database.create_profile(
        "custom-empty", "Custom Empty", None,
        {"model": "opus", "agent_invoke_mode": "custom"},
    )
    database.create_profile(
        "custom-empty-list", "Custom Empty List", None,
        {"model": "opus", "agent_invoke_mode": "custom", "enabled_agents": []},
    )
    database.create_profile(
        "custom-listed", "Custom Listed", None,
        {"model": "opus", "agent_invoke_mode": "custom", "enabled_agents": ["helper"]},
    )
    database.create_profile(
        "mode-all", "Mode All", None,
        {"model": "opus", "agent_invoke_mode": "all", "enabled_agents": ["helper"]},
    )
    return database


@pytest.fixture(autouse=True)
def report_dir(tmp_path, monkeypatch):
    """Point sync Task report files at tmp_path instead of the real data dir.

    Patches the cwd resolution the real ``_sync_report_dir`` reads, so the path
    logic under test is the shipped one.
    """
    from app.core import project_resolver

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setattr(
        project_resolver, "resolve_existing_cwd", lambda row: str(checkout)
    )
    return checkout / ".fluid" / "task-reports"


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


def test_dispatch_open_by_default(agents):
    """No enabled_agents on the caller -> any target dispatches (dispatch
    itself is stubbed)."""
    _mk_session(agents, "s-root", "root-agent")

    async def fake_worker(**kwargs):
        return WorkerOutcome(ok=True, session_id="w1", report="done")

    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        payload = asyncio.run(_dispatch({"agent": "helper", "prompt": "go"}, "s-root"))
    assert payload["ok"] is True
    assert payload["report_head"] == "done"
    assert payload["agent"] == "helper"


# ─── sync report delivery (out-of-band file + bounded head) ──────────────────


def test_sync_report_lands_in_a_file_not_the_tool_result(agents):
    """The full report goes to disk; the result carries a bounded head plus the
    path. Inlining it cost the parent up to MAX_REPORT_CHARS of context before
    it knew whether it needed any of it."""
    from pathlib import Path

    _mk_session(agents, "s-file-report", "root-agent")
    body = "x" * (td.SYNC_REPORT_HEAD_CHARS + 5_000)

    async def fake_worker(**kwargs):
        return WorkerOutcome(ok=True, session_id="w-report", report=body)

    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        payload = asyncio.run(
            _dispatch({"agent": "helper", "prompt": "go"}, "s-file-report")
        )

    assert payload["ok"] is True
    assert "report" not in payload
    assert payload["report_chars"] == len(body)
    assert payload["report_head"] == body[: td.SYNC_REPORT_HEAD_CHARS]
    assert len(payload["report_head"]) == td.SYNC_REPORT_HEAD_CHARS
    written = Path(payload["report_path"])
    assert written.read_text(encoding="utf-8") == body
    assert written.name == "w-report.md"
    assert written.parent.name == "task-reports"
    # The note is the contract the parent reads the file by.
    assert "report_path" in payload["note"]


def test_sync_report_file_failure_falls_back_to_inline(agents, monkeypatch):
    """A write failure must never lose a completed worker's output."""
    _mk_session(agents, "s-report-fail", "root-agent")
    monkeypatch.setattr(td, "_write_sync_report", lambda *a, **k: None)

    async def fake_worker(**kwargs):
        return WorkerOutcome(ok=True, session_id="w-fail", report="the whole thing")

    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        payload = asyncio.run(
            _dispatch({"agent": "helper", "prompt": "go"}, "s-report-fail")
        )

    assert payload["report"] == "the whole thing"
    assert payload["report_head"] == "the whole thing"
    assert "report_path" not in payload


def test_sync_report_keeps_truncated_and_error_semantics(agents):
    """``truncated`` still marks a report provider_agent bounded, and a failed
    worker still carries ``error`` with no report fields."""
    _mk_session(agents, "s-report-sem", "root-agent")

    async def truncated_worker(**kwargs):
        return WorkerOutcome(
            ok=True, session_id="w-trunc", report="cut", truncated=True
        )

    with patch("app.core.provider_agent.dispatch_worker", side_effect=truncated_worker):
        payload = asyncio.run(
            _dispatch({"agent": "helper", "prompt": "go"}, "s-report-sem")
        )
    assert payload["truncated"] is True
    assert payload["report_path"]

    async def failed_worker(**kwargs):
        return WorkerOutcome(ok=False, session_id="w-bad", error="boom")

    with patch("app.core.provider_agent.dispatch_worker", side_effect=failed_worker):
        payload = asyncio.run(
            _dispatch({"agent": "helper", "prompt": "go"}, "s-report-sem")
        )
    assert payload["ok"] is False and payload["error"] == "boom"
    assert "report_head" not in payload and "report_path" not in payload


def test_sync_report_dir_prefers_the_callers_checkout(monkeypatch, tmp_path):
    """The file must land where the parent's own Read already reaches — its
    materialized checkout — not in an unrelated data dir."""
    from app.core import project_resolver

    checkout = tmp_path / "repo"
    checkout.mkdir()
    monkeypatch.setattr(
        project_resolver, "resolve_existing_cwd", lambda row: str(checkout)
    )

    directory = td._sync_report_dir({"id": "s-1"})

    assert directory == checkout / ".fluid" / "task-reports"


def test_legacy_enabled_agents_restricts_dispatch(agents):
    """A legacy non-empty allowlist is enforced by the Task roster."""
    _mk_session(agents, "s-locked", "locked")

    async def fake_worker(**kwargs):
        return WorkerOutcome(ok=True, session_id="w1", report="ok")

    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        unavailable = asyncio.run(
            _dispatch({"agent": "root-agent", "prompt": "go"}, "s-locked")
        )
        available = asyncio.run(_dispatch({"agent": "helper", "prompt": "go"}, "s-locked"))
    assert unavailable["ok"] is False and "not available" in unavailable["error"]
    assert available["ok"] is True


@pytest.mark.parametrize("caller_pid", ["custom-empty", "custom-empty-list"])
def test_invoke_mode_custom_empty_dispatches_nobody(agents, caller_pid):
    """Explicit custom mode with an empty selection exposes no DB targets."""
    session_id = f"s-{caller_pid}"
    _mk_session(agents, session_id, caller_pid)

    async def fake_worker(**kwargs):
        return WorkerOutcome(ok=True, session_id="w1", report="ok")

    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        for target in ("helper", "root-agent"):
            payload = asyncio.run(
                _dispatch({"agent": target, "prompt": "go"}, session_id)
            )
            assert payload["ok"] is False
            assert "not available" in payload["error"]


def test_invoke_mode_custom_rejects_unlisted_agents(agents):
    _mk_session(agents, "s-custom-listed", "custom-listed")

    async def fake_worker(**kwargs):
        return WorkerOutcome(ok=True, session_id="w1", report="ok")

    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        unavailable = asyncio.run(
            _dispatch({"agent": "root-agent", "prompt": "go"}, "s-custom-listed")
        )
        available = asyncio.run(
            _dispatch({"agent": "helper", "prompt": "go"}, "s-custom-listed")
        )
    assert unavailable["ok"] is False and "not available" in unavailable["error"]
    assert available["ok"] is True


def test_invoke_mode_all_dispatches_anything(agents):
    """Mode ``all`` exposes every provider-compatible target."""
    _mk_session(agents, "s-mode-all", "mode-all")

    async def fake_worker(**kwargs):
        return WorkerOutcome(ok=True, session_id="w1", report="done")

    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        payload = asyncio.run(_dispatch({"agent": "root-agent", "prompt": "go"}, "s-mode-all"))
    assert payload["ok"] is True
    assert payload["agent"] == "root-agent"


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
    monkeypatch.setattr(td, "_credential_gap", lambda provider_id: None)

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
    assert payload["task_id"] == payload["worker_session_id"]
    assert started["worker_session_id"] == payload["worker_session_id"]


def test_background_registration_failure_is_not_returned_as_a_receipt(agents):
    _mk_session(agents, "s-bg-fail", "root-agent")

    async def fake_worker(**kwargs):
        return WorkerOutcome(
            ok=False,
            session_id=kwargs["worker_session_id"],
            error="could not create worker",
        )

    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        payload = asyncio.run(_dispatch(
            {"agent": "helper", "prompt": "go", "mode": "background"},
            "s-bg-fail",
        ))

    assert payload["ok"] is False
    assert payload["error"] == "could not create worker"
    assert "task_id" not in payload


def test_launch_tool_use_id_threads_to_the_worker(agents):
    """The fluid dispatcher reads the engine-stamped ctx.extra['tool_use_id'] and
    it reaches dispatch_worker as launch_tool_use_id — what lets the worker
    card absorb the launch tool_use block instead of the UI rendering both."""
    from types import SimpleNamespace

    _mk_session(agents, "s-tuid", "root-agent")
    seen = {}

    async def fake_worker(**kwargs):
        seen.update(kwargs)
        return WorkerOutcome(ok=True, session_id="w-tuid", report="r")

    dispatcher = td.make_fluid_dispatcher("s-tuid")
    ctx = SimpleNamespace(extra={"tool_use_id": "tu-99"})
    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        asyncio.run(dispatcher({"agent": "helper", "prompt": "go"}, ctx))
    assert seen["launch_tool_use_id"] == "tu-99"

    # Without the stamp (delegated leg's bridged tool) it degrades to None.
    seen.clear()
    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        asyncio.run(dispatcher({"agent": "helper", "prompt": "go"}, SimpleNamespace(extra={})))
    assert seen["launch_tool_use_id"] is None


def test_fluid_dispatcher_returns_structured_background_receipt(monkeypatch):
    from types import SimpleNamespace

    async def fake_dispatch(*args, **kwargs):
        return {
            "ok": True,
            "mode": "background",
            "task_id": "worker-42",
            "worker_session_id": "worker-42",
            "description": "research",
        }

    monkeypatch.setattr(td, "dispatch_task_tool", fake_dispatch)
    outcome = asyncio.run(td.make_fluid_dispatcher("parent-1")(
        {"agent": "helper", "prompt": "go", "mode": "background"},
        SimpleNamespace(extra={}),
    ))

    assert outcome.background_task is not None
    assert outcome.background_task.task_id == "worker-42"
    assert outcome.background_task.worker_session_id == "worker-42"
    assert outcome.background_task.description == "research"


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


# ─── native file-agent definitions (registry seam) ───────────────────────


def _definition(name="researcher", **kw):
    """A stand-in for fluid's AgentDefinition (duck-typed on purpose)."""
    from types import SimpleNamespace

    fields = dict(
        name=name,
        description="Researches things",
        path=f"/proj/.agents/agents/{name}.md",
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
    assert payload["ok"] is False and "not available" in payload["error"]


def test_file_agent_model_cannot_change_binding_and_tools_are_enforced(agents):
    """File-agent model metadata cannot move the worker off the caller's leg."""
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

    assert seen["model_id"] is None
    assert "Read, Grep" in seen["system_prompt_append"]
    assert seen["tool_allowlist"] == ["Read", "Grep"]
    assert payload["ok"] is True


# ─── CE12: the definition's tools list is the worker's tool surface ──────────


def test_file_agent_tools_become_the_workers_allowlist(agents):
    """``tools: [Read, Grep]`` reaches dispatch_worker as ``tool_allowlist`` —
    the seam that becomes ``NativeLoop(allowed_tools=...)`` (ruling CE12)."""
    _mk_session(agents, "s-ce12", "root-agent")
    seen = {}

    async def fake_worker(**kwargs):
        seen.update(kwargs)
        return WorkerOutcome(ok=True, session_id="w-ce12", report="r")

    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        payload = asyncio.run(_dispatch(
            {"agent": "researcher", "prompt": "go"}, "s-ce12",
            agent_registry={"researcher": _definition(tools=("Read", "Grep"))},
        ))

    assert payload["ok"] is True
    assert seen["tool_allowlist"] == ["Read", "Grep"]
    # The prompt block states the restriction as enforced, not advisory.
    assert "enforced by the runtime" in seen["system_prompt_append"]


def test_file_agent_without_tools_inherits_everything(agents):
    """Empty/absent ``tools:`` = inherit the caller's toolset: no allowlist is
    passed at all (fluid reads ``allowed_tools=None`` as unrestricted)."""
    _mk_session(agents, "s-ce12-open", "root-agent")
    seen = {}

    async def fake_worker(**kwargs):
        seen.update(kwargs)
        return WorkerOutcome(ok=True, session_id="w-open", report="r")

    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        asyncio.run(_dispatch(
            {"agent": "researcher", "prompt": "go"}, "s-ce12-open",
            agent_registry={"researcher": _definition(tools=())},
        ))
    assert "tool_allowlist" not in seen

    seen.clear()
    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        asyncio.run(_dispatch(
            {"agent": "researcher", "prompt": "go"}, "s-ce12-open",
            agent_registry={"researcher": _definition(tools=None)},
        ))
    assert "tool_allowlist" not in seen


def test_app_agent_dispatch_carries_no_tool_allowlist(agents):
    """An app agent owns its profile (and its tool config) — a Task dispatch
    must not narrow it with a per-turn allowlist."""
    _mk_session(agents, "s-ce12-app", "root-agent")
    seen = {}

    async def fake_worker(**kwargs):
        seen.update(kwargs)
        return WorkerOutcome(ok=True, session_id="w-app", report="r")

    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        asyncio.run(_dispatch({"agent": "helper", "prompt": "go"}, "s-ce12-app"))
    assert "tool_allowlist" not in seen


def test_definition_tool_allowlist_normalizes():
    """Blank entries are dropped; an all-blank list degrades to unrestricted."""
    assert td._definition_tool_allowlist(_definition(tools=("Read", " ", "Grep "))) == [
        "Read", "Grep",
    ]
    assert td._definition_tool_allowlist(_definition(tools=("", "  "))) is None
    assert td._definition_tool_allowlist(_definition(tools=())) is None


def test_file_agent_dispatches_even_when_undeclared(agents):
    """enabled_agents never gated file agents either — a resolvable definition
    runs on name alone (2026-08-17, superseding A10)."""
    _mk_session(agents, "s-locked-file", "locked")

    async def fake_worker(**kwargs):
        return WorkerOutcome(ok=True, session_id="w-file", report="r")

    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        payload = asyncio.run(_dispatch(
            {"agent": "researcher", "prompt": "go"}, "s-locked-file",
            agent_registry={"researcher": _definition()},
        ))
    assert payload["ok"] is True and payload["agent"] == "researcher"


# ─── file-agent model inheritance on a provider lane ─────────────────────────
#
# A file agent has no agents row, so it borrows a model. On a provider lane an
# unresolved model used to mean "whatever sits at index 0 of models_json" — the
# roster below deliberately puts the EXPENSIVE model there so any regression
# shows up as that id.

_GROK = "x-ai/grok-4.6"
_CHEAP = "deepseek/deepseek-v4-flash"


def _lane(agents, provider_id="prov-lane", default_model=None):
    """A provider whose curated list leads with the expensive model."""
    agents.create_model_provider(
        provider_id=provider_id,
        name="Lane",
        provider_type="openai",
        base_url="https://lane.test/v1",
        models=[{"id": _GROK, "label": "Grok"}, {"id": _CHEAP, "label": "Flash"}],
        default_model=default_model,
        enabled=True,
    )
    return provider_id


def _dispatch_file_agent(session_id, provider_id, monkeypatch):
    """Dispatch the ``researcher`` file agent on ``provider_id``; return
    ``(payload, seen_kwargs, calls)``."""
    monkeypatch.setattr(td, "_credential_gap", lambda pid: None)
    seen = {}
    calls = []

    async def fake_worker(**kwargs):
        calls.append(kwargs)
        seen.update(kwargs)
        return WorkerOutcome(ok=True, session_id="w-lane", report="r")

    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        payload = asyncio.run(_dispatch(
            {"agent": "researcher", "prompt": "go"}, session_id,
            caller_provider_id=provider_id,
            agent_registry={"researcher": _definition()},
        ))
    return payload, seen, calls


def test_file_agent_inherits_callers_launched_model(agents, monkeypatch):
    provider_id = _lane(agents, default_model=_GROK)
    _mk_session(agents, "s-lane-launched", "root-agent")
    agents.update_session("s-lane-launched", launched_model=_CHEAP)

    payload, seen, _ = _dispatch_file_agent(
        "s-lane-launched", provider_id, monkeypatch
    )

    assert payload["ok"] is True
    assert seen["model_id"] == _CHEAP


def test_file_agent_without_launched_model_uses_provider_default_not_index_0(
    agents, monkeypatch
):
    provider_id = _lane(agents, default_model=_CHEAP)
    _mk_session(agents, "s-lane-default", "root-agent")

    payload, seen, _ = _dispatch_file_agent(
        "s-lane-default", provider_id, monkeypatch
    )

    assert payload["ok"] is True
    assert seen["model_id"] == _CHEAP
    assert seen["model_id"] != _GROK


def test_file_agent_uncurated_launched_model_falls_through(agents, monkeypatch):
    provider_id = _lane(agents, default_model=_CHEAP)
    _mk_session(agents, "s-lane-uncurated", "root-agent")
    agents.update_session("s-lane-uncurated", launched_model="opus")

    payload, seen, _ = _dispatch_file_agent(
        "s-lane-uncurated", provider_id, monkeypatch
    )

    assert payload["ok"] is True
    assert seen["model_id"] == _CHEAP


def test_file_agent_with_no_resolvable_model_refuses_dispatch(agents, monkeypatch):
    provider_id = _lane(agents, default_model=None)
    _mk_session(agents, "s-lane-none", "root-agent")

    payload, _, calls = _dispatch_file_agent("s-lane-none", provider_id, monkeypatch)

    assert payload["ok"] is False
    assert "model" in payload["error"]
    assert calls == []


def test_file_agent_profile_mapping_beats_provider_default(agents, monkeypatch):
    provider_id = _lane(agents, default_model=_GROK)
    agents.create_profile(
        "lane-caller", "Lane Caller", None,
        {"model": "opus", "provider_models": {provider_id: _CHEAP}},
    )
    _mk_session(agents, "s-lane-mapped", "lane-caller")

    payload, seen, _ = _dispatch_file_agent(
        "s-lane-mapped", provider_id, monkeypatch
    )

    assert payload["ok"] is True
    assert seen["model_id"] == _CHEAP


def test_file_agent_native_caller_keeps_inherit_none(agents):
    """Native leg: no provider row to curate against, so ``None`` (inherit the
    caller's model) stays a legal answer."""
    _mk_session(agents, "s-lane-native", "root-agent")
    seen = {}

    async def fake_worker(**kwargs):
        seen.update(kwargs)
        return WorkerOutcome(ok=True, session_id="w-native", report="r")

    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        payload = asyncio.run(_dispatch(
            {"agent": "researcher", "prompt": "go"}, "s-lane-native",
            agent_registry={"researcher": _definition()},
        ))

    assert payload["ok"] is True
    assert seen["model_id"] is None


def test_dispatcher_reads_the_agent_registry_off_the_ctx(agents):
    """NativeLoop seeds ctx.extra['agent_registry'] AFTER this dispatcher is
    built into the same ctx, so the read must happen at call time."""
    from types import SimpleNamespace

    _mk_session(agents, "s-ctx", "root-agent")
    seen = {}

    async def fake_worker(**kwargs):
        seen.update(kwargs)
        return WorkerOutcome(ok=True, session_id="w-ctx", report="r")

    dispatcher = td.make_fluid_dispatcher("s-ctx")
    ctx = SimpleNamespace(extra={})  # empty at injection time
    ctx.extra["agent_registry"] = {"researcher": _definition()}  # seeded later
    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        asyncio.run(dispatcher({"agent": "researcher", "prompt": "go"}, ctx))

    assert seen["profile_id"] == "root-agent"
    assert "careful researcher" in seen["system_prompt_append"]


# ─── delegated-leg Task parity (the bridged mcp__builtins__Task) ─────────────
#
# The native leg's Task reads fluid's discovered definitions off
# ctx.extra["agent_registry"]. The delegated (claude CLI) leg runs no fluid
# engine, so its Task tool discovers them itself from the session's cwd —
# without that a .agents/agents definition is advertised in the prompt (the
# adapter injects the same index there) and then answers "Unknown agent".


def _builtin_task_tool(session_id, profile_id, cwd):
    """The real mcp__builtins__Task tool, captured without an MCP transport."""
    import claude_agent_sdk as sdk
    import app.mcp.builtin_tools as bt

    captured = {}

    def _spy(name, version, tools):
        captured["tools"] = tools
        return {"name": name, "version": version, "tools": tools}

    with patch.object(sdk, "create_sdk_mcp_server", side_effect=_spy):
        bt.create_builtin_tools_server(
            session_id=session_id, profile_id=profile_id, project_id=None, cwd=cwd,
        )
    tools = {t.name: t for t in captured["tools"]}
    return tools["Task"]


def _builtin_task_handler(session_id, profile_id, cwd):
    return _builtin_task_tool(session_id, profile_id, cwd).handler


def _envelope(result):
    import json

    return json.loads(result["content"][0]["text"])


@pytest.fixture()
def file_agent_project(tmp_path, monkeypatch):
    """A cwd holding one .agents/agents definition, with HOME redirected so the
    developer's own global/plugin agents cannot leak into the assertion."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    project = tmp_path / "proj"
    agent_dir = project / ".agents" / "agents"
    agent_dir.mkdir(parents=True)
    (agent_dir / "researcher.md").write_text(
        "---\nname: researcher\ndescription: Reads the codebase\n---\n"
        "You are a careful researcher. Read before you write.\n",
        encoding="utf-8",
    )
    return project


def test_delegated_task_tool_dispatches_a_file_agent(agents, file_agent_project):
    _mk_session(agents, "s-cli", "root-agent")
    handler = _builtin_task_handler("s-cli", "root-agent", str(file_agent_project))
    seen = {}

    async def fake_worker(**kwargs):
        seen.update(kwargs)
        return WorkerOutcome(ok=True, session_id="w-cli", report="found it")

    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        payload = _envelope(asyncio.run(
            handler({"agent": "researcher", "prompt": "go"})
        ))

    assert payload["ok"] is True
    assert payload["agent"] == "researcher"
    assert seen["profile_id"] == "root-agent"        # borrowed, as on the native leg
    assert "careful researcher" in seen["system_prompt_append"]


def test_delegated_task_schema_advertises_discovered_file_agent(
    agents, file_agent_project
):
    tool = _builtin_task_tool("s-cli-schema", "root-agent", str(file_agent_project))

    assert "researcher" in tool.input_schema["properties"]["agent"]["enum"]
    assert "researcher" in tool.description


def test_delegated_task_tool_without_a_cwd_still_serves_db_agents(agents):
    """No cwd (a session built before the seam, or a DB-less spawn): discovery
    is skipped, DB agents keep working, a file name is an honest miss."""
    _mk_session(agents, "s-cli-2", "root-agent")
    handler = _builtin_task_handler("s-cli-2", "root-agent", None)

    async def fake_worker(**kwargs):
        return WorkerOutcome(ok=True, session_id="w2", report="r")

    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        ok = _envelope(asyncio.run(handler({"agent": "helper", "prompt": "go"})))
        miss = _envelope(asyncio.run(handler({"agent": "researcher", "prompt": "go"})))

    assert ok["ok"] is True and ok["agent"] == "helper"
    assert miss["ok"] is False and "not available" in miss["error"]


def test_discover_agent_registry_shape(file_agent_project):
    """{name: definition}, the same mapping NativeLoop publishes."""
    import app.mcp.builtin_tools as bt

    registry = bt._discover_agent_registry(str(file_agent_project))
    assert set(registry) == {"researcher"}
    assert registry["researcher"].body.strip().startswith("You are a careful")
    assert bt._discover_agent_registry(None) is None
