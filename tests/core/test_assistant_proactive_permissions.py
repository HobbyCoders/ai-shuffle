"""Real admitted watch producer plus native/bundled/CLI execution fences."""

from __future__ import annotations
from typing import Any
from pathlib import Path
from pytest import MonkeyPatch
from unittest.mock import AsyncMock

import pytest

from app.core import assistant_service as service, session_modes as modes, watches
from app.core import watch_manager
from app.core.provider import harness_adapter as ha
from app.core.provider.types import SessionOptions
from app.db import database, turns, watches as store
from app.db.base import get_db
from fluid.tools.registry import ToolRegistry, ToolSpec, ToolOutcome
from tests.core.test_watches import owner, mail, make_watch  # noqa: F401


@pytest.fixture
async def admitted(owner: dict[str, Any], mail: Any) -> Any:
    connector, instance, sdk = mail
    watch = make_watch(
        owner,
        source={"kind": "connector", "connector_id": connector["id"]},
        permissions={"tools": ["WebSearch", "WebFetch"]},
    )
    await watch_manager.WatchManager().run_once()  # Initial Gmail history cursor.
    sdk.ids = ["new-message"]
    sdk.history_id = "101"
    await watch_manager.WatchManager().run_once()  # Real producing history path.
    event = store.events(watch["id"])[0]
    turn = turns.get_turn(event["turn_id"])
    assert turn["payload"]["_assistant_proactive"] == turn["overrides"]["_assistant_proactive"]
    return watch, event, turn, connector


def options(admitted: Any) -> Any:
    watch, event, turn, connector = admitted
    return SessionOptions(
        model="fake",
        permission_mode="bypassPermissions",
        extra={
            "app_session_id": event["session_id"],
            "assistant_proactive": turn["payload"]["_assistant_proactive"],
        },
    )


@pytest.mark.parametrize("native", [True, False])
async def test_registry_cannot_escape_through_safe_tools_or_supplied_registry(
    admitted: Any, native: bool
) -> None:
    opt = options(admitted)
    evil = ToolRegistry()
    called = []

    async def mutate(data: Any, ctx: Any) -> Any:
        called.append(data)
        return ToolOutcome(text="mutated")

    evil.register_builtin(
        ToolSpec(
            name="NotifyDevices", description="unsafe safe tool", input_schema={}, risk="safe"
        ),
        mutate,
    )
    opt.extra["fluid_registry"] = evil
    opt.mcp_servers = {"browser": {"type": "stdio", "command": "must-not-launch"}}
    registry = await ha.HarnessAdapter()._build_registry(opt, native=native)
    assert registry is not evil
    for name in [
        *modes.PROACTIVE_DENIED_TOOLS,
        "mcp__gmail__send",
        "mcp__browser__browser_eval",
        "FutureCLIWrite",
        "AssistantReadSource",
    ]:
        outcome = await registry.dispatch(name, {})
        assert outcome.is_error and "read-only" in outcome.text, name
    assert not called
    watch, event, turn, connector = admitted
    store.set_status(watch["id"], "admin", "paused")
    assert (await registry.dispatch("WebSearch", {"query": "test"})).is_error


async def test_delegated_hook_and_wake_revalidate_provenance(admitted: Any) -> None:
    from app.core.turn_scheduler import _context_payload
    opt = options(admitted)
    blocked = ha._delegated_disallowed(opt)
    assert {"run_python", "Read", "Bash", "WebFetch", "WebSearch", "Task"} <= set(blocked)
    policy = ha._delegated_tool_policy(opt, None, blocked)
    assert policy("mcp__fluid_bundled__WebFetch", {}) is None
    for name in (
        "mcp__evil__read",
        "Task",
        "run_python",
        "WebFetch",
        "mcp__fluid_bundled__AssistantReadSource",
    ):
        assert policy(name, {})
    watch, event, turn, connector = admitted
    assert _context_payload(turn)["_assistant_proactive"] == opt.extra["assistant_proactive"]
    opt.extra["assistant_proactive"] = {**opt.extra["assistant_proactive"], "read_only": False}
    assert policy("mcp__fluid_bundled__WebFetch", {})


@pytest.mark.parametrize(
    "change", ["agent", "pause", "credential", "agent_scope", "runner", "session_stop"]
)
async def test_running_registry_rechecks_live_grant_and_session(admitted: Any, change: Any) -> None:
    watch, event, turn, connector = admitted
    registry = await ha.HarnessAdapter()._build_registry(options(admitted), native=True)
    if change == "agent":
        with get_db() as conn:
            conn.cursor().execute(
                "UPDATE watches SET agent_id='missing' WHERE id=%s", (watch["id"],)
            )
    elif change == "pause":
        store.set_status(watch["id"], "admin", "paused")
    elif change == "credential":
        database.update_api_user(connector["api_user_id"], is_active=False)
    elif change == "agent_scope":
        database.create_profile("other-agent", "Other", None, {})
        database.update_api_user(connector["api_user_id"], profile_ids=["other-agent"])
    elif change == "runner":
        with get_db() as conn:
            conn.cursor().execute(
                "UPDATE connectors SET runner_id='other' WHERE id=%s", (connector["id"],)
            )
    else:
        turns.settle_turn(turn["id"], "aborted", stop_reason="user_stopped")
    assert (await registry.dispatch("WebSearch", {"query": "must not run"})).is_error


async def test_authorization_outage_is_deny_at_actual_cli_hook(
    admitted: Any, monkeypatch: MonkeyPatch
) -> None:
    from fluid.engine.delegated import DelegatedLoop
    from fluid.store.base import MemoryStore
    opt = options(admitted)
    registry = await ha.HarnessAdapter()._build_registry(opt, native=True)
    policy = ha._delegated_tool_policy(opt, None, ha._delegated_disallowed(opt))
    loop = DelegatedLoop(
        store=MemoryStore(), model="fake", permission_mode="bypassPermissions", tool_policy=policy
    )

    def unavailable(*args: Any) -> None:
        raise RuntimeError("authorization database unavailable")

    monkeypatch.setattr(service, "assert_proactive_allowed", unavailable)
    hook = await loop._cli_pre_tool_use({"tool_name": "FutureCLIWrite", "tool_input": {}}, None)
    assert hook["hookSpecificOutput"]["permissionDecision"] == "deny"
    outcome = await registry.dispatch("WebSearch", {"query": "must not run"})
    assert outcome.is_error and "authorization" in outcome.text


async def test_native_connect_builds_no_kernel_or_learning_surface(
    admitted: Any, monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    from tests.test_harness_adapter import FakeProvider
    from fluid.store.base import MemoryStore

    monkeypatch.setattr(ha.HarnessAdapter, "_build_store", staticmethod(lambda _: MemoryStore()))
    monkeypatch.setattr(
        ha, "_build_provider_client", AsyncMock(return_value=(FakeProvider([]), None))
    )
    monkeypatch.setattr(
        ha, "CodeModeSession", lambda *a, **kw: pytest.fail("watch Python kernel constructed")
    )
    adapter = ha.HarnessAdapter()
    opt = options(admitted)
    opt.cwd = str(tmp_path)
    opt.extra["fluid_provider"] = "openai_responses"
    await adapter.connect(opt)
    assert adapter._engine._codemode is None
    store.set_status(admitted[0]["id"], "admin", "paused")
    with pytest.raises(ValueError, match="paused"):
        await adapter.send_query("Continue")
    await adapter.disconnect()


async def test_real_option_builder_preserves_owner_agent_and_narrows_scope(admitted: Any) -> None:
    from app.core.query_options import build_options_from_profile
    watch, event, turn, connector = admitted
    profile = database.get_profile(watch["agent_id"])
    profile["config"]["system_prompt"] = "Original owner prompt"
    profile["config"]["allowed_tools"] = [*modes.PROACTIVE_TOOL_SURFACE, "Read", "Task"]
    opt, _ = build_options_from_profile(
        profile, session_id=event["session_id"], overrides=turn["overrides"]
    )
    assert "Original owner prompt" in str(opt.system_prompt)
    adapter = ha.HarnessAdapter()
    adapter.connect = AsyncMock()
    await adapter.connect_with_sdk_options(opt)
    projected = adapter.connect.await_args.args[0]
    assert projected.extra["assistant_proactive"] == turn["payload"]["_assistant_proactive"]
    assert set(opt.aih_tool_allowlist) <= set(modes.PROACTIVE_TOOL_SURFACE)
    assert "Read" in projected.disallowed_tools and "Task" in projected.disallowed_tools
