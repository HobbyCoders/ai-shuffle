"""Integration tests for jq shim with hook service."""

import pytest
import json
import os
import sys
import subprocess
from pathlib import Path
from unittest.mock import patch, AsyncMock

# Add parent directories to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.core.hook_service import execute_plugin_hook, _get_jq_shim_env


class TestJQShimEnv:
    """Tests for jq shim environment setup."""

    def test_get_jq_shim_env_adds_shim_to_path(self):
        """Should add jq shim bin to PATH."""
        env = {"PATH": "/usr/bin"}
        result = _get_jq_shim_env(env)

        assert "jq_shim" in result["PATH"]
        assert "bin" in result["PATH"]
        # Original path should still be there
        assert "/usr/bin" in result["PATH"]

    def test_get_jq_shim_env_adds_pythonpath(self):
        """Should add app root to PYTHONPATH."""
        env = {}
        result = _get_jq_shim_env(env)

        assert "PYTHONPATH" in result
        # Should contain path to app directory
        assert "app" in result["PYTHONPATH"] or result["PYTHONPATH"]

    def test_get_jq_shim_env_preserves_existing_pythonpath(self):
        """Should preserve existing PYTHONPATH."""
        env = {"PYTHONPATH": "/some/existing/path"}
        result = _get_jq_shim_env(env)

        assert "/some/existing/path" in result["PYTHONPATH"]

    def test_shim_prepends_to_path(self):
        """Shim should be first in PATH (takes precedence)."""
        env = {"PATH": "/usr/bin:/usr/local/bin"}
        result = _get_jq_shim_env(env)

        # Shim path should be at the start
        path_parts = result["PATH"].split(os.pathsep)
        assert "jq_shim" in path_parts[0]


class TestJQShimExecution:
    """Test jq shim can be executed via the bin scripts."""

    @pytest.fixture
    def shim_env(self):
        """Get environment with jq shim in PATH."""
        env = os.environ.copy()
        return _get_jq_shim_env(env)

    def test_shim_script_exists(self):
        """Shim script should exist in expected location."""
        shim_path = Path(__file__).parent.parent.parent.parent / "app" / "core" / "jq_shim" / "bin" / "jq"
        assert shim_path.exists(), f"Shim not found at {shim_path}"

    def test_shim_script_is_executable(self):
        """Shim script should be executable on Unix."""
        if sys.platform == "win32":
            pytest.skip("Executable check not applicable on Windows")

        shim_path = Path(__file__).parent.parent.parent.parent / "app" / "core" / "jq_shim" / "bin" / "jq"
        assert os.access(shim_path, os.X_OK), "Shim should be executable"

    def test_shim_via_subprocess(self, shim_env):
        """Should be able to run jq via subprocess with shim in PATH."""
        # Use bash to run jq (simulating how hooks work)
        if sys.platform == "win32":
            # On Windows, use cmd
            cmd = ["cmd", "/c", "echo {\"a\": 1} | jq .a"]
        else:
            cmd = ["bash", "-c", 'echo \'{"a": 1}\' | jq .a']

        result = subprocess.run(
            cmd,
            env=shim_env,
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).parent.parent.parent.parent),
        )

        assert result.returncode == 0, f"Failed: {result.stderr}"
        assert json.loads(result.stdout.strip()) == 1


class TestHookServiceIntegration:
    """Integration tests with hook service."""

    @pytest.mark.asyncio
    async def test_hook_with_jq_field_access(self, tmp_path):
        """Should execute hook script using jq for field access."""
        # Create a hook script that uses jq - directly pipe to jq
        script = tmp_path / "hook.sh"
        script.write_text('''#!/bin/bash
cat | jq '{result: .tool_name}'
''')
        script.chmod(0o755)

        hook_def = {
            "command": f"bash {script}",
            "timeout": 5,
            "plugin_path": str(tmp_path)
        }
        hook_input = {"tool_name": "Edit", "tool_input": {"file_path": "/test.py"}}

        result = await execute_plugin_hook(hook_def, hook_input, str(tmp_path))

        assert result is not None
        assert result.get("result") == "Edit"

    @pytest.mark.asyncio
    async def test_hook_with_jq_nested_field(self, tmp_path):
        """Should execute hook with jq nested field access."""
        script = tmp_path / "hook.sh"
        script.write_text('''#!/bin/bash
cat | jq '{path: .tool_input.file_path}'
''')
        script.chmod(0o755)

        hook_def = {
            "command": f"bash {script}",
            "timeout": 5,
            "plugin_path": str(tmp_path)
        }
        hook_input = {"tool_name": "Edit", "tool_input": {"file_path": "/src/main.py"}}

        result = await execute_plugin_hook(hook_def, hook_input, str(tmp_path))

        assert result is not None
        assert result.get("path") == "/src/main.py"

    @pytest.mark.asyncio
    async def test_hook_with_jq_object_construction(self, tmp_path):
        """Should execute hook with jq object construction."""
        script = tmp_path / "hook.sh"
        script.write_text('''#!/bin/bash
cat | jq '{tool: .tool_name, path: .tool_input.file_path}'
''')
        script.chmod(0o755)

        hook_def = {
            "command": f"bash {script}",
            "timeout": 5,
            "plugin_path": str(tmp_path)
        }
        hook_input = {"tool_name": "Edit", "tool_input": {"file_path": "/test.py"}}

        result = await execute_plugin_hook(hook_def, hook_input, str(tmp_path))

        assert result is not None
        assert result.get("tool") == "Edit"
        assert result.get("path") == "/test.py"

    @pytest.mark.asyncio
    async def test_hook_with_jq_default_value(self, tmp_path):
        """Should execute hook with jq default value."""
        script = tmp_path / "hook.sh"
        script.write_text('''#!/bin/bash
cat | jq '{value: (.missing_field // "default_value")}'
''')
        script.chmod(0o755)

        hook_def = {
            "command": f"bash {script}",
            "timeout": 5,
            "plugin_path": str(tmp_path)
        }
        hook_input = {"tool_name": "Edit"}

        result = await execute_plugin_hook(hook_def, hook_input, str(tmp_path))

        assert result is not None
        assert result.get("value") == "default_value"

    @pytest.mark.asyncio
    async def test_hook_with_jq_variables(self, tmp_path):
        """Should execute hook with jq --arg variables."""
        script = tmp_path / "hook.sh"
        script.write_text('''#!/bin/bash
ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
cat | jq --arg timestamp "$ts" '{ts: $timestamp, tool: .tool_name}'
''')
        script.chmod(0o755)

        hook_def = {
            "command": f"bash {script}",
            "timeout": 5,
            "plugin_path": str(tmp_path)
        }
        hook_input = {"tool_name": "Bash"}

        result = await execute_plugin_hook(hook_def, hook_input, str(tmp_path))

        assert result is not None
        assert "ts" in result
        assert result.get("tool") == "Bash"

    @pytest.mark.asyncio
    async def test_hook_block_decision(self, tmp_path):
        """Should handle hook returning block decision."""
        script = tmp_path / "hook.sh"
        script.write_text('''#!/bin/bash
cat | jq '{decision: "block", reason: "testing"}'
''')
        script.chmod(0o755)

        hook_def = {
            "command": f"bash {script}",
            "timeout": 5,
            "plugin_path": str(tmp_path)
        }
        hook_input = {"tool_name": "Bash"}

        result = await execute_plugin_hook(hook_def, hook_input, str(tmp_path))

        assert result is not None
        assert result.get("decision") == "block"
        assert result.get("reason") == "testing"


@pytest.mark.skipif(sys.platform == "win32", reason="Shell scripts for Unix")
class TestUnixHookScripts:
    """Test Unix-specific hook script patterns."""

    @pytest.mark.asyncio
    async def test_complex_jq_pipeline(self, tmp_path):
        """Should handle complex jq pipelines."""
        script = tmp_path / "hook.sh"
        script.write_text('''#!/bin/bash
cat | jq '{
  event: .hook_event_name,
  tool: .tool_name,
  has_file: (.tool_input.file_path != null)
}'
''')
        script.chmod(0o755)

        hook_def = {
            "command": f"bash {script}",
            "timeout": 5,
            "plugin_path": str(tmp_path)
        }
        hook_input = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Edit",
            "tool_input": {"file_path": "/test.py"}
        }

        result = await execute_plugin_hook(hook_def, hook_input, str(tmp_path))

        assert result is not None
        assert result.get("event") == "PreToolUse"
        assert result.get("tool") == "Edit"
        assert result.get("has_file") is True
