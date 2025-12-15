"""
Authentication API routes
"""

import os
import hashlib
import secrets
import subprocess
import logging
from pathlib import Path
from typing import Optional, Tuple
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Response, Request, Depends, status

from app.core.models import (
    SetupRequest, LoginRequest, AuthStatus, HealthResponse, ApiKeyLoginRequest,
    ApiUserRegisterRequest, ApiUserLoginRequest, ChangePasswordRequest,
    TwoFactorLoginRequest
)
import bcrypt
import json
from app.core.auth import auth_service
from app.core.config import settings
from app.core import encryption
from app.core import totp_service
from app.core import audit_service
from app.db import database as db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["Authentication"])


def get_client_ip(request: Request) -> str:
    """Get the real client IP address, respecting reverse proxy headers"""
    # Check trusted proxy headers in order
    trusted_headers = settings.trusted_proxy_headers.split(",")
    for header in trusted_headers:
        header = header.strip()
        value = request.headers.get(header)
        if value:
            # X-Forwarded-For can contain multiple IPs, take the first (original client)
            if "," in value:
                return value.split(",")[0].strip()
            return value.strip()
    # Fall back to direct client IP
    return request.client.host if request.client else "unknown"


def check_rate_limit(request: Request, username: Optional[str] = None) -> None:
    """Check if the request is rate limited. Raises HTTPException if blocked."""
    client_ip = get_client_ip(request)

    # Check if IP is locked out
    ip_lockout = db.is_ip_locked(client_ip)
    if ip_lockout:
        locked_until = datetime.fromisoformat(ip_lockout["locked_until"])
        remaining_minutes = int((locked_until - datetime.utcnow()).total_seconds() / 60)
        logger.warning(f"Blocked login attempt from locked IP: {client_ip}")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed login attempts. Try again in {remaining_minutes} minutes."
        )

    # Check if username is locked out (if provided)
    if username:
        username_lockout = db.is_username_locked(username)
        if username_lockout:
            locked_until = datetime.fromisoformat(username_lockout["locked_until"])
            remaining_minutes = int((locked_until - datetime.utcnow()).total_seconds() / 60)
            logger.warning(f"Blocked login attempt for locked user: {username}")
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Account temporarily locked. Try again in {remaining_minutes} minutes."
            )

    # Check current failed attempts count
    failed_count = db.get_failed_attempts_count(
        client_ip,
        settings.login_attempt_window_minutes
    )
    if failed_count >= settings.max_login_attempts:
        # Create lockout
        db.create_lockout(
            ip_address=client_ip,
            username=None,
            duration_minutes=settings.lockout_duration_minutes,
            reason="Too many failed login attempts from IP"
        )
        logger.warning(f"IP {client_ip} locked out after {failed_count} failed attempts")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed login attempts. Try again in {settings.lockout_duration_minutes} minutes."
        )


def record_login_result(request: Request, username: Optional[str], success: bool) -> None:
    """Record the result of a login attempt"""
    client_ip = get_client_ip(request)
    db.record_login_attempt(client_ip, username, success)

    if not success:
        # Check if we should lock out after this failure
        failed_count = db.get_failed_attempts_count(
            client_ip,
            settings.login_attempt_window_minutes
        )
        if failed_count >= settings.max_login_attempts:
            db.create_lockout(
                ip_address=client_ip,
                username=None,
                duration_minutes=settings.lockout_duration_minutes,
                reason="Too many failed login attempts from IP"
            )
            logger.warning(f"IP {client_ip} locked out after {failed_count} failed attempts")

        # Also check username-specific failures
        if username:
            username_failures = db.get_failed_attempts_for_username(
                username,
                settings.login_attempt_window_minutes
            )
            if username_failures >= settings.max_login_attempts:
                db.create_lockout(
                    ip_address=None,
                    username=username,
                    duration_minutes=settings.lockout_duration_minutes,
                    reason="Too many failed login attempts for username"
                )
                logger.warning(f"Username {username} locked out after {username_failures} failed attempts")


def get_session_token(request: Request) -> Optional[str]:
    """Extract session token from cookie"""
    return request.cookies.get("session")


def get_api_key(request: Request) -> Optional[str]:
    """Extract API key from Authorization header"""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    return None


def hash_api_key(api_key: str) -> str:
    """Hash an API key for lookup"""
    return hashlib.sha256(api_key.encode()).hexdigest()


def require_auth(request: Request) -> str:
    """Dependency that requires authentication (cookie, API key, or API key session)"""
    # First try cookie-based admin auth
    token = get_session_token(request)
    if token:
        # Check admin session
        if auth_service.validate_session(token):
            request.state.is_admin = True
            request.state.api_user = None
            return token

        # Check API key web session
        api_key_session = db.get_api_key_session(token)
        if api_key_session:
            api_user = db.get_api_user(api_key_session["api_user_id"])
            if api_user and api_user["is_active"]:
                request.state.is_admin = False
                request.state.api_user = api_user
                db.update_api_user_last_used(api_user["id"])
                return f"api_session:{api_user['id']}"

    # Then try API key auth (Bearer token)
    api_key = get_api_key(request)
    if api_key:
        key_hash = hash_api_key(api_key)
        api_user = db.get_api_user_by_key_hash(key_hash)
        if api_user:
            # Update last used timestamp
            db.update_api_user_last_used(api_user["id"])
            # Store API user info in request state for later use
            request.state.is_admin = False
            request.state.api_user = api_user
            return f"api_key:{api_user['id']}"

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated"
    )


def require_admin(request: Request) -> str:
    """Dependency that requires admin authentication only"""
    token = get_session_token(request)
    if token and auth_service.validate_session(token):
        request.state.is_admin = True
        request.state.api_user = None
        return token

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Admin access required"
    )


def get_api_user_from_request(request: Request) -> Optional[dict]:
    """Get API user from request state if authenticated via API key"""
    return getattr(request.state, "api_user", None)


def require_api_key(request: Request) -> dict:
    """
    Dependency that requires API key authentication only.
    Use this for /api/v1/ endpoints that should only be accessible via API keys,
    not admin sessions. Returns the API user dict.
    """
    # First check for Bearer token (direct API key)
    api_key = get_api_key(request)
    if api_key:
        key_hash = hash_api_key(api_key)
        api_user = db.get_api_user_by_key_hash(key_hash)
        if api_user and api_user.get("is_active", True):
            db.update_api_user_last_used(api_user["id"])
            request.state.is_admin = False
            request.state.api_user = api_user
            return api_user

    # Then check for API key web session (cookie-based API key login)
    token = get_session_token(request)
    if token:
        api_key_session = db.get_api_key_session(token)
        if api_key_session:
            api_user = db.get_api_user(api_key_session["api_user_id"])
            if api_user and api_user.get("is_active", True):
                db.update_api_user_last_used(api_user["id"])
                request.state.is_admin = False
                request.state.api_user = api_user
                return api_user

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="API key required. Use Bearer token or login with API key."
    )


def is_admin_request(request: Request) -> bool:
    """Check if the current request is from an admin user"""
    return getattr(request.state, "is_admin", False)


@router.get("/status")
async def get_auth_status(request: Request):
    """Get complete authentication status"""
    token = get_session_token(request)
    is_admin_authenticated = False
    is_api_user_authenticated = False
    api_user_info = None
    username = None

    if token:
        # Check admin session
        if auth_service.validate_session(token):
            is_admin_authenticated = True
            username = auth_service.get_admin_username()
        else:
            # Check API key web session
            api_key_session = db.get_api_key_session(token)
            if api_key_session:
                is_api_user_authenticated = True
                api_user_info = {
                    "id": api_key_session["api_user_id"],
                    "name": api_key_session["user_name"],
                    "project_id": api_key_session["project_id"],
                    "profile_id": api_key_session["profile_id"]
                }

    return {
        "authenticated": is_admin_authenticated or is_api_user_authenticated,
        "is_admin": is_admin_authenticated,
        "setup_required": auth_service.is_setup_required(),
        "claude_authenticated": auth_service.is_claude_authenticated(),
        "github_authenticated": auth_service.is_github_authenticated(),
        "username": username,
        "api_user": api_user_info
    }


@router.post("/setup")
async def setup_admin(request: SetupRequest, response: Response):
    """First-launch admin creation"""
    if not auth_service.is_setup_required():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin already configured"
        )

    try:
        result = auth_service.setup_admin(request.username, request.password)

        # Set encryption key in memory (derived from password)
        try:
            encryption.set_encryption_key(request.password, db)
        except Exception as e:
            logger.error(f"Failed to set up encryption during admin setup: {e}")

        # Set session cookie
        response.set_cookie(
            key="session",
            value=result["token"],
            httponly=True,
            secure=settings.cookie_secure,
            samesite="lax",
            max_age=settings.session_expire_days * 24 * 60 * 60
        )

        return {
            "status": "ok",
            "message": "Admin account created",
            "username": result["admin"]["username"]
        }

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )


@router.post("/login")
async def login(req: Request, login_data: LoginRequest, response: Response):
    """Login and get session cookie"""
    # Check rate limiting before attempting login
    check_rate_limit(req, login_data.username)

    token = auth_service.login(login_data.username, login_data.password)

    if not token:
        # Record failed attempt
        record_login_result(req, login_data.username, success=False)
        audit_service.log_login_failure(req, login_data.username, "invalid_credentials")
        client_ip = get_client_ip(req)
        logger.warning(f"Failed login attempt for user '{login_data.username}' from IP {client_ip}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials"
        )

    # Check if 2FA is enabled
    admin = db.get_admin()
    if admin and admin.get("totp_enabled"):
        # Create a pending 2FA session instead of completing login
        pending_token = secrets.token_urlsafe(32)
        expires_at = datetime.utcnow() + timedelta(minutes=5)  # 5 min to complete 2FA
        db.create_pending_2fa_session(pending_token, login_data.username, expires_at)

        client_ip = get_client_ip(req)
        logger.info(f"2FA required for user '{login_data.username}' from IP {client_ip}")

        return {
            "status": "2fa_required",
            "message": "Two-factor authentication required",
            "requires_2fa": True,
            "pending_2fa_token": pending_token,
            "is_admin": True
        }

    # No 2FA - complete login normally
    # Record successful login
    record_login_result(req, login_data.username, success=True)
    audit_service.log_login_success(req, login_data.username, "admin", "password")
    client_ip = get_client_ip(req)
    logger.info(f"Successful admin login for user '{login_data.username}' from IP {client_ip}")

    # Set encryption key in memory (derived from password)
    try:
        encryption.set_encryption_key(login_data.password, db)
        # Encrypt any existing plaintext API keys (migration)
        encrypted_count = encryption.encrypt_existing_plaintext_keys(db)
        if encrypted_count > 0:
            logger.info(f"Encrypted {encrypted_count} existing plaintext API keys")
    except Exception as e:
        logger.error(f"Failed to set up encryption: {e}")
        # Don't fail login, but log the error

    # Set session cookie
    response.set_cookie(
        key="session",
        value=token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=settings.session_expire_days * 24 * 60 * 60
    )

    return {"status": "ok", "message": "Logged in", "is_admin": True}


@router.post("/login/api-key")
async def login_with_api_key(req: Request, login_data: ApiKeyLoginRequest, response: Response):
    """Login to web UI using an API key - creates a restricted session"""
    # Check rate limiting
    check_rate_limit(req)

    # Validate API key
    key_hash = hash_api_key(login_data.api_key)
    api_user = db.get_api_user_by_key_hash(key_hash)

    if not api_user:
        # Record failed attempt
        record_login_result(req, None, success=False)
        client_ip = get_client_ip(req)
        logger.warning(f"Failed API key login attempt from IP {client_ip}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key"
        )

    if not api_user["is_active"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API key is disabled"
        )

    # Create API key web session
    session_token = secrets.token_urlsafe(32)
    expires_at = datetime.utcnow() + timedelta(hours=settings.api_key_session_expire_hours)
    db.create_api_key_session(session_token, api_user["id"], expires_at)

    # Record successful login
    record_login_result(req, f"api_user:{api_user['id']}", success=True)
    db.update_api_user_last_used(api_user["id"])
    client_ip = get_client_ip(req)
    logger.info(f"Successful API key login for user '{api_user['name']}' from IP {client_ip}")

    # Set session cookie
    response.set_cookie(
        key="session",
        value=session_token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=settings.api_key_session_expire_hours * 60 * 60
    )

    return {
        "status": "ok",
        "message": "Logged in",
        "is_admin": False,
        "api_user": {
            "id": api_user["id"],
            "name": api_user["name"],
            "project_id": api_user["project_id"],
            "profile_id": api_user["profile_id"]
        }
    }


@router.post("/register/api-user")
async def register_api_user(req: Request, register_data: ApiUserRegisterRequest, response: Response):
    """
    Register as an API user by claiming an API key with a username and password.

    The API key must be pre-created by an admin. This endpoint allows the end user
    to claim that API key by setting their own username and password. After registration,
    the user can login with just username/password.
    """
    # Check rate limiting
    check_rate_limit(req, register_data.username)

    # Validate API key format
    if not register_data.api_key.startswith("aih_"):
        record_login_result(req, register_data.username, success=False)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid API key format"
        )

    # Check if username is already taken
    if db.is_username_taken(register_data.username):
        record_login_result(req, register_data.username, success=False)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username already taken"
        )

    # Hash the API key to look it up
    key_hash = hash_api_key(register_data.api_key)

    # Check if API key exists
    api_user = db.get_api_user_by_key_hash(key_hash)
    if not api_user:
        record_login_result(req, register_data.username, success=False)
        client_ip = get_client_ip(req)
        logger.warning(f"Registration attempt with invalid API key from IP {client_ip}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid API key"
        )

    # Check if API key is already claimed
    if api_user.get("username"):
        record_login_result(req, register_data.username, success=False)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This API key has already been registered"
        )

    # Check if API user is active
    if not api_user.get("is_active", True):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This API key is disabled"
        )

    # Check if web login is allowed for this API user
    if not api_user.get("web_login_allowed", True):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Web login is not allowed for this API key. Use the API key directly for API access."
        )

    # Hash the password with bcrypt
    password_hash = bcrypt.hashpw(
        register_data.password.encode('utf-8'),
        bcrypt.gensalt()
    ).decode('utf-8')

    # Claim the API key
    updated_user = db.claim_api_key(key_hash, register_data.username, password_hash)
    if not updated_user:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to register. Please try again."
        )

    # Create session for the newly registered user
    session_token = secrets.token_urlsafe(32)
    expires_at = datetime.utcnow() + timedelta(hours=settings.api_key_session_expire_hours)
    db.create_api_key_session(session_token, updated_user["id"], expires_at)

    # Record successful registration
    record_login_result(req, register_data.username, success=True)
    db.update_api_user_last_used(updated_user["id"])
    client_ip = get_client_ip(req)
    logger.info(f"Successful API user registration for '{register_data.username}' from IP {client_ip}")

    # Set session cookie
    response.set_cookie(
        key="session",
        value=session_token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=settings.api_key_session_expire_hours * 60 * 60
    )

    return {
        "status": "ok",
        "message": "Registration successful",
        "is_admin": False,
        "api_user": {
            "id": updated_user["id"],
            "name": updated_user["name"],
            "username": updated_user["username"],
            "project_id": updated_user["project_id"],
            "profile_id": updated_user["profile_id"]
        }
    }


@router.post("/login/api-user")
async def login_api_user(req: Request, login_data: ApiUserLoginRequest, response: Response):
    """
    Login as an API user using username and password.

    This is for API users who have already registered (claimed their API key
    with a username and password).
    """
    # Check rate limiting
    check_rate_limit(req, login_data.username)

    # Look up user by username
    api_user = db.get_api_user_by_username(login_data.username)

    if not api_user:
        record_login_result(req, login_data.username, success=False)
        client_ip = get_client_ip(req)
        logger.warning(f"Failed API user login attempt for '{login_data.username}' from IP {client_ip}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password"
        )

    # Check if user is active
    if not api_user.get("is_active", True):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is disabled"
        )

    # Check if web login is allowed
    if not api_user.get("web_login_allowed", True):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Web login is not allowed for this account. Use your API key directly for API access."
        )

    # Verify password
    if not api_user.get("password_hash"):
        # User exists but hasn't set up password (shouldn't happen, but handle it)
        record_login_result(req, login_data.username, success=False)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password"
        )

    try:
        password_valid = bcrypt.checkpw(
            login_data.password.encode('utf-8'),
            api_user["password_hash"].encode('utf-8')
        )
    except Exception:
        password_valid = False

    if not password_valid:
        record_login_result(req, login_data.username, success=False)
        client_ip = get_client_ip(req)
        logger.warning(f"Failed API user login attempt for '{login_data.username}' from IP {client_ip}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password"
        )

    # Create session
    session_token = secrets.token_urlsafe(32)
    expires_at = datetime.utcnow() + timedelta(hours=settings.api_key_session_expire_hours)
    db.create_api_key_session(session_token, api_user["id"], expires_at)

    # Record successful login
    record_login_result(req, login_data.username, success=True)
    db.update_api_user_last_used(api_user["id"])
    client_ip = get_client_ip(req)
    logger.info(f"Successful API user login for '{login_data.username}' from IP {client_ip}")

    # Set session cookie
    response.set_cookie(
        key="session",
        value=session_token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=settings.api_key_session_expire_hours * 60 * 60
    )

    return {
        "status": "ok",
        "message": "Logged in",
        "is_admin": False,
        "api_user": {
            "id": api_user["id"],
            "name": api_user["name"],
            "username": api_user["username"],
            "project_id": api_user["project_id"],
            "profile_id": api_user["profile_id"]
        }
    }


@router.post("/logout")
async def logout(request: Request, response: Response):
    """Logout and invalidate session"""
    token = get_session_token(request)
    if token:
        # Try to logout from admin session
        auth_service.logout(token)
        # Also try to delete API key session if exists
        db.delete_api_key_session(token)

    # Note: We don't clear the encryption key on logout to allow
    # the app to continue functioning for API users

    response.delete_cookie(key="session")
    return {"status": "ok", "message": "Logged out"}


@router.post("/verify-2fa")
async def verify_2fa(
    req: Request,
    verify_data: TwoFactorLoginRequest,
    response: Response
):
    """
    Complete login by verifying 2FA code.

    After password authentication returns requires_2fa=True, the client
    must call this endpoint with the pending_2fa_token and a valid
    TOTP code or recovery code.
    """
    # Get pending token from request body or header
    pending_token = req.headers.get("X-2FA-Token")
    if not pending_token:
        try:
            body = await req.json()
            pending_token = body.get("pending_2fa_token")
        except Exception:
            pass

    if not pending_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing pending 2FA token"
        )

    # Validate pending session
    pending_session = db.get_pending_2fa_session(pending_token)
    if not pending_session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired 2FA session. Please login again."
        )

    username = pending_session["username"]

    # Get admin to verify 2FA
    admin = db.get_admin()
    if not admin or admin["username"] != username:
        db.delete_pending_2fa_session(pending_token)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session"
        )

    # Verify the code
    verified = False
    method = "totp"

    if verify_data.use_recovery_code:
        # Try recovery code
        method = "recovery_code"
        is_valid, used_hash = totp_service.verify_recovery_code(
            admin.get("recovery_codes", "[]"),
            verify_data.code
        )
        if is_valid and used_hash:
            # Remove used recovery code
            updated_codes = totp_service.remove_used_recovery_code(
                admin["recovery_codes"],
                used_hash
            )
            db.update_admin_recovery_codes(updated_codes)
            remaining = totp_service.get_recovery_codes_count(updated_codes)
            audit_service.log_recovery_code_used(req, username, remaining)
            verified = True
            logger.info(f"Recovery code used for user '{username}', {remaining} codes remaining")
    else:
        # Try TOTP code
        if totp_service.verify_totp(admin.get("totp_secret", ""), verify_data.code):
            verified = True

    if not verified:
        audit_service.log_2fa_verification_failure(req, username, method)
        client_ip = get_client_ip(req)
        logger.warning(f"Failed 2FA verification for user '{username}' from IP {client_ip}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid verification code"
        )

    # 2FA verified - complete login
    db.delete_pending_2fa_session(pending_token)

    # Create session
    token = auth_service.create_session()

    # Record successful login
    record_login_result(req, username, success=True)
    audit_service.log_login_success(req, username, "admin", f"password+{method}")
    audit_service.log_2fa_verification_success(req, username, method)
    client_ip = get_client_ip(req)
    logger.info(f"Successful 2FA login for user '{username}' from IP {client_ip}")

    # Set session cookie
    response.set_cookie(
        key="session",
        value=token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=settings.session_expire_days * 24 * 60 * 60
    )

    return {"status": "ok", "message": "Logged in", "is_admin": True}


@router.post("/change-password")
async def change_password(
    req: Request,
    password_data: ChangePasswordRequest,
    token: str = Depends(require_admin)
):
    """
    Change admin password and re-encrypt all sensitive data.

    This endpoint:
    1. Verifies the current password
    2. Decrypts all sensitive data with the old password
    3. Re-encrypts all sensitive data with the new password
    4. Updates the admin password hash
    """
    # Get current admin info
    admin = db.get_admin()
    if not admin:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Admin account not found"
        )

    # Verify current password
    if not auth_service.verify_password(password_data.current_password, admin["password_hash"]):
        client_ip = get_client_ip(req)
        logger.warning(f"Failed password change attempt - wrong current password from IP {client_ip}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Current password is incorrect"
        )

    # Check if new password is same as old
    if password_data.current_password == password_data.new_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must be different from current password"
        )

    # Re-encrypt all secrets with new password
    try:
        success = encryption.re_encrypt_all_secrets(
            password_data.current_password,
            password_data.new_password,
            db
        )
        if not success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to re-encrypt secrets. Password not changed."
            )
    except Exception as e:
        logger.error(f"Failed to re-encrypt secrets: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to re-encrypt secrets. Password not changed."
        )

    # Update admin password
    new_password_hash = bcrypt.hashpw(
        password_data.new_password.encode('utf-8'),
        bcrypt.gensalt()
    ).decode('utf-8')

    db.update_admin_password(new_password_hash)

    client_ip = get_client_ip(req)
    logger.info(f"Admin password changed successfully from IP {client_ip}")

    return {
        "status": "ok",
        "message": "Password changed successfully. All encrypted data has been re-encrypted."
    }


@router.get("/claude/status")
async def claude_auth_status():
    """Get Claude CLI authentication status"""
    return auth_service.get_claude_auth_info()


@router.get("/claude/validate")
async def validate_claude_credentials(token: str = Depends(require_admin)):
    """
    Validate Claude credentials by testing the CLI.
    Use this to check if OAuth token is still valid (not just if file exists).

    Returns:
        - valid: boolean - True if credentials work
        - authenticated: boolean - True if credentials file exists
        - error: string - Error message if validation failed
    """
    return auth_service.validate_claude_credentials()


@router.get("/claude/login-instructions")
async def claude_login_instructions():
    """Get Claude CLI login instructions"""
    return auth_service.get_login_instructions()


@router.post("/claude/login")
async def claude_login(request: Request, token: str = Depends(require_admin)):
    """
    Start Claude Code OAuth login process.
    Returns an OAuth URL that the user should open in their browser.

    Accepts optional JSON body with:
    - force_reauth: boolean - If true, delete existing credentials and force re-authentication.
                             Use when OAuth token has expired or user wants to reconnect.
    """
    force_reauth = False
    try:
        body = await request.json()
        force_reauth = body.get("force_reauth", False)
    except Exception:
        pass  # No body or invalid JSON, use defaults

    return auth_service.start_claude_oauth_login(force_reauth=force_reauth)


@router.get("/claude/login/poll")
async def claude_login_poll(token: str = Depends(require_admin)):
    """
    Poll for Claude authentication status after user completes OAuth flow.
    Returns when authentication is detected or after a short timeout.
    """
    # Short poll - check once with a brief delay for UI responsiveness
    import asyncio
    await asyncio.sleep(1)
    if auth_service.is_claude_authenticated():
        return {
            "success": True,
            "authenticated": True,
            "message": "Successfully authenticated with Claude Code"
        }
    return {
        "success": False,
        "authenticated": False,
        "message": "Not yet authenticated. Continue polling or try again."
    }


@router.post("/claude/complete")
async def claude_login_complete(request: Request, token: str = Depends(require_admin)):
    """
    Complete the Claude OAuth login by providing the authorization code and state.

    After starting the login with /auth/claude/login and getting an OAuth URL,
    the user visits the URL in their browser, authenticates, and receives a code.
    They then call this endpoint with that code and the state to complete the login flow.

    Expects JSON body with:
    - 'code': The authorization code from the OAuth callback
    - 'state': The state parameter from the original /auth/claude/login response
    """
    try:
        body = await request.json()
        auth_code = body.get("code")
        state = body.get("state")

        if not auth_code:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Authorization code is required"
            )
        if not state:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="State parameter is required"
            )

        return await auth_service.complete_claude_oauth_login(auth_code, state)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Claude complete login error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )


@router.post("/claude/logout")
async def claude_logout(token: str = Depends(require_admin)):
    """Logout from Claude CLI"""
    return auth_service.claude_logout()


# =========================================================================
# GitHub CLI Authentication
# =========================================================================

@router.get("/github/status")
async def github_auth_status():
    """Get GitHub CLI authentication status"""
    return auth_service.get_github_auth_info()


@router.post("/github/login")
async def github_login(request: Request, token: str = Depends(require_admin)):
    """
    Login to GitHub CLI using a personal access token.
    Expects JSON body with 'token' field containing the GitHub PAT.
    """
    try:
        body = await request.json()
        gh_token = body.get("token")
        if not gh_token:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="GitHub token is required"
            )
        return auth_service.github_login_with_token(gh_token)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"GitHub login error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )


@router.post("/github/logout")
async def github_logout(token: str = Depends(require_admin)):
    """Logout from GitHub CLI"""
    return auth_service.github_logout()


@router.get("/diagnostics")
async def auth_diagnostics(token: str = Depends(require_admin)):
    """Run diagnostic checks for authentication issues (Admin only)"""
    diagnostics = {}

    # Check HOME environment variable
    diagnostics["home_env"] = os.environ.get("HOME", "NOT SET")

    # Check if claude command exists
    try:
        result = subprocess.run(
            ['which', 'claude'],
            capture_output=True,
            text=True,
            timeout=5
        )
        diagnostics["claude_path"] = result.stdout.strip() if result.returncode == 0 else "NOT FOUND"
    except Exception as e:
        diagnostics["claude_path"] = f"ERROR: {str(e)}"

    # Check config locations
    home_dir = Path(os.environ.get('HOME', '/home/appuser'))

    # ~/.claude/ (newer Claude Code versions)
    claude_dir = home_dir / '.claude'
    diagnostics["claude_dir"] = str(claude_dir)
    diagnostics["claude_dir_exists"] = claude_dir.exists()

    if claude_dir.exists():
        creds_file = claude_dir / '.credentials.json'
        diagnostics["claude_credentials_exists"] = creds_file.exists()
        if creds_file.exists():
            diagnostics["credentials_file_size"] = creds_file.stat().st_size

    # Check process user
    try:
        import pwd
        diagnostics["process_user"] = pwd.getpwuid(os.getuid()).pw_name
        diagnostics["process_uid"] = os.getuid()
    except Exception as e:
        diagnostics["process_user_error"] = str(e)

    return diagnostics
