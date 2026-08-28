"""
Proactive scheduler service for scheduling briefings and event reminders.

Uses APScheduler for background task scheduling with persistence to database.
"""

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from app.db import database
from app.db.async_database import run_sync

logger = logging.getLogger(__name__)


class ScheduleType(str, Enum):
    """Types of scheduled events."""
    DAILY_BRIEFING = "daily_briefing"
    EVENT_REMINDER = "event_reminder"
    CUSTOM = "custom"


@dataclass
class ScheduledTask:
    """A scheduled task."""
    id: str
    device_id: str
    schedule_type: ScheduleType
    trigger_time: Optional[datetime] = None  # For one-time events
    hour: Optional[int] = None  # For daily schedules (0-23)
    minute: Optional[int] = None  # For daily schedules (0-59)
    params: Dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_triggered_at: Optional[datetime] = None


class ProactiveScheduler:
    """
    Proactive scheduler for mobile device notifications.

    Manages scheduled briefings and event reminders using APScheduler.
    Falls back to a simple in-memory scheduler if APScheduler is not available.
    """

    def __init__(self):
        self._scheduler = None
        self._initialized = False
        self._use_apscheduler = False
        self._trigger_callbacks: List[Callable] = []
        self._schedules: Dict[str, ScheduledTask] = {}  # In-memory cache
        self._background_task: Optional[asyncio.Task] = None

    async def initialize(self) -> bool:
        """
        Initialize the scheduler.

        Returns:
            True if APScheduler is available, False if using fallback.
        """
        if self._initialized:
            return self._use_apscheduler

        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler

            self._scheduler = AsyncIOScheduler()
            self._scheduler.start()
            self._use_apscheduler = True
            logger.info("APScheduler initialized successfully")

        except ImportError:
            logger.info("APScheduler not available, using simple scheduler")
            self._use_apscheduler = False

        # Load existing schedules from database
        await self._load_schedules_from_db()

        # Start background checker for simple scheduler
        if not self._use_apscheduler:
            self._background_task = asyncio.create_task(self._run_simple_scheduler())

        self._initialized = True
        return self._use_apscheduler

    async def shutdown(self) -> None:
        """Shutdown the scheduler."""
        if self._scheduler:
            self._scheduler.shutdown()

        if self._background_task:
            self._background_task.cancel()
            try:
                await self._background_task
            except asyncio.CancelledError:
                pass

    def on_trigger(self, callback: Callable[[ScheduledTask], Any]) -> None:
        """
        Register a callback for when schedules trigger.

        Args:
            callback: Function to call with the triggered ScheduledTask
        """
        self._trigger_callbacks.append(callback)

    async def _notify_trigger(self, task: ScheduledTask) -> None:
        """Notify all registered callbacks of a triggered schedule."""
        for callback in self._trigger_callbacks:
            try:
                result = callback(task)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(f"Trigger callback error for {task.id}: {e}")

    async def schedule_briefing(
        self,
        device_id: str,
        hour: int,
        minute: int = 0,
        params: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Schedule a daily briefing at a specific time.

        Args:
            device_id: Device to send briefing to
            hour: Hour of day (0-23)
            minute: Minute of hour (0-59)
            params: Optional additional parameters

        Returns:
            Schedule ID
        """
        schedule_id = str(uuid.uuid4())

        task = ScheduledTask(
            id=schedule_id,
            device_id=device_id,
            schedule_type=ScheduleType.DAILY_BRIEFING,
            hour=hour,
            minute=minute,
            params=params or {},
        )

        await self._add_schedule(task)
        logger.debug(f"Scheduled daily briefing {schedule_id} for {hour:02d}:{minute:02d}")

        return schedule_id

    async def schedule_event_reminder(
        self,
        device_id: str,
        event_id: str,
        trigger_time: datetime,
        params: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Schedule a one-time event reminder.

        Args:
            device_id: Device to send reminder to
            event_id: ID of the event to remind about
            trigger_time: When to trigger the reminder
            params: Optional additional parameters

        Returns:
            Schedule ID
        """
        schedule_id = str(uuid.uuid4())

        task_params = params or {}
        task_params["event_id"] = event_id

        task = ScheduledTask(
            id=schedule_id,
            device_id=device_id,
            schedule_type=ScheduleType.EVENT_REMINDER,
            trigger_time=trigger_time,
            params=task_params,
        )

        await self._add_schedule(task)
        logger.debug(f"Scheduled reminder {schedule_id} for {trigger_time.isoformat()}")

        return schedule_id

    async def cancel_schedule(self, schedule_id: str) -> bool:
        """
        Cancel a scheduled task.

        Args:
            schedule_id: ID of schedule to cancel

        Returns:
            True if cancelled, False if not found
        """
        if schedule_id not in self._schedules:
            return False

        if self._use_apscheduler and self._scheduler:
            try:
                self._scheduler.remove_job(schedule_id)
            except (KeyError, LookupError):
                pass  # Job doesn't exist in APScheduler
            except Exception as e:
                logger.warning(f"Unexpected error removing APScheduler job {schedule_id}: {e}")

        del self._schedules[schedule_id]
        await self._delete_schedule_from_db(schedule_id)

        logger.debug(f"Cancelled schedule {schedule_id}")
        return True

    async def get_schedules(self, device_id: str) -> List[Dict[str, Any]]:
        """
        Get all schedules for a device.

        Args:
            device_id: Device ID to filter by

        Returns:
            List of schedule dictionaries
        """
        return [
            self._task_to_dict(task)
            for task in self._schedules.values()
            if task.device_id == device_id
        ]

    async def get_schedule(self, schedule_id: str) -> Optional[Dict[str, Any]]:
        """
        Get a specific schedule by ID.

        Args:
            schedule_id: Schedule ID

        Returns:
            Schedule dictionary or None if not found
        """
        task = self._schedules.get(schedule_id)
        return self._task_to_dict(task) if task else None

    def _task_to_dict(self, task: ScheduledTask) -> Dict[str, Any]:
        """Convert a ScheduledTask to a dictionary."""
        return {
            "id": task.id,
            "device_id": task.device_id,
            "schedule_type": task.schedule_type.value,
            "trigger_time": task.trigger_time.isoformat() if task.trigger_time else None,
            "hour": task.hour,
            "minute": task.minute,
            "params": task.params,
            "enabled": task.enabled,
            "created_at": task.created_at.isoformat(),
            "last_triggered_at": task.last_triggered_at.isoformat() if task.last_triggered_at else None,
        }

    async def _add_schedule(self, task: ScheduledTask) -> None:
        """Add a schedule to memory and database."""
        self._schedules[task.id] = task
        await self._save_schedule_to_db(task)

        if self._use_apscheduler and self._scheduler:
            self._add_apscheduler_job(task)

    def _add_apscheduler_job(self, task: ScheduledTask) -> None:
        """Add a job to APScheduler."""
        try:
            from apscheduler.triggers.cron import CronTrigger
            from apscheduler.triggers.date import DateTrigger

            if task.schedule_type == ScheduleType.DAILY_BRIEFING:
                trigger = CronTrigger(hour=task.hour, minute=task.minute)
            elif task.trigger_time:
                trigger = DateTrigger(run_date=task.trigger_time)
            else:
                return

            self._scheduler.add_job(
                self._execute_trigger,
                trigger,
                id=task.id,
                args=[task.id],
                replace_existing=True,
            )

        except Exception as e:
            logger.error(f"Failed to add APScheduler job: {e}")

    async def _execute_trigger(self, schedule_id: str) -> None:
        """Execute a triggered schedule."""
        task = self._schedules.get(schedule_id)
        if not task or not task.enabled:
            return

        task.last_triggered_at = datetime.now(timezone.utc)
        await self._save_schedule_to_db(task)

        logger.debug(f"Triggered schedule {schedule_id}")
        await self._notify_trigger(task)

        # Remove one-time schedules after triggering
        if task.schedule_type == ScheduleType.EVENT_REMINDER:
            await self.cancel_schedule(schedule_id)

    async def _run_simple_scheduler(self) -> None:
        """Simple scheduler loop for when APScheduler is not available."""
        logger.info("Starting simple scheduler loop")

        while True:
            try:
                await asyncio.sleep(60)  # Check every minute
                await self._check_schedules()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Simple scheduler error: {e}")

    async def _check_schedules(self) -> None:
        """Check and trigger due schedules."""
        now = datetime.now(timezone.utc)

        for task in list(self._schedules.values()):
            if not task.enabled:
                continue

            should_trigger = False

            if task.schedule_type == ScheduleType.DAILY_BRIEFING:
                # Check if current time matches schedule
                if task.hour == now.hour and task.minute == now.minute:
                    # Avoid duplicate triggers within same minute
                    if task.last_triggered_at:
                        time_since = now - task.last_triggered_at
                        if time_since.total_seconds() < 120:
                            continue
                    should_trigger = True

            elif task.trigger_time and task.trigger_time <= now:
                should_trigger = True

            if should_trigger:
                await self._execute_trigger(task.id)

    async def _load_schedules_from_db(self) -> None:
        """Load schedules from database."""
        try:
            # Only the query is offloaded: the loop below calls
            # _add_apscheduler_job, which registers jobs against the
            # AsyncIOScheduler bound to THIS loop, so it must not move to a
            # worker thread.
            schedules = await run_sync(database.get_mobile_schedules)
            for row in schedules:
                task = ScheduledTask(
                    id=row["id"],
                    device_id=row["device_id"],
                    schedule_type=ScheduleType(row["schedule_type"]),
                    trigger_time=datetime.fromisoformat(row["trigger_time"]) if row.get("trigger_time") else None,
                    hour=row.get("hour"),
                    minute=row.get("minute"),
                    params=json.loads(row.get("params", "{}")),
                    enabled=bool(row.get("enabled", True)),
                    created_at=datetime.fromisoformat(row["created_at"]) if row.get("created_at") else datetime.now(timezone.utc),
                    last_triggered_at=datetime.fromisoformat(row["last_triggered_at"]) if row.get("last_triggered_at") else None,
                )
                self._schedules[task.id] = task

                if self._use_apscheduler:
                    self._add_apscheduler_job(task)

            logger.debug(f"Loaded {len(self._schedules)} schedules from database")

        except Exception as e:
            logger.warning(f"Could not load schedules from database: {e}")

    async def _save_schedule_to_db(self, task: ScheduledTask) -> None:
        """Save a schedule to database."""
        try:
            await run_sync(
                database.save_mobile_schedule,
                schedule_id=task.id,
                device_id=task.device_id,
                schedule_type=task.schedule_type.value,
                trigger_time=task.trigger_time.isoformat() if task.trigger_time else None,
                hour=task.hour,
                minute=task.minute,
                params=json.dumps(task.params),
                enabled=task.enabled,
                last_triggered_at=task.last_triggered_at.isoformat() if task.last_triggered_at else None,
            )
        except Exception as e:
            logger.warning(f"Could not save schedule to database: {e}")

    async def _delete_schedule_from_db(self, schedule_id: str) -> None:
        """Delete a schedule from database."""
        try:
            await run_sync(database.delete_mobile_schedule, schedule_id)
        except Exception as e:
            logger.warning(f"Could not delete schedule from database: {e}")


# Global scheduler instance
proactive_scheduler = ProactiveScheduler()
