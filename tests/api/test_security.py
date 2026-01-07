"""
Comprehensive tests for the security API endpoints (2FA and audit logging).

Tests cover:
- 2FA status endpoint
- 2FA setup workflow
- 2FA verification and enable
- 2FA disable
- Recovery codes regeneration
- Audit log retrieval
- All error paths and edge cases
"""

import json
import pytest
from datetime import datetime
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi.testclient import TestClient


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def admin_user_data():
    """Sample admin user data."""
    return {
        "id": 1,
        "username": "admin",
        "password_hash": "$2b$12$testheashvalue",
        "totp_enabled": False,
        "totp_secret": None,
        "recovery_codes": None,
        "totp_verified_at": None
    }


@pytest.fixture
def admin_with_2fa():
    """Admin user with 2FA enabled."""
    return {
        "id": 1,
        "username": "admin",
        "password_hash": "$2b$12$testheashvalue",
        "totp_enabled": True,
        "totp_secret": "EXISTINGSECRET123456",
        "recovery_codes": '["hash1", "hash2", "hash3"]',
        "totp_verified_at": "2024-01-01T00:00:00Z"
    }


@pytest.fixture
def admin_with_setup_pending():
    """Admin user with 2FA setup started but not verified."""
    return {
        "id": 1,
        "username": "admin",
        "password_hash": "$2b$12$testheashvalue",
        "totp_enabled": False,
        "totp_secret": "PENDINGSECRET123456",
        "recovery_codes": None,
        "totp_verified_at": None
    }


@pytest.fixture
def sample_audit_entries():
    """Sample audit log entries."""
    return [
        {
            "id": "audit-1",
            "user_id": "admin",
            "user_type": "admin",
            "event_type": "login_success",
            "ip_address": "192.168.1.1",
            "user_agent": "Mozilla/5.0",
            "details": {"method": "password"},
            "created_at": "2024-01-01T10:00:00Z"
        },
        {
            "id": "audit-2",
            "user_id": "admin",
            "user_type": "admin",
            "event_type": "2fa_enabled",
            "ip_address": "192.168.1.1",
            "user_agent": "Mozilla/5.0",
            "details": None,
            "created_at": "2024-01-01T11:00:00Z"
        }
    ]


@pytest.fixture
def app_with_mocks():
    """Create the app with dependency overrides."""
    from app.main import app
    from app.api.auth import require_admin

    # Override the require_admin dependency
    async def mock_require_admin():
        return "test-admin-token"

    app.dependency_overrides[require_admin] = mock_require_admin

    yield app

    # Clear overrides after test
    app.dependency_overrides.clear()


@pytest.fixture
def client(app_with_mocks):
    """Create test client with mocked auth dependencies."""
    with TestClient(app_with_mocks) as test_client:
        yield test_client


@pytest.fixture
def unauthenticated_client():
    """Create test client without auth overrides for auth testing."""
    from app.main import app
    with TestClient(app) as test_client:
        yield test_client


# =============================================================================
# 2FA Status Endpoint Tests
# =============================================================================

class TestGet2FAStatus:
    """Tests for GET /api/v1/security/2fa/status endpoint."""

    def test_get_2fa_status_disabled(self, client, admin_user_data):
        """Should return 2FA status when disabled."""
        with patch("app.api.security.db") as mock_db, \
             patch("app.api.security.totp_service") as mock_totp:
            mock_db.get_admin.return_value = admin_user_data
            mock_totp.get_recovery_codes_count.return_value = 0

            response = client.get("/api/v1/security/2fa/status")

            assert response.status_code == 200
            data = response.json()
            assert data["enabled"] is False
            assert data["has_recovery_codes"] is False
            assert data["recovery_codes_count"] == 0
            assert data["verified_at"] is None

    def test_get_2fa_status_enabled(self, client, admin_with_2fa):
        """Should return 2FA status when enabled with recovery codes."""
        with patch("app.api.security.db") as mock_db, \
             patch("app.api.security.totp_service") as mock_totp:
            mock_db.get_admin.return_value = admin_with_2fa
            mock_totp.get_recovery_codes_count.return_value = 3

            response = client.get("/api/v1/security/2fa/status")

            assert response.status_code == 200
            data = response.json()
            assert data["enabled"] is True
            assert data["has_recovery_codes"] is True
            assert data["recovery_codes_count"] == 3
            assert data["verified_at"] == "2024-01-01T00:00:00Z"

    def test_get_2fa_status_admin_not_found(self, client):
        """Should return 500 when admin not found."""
        with patch("app.api.security.db") as mock_db:
            mock_db.get_admin.return_value = None

            response = client.get("/api/v1/security/2fa/status")

            assert response.status_code == 500
            assert "Admin account not found" in response.json()["detail"]

    def test_get_2fa_status_no_recovery_codes(self, client):
        """Should handle admin with 2FA enabled but no recovery codes."""
        admin = {
            "id": 1,
            "username": "admin",
            "totp_enabled": True,
            "totp_secret": "SECRET",
            "recovery_codes": None,
            "totp_verified_at": "2024-01-01T00:00:00Z"
        }
        with patch("app.api.security.db") as mock_db, \
             patch("app.api.security.totp_service") as mock_totp:
            mock_db.get_admin.return_value = admin
            mock_totp.get_recovery_codes_count.return_value = 0

            response = client.get("/api/v1/security/2fa/status")

            assert response.status_code == 200
            data = response.json()
            assert data["enabled"] is True
            assert data["has_recovery_codes"] is False
            assert data["recovery_codes_count"] == 0


# =============================================================================
# 2FA Setup Endpoint Tests
# =============================================================================

class TestSetup2FA:
    """Tests for POST /api/v1/security/2fa/setup endpoint."""

    def test_setup_2fa_success(self, client, admin_user_data):
        """Should successfully start 2FA setup."""
        with patch("app.api.security.db") as mock_db, \
             patch("app.api.security.totp_service") as mock_totp, \
             patch("app.api.security.audit_service") as mock_audit:
            mock_db.get_admin.return_value = admin_user_data
            mock_db.update_admin_totp.return_value = True
            mock_totp.generate_secret.return_value = "TESTSECRET12345678"
            mock_totp.get_totp_uri.return_value = "otpauth://totp/AI%20Hub:admin?secret=TESTSECRET12345678&issuer=AI%20Hub"
            mock_totp.generate_qr_code.return_value = "data:image/png;base64,TESTQRCODE=="
            mock_audit.log_2fa_setup_started.return_value = {"id": "audit-1"}

            response = client.post("/api/v1/security/2fa/setup")

            assert response.status_code == 200
            data = response.json()
            assert data["secret"] == "TESTSECRET12345678"
            assert "otpauth://" in data["uri"]
            assert data["qr_code"].startswith("data:image/png;base64,")

            # Verify database was updated
            mock_db.update_admin_totp.assert_called_once_with(
                totp_secret="TESTSECRET12345678",
                totp_enabled=False,
                recovery_codes=None
            )

            # Verify audit log was created
            mock_audit.log_2fa_setup_started.assert_called_once()

    def test_setup_2fa_admin_not_found(self, client):
        """Should return 500 when admin not found."""
        with patch("app.api.security.db") as mock_db:
            mock_db.get_admin.return_value = None

            response = client.post("/api/v1/security/2fa/setup")

            assert response.status_code == 500
            assert "Admin account not found" in response.json()["detail"]

    def test_setup_2fa_already_enabled(self, client, admin_with_2fa):
        """Should return 400 when 2FA is already enabled."""
        with patch("app.api.security.db") as mock_db:
            mock_db.get_admin.return_value = admin_with_2fa

            response = client.post("/api/v1/security/2fa/setup")

            assert response.status_code == 400
            assert "2FA is already enabled" in response.json()["detail"]


# =============================================================================
# 2FA Verify and Enable Endpoint Tests
# =============================================================================

class TestVerifyAndEnable2FA:
    """Tests for POST /api/v1/security/2fa/verify endpoint."""

    def test_verify_2fa_success(self, client, admin_with_setup_pending):
        """Should successfully verify and enable 2FA."""
        with patch("app.api.security.db") as mock_db, \
             patch("app.api.security.totp_service") as mock_totp, \
             patch("app.api.security.audit_service") as mock_audit:
            mock_db.get_admin.return_value = admin_with_setup_pending
            mock_db.update_admin_totp.return_value = True
            mock_totp.verify_totp.return_value = True
            mock_totp.generate_recovery_codes.return_value = (
                ["CODE-1111-AAAA", "CODE-2222-BBBB", "CODE-3333-CCCC"],
                ["hash1", "hash2", "hash3"]
            )
            mock_audit.log_2fa_enabled.return_value = {"id": "audit-2"}

            response = client.post(
                "/api/v1/security/2fa/verify",
                json={"code": "123456"}
            )

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "ok"
            assert "recovery_codes" in data
            assert len(data["recovery_codes"]) == 3

            # Verify 2FA was enabled
            mock_db.update_admin_totp.assert_called_once()
            call_args = mock_db.update_admin_totp.call_args
            assert call_args[1]["totp_enabled"] is True

            # Verify audit log
            mock_audit.log_2fa_enabled.assert_called_once()

    def test_verify_2fa_admin_not_found(self, client):
        """Should return 500 when admin not found."""
        with patch("app.api.security.db") as mock_db:
            mock_db.get_admin.return_value = None

            response = client.post(
                "/api/v1/security/2fa/verify",
                json={"code": "123456"}
            )

            assert response.status_code == 500
            assert "Admin account not found" in response.json()["detail"]

    def test_verify_2fa_already_enabled(self, client, admin_with_2fa):
        """Should return 400 when 2FA is already enabled."""
        with patch("app.api.security.db") as mock_db:
            mock_db.get_admin.return_value = admin_with_2fa

            response = client.post(
                "/api/v1/security/2fa/verify",
                json={"code": "123456"}
            )

            assert response.status_code == 400
            assert "2FA is already enabled" in response.json()["detail"]

    def test_verify_2fa_setup_not_started(self, client, admin_user_data):
        """Should return 400 when 2FA setup was not started."""
        with patch("app.api.security.db") as mock_db:
            mock_db.get_admin.return_value = admin_user_data

            response = client.post(
                "/api/v1/security/2fa/verify",
                json={"code": "123456"}
            )

            assert response.status_code == 400
            assert "2FA setup not started" in response.json()["detail"]

    def test_verify_2fa_invalid_code(self, client, admin_with_setup_pending):
        """Should return 400 when verification code is invalid."""
        with patch("app.api.security.db") as mock_db, \
             patch("app.api.security.totp_service") as mock_totp, \
             patch("app.api.security.audit_service") as mock_audit:
            mock_db.get_admin.return_value = admin_with_setup_pending
            mock_totp.verify_totp.return_value = False
            mock_audit.log_2fa_verification_failure.return_value = {"id": "audit-4"}

            response = client.post(
                "/api/v1/security/2fa/verify",
                json={"code": "000000"}
            )

            assert response.status_code == 400
            assert "Invalid verification code" in response.json()["detail"]

            # Verify failure was logged
            mock_audit.log_2fa_verification_failure.assert_called_once()

    def test_verify_2fa_invalid_code_format(self, client, admin_with_setup_pending):
        """Should return 422 for invalid code format."""
        with patch("app.api.security.db") as mock_db:
            mock_db.get_admin.return_value = admin_with_setup_pending

            # Code too short
            response = client.post(
                "/api/v1/security/2fa/verify",
                json={"code": "12345"}
            )
            assert response.status_code == 422

            # Code too long
            response = client.post(
                "/api/v1/security/2fa/verify",
                json={"code": "1234567"}
            )
            assert response.status_code == 422

            # Non-numeric code
            response = client.post(
                "/api/v1/security/2fa/verify",
                json={"code": "abcdef"}
            )
            assert response.status_code == 422


# =============================================================================
# 2FA Disable Endpoint Tests
# =============================================================================

class TestDisable2FA:
    """Tests for POST /api/v1/security/2fa/disable endpoint."""

    def test_disable_2fa_success(self, client, admin_with_2fa):
        """Should successfully disable 2FA."""
        with patch("app.api.security.db") as mock_db, \
             patch("app.api.security.totp_service") as mock_totp, \
             patch("app.api.security.auth_service") as mock_auth, \
             patch("app.api.security.audit_service") as mock_audit:
            mock_db.get_admin.return_value = admin_with_2fa
            mock_auth.verify_password.return_value = True
            mock_totp.verify_totp.return_value = True
            mock_db.update_admin_totp.return_value = True
            mock_audit.log_2fa_disabled.return_value = {"id": "audit-3"}

            response = client.post(
                "/api/v1/security/2fa/disable",
                json={"code": "123456", "password": "correct_password"}
            )

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "ok"
            assert "disabled" in data["message"].lower()

            # Verify 2FA was disabled
            mock_db.update_admin_totp.assert_called_once_with(
                totp_secret=None,
                totp_enabled=False,
                recovery_codes=None
            )

            # Verify audit log
            mock_audit.log_2fa_disabled.assert_called_once()

    def test_disable_2fa_admin_not_found(self, client):
        """Should return 500 when admin not found."""
        with patch("app.api.security.db") as mock_db:
            mock_db.get_admin.return_value = None

            response = client.post(
                "/api/v1/security/2fa/disable",
                json={"code": "123456", "password": "password"}
            )

            assert response.status_code == 500
            assert "Admin account not found" in response.json()["detail"]

    def test_disable_2fa_not_enabled(self, client, admin_user_data):
        """Should return 400 when 2FA is not enabled."""
        with patch("app.api.security.db") as mock_db:
            mock_db.get_admin.return_value = admin_user_data

            response = client.post(
                "/api/v1/security/2fa/disable",
                json={"code": "123456", "password": "password"}
            )

            assert response.status_code == 400
            assert "2FA is not enabled" in response.json()["detail"]

    def test_disable_2fa_invalid_password(self, client, admin_with_2fa):
        """Should return 401 when password is invalid."""
        with patch("app.api.security.db") as mock_db, \
             patch("app.api.security.auth_service") as mock_auth, \
             patch("app.api.security.audit_service") as mock_audit:
            mock_db.get_admin.return_value = admin_with_2fa
            mock_auth.verify_password.return_value = False
            mock_audit.log_password_change_failed.return_value = {"id": "audit-6"}

            response = client.post(
                "/api/v1/security/2fa/disable",
                json={"code": "123456", "password": "wrong_password"}
            )

            assert response.status_code == 401
            assert "Invalid password" in response.json()["detail"]

            # Verify failure was logged
            mock_audit.log_password_change_failed.assert_called_once()

    def test_disable_2fa_invalid_code(self, client, admin_with_2fa):
        """Should return 400 when TOTP code is invalid."""
        with patch("app.api.security.db") as mock_db, \
             patch("app.api.security.auth_service") as mock_auth, \
             patch("app.api.security.totp_service") as mock_totp, \
             patch("app.api.security.audit_service") as mock_audit:
            mock_db.get_admin.return_value = admin_with_2fa
            mock_auth.verify_password.return_value = True
            mock_totp.verify_totp.return_value = False
            mock_audit.log_2fa_verification_failure.return_value = {"id": "audit-4"}

            response = client.post(
                "/api/v1/security/2fa/disable",
                json={"code": "000000", "password": "correct_password"}
            )

            assert response.status_code == 400
            assert "Invalid verification code" in response.json()["detail"]

            # Verify failure was logged
            mock_audit.log_2fa_verification_failure.assert_called_once()


# =============================================================================
# Recovery Codes Regeneration Endpoint Tests
# =============================================================================

class TestRegenerateRecoveryCodes:
    """Tests for POST /api/v1/security/2fa/recovery-codes endpoint."""

    def test_regenerate_recovery_codes_success(self, client, admin_with_2fa):
        """Should successfully regenerate recovery codes."""
        with patch("app.api.security.db") as mock_db, \
             patch("app.api.security.totp_service") as mock_totp, \
             patch("app.api.security.audit_service") as mock_audit:
            mock_db.get_admin.return_value = admin_with_2fa
            mock_db.update_admin_recovery_codes.return_value = True
            mock_totp.generate_recovery_codes.return_value = (
                ["NEW-1111-AAAA", "NEW-2222-BBBB", "NEW-3333-CCCC", "NEW-4444-DDDD",
                 "NEW-5555-EEEE", "NEW-6666-FFFF", "NEW-7777-GGGG", "NEW-8888-HHHH",
                 "NEW-9999-IIII", "NEW-0000-JJJJ"],
                ["newhash1", "newhash2", "newhash3", "newhash4", "newhash5",
                 "newhash6", "newhash7", "newhash8", "newhash9", "newhash10"]
            )
            mock_audit.log_recovery_codes_regenerated.return_value = {"id": "audit-5"}

            response = client.post("/api/v1/security/2fa/recovery-codes")

            assert response.status_code == 200
            data = response.json()
            assert "codes" in data
            assert len(data["codes"]) == 10
            assert data["count"] == 10

            # Verify database was updated
            mock_db.update_admin_recovery_codes.assert_called_once()

            # Verify audit log
            mock_audit.log_recovery_codes_regenerated.assert_called_once()

    def test_regenerate_recovery_codes_admin_not_found(self, client):
        """Should return 500 when admin not found."""
        with patch("app.api.security.db") as mock_db:
            mock_db.get_admin.return_value = None

            response = client.post("/api/v1/security/2fa/recovery-codes")

            assert response.status_code == 500
            assert "Admin account not found" in response.json()["detail"]

    def test_regenerate_recovery_codes_2fa_not_enabled(self, client, admin_user_data):
        """Should return 400 when 2FA is not enabled."""
        with patch("app.api.security.db") as mock_db:
            mock_db.get_admin.return_value = admin_user_data

            response = client.post("/api/v1/security/2fa/recovery-codes")

            assert response.status_code == 400
            assert "2FA must be enabled" in response.json()["detail"]


# =============================================================================
# Audit Log Endpoint Tests
# =============================================================================

class TestGetAuditLog:
    """Tests for GET /api/v1/security/audit-log endpoint."""

    def test_get_audit_log_success(self, client, sample_audit_entries):
        """Should successfully retrieve audit logs."""
        with patch("app.api.security.db") as mock_db:
            mock_db.get_audit_logs.return_value = sample_audit_entries
            mock_db.get_audit_log_count.return_value = 2

            response = client.get("/api/v1/security/audit-log")

            assert response.status_code == 200
            data = response.json()
            assert "entries" in data
            assert len(data["entries"]) == 2
            assert data["total"] == 2
            assert data["limit"] == 50
            assert data["offset"] == 0

    def test_get_audit_log_empty(self, client):
        """Should handle empty audit log."""
        with patch("app.api.security.db") as mock_db:
            mock_db.get_audit_logs.return_value = []
            mock_db.get_audit_log_count.return_value = 0

            response = client.get("/api/v1/security/audit-log")

            assert response.status_code == 200
            data = response.json()
            assert data["entries"] == []
            assert data["total"] == 0

    def test_get_audit_log_with_pagination(self, client, sample_audit_entries):
        """Should handle pagination parameters."""
        with patch("app.api.security.db") as mock_db:
            mock_db.get_audit_logs.return_value = sample_audit_entries[:1]
            mock_db.get_audit_log_count.return_value = 2

            response = client.get("/api/v1/security/audit-log?limit=1&offset=0")

            assert response.status_code == 200
            data = response.json()
            assert len(data["entries"]) == 1
            assert data["total"] == 2
            assert data["limit"] == 1
            assert data["offset"] == 0

            # Verify database was called with correct params
            mock_db.get_audit_logs.assert_called_with(
                event_type=None,
                limit=1,
                offset=0
            )

    def test_get_audit_log_with_event_type_filter(self, client):
        """Should filter by event type."""
        with patch("app.api.security.db") as mock_db:
            mock_db.get_audit_logs.return_value = [
                {
                    "id": "audit-1",
                    "user_id": "admin",
                    "user_type": "admin",
                    "event_type": "login_success",
                    "ip_address": "192.168.1.1",
                    "user_agent": "Mozilla/5.0",
                    "details": None,
                    "created_at": "2024-01-01T10:00:00Z"
                }
            ]
            mock_db.get_audit_log_count.return_value = 1

            response = client.get("/api/v1/security/audit-log?event_type=login_success")

            assert response.status_code == 200
            data = response.json()
            assert len(data["entries"]) == 1
            assert data["entries"][0]["event_type"] == "login_success"

            # Verify database was called with event_type filter
            mock_db.get_audit_logs.assert_called_with(
                event_type="login_success",
                limit=50,
                offset=0
            )
            mock_db.get_audit_log_count.assert_called_with(event_type="login_success")

    def test_get_audit_log_limit_cap(self, client):
        """Should cap limit at 500."""
        with patch("app.api.security.db") as mock_db:
            mock_db.get_audit_logs.return_value = []
            mock_db.get_audit_log_count.return_value = 0

            response = client.get("/api/v1/security/audit-log?limit=1000")

            assert response.status_code == 200
            data = response.json()
            assert data["limit"] == 500

            # Verify limit was capped
            mock_db.get_audit_logs.assert_called_with(
                event_type=None,
                limit=500,
                offset=0
            )

    def test_get_audit_log_entry_structure(self, client, sample_audit_entries):
        """Should return properly structured audit log entries."""
        with patch("app.api.security.db") as mock_db:
            mock_db.get_audit_logs.return_value = sample_audit_entries
            mock_db.get_audit_log_count.return_value = 2

            response = client.get("/api/v1/security/audit-log")

            assert response.status_code == 200
            data = response.json()
            entry = data["entries"][0]

            # Check all required fields
            assert "id" in entry
            assert "event_type" in entry
            assert "created_at" in entry
            assert "user_type" in entry

            # Check optional fields exist
            assert "user_id" in entry
            assert "ip_address" in entry
            assert "user_agent" in entry
            assert "details" in entry


# =============================================================================
# Integration Tests
# =============================================================================

class TestSecurityIntegration:
    """Integration tests for the full 2FA workflow."""

    def test_full_2fa_setup_workflow(self, client, admin_user_data, admin_with_setup_pending):
        """Test the complete 2FA setup flow: setup -> verify -> enable."""
        with patch("app.api.security.db") as mock_db, \
             patch("app.api.security.totp_service") as mock_totp, \
             patch("app.api.security.audit_service") as mock_audit:
            # Step 1: Start setup
            mock_db.get_admin.return_value = admin_user_data
            mock_db.update_admin_totp.return_value = True
            mock_totp.generate_secret.return_value = "TESTSECRET12345678"
            mock_totp.get_totp_uri.return_value = "otpauth://totp/AI%20Hub:admin?secret=TESTSECRET12345678&issuer=AI%20Hub"
            mock_totp.generate_qr_code.return_value = "data:image/png;base64,TESTQRCODE=="
            mock_audit.log_2fa_setup_started.return_value = {"id": "audit-1"}

            setup_response = client.post("/api/v1/security/2fa/setup")
            assert setup_response.status_code == 200
            assert "secret" in setup_response.json()

            # Step 2: Verify and enable
            mock_db.get_admin.return_value = admin_with_setup_pending
            mock_totp.verify_totp.return_value = True
            mock_totp.generate_recovery_codes.return_value = (
                ["CODE-1111-AAAA", "CODE-2222-BBBB", "CODE-3333-CCCC"],
                ["hash1", "hash2", "hash3"]
            )
            mock_audit.log_2fa_enabled.return_value = {"id": "audit-2"}

            verify_response = client.post(
                "/api/v1/security/2fa/verify",
                json={"code": "123456"}
            )
            assert verify_response.status_code == 200
            assert verify_response.json()["status"] == "ok"
            assert "recovery_codes" in verify_response.json()

    def test_2fa_disable_requires_both_password_and_code(self, client, admin_with_2fa):
        """Disabling 2FA requires both valid password and TOTP code."""
        with patch("app.api.security.db") as mock_db, \
             patch("app.api.security.auth_service") as mock_auth, \
             patch("app.api.security.totp_service") as mock_totp, \
             patch("app.api.security.audit_service") as mock_audit:
            mock_db.get_admin.return_value = admin_with_2fa
            mock_audit.log_password_change_failed.return_value = {"id": "audit-6"}
            mock_audit.log_2fa_verification_failure.return_value = {"id": "audit-4"}

            # Test with wrong password
            mock_auth.verify_password.return_value = False
            response = client.post(
                "/api/v1/security/2fa/disable",
                json={"code": "123456", "password": "wrong"}
            )
            assert response.status_code == 401

            # Test with wrong code
            mock_auth.verify_password.return_value = True
            mock_totp.verify_totp.return_value = False
            response = client.post(
                "/api/v1/security/2fa/disable",
                json={"code": "000000", "password": "correct"}
            )
            assert response.status_code == 400


# =============================================================================
# Authentication/Authorization Tests
# =============================================================================

class TestSecurityAuth:
    """Tests for authentication requirements on security endpoints."""

    def test_2fa_status_requires_admin(self, unauthenticated_client):
        """2FA status endpoint should require admin auth."""
        response = unauthenticated_client.get("/api/v1/security/2fa/status")
        assert response.status_code == 403

    def test_2fa_setup_requires_admin(self, unauthenticated_client):
        """2FA setup endpoint should require admin auth."""
        response = unauthenticated_client.post("/api/v1/security/2fa/setup")
        assert response.status_code == 403

    def test_2fa_verify_requires_admin(self, unauthenticated_client):
        """2FA verify endpoint should require admin auth."""
        response = unauthenticated_client.post(
            "/api/v1/security/2fa/verify",
            json={"code": "123456"}
        )
        assert response.status_code == 403

    def test_2fa_disable_requires_admin(self, unauthenticated_client):
        """2FA disable endpoint should require admin auth."""
        response = unauthenticated_client.post(
            "/api/v1/security/2fa/disable",
            json={"code": "123456", "password": "password"}
        )
        assert response.status_code == 403

    def test_recovery_codes_requires_admin(self, unauthenticated_client):
        """Recovery codes endpoint should require admin auth."""
        response = unauthenticated_client.post("/api/v1/security/2fa/recovery-codes")
        assert response.status_code == 403

    def test_audit_log_requires_admin(self, unauthenticated_client):
        """Audit log endpoint should require admin auth."""
        response = unauthenticated_client.get("/api/v1/security/audit-log")
        assert response.status_code == 403


# =============================================================================
# Edge Cases and Error Handling
# =============================================================================

class TestEdgeCases:
    """Tests for edge cases and error handling."""

    def test_audit_log_with_null_optional_fields(self, client):
        """Should handle audit entries with null optional fields."""
        entries = [
            {
                "id": "audit-1",
                "user_id": None,
                "user_type": "admin",
                "event_type": "system_event",
                "ip_address": None,
                "user_agent": None,
                "details": None,
                "created_at": "2024-01-01T10:00:00Z"
            }
        ]
        with patch("app.api.security.db") as mock_db:
            mock_db.get_audit_logs.return_value = entries
            mock_db.get_audit_log_count.return_value = 1

            response = client.get("/api/v1/security/audit-log")

            assert response.status_code == 200
            data = response.json()
            entry = data["entries"][0]
            assert entry["user_id"] is None
            assert entry["ip_address"] is None
            assert entry["user_agent"] is None
            assert entry["details"] is None

    def test_audit_log_with_complex_details(self, client):
        """Should handle audit entries with complex details objects."""
        entries = [
            {
                "id": "audit-1",
                "user_id": "admin",
                "user_type": "admin",
                "event_type": "settings_changed",
                "ip_address": "192.168.1.1",
                "user_agent": "Mozilla/5.0",
                "details": {
                    "changes": [
                        {"field": "theme", "old": "light", "new": "dark"},
                        {"field": "language", "old": "en", "new": "es"}
                    ],
                    "timestamp": "2024-01-01T10:00:00Z"
                },
                "created_at": "2024-01-01T10:00:00Z"
            }
        ]
        with patch("app.api.security.db") as mock_db:
            mock_db.get_audit_logs.return_value = entries
            mock_db.get_audit_log_count.return_value = 1

            response = client.get("/api/v1/security/audit-log")

            assert response.status_code == 200
            data = response.json()
            entry = data["entries"][0]
            assert entry["details"]["changes"][0]["field"] == "theme"

    def test_2fa_status_empty_recovery_codes_string(self, client):
        """Should handle empty recovery codes string."""
        admin = {
            "id": 1,
            "username": "admin",
            "totp_enabled": True,
            "totp_secret": "SECRET",
            "recovery_codes": "",
            "totp_verified_at": "2024-01-01T00:00:00Z"
        }
        with patch("app.api.security.db") as mock_db, \
             patch("app.api.security.totp_service") as mock_totp:
            mock_db.get_admin.return_value = admin
            mock_totp.get_recovery_codes_count.return_value = 0

            response = client.get("/api/v1/security/2fa/status")

            assert response.status_code == 200
            data = response.json()
            assert data["has_recovery_codes"] is False
            assert data["recovery_codes_count"] == 0

    def test_verify_2fa_with_empty_secret(self, client):
        """Should handle empty secret during verification."""
        admin = {
            "id": 1,
            "username": "admin",
            "totp_enabled": False,
            "totp_secret": "",  # Empty secret
            "recovery_codes": None
        }
        with patch("app.api.security.db") as mock_db:
            mock_db.get_admin.return_value = admin

            response = client.post(
                "/api/v1/security/2fa/verify",
                json={"code": "123456"}
            )

            assert response.status_code == 400
            assert "2FA setup not started" in response.json()["detail"]


# =============================================================================
# Module Import Tests
# =============================================================================

class TestSecurityModuleImports:
    """Verify security module can be imported correctly."""

    def test_security_module_imports(self):
        """Security module should import without errors."""
        from app.api import security
        assert security is not None

    def test_security_router_exists(self):
        """Security router should exist."""
        from app.api.security import router
        assert router is not None
        assert router.prefix == "/api/v1/security"
