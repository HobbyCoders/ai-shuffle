"""
Tests for the unified hook service.
"""

import pytest
from unittest.mock import patch, AsyncMock

from app.core.hook_service import (
    load_plugin_hooks,
    build_hooks_config,
    create_stop_hook_handler,
    create_plugin_hook_handler,
    create_db_pretooluse_hook_handler,
    create_agent_arg_strip_hook_handler,
    execute_plugin_hook,
    _fix_python_command_for_windows,
)


class TestLoadPluginHooks:
    """Tests for loading hooks from plugin hooks.json files."""

    def test_load_hooks_from_plugin(self, tmp_path):
        """Should load hooks from a plugin's hooks.json file."""
        # Create plugin structure
        plugin_dir = tmp_path / "test-plugin"
        hooks_dir = plugin_dir / "hooks"
        hooks_dir.mkdir(parents=True)

        hooks_json = hooks_dir / "hooks.json"
        hooks_json.write_text("""{
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Edit|Write",
                        "hooks": [
                            {"type": "command", "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/check.py"}
                        ]
                    }
                ]
            }
        }""")

        result = load_plugin_hooks([str(plugin_dir)])

        assert "PreToolUse" in result
        assert len(result["PreToolUse"]) == 1
        assert "check.py" in result["PreToolUse"][0]["command"]
        assert str(plugin_dir) in result["PreToolUse"][0]["command"]

    def test_agent_plugins_root_variable_is_substituted(self, tmp_path):
        """Agent Plugins hooks spell the plugin root ${PLUGIN_ROOT} (#1610)."""
        plugin_dir = tmp_path / "agent-plugin"
        (plugin_dir / "hooks").mkdir(parents=True)
        (plugin_dir / "hooks" / "hooks.json").write_text(
            '{"hooks": {"Stop": [{"hooks": [{"type": "command", '
            '"command": "python ${PLUGIN_ROOT}/stop.py"}]}]}}'
        )

        result = load_plugin_hooks([str(plugin_dir)])

        assert result["Stop"][0]["command"] == f"python {plugin_dir}/stop.py"

    def test_load_hooks_no_file(self, tmp_path):
        """Should return empty dict if no hooks.json exists."""
        plugin_dir = tmp_path / "empty-plugin"
        plugin_dir.mkdir()

        result = load_plugin_hooks([str(plugin_dir)])
        assert result == {}

    def test_load_hooks_multiple_plugins(self, tmp_path):
        """Should merge hooks from multiple plugins."""
        # Plugin 1
        plugin1 = tmp_path / "plugin1"
        hooks1_dir = plugin1 / "hooks"
        hooks1_dir.mkdir(parents=True)
        (hooks1_dir / "hooks.json").write_text("""{
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Edit", "hooks": [{"type": "command", "command": "echo plugin1"}]}
                ]
            }
        }""")

        # Plugin 2
        plugin2 = tmp_path / "plugin2"
        hooks2_dir = plugin2 / "hooks"
        hooks2_dir.mkdir(parents=True)
        (hooks2_dir / "hooks.json").write_text("""{
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Write", "hooks": [{"type": "command", "command": "echo plugin2"}]}
                ]
            }
        }""")

        result = load_plugin_hooks([str(plugin1), str(plugin2)])

        assert "PreToolUse" in result
        assert len(result["PreToolUse"]) == 2


class TestPythonCommandFix:
    """Tests for Windows Python command compatibility."""

    def test_fix_python3_on_windows(self):
        """Should replace python3 with sys.executable on Windows."""
        with patch("sys.platform", "win32"):
            import sys
            result = _fix_python_command_for_windows("python3 script.py --arg")
            assert sys.executable in result or "python" in result.lower()

    def test_no_change_on_linux(self):
        """Should not modify command on Linux."""
        with patch("sys.platform", "linux"):
            result = _fix_python_command_for_windows("python3 script.py")
            assert result == "python3 script.py"


class TestBuildHooksConfig:
    """Tests for building SDK hooks configuration."""

    def test_always_registers_stop_hook(self, tmp_path):
        """Should always register Stop hook (to support database hooks)."""
        result = build_hooks_config(str(tmp_path), "test-project")
        assert result is not None
        assert "Stop" in result
        assert len(result["Stop"]) == 1

    def test_builds_config_with_plugin_hooks(self, tmp_path):
        """Should include plugin hooks in configuration."""
        # Create a plugin with PreToolUse hook
        plugin_dir = tmp_path / "test-plugin"
        hooks_dir = plugin_dir / "hooks"
        hooks_dir.mkdir(parents=True)

        (hooks_dir / "hooks.json").write_text("""{
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Edit", "hooks": [{"type": "command", "command": "echo test"}]}
                ]
            }
        }""")

        result = build_hooks_config(str(tmp_path), "test-project", plugin_paths=[str(plugin_dir)])

        assert "PreToolUse" in result
        assert "Stop" in result


class TestStopHookHandler:
    """Tests for the unified Stop hook handler."""

    @pytest.mark.asyncio
    async def test_no_blocking_without_plugins(self, tmp_path):
        """Should allow stop when no plugin hooks or database hooks."""
        with patch('app.core.hook_service.get_database_hooks_for_event', new_callable=AsyncMock) as mock:
            mock.return_value = []
            handler = create_stop_hook_handler(str(tmp_path), plugin_stop_hooks=[])
            result = await handler(
                {"hook_event_name": "Stop", "stop_hook_active": False},
                None,
                {"signal": None}
            )

        # Should not block
        assert result.get("decision") != "block"

    @pytest.mark.asyncio
    async def test_executes_plugin_stop_hooks(self, tmp_path):
        """Should execute plugin Stop hooks and respect their decisions."""
        # Create a mock plugin hook that returns a block decision
        async def mock_execute(*args, **kwargs):
            return {"decision": "block", "reason": "Test prompt", "systemMessage": "Test message"}

        with patch('app.core.hook_service.execute_plugin_hook', mock_execute):
            handler = create_stop_hook_handler(
                str(tmp_path),
                plugin_stop_hooks=[{"command": "echo test", "plugin_path": "/test"}]
            )
            result = await handler(
                {"hook_event_name": "Stop", "stop_hook_active": False},
                None,
                {"signal": None}
            )

        assert result.get("decision") == "block"
        assert result.get("reason") == "Test prompt"
        assert result.get("continue_") == True  # Default for block decisions

    @pytest.mark.asyncio
    async def test_block_decision_defaults_continue_true(self, tmp_path):
        """Block decisions should default continue_ to True for loop support."""
        async def mock_execute(*args, **kwargs):
            # Return block without explicit continue field (like ralph-loop does)
            return {"decision": "block", "reason": "prompt"}

        with patch('app.core.hook_service.execute_plugin_hook', mock_execute):
            handler = create_stop_hook_handler(
                str(tmp_path),
                plugin_stop_hooks=[{"command": "test", "plugin_path": "/test"}]
            )
            result = await handler(
                {"hook_event_name": "Stop", "stop_hook_active": False},
                None,
                {"signal": None}
            )

        assert result.get("decision") == "block"
        assert result.get("continue_") == True

    @pytest.mark.asyncio
    async def test_script_input_session_id_from_hook_input_not_context(self, tmp_path):
        """
        Regression guard: script_input passed to plugin Stop scripts must carry
        the real session_id from hook_input even when context (HookContext,
        which in production only has `signal`) doesn't have one.
        """
        captured = {}

        async def mock_execute(hook_def, script_input, working_dir, stderr_callback=None):
            captured.update(script_input)
            return None

        with patch('app.core.hook_service.execute_plugin_hook', mock_execute):
            handler = create_stop_hook_handler(
                str(tmp_path),
                plugin_stop_hooks=[{"command": "echo test", "plugin_path": "/test"}]
            )
            await handler(
                {"hook_event_name": "Stop", "stop_hook_active": False, "session_id": "sess-abc"},
                None,
                {"signal": None},  # production-shaped HookContext: no session_id key
            )

        assert captured.get("session_id") == "sess-abc"


    @pytest.mark.asyncio
    async def test_database_bash_stop_hook_gets_timestamp_and_data(self, tmp_path):
        """A saved Stop bash hook reads the stop payload like the other events."""
        import json
        out = tmp_path / "env.txt"
        hook = {
            "hook_type": "bash", "timeout": 10000, "name": "stop-env",
            "command": f'printf "%s\\n%s\\n%s\\n%s" "$HOOK_EVENT" "$HOOK_TIMESTAMP" '
                       f'"$HOOK_SESSION_ID" "$HOOK_DATA" > "{out}"; echo \'{{"decision": "block", "reason": "again"}}\'',
        }
        with patch('app.core.hook_service.get_database_hooks_for_event', new_callable=AsyncMock) as mock:
            mock.return_value = [hook]
            handler = create_stop_hook_handler(str(tmp_path), project_id="proj-1", plugin_stop_hooks=[])
            result = await handler(
                {"hook_event_name": "Stop", "event": "TurnEnd", "session_id": "sess-abc",
                 "stop_hook_active": False, "stop_reason": "end_turn", "text": "done"},
                None,
                {"signal": None},
            )

        event, timestamp, session_id, data = out.read_text().split("\n", 3)
        assert event == "Stop"
        assert timestamp.startswith("20") and "T" in timestamp
        assert session_id == "sess-abc"
        data = json.loads(data)
        assert data["session_id"] == "sess-abc"
        assert data["stop_reason"] == "end_turn"
        assert data["project_id"] == "proj-1"
        assert data["cwd"] == str(tmp_path)
        assert result.get("decision") == "block" and result.get("reason") == "again"


class TestExecutePluginHook:
    """Tests for plugin hook execution."""

    @pytest.mark.asyncio
    async def test_execute_returns_json(self, tmp_path):
        """Should parse JSON output from plugin scripts."""
        # Create a simple script that outputs JSON
        script = tmp_path / "test_hook.sh"
        script.write_text('#!/bin/bash\necho \'{"decision": "allow"}\'')
        script.chmod(0o755)

        result = await execute_plugin_hook(
            {"command": f"bash '{script}'", "timeout": 5, "plugin_path": str(tmp_path)},
            {"hook_event_name": "Test"},
            str(tmp_path)
        )

        assert result is not None
        assert result.get("decision") == "allow"

    @pytest.mark.asyncio
    async def test_execute_handles_non_json(self, tmp_path):
        """Should return None for non-JSON output."""
        script = tmp_path / "test_hook.sh"
        script.write_text('#!/bin/bash\necho "not json"')
        script.chmod(0o755)

        result = await execute_plugin_hook(
            {"command": f"bash '{script}'", "timeout": 5, "plugin_path": str(tmp_path)},
            {"hook_event_name": "Test"},
            str(tmp_path)
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_execute_handles_timeout(self, tmp_path):
        """Should handle command timeout gracefully."""
        result = await execute_plugin_hook(
            {"command": "sleep 10", "timeout": 0.1, "plugin_path": str(tmp_path)},
            {"hook_event_name": "Test"},
            str(tmp_path)
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_stderr_callback_invoked(self, tmp_path):
        """Should invoke stderr callback when script outputs to stderr."""
        script = tmp_path / "test_hook.sh"
        script.write_text('#!/bin/bash\necho "warning message" >&2')
        script.chmod(0o755)

        callback_called = []
        async def stderr_callback(plugin_name, stderr, severity):
            callback_called.append((plugin_name, stderr, severity))

        await execute_plugin_hook(
            {"command": f"bash '{script}'", "timeout": 5, "plugin_path": str(tmp_path)},
            {"hook_event_name": "Test"},
            str(tmp_path),
            stderr_callback=stderr_callback
        )

        assert len(callback_called) == 1
        assert "warning message" in callback_called[0][1]


# =============================================================================
# Test create_plugin_hook_handler — event-type routing (SDK 0.1.81+)
# =============================================================================

class TestCreatePluginHookHandler:
    """Tests for create_plugin_hook_handler output-type routing."""

    @pytest.mark.asyncio
    async def test_posttooluse_updated_output_forwarded(self, tmp_path):
        """
        When a PostToolUse plugin script returns {"updated_output": "REWRITTEN"},
        the handler must return a PostToolUseHookSpecificOutput with
        updatedToolOutput == "REWRITTEN".
        """

        async def mock_execute(*args, **kwargs):
            return {"updated_output": "REWRITTEN"}

        with patch("app.core.hook_service.execute_plugin_hook", mock_execute):
            handler = create_plugin_hook_handler(
                event_name="PostToolUse",
                hook_defs=[{"command": "echo test", "plugin_path": str(tmp_path), "timeout": 5}],
                working_dir=str(tmp_path),
            )
            result = await handler(
                {"hook_event_name": "PostToolUse", "tool_name": "Read"},
                None,
                {"session_id": "test-session"},
            )

        assert result is not None
        assert result["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
        assert result["hookSpecificOutput"]["updatedToolOutput"] == "REWRITTEN"

    @pytest.mark.asyncio
    async def test_posttooluse_no_updated_output_returns_default(self, tmp_path):
        """
        When a PostToolUse plugin script returns nothing meaningful, the handler
        must return the default SyncHookJSONOutput (no updatedToolOutput).
        """
        async def mock_execute(*args, **kwargs):
            return {}

        with patch("app.core.hook_service.execute_plugin_hook", mock_execute):
            handler = create_plugin_hook_handler(
                event_name="PostToolUse",
                hook_defs=[{"command": "echo test", "plugin_path": str(tmp_path), "timeout": 5}],
                working_dir=str(tmp_path),
            )
            result = await handler(
                {"hook_event_name": "PostToolUse", "tool_name": "Write"},
                None,
                {"session_id": "test-session"},
            )

        # Should fall through to the SyncHookJSONOutput default
        assert result is not None
        assert result.get("hookSpecificOutput") is None

    @pytest.mark.asyncio
    async def test_pretooluse_permission_decision_forwarded(self, tmp_path):
        """
        When a PreToolUse plugin script returns {"decision": "deny"},
        the handler must return a PreToolUseHookSpecificOutput with
        permissionDecision == "deny".
        """
        async def mock_execute(*args, **kwargs):
            return {"decision": "deny", "reason": "blocked by policy"}

        with patch("app.core.hook_service.execute_plugin_hook", mock_execute):
            handler = create_plugin_hook_handler(
                event_name="PreToolUse",
                hook_defs=[{"command": "echo test", "plugin_path": str(tmp_path), "timeout": 5}],
                working_dir=str(tmp_path),
            )
            result = await handler(
                {"hook_event_name": "PreToolUse", "tool_name": "Bash"},
                None,
                {"session_id": "test-session"},
            )

        assert result is not None
        # Lab-confirmed regression guard: the permission decision MUST be nested
        # under hookSpecificOutput (SyncHookJSONOutput contract). A bare top-level
        # permissionDecision is silently ignored by the CLI schema (fail-open).
        assert result.get("permissionDecision") is None
        assert result["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert result["hookSpecificOutput"]["permissionDecisionReason"] == "blocked by policy"

    @pytest.mark.asyncio
    async def test_script_input_session_id_from_hook_input_not_context(self, tmp_path):
        """
        Regression guard: script_input passed to plugin PreToolUse scripts must
        carry the real session_id from hook_input even when context (HookContext,
        which in production only has `signal`) doesn't have one.
        """
        captured = {}

        async def mock_execute(hook_def, script_input, working_dir, stderr_callback=None):
            captured.update(script_input)
            return None

        with patch("app.core.hook_service.execute_plugin_hook", mock_execute):
            handler = create_plugin_hook_handler(
                event_name="PreToolUse",
                hook_defs=[{"command": "echo test", "plugin_path": str(tmp_path), "timeout": 5}],
                working_dir=str(tmp_path),
            )
            await handler(
                {"hook_event_name": "PreToolUse", "tool_name": "Bash", "session_id": "sess-abc"},
                None,
                {"signal": None},  # production-shaped HookContext: no session_id key
            )

        assert captured.get("session_id") == "sess-abc"

    @pytest.mark.asyncio
    async def test_other_event_uses_sync_hook_json_output(self, tmp_path):
        """
        Non-PreToolUse/PostToolUse events must still return SyncHookJSONOutput.
        """
        async def mock_execute(*args, **kwargs):
            return {"decision": "block", "reason": "stop loop", "systemMessage": "try harder"}

        with patch("app.core.hook_service.execute_plugin_hook", mock_execute):
            handler = create_plugin_hook_handler(
                event_name="Stop",
                hook_defs=[{"command": "echo test", "plugin_path": str(tmp_path), "timeout": 5}],
                working_dir=str(tmp_path),
            )
            result = await handler(
                {"hook_event_name": "Stop"},
                None,
                {"session_id": "test-session"},
            )

        # SyncHookJSONOutput uses snake_case / dataclass attributes
        assert result is not None
        assert result.get("decision") == "block"
        assert result.get("reason") == "stop loop"


# =============================================================================
# Test DB-backed PreToolUse hooks (block-capable)
# =============================================================================

def _db_bash_hook(command, tool_matcher="Bash", enabled=True, timeout=5000, hook_type="bash", name="test-hook"):
    """Helper to build a DB hook row dict as returned by the database layer."""
    return {
        "id": "hook-1",
        "name": name,
        "event": "PreToolUse",
        "hook_type": hook_type,
        "command": command,
        "timeout": timeout,
        "enabled": enabled,
        "tool_matcher": tool_matcher,
        "project_id": "test-project",
    }


class TestDbPreToolUseHooks:
    """Tests for DB-backed PreToolUse hooks executing and blocking tools."""

    @pytest.mark.asyncio
    async def test_registered_and_fires_for_matching_tool(self, tmp_path):
        """(a) A DB PreToolUse hook fires for a tool matching its tool_matcher."""
        hook = _db_bash_hook('echo \'{"decision": "block", "reason": "denied by policy"}\'')
        with patch("app.core.hook_service.get_database_hooks_for_event", new_callable=AsyncMock) as mock:
            mock.return_value = [hook]
            handler = create_db_pretooluse_hook_handler(str(tmp_path), "test-project")
            result = await handler(
                {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}},
                None,
                {"session_id": "s1"},
            )
        mock.assert_awaited()
        assert result["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    @pytest.mark.asyncio
    async def test_block_decision_propagates_as_deny_with_reason(self, tmp_path):
        """(b) decision:block maps to permissionDecision=deny with the reason passed through."""
        hook = _db_bash_hook('echo \'{"decision": "block", "reason": "no shell allowed"}\'')
        with patch("app.core.hook_service.get_database_hooks_for_event", new_callable=AsyncMock) as mock:
            mock.return_value = [hook]
            handler = create_db_pretooluse_hook_handler(str(tmp_path), "test-project")
            result = await handler(
                {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {}},
                None,
                {"session_id": "s1"},
            )
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert result["hookSpecificOutput"]["permissionDecisionReason"] == "no shell allowed"

    @pytest.mark.asyncio
    async def test_permission_decision_shape(self, tmp_path):
        """hookSpecificOutput.permissionDecision shape is honored."""
        payload = '{"hookSpecificOutput": {"permissionDecision": "deny", "permissionDecisionReason": "nope"}}'
        hook = _db_bash_hook(f"echo '{payload}'")
        with patch("app.core.hook_service.get_database_hooks_for_event", new_callable=AsyncMock) as mock:
            mock.return_value = [hook]
            handler = create_db_pretooluse_hook_handler(str(tmp_path), "test-project")
            result = await handler(
                {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {}},
                None,
                {"session_id": "s1"},
            )
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert result["hookSpecificOutput"]["permissionDecisionReason"] == "nope"

    @pytest.mark.asyncio
    async def test_approve_decision_allows(self, tmp_path):
        """decision:approve maps to permissionDecision=allow."""
        hook = _db_bash_hook('echo \'{"decision": "approve"}\'')
        with patch("app.core.hook_service.get_database_hooks_for_event", new_callable=AsyncMock) as mock:
            mock.return_value = [hook]
            handler = create_db_pretooluse_hook_handler(str(tmp_path), "test-project")
            result = await handler(
                {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {}},
                None,
                {"session_id": "s1"},
            )
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"

    @pytest.mark.asyncio
    async def test_non_matching_tool_matcher_not_invoked(self, tmp_path):
        """(c) A hook whose tool_matcher doesn't match the tool is never executed."""
        sentinel = tmp_path / "ran.txt"
        hook = _db_bash_hook(f"touch {sentinel}", tool_matcher="Edit")
        with patch("app.core.hook_service.get_database_hooks_for_event", new_callable=AsyncMock) as mock:
            mock.return_value = [hook]
            handler = create_db_pretooluse_hook_handler(str(tmp_path), "test-project")
            result = await handler(
                {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {}},
                None,
                {"session_id": "s1"},
            )
        assert not sentinel.exists(), "hook command ran despite non-matching tool_matcher"
        # No opinion -> default output, no permissionDecision
        assert result.get("hookSpecificOutput") is None

    @pytest.mark.asyncio
    async def test_disabled_hook_not_invoked(self, tmp_path):
        """(d) A disabled hook is skipped even if returned by the query."""
        sentinel = tmp_path / "ran.txt"
        hook = _db_bash_hook(f"touch {sentinel}", enabled=False)
        with patch("app.core.hook_service.get_database_hooks_for_event", new_callable=AsyncMock) as mock:
            mock.return_value = [hook]
            handler = create_db_pretooluse_hook_handler(str(tmp_path), "test-project")
            result = await handler(
                {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {}},
                None,
                {"session_id": "s1"},
            )
        assert not sentinel.exists()
        assert result.get("hookSpecificOutput") is None

    @pytest.mark.asyncio
    async def test_timeout_is_no_opinion(self, tmp_path):
        """(e) A hook that times out is treated as no-opinion (does not block)."""
        hook = _db_bash_hook("sleep 10", timeout=100)  # 100ms timeout
        with patch("app.core.hook_service.get_database_hooks_for_event", new_callable=AsyncMock) as mock:
            mock.return_value = [hook]
            handler = create_db_pretooluse_hook_handler(str(tmp_path), "test-project")
            result = await handler(
                {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {}},
                None,
                {"session_id": "s1"},
            )
        assert result.get("hookSpecificOutput") is None

    @pytest.mark.asyncio
    async def test_crash_is_no_opinion(self, tmp_path):
        """(e) A hook that errors with no parseable output and exit 1 is no-opinion."""
        hook = _db_bash_hook("exit 1")
        with patch("app.core.hook_service.get_database_hooks_for_event", new_callable=AsyncMock) as mock:
            mock.return_value = [hook]
            handler = create_db_pretooluse_hook_handler(str(tmp_path), "test-project")
            result = await handler(
                {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {}},
                None,
                {"session_id": "s1"},
            )
        assert result.get("hookSpecificOutput") is None

    @pytest.mark.asyncio
    async def test_exit_code_2_is_deny(self, tmp_path):
        """(f) Exit code 2 with no parseable stdout denies, using stderr as reason."""
        hook = _db_bash_hook('echo "blocked: dangerous command" >&2; exit 2')
        with patch("app.core.hook_service.get_database_hooks_for_event", new_callable=AsyncMock) as mock:
            mock.return_value = [hook]
            handler = create_db_pretooluse_hook_handler(str(tmp_path), "test-project")
            result = await handler(
                {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {}},
                None,
                {"session_id": "s1"},
            )
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "blocked: dangerous command" in (result["hookSpecificOutput"].get("permissionDecisionReason") or "")

    @pytest.mark.asyncio
    async def test_exit_0_no_output_is_no_opinion(self, tmp_path):
        """Exit 0 with no output proceeds to normal permission flow (no opinion)."""
        hook = _db_bash_hook("true")
        with patch("app.core.hook_service.get_database_hooks_for_event", new_callable=AsyncMock) as mock:
            mock.return_value = [hook]
            handler = create_db_pretooluse_hook_handler(str(tmp_path), "test-project")
            result = await handler(
                {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {}},
                None,
                {"session_id": "s1"},
            )
        assert result.get("hookSpecificOutput") is None

    @pytest.mark.asyncio
    async def test_stdin_receives_hook_contract_json(self, tmp_path):
        """The hook command receives tool_name/tool_input/session_id/cwd as JSON on stdin."""
        # Reflect stdin back: deny if the payload contains the expected tool_name.
        cmd = (
            "python3 -c \"import sys,json; d=json.load(sys.stdin); "
            "print(json.dumps({'decision':'block','reason':d['tool_name']+'|'+d['tool_input'].get('command','')}))\""
        )
        hook = _db_bash_hook(cmd)
        with patch("app.core.hook_service.get_database_hooks_for_event", new_callable=AsyncMock) as mock:
            mock.return_value = [hook]
            handler = create_db_pretooluse_hook_handler(str(tmp_path), "test-project")
            result = await handler(
                {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "rm -rf /"}},
                None,
                {"session_id": "sess-42"},
            )
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert result["hookSpecificOutput"]["permissionDecisionReason"] == "Bash|rm -rf /"

    @pytest.mark.asyncio
    async def test_stdin_session_id_sourced_from_hook_input_not_context(self, tmp_path):
        """
        The SDK's HookContext TypedDict only carries `signal` — it never has
        session_id. The real session id arrives via the first handler argument
        (hook_input["session_id"]). If the handler builds script_input from
        context.get("session_id") only, DB bash hooks (e.g. the Unraid host-op
        guard) receive session_id: null on stdin and fail closed.

        Regression guard: pass a hook_input WITH session_id and a context
        WITHOUT it (mirroring production HookContext shape), and assert the
        hook command observes the real session id on stdin.
        """
        cmd = (
            "python3 -c \"import sys,json; d=json.load(sys.stdin); "
            "print(json.dumps({'decision':'block','reason':'sid='+str(d.get('session_id'))}))\""
        )
        hook = _db_bash_hook(cmd)
        with patch("app.core.hook_service.get_database_hooks_for_event", new_callable=AsyncMock) as mock:
            mock.return_value = [hook]
            handler = create_db_pretooluse_hook_handler(str(tmp_path), "test-project")
            result = await handler(
                {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {}, "session_id": "sess-abc"},
                None,
                {"signal": None},  # production-shaped HookContext: no session_id key
            )
        assert result["hookSpecificOutput"]["permissionDecisionReason"] == "sid=sess-abc"

    @pytest.mark.asyncio
    async def test_webhook_type_skipped_not_blocking(self, tmp_path):
        """(5) A non-bash (webhook) PreToolUse hook is skipped for blocking, no-opinion."""
        hook = _db_bash_hook("", hook_type="webhook")
        hook["url"] = "http://example.invalid/hook"
        with patch("app.core.hook_service.get_database_hooks_for_event", new_callable=AsyncMock) as mock:
            mock.return_value = [hook]
            handler = create_db_pretooluse_hook_handler(str(tmp_path), "test-project")
            result = await handler(
                {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {}},
                None,
                {"session_id": "s1"},
            )
        assert result.get("hookSpecificOutput") is None

    def test_build_config_registers_db_pretooluse_when_present(self, tmp_path):
        """build_hooks_config registers a PreToolUse handler when DB hooks exist."""
        with patch("app.core.hook_service._project_has_db_pretooluse_hooks", return_value=True):
            result = build_hooks_config(str(tmp_path), "test-project")
        assert "PreToolUse" in result
        assert len(result["PreToolUse"]) >= 1
        # Stop handler still registered (unchanged behavior)
        assert "Stop" in result

    def test_build_config_no_db_pretooluse_when_absent(self, tmp_path):
        """build_hooks_config does not register a DB PreToolUse handler when none exist."""
        with patch("app.core.hook_service._project_has_db_pretooluse_hooks", return_value=False):
            result = build_hooks_config(str(tmp_path), "test-project")
        assert "PreToolUse" not in result
        assert "Stop" in result


# =============================================================================
# S8-R1: Agent-arg-strip PreToolUse hook (translated mains)
# =============================================================================

class TestAgentArgStripHandler:
    """Unit tests for create_agent_arg_strip_hook_handler."""

    @pytest.mark.asyncio
    async def test_strips_both_model_and_isolation(self):
        handler = create_agent_arg_strip_hook_handler()
        result = await handler(
            {
                "tool_name": "Agent",
                "tool_input": {
                    "prompt": "do it", "model": "opus",
                    "isolation": "worktree", "run_in_background": False,
                },
            },
            None,
            {"signal": None},
        )
        updated = result["hookSpecificOutput"]["updatedInput"]
        assert "model" not in updated
        assert "isolation" not in updated
        assert updated == {"prompt": "do it", "run_in_background": False}
        assert result["hookSpecificOutput"]["hookEventName"] == "PreToolUse"

    @pytest.mark.asyncio
    async def test_strips_when_only_model_present(self):
        handler = create_agent_arg_strip_hook_handler()
        result = await handler(
            {"tool_name": "Agent", "tool_input": {"prompt": "x", "model": "sonnet"}},
            None,
            {"signal": None},
        )
        assert result["hookSpecificOutput"]["updatedInput"] == {"prompt": "x"}

    @pytest.mark.asyncio
    async def test_strips_when_only_isolation_present(self):
        handler = create_agent_arg_strip_hook_handler()
        result = await handler(
            {"tool_name": "Agent", "tool_input": {"prompt": "x", "isolation": "remote"}},
            None,
            {"signal": None},
        )
        assert result["hookSpecificOutput"]["updatedInput"] == {"prompt": "x"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool_name", ["Task", "mcp__builtins__Task"])
    async def test_never_touches_the_app_task_model(self, tool_name):
        """The app's Task ``model`` is a user-requested override (#1830)."""
        handler = create_agent_arg_strip_hook_handler()
        result = await handler(
            {"tool_name": tool_name,
             "tool_input": {"agent": "a", "prompt": "x", "model": "prov:p:gpt-6.1-sol"}},
            None,
            {"signal": None},
        )
        assert result.get("hookSpecificOutput") is None

    @pytest.mark.asyncio
    async def test_preserves_all_other_keys(self):
        handler = create_agent_arg_strip_hook_handler()
        original = {
            "prompt": "p", "description": "d", "subagent_type": "claude",
            "run_in_background": True, "model": "opus", "isolation": "worktree",
        }
        result = await handler(
            {"tool_name": "Agent", "tool_input": original}, None, {"signal": None},
        )
        updated = result["hookSpecificOutput"]["updatedInput"]
        assert updated == {
            "prompt": "p", "description": "d", "subagent_type": "claude",
            "run_in_background": True,
        }

    @pytest.mark.asyncio
    async def test_no_opinion_when_neither_present(self):
        handler = create_agent_arg_strip_hook_handler()
        result = await handler(
            {"tool_name": "Agent", "tool_input": {"prompt": "x", "run_in_background": False}},
            None,
            {"signal": None},
        )
        assert result.get("hookSpecificOutput") is None

    @pytest.mark.asyncio
    async def test_no_opinion_for_non_dict_tool_input(self):
        handler = create_agent_arg_strip_hook_handler()
        result = await handler(
            {"tool_name": "Agent", "tool_input": None}, None, {"signal": None},
        )
        assert result.get("hookSpecificOutput") is None

    @pytest.mark.asyncio
    async def test_no_opinion_for_non_agent_tool(self):
        handler = create_agent_arg_strip_hook_handler()
        result = await handler(
            {"tool_name": "Bash", "tool_input": {"model": "opus", "isolation": "worktree"}},
            None,
            {"signal": None},
        )
        assert result.get("hookSpecificOutput") is None

    @pytest.mark.asyncio
    async def test_output_has_no_permission_decision(self):
        handler = create_agent_arg_strip_hook_handler()
        result = await handler(
            {"tool_name": "Agent", "tool_input": {"prompt": "x", "model": "opus"}},
            None,
            {"signal": None},
        )
        assert result.get("permissionDecision") is None
        assert "permissionDecision" not in result["hookSpecificOutput"]


class TestBuildHooksConfigTranslatedMain:
    """build_hooks_config translated_main registration."""

    def test_registers_agent_strip_matcher_when_translated(self, tmp_path):
        result = build_hooks_config(str(tmp_path), "test-project", translated_main=True)
        assert "PreToolUse" in result
        matchers = [m for m in result["PreToolUse"] if m.matcher == "Agent"]
        assert len(matchers) == 1

    def test_default_false_does_not_register_agent_strip(self, tmp_path):
        with patch("app.core.hook_service._project_has_db_pretooluse_hooks", return_value=False):
            result = build_hooks_config(str(tmp_path), "test-project")
        # No PreToolUse matcher at all (no plugin/DB hooks, no translated strip).
        assert "PreToolUse" not in result

    def test_translated_strip_coexists_with_db_pretooluse(self, tmp_path):
        with patch("app.core.hook_service._project_has_db_pretooluse_hooks", return_value=True):
            result = build_hooks_config(str(tmp_path), "test-project", translated_main=True)
        agent_matchers = [m for m in result["PreToolUse"] if m.matcher == "Agent"]
        assert len(agent_matchers) == 1
        # DB handler (no matcher) is still present alongside.
        assert len(result["PreToolUse"]) >= 2


# =============================================================================
# Stop webhooks, tool_matcher and per-hook timeouts
# =============================================================================

def _webhook_client(monkeypatch, status_code, text):
    from unittest.mock import MagicMock
    client = MagicMock()
    client.post = AsyncMock(return_value=MagicMock(status_code=status_code, text=text))
    monkeypatch.setattr("app.core.http_client.get_http_client", lambda: client)
    return client


class TestStopWebhookDecision:
    _hook = {"id": "w", "hook_type": "webhook", "url": "https://hooks.example/stop",
             "timeout": 5000, "name": "stop-webhook"}
    _input = {"hook_event_name": "Stop", "session_id": "s", "stop_reason": "end_turn"}

    async def _run(self, monkeypatch, tmp_path, status_code, body):
        client = _webhook_client(monkeypatch, status_code, body)
        monkeypatch.setattr("app.core.hook_executor.database.update_hook_triggered", lambda *a: None)
        with patch('app.core.hook_service.get_database_hooks_for_event', new_callable=AsyncMock) as mock:
            mock.return_value = [dict(self._hook)]
            handler = create_stop_hook_handler(str(tmp_path), project_id="p", plugin_stop_hooks=[])
            result = await handler(dict(self._input), None, {"signal": None})
        assert client.post.await_count == 1
        return result

    @pytest.mark.asyncio
    async def test_a_json_block_body_keeps_the_agent_working(self, monkeypatch, tmp_path):
        result = await self._run(
            monkeypatch, tmp_path, 200, '{"decision": "block", "reason": "CI is red"}',
        )
        assert result.get("decision") == "block"
        assert result.get("reason") == "CI is red"
        assert result.get("continue_") is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status_code,body", [
        (200, "ok"),
        (200, '{"decision": "approve"}'),
        (200, '["block"]'),
        (500, '{"decision": "block", "reason": "error pages never block"}'),
    ])
    async def test_other_responses_only_notify(self, monkeypatch, tmp_path, status_code, body):
        result = await self._run(monkeypatch, tmp_path, status_code, body)
        assert result.get("decision") is None


class TestToolMatcher:
    @pytest.mark.asyncio
    async def test_an_invalid_regex_does_not_skip_the_other_hooks(self, monkeypatch):
        from app.core import hook_executor

        hooks = [
            {"id": "bad", "hook_type": "bash", "tool_matcher": "Bash("},
            {"id": "good", "hook_type": "bash", "tool_matcher": "Bash|Edit"},
            {"id": "literal", "hook_type": "bash", "tool_matcher": "Bash("},
        ]
        monkeypatch.setattr(hook_executor.database, "get_hooks_for_project_and_event",
                            lambda _p, _e: hooks)
        ran: list[str] = []

        async def _execute(hook, data, session_id=None):
            ran.append(hook["id"])
            return {"success": True}

        monkeypatch.setattr(hook_executor, "execute_hook", _execute)
        await hook_executor.execute_hooks_for_event("p", "PostToolUse", {}, tool_name="Bash")
        await hook_executor.execute_hooks_for_event("p", "PostToolUse", {}, tool_name="Bash(")
        import asyncio
        await asyncio.gather(*list(hook_executor._active_hook_tasks))

        # "Bash(" is not a regex: it matches only the literal tool name "Bash(".
        assert sorted(ran) == ["bad", "good", "literal"]

    def test_matcher_helper(self):
        from app.core.hook_executor import tool_matcher_matches
        assert tool_matcher_matches("Bash|Edit", "Edit")
        assert not tool_matcher_matches("Bash", "BashOutput")
        assert not tool_matcher_matches("[", "Bash")
        assert tool_matcher_matches("[", "[")

    @pytest.mark.asyncio
    async def test_an_invalid_plugin_matcher_does_not_skip_the_other_plugin_hooks(self, tmp_path):
        ran: list[str] = []

        async def _execute(hook_def, *_args, **_kwargs):
            ran.append(hook_def["command"])
            return None

        hook_defs = [
            {"command": "bad", "matcher": "Zap(", "plugin_path": str(tmp_path)},
            {"command": "good", "matcher": "bash|Edit", "plugin_path": str(tmp_path)},
            {"command": "star", "matcher": "*", "plugin_path": str(tmp_path)},
            {"command": "other", "matcher": "Write", "plugin_path": str(tmp_path)},
        ]
        with patch("app.core.hook_service.execute_plugin_hook", _execute):
            handler = create_plugin_hook_handler("PreToolUse", hook_defs, str(tmp_path))
            await handler({"hook_event_name": "PreToolUse", "tool_name": "Bash"}, None, {})
            await handler({"hook_event_name": "PreToolUse", "tool_name": "zap("}, None, {})

        # "Zap(" is compared as plain text (case-insensitively, like plugin
        # regexes); "*" matches every tool.
        assert ran == ["good", "star", "bad", "star"]

    def test_plugin_matcher_helper_keeps_the_hooks_json_rule(self):
        from app.core.hook_executor import tool_matcher_matches
        assert tool_matcher_matches("bash", "BashOutput", plugin=True)
        assert not tool_matcher_matches("Edit", "MultiEdit", plugin=True)
        assert tool_matcher_matches("Bash(", "bash(", plugin=True)


class TestMatcherTimeouts:
    def test_db_pretooluse_and_stop_matchers_cover_each_hooks_timeout(self, monkeypatch, tmp_path):
        from app.core import hook_service
        rows = {
            "PreToolUse": [{"timeout": 120000}, {"timeout": 300000}],
            "Stop": [{"timeout": 200000}],
        }
        monkeypatch.setattr(hook_service, "_project_has_db_pretooluse_hooks", lambda _p: True)
        monkeypatch.setattr(hook_service, "_db_observed_hook_events", lambda _p: [])
        monkeypatch.setattr(hook_service.database, "get_hooks_for_project_and_event",
                            lambda _p, event: rows.get(event, []))
        config = build_hooks_config(str(tmp_path), "p")

        # Hooks run one after another, each under its own timeout.
        assert config["PreToolUse"][0].timeout >= 120 + 300
        assert config["Stop"][0].timeout >= 200

    def test_plugin_matchers_cover_hooks_json_timeouts(self, tmp_path):
        plugin_dir = tmp_path / "plugin"
        (plugin_dir / "hooks").mkdir(parents=True)
        (plugin_dir / "hooks" / "hooks.json").write_text("""{"hooks": {
            "PostToolUse": [{"hooks": [{"type": "command", "command": "a", "timeout": 90},
                                       {"type": "command", "command": "b", "timeout": 45}]}],
            "Stop": [{"hooks": [{"type": "command", "command": "c"}]}]
        }}""")
        config = build_hooks_config(str(tmp_path), None, plugin_paths=[str(plugin_dir)])
        assert config["PostToolUse"][0].timeout >= 135
        assert config["Stop"][0].timeout >= 120  # plugin Stop default

    def test_small_timeouts_keep_the_old_60s_floor(self, monkeypatch, tmp_path):
        from app.core import hook_service
        monkeypatch.setattr(hook_service, "_project_has_db_pretooluse_hooks", lambda _p: True)
        monkeypatch.setattr(hook_service, "_db_observed_hook_events", lambda _p: [])
        monkeypatch.setattr(hook_service.database, "get_hooks_for_project_and_event",
                            lambda _p, _e: [{"timeout": 1000}])
        config = build_hooks_config(str(tmp_path), "p")
        assert config["PreToolUse"][0].timeout == 60.0
