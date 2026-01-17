"""Tests for the jq shim CLI interface."""

import json
import subprocess
import sys
import os
from pathlib import Path


# Get the project root directory
# test_cli.py -> test_jq_shim -> core -> tests -> feature-jq-shim-bb7d (project root)
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent


def _run_jq_helper(*args, input_data=None):
    """Helper to run jq shim via subprocess with proper environment."""
    cmd = [sys.executable, "-m", "app.core.jq_shim"] + list(args)
    # Set PYTHONPATH to include project root
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        cmd,
        input=input_data,
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        env=env,
    )
    return result


class TestCLI:
    """CLI interface tests."""

    def _run_jq(self, *args, input_data=None):
        """Helper to run jq shim via subprocess."""
        return _run_jq_helper(*args, input_data=input_data)

    def test_basic_filter(self):
        """Should filter JSON from stdin."""
        result = self._run_jq(".field", input_data='{"field": "value"}')
        assert result.returncode == 0
        assert json.loads(result.stdout.strip()) == "value"

    def test_identity(self):
        """Should pass through with identity filter."""
        result = self._run_jq(".", input_data='{"a": 1}')
        assert result.returncode == 0
        assert json.loads(result.stdout.strip()) == {"a": 1}

    def test_raw_output(self):
        """Should output raw strings with -r flag."""
        result = self._run_jq("-r", ".field", input_data='{"field": "value"}')
        assert result.returncode == 0
        assert result.stdout.strip() == "value"  # No quotes

    def test_raw_output_long_flag(self):
        """Should output raw strings with --raw-output flag."""
        result = self._run_jq("--raw-output", ".field", input_data='{"field": "value"}')
        assert result.returncode == 0
        assert result.stdout.strip() == "value"

    def test_nested_field(self):
        """Should access nested fields."""
        result = self._run_jq(".a.b.c", input_data='{"a": {"b": {"c": 42}}}')
        assert result.returncode == 0
        assert json.loads(result.stdout.strip()) == 42

    def test_array_index(self):
        """Should index arrays."""
        result = self._run_jq(".[1]", input_data="[1, 2, 3]")
        assert result.returncode == 0
        assert json.loads(result.stdout.strip()) == 2

    def test_arg_variable(self):
        """Should support --arg for variables."""
        result = self._run_jq("--arg", "name", "test", "$name", input_data="{}")
        assert result.returncode == 0
        assert json.loads(result.stdout.strip()) == "test"

    def test_argjson_variable(self):
        """Should support --argjson for JSON variables."""
        result = self._run_jq("--argjson", "num", "42", "$num", input_data="{}")
        assert result.returncode == 0
        assert json.loads(result.stdout.strip()) == 42

    def test_default_value(self):
        """Should support // default syntax."""
        result = self._run_jq("-r", '.missing // "default"', input_data="{}")
        assert result.returncode == 0
        assert result.stdout.strip() == "default"

    def test_object_construction(self):
        """Should construct objects."""
        result = self._run_jq("{a: .x, b: .y}", input_data='{"x": 1, "y": 2}')
        assert result.returncode == 0
        assert json.loads(result.stdout.strip()) == {"a": 1, "b": 2}

    def test_compact_output(self):
        """Should compact output with -c flag."""
        result = self._run_jq("-c", ".", input_data='{"a": 1, "b": 2}')
        assert result.returncode == 0
        # No extra whitespace
        assert "\n" not in result.stdout.strip()

    def test_null_input(self):
        """Should use null input with -n flag."""
        result = self._run_jq("-n", "null")
        assert result.returncode == 0
        assert result.stdout.strip() == "null"

    def test_sort_keys(self):
        """Should sort keys with -S flag."""
        result = self._run_jq("-S", ".", input_data='{"b": 2, "a": 1}')
        assert result.returncode == 0
        output = result.stdout.strip()
        # a should come before b in output
        assert output.index('"a"') < output.index('"b"')

    def test_file_input(self, tmp_path):
        """Should read from file."""
        json_file = tmp_path / "test.json"
        json_file.write_text('{"key": "value"}')

        result = self._run_jq(".key", str(json_file))
        assert result.returncode == 0
        assert json.loads(result.stdout.strip()) == "value"

    def test_error_invalid_json(self):
        """Should error on invalid JSON input."""
        result = self._run_jq(".field", input_data="not json")
        assert result.returncode != 0
        assert "error" in result.stderr.lower()

    def test_error_invalid_filter(self):
        """Should error on invalid filter."""
        result = self._run_jq("@invalid", input_data="{}")
        assert result.returncode != 0
        assert "error" in result.stderr.lower()

    def test_version(self):
        """Should show version."""
        result = self._run_jq("--version")
        assert result.returncode == 0
        assert "jq-shim" in result.stdout.lower()

    def test_pipe_expression(self):
        """Should handle pipe expressions."""
        result = self._run_jq(".items | .[0]", input_data='{"items": [1, 2, 3]}')
        assert result.returncode == 0
        assert json.loads(result.stdout.strip()) == 1

    def test_select_function(self):
        """Should handle select function."""
        result = self._run_jq(
            ".[] | select(.x > 0)",
            input_data='[{"x": -1}, {"x": 5}, {"x": 0}]',
        )
        assert result.returncode == 0
        # Should only output the matching item
        assert json.loads(result.stdout.strip()) == {"x": 5}

    def test_map_function(self):
        """Should handle map function."""
        result = self._run_jq(
            "map(.name)",
            input_data='[{"name": "a"}, {"name": "b"}]',
        )
        assert result.returncode == 0
        assert json.loads(result.stdout.strip()) == ["a", "b"]


class TestCLIRealWorldPatterns:
    """Test real-world patterns via CLI."""

    def _run_jq(self, *args, input_data=None):
        """Helper to run jq shim via subprocess."""
        return _run_jq_helper(*args, input_data=input_data)

    def test_tool_input_command_pattern(self):
        """Common hook pattern for extracting command."""
        input_data = json.dumps({
            "tool_name": "Bash",
            "tool_input": {"command": "npm install"}
        })
        result = self._run_jq("-r", '.tool_input.command // ""', input_data=input_data)
        assert result.returncode == 0
        assert result.stdout.strip() == "npm install"

    def test_hook_decision_response(self):
        """Pattern for hook decision response construction."""
        input_data = json.dumps({
            "tool_name": "Edit",
            "message": "blocked"
        })
        result = self._run_jq(
            '{decision: "block", reason: .message}',
            input_data=input_data,
        )
        assert result.returncode == 0
        output = json.loads(result.stdout.strip())
        assert output == {"decision": "block", "reason": "blocked"}

    def test_variable_in_object(self):
        """Pattern using variables in object construction."""
        input_data = json.dumps({"tool_name": "Bash"})
        result = self._run_jq(
            "--arg", "ts", "2024-01-01",
            "{timestamp: $ts, tool: .tool_name}",
            input_data=input_data,
        )
        assert result.returncode == 0
        output = json.loads(result.stdout.strip())
        assert output == {"timestamp": "2024-01-01", "tool": "Bash"}
