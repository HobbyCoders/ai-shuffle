"""
Tests for the hook service.

The hook service enables the CLI's hook system by passing a hooks config to the SDK.
This allows:
1. Plugin hooks (like Ralph Loop) to work natively via CLI
2. Database hooks (from HooksCard) to work via SDK callbacks
"""

import pytest
from unittest.mock import patch, AsyncMock

from app.core.hook_service import (
    get_database_hooks_for_event,
    build_hooks_config,
    create_hook_handler,
    SUPPORTED_HOOK_EVENTS,
)


class TestGetDatabaseHooks:
    """Tests for database hook retrieval."""

    @pytest.mark.asyncio
    async def test_no_project_id(self):
        """Should return empty list when no project ID."""
        result = await get_database_hooks_for_event(None, "Stop")
        assert result == []

    @pytest.mark.asyncio
    async def test_with_project_id(self):
        """Should query database for hooks."""
        with patch('app.core.hook_service.database') as mock_db:
            mock_db.get_hooks_for_project_and_event.return_value = [
                {"name": "test-hook", "command": "echo test"}
            ]
            result = await get_database_hooks_for_event("test-project", "Stop")
            assert len(result) == 1
            assert result[0]["name"] == "test-hook"
            mock_db.get_hooks_for_project_and_event.assert_called_once_with("test-project", "Stop")

    @pytest.mark.asyncio
    async def test_database_error(self):
        """Should return empty list on database error."""
        with patch('app.core.hook_service.database') as mock_db:
            mock_db.get_hooks_for_project_and_event.side_effect = Exception("DB error")
            result = await get_database_hooks_for_event("test-project", "Stop")
            assert result == []


class TestBuildHooksConfig:
    """Tests for building SDK hooks configuration."""

    def test_always_returns_config(self, tmp_path):
        """Should always return a hooks config to enable CLI hook system."""
        with patch('app.core.hook_service.database') as mock_db:
            mock_db.get_hooks_for_project_and_event.return_value = []
            result = build_hooks_config(str(tmp_path), "test-project")

        # Should always return a config (never None)
        assert result is not None
        # Should have at least a Stop hook to enable CLI
        assert "Stop" in result
        assert len(result["Stop"]) == 1

    def test_with_database_hooks(self, tmp_path):
        """Should register handlers for events with database hooks."""
        with patch('app.core.hook_service.database') as mock_db:
            # Simulate Stop hooks exist in database
            def get_hooks(project_id, event):
                if event == "Stop":
                    return [{"name": "test-hook", "command": "echo test"}]
                return []
            mock_db.get_hooks_for_project_and_event.side_effect = get_hooks

            result = build_hooks_config(str(tmp_path), "test-project")

        assert result is not None
        assert "Stop" in result
        assert len(result["Stop"]) == 1

    def test_multiple_event_types(self, tmp_path):
        """Should register handlers for multiple event types with database hooks."""
        with patch('app.core.hook_service.database') as mock_db:
            # Simulate hooks for multiple events
            def get_hooks(project_id, event):
                if event in ["Stop", "PreToolUse"]:
                    return [{"name": f"{event}-hook", "command": "echo test"}]
                return []
            mock_db.get_hooks_for_project_and_event.side_effect = get_hooks

            result = build_hooks_config(str(tmp_path), "test-project")

        assert result is not None
        assert "Stop" in result
        assert "PreToolUse" in result

    def test_no_project_id(self, tmp_path):
        """Should still return config when no project ID (to enable plugins)."""
        result = build_hooks_config(str(tmp_path), None)

        # Should still return a config to enable CLI hook system
        assert result is not None
        assert "Stop" in result


class TestCreateHookHandler:
    """Tests for the generic hook handler factory."""

    @pytest.mark.asyncio
    async def test_wrong_event_type(self, tmp_path):
        """Should return empty output for mismatched event type."""
        handler = create_hook_handler("Stop", str(tmp_path), "test-project")
        result = await handler(
            {"hook_event_name": "PreToolUse"},
            None,
            {"signal": None}
        )
        assert result.get("decision") is None

    @pytest.mark.asyncio
    async def test_no_database_hooks(self, tmp_path):
        """Should return empty output when no database hooks."""
        with patch('app.core.hook_service.get_database_hooks_for_event', new_callable=AsyncMock) as mock:
            mock.return_value = []
            handler = create_hook_handler("Stop", str(tmp_path), "test-project")
            result = await handler(
                {"hook_event_name": "Stop", "stop_hook_active": False},
                None,
                {"signal": None}
            )
        assert result.get("decision") is None

    @pytest.mark.asyncio
    async def test_database_hook_blocks(self, tmp_path):
        """Should honor block decision from database hook."""
        with patch('app.core.hook_service.get_database_hooks_for_event', new_callable=AsyncMock) as mock_get:
            mock_get.return_value = [{"name": "test-hook", "command": "echo test"}]
            with patch('app.core.hook_service.execute_hook', new_callable=AsyncMock) as mock_exec:
                mock_exec.return_value = {
                    "success": True,
                    "output": '{"decision": "block", "reason": "continue", "systemMessage": "test"}'
                }
                handler = create_hook_handler("Stop", str(tmp_path), "test-project")
                result = await handler(
                    {"hook_event_name": "Stop", "stop_hook_active": False},
                    None,
                    {"signal": None}
                )
        assert result.get("decision") == "block"
        assert result.get("reason") == "continue"
        assert result.get("systemMessage") == "test"

    @pytest.mark.asyncio
    async def test_database_hook_denies(self, tmp_path):
        """Should honor deny decision from database hook (PreToolUse)."""
        with patch('app.core.hook_service.get_database_hooks_for_event', new_callable=AsyncMock) as mock_get:
            mock_get.return_value = [{"name": "test-hook", "command": "echo test"}]
            with patch('app.core.hook_service.execute_hook', new_callable=AsyncMock) as mock_exec:
                mock_exec.return_value = {
                    "success": True,
                    "output": '{"decision": "deny", "reason": "not allowed"}'
                }
                handler = create_hook_handler("PreToolUse", str(tmp_path), "test-project")
                result = await handler(
                    {"hook_event_name": "PreToolUse", "tool_name": "Bash"},
                    "tool-123",
                    {"signal": None}
                )
        assert result.get("decision") == "deny"
        assert result.get("reason") == "not allowed"

    @pytest.mark.asyncio
    async def test_database_hook_approves(self, tmp_path):
        """Should honor approve decision from database hook (PreToolUse)."""
        with patch('app.core.hook_service.get_database_hooks_for_event', new_callable=AsyncMock) as mock_get:
            mock_get.return_value = [{"name": "test-hook", "command": "echo test"}]
            with patch('app.core.hook_service.execute_hook', new_callable=AsyncMock) as mock_exec:
                mock_exec.return_value = {
                    "success": True,
                    "output": '{"decision": "approve"}'
                }
                handler = create_hook_handler("PreToolUse", str(tmp_path), "test-project")
                result = await handler(
                    {"hook_event_name": "PreToolUse", "tool_name": "Bash"},
                    "tool-123",
                    {"signal": None}
                )
        assert result.get("decision") == "approve"


class TestSupportedHookEvents:
    """Tests for supported hook events."""

    def test_all_events_defined(self):
        """Should have all expected hook events defined."""
        expected = ["PreToolUse", "PostToolUse", "Stop", "SubagentStop", "PreCompact", "UserPromptSubmit"]
        for event in expected:
            assert event in SUPPORTED_HOOK_EVENTS
