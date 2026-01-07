"""
Security API routes for 2FA and audit logging
"""

import json
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Depends, status

from app.core.models import (
    TwoFactorSetupResponse,
    TwoFactorVerifyRequest,
    TwoFactorDisableRequest,
    TwoFactorStatusResponse,
    RecoveryCodesResponse,
    AuditLogResponse,
    AuditLogEntry
)
from app.core import totp_service
from app.core import audit_service
from app.core.auth import auth_service
from app.api.auth import require_admin
from app.db import database as db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/security", tags=["Security"])


# ============================================================================
# 2FA Setup and Management
# ============================================================================

@router.get("/2fa/status", response_model=TwoFactorStatusResponse)
async def get_2fa_status(token: str = Depends(require_admin)):
    """
    Get current 2FA status for the admin account.

    Returns whether 2FA is enabled and if recovery codes exist.
    """
    admin = db.get_admin()
    if not admin:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Admin account not found"
        )

    recovery_count = 0
    if admin.get("recovery_codes"):
        recovery_count = totp_service.get_recovery_codes_count(admin["recovery_codes"])

    return TwoFactorStatusResponse(
        enabled=bool(admin.get("totp_enabled")),
        has_recovery_codes=bool(admin.get("recovery_codes")),
        recovery_codes_count=recovery_count,
        verified_at=admin.get("totp_verified_at")
    )


@router.post("/2fa/setup", response_model=TwoFactorSetupResponse)
async def setup_2fa(request: Request, token: str = Depends(require_admin)):
    """
    Start 2FA setup - generates a new TOTP secret and QR code.

    The secret is stored temporarily until verified. User must scan QR code
    and verify with a code from their authenticator app.

    Returns:
        - secret: Base32 secret for manual entry
        - qr_code: Base64-encoded PNG QR code
        - uri: otpauth:// URI for the QR code
    """
    admin = db.get_admin()
    if not admin:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Admin account not found"
        )

    # Check if already enabled
    if admin.get("totp_enabled"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="2FA is already enabled. Disable it first to reconfigure."
        )

    # Generate new secret
    secret = totp_service.generate_secret()

    # Generate URI and QR code
    uri = totp_service.get_totp_uri(secret, admin["username"])
    qr_code = totp_service.generate_qr_code(uri)

    # Store secret temporarily (not enabled yet)
    # We store it in the database but with totp_enabled=False
    db.update_admin_totp(totp_secret=secret, totp_enabled=False, recovery_codes=None)

    # Log the setup attempt
    audit_service.log_2fa_setup_started(request, admin["username"])

    logger.info(f"2FA setup started for admin user: {admin['username']}")

    return TwoFactorSetupResponse(
        secret=secret,
        qr_code=qr_code,
        uri=uri
    )


@router.post("/2fa/verify")
async def verify_and_enable_2fa(
    request: Request,
    verify_data: TwoFactorVerifyRequest,
    token: str = Depends(require_admin)
):
    """
    Verify TOTP code and enable 2FA.

    User must provide a valid 6-digit code from their authenticator app
    to complete setup. On success, recovery codes are generated.

    Returns:
        - status: "ok"
        - recovery_codes: List of one-time backup codes (save these!)
    """
    admin = db.get_admin()
    if not admin:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Admin account not found"
        )

    # Check if already enabled
    if admin.get("totp_enabled"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="2FA is already enabled"
        )

    # Check if setup was started (secret exists)
    if not admin.get("totp_secret"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="2FA setup not started. Call /2fa/setup first."
        )

    # Verify the code
    if not totp_service.verify_totp(admin["totp_secret"], verify_data.code):
        audit_service.log_2fa_verification_failure(request, admin["username"])
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid verification code. Please try again."
        )

    # Generate recovery codes
    plaintext_codes, hashed_codes = totp_service.generate_recovery_codes(10)
    recovery_codes_json = json.dumps(hashed_codes)

    # Enable 2FA
    db.update_admin_totp(
        totp_secret=admin["totp_secret"],
        totp_enabled=True,
        recovery_codes=recovery_codes_json
    )

    # Log successful setup
    audit_service.log_2fa_enabled(request, admin["username"])

    logger.info(f"2FA enabled for admin user: {admin['username']}")

    return {
        "status": "ok",
        "message": "2FA has been enabled. Save your recovery codes!",
        "recovery_codes": plaintext_codes
    }


@router.post("/2fa/disable")
async def disable_2fa(
    request: Request,
    disable_data: TwoFactorDisableRequest,
    token: str = Depends(require_admin)
):
    """
    Disable 2FA for the admin account.

    Requires both current TOTP code and password for security.
    """
    admin = db.get_admin()
    if not admin:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Admin account not found"
        )

    # Check if 2FA is enabled
    if not admin.get("totp_enabled"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="2FA is not enabled"
        )

    # Verify password
    if not auth_service.verify_password(disable_data.password, admin["password_hash"]):
        audit_service.log_password_change_failed(request, admin["username"], "invalid_password")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid password"
        )

    # Verify TOTP code
    if not totp_service.verify_totp(admin["totp_secret"], disable_data.code):
        audit_service.log_2fa_verification_failure(request, admin["username"])
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid verification code"
        )

    # Disable 2FA
    db.update_admin_totp(totp_secret=None, totp_enabled=False, recovery_codes=None)

    # Log
    audit_service.log_2fa_disabled(request, admin["username"])

    logger.info(f"2FA disabled for admin user: {admin['username']}")

    return {
        "status": "ok",
        "message": "2FA has been disabled"
    }


@router.post("/2fa/recovery-codes", response_model=RecoveryCodesResponse)
async def regenerate_recovery_codes(
    request: Request,
    token: str = Depends(require_admin)
):
    """
    Generate new recovery codes.

    This invalidates all existing recovery codes. Requires 2FA to be enabled.
    """
    admin = db.get_admin()
    if not admin:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Admin account not found"
        )

    # Check if 2FA is enabled
    if not admin.get("totp_enabled"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="2FA must be enabled to generate recovery codes"
        )

    # Generate new recovery codes
    plaintext_codes, hashed_codes = totp_service.generate_recovery_codes(10)
    recovery_codes_json = json.dumps(hashed_codes)

    # Update database
    db.update_admin_recovery_codes(recovery_codes_json)

    # Log
    audit_service.log_recovery_codes_regenerated(request, admin["username"])

    logger.info(f"Recovery codes regenerated for admin user: {admin['username']}")

    return RecoveryCodesResponse(
        codes=plaintext_codes,
        count=len(plaintext_codes)
    )


# ============================================================================
# Audit Log
# ============================================================================

@router.get("/audit-log", response_model=AuditLogResponse)
async def get_audit_log(
    request: Request,
    event_type: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    token: str = Depends(require_admin)
):
    """
    Get audit log entries (admin only).

    Optional filters:
        - event_type: Filter by specific event type
        - limit: Maximum entries to return (default 50, max 500)
        - offset: Pagination offset
    """
    # Cap limit at 500
    limit = min(limit, 500)

    # Get logs
    entries = db.get_audit_logs(
        event_type=event_type,
        limit=limit,
        offset=offset
    )

    # Get total count
    total = db.get_audit_log_count(event_type=event_type)

    # Convert to response model
    audit_entries = [
        AuditLogEntry(
            id=entry["id"],
            user_id=entry.get("user_id"),
            user_type=entry.get("user_type", "admin"),
            event_type=entry["event_type"],
            ip_address=entry.get("ip_address"),
            user_agent=entry.get("user_agent"),
            details=entry.get("details"),
            created_at=entry["created_at"]
        )
        for entry in entries
    ]

    return AuditLogResponse(
        entries=audit_entries,
        total=total,
        limit=limit,
        offset=offset
    )
