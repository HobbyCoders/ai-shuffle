"""
Unit tests for action queue service.

Tests cover:
- ActionStatus enum
- SecurityLevel enum
- QueuedAction dataclass
- ActionQueue class
- Global instance
"""

import pytest
from datetime import datetime
from unittest.mock import patch, MagicMock

from app.services.action_queue import (
    ActionStatus,
    SecurityLevel,
    QueuedAction,
    ActionQueue,
    action_queue,
)


# =============================================================================
# ActionStatus Enum Tests
# =============================================================================

class TestActionStatus:
    """Test ActionStatus enum."""

    def test_action_status_values(self):
        """ActionStatus should have expected values."""
        assert ActionStatus.PENDING.value == "pending"
        assert ActionStatus.APPROVED.value == "approved"
        assert ActionStatus.REJECTED.value == "rejected"
        assert ActionStatus.EXPIRED.value == "expired"
        assert ActionStatus.COMPLETED.value == "completed"
        assert ActionStatus.FAILED.value == "failed"

    def test_action_status_count(self):
        """Should have expected number of statuses."""
        statuses = list(ActionStatus)
        assert len(statuses) >= 6


# =============================================================================
# SecurityLevel Enum Tests
# =============================================================================

class TestSecurityLevel:
    """Test SecurityLevel enum."""

    def test_security_level_values(self):
        """SecurityLevel should have expected values."""
        assert SecurityLevel.LOW.value == "low"
        assert SecurityLevel.MEDIUM.value == "medium"
        assert SecurityLevel.HIGH.value == "high"

    def test_security_level_count(self):
        """Should have expected number of security levels."""
        levels = list(SecurityLevel)
        assert len(levels) >= 4

    def test_critical_level_exists(self):
        """Should have critical security level."""
        assert SecurityLevel.CRITICAL.value == "critical"


# =============================================================================
# QueuedAction Tests
# =============================================================================

class TestQueuedAction:
    """Test QueuedAction dataclass."""

    def test_create_queued_action(self):
        """Should create queued action."""
        action = QueuedAction(
            id="action-1",
            device_id="device-1",
            action_type="send_sms",
            params={"to": "+1234567890", "message": "Hello"},
            security_level=SecurityLevel.MEDIUM,
            status=ActionStatus.PENDING,
        )
        assert action.id == "action-1"
        assert action.device_id == "device-1"
        assert action.action_type == "send_sms"
        assert action.params["to"] == "+1234567890"
        assert action.security_level == SecurityLevel.MEDIUM
        assert action.status == ActionStatus.PENDING

    def test_queued_action_has_timestamps(self):
        """Action should have timestamp fields."""
        action = QueuedAction(
            id="action-2",
            device_id="device-1",
            action_type="test",
            params={},
            security_level=SecurityLevel.LOW,
            status=ActionStatus.PENDING,
        )
        assert hasattr(action, "created_at")
        assert hasattr(action, "expires_at")


# =============================================================================
# ActionQueue Tests
# =============================================================================

class TestActionQueue:
    """Test ActionQueue class."""

    def test_create_action_queue(self):
        """Should create action queue instance."""
        queue = ActionQueue()
        assert queue is not None

    def test_queue_has_actions_dict(self):
        """Queue should have actions storage."""
        queue = ActionQueue()
        assert hasattr(queue, "_actions")

    def test_queue_has_callbacks(self):
        """Queue should support callbacks."""
        queue = ActionQueue()
        assert hasattr(queue, "_confirmation_callbacks")
        assert hasattr(queue, "_completion_callbacks")


# =============================================================================
# Global Instance Tests
# =============================================================================

class TestGlobalInstance:
    """Test global action queue instance."""

    def test_global_instance_exists(self):
        """Global action queue instance should exist."""
        assert action_queue is not None
        assert isinstance(action_queue, ActionQueue)


# =============================================================================
# Module Import Tests
# =============================================================================

class TestModuleImports:
    """Test module imports."""

    def test_action_queue_module_imports(self):
        """Action queue module should import without errors."""
        from app.services import action_queue as aq_module
        assert aq_module is not None

    def test_enums_importable(self):
        """Enums should be importable."""
        from app.services.action_queue import ActionStatus, SecurityLevel
        assert ActionStatus is not None
        assert SecurityLevel is not None

    def test_dataclass_importable(self):
        """QueuedAction should be importable."""
        from app.services.action_queue import QueuedAction
        assert QueuedAction is not None

    def test_class_importable(self):
        """ActionQueue class should be importable."""
        from app.services.action_queue import ActionQueue
        assert ActionQueue is not None
