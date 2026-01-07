"""
Authentication service for AI Hub
"""

import os
import secrets
import logging
import subprocess
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

import bcrypt

from app.db import database
from app.core.config import settings

logger = logging.getLogger(__name__)


class AuthService:
    """Handles both web UI authentication and Claude CLI authentication"""

    def __init__(self):
        """Initialize auth service"""
        self.config_dir = Path(os.environ.get('HOME', '/home/appuser')) / '.claude'

    # =========================================================================
    # Web UI Authentication
    # =========================================================================

    def is_setup_required(self) -> bool:
        """Check if initial setup is required"""
        return database.is_setup_required()

    def setup_admin(self, username: str, password: str) -> Dict[str, Any]:
        """Create the admin account (first-run only)"""
        if not self.is_setup_required():
            raise ValueError("Admin already configured")

        # Hash password with bcrypt
        password_hash = bcrypt.hashpw(
            password.encode('utf-8'),
            bcrypt.gensalt()
        ).decode('utf-8')

        admin = database.create_admin(username, password_hash)

        # Create session token
        token = self.create_session()

        return {
            "admin": admin,
            "token": token
        }

    def login(self, username: str, password: str) -> Optional[str]:
        """Verify credentials and create session"""
        admin = database.get_admin()
        if not admin:
            return None

        if admin["username"] != username:
            return None

        # Verify password
        if not bcrypt.checkpw(
            password.encode('utf-8'),
            admin["password_hash"].encode('utf-8')
        ):
            return None

        return self.create_session()

    def create_session(self) -> str:
        """Create a new auth session token"""
        token = secrets.token_urlsafe(32)
        expires_at = datetime.utcnow() + timedelta(days=settings.session_expire_days)
        database.create_auth_session(token, expires_at)
        return token

    def validate_session(self, token: str) -> bool:
        """Validate a session token"""
        if not token:
            return False
        session = database.get_auth_session(token)
        return session is not None

    def logout(self, token: str):
        """Invalidate a session"""
        if token:
            database.delete_auth_session(token)

    def get_admin_username(self) -> Optional[str]:
        """Get the admin username"""
        admin = database.get_admin()
        return admin["username"] if admin else None

    # =========================================================================
    # Claude CLI Authentication
    # =========================================================================

    def is_claude_authenticated(self) -> bool:
        """Check if Claude CLI is authenticated"""
        creds_file = self.config_dir / '.credentials.json'

        logger.debug(f"Checking for credentials at: {creds_file}")

        if not creds_file.exists():
            logger.debug("Credentials file does not exist")
            return False

        # Check if file has content
        if creds_file.stat().st_size == 0:
            logger.debug("Credentials file is empty")
            return False

        logger.debug("Credentials file exists and has content")
        return True

    def get_claude_auth_info(self) -> Dict[str, Any]:
        """Get Claude CLI authentication info"""
        return {
            "authenticated": self.is_claude_authenticated(),
            "config_dir": str(self.config_dir),
            "credentials_file": str(self.config_dir / '.credentials.json')
        }

    def get_login_instructions(self) -> Dict[str, Any]:
        """Get instructions for Claude CLI login"""
        if self.is_claude_authenticated():
            return {
                "status": "authenticated",
                "message": "Already authenticated with Claude Code"
            }

        return {
            "status": "not_authenticated",
            "message": "Claude Code login required",
            "instructions": [
                "1. Access the container: docker exec -it claude-sdk-agent /bin/bash",
                "2. Run: claude login",
                "3. Follow the OAuth prompts in your browser",
                "4. Return here and refresh"
            ],
            "command": "docker exec -it claude-sdk-agent claude login"
        }

    def claude_logout(self) -> Dict[str, Any]:
        """Logout from Claude CLI"""
        try:
            home_env = os.environ.get('HOME', '/home/appuser')
            result = subprocess.run(
                ['claude', 'logout'],
                capture_output=True,
                text=True,
                timeout=30,
                env={**os.environ, 'HOME': home_env}
            )

            if result.returncode == 0:
                return {
                    "success": True,
                    "message": "Logged out from Claude Code"
                }
            else:
                return {
                    "success": False,
                    "message": "Logout failed",
                    "error": result.stderr
                }
        except Exception as e:
            logger.error(f"Claude logout error: {e}")
            return {
                "success": False,
                "message": "Logout failed",
                "error": str(e)
            }

    # =========================================================================
    # Combined Status
    # =========================================================================

    def get_auth_status(self) -> Dict[str, Any]:
        """Get complete authentication status"""
        setup_required = self.is_setup_required()
        claude_auth = self.is_claude_authenticated()

        return {
            "setup_required": setup_required,
            "authenticated": False,  # Set by middleware based on session
            "claude_authenticated": claude_auth,
            "username": self.get_admin_username() if not setup_required else None
        }


# Global auth service instance
auth_service = AuthService()
