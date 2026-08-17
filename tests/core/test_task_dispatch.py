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
    assert payload["report"] == "done"
    assert payload["agent"] == "helper"


def test_enabled_agents_never_restricts_dispatch(agents):
    """(2026-08-17, superseding A10) enabled_agents declares context; an agent
    left OFF the list still dispatches."""
    _mk_session(agents, "s-locked", "locked")

    async def fake_worker(**kwargs):
        return WorkerOutcome(ok=True, session_id="w1", report="ok")

    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        undeclared = asyncio.run(
            _dispatch({"agent": "root-agent", "prompt": "go"}, "s-locked")
        )
        declared = asyncio.run(_dispatch({"agent": "helper", "prompt": "go"}, "s-locked"))
    assert undeclared["ok"] is True and undeclared["agent"] == "root-agent"
    assert declared["ok"] is True


@pytest.mark.parametrize("caller_pid", ["custom-empty", "custom-empty-list"])
def test_invoke_mode_custom_empty_still_dispatches(agents, caller_pid):
    """mode 'custom' with a missing/empty list declares NOBODY — and blocks
    nobody: a name the model already knows still runs."""
    session_id = f"s-{caller_pid}"
    _mk_session(agents, session_id, caller_pid)

    async def fake_worker(**kwargs):
        return WorkerOutcome(ok=True, session_id="w1", report="ok")

    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        for target in ("helper", "root-agent"):
            payload = asyncio.run(
                _dispatch({"agent": target, "prompt": "go"}, session_id)
            )
            assert payload["ok"] is True
            assert payload["agent"] == target


def test_invoke_mode_custom_dispatches_unlisted_agents(agents):
    _mk_session(agents, "s-custom-listed", "custom-listed")

    async def fake_worker(**kwargs):
        return WorkerOutcome(ok=True, session_id="w1", report="ok")

    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        unlisted = asyncio.run(
            _dispatch({"agent": "root-agent", "prompt": "go"}, "s-custom-listed")
        )
        listed = asyncio.run(
            _dispatch({"agent": "helper", "prompt": "go"}, "s-custom-listed")
        )
    assert unlisted["ok"] is True and unlisted["agent"] == "root-agent"
    assert listed["ok"] is True


def test_invoke_mode_all_dispatches_anything(agents):
    """mode 'all' declares everything and, like every other mode, restricts
    nothing."""
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
    """``model:`` rides through on an unbound (delegated) caller; ``tools:``
    becomes the worker's ENFORCED tool surface (CE12) and is stated in both the
    result note and the injected prompt."""
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
    assert "advisory" not in payload["note"]
    assert "restricted" in payload["note"] and "Read, Grep" in payload["note"]


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


# ─── worker leg resolution (ruling 2026-08-17, as refined) ───────────────────
#
# Rule 1  default = the TARGET's own provider+model, cross-provider included.
# Rule 2  the Claude-subscription leg is the ONLY leg that can't mix and match:
#         a Claude-sub target on a non-Claude-sub caller is re-routed (exact
#         slug -> same family -> delegated CLI anyway, with a note).
# Rule 3  an explicit ``model`` override is the exception path: the SLUG picks
#         the provider, and an unservable slug fails SYNCHRONOUSLY.
# Rule 4  the resolved leg's credentials are pre-flighted before anything spawns.


@pytest.fixture()
def providers(agents):
    """Three servable providers plus one that is not, all credentialed."""
    from app.db import database

    database.create_model_provider(
        "openai-1", "OpenAI", "openai", "https://api.openai.com",
        auth_token="enc:key", models=[{"id": "gpt-4o"}, {"id": "gpt-5.6-terra"}],
    )
    database.create_model_provider(
        "anthropic-1", "Anthropic API", "anthropic", "https://api.anthropic.com",
        auth_token="enc:key", models=[{"id": "claude-opus-4-6"}],
    )
    database.create_model_provider(
        "xai-1", "xAI", "openai", "https://api.x.ai",
        auth_token="enc:key", models=[{"id": "grok-9"}],
    )
    # OpenRouter is deliberately NOT servable for cross-provider workers
    # (environment.is_provider_servable), so it must never be picked.
    database.create_model_provider(
        "openrouter-1", "OpenRouter", "openrouter", "https://openrouter.ai/api",
        auth_token="enc:key", models=[{"id": "grok-9"}, {"id": "gpt-4o"}],
    )

    database.create_profile(
        "openai-agent", "OpenAI Agent", None,
        {"model": "gpt-4o", "provider": "openai-1"},
    )
    database.create_profile(
        "xai-agent", "xAI Agent", None, {"model": "grok-9", "provider": "xai-1"},
    )
    # Claude-subscription (delegated) agents: no provider binding at all.
    database.create_profile(
        "claude-exact", "Claude Exact", None, {"model": "claude-opus-4-6"},
    )
    database.create_profile("claude-alias", "Claude Alias", None, {"model": "opus"})
    database.create_profile("claude-haiku", "Claude Haiku", None, {"model": "haiku"})
    return database


def _spawn(providers_db, args, caller_session, caller_profile, **kw):
    """Dispatch ``args`` from a session on ``caller_profile``; return kwargs seen."""
    seen = {}

    async def fake_worker(**kwargs):
        seen.update(kwargs)
        return WorkerOutcome(ok=True, session_id="w", report="r")

    _mk_session(providers_db, caller_session, caller_profile)
    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        payload = asyncio.run(_dispatch(args, caller_session, **kw))
    return payload, seen


# --- rule 1: the target's own binding, untouched -----------------------------


def test_rule1_target_keeps_its_own_provider_across_providers(providers):
    """A caller on one provider spawning an agent bound to ANOTHER provider gets
    the target's binding verbatim - no reroute, no override needed."""
    payload, seen = _spawn(
        providers, {"agent": "xai-agent", "prompt": "go"}, "s-r1", "openai-agent",
    )
    assert payload["ok"] is True
    assert seen["provider_id"] == "xai-1"
    # model_id stays None = inherit the TARGET profile's own configured model.
    assert seen["model_id"] is None
    assert "note" not in payload


def test_rule1_claude_sub_caller_to_claude_sub_target_is_untouched(providers):
    payload, seen = _spawn(
        providers, {"agent": "claude-alias", "prompt": "go"}, "s-r1b", "root-agent",
    )
    assert payload["ok"] is True
    assert seen["provider_id"] is None and seen["model_id"] is None
    assert "note" not in payload


# --- rule 2: the Claude-subscription leg is the one special case -------------


def test_rule2_exact_slug_reroutes_claude_sub_target_to_an_api_provider(providers):
    """Non-Claude-sub caller + Claude-sub target: the target's model is served
    verbatim by a configured Anthropic-API provider, so bind there."""
    payload, seen = _spawn(
        providers, {"agent": "claude-exact", "prompt": "go"}, "s-r2a", "openai-agent",
    )
    assert payload["ok"] is True
    assert seen["provider_id"] == "anthropic-1"
    assert seen["model_id"] == "claude-opus-4-6"
    assert "Anthropic API" in payload["note"]


def test_rule2_family_match_when_no_provider_curates_the_exact_slug(providers):
    """'opus' is curated nowhere, but 'claude-opus-4-6' is the same FAMILY."""
    payload, seen = _spawn(
        providers, {"agent": "claude-alias", "prompt": "go"}, "s-r2b", "openai-agent",
    )
    assert payload["ok"] is True
    assert seen["provider_id"] == "anthropic-1"
    assert seen["model_id"] == "claude-opus-4-6"
    assert "family level" in payload["note"]


def test_rule2_falls_back_to_the_delegated_cli_with_a_note(providers):
    """Nothing curates haiku or its family - the delegated worker still spawns
    (it does function) and the result SAYS it ran on the subscription."""
    payload, seen = _spawn(
        providers, {"agent": "claude-haiku", "prompt": "go"}, "s-r2c", "openai-agent",
    )
    assert payload["ok"] is True
    assert seen["provider_id"] is None
    assert seen["model_id"] == "haiku"
    assert "Claude subscription" in payload["note"]


def test_rule2_never_picks_a_non_servable_provider(providers):
    """OpenRouter curates 'gpt-4o' too, but is not servable for workers."""
    binding = td.resolve_dispatch_binding(
        target_provider_id=None, target_model=None,
        model_override="gpt-4o", caller_provider_id=None,
    )
    assert binding.provider_id == "openai-1"


# --- rule 3: an explicit model override --------------------------------------


def test_rule3_override_binds_the_worker_to_the_callers_provider(providers):
    """Provider-less target + a slug the CALLER's provider curates -> the worker
    is BOUND to that provider (so the leg comes from the binding, not a sniff)."""
    payload, seen = _spawn(
        providers,
        {"agent": "claude-alias", "prompt": "go", "model": "gpt-4o"},
        "s-r3a", "openai-agent",
    )
    assert payload["ok"] is True
    assert seen["provider_id"] == "openai-1"
    assert seen["model_id"] == "gpt-4o"


def test_rule3_override_binds_to_a_third_provider(providers):
    """The slug belongs to neither the caller's nor the target's provider - it
    still resolves, to the one provider that curates it."""
    payload, seen = _spawn(
        providers,
        {"agent": "claude-alias", "prompt": "go", "model": "grok-9"},
        "s-r3b", "openai-agent",
    )
    assert payload["ok"] is True
    assert seen["provider_id"] == "xai-1"
    assert seen["model_id"] == "grok-9"
    assert "xAI" in payload["note"]


def test_rule3_unknown_slug_fails_synchronously(providers):
    """The live failure this whole change exists for: an unservable slug used to
    reach the worker and 401 half a second after ok:true."""
    seen = {}

    async def fake_worker(**kwargs):  # pragma: no cover - must never run
        seen.update(kwargs)
        return WorkerOutcome(ok=True, session_id="w", report="r")

    _mk_session(providers, "s-r3c", "openai-agent")
    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        payload = asyncio.run(_dispatch(
            {"agent": "claude-alias", "prompt": "go", "model": "gpt-5.6-nope"},
            "s-r3c",
        ))
    assert payload["ok"] is False
    assert payload["error"].startswith(
        "Model 'gpt-5.6-nope' is not available on any configured provider. "
        "The target agent's default is 'opus' — omit 'model' to use it."
    )
    assert "Valid model ids include:" in payload["error"]
    assert seen == {}  # nothing was spawned


def test_rule3_background_mode_also_fails_synchronously(providers):
    """Background dispatch must not answer ok:true for a slug it cannot serve."""
    _mk_session(providers, "s-r3c2", "openai-agent")
    payload = asyncio.run(_dispatch(
        {"agent": "claude-alias", "prompt": "go", "model": "nope-9",
         "mode": "background"},
        "s-r3c2",
    ))
    assert payload["ok"] is False and "not available" in payload["error"]


def test_rule3_claude_slug_stays_delegated_for_a_claude_sub_caller(providers):
    """Claude-sub caller + Claude-sub target: the CLI leg is preserved even
    though an Anthropic-API provider curates the very same id."""
    payload, seen = _spawn(
        providers,
        {"agent": "claude-alias", "prompt": "go", "model": "claude-opus-4-6"},
        "s-r3d", "root-agent",
    )
    assert payload["ok"] is True
    assert seen["provider_id"] is None
    assert seen["model_id"] == "claude-opus-4-6"


def test_rule3_claude_slug_off_claude_sub_prefers_an_api_provider(providers):
    """An override only the subscription could serve, asked by a session that is
    not on it, takes rule 2's preference chain instead."""
    payload, seen = _spawn(
        providers,
        {"agent": "claude-alias", "prompt": "go", "model": "opus"},
        "s-r3e", "openai-agent",
    )
    assert payload["ok"] is True
    assert seen["provider_id"] == "anthropic-1"
    assert seen["model_id"] == "claude-opus-4-6"


def test_rule3_file_agent_takes_the_same_resolution(providers):
    """The file-agent branch resolves cross-provider too, instead of silently
    dropping a model its caller's provider does not curate."""
    payload, seen = _spawn(
        providers,
        {"agent": "researcher", "prompt": "go"}, "s-r3f", "openai-agent",
        agent_registry={"researcher": _definition(model="grok-9")},
    )
    assert payload["ok"] is True
    assert seen["provider_id"] == "xai-1"
    assert seen["model_id"] == "grok-9"


def test_rule3_file_agent_still_spawns_when_nothing_serves_its_model(providers):
    """Drop-with-note survives as the FINAL fallback: a file agent must spawn."""
    payload, seen = _spawn(
        providers,
        {"agent": "researcher", "prompt": "go"}, "s-r3g", "openai-agent",
        agent_registry={"researcher": _definition(model="totally-made-up")},
    )
    assert payload["ok"] is True
    assert seen["provider_id"] == "openai-1"      # the caller's own binding
    assert seen["model_id"] is None               # dropped, not fatal
    assert "inherits the session's model" in payload["note"]


# --- rule 4: credential pre-flight -------------------------------------------


def test_rule4_missing_credentials_fail_synchronously(providers):
    """A provider row with no key at all: the worker would die at connect, so it
    is never spawned."""
    from app.db import database

    database.update_model_provider("xai-1", auth_token="")
    seen = {}

    async def fake_worker(**kwargs):  # pragma: no cover - must never run
        seen.update(kwargs)
        return WorkerOutcome(ok=True, session_id="w", report="r")

    _mk_session(providers, "s-r4", "openai-agent")
    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        payload = asyncio.run(_dispatch(
            {"agent": "xai-agent", "prompt": "go"}, "s-r4",
        ))
    assert payload["ok"] is False
    assert "no credentials configured" in payload["error"]
    assert seen == {}


def test_rule4_credential_check_is_skipped_for_the_delegated_leg(providers):
    """A provider-less worker is the app's default path; its credentials live in
    the CLI's own config, so the pre-flight must not refuse it."""
    assert td._credential_gap(None) is None


def test_rule4_needs_reauth_chatgpt_row_is_refused(providers):
    from app.db import database

    database.create_model_provider(
        "chatgpt-1", "ChatGPT", "chatgpt", "https://chatgpt.com/backend-api",
        models=[{"id": "gpt-5-codex"}],
    )
    database.update_model_provider(
        "chatgpt-1", codex_access_token="enc:tok", needs_reauth=True,
    )
    assert "reconnected" in (td._credential_gap("chatgpt-1") or "")


# --- helper units ------------------------------------------------------------


def test_model_family_resolves_aliases_and_dated_ids():
    assert td._model_family("opus") == "opus"
    assert td._model_family("claude-opus-4-6") == "opus"
    assert td._model_family("claude-sonnet-4-20250514") == "sonnet"
    assert td._model_family("gpt-4o") is None


def test_multiple_curating_providers_resolve_deterministically(providers):
    """Two servable providers curate the same slug - the pick is stable, and the
    CALLER's own provider wins when it is one of them."""
    from app.db import database

    database.create_model_provider(
        "azure-1", "Azure OpenAI", "openai", "https://azure.example",
        auth_token="enc:key", models=[{"id": "gpt-4o"}],
    )
    # No caller binding: stable (name, id) order -> "Azure OpenAI" before "OpenAI".
    assert td.resolve_dispatch_binding(
        target_provider_id=None, target_model=None,
        model_override="gpt-4o", caller_provider_id=None,
    ).provider_id == "azure-1"
    # The caller's own provider wins the tie.
    assert td.resolve_dispatch_binding(
        target_provider_id=None, target_model=None,
        model_override="gpt-4o", caller_provider_id="openai-1",
    ).provider_id == "openai-1"
