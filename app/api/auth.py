"""
Authentication API routes
"""

import os
import subprocess
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Response, Request, Depends, status

from app.core.models import (
    SetupRequest, LoginRequest, AuthStatus, HealthResponse
)
from app.core.auth import auth_service
from app.core.config import settings

router = APIRouter(prefix="/api/v1/auth", tags=["Authentication"])


def get_session_token(request: Request) -> Optional[str]:
    """Extract session token from cookie"""
    return request.cookies.get("session")


def require_auth(request: Request):
    """Dependency that requires authentication"""
    token = get_session_token(request)
    if not token or not auth_service.validate_session(token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated"
        )
    return token


@router.get("/status", response_model=AuthStatus)
async def get_auth_status(request: Request):
    """Get complete authentication status"""
    token = get_session_token(request)
    is_authenticated = auth_service.validate_session(token) if token else False

    return {
        "authenticated": is_authenticated,
        "setup_required": auth_service.is_setup_required(),
        "claude_authenticated": auth_service.is_claude_authenticated(),
        "username": auth_service.get_admin_username() if is_authenticated else None
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
async def login(request: LoginRequest, response: Response):
    """Login and get session cookie"""
    token = auth_service.login(request.username, request.password)

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials"
        )

    # Set session cookie
    response.set_cookie(
        key="session",
        value=token,
        httponly=True,
        secure=False,  # Set to True in production with HTTPS
        samesite="lax",
        max_age=settings.session_expire_days * 24 * 60 * 60
    )

    return {"status": "ok", "message": "Logged in"}


@router.post("/logout")
async def logout(request: Request, response: Response):
    """Logout and invalidate session"""
    token = get_session_token(request)
    if token:
        auth_service.logout(token)

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
