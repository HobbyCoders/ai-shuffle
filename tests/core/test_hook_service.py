"""
Tests for the hook service.

Note: Plugin hooks (like Ralph Loop) are handled natively by Claude CLI.
These tests only cover database hooks created via AI Shuffle's HooksCard.
"""

import pytest
from unittest.mock import patch, AsyncMock

from app.core.hook_service import (
    get_database_hooks_for_event,
    build_hooks_config,
    create_stop_hook_handler,
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

    def test_no_database_hooks(self, tmp_path):
        """Should return None when no database hooks exist."""
        with patch('app.core.hook_service.database') as mock_db:
            mock_db.get_hooks_for_project_and_event.return_value = []
            result = build_hooks_config(str(tmp_path), "test-project")
        assert result is None

    def test_with_database_hooks(self, tmp_path):
        """Should register Stop hook when database hooks exist."""
        with patch('app.core.hook_service.database') as mock_db:
            mock_db.get_hooks_for_project_and_event.return_value = [
                {"name": "test-hook", "command": "echo test"}
            ]
            result = build_hooks_config(str(tmp_path), "test-project")
        assert result is not None
        assert "Stop" in result
        assert len(result["Stop"]) == 1

    def test_no_project_id(self, tmp_path):
        """Should return None when no project ID (no database hooks possible)."""
        result = build_hooks_config(str(tmp_path), None)
        assert result is None


class TestStopHookHandler:
    """Tests for the Stop hook handler."""

    @pytest.mark.asyncio
    async def test_non_stop_event(self, tmp_path):
        """Should return empty output for non-Stop events."""
        handler = create_stop_hook_handler(str(tmp_path), "test-project")
        result = await handler(
            {"hook_event_name": "PreToolUse"},
            None,
            {"signal": None}
        )
        assert result.get("decision") is None

    @pytest.mark.asyncio
    async def test_no_database_hooks(self, tmp_path):
        """Should allow stop when no database hooks."""
        with patch('app.core.hook_service.get_database_hooks_for_event', new_callable=AsyncMock) as mock:
            mock.return_value = []
            handler = create_stop_hook_handler(str(tmp_path), "test-project")
            result = await handler(
                {"hook_event_name": "Stop", "stop_hook_active": False},
                None,
                {"signal": None}
            )
        assert result.get("decision") is None

    @pytest.mark.asyncio
    async def test_database_hook_blocks(self, tmp_path):
        """Should block when database hook returns block decision."""
        with patch('app.core.hook_service.get_database_hooks_for_event', new_callable=AsyncMock) as mock_get:
            mock_get.return_value = [{"name": "test-hook", "command": "echo test"}]
            with patch('app.core.hook_service.execute_hook', new_callable=AsyncMock) as mock_exec:
                mock_exec.return_value = {
                    "success": True,
                    "output": '{"decision": "block", "reason": "continue prompt", "systemMessage": "test"}'
                }
                handler = create_stop_hook_handler(str(tmp_path), "test-project")
                result = await handler(
                    {"hook_event_name": "Stop", "stop_hook_active": False},
                    None,
                    {"signal": None}
                )
        assert result.get("decision") == "block"
        assert result.get("reason") == "continue prompt"
        assert result.get("systemMessage") == "test"

    @pytest.mark.asyncio
    async def test_database_hook_allows(self, tmp_path):
        """Should allow stop when database hook doesn't block."""
        with patch('app.core.hook_service.get_database_hooks_for_event', new_callable=AsyncMock) as mock_get:
            mock_get.return_value = [{"name": "test-hook", "command": "echo test"}]
            with patch('app.core.hook_service.execute_hook', new_callable=AsyncMock) as mock_exec:
                mock_exec.return_value = {
                    "success": True,
                    "output": '{"status": "ok"}'  # No block decision
                }
                handler = create_stop_hook_handler(str(tmp_path), "test-project")
                result = await handler(
                    {"hook_event_name": "Stop", "stop_hook_active": False},
                    None,
                    {"signal": None}
                )
        assert result.get("decision") is None
