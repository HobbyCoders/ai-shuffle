"""
Unit tests for proactive scheduler service.

Tests cover:
- Scheduler initialization (with and without APScheduler)
- Daily briefing scheduling
- Event reminder scheduling
- Schedule cancellation
- Trigger callbacks
- Database persistence
- Simple scheduler fallback
"""

import pytest
import asyncio
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock, AsyncMock

from app.services.proactive_scheduler import (
    ProactiveScheduler,
    ScheduledTask,
    ScheduleType,
    proactive_scheduler,
)


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def scheduler():
    """Create a fresh scheduler instance."""
    return ProactiveScheduler()


@pytest.fixture
def mock_database():
    """Mock database module."""
    with patch("app.services.proactive_scheduler.database") as mock_db:
        mock_db.get_mobile_schedules.return_value = []
        mock_db.save_mobile_schedule.return_value = None
        mock_db.delete_mobile_schedule.return_value = None
        yield mock_db


# =============================================================================
# ScheduleType Tests
# =============================================================================

class TestScheduleType:
    """Test ScheduleType enum."""

    def test_schedule_type_values(self):
        """ScheduleType should have expected values."""
        assert ScheduleType.DAILY_BRIEFING.value == "daily_briefing"
        assert ScheduleType.EVENT_REMINDER.value == "event_reminder"
        assert ScheduleType.CUSTOM.value == "custom"

    def test_schedule_type_is_string_enum(self):
        """ScheduleType should be string-based."""
        assert isinstance(ScheduleType.DAILY_BRIEFING.value, str)


# =============================================================================
# ScheduledTask Tests
# =============================================================================

class TestScheduledTask:
    """Test ScheduledTask dataclass."""

    def test_create_daily_briefing_task(self):
        """Should create daily briefing task."""
        task = ScheduledTask(
            id="task-1",
            device_id="device-1",
            schedule_type=ScheduleType.DAILY_BRIEFING,
            hour=9,
            minute=0,
        )
        assert task.id == "task-1"
        assert task.device_id == "device-1"
        assert task.schedule_type == ScheduleType.DAILY_BRIEFING
        assert task.hour == 9
        assert task.minute == 0
        assert task.enabled is True

    def test_create_event_reminder_task(self):
        """Should create event reminder task."""
        trigger_time = datetime.utcnow() + timedelta(hours=1)
        task = ScheduledTask(
            id="task-2",
            device_id="device-1",
            schedule_type=ScheduleType.EVENT_REMINDER,
            trigger_time=trigger_time,
            params={"event_id": "event-123"}
        )
        assert task.id == "task-2"
        assert task.schedule_type == ScheduleType.EVENT_REMINDER
        assert task.trigger_time == trigger_time
        assert task.params["event_id"] == "event-123"

    def test_task_default_values(self):
        """Task should have correct defaults."""
        task = ScheduledTask(
            id="task-3",
            device_id="device-1",
            schedule_type=ScheduleType.CUSTOM,
        )
        assert task.trigger_time is None
        assert task.hour is None
        assert task.minute is None
        assert task.params == {}
        assert task.enabled is True
        assert task.last_triggered_at is None


# =============================================================================
# Scheduler Initialization Tests
# =============================================================================

class TestSchedulerInitialization:
    """Test scheduler initialization."""

    @pytest.mark.asyncio
    async def test_initialize_without_apscheduler(self, scheduler, mock_database):
        """Should initialize with simple scheduler when APScheduler unavailable."""
        with patch.dict("sys.modules", {"apscheduler": None}):
            with patch("app.services.proactive_scheduler.asyncio.create_task") as mock_task:
                mock_task.return_value = MagicMock()
                result = await scheduler.initialize()

        # May or may not use APScheduler depending on environment
        assert scheduler._initialized is True

    @pytest.mark.asyncio
    async def test_initialize_idempotent(self, scheduler, mock_database):
        """Initialize should be idempotent."""
        with patch("app.services.proactive_scheduler.asyncio.create_task") as mock_task:
            mock_task.return_value = MagicMock()
            await scheduler.initialize()
            first_state = scheduler._initialized

            await scheduler.initialize()
            second_state = scheduler._initialized

        assert first_state == second_state

    @pytest.mark.asyncio
    async def test_shutdown(self, scheduler, mock_database):
        """Should shutdown cleanly when no background task."""
        scheduler._background_task = None
        scheduler._scheduler = None

        # Should not raise
        await scheduler.shutdown()

    @pytest.mark.asyncio
    async def test_shutdown_with_apscheduler(self, scheduler, mock_database):
        """Should shutdown APScheduler if present."""
        mock_scheduler = MagicMock()
        scheduler._scheduler = mock_scheduler
        scheduler._background_task = None

        await scheduler.shutdown()

        mock_scheduler.shutdown.assert_called_once()


# =============================================================================
# Schedule Briefing Tests
# =============================================================================

class TestScheduleBriefing:
    """Test daily briefing scheduling."""

    @pytest.mark.asyncio
    async def test_schedule_briefing_creates_task(self, scheduler, mock_database):
        """Should create briefing task."""
        with patch("app.services.proactive_scheduler.asyncio.create_task"):
            await scheduler.initialize()

        schedule_id = await scheduler.schedule_briefing(
            device_id="device-1",
            hour=9,
            minute=30
        )

        assert schedule_id is not None
        assert len(schedule_id) > 0
        assert schedule_id in scheduler._schedules

        task = scheduler._schedules[schedule_id]
        assert task.device_id == "device-1"
        assert task.schedule_type == ScheduleType.DAILY_BRIEFING
        assert task.hour == 9
        assert task.minute == 30

    @pytest.mark.asyncio
    async def test_schedule_briefing_with_params(self, scheduler, mock_database):
        """Should accept additional params."""
        with patch("app.services.proactive_scheduler.asyncio.create_task"):
            await scheduler.initialize()

        schedule_id = await scheduler.schedule_briefing(
            device_id="device-1",
            hour=8,
            minute=0,
            params={"timezone": "America/New_York"}
        )

        task = scheduler._schedules[schedule_id]
        assert task.params["timezone"] == "America/New_York"


# =============================================================================
# Schedule Event Reminder Tests
# =============================================================================

class TestScheduleEventReminder:
    """Test event reminder scheduling."""

    @pytest.mark.asyncio
    async def test_schedule_event_reminder_creates_task(self, scheduler, mock_database):
        """Should create event reminder task."""
        with patch("app.services.proactive_scheduler.asyncio.create_task"):
            await scheduler.initialize()

        trigger_time = datetime.utcnow() + timedelta(hours=2)
        schedule_id = await scheduler.schedule_event_reminder(
            device_id="device-1",
            event_id="event-123",
            trigger_time=trigger_time
        )

        assert schedule_id in scheduler._schedules

        task = scheduler._schedules[schedule_id]
        assert task.schedule_type == ScheduleType.EVENT_REMINDER
        assert task.trigger_time == trigger_time
        assert task.params["event_id"] == "event-123"

    @pytest.mark.asyncio
    async def test_schedule_event_reminder_with_params(self, scheduler, mock_database):
        """Should merge params with event_id."""
        with patch("app.services.proactive_scheduler.asyncio.create_task"):
            await scheduler.initialize()

        trigger_time = datetime.utcnow() + timedelta(hours=1)
        schedule_id = await scheduler.schedule_event_reminder(
            device_id="device-1",
            event_id="event-456",
            trigger_time=trigger_time,
            params={"reminder_type": "15min_before"}
        )

        task = scheduler._schedules[schedule_id]
        assert task.params["event_id"] == "event-456"
        assert task.params["reminder_type"] == "15min_before"


# =============================================================================
# Cancel Schedule Tests
# =============================================================================

class TestCancelSchedule:
    """Test schedule cancellation."""

    @pytest.mark.asyncio
    async def test_cancel_existing_schedule(self, scheduler, mock_database):
        """Should cancel existing schedule."""
        with patch("app.services.proactive_scheduler.asyncio.create_task"):
            await scheduler.initialize()

        schedule_id = await scheduler.schedule_briefing(
            device_id="device-1",
            hour=9,
            minute=0
        )

        result = await scheduler.cancel_schedule(schedule_id)

        assert result is True
        assert schedule_id not in scheduler._schedules
        mock_database.delete_mobile_schedule.assert_called_once_with(schedule_id)

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_schedule(self, scheduler, mock_database):
        """Should return False for non-existent schedule."""
        with patch("app.services.proactive_scheduler.asyncio.create_task"):
            await scheduler.initialize()

        result = await scheduler.cancel_schedule("nonexistent-id")

        assert result is False


# =============================================================================
# Get Schedules Tests
# =============================================================================

class TestGetSchedules:
    """Test schedule retrieval."""

    @pytest.mark.asyncio
    async def test_get_schedules_for_device(self, scheduler, mock_database):
        """Should return schedules for specific device."""
        with patch("app.services.proactive_scheduler.asyncio.create_task"):
            await scheduler.initialize()

        await scheduler.schedule_briefing("device-1", 9, 0)
        await scheduler.schedule_briefing("device-1", 18, 0)
        await scheduler.schedule_briefing("device-2", 10, 0)

        device1_schedules = await scheduler.get_schedules("device-1")
        device2_schedules = await scheduler.get_schedules("device-2")

        assert len(device1_schedules) == 2
        assert len(device2_schedules) == 1

    @pytest.mark.asyncio
    async def test_get_schedule_by_id(self, scheduler, mock_database):
        """Should return specific schedule by ID."""
        with patch("app.services.proactive_scheduler.asyncio.create_task"):
            await scheduler.initialize()

        schedule_id = await scheduler.schedule_briefing("device-1", 9, 0)

        schedule = await scheduler.get_schedule(schedule_id)

        assert schedule is not None
        assert schedule["id"] == schedule_id
        assert schedule["device_id"] == "device-1"

    @pytest.mark.asyncio
    async def test_get_nonexistent_schedule(self, scheduler, mock_database):
        """Should return None for non-existent schedule."""
        with patch("app.services.proactive_scheduler.asyncio.create_task"):
            await scheduler.initialize()

        schedule = await scheduler.get_schedule("nonexistent-id")

        assert schedule is None


# =============================================================================
# Trigger Callback Tests
# =============================================================================

class TestTriggerCallbacks:
    """Test trigger callback functionality."""

    @pytest.mark.asyncio
    async def test_register_callback(self, scheduler, mock_database):
        """Should register trigger callback."""
        callback = MagicMock()
        scheduler.on_trigger(callback)

        assert callback in scheduler._trigger_callbacks

    @pytest.mark.asyncio
    async def test_notify_trigger_calls_callbacks(self, scheduler, mock_database):
        """Should call all registered callbacks on trigger."""
        callback1 = MagicMock()
        callback2 = MagicMock()

        scheduler.on_trigger(callback1)
        scheduler.on_trigger(callback2)

        task = ScheduledTask(
            id="task-1",
            device_id="device-1",
            schedule_type=ScheduleType.DAILY_BRIEFING,
        )

        await scheduler._notify_trigger(task)

        callback1.assert_called_once_with(task)
        callback2.assert_called_once_with(task)

    @pytest.mark.asyncio
    async def test_notify_trigger_handles_async_callbacks(self, scheduler, mock_database):
        """Should handle async callbacks."""
        async_callback = AsyncMock()
        scheduler.on_trigger(async_callback)

        task = ScheduledTask(
            id="task-1",
            device_id="device-1",
            schedule_type=ScheduleType.DAILY_BRIEFING,
        )

        await scheduler._notify_trigger(task)

        async_callback.assert_called_once_with(task)

    @pytest.mark.asyncio
    async def test_notify_trigger_handles_callback_errors(self, scheduler, mock_database):
        """Should handle callback errors gracefully."""
        def failing_callback(task):
            raise ValueError("Callback failed")

        successful_callback = MagicMock()

        scheduler.on_trigger(failing_callback)
        scheduler.on_trigger(successful_callback)

        task = ScheduledTask(
            id="task-1",
            device_id="device-1",
            schedule_type=ScheduleType.DAILY_BRIEFING,
        )

        # Should not raise, should continue to other callbacks
        await scheduler._notify_trigger(task)

        successful_callback.assert_called_once_with(task)


# =============================================================================
# Task to Dict Tests
# =============================================================================

class TestTaskToDict:
    """Test task serialization."""

    def test_task_to_dict_daily_briefing(self, scheduler):
        """Should serialize daily briefing task."""
        task = ScheduledTask(
            id="task-1",
            device_id="device-1",
            schedule_type=ScheduleType.DAILY_BRIEFING,
            hour=9,
            minute=30,
            params={"key": "value"},
        )

        result = scheduler._task_to_dict(task)

        assert result["id"] == "task-1"
        assert result["device_id"] == "device-1"
        assert result["schedule_type"] == "daily_briefing"
        assert result["hour"] == 9
        assert result["minute"] == 30
        assert result["params"] == {"key": "value"}
        assert result["enabled"] is True

    def test_task_to_dict_event_reminder(self, scheduler):
        """Should serialize event reminder task."""
        trigger_time = datetime(2026, 1, 15, 14, 30, 0)
        task = ScheduledTask(
            id="task-2",
            device_id="device-1",
            schedule_type=ScheduleType.EVENT_REMINDER,
            trigger_time=trigger_time,
        )

        result = scheduler._task_to_dict(task)

        assert result["schedule_type"] == "event_reminder"
        assert result["trigger_time"] == "2026-01-15T14:30:00"


# =============================================================================
# Database Persistence Tests
# =============================================================================

class TestDatabasePersistence:
    """Test database persistence."""

    @pytest.mark.asyncio
    async def test_save_schedule_to_db(self, scheduler, mock_database):
        """Should save schedule to database."""
        task = ScheduledTask(
            id="task-1",
            device_id="device-1",
            schedule_type=ScheduleType.DAILY_BRIEFING,
            hour=9,
            minute=0,
        )

        await scheduler._save_schedule_to_db(task)

        mock_database.save_mobile_schedule.assert_called_once()
        call_kwargs = mock_database.save_mobile_schedule.call_args.kwargs
        assert call_kwargs["schedule_id"] == "task-1"
        assert call_kwargs["device_id"] == "device-1"

    @pytest.mark.asyncio
    async def test_delete_schedule_from_db(self, scheduler, mock_database):
        """Should delete schedule from database."""
        await scheduler._delete_schedule_from_db("task-1")

        mock_database.delete_mobile_schedule.assert_called_once_with("task-1")

    @pytest.mark.asyncio
    async def test_load_schedules_from_db(self, scheduler, mock_database):
        """Should load schedules from database."""
        mock_database.get_mobile_schedules.return_value = [
            {
                "id": "task-1",
                "device_id": "device-1",
                "schedule_type": "daily_briefing",
                "trigger_time": None,
                "hour": 9,
                "minute": 0,
                "params": "{}",
                "enabled": True,
                "created_at": datetime.utcnow().isoformat(),
                "last_triggered_at": None,
            }
        ]

        await scheduler._load_schedules_from_db()

        assert "task-1" in scheduler._schedules
        task = scheduler._schedules["task-1"]
        assert task.device_id == "device-1"
        assert task.hour == 9


# =============================================================================
# Global Instance Tests
# =============================================================================

class TestGlobalInstance:
    """Test global scheduler instance."""

    def test_global_instance_exists(self):
        """Global scheduler instance should exist."""
        assert proactive_scheduler is not None
        assert isinstance(proactive_scheduler, ProactiveScheduler)


# =============================================================================
# Module Import Tests
# =============================================================================

class TestModuleImports:
    """Test module imports."""

    def test_scheduler_module_imports(self):
        """Scheduler module should import without errors."""
        from app.services import proactive_scheduler as ps_module
        assert ps_module is not None

    def test_schedule_type_importable(self):
        """ScheduleType should be importable."""
        from app.services.proactive_scheduler import ScheduleType
        assert ScheduleType is not None

    def test_scheduled_task_importable(self):
        """ScheduledTask should be importable."""
        from app.services.proactive_scheduler import ScheduledTask
        assert ScheduledTask is not None
