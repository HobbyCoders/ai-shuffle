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


# ─── delegated-leg Task parity (the bridged mcp__builtins__Task) ─────────────
#
# The native leg's Task reads fluid's discovered definitions off
# ctx.extra["agent_registry"]. The delegated (claude CLI) leg runs no fluid
# engine, so its Task tool discovers them itself from the session's cwd —
# without that a .claude/agents definition is advertised in the prompt (the
# adapter injects the same index there) and then answers "Unknown agent".


def _builtin_task_handler(session_id, profile_id, cwd):
    """The real mcp__builtins__Task handler, invoked without an MCP transport."""
    import claude_agent_sdk as sdk
    import app.mcp.builtin_tools as bt

    captured = {}
    real_create = sdk.create_sdk_mcp_server

    def _spy(name, version, tools):
        captured["tools"] = tools
        return real_create(name=name, version=version, tools=tools)

    with patch.object(sdk, "create_sdk_mcp_server", side_effect=_spy):
        bt.create_builtin_tools_server(
            session_id=session_id, profile_id=profile_id, project_id=None, cwd=cwd,
        )
    tools = {t.name: t for t in captured["tools"]}
    return tools["Task"].handler


def _envelope(result):
    import json

    return json.loads(result["content"][0]["text"])


@pytest.fixture()
def file_agent_project(tmp_path, monkeypatch):
    """A cwd holding one .claude/agents definition, with HOME redirected so the
    developer's own global/plugin agents cannot leak into the assertion."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    project = tmp_path / "proj"
    agent_dir = project / ".claude" / "agents"
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

    assert payload["success"] is True
    assert payload["agent"] == "researcher"
    assert seen["profile_id"] == "root-agent"        # borrowed, as on the native leg
    assert "careful researcher" in seen["system_prompt_append"]


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

    assert ok["success"] is True and ok["agent"] == "helper"
    assert miss["success"] is False and "Unknown agent" in miss["error"]


def test_discover_agent_registry_shape(file_agent_project):
    """{name: definition}, the same mapping NativeLoop publishes."""
    import app.mcp.builtin_tools as bt

    registry = bt._discover_agent_registry(str(file_agent_project))
    assert set(registry) == {"researcher"}
    assert registry["researcher"].body.strip().startswith("You are a careful")
    assert bt._discover_agent_registry(None) is None


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
    """Four servable providers plus one that is not, all credentialed."""
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
    # OpenRouter IS servable for cross-provider workers as of the 2026-08-17
    # ruling (environment.is_provider_servable) — fluid drives it natively.
    database.create_model_provider(
        "openrouter-1", "OpenRouter", "openrouter", "https://openrouter.ai/api",
        auth_token="enc:key", models=[{"id": "moonshotai/kimi-k2"}],
    )
    # A ``custom`` row is the remaining non-servable kind (its leg depends on a
    # user-supplied base_url, not on the type), so it must never be picked. Its
    # name sorts FIRST, so any test it does not win proves the servable gate ran
    # rather than the (name, id) ordering.
    database.create_model_provider(
        "custom-1", "Custom Endpoint", "custom", "https://custom.example",
        auth_token="enc:key", models=[{"id": "gpt-4o"}, {"id": "grok-9"}],
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


# --- rule 1, the CLAUDE-SUB CALLER direction ---------------------------------
#
# The mirror of the tests above: the caller sits on the Claude subscription
# (delegated CLI leg — a profile with no ``provider`` binding at all) and
# dispatches an agent bound to a real provider row. Rule 2 keys on the TARGET
# being Claude-sub, so none of it applies here: the target's own provider+model
# must survive verbatim, exactly as for a native-leg caller.


def test_claude_sub_caller_to_provider_bound_target_keeps_the_targets_binding(
    providers,
):
    payload, seen = _spawn(
        providers, {"agent": "openai-agent", "prompt": "go"}, "s-cs1", "root-agent",
    )
    assert payload["ok"] is True
    assert seen["provider_id"] == "openai-1"
    # None = inherit the TARGET profile's own configured model (gpt-4o).
    assert seen["model_id"] is None
    assert "note" not in payload


def test_claude_sub_caller_reaches_every_bound_provider(providers):
    """Not just the first one: a Claude-sub caller mixes and matches freely."""
    payload, seen = _spawn(
        providers, {"agent": "xai-agent", "prompt": "go"}, "s-cs2", "root-agent",
    )
    assert payload["ok"] is True
    assert seen["provider_id"] == "xai-1"
    assert seen["model_id"] is None


def test_claude_sub_caller_cannot_spend_a_non_claude_override(providers):
    """(2026-08-17 override policy) A Claude-sub caller has no provider row, so
    its only currency is the Claude family: an OpenAI slug is DROPPED and the
    target runs as configured."""
    payload, seen = _spawn(
        providers,
        {"agent": "claude-alias", "prompt": "go", "model": "gpt-4o"},
        "s-cs3", "root-agent",
    )
    assert payload["ok"] is True
    assert seen["provider_id"] is None       # the target's own (claude-sub) leg
    assert seen["model_id"] is None          # inherit 'opus', the configured one
    assert "isn't curated on your provider" in payload["note"]


def test_claude_sub_caller_override_cannot_move_a_bound_target(providers):
    """A provider-BOUND target plus a slug the CALLER cannot vouch for: the
    override is dropped and the target keeps its own binding."""
    payload, seen = _spawn(
        providers,
        {"agent": "openai-agent", "prompt": "go", "model": "grok-9"},
        "s-cs4", "root-agent",
    )
    assert payload["ok"] is True
    assert seen["provider_id"] == "openai-1"
    assert seen["model_id"] is None
    assert "isn't curated on your provider" in payload["note"]
    assert "OpenAI Agent" in payload["note"]  # the note names the agent


def test_claude_sub_caller_override_dropped_even_on_the_targets_own_provider(
    providers,
):
    """Step (a) no longer stands on its own: the target's provider curating the
    slug is irrelevant when the CALLER has no row that curates it."""
    payload, seen = _spawn(
        providers,
        {"agent": "openai-agent", "prompt": "go", "model": "gpt-5.6-terra"},
        "s-cs5", "root-agent",
    )
    assert payload["ok"] is True
    assert seen["provider_id"] == "openai-1"
    assert seen["model_id"] is None
    assert "isn't curated on your provider" in payload["note"]


def test_claude_sub_caller_to_openrouter_bound_target_keeps_the_targets_binding(
    providers,
):
    """2026-08-17 ruling: an OpenRouter row IS
    ``environment.is_provider_servable``, so an agent bound to one is a legal
    worker target — rule 1 applies to it like any other native leg."""
    from app.db import database

    database.create_profile(
        "openrouter-agent", "OpenRouter Agent", None,
        {"model": "moonshotai/kimi-k2", "provider": "openrouter-1"},
    )
    payload, seen = _spawn(
        providers, {"agent": "openrouter-agent", "prompt": "go"}, "s-cs6", "root-agent",
    )
    assert payload["ok"] is True
    assert seen["provider_id"] == "openrouter-1"
    # None = inherit the TARGET profile's own model (moonshotai/kimi-k2).
    assert seen["model_id"] is None
    assert "note" not in payload


def test_openrouter_bound_target_is_servable_from_a_native_caller_too(providers):
    """Not just the Claude-sub direction: an OpenAI-bound caller reaches it, and
    an explicit override on an OpenRouter slug resolves to that row."""
    from app.db import database

    database.create_profile(
        "openrouter-agent2", "OpenRouter Agent 2", None,
        {"model": "moonshotai/kimi-k2", "provider": "openrouter-1"},
    )
    payload, seen = _spawn(
        providers,
        {"agent": "openrouter-agent2", "prompt": "go"}, "s-cs7", "openai-agent",
    )
    assert payload["ok"] is True
    assert seen["provider_id"] == "openrouter-1"

    # ...but an OpenAI-bound caller may NOT hand that same slug to another agent
    # as an override: its own provider does not curate it (2026-08-17 policy).
    payload, seen = _spawn(
        providers,
        {"agent": "openrouter-agent2", "prompt": "go", "model": "moonshotai/kimi-k2"},
        "s-cs8", "openai-agent",
    )
    assert payload["ok"] is True
    assert seen["provider_id"] == "openrouter-1"   # the target's own binding
    assert seen["model_id"] is None
    assert "isn't curated on your provider" in payload["note"]


def test_custom_bound_target_is_still_refused(providers):
    """The narrowed refusal that survives: a ``custom`` row's leg is not implied
    by its type (base_url + api_dialect decide), so it cannot be a worker
    target."""
    from app.db import database

    database.create_profile(
        "custom-agent", "Custom Agent", None,
        {"model": "grok-9", "provider": "custom-1"},
    )
    payload, seen = _spawn(
        providers, {"agent": "custom-agent", "prompt": "go"}, "s-cs9", "root-agent",
    )
    assert payload["ok"] is False
    assert "cannot serve worker sessions" in payload["error"]
    assert seen == {}


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
    """The custom row curates 'grok-9' too and sorts FIRST by name, but is not
    servable for workers, so the xAI row still wins the rule-2 reroute."""
    binding = td.resolve_dispatch_binding(
        target_provider_id=None, target_model="grok-9",
        model_override=None, caller_provider_id="openai-1",
    )
    assert binding.provider_id == "xai-1"


def test_rule2_prefers_a_subscription_row_over_an_api_row(providers):
    """Fallback ordering: when the caller's own provider is not among the rows
    curating the model, a SUBSCRIPTION-auth row beats a metered API-key one —
    even though its name sorts last."""
    from app.db import database

    database.create_model_provider(
        "codex-1", "ZZZ Codex", "chatgpt", "https://chatgpt.com/backend-api",
        models=[{"id": "claude-opus-4-6"}],
    )
    database.update_model_provider("codex-1", codex_access_token="enc:tok")

    binding = td.resolve_dispatch_binding(
        target_provider_id=None, target_model="claude-opus-4-6",
        model_override=None, caller_provider_id="openai-1",
    )
    assert binding.provider_id == "codex-1"
    # The API row is still the pick once the subscription row is gone.
    database.update_model_provider("codex-1", enabled=False)
    assert td.resolve_dispatch_binding(
        target_provider_id=None, target_model="claude-opus-4-6",
        model_override=None, caller_provider_id="openai-1",
    ).provider_id == "anthropic-1"


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


def test_rule3_override_onto_a_third_provider_is_dropped(providers):
    """THE regression the policy exists for (live incident, gpt-5.4-mini shape):
    a caller names a slug only some OTHER provider curates. It is DROPPED — the
    target keeps its own configured provider+model — and the caller is told."""
    payload, seen = _spawn(
        providers,
        {"agent": "xai-agent", "prompt": "go", "model": "gpt-5.4-mini"},
        "s-r3b", "openai-agent",
    )
    assert payload["ok"] is True
    assert seen["provider_id"] == "xai-1"     # the TARGET's own binding
    assert seen["model_id"] is None           # ...and its own configured model
    assert "gpt-5.4-mini" in payload["note"]
    assert "isn't curated on your provider" in payload["note"]
    assert "xAI Agent" in payload["note"]


def test_rule3_override_the_caller_curates_is_honored(providers):
    """The other side of the gate: the caller's OWN provider curates the slug,
    so the override binds — a caller may redirect onto what it already pays for."""
    payload, seen = _spawn(
        providers,
        {"agent": "claude-alias", "prompt": "go", "model": "gpt-5.6-terra"},
        "s-r3b2", "openai-agent",
    )
    assert payload["ok"] is True
    assert seen["provider_id"] == "openai-1"
    assert seen["model_id"] == "gpt-5.6-terra"


def test_rule3_unknown_slug_is_dropped_not_fatal(providers):
    """A slug NO provider has ever heard of must never fail a dispatch — the
    worker runs the target as configured with a note."""
    payload, seen = _spawn(
        providers,
        {"agent": "xai-agent", "prompt": "go", "model": "gpt-5.6-nope"},
        "s-r3c", "openai-agent",
    )
    assert payload["ok"] is True
    assert seen["provider_id"] == "xai-1"
    assert seen["model_id"] is None
    assert "gpt-5.6-nope" in payload["note"]


def test_rule3_background_mode_also_drops_rather_than_fails(providers):
    """Background dispatch answers ok:true too — the drop is a note, not an
    error, on both modes."""
    payload, seen = _spawn(
        providers,
        {"agent": "xai-agent", "prompt": "go", "model": "nope-9",
         "mode": "background"},
        "s-r3c2", "openai-agent",
    )
    assert payload["ok"] is True
    assert "isn't curated on your provider" in payload["note"]
    assert seen["provider_id"] == "xai-1"
    assert seen["model_id"] is None


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
    """The file-agent branch runs the same gate: its ``model:`` binds when the
    borrowed (caller's) provider curates it..."""
    payload, seen = _spawn(
        providers,
        {"agent": "researcher", "prompt": "go"}, "s-r3f", "openai-agent",
        agent_registry={"researcher": _definition(model="gpt-5.6-terra")},
    )
    assert payload["ok"] is True
    assert seen["provider_id"] == "openai-1"
    assert seen["model_id"] == "gpt-5.6-terra"


def test_rule3_file_agent_model_the_caller_cannot_serve_is_dropped(providers):
    """...and degrades to the session's own model when it does not, instead of
    binding the worker to a provider the caller never chose."""
    payload, seen = _spawn(
        providers,
        {"agent": "researcher", "prompt": "go"}, "s-r3f2", "openai-agent",
        agent_registry={"researcher": _definition(model="grok-9")},
    )
    assert payload["ok"] is True
    assert seen["provider_id"] == "openai-1"      # the caller's own binding
    assert seen["model_id"] is None
    assert "inherits the session's model" in payload["note"]


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
    """Two servable providers curate the same slug - the enumeration order is
    stable, and the CALLER's own provider wins when it is one of them."""
    from app.db import database

    database.create_model_provider(
        "azure-1", "Azure OpenAI", "openai", "https://azure.example",
        auth_token="enc:key", models=[{"id": "gpt-4o"}],
    )
    # Stable (name, id) order -> "Azure OpenAI" before "OpenAI"; the non-servable
    # "Custom Endpoint" curates gpt-4o too and sorts first, but never appears.
    assert [
        r["id"] for r in td._servable_providers_curating("gpt-4o")
    ] == ["azure-1", "openai-1"]
    # The caller's own provider wins the tie when the override is admitted.
    assert td.resolve_dispatch_binding(
        target_provider_id=None, target_model="claude-opus-4-6",
        model_override="gpt-4o", caller_provider_id="openai-1",
    ).provider_id == "openai-1"


def test_note_names_the_subscription_when_a_row_wins_over_it(providers):
    """The note used to compare the chosen row against the TARGET only, so
    "your own subscription was passed over" could never be said. Branch (c) now
    compares against the CALLER's leg too and names BOTH."""
    binding = td.resolve_dispatch_binding(
        target_provider_id="openai-1", target_model="gpt-4o",
        model_override="claude-opus-4-6", caller_provider_id=None,
    )
    assert binding.provider_id == "anthropic-1"
    assert "the target agent's own binding" in (binding.note or "")
    assert "Claude subscription" in (binding.note or "")
