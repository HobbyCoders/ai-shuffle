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
    SetupRequest, LoginRequest, AuthStatus, HealthResponse, ApiKeyLoginRequest
)
from app.core.auth import auth_service
from app.core.config import settings
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

        # Set session cookie
        response.set_cookie(
            key="session",
            value=result["token"],
            httponly=True,
            secure=False,  # Set to True in production with HTTPS
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
        client_ip = get_client_ip(req)
        logger.warning(f"Failed login attempt for user '{login_data.username}' from IP {client_ip}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials"
        )

    # Record successful login
    record_login_result(req, login_data.username, success=True)
    client_ip = get_client_ip(req)
    logger.info(f"Successful admin login for user '{login_data.username}' from IP {client_ip}")

    # Set session cookie
    response.set_cookie(
        key="session",
        value=token,
        httponly=True,
        secure=False,  # Set to True in production with HTTPS
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
        secure=False,  # Set to True in production with HTTPS
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


@router.post("/logout")
async def logout(request: Request, response: Response):
    """Logout and invalidate session"""
    token = get_session_token(request)
    if token:
        # Try to logout from admin session
        auth_service.logout(token)
        # Also try to delete API key session if exists
        db.delete_api_key_session(token)

    response.delete_cookie(key="session")
    return {"status": "ok", "message": "Logged out"}


@router.get("/claude/status")
async def claude_auth_status():
    """Get Claude CLI authentication status"""
    return auth_service.get_claude_auth_info()


@router.get("/claude/login-instructions")
async def claude_login_instructions():
    """Get Claude CLI login instructions"""
    return auth_service.get_login_instructions()


@router.post("/claude/logout")
async def claude_logout(token: str = Depends(require_auth)):
    """Logout from Claude CLI"""
    return auth_service.claude_logout()


@router.get("/diagnostics")
async def auth_diagnostics(token: str = Depends(require_auth)):
    """Run diagnostic checks for authentication issues"""
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
