"""
Unit tests for mobile device API endpoints.

Tests cover:
- Device registration and authentication
- Mobile API key generation and validation
- Pending actions management
- Action confirmation with biometric verification
- Push notification token management
- Device capabilities updates
- Admin device management
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.mobile import (
    router,
    generate_mobile_api_key,
    get_device_from_api_key,
    MOBILE_API_KEY_PREFIX,
    MOBILE_API_KEY_EXPIRY_DAYS,
    PENDING_ACTION_EXPIRY_MINUTES,
)


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def app():
    """Create FastAPI app with mobile router."""
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
def client(app):
    """Create test client."""
    return TestClient(app)


@pytest.fixture
def mock_database():
    """Mock database module."""
    with patch("app.api.mobile.db") as mock_db:
        yield mock_db


@pytest.fixture
def mock_auth():
    """Mock auth functions."""
    with patch("app.api.mobile.get_api_key") as mock_get_key, \
         patch("app.api.mobile.hash_api_key") as mock_hash, \
         patch("app.api.mobile.require_admin") as mock_admin:
        mock_hash.return_value = "hashed_key"
        mock_admin.return_value = "admin_token"
        yield {
            "get_api_key": mock_get_key,
            "hash_api_key": mock_hash,
            "require_admin": mock_admin,
        }


# =============================================================================
# Mobile API Key Tests
# =============================================================================

class TestMobileApiKeyGeneration:
    """Test mobile API key generation."""

    def test_generate_mobile_api_key_format(self):
        """Generated key should have correct prefix."""
        key = generate_mobile_api_key()
        assert key.startswith(MOBILE_API_KEY_PREFIX)

    def test_generate_mobile_api_key_length(self):
        """Generated key should have sufficient length."""
        key = generate_mobile_api_key()
        # Prefix + 32 bytes base64 encoded = ~48+ chars total
        assert len(key) > 40

    def test_generate_mobile_api_key_uniqueness(self):
        """Multiple keys should be unique."""
        keys = [generate_mobile_api_key() for _ in range(100)]
        assert len(set(keys)) == 100


class TestMobileApiKeyPrefix:
    """Test mobile API key prefix constant."""

    def test_prefix_value(self):
        """Prefix should be 'aim_'."""
        assert MOBILE_API_KEY_PREFIX == "aim_"


class TestExpiryConstants:
    """Test expiry constant values."""

    def test_api_key_expiry_days(self):
        """API key expiry should be 365 days."""
        assert MOBILE_API_KEY_EXPIRY_DAYS == 365

    def test_pending_action_expiry_minutes(self):
        """Pending action expiry should be 5 minutes."""
        assert PENDING_ACTION_EXPIRY_MINUTES == 5


# =============================================================================
# Device Authentication Tests
# =============================================================================

class TestGetDeviceFromApiKey:
    """Test device authentication via API key."""

    def test_missing_api_key_raises_401(self, mock_database, mock_auth):
        """Missing API key should raise 401."""
        mock_auth["get_api_key"].return_value = None
        mock_request = MagicMock()

        with pytest.raises(Exception) as exc_info:
            get_device_from_api_key(mock_request)
        assert "401" in str(exc_info.value) or "Unauthorized" in str(exc_info.value)

    def test_invalid_prefix_raises_401(self, mock_database, mock_auth):
        """Key without mobile prefix should raise 401."""
        mock_auth["get_api_key"].return_value = "regular_api_key"
        mock_request = MagicMock()

        with pytest.raises(Exception) as exc_info:
            get_device_from_api_key(mock_request)
        assert "401" in str(exc_info.value) or "Unauthorized" in str(exc_info.value)

    def test_unknown_key_raises_401(self, mock_database, mock_auth):
        """Unknown API key should raise 401."""
        mock_auth["get_api_key"].return_value = f"{MOBILE_API_KEY_PREFIX}unknown_key"
        mock_database.get_mobile_device_by_api_key_hash.return_value = None
        mock_request = MagicMock()

        with pytest.raises(Exception) as exc_info:
            get_device_from_api_key(mock_request)
        assert "401" in str(exc_info.value) or "Unauthorized" in str(exc_info.value)

    def test_valid_key_returns_device(self, mock_database, mock_auth):
        """Valid API key should return device."""
        mock_auth["get_api_key"].return_value = f"{MOBILE_API_KEY_PREFIX}valid_key"
        mock_device = {"device_id": "test-device-123", "device_name": "Test Phone"}
        mock_database.get_mobile_device_by_api_key_hash.return_value = mock_device
        mock_request = MagicMock()

        device = get_device_from_api_key(mock_request)

        assert device["device_id"] == "test-device-123"
        mock_database.update_mobile_device_last_seen.assert_called_once_with("test-device-123")


# =============================================================================
# Device Registration Tests
# =============================================================================

class TestRegisterDevice:
    """Test device registration endpoint."""

    def test_register_new_device_success(self, client, mock_database, mock_auth):
        """New device registration should succeed."""
        mock_database.get_mobile_device.return_value = None
        mock_database.create_mobile_device.return_value = {"device_id": "new-device"}

        response = client.post("/api/v1/mobile/register", json={
            "device_info": {
                "device_id": "new-device",
                "device_name": "My iPhone",
                "platform": "ios",
                "capabilities": {
                    "push_enabled": True,
                    "biometric_available": True,
                    "biometric_type": "face_id"
                }
            }
        })

        assert response.status_code == 200
        data = response.json()
        assert "api_key" in data
        assert data["api_key"].startswith(MOBILE_API_KEY_PREFIX)
        assert data["device_id"] == "new-device"
        assert "expires_at" in data

    def test_register_duplicate_device_fails(self, client, mock_database, mock_auth):
        """Duplicate device registration should fail."""
        mock_database.get_mobile_device.return_value = {"device_id": "existing-device"}

        response = client.post("/api/v1/mobile/register", json={
            "device_info": {
                "device_id": "existing-device",
                "device_name": "My iPhone",
                "platform": "ios",
                "capabilities": {
                    "push_enabled": True,
                    "biometric_available": False
                }
            }
        })

        assert response.status_code == 400
        assert "already registered" in response.json()["detail"]


# =============================================================================
# Capabilities Update Tests
# =============================================================================

class TestUpdateCapabilities:
    """Test device capabilities update endpoint."""

    def test_update_capabilities_success(self, client, mock_database, mock_auth):
        """Capabilities update should succeed for authenticated device."""
        mock_auth["get_api_key"].return_value = f"{MOBILE_API_KEY_PREFIX}valid_key"
        mock_database.get_mobile_device_by_api_key_hash.return_value = {
            "device_id": "test-device"
        }
        mock_database.update_mobile_device_capabilities.return_value = True

        response = client.post(
            "/api/v1/mobile/capabilities",
            json={
                "device_id": "test-device",
                "capabilities": {
                    "push_enabled": True,
                    "biometric_available": True,
                    "biometric_type": "fingerprint"
                }
            },
            headers={"Authorization": f"Bearer {MOBILE_API_KEY_PREFIX}valid_key"}
        )

        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_update_capabilities_device_mismatch(self, client, mock_database, mock_auth):
        """Capabilities update should fail for mismatched device ID."""
        mock_auth["get_api_key"].return_value = f"{MOBILE_API_KEY_PREFIX}valid_key"
        mock_database.get_mobile_device_by_api_key_hash.return_value = {
            "device_id": "device-a"
        }

        response = client.post(
            "/api/v1/mobile/capabilities",
            json={
                "device_id": "device-b",
                "capabilities": {
                    "push_enabled": True,
                    "biometric_available": False
                }
            },
            headers={"Authorization": f"Bearer {MOBILE_API_KEY_PREFIX}valid_key"}
        )

        assert response.status_code == 403


# =============================================================================
# Pending Actions Tests
# =============================================================================

class TestGetPendingActions:
    """Test pending actions retrieval."""

    def test_get_pending_actions_success(self, client, mock_database, mock_auth):
        """Should return pending actions for authenticated device."""
        mock_auth["get_api_key"].return_value = f"{MOBILE_API_KEY_PREFIX}valid_key"
        mock_database.get_mobile_device_by_api_key_hash.return_value = {
            "device_id": "test-device"
        }
        now = datetime.utcnow()
        mock_database.get_pending_actions_for_device.return_value = [
            {
                "id": "action-1",
                "device_id": "test-device",
                "action_type": "approve_command",
                "params": {"command": "rm -rf /tmp/test"},
                "security_level": "high",
                "created_at": now.isoformat(),
                "expires_at": (now + timedelta(minutes=5)).isoformat(),
                "status": "pending"
            }
        ]

        response = client.get(
            "/api/v1/mobile/pending",
            headers={"Authorization": f"Bearer {MOBILE_API_KEY_PREFIX}valid_key"}
        )

        assert response.status_code == 200
        actions = response.json()
        assert len(actions) == 1
        assert actions[0]["id"] == "action-1"
        assert actions[0]["action_type"] == "approve_command"


class TestConfirmAction:
    """Test action confirmation endpoint."""

    def test_confirm_action_approved(self, client, mock_database, mock_auth):
        """Approved action should update status."""
        mock_auth["get_api_key"].return_value = f"{MOBILE_API_KEY_PREFIX}valid_key"
        mock_database.get_mobile_device_by_api_key_hash.return_value = {
            "device_id": "test-device"
        }
        now = datetime.utcnow()
        mock_database.get_pending_action.return_value = {
            "id": "action-1",
            "device_id": "test-device",
            "status": "pending",
            "security_level": "normal",
            "expires_at": (now + timedelta(minutes=5)).isoformat()
        }
        mock_database.update_pending_action_status.return_value = True

        response = client.post(
            "/api/v1/mobile/confirm/action-1",
            json={"approved": True, "biometric_verified": False},
            headers={"Authorization": f"Bearer {MOBILE_API_KEY_PREFIX}valid_key"}
        )

        assert response.status_code == 200
        assert response.json()["result"] == "approved"
        mock_database.update_pending_action_status.assert_called_once_with("action-1", "approved")

    def test_confirm_action_rejected(self, client, mock_database, mock_auth):
        """Rejected action should update status."""
        mock_auth["get_api_key"].return_value = f"{MOBILE_API_KEY_PREFIX}valid_key"
        mock_database.get_mobile_device_by_api_key_hash.return_value = {
            "device_id": "test-device"
        }
        now = datetime.utcnow()
        mock_database.get_pending_action.return_value = {
            "id": "action-2",
            "device_id": "test-device",
            "status": "pending",
            "security_level": "normal",
            "expires_at": (now + timedelta(minutes=5)).isoformat()
        }
        mock_database.update_pending_action_status.return_value = True

        response = client.post(
            "/api/v1/mobile/confirm/action-2",
            json={"approved": False, "biometric_verified": False},
            headers={"Authorization": f"Bearer {MOBILE_API_KEY_PREFIX}valid_key"}
        )

        assert response.status_code == 200
        assert response.json()["result"] == "rejected"

    def test_confirm_action_not_found(self, client, mock_database, mock_auth):
        """Non-existent action should return 404."""
        mock_auth["get_api_key"].return_value = f"{MOBILE_API_KEY_PREFIX}valid_key"
        mock_database.get_mobile_device_by_api_key_hash.return_value = {
            "device_id": "test-device"
        }
        mock_database.get_pending_action.return_value = None

        response = client.post(
            "/api/v1/mobile/confirm/nonexistent",
            json={"approved": True, "biometric_verified": False},
            headers={"Authorization": f"Bearer {MOBILE_API_KEY_PREFIX}valid_key"}
        )

        assert response.status_code == 404

    def test_confirm_action_wrong_device(self, client, mock_database, mock_auth):
        """Action for different device should return 403."""
        mock_auth["get_api_key"].return_value = f"{MOBILE_API_KEY_PREFIX}valid_key"
        mock_database.get_mobile_device_by_api_key_hash.return_value = {
            "device_id": "device-a"
        }
        mock_database.get_pending_action.return_value = {
            "id": "action-1",
            "device_id": "device-b",
            "status": "pending"
        }

        response = client.post(
            "/api/v1/mobile/confirm/action-1",
            json={"approved": True, "biometric_verified": False},
            headers={"Authorization": f"Bearer {MOBILE_API_KEY_PREFIX}valid_key"}
        )

        assert response.status_code == 403

    def test_confirm_already_processed_action(self, client, mock_database, mock_auth):
        """Already processed action should return 400."""
        mock_auth["get_api_key"].return_value = f"{MOBILE_API_KEY_PREFIX}valid_key"
        mock_database.get_mobile_device_by_api_key_hash.return_value = {
            "device_id": "test-device"
        }
        mock_database.get_pending_action.return_value = {
            "id": "action-1",
            "device_id": "test-device",
            "status": "approved"
        }

        response = client.post(
            "/api/v1/mobile/confirm/action-1",
            json={"approved": True, "biometric_verified": False},
            headers={"Authorization": f"Bearer {MOBILE_API_KEY_PREFIX}valid_key"}
        )

        assert response.status_code == 400

    def test_confirm_high_security_requires_biometric(self, client, mock_database, mock_auth):
        """High security action requires biometric verification."""
        mock_auth["get_api_key"].return_value = f"{MOBILE_API_KEY_PREFIX}valid_key"
        mock_database.get_mobile_device_by_api_key_hash.return_value = {
            "device_id": "test-device"
        }
        now = datetime.utcnow()
        mock_database.get_pending_action.return_value = {
            "id": "action-1",
            "device_id": "test-device",
            "status": "pending",
            "security_level": "high",
            "expires_at": (now + timedelta(minutes=5)).isoformat()
        }

        response = client.post(
            "/api/v1/mobile/confirm/action-1",
            json={"approved": True, "biometric_verified": False},
            headers={"Authorization": f"Bearer {MOBILE_API_KEY_PREFIX}valid_key"}
        )

        assert response.status_code == 400
        assert "Biometric" in response.json()["detail"]


# =============================================================================
# Push Token Tests
# =============================================================================

class TestRegisterPushToken:
    """Test push notification token registration."""

    def test_register_token_success(self, client, mock_database, mock_auth):
        """Push token registration should succeed."""
        mock_auth["get_api_key"].return_value = f"{MOBILE_API_KEY_PREFIX}valid_key"
        mock_database.get_mobile_device_by_api_key_hash.return_value = {
            "device_id": "test-device"
        }
        mock_database.update_mobile_device_fcm_token.return_value = True

        response = client.post(
            "/api/v1/mobile/push/token",
            json={
                "device_id": "test-device",
                "fcm_token": "fcm_token_12345"
            },
            headers={"Authorization": f"Bearer {MOBILE_API_KEY_PREFIX}valid_key"}
        )

        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_register_token_device_mismatch(self, client, mock_database, mock_auth):
        """Push token for mismatched device should fail."""
        mock_auth["get_api_key"].return_value = f"{MOBILE_API_KEY_PREFIX}valid_key"
        mock_database.get_mobile_device_by_api_key_hash.return_value = {
            "device_id": "device-a"
        }

        response = client.post(
            "/api/v1/mobile/push/token",
            json={
                "device_id": "device-b",
                "fcm_token": "fcm_token_12345"
            },
            headers={"Authorization": f"Bearer {MOBILE_API_KEY_PREFIX}valid_key"}
        )

        assert response.status_code == 403


# =============================================================================
# Admin Device Management Tests
# =============================================================================

class TestListDevices:
    """Test admin device listing."""

    def test_list_devices_module_structure(self, mock_database):
        """Admin device listing endpoint should exist in router."""
        from app.api.mobile import router
        routes = [r.path for r in router.routes]
        assert "/devices" in routes or any("/devices" in r for r in routes)


class TestDeleteDevice:
    """Test admin device deletion."""

    def test_delete_device_module_structure(self, mock_database):
        """Admin device delete endpoint should exist in router."""
        from app.api.mobile import router
        routes = [r.path for r in router.routes]
        assert any("devices" in str(r) for r in routes)


# =============================================================================
# Module Import Tests
# =============================================================================

class TestModuleImports:
    """Test that mobile module imports correctly."""

    def test_mobile_module_imports(self):
        """Mobile module should import without errors."""
        from app.api import mobile
        assert mobile is not None

    def test_mobile_router_exists(self):
        """Mobile router should exist."""
        from app.api.mobile import router
        assert router is not None

    def test_mobile_models_import(self):
        """Mobile models should be importable."""
        from app.core.models import (
            MobileDevice,
            MobileDeviceRegisterRequest,
            MobileDeviceRegisterResponse,
            PendingAction,
            ActionConfirmRequest,
            PushTokenRequest,
        )
        assert MobileDevice is not None
        assert PendingAction is not None
