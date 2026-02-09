"""
Unit tests for push notification service.

Tests cover:
- NotificationPriority enum
- SecurityLevel enum
- NotificationResult dataclass
- PushNotificationService class
- Global instance
"""

import pytest
from unittest.mock import patch, MagicMock

from app.services.push_notification import (
    NotificationPriority,
    SecurityLevel,
    NotificationResult,
    PushNotificationService,
    push_notification_service,
)


# =============================================================================
# NotificationPriority Enum Tests
# =============================================================================

class TestNotificationPriority:
    """Test NotificationPriority enum."""

    def test_priority_values(self):
        """NotificationPriority should have expected values."""
        assert NotificationPriority.NORMAL.value == "normal"
        assert NotificationPriority.HIGH.value == "high"

    def test_priority_count(self):
        """Should have expected number of priorities."""
        priorities = list(NotificationPriority)
        assert len(priorities) >= 2


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
        assert SecurityLevel.CRITICAL.value == "critical"

    def test_security_levels_match_action_queue(self):
        """Security levels should match action queue levels."""
        from app.services.action_queue import SecurityLevel as ActionSecurityLevel
        assert SecurityLevel.LOW.value == ActionSecurityLevel.LOW.value
        assert SecurityLevel.MEDIUM.value == ActionSecurityLevel.MEDIUM.value
        assert SecurityLevel.HIGH.value == ActionSecurityLevel.HIGH.value


# =============================================================================
# NotificationResult Tests
# =============================================================================

class TestNotificationResult:
    """Test NotificationResult dataclass."""

    def test_create_success_result(self):
        """Should create success result."""
        result = NotificationResult(
            success=True,
            message_id="msg-123",
            device_id="device-1",
        )
        assert result.success is True
        assert result.message_id == "msg-123"
        assert result.device_id == "device-1"
        assert result.error is None

    def test_create_error_result(self):
        """Should create error result."""
        result = NotificationResult(
            success=False,
            error="Failed to send",
            device_id="device-1",
        )
        assert result.success is False
        assert result.error == "Failed to send"
        assert result.message_id is None

    def test_result_optional_fields(self):
        """Should have correct optional field defaults."""
        result = NotificationResult(success=True)
        assert result.message_id is None
        assert result.error is None
        assert result.device_id is None


# =============================================================================
# PushNotificationService Tests
# =============================================================================

class TestPushNotificationService:
    """Test PushNotificationService class."""

    def test_create_service(self):
        """Should create service instance."""
        service = PushNotificationService()
        assert service is not None

    def test_service_tracks_sent_notifications(self):
        """Service should track sent notifications in mock mode."""
        service = PushNotificationService()
        assert hasattr(service, "_sent_notifications")
        assert isinstance(service._sent_notifications, list)

    def test_service_has_callbacks(self):
        """Service should support notification callbacks."""
        service = PushNotificationService()
        assert hasattr(service, "_notification_callbacks")

    def test_service_starts_uninitialized(self):
        """Service should start uninitialized."""
        service = PushNotificationService()
        assert service._initialized is False

    def test_service_mock_mode_default(self):
        """Service should default to mock mode."""
        service = PushNotificationService()
        assert service._mock_mode is True


class TestPushNotificationServiceInitialization:
    """Test PushNotificationService initialization."""

    def test_initialize_without_credentials(self):
        """Should initialize in mock mode without credentials."""
        service = PushNotificationService()
        with patch.dict("os.environ", {}, clear=True):
            result = service.initialize()

        assert result is False  # Returns False for mock mode
        assert service._initialized is True
        assert service._mock_mode is True

    def test_initialize_idempotent(self):
        """Initialize should be idempotent."""
        service = PushNotificationService()
        with patch.dict("os.environ", {}, clear=True):
            result1 = service.initialize()
            result2 = service.initialize()

        assert result1 == result2


# =============================================================================
# Global Instance Tests
# =============================================================================

class TestGlobalInstance:
    """Test global push notification service instance."""

    def test_global_instance_exists(self):
        """Global push notification service instance should exist."""
        assert push_notification_service is not None
        assert isinstance(push_notification_service, PushNotificationService)


# =============================================================================
# Module Import Tests
# =============================================================================

class TestModuleImports:
    """Test module imports."""

    def test_push_notification_module_imports(self):
        """Push notification module should import without errors."""
        from app.services import push_notification as pn_module
        assert pn_module is not None

    def test_enums_importable(self):
        """Enums should be importable."""
        from app.services.push_notification import NotificationPriority, SecurityLevel
        assert NotificationPriority is not None
        assert SecurityLevel is not None

    def test_dataclass_importable(self):
        """NotificationResult should be importable."""
        from app.services.push_notification import NotificationResult
        assert NotificationResult is not None

    def test_class_importable(self):
        """PushNotificationService should be importable."""
        from app.services.push_notification import PushNotificationService
        assert PushNotificationService is not None
