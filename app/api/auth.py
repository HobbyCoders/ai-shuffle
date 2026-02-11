"""
Authentication API routes
"""

import os
import hashlib
import secrets
import subprocess
import logging
import uuid as _uuid
from pathlib import Path
from typing import Optional
from datetime import datetime, timedelta

import bcrypt
from fastapi import APIRouter, HTTPException, Response, Request, Depends, status

from app.core.models import (
    SetupRequest, LoginRequest, ChangePasswordRequest,
    TwoFactorLoginRequest
)
from app.core.auth import auth_service
from app.core.config import settings
from app.core import encryption
from app.core import totp_service
from app.core import audit_service
from app.core import auth as _auth_module
from app.db import database as db

logger = logging.getLogger(__name__)

# Unique server instance ID to help debug multi-instance issues
_SERVER_INSTANCE_ID = _uuid.uuid4().hex[:8]

# Share instance ID with auth service for consistent logging
_auth_module.set_server_instance_id(_SERVER_INSTANCE_ID)

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
    """Dependency that requires authentication (cookie or API key)"""
    # First try cookie-based admin auth
    token = get_session_token(request)
    if token:
        # Check admin session
        if auth_service.validate_session(token):
            request.state.is_admin = True
            request.state.api_user = None
            return token

        # Diagnostic logging - session token exists but validation failed
        last4 = token[-4:] if len(token) >= 4 else token
        logger.warning(
            f"[{_SERVER_INSTANCE_ID}] Auth failed: token exists "
            f"(len={len(token)}, last4=...{last4}) "
            f"but validation failed. Path: {request.url.path}"
        )

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

    # Log what auth methods were attempted
    has_cookie = token is not None
    has_bearer = api_key is not None
    path = request.url.path
    logger.warning(
        f"[{_SERVER_INSTANCE_ID}] Auth failed completely: "
        f"cookie={has_cookie}, bearer={has_bearer}, path={path}"
    )

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
    api_key = get_api_key(request)
    if api_key:
        key_hash = hash_api_key(api_key)
        api_user = db.get_api_user_by_key_hash(key_hash)
        if api_user and api_user.get("is_active", True):
            db.update_api_user_last_used(api_user["id"])
            request.state.is_admin = False
            request.state.api_user = api_user
            return api_user

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="API key required. Use Bearer token."
    )


def is_admin_request(request: Request) -> bool:
    """Check if the current request is from an admin user"""
    return getattr(request.state, "is_admin", False)


def get_current_api_user(request: Request) -> dict:
    """
    Dependency that requires an API user to be authenticated via API key.
    Returns the API user dict.
    """
    api_key = get_api_key(request)
    if api_key:
        key_hash = hash_api_key(api_key)
        api_user = db.get_api_user_by_key_hash(key_hash)
        if api_user and api_user.get("is_active", True):
            db.update_api_user_last_used(api_user["id"])
            request.state.is_admin = False
            request.state.api_user = api_user
            return api_user

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="API key authentication required."
    )


@router.get("/status")
async def get_auth_status(request: Request):
    """Get complete authentication status"""
    token = get_session_token(request)
    is_admin_authenticated = False
    username = None

    if token:
        if auth_service.validate_session(token):
            is_admin_authenticated = True
            username = auth_service.get_admin_username()

    return {
        "authenticated": is_admin_authenticated,
        "is_admin": is_admin_authenticated,
        "setup_required": auth_service.is_setup_required(),
        "claude_authenticated": auth_service.is_claude_authenticated(),
        "github_authenticated": auth_service.is_github_authenticated(),
        "onboarding_complete": auth_service.is_onboarding_complete(),
        "username": username,
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

        # Mark that onboarding was started via the setup flow so
        # is_onboarding_complete() can distinguish new installs from upgrades
        db.set_system_setting("onboarding_started", "true")

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
        logger.debug(f"2FA required for user '{login_data.username}' from IP {client_ip}")

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


@router.post("/logout")
async def logout(request: Request, response: Response):
    """Logout and invalidate session"""
    token = get_session_token(request)
    if token:
        auth_service.logout(token)

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
            logger.debug(f"Recovery code used for user '{username}', {remaining} codes remaining")
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


@router.post("/github/login/web")
async def github_login_web(request: Request, token: str = Depends(require_admin)):
    """
    Start GitHub CLI device flow authentication (gh auth login --web).
    Returns a one-time code and verification URL for the user.
    Poll GET /auth/github/status for completion.
    """
    try:
        return await auth_service.github_login_web()
    except Exception as e:
        logger.error(f"GitHub web login error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )


@router.post("/github/logout")
async def github_logout(token: str = Depends(require_admin)):
    """Logout from GitHub CLI"""
    return auth_service.github_logout()


# =========================================================================
# Onboarding
# =========================================================================

@router.post("/onboarding/complete")
async def complete_onboarding(token: str = Depends(require_admin)):
    """Mark the onboarding flow as complete"""
    auth_service.set_onboarding_complete()
    return {"status": "ok", "message": "Onboarding completed"}


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
