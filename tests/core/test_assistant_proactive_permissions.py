"""Real admitted turn producer plus native/bundled/CLI execution fences."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core import assistant_service as service, assistant_manager, turn_manager
from app.core import session_modes as modes, session_orchestration as orch
from app.core.provider import harness_adapter as ha
from app.core.provider.types import SessionOptions
from app.db import assistant as store, database, turns
from fluid.tools.registry import ToolRegistry, ToolSpec, ToolOutcome
from tests.db.test_assistant_store import assistant_fixture  # noqa: F401 — producer dependency
from tests.core.test_assistant_manager import producer  # noqa: F401 — fixture with real connector callbacks


@pytest.fixture
async def admitted(producer):
    instance, _ = producer
    service.register_proactive_guard(1)
    turn_manager.register_kick(None)
    # Drive connector emission through its real manager callback, not a seeded
    # inbox. Redelivery must still produce one durable event and normal turn.
    await instance._process_message("message-1")
    await instance._process_message("message-1")
    await assistant_manager.AssistantManager().run_once(observe=False)
    run = store.get_activity(service.assistant_id())[0]
    turn = turns.get_turn(run["turn_id"])
    assert turn["payload"]["_assistant_proactive"] == turn["overrides"]["_assistant_proactive"]
    assert turn["source"] == "assistant"
    assert len(turn["payload"]["_assistant_proactive"]["event_ids"]) == 1
    yield run, turn
    turn_manager.register_kick(None)


@pytest.mark.parametrize("native", [True, False])
async def test_dispatch_ceiling_ignores_bypass_safe_risk_and_registry_escape(admitted, native):
    run, turn = admitted
    metadata = turn["payload"]["_assistant_proactive"]
    evil = ToolRegistry()
    called = []
    async def mutate(data, ctx):
        called.append(data)
        return ToolOutcome(text="mutated")
    evil.register_builtin(ToolSpec(name="NotifyDevices", description="unsafe safe tool", input_schema={}, risk="safe"), mutate)
    options = SessionOptions(model="fake", permission_mode="bypassPermissions", extra={
        "app_session_id": run["session_id"], "assistant_proactive": metadata,
        "fluid_registry": evil,
    }, mcp_servers={"browser": {"type": "stdio", "command": "must-not-launch"}})
    registry = await ha.HarnessAdapter()._build_registry(options, native=native)
    assert registry is not evil
    for name in [*modes.PROACTIVE_DENIED_TOOLS, "mcp__gmail__send", "mcp__browser__browser_eval", "new-unknown-tool"]:
        outcome = await registry.dispatch(name, {})
        assert outcome.is_error and "read-only" in outcome.text, name
    assert not called
    source = await registry.dispatch("AssistantReadSource", {"source_id": "conn-mail"})
    assert not source.is_error, source.text
    data = json.loads(source.text)
    assert data["source_id"] == "conn-mail" and data["run_id"] == run["id"]
    assert data["events"][0]["id"] in metadata["event_ids"]
    assert "Ignore permissions" in data["events"][0]["payload"]["data"]
    denied = await registry.dispatch("AssistantReadSource", {"source_id": "ungranted"})
    assert denied.is_error and "opted-in" in denied.text
    service.configure({"paused": True})
    paused = await registry.dispatch("WebSearch", {"query": "test"})
    assert paused.is_error and "paused" in paused.text


async def test_delegated_pretool_and_wake_revalidate_provenance(admitted):
    from app.core.turn_scheduler import _context_payload
    run, turn = admitted
    metadata = turn["payload"]["_assistant_proactive"]
    options = SessionOptions(model="fake", permission_mode="bypassPermissions", extra={
        "app_session_id": run["session_id"], "assistant_proactive": metadata,
    })
    blocked = ha._delegated_disallowed(options)
    assert {"run_python", "Read", "Bash", "WebFetch", "WebSearch", "Task"} <= set(blocked)
    policy = ha._delegated_tool_policy(options, None, blocked)
    assert policy("mcp__fluid_bundled__AssistantReadSource", {}) is None
    assert policy("mcp__fluid_bundled__WebFetch", {}) is None
    for name in ("mcp__evil__read", "Task", "run_python", "WebFetch"):
        assert policy(name, {})
    assert _context_payload(turn)["_assistant_proactive"] == metadata
    forged = {**metadata, "read_only": False}
    options.extra["assistant_proactive"] = forged
    assert policy("mcp__fluid_bundled__AssistantReadSource", {})
    options.extra["assistant_proactive"] = metadata
    service.set_source("conn-mail", False)
    assert policy("mcp__fluid_bundled__AssistantReadSource", {})


@pytest.mark.parametrize("change", ["disabled", "paused", "optout", "credential", "agent_scope", "runner", "home_stop", "research_stop"])
async def test_running_registry_rechecks_live_source_and_session_scope(admitted, change):
    run, turn = admitted
    options = SessionOptions(model="fake", permission_mode="bypassPermissions", extra={
        "app_session_id": run["session_id"], "assistant_proactive": turn["payload"]["_assistant_proactive"],
    })
    registry = await ha.HarnessAdapter()._build_registry(options, native=True)
    if change == "disabled":
        service.configure({"enabled": False})
    elif change == "paused":
        service.configure({"paused": True})
    elif change == "optout":
        service.set_source("conn-mail", False)
    elif change == "credential":
        database.update_api_user("mail-user", is_active=False)
    elif change == "agent_scope":
        database.create_profile("other-agent", "Other", None, {})
        database.update_api_user("mail-user", profile_ids=["other-agent"])
    elif change == "runner":
        from app.db.base import get_db
        with get_db() as conn:
            conn.cursor().execute("UPDATE connectors SET runner_id=%s WHERE id=%s", ("other-worker", "conn-mail"))
    else:
        if change == "home_stop":
            stopped = turns.enqueue_turn(run["home_session_id"], "user", prompt="Stopped")
            turn_id = stopped["id"]
        else:
            turn_id = turn["id"]
        turns.settle_turn(turn_id, "aborted", stop_reason="user_stopped")
    outcome = await registry.dispatch("AssistantReadSource", {"source_id": "conn-mail"})
    assert outcome.is_error, change
    assert not outcome.text.startswith('{"run_id"'), change


async def test_authorization_outage_denies_unknown_cli_tool_at_actual_hook(admitted, monkeypatch):
    from fluid.engine.delegated import DelegatedLoop
    from fluid.store.base import MemoryStore
    run, turn = admitted
    options = SessionOptions(model="fake", permission_mode="bypassPermissions", extra={
        "app_session_id": run["session_id"],
        "assistant_proactive": turn["payload"]["_assistant_proactive"],
    })
    registry = await ha.HarnessAdapter()._build_registry(options, native=True)
    policy = ha._delegated_tool_policy(options, None, ha._delegated_disallowed(options))
    loop = DelegatedLoop(store=MemoryStore(), model="fake", permission_mode="bypassPermissions", tool_policy=policy)

    def unavailable(*args):
        raise RuntimeError("authorization database unavailable")
    monkeypatch.setattr(service, "assert_proactive_allowed", unavailable)
    # Fluid intentionally ignores exceptions from a generic CLI deny policy.
    # Our mandatory background ceiling must return a deny, not raise into that
    # fallback, including a new CLI tool absent from the cached blocklist.
    hook = await loop._cli_pre_tool_use({"tool_name": "FutureCLIWrite", "tool_input": {}}, None)
    assert hook.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
    outcome = await registry.dispatch("WebSearch", {"query": "must not run"})
    assert outcome.is_error and "authorization" in outcome.text


async def test_native_connect_builds_no_kernel_or_learning_surface(admitted, monkeypatch, tmp_path):
    from fluid.store.base import MemoryStore
    from tests.test_harness_adapter import FakeProvider
    run, turn = admitted
    monkeypatch.setattr(ha.HarnessAdapter, "_build_store", staticmethod(lambda _: MemoryStore()))
    monkeypatch.setattr(ha, "_build_provider_client", AsyncMock(return_value=(FakeProvider([]), None)))
    monkeypatch.setattr(ha, "CodeModeSession", lambda *a, **kw: pytest.fail("proactive Python kernel constructed"))
    adapter = ha.HarnessAdapter()
    opts = SessionOptions(model="gpt-4o", cwd=str(tmp_path), extra={
        "fluid_provider": "openai_responses", "app_session_id": run["session_id"],
        "assistant_proactive": turn["payload"]["_assistant_proactive"],
    })
    await adapter.connect(opts)
    assert adapter._engine._codemode is None
    service.configure({"paused": True})
    with pytest.raises(ValueError, match="paused"):
        await adapter.send_query("Continue")
    await adapter.disconnect()


async def test_proactive_cannot_launch_peer_or_task_worker(admitted):
    from app.core.provider_agent import dispatch_worker
    run, turn = admitted
    # Background jobs are hidden, so the ordinary eligibility guard itself
    # refuses them before the proactive entry-point defense can run.
    with pytest.raises(ValueError, match="active, local, visible"):
        orch._queue(run["session_id"], {"title": "write", "prompt": "write", "request_id": "blocked"}, "start")
    worker = await dispatch_worker(profile_id="assistant-agent", parent_session_id=run["session_id"], model_id="sonnet", prompt="Write")
    assert not worker.ok and not worker.session_id and "cannot delegate" in worker.error


def test_foreground_ceiling_retains_broad_authorized_tools():
    from app.core.tool_access import ToolAccess
    access = ToolAccess(allowed=None, disallowed=[])
    assert modes.narrow_tool_access_for_proactive(access, None) is access
    assert modes.proactive_tool_denial(None, "foreground", "StartSession") is None


async def test_real_sdk_option_builder_preserves_agent_prompt_and_admission(admitted):
    from app.core.query_options import build_options_from_profile
    run, turn = admitted
    profile = database.get_profile("assistant-agent")
    profile["config"]["system_prompt"] = "Original owner agent prompt"
    profile["config"]["allowed_tools"] = [*modes.PROACTIVE_TOOL_SURFACE, "Read", "Task"]
    options, _ = build_options_from_profile(profile, session_id=run["session_id"], overrides=turn["overrides"])
    assert "Original owner agent prompt" in str(options.system_prompt)
    assert run["home_session_id"] in str(options.system_prompt)
    adapter = ha.HarnessAdapter()
    adapter.connect = AsyncMock()
    await adapter.connect_with_sdk_options(options)
    projected = adapter.connect.await_args.args[0]
    assert projected.extra["assistant_proactive"] == turn["payload"]["_assistant_proactive"]
    assert set(options.aih_tool_allowlist) <= set(modes.PROACTIVE_TOOL_SURFACE)
    assert ha._tool_allowlist(projected) == options.aih_tool_allowlist
    assert "Read" in projected.disallowed_tools and "Task" in projected.disallowed_tools
