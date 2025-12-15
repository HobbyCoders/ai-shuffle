"""
Audit logging service for security events
"""

import uuid
import logging
from typing import Optional, Dict, Any

from fastapi import Request

from app.db import database as db

logger = logging.getLogger(__name__)


# Event types for audit logging
class AuditEventType:
    # Authentication events
    LOGIN_SUCCESS = "login_success"
    LOGIN_FAILURE = "login_failure"
    LOGOUT = "logout"
    SESSION_EXPIRED = "session_expired"

    # 2FA events
    TOTP_SETUP_STARTED = "2fa_setup_started"
    TOTP_ENABLED = "2fa_enabled"
    TOTP_DISABLED = "2fa_disabled"
    TOTP_VERIFICATION_SUCCESS = "2fa_verification_success"
    TOTP_VERIFICATION_FAILURE = "2fa_verification_failure"
    RECOVERY_CODE_USED = "recovery_code_used"
    RECOVERY_CODES_REGENERATED = "recovery_codes_regenerated"

    # Account events
    PASSWORD_CHANGED = "password_changed"
    PASSWORD_CHANGE_FAILED = "password_change_failed"

    # API key events
    API_KEY_CREATED = "api_key_created"
    API_KEY_DELETED = "api_key_deleted"
    API_KEY_DISABLED = "api_key_disabled"
    API_KEY_ENABLED = "api_key_enabled"

    # Admin events
    ADMIN_SETUP = "admin_setup"
    SETTINGS_CHANGED = "settings_changed"


def get_client_ip(request: Request) -> str:
    """
    Get the real client IP address, respecting reverse proxy headers.

    Args:
        request: FastAPI Request object

    Returns:
        Client IP address string
    """
    # Check common proxy headers
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        # X-Forwarded-For can contain multiple IPs, take the first
        return forwarded_for.split(",")[0].strip()

    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()

    # Fall back to direct client IP
    if request.client:
        return request.client.host

    return "unknown"


def get_user_agent(request: Request) -> str:
    """
    Get the User-Agent header from the request.

    Args:
        request: FastAPI Request object

    Returns:
        User-Agent string or "unknown"
    """
    return request.headers.get("User-Agent", "unknown")


def log_event(
    event_type: str,
    request: Optional[Request] = None,
    user_id: Optional[str] = None,
    user_type: str = "admin",
    details: Optional[Dict[str, Any]] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None
) -> Dict[str, Any]:
    """
    Log a security event to the audit log.

    Args:
        event_type: Type of event (use AuditEventType constants)
        request: Optional FastAPI Request object (to extract IP/UA)
        user_id: Optional user identifier
        user_type: Type of user ("admin" or "api_user")
        details: Optional dictionary of additional event details
        ip_address: Optional IP address (overrides request extraction)
        user_agent: Optional User-Agent (overrides request extraction)

    Returns:
        The created audit log entry
    """
    event_id = str(uuid.uuid4())

    # Extract IP and UA from request if not provided
    if request:
        if not ip_address:
            ip_address = get_client_ip(request)
        if not user_agent:
            user_agent = get_user_agent(request)

    try:
        entry = db.create_audit_log(
            event_id=event_id,
            event_type=event_type,
            user_id=user_id,
            user_type=user_type,
            ip_address=ip_address,
            user_agent=user_agent,
            details=details
        )
        logger.info(f"Audit log: {event_type} for user {user_id} from {ip_address}")
        return entry
    except Exception as e:
        logger.error(f"Failed to create audit log entry: {e}")
        # Return a partial entry even on failure
        return {
            "id": event_id,
            "event_type": event_type,
            "user_id": user_id,
            "error": str(e)
        }


def log_login_success(request: Request, user_id: str, user_type: str = "admin", method: str = "password") -> Dict[str, Any]:
    """Log a successful login event."""
    return log_event(
        event_type=AuditEventType.LOGIN_SUCCESS,
        request=request,
        user_id=user_id,
        user_type=user_type,
        details={"method": method}
    )


def log_login_failure(request: Request, username: Optional[str] = None, reason: str = "invalid_credentials") -> Dict[str, Any]:
    """Log a failed login attempt."""
    return log_event(
        event_type=AuditEventType.LOGIN_FAILURE,
        request=request,
        user_id=username,
        details={"reason": reason, "attempted_username": username}
    )


def log_logout(request: Request, user_id: str, user_type: str = "admin") -> Dict[str, Any]:
    """Log a logout event."""
    return log_event(
        event_type=AuditEventType.LOGOUT,
        request=request,
        user_id=user_id,
        user_type=user_type
    )


def log_2fa_setup_started(request: Request, user_id: str) -> Dict[str, Any]:
    """Log 2FA setup initiation."""
    return log_event(
        event_type=AuditEventType.TOTP_SETUP_STARTED,
        request=request,
        user_id=user_id
    )


def log_2fa_enabled(request: Request, user_id: str) -> Dict[str, Any]:
    """Log 2FA being enabled."""
    return log_event(
        event_type=AuditEventType.TOTP_ENABLED,
        request=request,
        user_id=user_id
    )


def log_2fa_disabled(request: Request, user_id: str) -> Dict[str, Any]:
    """Log 2FA being disabled."""
    return log_event(
        event_type=AuditEventType.TOTP_DISABLED,
        request=request,
        user_id=user_id
    )


def log_2fa_verification_success(request: Request, user_id: str, method: str = "totp") -> Dict[str, Any]:
    """Log successful 2FA verification."""
    return log_event(
        event_type=AuditEventType.TOTP_VERIFICATION_SUCCESS,
        request=request,
        user_id=user_id,
        details={"method": method}
    )


def log_2fa_verification_failure(request: Request, user_id: str, method: str = "totp") -> Dict[str, Any]:
    """Log failed 2FA verification attempt."""
    return log_event(
        event_type=AuditEventType.TOTP_VERIFICATION_FAILURE,
        request=request,
        user_id=user_id,
        details={"method": method}
    )


def log_recovery_code_used(request: Request, user_id: str, remaining_codes: int) -> Dict[str, Any]:
    """Log recovery code usage."""
    return log_event(
        event_type=AuditEventType.RECOVERY_CODE_USED,
        request=request,
        user_id=user_id,
        details={"remaining_codes": remaining_codes}
    )


def log_recovery_codes_regenerated(request: Request, user_id: str) -> Dict[str, Any]:
    """Log recovery codes regeneration."""
    return log_event(
        event_type=AuditEventType.RECOVERY_CODES_REGENERATED,
        request=request,
        user_id=user_id
    )


def log_password_changed(request: Request, user_id: str) -> Dict[str, Any]:
    """Log password change."""
    return log_event(
        event_type=AuditEventType.PASSWORD_CHANGED,
        request=request,
        user_id=user_id
    )


def log_password_change_failed(request: Request, user_id: str, reason: str) -> Dict[str, Any]:
    """Log failed password change attempt."""
    return log_event(
        event_type=AuditEventType.PASSWORD_CHANGE_FAILED,
        request=request,
        user_id=user_id,
        details={"reason": reason}
    )


def log_api_key_created(request: Request, user_id: str, api_key_name: str, api_key_id: str) -> Dict[str, Any]:
    """Log API key creation."""
    return log_event(
        event_type=AuditEventType.API_KEY_CREATED,
        request=request,
        user_id=user_id,
        details={"api_key_name": api_key_name, "api_key_id": api_key_id}
    )


def log_api_key_deleted(request: Request, user_id: str, api_key_name: str, api_key_id: str) -> Dict[str, Any]:
    """Log API key deletion."""
    return log_event(
        event_type=AuditEventType.API_KEY_DELETED,
        request=request,
        user_id=user_id,
        details={"api_key_name": api_key_name, "api_key_id": api_key_id}
    )


def log_admin_setup(request: Request, username: str) -> Dict[str, Any]:
    """Log admin account setup."""
    return log_event(
        event_type=AuditEventType.ADMIN_SETUP,
        request=request,
        user_id=username,
        details={"action": "initial_setup"}
    )
