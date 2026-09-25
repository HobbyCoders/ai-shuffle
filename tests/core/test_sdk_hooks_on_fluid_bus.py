"""The app's Agent SDK hooks run on fluid's HookBus, on both fluid legs.

``hook_service.attach_hooks_to_options`` puts DB hooks (blocking PreToolUse
guards among them), plugin hooks.json scripts and the Stop handler on
``options.hooks``. fluid legs have no Agent SDK to call them, so before this
none of them ran on a fluid session: the adapter now registers each callback
on the bus both legs emit into.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core import hook_service
from app.core.agent_types import HookMatcher, SyncHookJSONOutput
from app.core.provider.harness_adapter import HarnessAdapter
from app.core.provider.sdk_hook_bridge import sdk_hooks_bus


def _pre_tool_payload(name: str, command: str = "curl -X POST x") -> dict:
    return {
        "session_id": "ses_1", "transcript_path": "", "name": name,
        "input": {"command": command}, "tool_use_id": "toolu_1", "turn_id": "turn_1",
    }


@pytest.mark.asyncio
async def test_a_db_pretooluse_guard_denies_through_the_adapter_bus(monkeypatch, tmp_path):
    # The real DB PreToolUse handler, with its DB row and bash run stubbed:
    # the guard exits 2 on an outward write.
    async def _db_hooks(project_id, event):
        return [{"id": "h1", "hook_type": "bash", "enabled": True, "tool_matcher": "Bash"}]

    seen: list[dict] = []

    async def _run_bash(hook, script_input, working_dir):
        seen.append(script_input)
        return "ok", 2, "", "outward writes need approval"

    monkeypatch.setattr(hook_service, "get_database_hooks_for_event", _db_hooks)
    monkeypatch.setattr(hook_service, "_execute_db_bash_hook", _run_bash)
    monkeypatch.setattr(hook_service.database, "update_hook_triggered", lambda *a: None)
    handler = hook_service.create_db_pretooluse_hook_handler(str(tmp_path), "proj-1")

    adapter = HarnessAdapter()
    adapter._sdk_hook_bus = sdk_hooks_bus(
        {"PreToolUse": [HookMatcher(hooks=[handler])]}, str(tmp_path),
    )
    decision = await adapter.hooks.emit("PreToolUse", _pre_tool_payload("Bash"))

    assert decision.action == "deny"
    assert decision.note == "outward writes need approval"
    assert seen[0]["hook_event_name"] == "PreToolUse"
    assert seen[0]["tool_name"] == "Bash"
    assert seen[0]["tool_input"] == {"command": "curl -X POST x"}


@pytest.mark.asyncio
async def test_matchers_modify_and_the_stop_handler_keep_their_sdk_meaning(tmp_path):
    calls: list[tuple[str, str]] = []

    async def bash_only(hook_input, tool_use_id, context):
        calls.append(("bash_only", hook_input["tool_name"]))
        return SyncHookJSONOutput(hookSpecificOutput={
            "hookEventName": "PreToolUse", "updatedInput": {"command": "ls -la"},
        })

    async def stop(hook_input, tool_use_id, context):
        assert hook_input["hook_event_name"] == "Stop"
        return SyncHookJSONOutput(decision="block", reason="tests still fail", continue_=True)

    adapter = HarnessAdapter()
    adapter._sdk_hook_bus = sdk_hooks_bus({
        "PreToolUse": [HookMatcher(matcher="Bash", hooks=[bash_only])],
        "Stop": [HookMatcher(hooks=[stop])],
    }, str(tmp_path))

    read = await adapter.hooks.emit("PreToolUse", _pre_tool_payload("Read"))
    bash = await adapter.hooks.emit("PreToolUse", _pre_tool_payload("Bash", "ls"))
    turn_end = await adapter.hooks.emit("TurnEnd", {
        "session_id": "ses_1", "transcript_path": "", "turn_id": "turn_1",
        "text": "done", "stop_reason": "end_turn", "stop_hook_active": False,
    })

    assert calls == [("bash_only", "Bash")]
    assert read.action == "allow"
    assert bash.action == "modify" and bash.replacement_input == {"command": "ls -la"}
    # A Stop block sends the model back to work, as the SDK path did.
    assert turn_end.action == "deny" and turn_end.note == "tests still fail"
    assert not turn_end.stop


@pytest.mark.asyncio
async def test_connect_installs_the_options_hooks_on_the_bus(monkeypatch, tmp_path):
    async def deny(hook_input, tool_use_id, context):
        return SyncHookJSONOutput(hookSpecificOutput={
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": "guard",
        })

    captured = {}
    adapter = HarnessAdapter()
    real_connect = adapter.connect

    class _Stop(Exception):
        pass

    def _no_store(options):
        raise _Stop

    async def _capture(options):
        captured["hooks"] = options.hooks
        await real_connect(options)

    monkeypatch.setattr(adapter, "_build_store", _no_store)
    monkeypatch.setattr(adapter, "connect", _capture)
    sdk_options = SimpleNamespace(
        cwd=str(tmp_path), hooks={"PreToolUse": [HookMatcher(hooks=[deny])]},
    )
    with pytest.raises(_Stop):
        await adapter.connect_with_sdk_options(sdk_options)

    assert captured["hooks"] is sdk_options.hooks
    decision = await adapter.hooks.emit("PreToolUse", _pre_tool_payload("Bash"))
    assert decision.action == "deny" and decision.note == "guard"


@pytest.mark.asyncio
async def test_no_sdk_hooks_leave_the_bus_neutral():
    adapter = HarnessAdapter()
    decision = await adapter.hooks.emit("PreToolUse", _pre_tool_payload("Bash"))
    assert decision.action == "allow" and decision.permission_decision is None


@pytest.mark.asyncio
async def test_db_hooks_on_observed_events_are_dispatched_from_the_bus(monkeypatch, tmp_path):
    """DB hooks on events without a blocking handler (PostToolUse,
    UserPromptSubmit, ...) reach execute_hooks_for_event when the bus fires,
    and never change the turn's outcome."""
    monkeypatch.setattr(hook_service, "_project_has_db_pretooluse_hooks", lambda _p: False)
    monkeypatch.setattr(hook_service.database, "get_all_hooks_for_project", lambda _p: [
        {"event": "PostToolUse", "enabled": True},
        {"event": "UserPromptSubmit", "enabled": True},
        {"event": "SessionEnd", "enabled": False},
    ])
    fired: list[tuple] = []

    async def _dispatch(project_id, event, event_data, tool_name=None, session_id=None):
        fired.append((project_id, event, tool_name, event_data.get("hook_event_name")))

    monkeypatch.setattr(hook_service, "execute_hooks_for_event", _dispatch)
    config = hook_service.build_hooks_config(str(tmp_path), "proj-1")
    assert "SessionEnd" not in config  # disabled hooks register nothing

    adapter = HarnessAdapter()
    adapter._sdk_hook_bus = sdk_hooks_bus(config, str(tmp_path))
    post = await adapter.hooks.emit("PostToolUse", {
        **_pre_tool_payload("Bash"), "output": {"stdout": "ok"},
    })
    await adapter.hooks.emit("TurnStart", {"session_id": "ses_1", "turn_id": "t1", "prompt": "hi"})

    assert post.action != "deny"
    assert fired == [
        ("proj-1", "PostToolUse", "Bash", "PostToolUse"),
        ("proj-1", "UserPromptSubmit", None, "UserPromptSubmit"),
    ]


def test_project_less_sessions_register_app_level_observed_hooks(monkeypatch, tmp_path):
    monkeypatch.setattr(hook_service.database, "get_app_hooks", lambda: [
        {"event": "SessionStart", "enabled": True},
    ])
    config = hook_service.build_hooks_config(str(tmp_path), None)
    assert "SessionStart" in config


@pytest.mark.asyncio
async def test_a_message_hook_posts_into_the_app_session(monkeypatch, tmp_path):
    """attach_hooks_to_options hands the app session id (options.aih_session_id)
    to the observed-event handler, so a message hook posts its rendered
    message into that session instead of failing with "No active session"."""
    import asyncio

    from app.core import hook_executor
    from app.core.sync_engine import sync_engine

    hook = {
        "id": "h-msg", "name": "After tool", "hook_type": "message", "enabled": True,
        "event": "PostToolUse", "tool_matcher": None,
        "message_template": "{tool_name} ran in {session_id}",
    }
    monkeypatch.setattr(hook_service, "_project_has_db_pretooluse_hooks", lambda _p: False)
    monkeypatch.setattr(hook_service.database, "get_all_hooks_for_project", lambda _p: [hook])
    monkeypatch.setattr(hook_executor.database, "get_hooks_for_project_and_event",
                        lambda _p, _e: [hook])
    added: list[dict] = []
    triggered: list[tuple] = []
    broadcast: list[dict] = []
    monkeypatch.setattr(hook_executor.database, "add_session_message",
                        lambda **kw: added.append(kw))
    monkeypatch.setattr(hook_executor.database, "update_hook_triggered",
                        lambda hook_id, ok: triggered.append((hook_id, ok)))

    async def _broadcast(**kw):
        broadcast.append(kw)

    monkeypatch.setattr(sync_engine, "broadcast_message_added", _broadcast)

    options = SimpleNamespace(cwd=str(tmp_path), plugins=[], env={}, hooks=None,
                              aih_session_id="app-session-1")
    hook_service.attach_hooks_to_options(options, project_id="proj-1")

    adapter = HarnessAdapter()
    adapter._sdk_hook_bus = sdk_hooks_bus(options.hooks, str(tmp_path))
    decision = await adapter.hooks.emit("PostToolUse", {
        **_pre_tool_payload("Bash"), "output": {"stdout": "ok"},
    })
    await asyncio.gather(*list(hook_executor._active_hook_tasks))

    assert decision.action != "deny"
    assert [(a["session_id"], a["role"], a["content"]) for a in added] == [
        ("app-session-1", "user", "Bash ran in app-session-1"),
    ]
    assert broadcast and broadcast[0]["session_id"] == "app-session-1"
    assert triggered == [("h-msg", True)]


@pytest.mark.asyncio
async def test_a_failing_observed_hook_never_blocks_the_turn(monkeypatch, tmp_path):
    monkeypatch.setattr(hook_service, "_project_has_db_pretooluse_hooks", lambda _p: False)
    monkeypatch.setattr(hook_service.database, "get_all_hooks_for_project", lambda _p: [
        {"event": "PostToolUse", "enabled": True},
    ])

    async def _explode(*_a, **_kw):
        raise RuntimeError("hook dispatch blew up")

    monkeypatch.setattr(hook_service, "execute_hooks_for_event", _explode)
    handler = hook_service.build_hooks_config(str(tmp_path), "proj-1")["PostToolUse"][0].hooks[0]

    # The callback itself swallows the failure...
    assert await handler({"hook_event_name": "PostToolUse", "tool_name": "Bash"}, None, None) == {}

    # ...and the bus emit still lets the turn continue.
    adapter = HarnessAdapter()
    adapter._sdk_hook_bus = sdk_hooks_bus({"PostToolUse": [HookMatcher(hooks=[handler])]}, str(tmp_path))
    decision = await adapter.hooks.emit("PostToolUse", {
        **_pre_tool_payload("Bash"), "output": {"stdout": "ok"},
    })
    assert decision.action != "deny"
