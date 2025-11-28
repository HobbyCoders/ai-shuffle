"""
Authentication service for AI Hub
"""

import os
import secrets
import logging
import subprocess
import json
import asyncio
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

import bcrypt

from app.db import database
from app.core.config import settings

logger = logging.getLogger(__name__)


class AuthService:
    """Handles both web UI authentication, Claude CLI, and GitHub CLI authentication"""

    def __init__(self):
        """Initialize auth service"""
        self.config_dir = Path(os.environ.get('HOME', '/home/appuser')) / '.claude'
        self.gh_config_dir = Path(os.environ.get('HOME', '/home/appuser')) / '.config' / 'gh'

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
    # GitHub CLI Authentication
    # =========================================================================

    def is_github_authenticated(self) -> bool:
        """Check if GitHub CLI is authenticated"""
        hosts_file = self.gh_config_dir / 'hosts.yml'

        if not hosts_file.exists():
            return False

        # Check if file has content
        if hosts_file.stat().st_size == 0:
            return False

        # Verify with gh auth status
        try:
            home_env = os.environ.get('HOME', '/home/appuser')
            result = subprocess.run(
                ['gh', 'auth', 'status'],
                capture_output=True,
                text=True,
                timeout=10,
                env={**os.environ, 'HOME': home_env}
            )
            return result.returncode == 0
        except Exception as e:
            logger.warning(f"GitHub auth status check failed: {e}")
            return False

    def get_github_auth_info(self) -> Dict[str, Any]:
        """Get GitHub CLI authentication info"""
        authenticated = self.is_github_authenticated()
        user = None

        if authenticated:
            try:
                home_env = os.environ.get('HOME', '/home/appuser')
                result = subprocess.run(
                    ['gh', 'api', 'user', '-q', '.login'],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    env={**os.environ, 'HOME': home_env}
                )
                if result.returncode == 0:
                    user = result.stdout.strip()
            except Exception as e:
                logger.warning(f"Could not get GitHub user: {e}")

        return {
            "authenticated": authenticated,
            "user": user,
            "config_dir": str(self.gh_config_dir)
        }

    def github_login_with_token(self, token: str) -> Dict[str, Any]:
        """Login to GitHub CLI using a personal access token"""
        try:
            home_env = os.environ.get('HOME', '/home/appuser')

            # Ensure config directory exists
            self.gh_config_dir.mkdir(parents=True, exist_ok=True)

            # Login with the token
            result = subprocess.run(
                ['gh', 'auth', 'login', '--with-token'],
                input=token,
                capture_output=True,
                text=True,
                timeout=30,
                env={**os.environ, 'HOME': home_env}
            )

            if result.returncode == 0:
                # Configure git credential helper
                subprocess.run(
                    ['git', 'config', '--global', 'credential.helper', '!gh auth git-credential'],
                    capture_output=True,
                    timeout=10,
                    env={**os.environ, 'HOME': home_env}
                )

                logger.info("GitHub CLI login successful")
                return {
                    "success": True,
                    "message": "Successfully logged in to GitHub"
                }
            else:
                error_msg = result.stderr.strip() if result.stderr else "Login failed"
                logger.warning(f"GitHub login failed: {error_msg}")
                return {
                    "success": False,
                    "message": "GitHub login failed",
                    "error": error_msg
                }

        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "message": "Login timed out",
                "error": "GitHub CLI login process timed out"
            }
        except Exception as e:
            logger.error(f"GitHub login error: {e}")
            return {
                "success": False,
                "message": "Login failed",
                "error": str(e)
            }

    def github_logout(self) -> Dict[str, Any]:
        """Logout from GitHub CLI"""
        try:
            home_env = os.environ.get('HOME', '/home/appuser')
            result = subprocess.run(
                ['gh', 'auth', 'logout', '--hostname', 'github.com'],
                input='Y\n',  # Confirm logout
                capture_output=True,
                text=True,
                timeout=30,
                env={**os.environ, 'HOME': home_env}
            )

            if result.returncode == 0:
                logger.info("GitHub CLI logout successful")
                return {
                    "success": True,
                    "message": "Logged out from GitHub"
                }
            else:
                # Even if gh returns error, try to remove config
                hosts_file = self.gh_config_dir / 'hosts.yml'
                if hosts_file.exists():
                    hosts_file.unlink()
                return {
                    "success": True,
                    "message": "Logged out from GitHub"
                }

        except Exception as e:
            logger.error(f"GitHub logout error: {e}")
            return {
                "success": False,
                "message": "Logout failed",
                "error": str(e)
            }

    # =========================================================================
    # Claude Code OAuth Login (in-app)
    # =========================================================================

    def start_claude_oauth_login(self) -> Dict[str, Any]:
        """
        Start Claude Code OAuth login process.
        Returns the OAuth URL for the user to complete in their browser.
        """
        try:
            home_env = os.environ.get('HOME', '/home/appuser')

            # Ensure config directory exists
            self.config_dir.mkdir(parents=True, exist_ok=True)

            # Start claude login in a way that captures the URL
            # The --no-open flag prevents auto-opening browser
            result = subprocess.run(
                ['claude', 'login', '--no-open'],
                capture_output=True,
                text=True,
                timeout=30,
                env={**os.environ, 'HOME': home_env}
            )

            # Parse output to find OAuth URL
            output = result.stdout + result.stderr

            # Look for URL in output
            import re
            url_match = re.search(r'(https://[^\s]+anthropic[^\s]+)', output)

            if url_match:
                return {
                    "success": True,
                    "oauth_url": url_match.group(1),
                    "message": "Open this URL in your browser to complete login"
                }
            elif result.returncode == 0:
                # Already logged in
                return {
                    "success": True,
                    "already_authenticated": True,
                    "message": "Already authenticated with Claude Code"
                }
            else:
                return {
                    "success": False,
                    "message": "Could not start OAuth login",
                    "error": output
                }

        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "message": "Login process timed out",
                "error": "Claude login process timed out"
            }
        except Exception as e:
            logger.error(f"Claude OAuth login error: {e}")
            return {
                "success": False,
                "message": "Login failed",
                "error": str(e)
            }

    async def poll_claude_auth_status(self, timeout_seconds: int = 300) -> Dict[str, Any]:
        """
        Poll for Claude authentication status after user completes OAuth flow.
        Returns when authentication is detected or timeout is reached.
        """
        start_time = datetime.now()
        poll_interval = 2  # seconds

        while (datetime.now() - start_time).total_seconds() < timeout_seconds:
            if self.is_claude_authenticated():
                return {
                    "success": True,
                    "authenticated": True,
                    "message": "Successfully authenticated with Claude Code"
                }
            await asyncio.sleep(poll_interval)

        return {
            "success": False,
            "authenticated": False,
            "message": "Authentication timed out. Please try again."
        }

    # =========================================================================
    # Combined Status
    # =========================================================================

    def get_auth_status(self) -> Dict[str, Any]:
        """Get complete authentication status"""
        setup_required = self.is_setup_required()
        claude_auth = self.is_claude_authenticated()
        github_auth = self.is_github_authenticated()

        return {
            "setup_required": setup_required,
            "authenticated": False,  # Set by middleware based on session
            "claude_authenticated": claude_auth,
            "github_authenticated": github_auth,
            "username": self.get_admin_username() if not setup_required else None
        }


# Global auth service instance
auth_service = AuthService()
