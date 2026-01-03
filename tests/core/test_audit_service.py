"""
Unit tests for audit_service module.

Tests cover:
- AuditEventType constants
- Client IP extraction from request headers
- User-Agent extraction
- log_event function with all code paths
- All convenience logging functions
- Error handling when database fails
"""

import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from typing import Dict, Any

from app.core.audit_service import (
    AuditEventType,
    get_client_ip,
    get_user_agent,
    log_event,
    log_login_success,
    log_login_failure,
    log_logout,
    log_2fa_setup_started,
    log_2fa_enabled,
    log_2fa_disabled,
    log_2fa_verification_success,
    log_2fa_verification_failure,
    log_recovery_code_used,
    log_recovery_codes_regenerated,
    log_password_changed,
    log_password_change_failed,
    log_api_key_created,
    log_api_key_deleted,
    log_admin_setup,
)


# =============================================================================
# Test Fixtures
# =============================================================================

@pytest.fixture
def mock_request():
    """Create a mock FastAPI Request object."""
    request = MagicMock()
    request.headers = {}
    request.client = MagicMock()
    request.client.host = "127.0.0.1"
    return request


@pytest.fixture
def mock_request_with_proxy_headers():
    """Create a mock request with proxy headers."""
    request = MagicMock()
    request.headers = {
        "X-Forwarded-For": "203.0.113.195, 70.41.3.18, 150.172.238.178",
        "X-Real-IP": "203.0.113.100",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    request.client = MagicMock()
    request.client.host = "10.0.0.1"
    return request


@pytest.fixture
def mock_db_create_audit_log():
    """Mock the database create_audit_log function."""
    with patch("app.core.audit_service.db.create_audit_log") as mock:
        mock.return_value = {
            "id": "test-event-id",
            "event_type": "test_event",
            "user_id": "test-user",
            "user_type": "admin",
            "ip_address": "127.0.0.1",
            "user_agent": "test-agent",
            "details": {}
        }
        yield mock


# =============================================================================
# Test AuditEventType Constants
# =============================================================================

class TestAuditEventType:
    """Test audit event type constants."""

    def test_authentication_events(self):
        """Should define authentication event types."""
        assert AuditEventType.LOGIN_SUCCESS == "login_success"
        assert AuditEventType.LOGIN_FAILURE == "login_failure"
        assert AuditEventType.LOGOUT == "logout"
        assert AuditEventType.SESSION_EXPIRED == "session_expired"

    def test_2fa_events(self):
        """Should define 2FA event types."""
        assert AuditEventType.TOTP_SETUP_STARTED == "2fa_setup_started"
        assert AuditEventType.TOTP_ENABLED == "2fa_enabled"
        assert AuditEventType.TOTP_DISABLED == "2fa_disabled"
        assert AuditEventType.TOTP_VERIFICATION_SUCCESS == "2fa_verification_success"
        assert AuditEventType.TOTP_VERIFICATION_FAILURE == "2fa_verification_failure"
        assert AuditEventType.RECOVERY_CODE_USED == "recovery_code_used"
        assert AuditEventType.RECOVERY_CODES_REGENERATED == "recovery_codes_regenerated"

    def test_account_events(self):
        """Should define account event types."""
        assert AuditEventType.PASSWORD_CHANGED == "password_changed"
        assert AuditEventType.PASSWORD_CHANGE_FAILED == "password_change_failed"

    def test_api_key_events(self):
        """Should define API key event types."""
        assert AuditEventType.API_KEY_CREATED == "api_key_created"
        assert AuditEventType.API_KEY_DELETED == "api_key_deleted"
        assert AuditEventType.API_KEY_DISABLED == "api_key_disabled"
        assert AuditEventType.API_KEY_ENABLED == "api_key_enabled"

    def test_admin_events(self):
        """Should define admin event types."""
        assert AuditEventType.ADMIN_SETUP == "admin_setup"
        assert AuditEventType.SETTINGS_CHANGED == "settings_changed"


# =============================================================================
# Test get_client_ip
# =============================================================================

class TestGetClientIp:
    """Test client IP extraction from request."""

    def test_get_client_ip_from_x_forwarded_for(self, mock_request_with_proxy_headers):
        """Should extract first IP from X-Forwarded-For header."""
        ip = get_client_ip(mock_request_with_proxy_headers)
        assert ip == "203.0.113.195"

    def test_get_client_ip_from_x_forwarded_for_single(self, mock_request):
        """Should handle single IP in X-Forwarded-For."""
        mock_request.headers = {"X-Forwarded-For": "192.168.1.100"}
        ip = get_client_ip(mock_request)
        assert ip == "192.168.1.100"

    def test_get_client_ip_from_x_forwarded_for_with_spaces(self, mock_request):
        """Should trim whitespace from X-Forwarded-For."""
        mock_request.headers = {"X-Forwarded-For": "  192.168.1.100  , 10.0.0.1"}
        ip = get_client_ip(mock_request)
        assert ip == "192.168.1.100"

    def test_get_client_ip_from_x_real_ip(self, mock_request):
        """Should use X-Real-IP when X-Forwarded-For is not present."""
        mock_request.headers = {"X-Real-IP": "  203.0.113.50  "}
        ip = get_client_ip(mock_request)
        assert ip == "203.0.113.50"

    def test_get_client_ip_prefers_x_forwarded_for_over_x_real_ip(self, mock_request):
        """Should prefer X-Forwarded-For over X-Real-IP."""
        mock_request.headers = {
            "X-Forwarded-For": "10.10.10.10",
            "X-Real-IP": "20.20.20.20"
        }
        ip = get_client_ip(mock_request)
        assert ip == "10.10.10.10"

    def test_get_client_ip_from_request_client(self, mock_request):
        """Should fall back to request.client.host."""
        mock_request.headers = {}
        mock_request.client.host = "192.168.0.1"
        ip = get_client_ip(mock_request)
        assert ip == "192.168.0.1"

    def test_get_client_ip_unknown_when_no_client(self, mock_request):
        """Should return 'unknown' when no client info available."""
        mock_request.headers = {}
        mock_request.client = None
        ip = get_client_ip(mock_request)
        assert ip == "unknown"


# =============================================================================
# Test get_user_agent
# =============================================================================

class TestGetUserAgent:
    """Test User-Agent extraction from request."""

    def test_get_user_agent_from_header(self, mock_request_with_proxy_headers):
        """Should extract User-Agent from header."""
        ua = get_user_agent(mock_request_with_proxy_headers)
        assert ua == "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

    def test_get_user_agent_unknown_when_missing(self, mock_request):
        """Should return 'unknown' when User-Agent is missing."""
        mock_request.headers = {}
        ua = get_user_agent(mock_request)
        assert ua == "unknown"

    def test_get_user_agent_empty_string(self, mock_request):
        """Should return empty string if that's the header value."""
        mock_request.headers = {"User-Agent": ""}
        ua = get_user_agent(mock_request)
        assert ua == ""


# =============================================================================
# Test log_event
# =============================================================================

class TestLogEvent:
    """Test the core log_event function."""

    def test_log_event_basic(self, mock_db_create_audit_log):
        """Should create audit log with basic parameters."""
        result = log_event(
            event_type=AuditEventType.LOGIN_SUCCESS,
            user_id="test-user"
        )

        mock_db_create_audit_log.assert_called_once()
        call_kwargs = mock_db_create_audit_log.call_args[1]
        assert call_kwargs["event_type"] == "login_success"
        assert call_kwargs["user_id"] == "test-user"
        assert call_kwargs["user_type"] == "admin"
        assert "id" in result

    def test_log_event_with_request(self, mock_request, mock_db_create_audit_log):
        """Should extract IP and UA from request."""
        mock_request.headers = {"User-Agent": "TestAgent/1.0"}
        mock_request.client.host = "192.168.1.1"

        result = log_event(
            event_type=AuditEventType.LOGIN_SUCCESS,
            request=mock_request,
            user_id="test-user"
        )

        call_kwargs = mock_db_create_audit_log.call_args[1]
        assert call_kwargs["ip_address"] == "192.168.1.1"
        assert call_kwargs["user_agent"] == "TestAgent/1.0"

    def test_log_event_with_explicit_ip_and_ua(self, mock_request, mock_db_create_audit_log):
        """Should use explicit IP and UA over request extraction."""
        mock_request.headers = {"User-Agent": "RequestAgent"}
        mock_request.client.host = "10.0.0.1"

        result = log_event(
            event_type=AuditEventType.LOGIN_SUCCESS,
            request=mock_request,
            user_id="test-user",
            ip_address="8.8.8.8",
            user_agent="ExplicitAgent"
        )

        call_kwargs = mock_db_create_audit_log.call_args[1]
        assert call_kwargs["ip_address"] == "8.8.8.8"
        assert call_kwargs["user_agent"] == "ExplicitAgent"

    def test_log_event_with_details(self, mock_db_create_audit_log):
        """Should pass details to database."""
        details = {"method": "password", "extra_info": "test"}

        log_event(
            event_type=AuditEventType.LOGIN_SUCCESS,
            user_id="test-user",
            details=details
        )

        call_kwargs = mock_db_create_audit_log.call_args[1]
        assert call_kwargs["details"] == details

    def test_log_event_with_user_type(self, mock_db_create_audit_log):
        """Should pass custom user_type."""
        log_event(
            event_type=AuditEventType.LOGIN_SUCCESS,
            user_id="api-user-123",
            user_type="api_user"
        )

        call_kwargs = mock_db_create_audit_log.call_args[1]
        assert call_kwargs["user_type"] == "api_user"

    def test_log_event_without_request(self, mock_db_create_audit_log):
        """Should work without request object."""
        result = log_event(
            event_type=AuditEventType.LOGOUT,
            user_id="test-user",
            ip_address="1.2.3.4",
            user_agent="CLI/1.0"
        )

        call_kwargs = mock_db_create_audit_log.call_args[1]
        assert call_kwargs["ip_address"] == "1.2.3.4"
        assert call_kwargs["user_agent"] == "CLI/1.0"

    def test_log_event_generates_unique_event_id(self, mock_db_create_audit_log):
        """Should generate unique event IDs."""
        with patch("app.core.audit_service.uuid.uuid4") as mock_uuid:
            mock_uuid.return_value = MagicMock()
            mock_uuid.return_value.__str__ = lambda self: "unique-event-id"

            log_event(event_type=AuditEventType.LOGOUT, user_id="test")

            call_kwargs = mock_db_create_audit_log.call_args[1]
            assert call_kwargs["event_id"] == "unique-event-id"

    def test_log_event_database_error_returns_partial_entry(self):
        """Should return partial entry when database fails."""
        with patch("app.core.audit_service.db.create_audit_log") as mock_db:
            mock_db.side_effect = Exception("Database connection failed")

            result = log_event(
                event_type=AuditEventType.LOGIN_FAILURE,
                user_id="test-user"
            )

            assert "error" in result
            assert result["event_type"] == "login_failure"
            assert result["user_id"] == "test-user"
            assert "Database connection failed" in result["error"]

    def test_log_event_logs_info_on_success(self, mock_db_create_audit_log, caplog):
        """Should log info message on successful audit."""
        import logging
        with caplog.at_level(logging.INFO):
            log_event(
                event_type=AuditEventType.LOGIN_SUCCESS,
                user_id="test-user",
                ip_address="192.168.1.1"
            )

        assert "Audit log: login_success" in caplog.text
        assert "test-user" in caplog.text

    def test_log_event_logs_error_on_failure(self, caplog):
        """Should log error message when database fails."""
        import logging
        with patch("app.core.audit_service.db.create_audit_log") as mock_db:
            mock_db.side_effect = Exception("DB error")

            with caplog.at_level(logging.ERROR):
                log_event(event_type=AuditEventType.LOGOUT, user_id="test")

        assert "Failed to create audit log entry" in caplog.text


# =============================================================================
# Test Convenience Logging Functions
# =============================================================================

class TestLogLoginSuccess:
    """Test log_login_success function."""

    def test_log_login_success_basic(self, mock_request, mock_db_create_audit_log):
        """Should log successful login."""
        result = log_login_success(mock_request, "user-123")

        call_kwargs = mock_db_create_audit_log.call_args[1]
        assert call_kwargs["event_type"] == AuditEventType.LOGIN_SUCCESS
        assert call_kwargs["user_id"] == "user-123"
        assert call_kwargs["user_type"] == "admin"
        assert call_kwargs["details"]["method"] == "password"

    def test_log_login_success_with_custom_method(self, mock_request, mock_db_create_audit_log):
        """Should log login with custom method."""
        log_login_success(mock_request, "user-123", method="totp")

        call_kwargs = mock_db_create_audit_log.call_args[1]
        assert call_kwargs["details"]["method"] == "totp"

    def test_log_login_success_with_user_type(self, mock_request, mock_db_create_audit_log):
        """Should log login with custom user type."""
        log_login_success(mock_request, "user-123", user_type="api_user")

        call_kwargs = mock_db_create_audit_log.call_args[1]
        assert call_kwargs["user_type"] == "api_user"


class TestLogLoginFailure:
    """Test log_login_failure function."""

    def test_log_login_failure_basic(self, mock_request, mock_db_create_audit_log):
        """Should log failed login."""
        result = log_login_failure(mock_request)

        call_kwargs = mock_db_create_audit_log.call_args[1]
        assert call_kwargs["event_type"] == AuditEventType.LOGIN_FAILURE
        assert call_kwargs["details"]["reason"] == "invalid_credentials"

    def test_log_login_failure_with_username(self, mock_request, mock_db_create_audit_log):
        """Should log failed login with attempted username."""
        log_login_failure(mock_request, username="baduser")

        call_kwargs = mock_db_create_audit_log.call_args[1]
        assert call_kwargs["user_id"] == "baduser"
        assert call_kwargs["details"]["attempted_username"] == "baduser"

    def test_log_login_failure_with_reason(self, mock_request, mock_db_create_audit_log):
        """Should log failed login with custom reason."""
        log_login_failure(mock_request, reason="account_locked")

        call_kwargs = mock_db_create_audit_log.call_args[1]
        assert call_kwargs["details"]["reason"] == "account_locked"


class TestLogLogout:
    """Test log_logout function."""

    def test_log_logout_basic(self, mock_request, mock_db_create_audit_log):
        """Should log logout event."""
        log_logout(mock_request, "user-123")

        call_kwargs = mock_db_create_audit_log.call_args[1]
        assert call_kwargs["event_type"] == AuditEventType.LOGOUT
        assert call_kwargs["user_id"] == "user-123"
        assert call_kwargs["user_type"] == "admin"

    def test_log_logout_with_user_type(self, mock_request, mock_db_create_audit_log):
        """Should log logout with custom user type."""
        log_logout(mock_request, "api-user-456", user_type="api_user")

        call_kwargs = mock_db_create_audit_log.call_args[1]
        assert call_kwargs["user_type"] == "api_user"


class TestLog2faSetupStarted:
    """Test log_2fa_setup_started function."""

    def test_log_2fa_setup_started(self, mock_request, mock_db_create_audit_log):
        """Should log 2FA setup initiation."""
        log_2fa_setup_started(mock_request, "user-123")

        call_kwargs = mock_db_create_audit_log.call_args[1]
        assert call_kwargs["event_type"] == AuditEventType.TOTP_SETUP_STARTED
        assert call_kwargs["user_id"] == "user-123"


class TestLog2faEnabled:
    """Test log_2fa_enabled function."""

    def test_log_2fa_enabled(self, mock_request, mock_db_create_audit_log):
        """Should log 2FA being enabled."""
        log_2fa_enabled(mock_request, "user-123")

        call_kwargs = mock_db_create_audit_log.call_args[1]
        assert call_kwargs["event_type"] == AuditEventType.TOTP_ENABLED
        assert call_kwargs["user_id"] == "user-123"


class TestLog2faDisabled:
    """Test log_2fa_disabled function."""

    def test_log_2fa_disabled(self, mock_request, mock_db_create_audit_log):
        """Should log 2FA being disabled."""
        log_2fa_disabled(mock_request, "user-123")

        call_kwargs = mock_db_create_audit_log.call_args[1]
        assert call_kwargs["event_type"] == AuditEventType.TOTP_DISABLED
        assert call_kwargs["user_id"] == "user-123"


class TestLog2faVerificationSuccess:
    """Test log_2fa_verification_success function."""

    def test_log_2fa_verification_success_default_method(self, mock_request, mock_db_create_audit_log):
        """Should log successful 2FA verification with default method."""
        log_2fa_verification_success(mock_request, "user-123")

        call_kwargs = mock_db_create_audit_log.call_args[1]
        assert call_kwargs["event_type"] == AuditEventType.TOTP_VERIFICATION_SUCCESS
        assert call_kwargs["user_id"] == "user-123"
        assert call_kwargs["details"]["method"] == "totp"

    def test_log_2fa_verification_success_custom_method(self, mock_request, mock_db_create_audit_log):
        """Should log successful 2FA verification with custom method."""
        log_2fa_verification_success(mock_request, "user-123", method="recovery_code")

        call_kwargs = mock_db_create_audit_log.call_args[1]
        assert call_kwargs["details"]["method"] == "recovery_code"


class TestLog2faVerificationFailure:
    """Test log_2fa_verification_failure function."""

    def test_log_2fa_verification_failure_default_method(self, mock_request, mock_db_create_audit_log):
        """Should log failed 2FA verification with default method."""
        log_2fa_verification_failure(mock_request, "user-123")

        call_kwargs = mock_db_create_audit_log.call_args[1]
        assert call_kwargs["event_type"] == AuditEventType.TOTP_VERIFICATION_FAILURE
        assert call_kwargs["user_id"] == "user-123"
        assert call_kwargs["details"]["method"] == "totp"

    def test_log_2fa_verification_failure_custom_method(self, mock_request, mock_db_create_audit_log):
        """Should log failed 2FA verification with custom method."""
        log_2fa_verification_failure(mock_request, "user-123", method="recovery_code")

        call_kwargs = mock_db_create_audit_log.call_args[1]
        assert call_kwargs["details"]["method"] == "recovery_code"


class TestLogRecoveryCodeUsed:
    """Test log_recovery_code_used function."""

    def test_log_recovery_code_used(self, mock_request, mock_db_create_audit_log):
        """Should log recovery code usage."""
        log_recovery_code_used(mock_request, "user-123", remaining_codes=7)

        call_kwargs = mock_db_create_audit_log.call_args[1]
        assert call_kwargs["event_type"] == AuditEventType.RECOVERY_CODE_USED
        assert call_kwargs["user_id"] == "user-123"
        assert call_kwargs["details"]["remaining_codes"] == 7


class TestLogRecoveryCodesRegenerated:
    """Test log_recovery_codes_regenerated function."""

    def test_log_recovery_codes_regenerated(self, mock_request, mock_db_create_audit_log):
        """Should log recovery codes regeneration."""
        log_recovery_codes_regenerated(mock_request, "user-123")

        call_kwargs = mock_db_create_audit_log.call_args[1]
        assert call_kwargs["event_type"] == AuditEventType.RECOVERY_CODES_REGENERATED
        assert call_kwargs["user_id"] == "user-123"


class TestLogPasswordChanged:
    """Test log_password_changed function."""

    def test_log_password_changed(self, mock_request, mock_db_create_audit_log):
        """Should log password change."""
        log_password_changed(mock_request, "user-123")

        call_kwargs = mock_db_create_audit_log.call_args[1]
        assert call_kwargs["event_type"] == AuditEventType.PASSWORD_CHANGED
        assert call_kwargs["user_id"] == "user-123"


class TestLogPasswordChangeFailed:
    """Test log_password_change_failed function."""

    def test_log_password_change_failed(self, mock_request, mock_db_create_audit_log):
        """Should log failed password change."""
        log_password_change_failed(mock_request, "user-123", reason="weak_password")

        call_kwargs = mock_db_create_audit_log.call_args[1]
        assert call_kwargs["event_type"] == AuditEventType.PASSWORD_CHANGE_FAILED
        assert call_kwargs["user_id"] == "user-123"
        assert call_kwargs["details"]["reason"] == "weak_password"


class TestLogApiKeyCreated:
    """Test log_api_key_created function."""

    def test_log_api_key_created(self, mock_request, mock_db_create_audit_log):
        """Should log API key creation."""
        log_api_key_created(mock_request, "user-123", "My API Key", "key-456")

        call_kwargs = mock_db_create_audit_log.call_args[1]
        assert call_kwargs["event_type"] == AuditEventType.API_KEY_CREATED
        assert call_kwargs["user_id"] == "user-123"
        assert call_kwargs["details"]["api_key_name"] == "My API Key"
        assert call_kwargs["details"]["api_key_id"] == "key-456"


class TestLogApiKeyDeleted:
    """Test log_api_key_deleted function."""

    def test_log_api_key_deleted(self, mock_request, mock_db_create_audit_log):
        """Should log API key deletion."""
        log_api_key_deleted(mock_request, "user-123", "Old API Key", "key-789")

        call_kwargs = mock_db_create_audit_log.call_args[1]
        assert call_kwargs["event_type"] == AuditEventType.API_KEY_DELETED
        assert call_kwargs["user_id"] == "user-123"
        assert call_kwargs["details"]["api_key_name"] == "Old API Key"
        assert call_kwargs["details"]["api_key_id"] == "key-789"


class TestLogAdminSetup:
    """Test log_admin_setup function."""

    def test_log_admin_setup(self, mock_request, mock_db_create_audit_log):
        """Should log admin account setup."""
        log_admin_setup(mock_request, "admin")

        call_kwargs = mock_db_create_audit_log.call_args[1]
        assert call_kwargs["event_type"] == AuditEventType.ADMIN_SETUP
        assert call_kwargs["user_id"] == "admin"
        assert call_kwargs["details"]["action"] == "initial_setup"


# =============================================================================
# Integration-style Tests
# =============================================================================

class TestLogEventIntegration:
    """Integration tests for log_event with various scenarios."""

    def test_log_event_with_all_parameters(self, mock_request, mock_db_create_audit_log):
        """Should handle all parameters correctly."""
        mock_request.headers = {"User-Agent": "Integration/Test"}
        mock_request.client.host = "10.0.0.1"

        result = log_event(
            event_type=AuditEventType.SETTINGS_CHANGED,
            request=mock_request,
            user_id="admin-user",
            user_type="admin",
            details={"setting": "theme", "old_value": "light", "new_value": "dark"},
            ip_address="203.0.113.50",  # Should override request IP
            user_agent="Admin/2.0"  # Should override request UA
        )

        call_kwargs = mock_db_create_audit_log.call_args[1]
        assert call_kwargs["event_type"] == AuditEventType.SETTINGS_CHANGED
        assert call_kwargs["user_id"] == "admin-user"
        assert call_kwargs["user_type"] == "admin"
        assert call_kwargs["ip_address"] == "203.0.113.50"
        assert call_kwargs["user_agent"] == "Admin/2.0"
        assert call_kwargs["details"]["setting"] == "theme"

    def test_log_event_extracts_from_request_when_explicit_not_provided(
        self, mock_request, mock_db_create_audit_log
    ):
        """Should extract IP/UA from request when not explicitly provided."""
        mock_request.headers = {
            "X-Forwarded-For": "192.168.100.100",
            "User-Agent": "Browser/1.0"
        }

        log_event(
            event_type=AuditEventType.LOGOUT,
            request=mock_request,
            user_id="user-123"
        )

        call_kwargs = mock_db_create_audit_log.call_args[1]
        assert call_kwargs["ip_address"] == "192.168.100.100"
        assert call_kwargs["user_agent"] == "Browser/1.0"

    def test_log_event_with_none_values(self, mock_db_create_audit_log):
        """Should handle None values correctly."""
        log_event(
            event_type=AuditEventType.SESSION_EXPIRED,
            request=None,
            user_id=None,
            user_type="admin",
            details=None,
            ip_address=None,
            user_agent=None
        )

        call_kwargs = mock_db_create_audit_log.call_args[1]
        assert call_kwargs["user_id"] is None
        assert call_kwargs["ip_address"] is None
        assert call_kwargs["user_agent"] is None
        assert call_kwargs["details"] is None
