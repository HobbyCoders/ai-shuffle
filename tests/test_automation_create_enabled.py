"""
Regression tests for the "created disabled but reported scheduled" trap.

Every automation creation path used to omit ``is_enabled`` from the INSERT, so
automations were always created disabled (``is_enabled`` DEFAULT false) while
the MCP tool cheerfully returned ``scheduled: true`` and "created and
scheduled" — a lie, since the 60s reconcile loop only schedules
``get_enabled_automations()`` and would immediately remove any job for a
disabled automation. These tests pin the honest contract per path.
"""

import json

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# Eagerly import the singleton BEFORE any patching so we never freeze a stale
# from-import binding (see MEMORY: lazy-import mock freeze).
from app.core.automation_manager import automation_manager
from app.core import automation_service
from app.mcp.config_automations import _create_automation_tools


def _get_tool(tools, name):
    for t in tools:
        if getattr(t, "name", None) == name:
            return t
    raise AssertionError(f"tool {name!r} not found")


def _text(result: dict) -> str:
    return result["content"][0]["text"]


def _payload(result: dict) -> dict:
    return json.loads(_text(result))


# =============================================================================
# DB layer
# =============================================================================


class TestDbCreateEnabled:
    def test_default_creates_disabled(self, mock_db):
        from app.db import database
        auto = database.create_automation(
            automation_id="auto-def",
            name="Default",
            trigger_type="interval",
            trigger_config={"seconds": 60},
            profile_id="default",
            prompt="hi",
        )
        assert auto is not None
        assert not auto["is_enabled"]

    def test_enabled_true_creates_enabled(self, mock_db):
        from app.db import database
        auto = database.create_automation(
            automation_id="auto-en",
            name="Enabled",
            trigger_type="interval",
            trigger_config={"seconds": 60},
            profile_id="default",
            prompt="hi",
            enabled=True,
        )
        assert auto is not None
        assert auto["is_enabled"]
        # And it is surfaced by the enabled-only loader the reconcile uses.
        ids = {a["id"] for a in database.get_enabled_automations()}
        assert "auto-en" in ids


# =============================================================================
# Service / REST layer
# =============================================================================


class TestServiceCreateEnabled:
    @pytest.mark.asyncio
    async def test_default_disabled_no_schedule(self, mock_db):
        with patch.object(automation_manager, "schedule_automation", new=AsyncMock()) as sched:
            auto = await automation_service.create_automation(
                name="svc-default",
                trigger_type="interval",
                trigger_config={"seconds": 60},
                profile_id="default",
                prompt="hi",
            )
        assert not auto["is_enabled"]
        sched.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_enabled_true_carries_state_and_schedules(self, mock_db):
        with patch.object(automation_manager, "schedule_automation", new=AsyncMock()) as sched:
            auto = await automation_service.create_automation(
                name="svc-enabled",
                trigger_type="interval",
                trigger_config={"seconds": 60},
                profile_id="default",
                prompt="hi",
                enabled=True,
            )
        assert auto["is_enabled"]
        sched.assert_awaited_once_with(auto["id"])


# =============================================================================
# MCP createAutomation tool
# =============================================================================


class TestMcpCreateAutomation:
    def _create_tool(self):
        tools = _create_automation_tools(is_admin=True, project_id=None)
        return _get_tool(tools, "createAutomation")

    @pytest.mark.asyncio
    async def test_default_enabled_and_active_message(self, mock_db):
        tool = self._create_tool()
        with patch.object(automation_manager, "schedule_automation", new=AsyncMock()) as sched:
            result = await tool.handler({
                "name": "mcp-default",
                "trigger_type": "interval",
                "interval_seconds": 60,
                "prompt": "hi",
                "profile_id": "default",
            })
        payload = _payload(result)
        assert payload["success"] is True
        assert payload["enabled"] is True
        assert payload["scheduled"] is True
        assert "ACTIVE" in payload["message"]
        # Persisted state agrees with the report.
        from app.db import database
        auto = database.get_automation(payload["automation_id"])
        assert auto["is_enabled"]
        sched.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_enabled_false_disabled_message_and_not_scheduled(self, mock_db):
        tool = self._create_tool()
        with patch.object(automation_manager, "schedule_automation", new=AsyncMock()) as sched:
            result = await tool.handler({
                "name": "mcp-paused",
                "trigger_type": "interval",
                "interval_seconds": 60,
                "prompt": "hi",
                "profile_id": "default",
                "enabled": False,
            })
        payload = _payload(result)
        assert payload["enabled"] is False
        assert payload["scheduled"] is False
        assert "DISABLED" in payload["message"]
        assert "toggleAutomation" in payload["message"]
        from app.db import database
        auto = database.get_automation(payload["automation_id"])
        assert not auto["is_enabled"]
        sched.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cron_active_message_mentions_schedule(self, mock_db):
        tool = self._create_tool()
        with patch.object(automation_manager, "schedule_automation", new=AsyncMock()):
            result = await tool.handler({
                "name": "mcp-cron",
                "trigger_type": "cron",
                "cron_expression": "0 9 * * 1-5",
                "prompt": "hi",
                "profile_id": "default",
            })
        payload = _payload(result)
        assert payload["enabled"] is True
        assert payload["scheduled"] is True
        assert "0 9 * * 1-5" in payload["message"]


# =============================================================================
# schedule_automation no-ops for disabled automations
# =============================================================================


class TestScheduleDisabledNoop:
    @pytest.mark.asyncio
    async def test_disabled_is_not_scheduled(self, mock_db):
        from app.db import database
        database.create_automation(
            automation_id="auto-noop",
            name="noop",
            trigger_type="interval",
            trigger_config={"seconds": 60},
            profile_id="default",
            prompt="hi",
            enabled=False,
        )
        manager = automation_manager
        scheduler = MagicMock()
        scheduler.get_jobs.return_value = []
        manager._scheduler = scheduler
        try:
            await manager.schedule_automation("auto-noop")
        finally:
            manager._scheduler = None
        scheduler.add_job.assert_not_called()

    @pytest.mark.asyncio
    async def test_enabled_is_scheduled(self, mock_db):
        from app.db import database
        database.create_automation(
            automation_id="auto-live",
            name="live",
            trigger_type="interval",
            trigger_config={"seconds": 60},
            profile_id="default",
            prompt="hi",
            enabled=True,
        )
        manager = automation_manager
        scheduler = MagicMock()
        scheduler.get_jobs.return_value = []
        manager._scheduler = scheduler
        try:
            await manager.schedule_automation("auto-live")
        finally:
            manager._scheduler = None
        scheduler.add_job.assert_called_once()
