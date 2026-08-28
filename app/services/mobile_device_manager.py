"""
Mobile device manager service for registering and managing mobile devices.

Handles device registration, API key generation, capability updates, and FCM token management.
"""

import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.db import database
from app.db.async_database import run_sync

logger = logging.getLogger(__name__)


@dataclass
class DeviceCapabilities:
    """Capabilities of a mobile device."""
    biometric_auth: bool = False
    push_notifications: bool = False
    background_location: bool = False
    camera: bool = False
    microphone: bool = False
    local_storage: bool = False


@dataclass
class MobileDevice:
    """A registered mobile device."""
    id: str
    name: str
    platform: str  # "ios", "android"
    model: Optional[str] = None
    os_version: Optional[str] = None
    app_version: Optional[str] = None
    fcm_token: Optional[str] = None
    api_key_hash: Optional[str] = None
    capabilities: DeviceCapabilities = field(default_factory=DeviceCapabilities)
    is_active: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_seen_at: Optional[datetime] = None


class MobileDeviceManager:
    """
    Manager for mobile device registration and lifecycle.

    Handles device registration with API key generation, capability updates,
    FCM token management, and device verification.
    """

    API_KEY_PREFIX = "ais_mobile_"  # AI Shuffle mobile prefix
    API_KEY_LENGTH = 32

    def __init__(self):
        self._devices: Dict[str, MobileDevice] = {}
        self._initialized = False

    async def initialize(self) -> None:
        """Load devices from database."""
        if self._initialized:
            return

        try:
            # Off-loop read; the row->MobileDevice decode stays on the loop
            # (pure CPU, and it populates loop-owned state).
            devices = await run_sync(database.get_mobile_devices)
            for row in devices:
                device = self._row_to_device(row)
                self._devices[device.id] = device

            logger.info(f"Loaded {len(self._devices)} mobile devices from database")

        except Exception as e:
            logger.warning(f"Could not load devices from database: {e}")

        self._initialized = True

    def _row_to_device(self, row: Dict[str, Any]) -> MobileDevice:
        """Convert a database row to a MobileDevice."""
        import json

        caps_data = json.loads(row.get("capabilities", "{}"))
        capabilities = DeviceCapabilities(
            biometric_auth=caps_data.get("biometric_auth", False),
            push_notifications=caps_data.get("push_notifications", False),
            background_location=caps_data.get("background_location", False),
            camera=caps_data.get("camera", False),
            microphone=caps_data.get("microphone", False),
            local_storage=caps_data.get("local_storage", False),
        )

        return MobileDevice(
            id=row["id"],
            name=row["name"],
            platform=row["platform"],
            model=row.get("model"),
            os_version=row.get("os_version"),
            app_version=row.get("app_version"),
            fcm_token=row.get("fcm_token"),
            api_key_hash=row.get("api_key_hash"),
            capabilities=capabilities,
            is_active=bool(row.get("is_active", True)),
            created_at=datetime.fromisoformat(row["created_at"]) if row.get("created_at") else datetime.now(timezone.utc),
            updated_at=datetime.fromisoformat(row["updated_at"]) if row.get("updated_at") else datetime.now(timezone.utc),
            last_seen_at=datetime.fromisoformat(row["last_seen_at"]) if row.get("last_seen_at") else None,
        )

    def _device_to_dict(self, device: MobileDevice, include_hash: bool = False) -> Dict[str, Any]:
        """Convert a MobileDevice to a dictionary."""
        result = {
            "id": device.id,
            "name": device.name,
            "platform": device.platform,
            "model": device.model,
            "os_version": device.os_version,
            "app_version": device.app_version,
            "fcm_token": device.fcm_token,
            "capabilities": {
                "biometric_auth": device.capabilities.biometric_auth,
                "push_notifications": device.capabilities.push_notifications,
                "background_location": device.capabilities.background_location,
                "camera": device.capabilities.camera,
                "microphone": device.capabilities.microphone,
                "local_storage": device.capabilities.local_storage,
            },
            "is_active": device.is_active,
            "created_at": device.created_at.isoformat(),
            "updated_at": device.updated_at.isoformat(),
            "last_seen_at": device.last_seen_at.isoformat() if device.last_seen_at else None,
        }

        if include_hash:
            result["api_key_hash"] = device.api_key_hash

        return result

    def _generate_api_key(self) -> str:
        """Generate a new API key for a device."""
        random_part = secrets.token_urlsafe(self.API_KEY_LENGTH)
        return f"{self.API_KEY_PREFIX}{random_part}"

    def _hash_api_key(self, api_key: str) -> str:
        """Hash an API key for storage."""
        return hashlib.sha256(api_key.encode()).hexdigest()

    async def register_device(
        self,
        device_info: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Register a new mobile device.

        Args:
            device_info: Device information dict with:
                - name: Display name for the device
                - platform: "ios" or "android"
                - model: Device model (optional)
                - os_version: OS version (optional)
                - app_version: App version (optional)
                - fcm_token: FCM registration token (optional)
                - capabilities: Device capabilities dict (optional)

        Returns:
            Dict with device info and generated API key (key only returned once)
        """
        if not self._initialized:
            await self.initialize()

        device_id = str(uuid.uuid4())
        api_key = self._generate_api_key()
        api_key_hash = self._hash_api_key(api_key)

        caps_data = device_info.get("capabilities", {})
        capabilities = DeviceCapabilities(
            biometric_auth=caps_data.get("biometric_auth", False),
            push_notifications=caps_data.get("push_notifications", False),
            background_location=caps_data.get("background_location", False),
            camera=caps_data.get("camera", False),
            microphone=caps_data.get("microphone", False),
            local_storage=caps_data.get("local_storage", False),
        )

        device = MobileDevice(
            id=device_id,
            name=device_info.get("name", "Mobile Device"),
            platform=device_info.get("platform", "unknown"),
            model=device_info.get("model"),
            os_version=device_info.get("os_version"),
            app_version=device_info.get("app_version"),
            fcm_token=device_info.get("fcm_token"),
            api_key_hash=api_key_hash,
            capabilities=capabilities,
        )

        self._devices[device_id] = device
        await self._save_device_to_db(device)

        logger.debug(f"Registered new device: {device_id} ({device.platform})")

        # Return device info with the API key (only returned once)
        result = self._device_to_dict(device)
        result["api_key"] = api_key

        return result

    async def update_capabilities(
        self,
        device_id: str,
        capabilities: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """
        Update device capabilities.

        Args:
            device_id: Device ID
            capabilities: New capabilities dict

        Returns:
            Updated device dict or None if not found
        """
        if not self._initialized:
            await self.initialize()

        device = self._devices.get(device_id)
        if not device:
            return None

        device.capabilities = DeviceCapabilities(
            biometric_auth=capabilities.get("biometric_auth", device.capabilities.biometric_auth),
            push_notifications=capabilities.get("push_notifications", device.capabilities.push_notifications),
            background_location=capabilities.get("background_location", device.capabilities.background_location),
            camera=capabilities.get("camera", device.capabilities.camera),
            microphone=capabilities.get("microphone", device.capabilities.microphone),
            local_storage=capabilities.get("local_storage", device.capabilities.local_storage),
        )
        device.updated_at = datetime.now(timezone.utc)

        await self._save_device_to_db(device)

        logger.debug(f"Updated capabilities for device {device_id}")
        return self._device_to_dict(device)

    async def get_device(self, device_id: str) -> Optional[Dict[str, Any]]:
        """
        Get device information.

        Args:
            device_id: Device ID

        Returns:
            Device dict or None if not found
        """
        if not self._initialized:
            await self.initialize()

        device = self._devices.get(device_id)
        return self._device_to_dict(device) if device else None

    async def list_devices(self, active_only: bool = False) -> List[Dict[str, Any]]:
        """
        List all registered devices.

        Args:
            active_only: If True, only return active devices

        Returns:
            List of device dicts
        """
        if not self._initialized:
            await self.initialize()

        devices = self._devices.values()
        if active_only:
            devices = [d for d in devices if d.is_active]

        return [self._device_to_dict(d) for d in devices]

    async def remove_device(self, device_id: str) -> bool:
        """
        Remove a device registration.

        Args:
            device_id: Device ID to remove

        Returns:
            True if removed, False if not found
        """
        if not self._initialized:
            await self.initialize()

        if device_id not in self._devices:
            return False

        del self._devices[device_id]
        await self._delete_device_from_db(device_id)

        logger.debug(f"Removed device {device_id}")
        return True

    async def verify_device_key(self, device_id: str, api_key: str) -> bool:
        """
        Verify a device's API key.

        Args:
            device_id: Device ID
            api_key: API key to verify

        Returns:
            True if valid, False otherwise
        """
        if not self._initialized:
            await self.initialize()

        device = self._devices.get(device_id)
        if not device or not device.is_active:
            return False

        if not device.api_key_hash:
            return False

        provided_hash = self._hash_api_key(api_key)
        return secrets.compare_digest(provided_hash, device.api_key_hash)

    async def update_fcm_token(
        self,
        device_id: str,
        token: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Update a device's FCM token.

        Args:
            device_id: Device ID
            token: New FCM registration token

        Returns:
            Updated device dict or None if not found
        """
        if not self._initialized:
            await self.initialize()

        device = self._devices.get(device_id)
        if not device:
            return None

        device.fcm_token = token
        device.updated_at = datetime.now(timezone.utc)

        await self._save_device_to_db(device)

        logger.debug(f"Updated FCM token for device {device_id}")
        return self._device_to_dict(device)

    async def update_last_seen(self, device_id: str) -> Optional[Dict[str, Any]]:
        """
        Update a device's last seen timestamp.

        Args:
            device_id: Device ID

        Returns:
            Updated device dict or None if not found
        """
        if not self._initialized:
            await self.initialize()

        device = self._devices.get(device_id)
        if not device:
            return None

        device.last_seen_at = datetime.now(timezone.utc)

        # Only save periodically to avoid excessive DB writes
        await self._save_device_to_db(device)

        return self._device_to_dict(device)

    async def set_device_active(
        self,
        device_id: str,
        is_active: bool,
    ) -> Optional[Dict[str, Any]]:
        """
        Enable or disable a device.

        Args:
            device_id: Device ID
            is_active: Whether device should be active

        Returns:
            Updated device dict or None if not found
        """
        if not self._initialized:
            await self.initialize()

        device = self._devices.get(device_id)
        if not device:
            return None

        device.is_active = is_active
        device.updated_at = datetime.now(timezone.utc)

        await self._save_device_to_db(device)

        logger.debug(f"Set device {device_id} active={is_active}")
        return self._device_to_dict(device)

    async def _save_device_to_db(self, device: MobileDevice) -> None:
        """Save a device to database."""
        import json

        try:
            await run_sync(
                database.save_mobile_device,
                device_id=device.id,
                name=device.name,
                platform=device.platform,
                model=device.model,
                os_version=device.os_version,
                app_version=device.app_version,
                fcm_token=device.fcm_token,
                api_key_hash=device.api_key_hash,
                capabilities=json.dumps({
                    "biometric_auth": device.capabilities.biometric_auth,
                    "push_notifications": device.capabilities.push_notifications,
                    "background_location": device.capabilities.background_location,
                    "camera": device.capabilities.camera,
                    "microphone": device.capabilities.microphone,
                    "local_storage": device.capabilities.local_storage,
                }),
                is_active=device.is_active,
                last_seen_at=device.last_seen_at.isoformat() if device.last_seen_at else None,
            )
        except Exception as e:
            logger.warning(f"Could not save device to database: {e}")

    async def _delete_device_from_db(self, device_id: str) -> None:
        """Delete a device from database."""
        try:
            await run_sync(database.delete_mobile_device, device_id)
        except Exception as e:
            logger.warning(f"Could not delete device from database: {e}")


# Global device manager instance
mobile_device_manager = MobileDeviceManager()
