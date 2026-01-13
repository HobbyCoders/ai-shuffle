"""
Tests for the unified hook service.
"""

import pytest
import tempfile
import os
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

from app.core.hook_service import (
    detect_plugin_loop_state,
    _read_ralph_loop_state,
    _update_plugin_iteration,
    _remove_plugin_state,
    build_hooks_config,
    create_stop_hook_handler,
)


class TestRalphLoopStateDetection:
    """Tests for Ralph Loop state file detection."""

    def test_no_state_file(self, tmp_path):
        """Should return None if no state file exists."""
        result = _read_ralph_loop_state(str(tmp_path))
        assert result is None

    def test_read_valid_state_file(self, tmp_path):
        """Should correctly parse Ralph Loop state file."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        state_file = claude_dir / "ralph-loop.local.md"
        state_file.write_text("""---
active: true
iteration: 3
max_iterations: 10
completion_promise: "DONE"
started_at: "2024-01-01T00:00:00Z"
---

Build a REST API for todos
""")
        result = _read_ralph_loop_state(str(tmp_path))

        assert result is not None
        assert result["iteration"] == 3
        assert result["max_iterations"] == 10
        assert result["completion_promise"] == "DONE"
        assert result["prompt"] == "Build a REST API for todos"

    def test_read_state_file_null_promise(self, tmp_path):
        """Should handle null completion_promise."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        state_file = claude_dir / "ralph-loop.local.md"
        state_file.write_text("""---
active: true
iteration: 1
max_iterations: 0
completion_promise: null
---

Some task
""")
        result = _read_ralph_loop_state(str(tmp_path))

        assert result is not None
        assert result["iteration"] == 1
        assert result["max_iterations"] == 0
        assert result["completion_promise"] is None

    def test_read_malformed_state_file(self, tmp_path):
        """Should return None for malformed state files."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        state_file = claude_dir / "ralph-loop.local.md"
        state_file.write_text("not a valid state file")

        result = _read_ralph_loop_state(str(tmp_path))
        assert result is None


class TestPluginLoopDetection:
    """Tests for generic plugin loop detection."""

    def test_detect_ralph_loop(self, tmp_path):
        """Should detect active Ralph Loop."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        state_file = claude_dir / "ralph-loop.local.md"
        state_file.write_text("""---
active: true
iteration: 1
max_iterations: 5
completion_promise: null
---

Test prompt
""")
        result = detect_plugin_loop_state(str(tmp_path))

        assert result is not None
        assert result["plugin"] == "ralph-loop"
        assert result["state"]["iteration"] == 1

    def test_detect_no_plugin_loop(self, tmp_path):
        """Should return None when no plugin loop is active."""
        result = detect_plugin_loop_state(str(tmp_path))
        assert result is None


class TestPluginStateManagement:
    """Tests for plugin state file management."""

    def test_update_iteration(self, tmp_path):
        """Should update iteration count in state file."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        state_file = claude_dir / "ralph-loop.local.md"
        state_file.write_text("""---
active: true
iteration: 1
max_iterations: 5
---

Test
""")
        result = _update_plugin_iteration(str(tmp_path), "ralph-loop", 2)
        assert result is True

        content = state_file.read_text()
        assert "iteration: 2" in content

    def test_remove_state(self, tmp_path):
        """Should remove plugin state file."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        state_file = claude_dir / "ralph-loop.local.md"
        state_file.write_text("test")

        result = _remove_plugin_state(str(tmp_path), "ralph-loop")
        assert result is True
        assert not state_file.exists()


class TestBuildHooksConfig:
    """Tests for building SDK hooks configuration."""

    def test_always_registers_stop_hook(self, tmp_path):
        """Should always register Stop hook (to catch plugins that start mid-query)."""
        # Even with no active plugin loop or database hooks, we still register Stop hook
        # because plugins like Ralph Loop create their state files DURING the query
        result = build_hooks_config(str(tmp_path), "test-project")
        assert result is not None
        assert "Stop" in result
        assert len(result["Stop"]) == 1

    def test_hooks_for_active_plugin_loop(self, tmp_path):
        """Should create Stop hook when plugin loop is active."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        state_file = claude_dir / "ralph-loop.local.md"
        state_file.write_text("""---
active: true
iteration: 1
max_iterations: 5
---

Test
""")
        with patch('app.core.hook_service.database') as mock_db:
            mock_db.get_hooks_for_project_and_event.return_value = []
            result = build_hooks_config(str(tmp_path), "test-project")

        assert result is not None
        assert "Stop" in result


class TestStopHookHandler:
    """Tests for the unified Stop hook handler."""

    @pytest.mark.asyncio
    async def test_ralph_loop_continuation(self, tmp_path):
        """Should return block decision for active Ralph Loop."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        state_file = claude_dir / "ralph-loop.local.md"
        state_file.write_text("""---
active: true
iteration: 1
max_iterations: 5
completion_promise: null
---

Test prompt
""")

        handler = create_stop_hook_handler(str(tmp_path))
        result = await handler(
            {"hook_event_name": "Stop", "stop_hook_active": False},
            None,
            {"signal": None}
        )

        assert result.get("decision") == "block"
        assert result.get("reason") == "Test prompt"
        assert "iteration 2" in result.get("systemMessage", "")

    @pytest.mark.asyncio
    async def test_ralph_loop_max_iterations(self, tmp_path):
        """Should allow stop when max iterations reached."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        state_file = claude_dir / "ralph-loop.local.md"
        state_file.write_text("""---
active: true
iteration: 5
max_iterations: 5
completion_promise: null
---

Test prompt
""")

        handler = create_stop_hook_handler(str(tmp_path))
        result = await handler(
            {"hook_event_name": "Stop", "stop_hook_active": False},
            None,
            {"signal": None}
        )

        # Should not block when max iterations reached
        assert result.get("decision") != "block"

    @pytest.mark.asyncio
    async def test_no_active_loop(self, tmp_path):
        """Should allow stop when no plugin loop is active."""
        with patch('app.core.hook_service.get_database_hooks_for_event', new_callable=AsyncMock) as mock:
            mock.return_value = []
            handler = create_stop_hook_handler(str(tmp_path))
            result = await handler(
                {"hook_event_name": "Stop", "stop_hook_active": False},
                None,
                {"signal": None}
            )

        # Should not block
        assert result.get("decision") != "block"
