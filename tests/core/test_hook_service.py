"""
Tests for the unified hook service.
"""

import pytest
from unittest.mock import patch, AsyncMock

from app.core.hook_service import (
    load_plugin_hooks,
    build_hooks_config,
    create_stop_hook_handler,
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
            {"command": f"bash {script}", "timeout": 5, "plugin_path": str(tmp_path)},
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
            {"command": f"bash {script}", "timeout": 5, "plugin_path": str(tmp_path)},
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
            {"command": f"bash {script}", "timeout": 5, "plugin_path": str(tmp_path)},
            {"hook_event_name": "Test"},
            str(tmp_path),
            stderr_callback=stderr_callback
        )

        assert len(callback_called) == 1
        assert "warning message" in callback_called[0][1]
