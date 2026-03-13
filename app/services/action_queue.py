"""
Action queue service for managing pending actions requiring user confirmation.

Handles queuing actions, processing confirmations, and expiring stale actions.
"""

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from app.db import database

logger = logging.getLogger(__name__)


# Default action expiry time in seconds
DEFAULT_EXPIRY_SECONDS = 300  # 5 minutes


class ActionStatus(str, Enum):
    """Status of a queued action."""
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    COMPLETED = "completed"
    FAILED = "failed"


class SecurityLevel(str, Enum):
    """Security level for action approval."""
    LOW = "low"  # Auto-approve after delay
    MEDIUM = "medium"  # Requires user confirmation
    HIGH = "high"  # Requires biometric/PIN verification
    CRITICAL = "critical"  # Requires multi-factor confirmation


@dataclass
class QueuedAction:
    """A pending action in the queue."""
    id: str
    device_id: str
    action_type: str
    params: Dict[str, Any]
    security_level: SecurityLevel
    status: ActionStatus = ActionStatus.PENDING
    executor_result: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc) + timedelta(seconds=DEFAULT_EXPIRY_SECONDS))
    confirmed_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class ActionQueue:
    """
    Queue for actions requiring user confirmation on mobile devices.

    Handles action lifecycle from creation through confirmation/rejection to completion.
    Automatically expires old actions.
    """

    def __init__(self, expiry_seconds: int = DEFAULT_EXPIRY_SECONDS):
        self._actions: Dict[str, QueuedAction] = {}
        self._expiry_seconds = expiry_seconds
        self._initialized = False
        self._cleanup_task: Optional[asyncio.Task] = None
        self._confirmation_callbacks: List[Callable] = []
        self._completion_callbacks: List[Callable] = []

    async def initialize(self) -> None:
        """Initialize the action queue and start cleanup task."""
        if self._initialized:
            return

        # Load pending actions from database
        await self._load_actions_from_db()

        # Start background cleanup task
        self._cleanup_task = asyncio.create_task(self._run_cleanup_loop())

        self._initialized = True
        logger.info("Action queue initialized")

    async def shutdown(self) -> None:
        """Shutdown the action queue."""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

    def on_confirmation(self, callback: Callable[[QueuedAction], Any]) -> None:
        """Register callback for when an action is confirmed/rejected."""
        self._confirmation_callbacks.append(callback)

    def on_completion(self, callback: Callable[[QueuedAction], Any]) -> None:
        """Register callback for when an action completes."""
        self._completion_callbacks.append(callback)

    async def _notify_confirmation(self, action: QueuedAction) -> None:
        """Notify callbacks of action confirmation."""
        for callback in self._confirmation_callbacks:
            try:
                result = callback(action)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(f"Confirmation callback error: {e}")

    async def _notify_completion(self, action: QueuedAction) -> None:
        """Notify callbacks of action completion."""
        for callback in self._completion_callbacks:
            try:
                result = callback(action)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(f"Completion callback error: {e}")

    async def queue_action(
        self,
        device_id: str,
        action_type: str,
        params: Dict[str, Any],
        security_level: SecurityLevel = SecurityLevel.MEDIUM,
        expiry_seconds: Optional[int] = None,
    ) -> str:
        """
        Queue an action for user confirmation.

        Args:
            device_id: Device to request confirmation from
            action_type: Type of action (e.g., "file_write", "command_exec")
            params: Action parameters
            security_level: Required security level for approval
            expiry_seconds: Custom expiry time (default: 5 minutes)

        Returns:
            Action ID
        """
        if not self._initialized:
            await self.initialize()

        action_id = str(uuid.uuid4())
        expiry = expiry_seconds or self._expiry_seconds

        action = QueuedAction(
            id=action_id,
            device_id=device_id,
            action_type=action_type,
            params=params,
            security_level=security_level,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=expiry),
        )

        self._actions[action_id] = action
        await self._save_action_to_db(action)

        logger.debug(f"Queued action {action_id}: {action_type} for device {device_id}")

        return action_id

    async def get_pending(self, device_id: str) -> List[Dict[str, Any]]:
        """
        Get all pending actions for a device.

        Args:
            device_id: Device ID to filter by

        Returns:
            List of pending action dicts
        """
        if not self._initialized:
            await self.initialize()

        # First expire any stale actions
        await self.expire_old_actions()

        pending = [
            self._action_to_dict(a)
            for a in self._actions.values()
            if a.device_id == device_id and a.status == ActionStatus.PENDING
        ]

        return pending

    async def get_action(self, action_id: str) -> Optional[Dict[str, Any]]:
        """
        Get a specific action by ID.

        Args:
            action_id: Action ID

        Returns:
            Action dict or None if not found
        """
        if not self._initialized:
            await self.initialize()

        action = self._actions.get(action_id)
        return self._action_to_dict(action) if action else None

    async def confirm_action(
        self,
        action_id: str,
        approved: bool,
        executor_result: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Confirm or reject a pending action.

        Args:
            action_id: Action ID
            approved: True to approve, False to reject
            executor_result: Optional result from action execution

        Returns:
            Updated action dict or None if not found
        """
        if not self._initialized:
            await self.initialize()

        action = self._actions.get(action_id)
        if not action:
            logger.warning(f"Action not found: {action_id}")
            return None

        if action.status != ActionStatus.PENDING:
            logger.warning(f"Action {action_id} is not pending (status: {action.status})")
            return self._action_to_dict(action)

        # Check if expired
        if datetime.now(timezone.utc) > action.expires_at:
            action.status = ActionStatus.EXPIRED
            await self._save_action_to_db(action)
            logger.debug(f"Action {action_id} was already expired")
            return self._action_to_dict(action)

        action.confirmed_at = datetime.now(timezone.utc)

        if approved:
            action.status = ActionStatus.APPROVED
            action.executor_result = executor_result
            logger.debug(f"Action {action_id} approved")
        else:
            action.status = ActionStatus.REJECTED
            logger.debug(f"Action {action_id} rejected")

        await self._save_action_to_db(action)
        await self._notify_confirmation(action)

        return self._action_to_dict(action)

    async def complete_action(
        self,
        action_id: str,
        success: bool,
        result: Optional[str] = None,
        error: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Mark an approved action as completed.

        Args:
            action_id: Action ID
            success: Whether execution succeeded
            result: Execution result
            error: Error message if failed

        Returns:
            Updated action dict or None if not found
        """
        if not self._initialized:
            await self.initialize()

        action = self._actions.get(action_id)
        if not action:
            return None

        if action.status != ActionStatus.APPROVED:
            logger.warning(f"Cannot complete action {action_id} with status {action.status}")
            return self._action_to_dict(action)

        action.completed_at = datetime.now(timezone.utc)

        if success:
            action.status = ActionStatus.COMPLETED
            action.executor_result = result
        else:
            action.status = ActionStatus.FAILED
            action.error_message = error

        await self._save_action_to_db(action)
        await self._notify_completion(action)

        logger.debug(f"Action {action_id} completed (success={success})")

        return self._action_to_dict(action)

    async def expire_old_actions(self) -> int:
        """
        Expire actions past their expiry time.

        Returns:
            Number of actions expired
        """
        if not self._initialized:
            await self.initialize()

        now = datetime.now(timezone.utc)
        expired_count = 0

        for action in list(self._actions.values()):
            if action.status == ActionStatus.PENDING and now > action.expires_at:
                action.status = ActionStatus.EXPIRED
                await self._save_action_to_db(action)
                expired_count += 1
                logger.debug(f"Expired action {action.id}")

        if expired_count > 0:
            logger.debug(f"Expired {expired_count} old actions")

        return expired_count

    async def cancel_action(self, action_id: str) -> bool:
        """
        Cancel a pending action.

        Args:
            action_id: Action ID

        Returns:
            True if cancelled, False if not found or not pending
        """
        if not self._initialized:
            await self.initialize()

        action = self._actions.get(action_id)
        if not action or action.status != ActionStatus.PENDING:
            return False

        action.status = ActionStatus.EXPIRED  # Use EXPIRED for cancellation
        await self._save_action_to_db(action)

        logger.debug(f"Cancelled action {action_id}")
        return True

    async def cleanup_completed_actions(self, older_than_hours: int = 24) -> int:
        """
        Remove completed/expired actions older than specified hours.

        Args:
            older_than_hours: Remove actions older than this

        Returns:
            Number of actions removed
        """
        if not self._initialized:
            await self.initialize()

        cutoff = datetime.now(timezone.utc) - timedelta(hours=older_than_hours)
        removed_count = 0

        terminal_statuses = {
            ActionStatus.COMPLETED,
            ActionStatus.FAILED,
            ActionStatus.EXPIRED,
            ActionStatus.REJECTED,
        }

        for action_id, action in list(self._actions.items()):
            if action.status in terminal_statuses and action.created_at < cutoff:
                del self._actions[action_id]
                await self._delete_action_from_db(action_id)
                removed_count += 1

        if removed_count > 0:
            logger.debug(f"Cleaned up {removed_count} old actions")

        return removed_count

    def _action_to_dict(self, action: QueuedAction) -> Dict[str, Any]:
        """Convert a QueuedAction to a dictionary."""
        return {
            "id": action.id,
            "device_id": action.device_id,
            "action_type": action.action_type,
            "params": action.params,
            "security_level": action.security_level.value,
            "status": action.status.value,
            "executor_result": action.executor_result,
            "error_message": action.error_message,
            "created_at": action.created_at.isoformat(),
            "expires_at": action.expires_at.isoformat(),
            "confirmed_at": action.confirmed_at.isoformat() if action.confirmed_at else None,
            "completed_at": action.completed_at.isoformat() if action.completed_at else None,
        }

    async def _run_cleanup_loop(self) -> None:
        """Background loop to expire and cleanup old actions."""
        logger.info("Starting action cleanup loop")

        while True:
            try:
                await asyncio.sleep(60)  # Check every minute
                await self.expire_old_actions()

                # Cleanup old completed actions once per hour
                if datetime.now(timezone.utc).minute == 0:
                    await self.cleanup_completed_actions()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Action cleanup error: {e}")

    async def _load_actions_from_db(self) -> None:
        """Load pending actions from database."""
        try:
            actions = database.get_mobile_actions()
            for row in actions:
                action = QueuedAction(
                    id=row["id"],
                    device_id=row["device_id"],
                    action_type=row["action_type"],
                    params=json.loads(row.get("params", "{}")),
                    security_level=SecurityLevel(row.get("security_level", "medium")),
                    status=ActionStatus(row.get("status", "pending")),
                    executor_result=row.get("executor_result"),
                    error_message=row.get("error_message"),
                    created_at=datetime.fromisoformat(row["created_at"]) if row.get("created_at") else datetime.now(timezone.utc),
                    expires_at=datetime.fromisoformat(row["expires_at"]) if row.get("expires_at") else datetime.now(timezone.utc) + timedelta(seconds=self._expiry_seconds),
                    confirmed_at=datetime.fromisoformat(row["confirmed_at"]) if row.get("confirmed_at") else None,
                    completed_at=datetime.fromisoformat(row["completed_at"]) if row.get("completed_at") else None,
                )
                self._actions[action.id] = action

            logger.debug(f"Loaded {len(self._actions)} actions from database")

        except Exception as e:
            logger.warning(f"Could not load actions from database: {e}")

    async def _save_action_to_db(self, action: QueuedAction) -> None:
        """Save an action to database."""
        try:
            database.save_mobile_action(
                action_id=action.id,
                device_id=action.device_id,
                action_type=action.action_type,
                params=json.dumps(action.params),
                security_level=action.security_level.value,
                status=action.status.value,
                executor_result=action.executor_result,
                error_message=action.error_message,
                expires_at=action.expires_at.isoformat(),
                confirmed_at=action.confirmed_at.isoformat() if action.confirmed_at else None,
                completed_at=action.completed_at.isoformat() if action.completed_at else None,
            )
        except Exception as e:
            logger.warning(f"Could not save action to database: {e}")

    async def _delete_action_from_db(self, action_id: str) -> None:
        """Delete an action from database."""
        try:
            database.delete_mobile_action(action_id)
        except Exception as e:
            logger.warning(f"Could not delete action from database: {e}")


# Global action queue instance
action_queue = ActionQueue()
