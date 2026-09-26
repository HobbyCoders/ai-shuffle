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
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.mobile import (
    router,
    generate_mobile_api_key,
    get_device_from_api_key,
    MOBILE_API_KEY_PREFIX,
    MOBILE_API_KEY_EXPIRY_DAYS,
)


def valid_mobile(device_id: str, **overrides):
    row = {
        "device_id": device_id,
        "device_name": "Test Phone",
        "api_key_expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        "scopes": ["controller", "mobile:notifications", "mobile:actions", "web:session"],
        "revoked_at": None,
    }
    row.update(overrides)
    return row


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
         patch("app.api.mobile.require_admin") as mock_admin, \
         patch("app.api.mobile.pairing.hash_pairing_code", return_value="pairing-hash"):
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
        """New API keys expire after 90 days; legacy rows retain their deadline."""
        assert MOBILE_API_KEY_EXPIRY_DAYS == 90


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
        mock_device = valid_mobile("test-device-123")
        mock_database.get_mobile_device_by_api_key_hash.return_value = mock_device
        mock_request = MagicMock()

        device = get_device_from_api_key(mock_request)

        assert device["device_id"] == "test-device-123"
        mock_database.update_mobile_device_last_seen.assert_called_once_with("test-device-123")

    def test_revoked_key_raises_401(self, mock_database, mock_auth):
        """Revoking the paired client must invalidate the linked mobile key."""
        mock_auth["get_api_key"].return_value = f"{MOBILE_API_KEY_PREFIX}revoked_key"
        mock_database.get_mobile_device_by_api_key_hash.return_value = valid_mobile(
            "revoked-device",
            revoked_at=datetime.now(timezone.utc).isoformat(),
        )

        with pytest.raises(Exception) as exc_info:
            get_device_from_api_key(MagicMock())

        assert exc_info.value.status_code == 401
        mock_database.update_mobile_device_last_seen.assert_not_called()

    def test_expired_key_raises_401(self, mock_database, mock_auth):
        """Stored mobile-key expiry is enforced on every authenticated call."""
        mock_auth["get_api_key"].return_value = f"{MOBILE_API_KEY_PREFIX}expired_key"
        mock_database.get_mobile_device_by_api_key_hash.return_value = valid_mobile(
            "expired-device",
            api_key_expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        )

        with pytest.raises(Exception) as exc_info:
            get_device_from_api_key(MagicMock())

        assert exc_info.value.status_code == 401
        mock_database.update_mobile_device_last_seen.assert_not_called()

    def test_legacy_key_uses_created_at_for_expiry(self, mock_database, mock_auth):
        """Pre-migration rows get the documented one-year limit, not forever access."""
        mock_auth["get_api_key"].return_value = f"{MOBILE_API_KEY_PREFIX}legacy_key"
        mock_database.get_mobile_device_by_api_key_hash.return_value = valid_mobile(
            "legacy-device",
            api_key_expires_at=None,
            created_at=(datetime.now(timezone.utc) - timedelta(days=366)).isoformat(),
        )

        with pytest.raises(Exception) as exc_info:
            get_device_from_api_key(MagicMock())

        assert exc_info.value.status_code == 401
        mock_database.update_mobile_device_last_seen.assert_not_called()


# =============================================================================
# Device Registration Tests
# =============================================================================

class TestRegisterDevice:
    """Test device registration endpoint."""

    def test_register_new_device_success(self, client, mock_database, mock_auth):
        """New device registration should succeed."""
        mock_database.get_mobile_device.return_value = None
        mock_database.exchange_grant_for_mobile.return_value = {
            "client": {"scopes": ["controller", "web:session"]}
        }

        response = client.post("/api/v1/mobile/register", json={
            "pairing_code": "ABCD-EFGH-IJKL",
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

    def test_register_without_pairing_code_is_forbidden(self, client, mock_database, mock_auth):
        response = client.post("/api/v1/mobile/register", json={
            "device_info": {
                "device_id": "new-device",
                "device_name": "My iPhone",
                "platform": "ios",
                "capabilities": {},
            }
        })
        assert response.status_code == 403
        mock_database.exchange_grant_for_mobile.assert_not_called()

    def test_register_duplicate_device_fails(self, client, mock_database, mock_auth):
        """Duplicate device registration should fail."""
        mock_database.get_mobile_device.return_value = {"device_id": "existing-device"}

        response = client.post("/api/v1/mobile/register", json={
            "pairing_code": "ABCD-EFGH-IJKL",
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
# Web Session Tests
# =============================================================================

class TestCreateWebSession:
    """Test minting a web session cookie from a mobile API key."""

    def test_web_session_success(self, client, mock_database, mock_auth):
        """Valid device key should mint a web session and set the cookie."""
        mock_auth["get_api_key"].return_value = f"{MOBILE_API_KEY_PREFIX}valid_key"
        mock_database.get_mobile_device_by_api_key_hash.return_value = valid_mobile("test-device")

        mock_database.ensure_paired_client_for_mobile.return_value = {"id": "client-1"}
        mock_database.set_paired_client_session.return_value = {"id": "client-1"}
        with patch("app.api.mobile.pairing.generate_controller_session", return_value="shc_web-session-token-xyz"):
            response = client.post(
                "/api/v1/mobile/web-session",
                headers={"Authorization": f"Bearer {MOBILE_API_KEY_PREFIX}valid_key"}
            )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["session_token"] == "shc_web-session-token-xyz"
        assert "expires_at" in body
        # Cookie should be set on the response for the WebView CookieManager.
        assert "session" in response.cookies
        assert response.cookies["session"] == "shc_web-session-token-xyz"

    def test_web_session_requires_valid_key(self, client, mock_database, mock_auth):
        """Invalid mobile key should yield 401 and not mint a session."""
        mock_auth["get_api_key"].return_value = f"{MOBILE_API_KEY_PREFIX}unknown"
        mock_database.get_mobile_device_by_api_key_hash.return_value = None

        response = client.post(
            "/api/v1/mobile/web-session",
            headers={"Authorization": f"Bearer {MOBILE_API_KEY_PREFIX}unknown"}
        )

        assert response.status_code == 401

    def test_web_session_requires_scope(self, client, mock_database, mock_auth):
        """A valid mobile key cannot mint capabilities it was not granted."""
        mock_auth["get_api_key"].return_value = f"{MOBILE_API_KEY_PREFIX}limited"
        mock_database.get_mobile_device_by_api_key_hash.return_value = valid_mobile(
            "limited-device",
            scopes=["controller"],
        )

        response = client.post(
            "/api/v1/mobile/web-session",
            headers={"Authorization": f"Bearer {MOBILE_API_KEY_PREFIX}limited"},
        )

        assert response.status_code == 403
        mock_database.ensure_paired_client_for_mobile.assert_not_called()


# =============================================================================
# Notification SSE Stream Tests
# =============================================================================

# =============================================================================
# Admin Device Management Tests
# =============================================================================

class TestListDevices:
    """Test admin device listing."""

    def test_list_devices_module_structure(self, mock_database):
        """Admin device listing endpoint should exist in router."""
        from app.api.mobile import router
        routes = [r.path for r in router.routes if hasattr(r, "path")]
        assert "/devices" in routes or any("/devices" in r for r in routes)


class TestDeleteDevice:
    """Test admin device deletion."""

    def test_delete_device_module_structure(self, mock_database):
        """Admin device delete endpoint should exist in router."""
        from app.api.mobile import router
        routes = [r.path for r in router.routes if hasattr(r, "path")]
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
        from app.core.models import MobileDevice
        assert MobileDevice is not None
