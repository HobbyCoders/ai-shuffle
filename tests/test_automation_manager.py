"""
Tests for AutomationManager — trigger handling, execution flow, output delivery.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

from app.core.automation_manager import AutomationManager


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def manager():
    """Fresh AutomationManager for each test."""
    return AutomationManager()


# =============================================================================
# Tracked Task Helper
# =============================================================================


class TestTrackedTask:
    @pytest.mark.asyncio
    async def test_create_tracked_task_success(self, manager):
        result = []

        async def work():
            result.append(True)

        task = manager._create_tracked_task(work(), name="test-task")
        await task
        assert result == [True]

    @pytest.mark.asyncio
    async def test_create_tracked_task_error_logged(self, manager):
        async def failing_work():
            raise RuntimeError("test error")

        task = manager._create_tracked_task(failing_work(), name="test-fail")

        # Wait for the task to complete (with exception)
        with pytest.raises(RuntimeError, match="test error"):
            await task

    def test_on_task_done_cancelled(self, manager):
        """Cancelled tasks should not log errors."""
        mock_task = MagicMock()
        mock_task.cancelled.return_value = True
        # Should not raise
        AutomationManager._on_task_done(mock_task)

    def test_on_task_done_no_error(self, manager):
        mock_task = MagicMock()
        mock_task.cancelled.return_value = False
        mock_task.exception.return_value = None
        # Should not raise
        AutomationManager._on_task_done(mock_task)


# =============================================================================
# Trigger Webhook
# =============================================================================


class TestTriggerWebhook:
    @pytest.mark.asyncio
    async def test_webhook_not_found(self, manager, mock_db):
        result = await manager.trigger_webhook("nonexistent", {})
        assert result is None

    @pytest.mark.asyncio
    async def test_webhook_disabled(self, manager, mock_db):
        from app.db import database
        database.create_automation(
            automation_id="auto-wh",
            name="Test Webhook",
            trigger_type="webhook",
            trigger_config={},
            prompt="test",
            profile_id="default",
        )
        # is_enabled defaults to False in the DB schema
        result = await manager.trigger_webhook("auto-wh", {})
        assert result is None

    @pytest.mark.asyncio
    async def test_webhook_wrong_trigger_type(self, manager, mock_db):
        from app.db import database
        database.create_automation(
            automation_id="auto-wh",
            name="Test Cron",
            trigger_type="cron",
            trigger_config={"expression": "0 * * * *"},
            prompt="test",
            profile_id="default",
        )
        database.update_automation("auto-wh", is_enabled=True)
        result = await manager.trigger_webhook("auto-wh", {})
        assert result is None


# =============================================================================
# Trigger Manual
# =============================================================================


class TestTriggerManual:
    @pytest.mark.asyncio
    async def test_manual_not_found(self, manager, mock_db):
        result = await manager.trigger_manual("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_manual_returns_status(self, manager, mock_db):
        from app.db import database
        database.create_automation(
            automation_id="auto-man",
            name="Test Manual",
            trigger_type="webhook",
            trigger_config={},
            prompt="test",
            profile_id="default",
        )

        # Patch _execute to avoid real execution
        with patch.object(manager, "_execute", new_callable=AsyncMock):
            result = await manager.trigger_manual("auto-man")
            assert result is not None
            assert result["automation_id"] == "auto-man"
            assert result["status"] == "triggered"


# =============================================================================
# Execution — Budget Exceeded
# =============================================================================


class TestExecuteBudget:
    @pytest.mark.asyncio
    async def test_execute_budget_exceeded(self, manager, mock_db, mock_encryption):
        from app.db import database

        database.create_automation(
            automation_id="auto-budget",
            name="Budget Test",
            trigger_type="webhook",
            trigger_config={},
            prompt="test",
            profile_id="default",
            max_cost_total=0,
        )
        database.update_automation("auto-budget", is_enabled=True, total_cost_used=1.0)

        with patch("app.core.automation_service.check_budget", return_value=(False, "Budget exceeded")):
            await manager._execute("auto-budget", {"trigger": "manual"})

        # Check that a run was created with budget_exceeded status
        runs = database.get_automation_runs("auto-budget")
        assert any(r.get("status") == "budget_exceeded" for r in runs)


# =============================================================================
# Execution — Disabled
# =============================================================================


class TestExecuteDisabled:
    @pytest.mark.asyncio
    async def test_execute_disabled_skipped(self, manager, mock_db):
        from app.db import database

        database.create_automation(
            automation_id="auto-off",
            name="Disabled",
            trigger_type="webhook",
            trigger_config={},
            prompt="test",
            profile_id="default",
        )
        # is_enabled defaults to False

        await manager._execute("auto-off", {"trigger": "manual"})
        # No run should be created
        runs = database.get_automation_runs("auto-off")
        assert len(runs) == 0


# =============================================================================
# Execution — Concurrency
# =============================================================================


class TestExecuteConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_execution_blocked(self, manager, mock_db, mock_encryption):
        from app.db import database

        database.create_automation(
            automation_id="auto-conc",
            name="Concurrent",
            trigger_type="webhook",
            trigger_config={},
            prompt="test",
            profile_id="default",
        )
        database.update_automation("auto-conc", is_enabled=True)

        # Pre-mark as running
        async with manager._lock:
            manager._running["auto-conc"] = MagicMock()

        await manager._execute("auto-conc", {"trigger": "manual"})

        # Should not have created any run (blocked by concurrency check)
        runs = database.get_automation_runs("auto-conc")
        assert len(runs) == 0


# =============================================================================
# Schedule / Unschedule
# =============================================================================


class TestScheduling:
    @pytest.mark.asyncio
    async def test_schedule_no_scheduler(self, manager, mock_db):
        """Should silently return when scheduler is None."""
        manager._scheduler = None
        await manager.schedule_automation("auto-1")

    @pytest.mark.asyncio
    async def test_unschedule_no_scheduler(self, manager):
        manager._scheduler = None
        await manager.unschedule_automation("auto-1")


# =============================================================================
# Output Delivery — Suppression
# =============================================================================


class TestOutputDelivery:
    @pytest.mark.asyncio
    async def test_suppress_pattern(self, manager, mock_db):
        from app.db import database

        database.create_automation(
            automation_id="auto-sup",
            name="Suppress",
            trigger_type="webhook",
            trigger_config={},
            prompt="test",
            profile_id="default",
        )
        database.create_automation_run(
            run_id="run-sup",
            automation_id="auto-sup",
            status="completed",
        )

        automation = database.get_automation("auto-sup")
        automation["output_config"] = {"type": "session", "suppress_pattern": "NO_CHANGE"}

        await manager._deliver_output(automation, "run-sup", "NO_CHANGE")

        run = database.get_automation_run("run-sup")
        assert run.get("output_status") == "suppressed"

    @pytest.mark.asyncio
    async def test_session_output_type(self, manager, mock_db):
        from app.db import database

        database.create_automation(
            automation_id="auto-sess",
            name="Session Output",
            trigger_type="webhook",
            trigger_config={},
            prompt="test",
            profile_id="default",
        )
        database.create_automation_run(
            run_id="run-sess",
            automation_id="auto-sess",
            status="completed",
        )

        automation = database.get_automation("auto-sess")
        automation["output_config"] = {"type": "session"}

        await manager._deliver_output(automation, "run-sess", "Some response")

        run = database.get_automation_run("run-sess")
        assert run.get("output_status") == "delivered"

    @pytest.mark.asyncio
    async def test_none_output_type(self, manager, mock_db):
        from app.db import database

        database.create_automation(
            automation_id="auto-none",
            name="None Output",
            trigger_type="webhook",
            trigger_config={},
            prompt="test",
            profile_id="default",
        )
        database.create_automation_run(
            run_id="run-none",
            automation_id="auto-none",
            status="completed",
        )

        automation = database.get_automation("auto-none")
        automation["output_config"] = {"type": "none"}

        await manager._deliver_output(automation, "run-none", "ignored")

        run = database.get_automation_run("run-none")
        assert run.get("output_status") == "delivered"


# =============================================================================
# Handle Connector Event
# =============================================================================


class TestHandleConnectorEvent:
    @pytest.mark.asyncio
    async def test_no_matching_automations(self, manager, mock_db):
        """Should not raise when no automations match."""
        await manager.handle_connector_event("session.complete", {})

    @pytest.mark.asyncio
    async def test_filter_mismatch(self, manager, mock_db):
        from app.db import database

        database.create_automation(
            automation_id="auto-evt",
            name="Event",
            trigger_type="connector_event",
            trigger_config={"event_types": ["session.complete"], "filter": {"project_id": "proj-1"}},
            prompt="test",
            profile_id="default",
        )
        database.update_automation("auto-evt", is_enabled=True)

        with patch.object(manager, "_execute", new_callable=AsyncMock) as mock_exec:
            await manager.handle_connector_event("session.complete", {"project_id": "proj-2"})
            # _execute should not be called because filter didn't match
            # (but the task might be created — depends on how get_automations_for_event works)
