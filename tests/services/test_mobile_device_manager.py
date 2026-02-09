"""
Unit tests for mobile device manager service.

Tests cover:
- DeviceCapabilities dataclass
- MobileDevice dataclass
- MobileDeviceManager class
- Global instance
"""

import pytest
from datetime import datetime
from unittest.mock import patch, MagicMock

from app.services.mobile_device_manager import (
    DeviceCapabilities,
    MobileDevice,
    MobileDeviceManager,
    mobile_device_manager,
)


# =============================================================================
# DeviceCapabilities Tests
# =============================================================================

class TestDeviceCapabilities:
    """Test DeviceCapabilities dataclass."""

    def test_create_capabilities(self):
        """Should create capabilities with defaults."""
        caps = DeviceCapabilities()
        assert caps.biometric_auth is False
        assert caps.push_notifications is False
        assert caps.background_location is False
        assert caps.camera is False
        assert caps.microphone is False
        assert caps.local_storage is False

    def test_create_capabilities_with_values(self):
        """Should create capabilities with custom values."""
        caps = DeviceCapabilities(
            biometric_auth=True,
            push_notifications=True,
            camera=True,
        )
        assert caps.biometric_auth is True
        assert caps.push_notifications is True
        assert caps.camera is True
        assert caps.background_location is False


# =============================================================================
# MobileDevice Tests
# =============================================================================

class TestMobileDevice:
    """Test MobileDevice dataclass."""

    def test_create_device(self):
        """Should create device with required fields."""
        device = MobileDevice(
            id="device-1",
            name="My iPhone",
            platform="ios",
        )
        assert device.id == "device-1"
        assert device.name == "My iPhone"
        assert device.platform == "ios"
        assert device.is_active is True

    def test_device_has_timestamps(self):
        """Device should have timestamp fields."""
        device = MobileDevice(
            id="device-2",
            name="Test Device",
            platform="android",
        )
        assert device.created_at is not None
        assert device.updated_at is not None
        assert device.last_seen_at is None

    def test_device_optional_fields(self):
        """Device should have correct optional field defaults."""
        device = MobileDevice(
            id="device-3",
            name="Test",
            platform="ios",
        )
        assert device.model is None
        assert device.os_version is None
        assert device.app_version is None
        assert device.fcm_token is None
        assert device.api_key_hash is None

    def test_device_default_capabilities(self):
        """Device should have default capabilities."""
        device = MobileDevice(
            id="device-4",
            name="Test",
            platform="android",
        )
        assert isinstance(device.capabilities, DeviceCapabilities)
        assert device.capabilities.biometric_auth is False


# =============================================================================
# MobileDeviceManager Tests
# =============================================================================

class TestMobileDeviceManager:
    """Test MobileDeviceManager class."""

    def test_create_manager(self):
        """Should create manager instance."""
        manager = MobileDeviceManager()
        assert manager is not None

    def test_manager_has_devices_dict(self):
        """Manager should track devices."""
        manager = MobileDeviceManager()
        assert hasattr(manager, "_devices")
        assert isinstance(manager._devices, dict)

    def test_manager_starts_uninitialized(self):
        """Manager should start uninitialized."""
        manager = MobileDeviceManager()
        assert manager._initialized is False

    def test_manager_api_key_prefix(self):
        """Manager should have API key prefix."""
        assert MobileDeviceManager.API_KEY_PREFIX == "ais_mobile_"

    def test_manager_api_key_length(self):
        """Manager should have API key length."""
        assert MobileDeviceManager.API_KEY_LENGTH == 32


# =============================================================================
# Global Instance Tests
# =============================================================================

class TestGlobalInstance:
    """Test global manager instance."""

    def test_global_instance_exists(self):
        """Global manager instance should exist."""
        assert mobile_device_manager is not None
        assert isinstance(mobile_device_manager, MobileDeviceManager)


# =============================================================================
# Module Import Tests
# =============================================================================

class TestModuleImports:
    """Test module imports."""

    def test_manager_module_imports(self):
        """Manager module should import without errors."""
        from app.services import mobile_device_manager as mdm_module
        assert mdm_module is not None

    def test_dataclasses_importable(self):
        """Dataclasses should be importable."""
        from app.services.mobile_device_manager import DeviceCapabilities, MobileDevice
        assert DeviceCapabilities is not None
        assert MobileDevice is not None

    def test_class_importable(self):
        """MobileDeviceManager class should be importable."""
        from app.services.mobile_device_manager import MobileDeviceManager
        assert MobileDeviceManager is not None
